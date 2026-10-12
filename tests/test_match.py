from ade_radar.match import Matcher, account_for_handle, decide
from ade_radar.models import Classification, Item


def test_account_for_handle(ctx):
    assert account_for_handle("socialpleasure", ctx.accounts).name == "Social Pleasure"
    assert account_for_handle("socialpleasure.ldn", ctx.accounts).name == "Social Pleasure"
    assert account_for_handle("dgtlfestival", ctx.accounts).name == "DGTL"
    assert account_for_handle("randomperson", ctx.accounts) is None


def test_priority_account_urgent_goes_to_everyone(ctx):
    m = Matcher(ctx.taste, ctx.accounts)
    item = Item("instagram", "p1", author="socialpleasure",
                text="ADE pop-up. Presale sign-up now, link in bio. Amsterdam 22 Oct")
    res = m.match(item.text, item.author)
    assert res.ade_related and res.priority_urgent and "sign-up" in res.urgent_keywords
    d = decide(item, Classification(model="rules"), res, ctx)
    assert d.send and d.urgency == "urgent" and d.recipients == ["jack", "friend1"]


def test_must_see_artist_normal_alert_only_for_matching_person(ctx):
    m = Matcher(ctx.taste, ctx.accounts)
    item = Item("instagram", "p2", author="somepromoter", text="KETTAMA live at ADE, Thu 22 Oct, NDSM")
    res = m.match(item.text, item.author)
    assert res.persons == {"jack"}
    cls = Classification(is_ade_related=True, is_event_announcement=True, urgency="normal", date="2026-10-22",
                         venue="NDSM", relevant_for=["jack"], model="claude")
    d = decide(item, cls, res, ctx)
    assert d.send and d.urgency == "normal" and d.recipients == ["jack"]


def test_claude_not_ade_wins(ctx):
    m = Matcher(ctx.taste, ctx.accounts)
    item = Item("instagram", "p3", author="kettama", text="Presale sign-up for Dublin tour, ADE vibes")
    cls = Classification(is_ade_related=False, model="claude")
    assert not decide(item, cls, m.match(item.text, item.author), ctx).send


def test_ade_word_is_case_sensitive(ctx):
    m = Matcher(ctx.taste, ctx.accounts)
    assert not m.match("we made a new track").ade_related
    assert m.match("See you at ADE").ade_related


def test_unknown_account_urgent_without_persons_goes_to_owner(ctx):
    m = Matcher(ctx.taste, ctx.accounts)
    item = Item("instagram", "p4", author="tinybrand", text="ADE secret location party, waitlist open")
    cls = Classification(is_ade_related=True, is_event_announcement=True, urgency="urgent", model="claude")
    d = decide(item, cls, m.match(item.text, item.author), ctx)
    assert d.send and d.recipients == ["jack"]


EWAN = ("CLUB KNUCKLES CLOSED. can't believe after 23 weeks we brought my very first residency to a close. "
        "to all the djs that played thank you. next we have a show at amsterdam for ade & after that who knows")


def test_ade_mention_without_details_is_a_teaser_not_urgent(ctx):
    """Live case 12 Oct: Claude called a residency recap 'urgent' because it mentions an ADE show."""
    m = Matcher(ctx.taste, ctx.accounts)
    item = Item("instagram", "https://www.instagram.com/p/DeZF5xVjEIL/", author="ewanmcvicar_", text=EWAN)
    cls = Classification(is_ade_related=True, is_event_announcement=False, urgency="urgent",
                         relevant_for=["jack"], reason="mentions ADE show", model="claude")
    d = decide(item, cls, m.match(item.text, item.author), ctx)
    assert not d.send and d.urgency == "teaser" and d.recipients == ["jack"]


def test_claude_urgent_with_ticket_signal_still_urgent(ctx):
    m = Matcher(ctx.taste, ctx.accounts)
    item = Item("instagram", "x", author="ewanmcvicar_", text="ADE show, tickets via dice")
    cls = Classification(is_ade_related=True, is_event_announcement=True, urgency="urgent",
                         ticket_status="presale_signup", relevant_for=["jack"], model="claude")
    d = decide(item, cls, m.match(item.text, item.author), ctx)
    assert d.send and d.urgency == "urgent"
