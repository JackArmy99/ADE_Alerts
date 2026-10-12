import json

from ade_radar.alerts import AlertStats
from ade_radar.sources import ade

from .conftest import FIXTURES

F = FIXTURES / "ade"


def test_parse_listing():
    evs = {e["ade_id"]: e for e in ade.parse_listing_html((F / "listing_synthetic.html").read_text())}
    assert set(evs) == {"2001234", "2001300", "2001400"}
    assert evs["2001234"]["title"] == "Gashouder presents: Josh Baker"
    assert "Sold out" in evs["2001234"]["card_text"]
    assert evs["2001400"]["title"] == "Random Techno Night"
    assert evs["2001300"]["url"] == "https://www.amsterdam-dance-event.nl/en/program/2026/dgtl-opening-day/2001300/"


def test_parse_event_page():
    ev = ade.parse_event_html((F / "event_synthetic.html").read_text())
    assert ev["title"] == "DGTL Opening Day"
    assert ev["lineup"] == ["Benwal", "LAMMER", "Paige Tomlinson", "Helena Lauwaert", "DJ EZ"]
    assert ev["set_times"] == {"Benwal": "13:00-14:30", "Paige Tomlinson": "16:00-17:30"}
    assert ev["sold_out"] is False  # footer text is stripped


def test_parse_ics():
    ics = ade.parse_ics((F / "event_2001300.ics").read_text())
    assert ics == {"title": "DGTL Opening Day", "date": "2026-10-22", "time": "13:00", "venue": "NDSM Docklands",
                   "location": "NDSM Docklands, Amsterdam"}


def test_events_from_json():
    evs = {e["ade_id"]: e for e in ade.events_from_json(json.loads((F / "api_synthetic.json").read_text()))}
    assert set(evs) == {"2001234", "2001300"}  # the filter object is not an event
    assert evs["2001234"]["lineup"] == ["Josh Baker", "Marsolo"] and evs["2001234"]["sold_out"]
    assert evs["2001234"]["venue"] == "Gashouder" and evs["2001234"]["date"] == "2026-10-25"


def test_diff():
    old = {"lineup": ["Benwal"], "sold_out": False, "set_times": {}}
    new = {"lineup": ["Benwal", "Paige Tomlinson (GB)"], "sold_out": True, "set_times": {"Benwal": "13:00-14:30"}}
    assert ade.diff(old, new) == ["lineup:Paige Tomlinson (GB)", "sold_out", "set_times"]
    assert ade.diff(None, new) == ["new"]


class FakeFetcher:
    def __init__(self, pages):
        self.pages = pages

    def get(self, url, **kw):
        for key, text in self.pages.items():
            if key in url:
                return type("R", (), {"text": text, "json": lambda self: json.loads(text)})()
        import requests
        raise requests.HTTPError("404 " + url)


def _pages(event_html):
    return {"page=2": "<html></html>", "program/filter": (F / "listing_synthetic.html").read_text(),
            "dgtl-opening-day/2001300": event_html, "2001300.ics": (F / "event_2001300.ics").read_text(),
            "gashouder-presents-josh-baker": "<h1>Gashouder presents: Josh Baker</h1><p>Josh Baker (GB)</p>"
                                             "<p>Marsolo (GB)</p><p>SOLD OUT</p>",
            "random-techno": "<h1>Random Techno Night</h1><p>Nobody (DE)</p>"}


def test_run_baseline_then_lineup_change(conn, ctx, fake_wa, monkeypatch):
    html = (F / "event_synthetic.html").read_text()
    without_paige = html.replace("<li>Paige Tomlinson (GB)</li><li>16:00 – 17:30</li>", "") \
                        .replace("<li>13:00 - 14:30</li>", "")
    fetcher = FakeFetcher(_pages(without_paige))
    monkeypatch.setattr(ade, "Fetcher", lambda: fetcher)
    stats, counts = AlertStats(), {}
    ade.run(ctx, conn, counts, wa=fake_wa, stats=stats, dry_run=False)
    assert counts["baseline"] and counts["events_listed"] == 3
    # baseline: only must_see matches alert -> Josh Baker (sold out) for jack
    assert [p for _, p in fake_wa.sent][0][0].startswith("Gashouder presents: Josh Baker")
    assert "SOLD OUT" in fake_wa.sent[0][1][3]
    n_baseline = len(fake_wa.sent)

    fetcher.pages = _pages(html)  # Paige Tomlinson added + set times published
    stats2, counts2 = AlertStats(), {}
    ade.run(ctx, conn, counts2, wa=fake_wa, stats=stats2, dry_run=False)
    new_msgs = [p for _, p in fake_wa.sent[n_baseline:]]
    details = [p[3] for p in new_msgs]
    assert any("lineup added: Paige Tomlinson" in d for d in details)
    assert any(d.startswith("set times:") and "Paige Tomlinson 16:00-17:30" in d for d in details)
    assert all(p[2] == "NDSM Docklands" and p[1].startswith("Thu 22 Oct") for p in new_msgs)

    stats3 = AlertStats()
    ade.run(ctx, conn, {}, wa=fake_wa, stats=stats3, dry_run=False)
    assert stats3.sent == 0  # nothing changed -> nothing sent
