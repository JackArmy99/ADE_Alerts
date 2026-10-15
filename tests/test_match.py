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


def test_popup_with_must_see_is_gold(ctx):
    from ade_radar.alerts import template_params
    m = Matcher(ctx.taste, ctx.accounts)
    item = Item("instagram", "g1", url="https://www.instagram.com/p/g1/", author="somebrand",
                text="Secret location ADE pop-up with Josh Baker, Thu 22 Oct")
    cls = Classification(is_ade_related=True, is_event_announcement=True, urgency="normal", date="2026-10-22",
                         artists=["Josh Baker"], model="claude")
    d = decide(item, cls, m.match(item.text, item.author), ctx)
    assert d.send and d.gold and d.urgency == "urgent"
    assert template_params(item, cls, d)[3].startswith("GOLD - POP-UP")


def test_popup_with_only_like_artist_is_urgent_not_gold(ctx):
    m = Matcher(ctx.taste, ctx.accounts)
    item = Item("instagram", "g2", author="somebrand", text="ADE pop-up with Marsolo, Fri 23 Oct")
    cls = Classification(is_ade_related=True, is_event_announcement=True, urgency="normal", date="2026-10-23",
                         artists=["Marsolo"], model="claude")
    d = decide(item, cls, m.match(item.text, item.author), ctx)
    assert d.send and not d.gold and d.urgency == "urgent"  # "pop-up" is an urgent keyword


def test_popup_wording_variants_and_new_must_see(ctx):
    from ade_radar.match import is_popup, popup_regex
    from ade_radar.config import load_taste
    from tests.conftest import REPO
    taste = load_taste(REPO)
    r = popup_regex(taste)
    for text in ("SECRET-LOCATION announced", "invite only rave", "Geheime locatie!", "guest list now open",
                 "one-off takeover", "location revealed on the day", "Pop Up"):
        assert r.search(text), text
    assert not r.search("we popped upstairs") and not r.search("popular night")
    m = Matcher(taste, ctx.accounts)
    assert "Klangkuenstler" in m.match("ADE: Klangkünstler all night long").must_see_for("jack")
    assert "Dart" in m.match("ADE: Dart b2b Bou").must_see_for("jack")
    assert not m.match("ADE: Eli Brown live").must_see_for("jack")  # moved to like


def test_events_outside_ade_week_never_alert(ctx):
    m = Matcher(ctx.taste, ctx.accounts)
    item = Item("instagram", "v1", author="thuishaven", text="Thuishaven March programme: Josh Baker, presale now")
    march = Classification(is_ade_related=True, is_event_announcement=True, urgency="urgent", date="2027-03-14",
                           artists=["Josh Baker"], ticket_status="presale_signup", model="claude")
    d = decide(item, march, m.match(item.text, item.author), ctx)
    assert not d.send and "outside ADE week" in d.reason
    # no date extracted, but the text names another month
    nodate = Classification(is_ade_related=True, is_event_announcement=True, urgency="urgent",
                            artists=["Josh Baker"], model="claude")
    item2 = Item("instagram", "v2", author="thuishaven", text="Josh Baker at Thuishaven on 14 March, presale now")
    assert not decide(item2, nodate, m.match(item2.text, item2.author), ctx).send
    # inside the week, including an after-midnight set on the 26th (= night of the 25th)
    for date, time in (("2026-10-22", "23:00"), ("2026-10-26", "02:00")):
        ok = Classification(is_ade_related=True, is_event_announcement=True, urgency="urgent", date=date, time=time,
                            artists=["Josh Baker"], ticket_status="presale_signup", model="claude")
        assert decide(item, ok, m.match(item.text, item.author), ctx).send, date
    # 27 Oct is after ADE
    late = Classification(is_ade_related=True, is_event_announcement=True, urgency="urgent", date="2026-10-27",
                          time="23:00", artists=["Josh Baker"], model="claude")
    assert not decide(item, late, m.match(item.text, item.author), ctx).send
    # "ADE 2026 on 22 Oct, tickets from 1 March" style text still passes (October / ADE mentioned)
    item3 = Item("instagram", "v3", author="x", text="ADE pop-up 22 Oct with Josh Baker, presale since 1 March")
    assert decide(item3, nodate, m.match(item3.text, item3.author), ctx).send


def test_recipients_without_taste_share_the_owners(ctx):
    from ade_radar.config import share_owner_taste
    from ade_radar.match import Matcher, decide
    from ade_radar.models import Classification, Item

    ctx.settings.recipients["sam"] = "31600000003"
    share_owner_taste(ctx.taste, ctx.settings)
    assert ctx.taste.people["sam"] is ctx.taste.people["jack"]
    assert ctx.taste.people["friend1"] is not ctx.taste.people["jack"]  # an own block is kept
    item = Item("instagram", "p1", author="someone", text="flyer")
    cls = Classification(is_ade_related=True, is_event_announcement=True, event_name="Pop-up",
                         artists=["Paige Tomlinson"], date="2026-10-22", venue="Somewhere",
                         urgency="urgent", relevant_for=["jack"], model="claude")
    d = decide(item, cls, Matcher(ctx.taste, ctx.accounts).match("flyer", "someone"), ctx)
    assert d.send and d.recipients == ["jack", "sam"]


def test_timetables_are_not_alerts(ctx):
    from ade_radar.match import Matcher, decide
    from ade_radar.models import Classification, Item

    m = Matcher(ctx.taste, ctx.accounts)
    item = Item("instagram", "t1", author="dgtlfestival", text="ADE timetable is here! Paige Tomlinson 16:00")
    cls = Classification(is_ade_related=True, is_event_announcement=True, event_name="DGTL Opening Day",
                         artists=["Paige Tomlinson"], date="2026-10-22", venue="NDSM", urgency="normal",
                         ticket_status="on_sale", relevant_for=["jack"], model="claude")
    assert not decide(item, cls, m.match(item.text, item.author), ctx).send
    # ...unless it also opens a new way in
    cls.ticket_status = "waitlist"
    assert decide(item, cls, m.match(item.text, item.author), ctx).send


def test_sold_out_is_not_an_alert_but_a_waitlist_is(ctx):
    from ade_radar.match import Matcher, decide
    from ade_radar.models import Classification, Item

    m = Matcher(ctx.taste, ctx.accounts)
    item = Item("instagram", "so1", author="joshbaker", text="Gashouder ADE is SOLD OUT. Thank you!")
    cls = Classification(is_ade_related=True, is_event_announcement=True, event_name="Josh Baker Gashouder",
                         artists=["Josh Baker"], date="2026-10-25", venue="Gashouder", urgency="normal",
                         ticket_status="unknown", relevant_for=["jack"], model="claude")
    assert not decide(item, cls, m.match(item.text, item.author), ctx).send
    cls.ticket_status = "waitlist"
    assert decide(item, cls, m.match(item.text, item.author), ctx).send
    almost = Item("instagram", "so2", author="joshbaker", text="Gashouder ADE almost sold out, final tickets")
    cls.ticket_status = "on_sale"
    assert decide(almost, cls, m.match(almost.text, almost.author), ctx).send
