import json
from types import SimpleNamespace

from ade_radar.alerts import AlertStats, template_params
from ade_radar.classify import Classifier
from ade_radar.match import Decision
from ade_radar.models import Classification, Item
from ade_radar.pipeline import process_items

from .test_classify import GOOD, FakeMessages


def flyer(n="1", author="socialpleasure"):
    return Item("instagram", f"https://www.instagram.com/p/{n}/", url=f"https://www.instagram.com/p/{n}/",
                author=author, text="ADE pop-up! Presale sign-up open now, link in bio")


def run(conn, ctx, items, wa, replies=None, dry=False):
    stats, counts = AlertStats(), {}
    clf = None
    if replies is not None:
        clf = Classifier(ctx, client=SimpleNamespace(messages=FakeMessages(replies)))
    process_items(conn, ctx, items, dry_run=dry, classifier=clf, wa=wa, stats=stats, counts=counts)
    return stats, counts


def test_fake_flyer_is_urgent_whatsapp_to_everyone(conn, ctx, fake_wa):
    stats, counts = run(conn, ctx, [flyer()], fake_wa, [json.dumps(GOOD)])
    assert stats.sent == 2 and counts["new_items"] == 1
    params = fake_wa.sent[0][1]
    assert params[0].startswith("Social Pleasure ADE pop-up")
    assert params[1] == "Thu 22 Oct 23:00" and params[2] == "TBA"
    assert params[3].startswith("URGENT: presale sign-up open")
    assert params[3].endswith("https://www.instagram.com/socialpleasure/")  # "link in bio" -> profile


def test_rerun_never_sends_twice(conn, ctx, fake_wa):
    run(conn, ctx, [flyer()], fake_wa, [json.dumps(GOOD)])
    stats, counts = run(conn, ctx, [flyer()], fake_wa, [json.dumps(GOOD)])
    assert stats.sent == 0 and counts["duplicates"] == 1 and len(fake_wa.sent) == 2


def test_same_event_from_second_account_is_deduped(conn, ctx, fake_wa):
    run(conn, ctx, [flyer("1")], fake_wa, [json.dumps(GOOD)])
    stats, _ = run(conn, ctx, [flyer("2", author="paigetomlinson")], fake_wa, [json.dumps(GOOD)])
    assert stats.sent == 0 and stats.skipped == 2


def test_failed_send_is_retried_next_time(conn, ctx):
    from .conftest import FakeWA
    wa = FakeWA(fail_codes=[500])
    stats, _ = run(conn, ctx, [flyer()], wa, [json.dumps(GOOD)])
    assert stats.failed == 1 and stats.sent == 1
    # next run: a new item for the same event; the failed recipient gets it, the other does not.
    stats2, _ = run(conn, ctx, [flyer("9", author="paigetomlinson")], wa, [json.dumps(GOOD)])
    assert stats2.sent == 1


def test_dry_run_sends_nothing(conn, ctx, fake_wa, capsys):
    stats, _ = run(conn, ctx, [flyer()], fake_wa, [json.dumps(GOOD)], dry=True)
    assert stats.dry_run == 2 and not fake_wa.sent
    assert "Up the scouse" in capsys.readouterr().out


def test_rules_fallback_without_claude(conn, ctx, fake_wa):
    stats, _ = run(conn, ctx, [flyer()], fake_wa, None)
    assert stats.sent == 2


def test_irrelevant_post_no_alert(conn, ctx, fake_wa):
    item = Item("instagram", "x", author="randomdj", text="Great night in Berlin")
    stats, _ = run(conn, ctx, [item], fake_wa, None)
    assert stats.sent == 0


def test_template_params_lengths():
    item = Item("instagram", "p", url="https://www.instagram.com/p/1/", author="a")
    cls = Classification(event_name="X" * 300, artists=["A"], venue="V" * 300)
    params = template_params(item, cls, Decision(True, "normal", ["jack"], "announce"))
    assert all(len(p) <= 150 for p in params) and all("\n" not in p for p in params)


def test_instagram_items_marked_seen_only_after_processing(conn, ctx, fake_wa):
    from ade_radar import db
    run(conn, ctx, [flyer("5")], fake_wa, [json.dumps(GOOD)])
    assert db.ig_is_seen(conn, "https://www.instagram.com/p/5/")
    run(conn, ctx, [flyer("6")], fake_wa, [json.dumps(GOOD)], dry=True)
    conn.rollback()
    assert not db.ig_is_seen(conn, "https://www.instagram.com/p/6/")


def test_every_alert_has_a_link():
    from ade_radar.alerts import best_link
    cls = Classification()
    assert best_link(Item("instagram", "p", url="https://www.instagram.com/p/X/", author="a"), cls) == \
        "https://www.instagram.com/p/X/"
    story = Item("instagram_story", "s", url="https://www.instagram.com/stories/kettama/123/", author="kettama")
    assert best_link(story, cls) == "https://www.instagram.com/kettama/"
    tickets = Classification(links=["https://dice.fm/event/x"])
    assert best_link(story, tickets) == "https://dice.fm/event/x"
    assert best_link(Item("gmail", "1", author="RA <x@ra.co>"), cls).startswith("https://")


def test_failed_whatsapp_goes_to_ntfy_once_and_is_retried(conn, ctx, monkeypatch):
    from .conftest import FakeWA
    from ade_radar.alerts import retry_failed
    from ade_radar.notify import ntfy
    notices = []
    monkeypatch.setattr(ntfy, "send", lambda s, msg, **kw: notices.append(kw["title"]) or True)
    wa = FakeWA(fail_codes=[131049, 131049])
    stats, _ = run(conn, ctx, [flyer("7")], wa, [json.dumps(GOOD)])
    assert stats.failed == 2 and len(notices) == 2  # one notice per person
    wa.fail = [131049]  # next run: jack still refused, friend1 goes through
    s2 = AlertStats()
    retry_failed(conn, ctx, wa, s2)
    assert s2.sent == 1 and s2.failed == 1 and len(notices) == 2  # no repeat notice
    s3 = AlertStats()
    retry_failed(conn, ctx, wa, s3)
    assert s3.sent == 1 and len(wa.sent) == 2
    assert retry_failed(conn, ctx, wa, AlertStats()) is None and len(wa.sent) == 2  # nothing left


def test_cli_source_list():
    import argparse
    import pytest
    from ade_radar.cli import _sources
    assert _sources("instagram,gmail") == ["instagram", "gmail"]
    assert _sources("all") == ["instagram", "gmail", "ade"]
    with pytest.raises(argparse.ArgumentTypeError):
        _sources("instagram,nope")
