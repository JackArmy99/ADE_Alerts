from ade_radar import db
from ade_radar.dedupe import upsert_event
from ade_radar.models import Classification
from ade_radar.reports import heartbeat_summary


def test_dedupe_same_night_and_venue(conn):
    a = Classification(event_name="Josh Baker Gashouder", date="2026-10-25", time="12:00", venue="Gashouder",
                       artists=["Josh Baker"])
    b = Classification(event_name="You&Me ADE", date="2026-10-25", venue="Gashouder Westergas")
    c = Classification(event_name="Late one", date="2026-10-26", time="02:00", venue="Gashouder")
    d = Classification(event_name="Other", date="2026-10-24", venue="Pllek")
    e1, new1 = upsert_event(conn, a, "i1")
    e2, new2 = upsert_event(conn, b, "i2")
    e3, _ = upsert_event(conn, c, "i3")   # 02:00 on the 26th = night of the 25th
    e4, _ = upsert_event(conn, d, "i4")
    assert new1 and not new2 and e1 == e2 == e3 and e4 != e1


def test_events_without_identity_never_merge(conn):
    e1, _ = upsert_event(conn, Classification(), "i1")
    e2, _ = upsert_event(conn, Classification(), "i2")
    assert e1 != e2


def test_heartbeat_summary(conn):
    r = db.start_run(conn, "instagram")
    db.finish_run(conn, r, True, {"posts_checked": 12, "claude_calls": 5})
    r = db.start_run(conn, "gmail")
    db.finish_run(conn, r, False, {}, "boom")
    text, healthy = heartbeat_summary(conn)
    assert "instagram 1/1 ok" in text and "gmail 0/1 ok" in text and "Claude calls 5" in text
    assert not healthy


def test_teasers_go_to_digest_once(ctx, conn, capsys):
    from ade_radar.reports import digest
    db.save_teaser(conn, "i1", "ewanmcvicar_", "https://www.instagram.com/p/DeZF5xVjEIL/",
                   "teaser: mentions an ADE show, no details", ["jack"])
    conn.commit()
    ctx.settings.wa_token = ""  # no WhatsApp in tests
    text = digest(ctx)
    assert "@ewanmcvicar_: teaser" in text and "DeZF5xVjEIL" in text
    assert "ewanmcvicar_" not in digest(ctx)  # teasers are only listed once


def test_email_report_measures_lag(conn):
    import json as _json
    from ade_radar.models import Item
    from ade_radar.reports import email_report
    ig = Item("instagram", "p1", author="joshbaker")
    ra = Item("gmail", "1:5", author="Resident Advisor <noreply@ra.co>")
    dice = Item("gmail", "1:6", author="DICE <hello@mail.dice.fm>")
    for it, t in ((ig, "2026-10-12T10:00:00+02:00"), (ra, "2026-10-12T13:00:00+02:00"),
                  (dice, "2026-10-12T09:00:00+02:00")):
        db.save_item(conn, it)
        conn.execute("UPDATE items SET first_seen=? WHERE id=?", (t, it.id))
    conn.execute("INSERT INTO events (title, sources) VALUES (?,?)", ("Josh Baker", _json.dumps([ig.id, ra.id, dice.id])))
    rep = email_report(conn)
    assert "ra.co: 1 mails, 0 led to an alert; matched 1 events, first source for 0, otherwise median 3.0h later" in rep
    assert "dice.fm: 1 mails" in rep and "first source for 1" in rep.split("dice.fm")[1]
