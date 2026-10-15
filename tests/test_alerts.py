

def test_emoji_in_front_of_event_name():
    from ade_radar.alerts import template_params
    from ade_radar.match import Decision
    from ade_radar.models import Classification, Item

    item = Item("instagram", "p1", url="https://www.instagram.com/p/x/", author="kettama", text="")
    cls = Classification(event_name="KETTAMA ADE", date="2026-10-23")
    params = template_params(item, cls, Decision(True, "normal"), emoji="\U0001FAA8")
    assert params[0] == "\U0001FAA8 KETTAMA ADE"
    assert template_params(item, cls, Decision(True, "normal"))[0] == "KETTAMA ADE"


def test_everyone_gets_new_alerts_but_no_catch_up(conn, ctx, fake_wa):
    from ade_radar.alerts import AlertStats, dispatch
    from ade_radar.match import Decision, Matcher, decide
    from ade_radar.models import Classification, Item

    ctx.settings.alert_everyone = True
    item = Item("instagram", "p1", url="https://www.instagram.com/p/x/", author="kettama", text="KETTAMA ADE show")
    cls = Classification(is_ade_related=True, is_event_announcement=True, event_name="KETTAMA ADE",
                         artists=["KETTAMA"], date="2026-10-23", venue="Gashouder", urgency="normal",
                         relevant_for=["jack"], model="claude")
    d = decide(item, cls, Matcher(ctx.taste, ctx.accounts).match(item.text, item.author), ctx)
    assert d.send and d.recipients == ["jack", "friend1"]  # friend1 has no taste but still gets it

    dispatch(conn, ctx, fake_wa, item, cls, Decision(True, "normal", ["jack"], "announce"), 1, AlertStats(), False)
    assert len(fake_wa.sent) == 1
    # Later someone new joins: the same event is old news, nobody gets it again
    stats = AlertStats()
    dispatch(conn, ctx, fake_wa, item, cls, Decision(True, "normal", ["jack", "friend1"], "announce"), 1, stats,
             False)
    assert len(fake_wa.sent) == 1 and stats.skipped == 2


def test_story_link_goes_to_the_events_account_not_the_resharer():
    from ade_radar.alerts import best_link, template_params
    from ade_radar.match import Decision
    from ade_radar.models import Classification, Item

    story = Item("instagram_story", "s1", url="https://www.instagram.com/rossi.dj_/", author="rossi.dj_",
                 text="@demachine ADE", tagged=["demachine"])
    cls = Classification(event_name="De Machine op ADE", artists=["Benwal", "Jam & Jali van der Valk"],
                         venue="De Machine")
    assert best_link(story, cls) == "https://www.instagram.com/demachine/"  # matched from the mention
    cls.event_account = "demachine.ams"  # Claude read the reshare header
    assert best_link(story, cls) == "https://www.instagram.com/demachine.ams/"
    assert "@demachine.ams via @rossi.dj_" in template_params(story, cls, Decision(True, "normal"))[3]
    # the poster's own event keeps the poster
    own = Item("instagram_story", "s2", author="benwal", text="")
    assert best_link(own, Classification(event_name="Benwal ADE")) == "https://www.instagram.com/benwal/"
    # a ticket link always wins
    cls.links = ["https://dice.fm/event/abc"]
    assert best_link(story, cls) == "https://dice.fm/event/abc"
