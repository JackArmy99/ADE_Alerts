import json
from pathlib import Path
import os

import pytest

from ade_radar.alerts import AlertStats
from ade_radar.sources import ade

from .conftest import FIXTURES

F = FIXTURES / "ade"
CHROMIUM = Path(os.environ.get("PLAYWRIGHT_BROWSERS_PATH", "/opt/pw-browsers")) / "chromium"


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
        self.rendered = 0

    def render(self, url):
        self.rendered += 1
        return self.get(url).text

    def render_listing(self, url):
        return ade.parse_listing_html(self.pages.get("RENDERED", ""))

    def close(self):
        pass

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


def test_listing_falls_back_to_rendering_when_plain_html_is_empty(ctx):
    pages = {"program/filter": "<html><body><div id='app'></div></body></html>",
             "RENDERED": (F / "listing_synthetic.html").read_text()}
    counts = {}
    events = ade.fetch_listing(ctx, FakeFetcher(pages), counts)
    assert counts["listing_mode"] == "rendered" and len(events) == 3


LOAD_MORE_PAGE = """<html><body><main id="list"></main><button id="more">Load more</button><script>
let n = 0;
function add(k) { for (let i = 0; i < k; i++) { n++; const a = document.createElement('a');
  a.href = '/en/program/2026/event-' + n + '/' + (3000000 + n) + '/'; a.innerHTML = '<h3>Event ' + n + '</h3>';
  document.getElementById('list').appendChild(a); }
  if (n >= 30) document.getElementById('more').remove(); }
add(10); document.getElementById('more').onclick = () => add(10);
</script></body></html>"""


@pytest.mark.skipif(not CHROMIUM.exists(), reason="no local chromium")
def test_render_listing_clicks_load_more_until_done(tmp_path, monkeypatch):
    from playwright.sync_api import sync_playwright
    page_file = tmp_path / "listing.html"
    page_file.write_text(LOAD_MORE_PAGE)
    monkeypatch.setattr(ade, "MAX_RENDER_ROUNDS", 10)
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=str(CHROMIUM), headless=True)
        f = ade.Fetcher()
        f._browser = browser
        events = f.render_listing(page_file.as_uri())
        browser.close()
    assert len(events) == 30 and events[0]["title"].startswith("Event")


def test_ade_route(ctx):
    from ade_radar.match import Matcher
    m = Matcher(ctx.taste, ctx.accounts)
    must = m.match("Gashouder presents", "", ["Josh Baker", "Marsolo"])       # Josh Baker = must_see
    like = m.match("Some night", "", ["Marsolo", "Random DJ"])                 # Marsolo = like
    assert ade.ade_route("new", must, ctx) == "whatsapp"
    assert ade.ade_route("new", like, ctx) == "whatsapp"
    assert ade.ade_route("lineup:Josh Baker", must, ctx) == "whatsapp"
    assert ade.ade_route("lineup:Marsolo", like, ctx) == "whatsapp"
    assert ade.ade_route("lineup:Random DJ", like, ctx) == "ignore"
    assert ade.ade_route("sold_out", must, ctx) == "digest"
    assert ade.ade_route("set_times", must, ctx) == "whatsapp"
    assert ade.ade_route("set_times", like, ctx) == "digest"


def test_must_see_found_when_listed_event_is_read_in_detail_later(conn, ctx, fake_wa, monkeypatch):
    """First run reads only some events in detail; a must-see in a later-read lineup is still reported once."""
    listing = ('<ul><li><a href="/en/program/2026/night-one/2009001/"><h3>Night One</h3></a></li>'
               '<li><a href="/en/program/2026/night-two/2009002/"><h3>Night Two</h3></a></li></ul>')
    pages = {"program/filter": listing, "page=2": "<html></html>",
             "night-one/2009001": "<h1>Night One</h1><p>Nobody (DE)</p>",
             "night-two/2009002": "<h1>Night Two</h1><p>Kyle Starkey (GB)</p><p>Someone (NL)</p>"}
    fetcher = FakeFetcher(pages)
    monkeypatch.setattr(ade, "Fetcher", lambda: fetcher)
    monkeypatch.setattr(ade, "MAX_DETAIL_FETCHES", 1)  # first run only reads one event in detail
    ade.run(ctx, conn, {}, wa=fake_wa, stats=AlertStats(), dry_run=False)
    ade.run(ctx, conn, {}, wa=fake_wa, stats=AlertStats(), dry_run=False)
    ade.run(ctx, conn, {}, wa=fake_wa, stats=AlertStats(), dry_run=False)
    titles = [p[0] for _, p in fake_wa.sent]
    assert sum(t.startswith("Night Two") for t in titles) == 1  # Kyle Starkey (must-see): reported exactly once
    assert not any(t.startswith("Night One") for t in titles)


def test_wrong_ics_date_is_replaced_by_page_date():
    ev = {"ade_id": "1", "date": "2026-03-10", "time": "12:00", "page_date": "2026-10-23"}
    out = ade.sanitize_date(ev)
    assert out["date"] == "2026-10-23" and out["time"] is None
    assert ade.sanitize_date({"ade_id": "2", "date": "2026-03-10", "card_text": "Sat 24 Oct · Pllek"})["date"] == "2026-10-24"
    assert ade.sanitize_date({"ade_id": "3", "date": "2026-03-10"})["date"] is None  # unknown -> TBA, never March
    keep = {"ade_id": "4", "date": "2026-10-22", "time": "23:00"}
    assert ade.sanitize_date(keep) == keep
    assert ade.date_from_text("Thursday 22 October, NDSM") == "2026-10-22"
    assert ade.date_from_text("25.10.2026 Gashouder") == "2026-10-25"
