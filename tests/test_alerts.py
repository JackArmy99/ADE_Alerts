

def test_emoji_in_front_of_event_name():
    from ade_radar.alerts import template_params
    from ade_radar.match import Decision
    from ade_radar.models import Classification, Item

    item = Item("instagram", "p1", url="https://www.instagram.com/p/x/", author="kettama", text="")
    cls = Classification(event_name="KETTAMA ADE", date="2026-10-23")
    params = template_params(item, cls, Decision(True, "normal"), emoji="\U0001FAA8")
    assert params[0] == "\U0001FAA8 KETTAMA ADE"
    assert template_params(item, cls, Decision(True, "normal"))[0] == "KETTAMA ADE"
