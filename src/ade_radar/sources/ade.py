"""Official ADE programme: new matched events, lineup additions, sold out and set times.

Discovery: `ade-radar ade-discover` opens the filter page with Playwright, captures the JSON behind it and
stores the endpoint in data/ade_endpoint.json. Without it we parse the HTML listing and event pages.
"""

from __future__ import annotations

import json
import logging
import random
import re
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urljoin, urlparse, urlunparse

import requests
from bs4 import BeautifulSoup

from .. import db
from ..alerts import AlertStats, dispatch
from ..config import TZ, Context
from ..dedupe import upsert_event
from ..match import Decision, Matcher, decide
from ..models import Classification, Item, now
from ..normalize import clean_artist, compact, equal_names

log = logging.getLogger(__name__)

BASE = "https://www.amsterdam-dance-event.nl"
FILTER_URL = (f"{BASE}/en/program/filter/?section=events&type=8262%2C8263&from=2026-10-21&to=2026-10-25")
EVENT_URL = re.compile(r"/en/program/2026/([^/]+)/(\d+)/?")
LINEUP_NAME = re.compile(r"^\s*([^\n()]{2,60}?)\s*\(([A-Z]{2,3}(?:[/,]\s*[A-Z]{2,3})*)\)\s*$")
SET_TIME = re.compile(r"\b([01]?\d|2[0-3])[:.]([0-5]\d)\s*[-–]\s*([01]?\d|2[0-3])[:.]([0-5]\d)\b")
SOLD_OUT = re.compile(r"\bsold[\s-]*out\b|\buitverkocht\b", re.I)
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0 Safari/537.36"
MAX_DETAIL_FETCHES = 100  # keeps a run well inside the 15-minute task limit, even when pages need rendering
MAX_PAGES = 80
DETAIL_REFRESH_HOURS = 24


# ---- pure parsing ----------------------------------------------------------

def parse_listing_html(html: str) -> list[dict]:
    """Event links from a listing page: [{ade_id, url, title, card_text}]."""
    soup = BeautifulSoup(html, "html.parser")
    out: dict[str, dict] = {}
    for a in soup.find_all("a", href=True):
        m = EVENT_URL.search(a["href"])
        if not m:
            continue
        ade_id = m.group(2)
        card = a.find_parent(["article", "li"]) or a
        title = (a.get("title") or "").strip()
        if not title:
            h = card.find(["h2", "h3", "h4"]) if card is not a else None
            title = (h.get_text(" ", strip=True) if h else a.get_text(" ", strip=True))
        ev = out.setdefault(ade_id, {"ade_id": ade_id, "url": urljoin(BASE, m.group(0)), "title": "",
                                     "card_text": ""})
        if title and len(title) > len(ev["title"]):
            ev["title"] = title
        card_text = card.get_text("\n", strip=True)
        if len(card_text) > len(ev["card_text"]):
            ev["card_text"] = card_text[:600]
    return list(out.values())


def parse_event_html(html: str) -> dict:
    """Event page: title, lineup (country codes stripped), sold_out, set_times {artist: 'HH:MM-HH:MM'}."""
    soup = BeautifulSoup(html, "html.parser")
    for t in soup(["script", "style", "noscript", "header", "footer", "nav"]):
        t.decompose()
    h1 = soup.find("h1")
    title = h1.get_text(" ", strip=True) if h1 else ""
    if not title:
        og = soup.find("meta", property="og:title")
        title = (og.get("content") or "").strip() if og else ""
    lines = [ln.strip() for ln in soup.get_text("\n").splitlines() if ln.strip()]
    lineup: list[str] = []
    set_times: dict[str, str] = {}
    for i, ln in enumerate(lines):
        m = LINEUP_NAME.match(ln)
        if not m:
            continue
        name = clean_artist(m.group(1))
        if name and not any(equal_names(name, x) for x in lineup):
            lineup.append(name)
        # set time on the same line or the line right after the name
        for near in (ln, lines[i + 1] if i + 1 < len(lines) and not LINEUP_NAME.match(lines[i + 1]) else ""):
            t = SET_TIME.search(near)
            if t:
                set_times[name] = f"{int(t.group(1)):02d}:{t.group(2)}-{int(t.group(3)):02d}:{t.group(4)}"
                break
    main_text = "\n".join(lines[:400])
    return {"title": title, "lineup": lineup, "sold_out": bool(SOLD_OUT.search(main_text)), "set_times": set_times}


def parse_ics(text: str) -> dict:
    """DTSTART / LOCATION / SUMMARY from the per-event calendar file."""
    unfolded = re.sub(r"\r?\n[ \t]", "", text)
    out: dict = {}
    for line in unfolded.splitlines():
        key, _, value = line.partition(":")
        base = key.split(";")[0].upper()
        value = value.replace("\\,", ",").replace("\\n", " ").replace("\\;", ";").strip()
        if base == "DTSTART" and "date" not in out:
            out["date"], out["time"] = _ics_datetime(key, value)
        elif base == "LOCATION" and value:
            out["venue"] = value.split(",")[0].strip()
            out["location"] = value
        elif base == "SUMMARY" and value:
            out["title"] = value
    return out


def _ics_datetime(key: str, value: str) -> tuple[str | None, str | None]:
    try:
        if len(value) == 8:
            return datetime.strptime(value, "%Y%m%d").date().isoformat(), None
        dt = datetime.strptime(value.rstrip("Z"), "%Y%m%dT%H%M%S")
        if value.endswith("Z"):
            from datetime import timezone

            dt = dt.replace(tzinfo=timezone.utc).astimezone(TZ)
        return dt.date().isoformat(), dt.strftime("%H:%M")
    except ValueError:
        return None, None


_TITLE_KEYS = ("title", "name", "eventTitle", "event_title")
_ID_KEYS = ("id", "eventId", "event_id", "nodeId")
_URL_KEYS = ("url", "link", "href", "permalink", "uri")
_VENUE_KEYS = ("venue", "location", "venueName", "location_name", "locationName")
_DATE_KEYS = ("startDate", "start_date", "date", "start", "dateStart", "from")
_LINEUP_KEYS = ("lineup", "artists", "performers", "acts", "lineUp")
_SOLD_KEYS = ("soldOut", "sold_out", "isSoldOut", "is_sold_out")


def events_from_json(data) -> list[dict]:
    """Heuristic walk over an unknown JSON response; picks out event-like objects."""
    found: dict[str, dict] = {}

    def walk(node):
        if isinstance(node, list):
            for x in node:
                walk(x)
        elif isinstance(node, dict):
            ev = _event_from_obj(node)
            if ev:
                found.setdefault(ev["ade_id"], ev)
            for v in node.values():
                if isinstance(v, (list, dict)):
                    walk(v)

    walk(data)
    return list(found.values())


def _first(d: dict, keys) -> object:
    for k in keys:
        if k in d and d[k] not in (None, "", []):
            return d[k]
    return None


def _text(v) -> str:
    if isinstance(v, dict):
        return str(_first(v, ("title", "name", "label")) or "")
    return str(v or "")


def _event_from_obj(o: dict) -> dict | None:
    url = _first(o, _URL_KEYS)
    url = _text(url)
    m = EVENT_URL.search(url) if url else None
    ade_id = m.group(2) if m else None
    title = _text(_first(o, _TITLE_KEYS))
    if not ade_id:
        raw_id = _first(o, _ID_KEYS)
        # Only trust a bare id when the object also looks like an event.
        if raw_id is None or not title or not (_first(o, _DATE_KEYS) or _first(o, _VENUE_KEYS)):
            return None
        ade_id = str(raw_id)
    if not title:
        return None
    lineup_raw = _first(o, _LINEUP_KEYS) or []
    lineup = []
    if isinstance(lineup_raw, str):
        lineup_raw = re.split(r"\s*[,\n]\s*", lineup_raw)
    for x in lineup_raw if isinstance(lineup_raw, list) else []:
        name = clean_artist(_text(x))
        if name:
            lineup.append(name)
    date_v = _text(_first(o, _DATE_KEYS))
    date, tme = None, None
    dm = re.match(r"(\d{4}-\d{2}-\d{2})(?:[T ](\d{2}:\d{2}))?", date_v)
    if dm:
        date, tme = dm.group(1), dm.group(2)
    sold = _first(o, _SOLD_KEYS)
    status = str(o.get("status") or o.get("ticketStatus") or "")
    return {"ade_id": ade_id, "title": title, "url": urljoin(BASE, url) if url else "",
            "venue": _text(_first(o, _VENUE_KEYS)) or None, "date": date, "time": tme, "lineup": lineup,
            "sold_out": bool(sold) or bool(SOLD_OUT.search(status))}


def diff(old: dict | None, new: dict) -> list[str]:
    """Change kinds between snapshots: new | lineup:<names> | sold_out | set_times."""
    if old is None:
        return ["new"]
    changes = []
    added = [a for a in new.get("lineup", []) if not any(equal_names(a, b) for b in old.get("lineup", []))]
    if added and old.get("lineup"):
        changes.append("lineup:" + ", ".join(added))
    if new.get("sold_out") and not old.get("sold_out"):
        changes.append("sold_out")
    if new.get("set_times") and not old.get("set_times"):
        changes.append("set_times")
    return changes


# ---- fetching --------------------------------------------------------------

LOAD_MORE = re.compile(r"^\s*(load more|show more|more events|view more|meer laden|toon meer|meer tonen|laad meer)\s*$",
                       re.I)
COOKIE_OK = re.compile(r"^\s*(accept|accept all|allow all|agree|ok|i agree|akkoord|accepteren|alles accepteren)\s*$",
                       re.I)
MAX_RENDER_ROUNDS = 60
MAX_RENDERED_DETAILS = 60


class Fetcher:
    """Plain HTTP first; a hidden Chrome renders pages the site builds with JavaScript."""

    def __init__(self):
        self.http = requests.Session()
        self.http.headers.update({"User-Agent": UA, "Accept-Language": "en-GB,en;q=0.9"})
        self._pw = None
        self._browser = None
        self.rendered = 0

    def get(self, url: str, **kw) -> requests.Response:
        r = self.http.get(url, timeout=30, **kw)
        r.raise_for_status()
        time.sleep(random.uniform(0.5, 1.2))
        return r

    def _page(self):
        if self._browser is None:
            from playwright.sync_api import sync_playwright
            self._pw = sync_playwright().start()
            try:
                self._browser = self._pw.chromium.launch(channel="chrome", headless=True)
            except Exception:  # noqa: BLE001 - no installed Chrome: Playwright's own Chromium
                self._browser = self._pw.chromium.launch(headless=True)
        return self._browser.new_page(user_agent=UA, locale="en-GB", viewport={"width": 1280, "height": 1600})

    def render(self, url: str) -> str:
        page = self._page()
        try:
            page.goto(url, wait_until="networkidle", timeout=60_000)
            accept_cookies(page)
            self.rendered += 1
            return page.content()
        finally:
            page.close()

    def render_listing(self, url: str) -> list[dict]:
        """Open the listing, then scroll and press 'load more' until no new events appear."""
        page = self._page()
        events: dict[str, dict] = {}
        try:
            page.goto(url, wait_until="networkidle", timeout=90_000)
            accept_cookies(page)
            stale = 0
            for _ in range(MAX_RENDER_ROUNDS):
                before = len(events)
                _merge_listing(events, parse_listing_html(page.content()))
                click_load_more(page)
                page.mouse.wheel(0, 6000)
                page.wait_for_timeout(1500)
                _merge_listing(events, parse_listing_html(page.content()))
                stale = stale + 1 if len(events) == before else 0
                if stale >= 3:
                    break
            # Numbered pages instead of (or after) "load more": try ?page=2, 3, ... until nothing new appears.
            sep = "&" if "?" in url else "?"
            for n in range(2, MAX_PAGES + 1):
                before = len(events)
                try:
                    page.goto(f"{url}{sep}page={n}", wait_until="networkidle", timeout=90_000)
                except Exception as e:  # noqa: BLE001 - keep what we already have
                    log.warning("ADE listing page %d failed: %s", n, e)
                    break
                _merge_listing(events, parse_listing_html(page.content()))
                if len(events) == before:
                    break
        finally:
            page.close()
        return list(events.values())

    def close(self) -> None:
        try:
            if self._browser is not None:
                self._browser.close()
            if self._pw is not None:
                self._pw.stop()
        except Exception:  # noqa: BLE001
            pass


def _merge_listing(events: dict[str, dict], batch: list[dict]) -> None:
    for e in batch:
        old = events.get(e["ade_id"])
        if old is None or len(e.get("title", "")) > len(old.get("title", "")):
            events[e["ade_id"]] = e


def accept_cookies(page) -> None:
    try:
        btn = page.get_by_role("button", name=COOKIE_OK)
        if btn.count():
            btn.first.click(timeout=3000)
            page.wait_for_timeout(800)
    except Exception:  # noqa: BLE001
        pass


def click_load_more(page) -> bool:
    for role in ("button", "link"):
        try:
            more = page.get_by_role(role, name=LOAD_MORE)
            if more.count():
                more.first.scroll_into_view_if_needed(timeout=3000)
                more.first.click(timeout=5000)
                page.wait_for_timeout(2000)
                return True
        except Exception:  # noqa: BLE001
            continue
    return False


def endpoint_path(ctx: Context) -> Path:
    return ctx.settings.data_dir / "ade_endpoint.json"


def fetch_listing(ctx: Context, f: Fetcher, counts: dict) -> list[dict]:
    ep = endpoint_path(ctx)
    if ep.exists():
        try:
            events = fetch_json_listing(json.loads(ep.read_text()), f)
            if events:
                counts["listing_mode"] = "json"
                return events
            log.warning("ADE JSON endpoint returned no events; falling back to HTML")
        except (requests.RequestException, ValueError) as e:
            log.warning("ADE JSON endpoint failed (%s); falling back to HTML", e)
    counts["listing_mode"] = "html"
    events = fetch_html_listing(f, filter_url(ctx))
    if events:
        return events
    # The programme is built by JavaScript: a plain download has no events, so render it in hidden Chrome.
    log.info("ADE listing: no events in the plain HTML, rendering it in headless Chrome")
    counts["listing_mode"] = "rendered"
    return f.render_listing(filter_url(ctx))


def fetch_json_listing(cfg: dict, f: Fetcher) -> list[dict]:
    """GET the discovered endpoint; follow a page/offset query parameter if it has one."""
    parts = urlparse(cfg["url"])
    q = {k: v[0] for k, v in parse_qs(parts.query).items()}
    page_key = next((k for k in ("page", "p", "offset", "skip", "start") if k in q), None)
    by_offset = page_key in ("offset", "skip", "start")
    value = int(q[page_key]) if page_key and q[page_key].isdigit() else 0
    events: dict[str, dict] = {}
    for _ in range(MAX_PAGES):
        if page_key:
            q[page_key] = str(value)
        batch = events_from_json(f.get(urlunparse(parts._replace(query=urlencode(q)))).json())
        new = [e for e in batch if e["ade_id"] not in events]
        for e in new:
            events[e["ade_id"]] = e
        if not page_key or not new:
            break
        value += len(batch) if by_offset else 1
    return list(events.values())


def filter_url(ctx: Context) -> str:
    return ctx.settings.ade_filter_url or FILTER_URL


def fetch_html_listing(f: Fetcher, url: str = FILTER_URL) -> list[dict]:
    events: dict[str, dict] = {}
    for page in range(1, MAX_PAGES + 1):
        page_url = url + (f"&page={page}" if page > 1 else "")
        batch = parse_listing_html(f.get(page_url).text)
        new = [e for e in batch if e["ade_id"] not in events]
        for e in new:
            events[e["ade_id"]] = e
        if not new:
            break
    return list(events.values())


def fetch_details(f: Fetcher, ev: dict) -> dict:
    out = dict(ev)
    if ev.get("url"):
        page = parse_event_html(f.get(ev["url"]).text)
        if not page["lineup"] and f.rendered < MAX_RENDERED_DETAILS:
            page = parse_event_html(f.render(ev["url"]))  # event page built by JavaScript
        out["title"] = ev.get("title") or page["title"]
        out["lineup"] = page["lineup"] or ev.get("lineup", [])
        out["sold_out"] = page["sold_out"] or ev.get("sold_out", False)
        out["set_times"] = page["set_times"]
    try:
        ics = parse_ics(f.get(f"{BASE}/en/events/calendar/{ev['ade_id']}.ics").text)
        for k in ("date", "time", "venue"):
            if ics.get(k) and not out.get(k):
                out[k] = ics[k]
    except requests.RequestException:
        pass
    out["fetched_at"] = now().isoformat(timespec="seconds")
    return out


# ---- run -------------------------------------------------------------------

def _persons(matcher: Matcher, ev: dict):
    m = matcher.match(" ".join([ev.get("title", ""), ev.get("card_text", "")]), "", ev.get("lineup", []))
    return m


def run(ctx: Context, conn, counts: dict, *, wa, stats: AlertStats, dry_run: bool) -> None:
    f = Fetcher()
    try:
        _run(ctx, conn, counts, f, wa=wa, stats=stats, dry_run=dry_run)
    finally:
        counts["pages_rendered"] = f.rendered
        f.close()


def _run(ctx: Context, conn, counts: dict, f, *, wa, stats: AlertStats, dry_run: bool) -> None:
    listing = fetch_listing(ctx, f, counts)
    counts["events_listed"] = len(listing)
    if not listing:
        raise RuntimeError("ADE listing returned no events (markup changed?). Run: ade-radar ade-discover")
    snapshots = {r["ade_id"]: json.loads(r["json"]) for r in conn.execute("SELECT * FROM ade_snapshots")}
    baseline = not snapshots
    matcher = Matcher(ctx.taste, ctx.accounts)
    fetched = 0
    alerts_considered = 0
    # Matched events first, then never-fetched ones, then stale ones.
    def priority(ev):
        old = snapshots.get(ev["ade_id"])
        matched = bool(_persons(matcher, {**(old or {}), **ev}).persons)
        return (0 if matched else 1, 0 if old is None or not old.get("fetched_at") else 1)

    for ev in sorted(listing, key=priority):
        old = snapshots.get(ev["ade_id"])
        merged = {**(old or {}), **{k: v for k, v in ev.items() if v not in (None, "", [])}}
        m = _persons(matcher, merged)
        needs_fetch = old is None or not old.get("fetched_at") or bool(m.persons) or _stale(old)
        if needs_fetch and fetched < MAX_DETAIL_FETCHES:
            try:
                merged = fetch_details(f, merged)
                fetched += 1
            except requests.RequestException as e:
                log.warning("ADE event %s fetch failed: %s", ev["ade_id"], e)
            m = _persons(matcher, merged)
        changes = diff(old, merged) if (old is None or old.get("fetched_at")) else []
        conn.execute("INSERT INTO ade_snapshots (ade_id, json, updated_at) VALUES (?,?,?) ON CONFLICT(ade_id)"
                     " DO UPDATE SET json=excluded.json, updated_at=excluded.updated_at",
                     (ev["ade_id"], json.dumps(merged, ensure_ascii=False, sort_keys=True), db.ts()))
        if not m.persons or not changes:
            continue
        for change in changes:
            if baseline and change == "new" and not any(m.must_see_for(p) for p in m.persons):
                continue  # first run: only must_see matches, the rest is the baseline
            alerts_considered += 1
            _alert(conn, ctx, wa, merged, m, change, stats, dry_run)
        if not dry_run:
            conn.commit()
    counts.update({"events_fetched": fetched, "baseline": baseline, "changes_alerted": alerts_considered})


def _stale(old: dict) -> bool:
    try:
        t = datetime.fromisoformat(old["fetched_at"])
    except (KeyError, ValueError):
        return True
    return (now() - t).total_seconds() > DETAIL_REFRESH_HOURS * 3600


def ade_route(change: str, m, ctx: Context) -> str:
    """whatsapp: a new event or a lineup addition with any of the group's artists (must_see or like), set times
                 for a must_see artist.
    digest:   sold out changes, set times for 'like' artists.
    ignore:   lineup changes without any of the group's artists."""
    must = {compact(n) for p in m.hits for n in m.must_see_for(p)}
    liked = {compact(h.name) for hits in m.hits.values() for h in hits if h.level in ("must_see", "like")}
    if change.startswith("lineup:"):
        added = {compact(clean_artist(a)) for a in change.split(":", 1)[1].split(",")}
        return "whatsapp" if added & liked else "ignore"
    if change == "new":
        return "whatsapp"
    if change == "set_times":
        return "whatsapp" if must else "digest"
    return "digest"  # sold_out and anything else


def _digest_text(change: str, ev: dict) -> str:
    if change == "new":
        return "new on the ADE programme"
    if change.startswith("lineup:"):
        return "lineup added " + change.split(":", 1)[1]
    if change == "sold_out":
        return "now sold out"
    return change.replace("_", " ")


def _alert(conn, ctx: Context, wa, ev: dict, m, change: str, stats: AlertStats, dry_run: bool) -> None:
    lineup = ev.get("lineup", [])
    matched = [h.name for hits in m.hits.values() for h in hits if h.level in ("must_see", "like")]
    artists = sorted(set(matched), key=lambda a: compact(a)) + [a for a in lineup if a not in matched]
    item = Item(source="ade", external_id=ev["ade_id"], url=ev.get("url", ""), author="ADE programme",
                text="\n".join([ev.get("title", ""), ", ".join(lineup)]), raw=ev)
    db.save_item(conn, item)
    cls = Classification(is_ade_related=True, is_event_announcement=True, event_name=ev.get("title"),
                         artists=artists, date=ev.get("date"), time=ev.get("time"), venue=ev.get("venue"),
                         ticket_status="sold_out" if ev.get("sold_out") else "on_sale", links=[ev.get("url", "")],
                         urgency="normal", relevant_for=sorted(m.persons), reason=f"ADE programme: {change}",
                         model="rules")
    d = decide(item, cls, m, ctx)
    if not d.send:
        return
    route = ade_route(change, m, ctx)
    log.info("ADE %s %s -> %s (%s)", ev["ade_id"], ev.get("title", ""), route, change)
    if route == "ignore":
        return
    if route == "digest":
        db.save_teaser(conn, f"{item.id}:{change}", "ADE programme", ev.get("url", ""),
                       f"{ev.get('title', '')}: {_digest_text(change, ev)}", d.recipients)
        stats.skipped += 1
        return
    if change == "new":
        detail, kind = ("on ADE programme, already SOLD OUT" if ev.get("sold_out") else "new on ADE programme"), \
            "announce"
    elif change.startswith("lineup:"):
        detail, kind = "lineup added: " + change.split(":", 1)[1], change
    elif change == "sold_out":
        detail, kind = "now SOLD OUT (watch resale)", "sold_out"
    else:
        times = ev.get("set_times") or {}
        mine = [f"{a} {t}" for a, t in times.items() if a in matched] or list(f"{a} {t}" for a, t in times.items())
        detail, kind = "set times: " + "; ".join(mine[:3]), "set_times"
    d = Decision(True, d.urgency, d.recipients, kind, d.reason)
    event_id, _ = upsert_event(conn, cls, item.id, ade_id=ev["ade_id"])
    dispatch(conn, ctx, wa, item, cls, d, event_id, stats, dry_run, detail=detail)


# ---- discovery (run on Jack's laptop) --------------------------------------

def _print_page_hints(page) -> None:
    """What the page itself says about its size and navigation, to tell 'all events' from 'first page'."""
    try:
        text = page.locator("body").inner_text(timeout=5000)
    except Exception:  # noqa: BLE001
        text = ""
    counts = sorted(set(re.findall(r"\b\d[\d.,]*\s+(?:events?|results?|resultaten|evenementen)\b", text, re.I)))
    print(f"page says: {', '.join(counts[:5]) or '(no total count shown)'}")
    hints = page.evaluate("""() => Array.from(document.querySelectorAll('a, button')).map(e => ({
        t: (e.innerText || e.getAttribute('aria-label') || '').trim().slice(0, 40),
        h: e.getAttribute('href') || ''})).filter(x =>
        /^(\\d+|next|volgende|load more|show more|meer|more|»|›|>)$/i.test(x.t) || /page=|pagina/i.test(x.h))""")
    seen = []
    for h in hints:
        key = f"{h['t']!r} {h['h'][:80]}"
        if key not in seen:
            seen.append(key)
    print("navigation: " + ("; ".join(seen[:12]) if seen else "(no next / page / load-more controls found)"))


def discover(ctx: Context) -> Path:
    """Open the filter page, capture JSON responses, save them as fixtures and pick the event list endpoint."""
    from playwright.sync_api import sync_playwright

    out = ctx.settings.root / "tests" / "fixtures" / "ade" / now().strftime("discover_%Y%m%d_%H%M")
    out.mkdir(parents=True, exist_ok=True)
    captured: list[tuple[str, object]] = []
    with sync_playwright() as p:
        browser = p.chromium.launch(channel="chrome", headless=True)
        page = browser.new_page(user_agent=UA)

        def on_response(resp):
            try:
                if "json" in (resp.headers.get("content-type") or "") and resp.request.method == "GET":
                    captured.append((resp.url, resp.json()))
            except Exception:  # noqa: BLE001
                pass

        page.on("response", on_response)
        page.goto(filter_url(ctx), wait_until="networkidle", timeout=90_000)
        for _ in range(4):
            page.mouse.wheel(0, 3000)
            page.wait_for_timeout(1500)
        (out / "filter_page.html").write_text(page.content(), encoding="utf-8")
        _print_page_hints(page)
        browser.close()
    best = None
    for i, (url, data) in enumerate(captured):
        n = len(events_from_json(data))
        (out / f"response_{i:02d}.json").write_text(json.dumps({"url": url, "data": data}, ensure_ascii=False)[:5_000_000],
                                                    encoding="utf-8")
        print(f"[{i:02d}] {n:4d} events  {url[:140]}")
        if n and (best is None or n > best[1]):
            best = (url, n)
    html_events = parse_listing_html((out / "filter_page.html").read_text(encoding="utf-8"))
    print(f"HTML listing parse: {len(html_events)} events on the first page")
    if best:
        endpoint_path(ctx).parent.mkdir(parents=True, exist_ok=True)
        endpoint_path(ctx).write_text(json.dumps({"url": best[0]}, indent=2))
        print(f"Using JSON endpoint ({best[1]} events): {best[0]}\nSaved to {endpoint_path(ctx)}")
    else:
        print("No JSON endpoint with events found; the HTML fallback will be used.")
    return out
