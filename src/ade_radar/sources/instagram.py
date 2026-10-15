"""Instagram Following-feed + stories watcher (separate account only).

DOM is used only for post boundaries, permalinks and handles; the content is read from screenshots by Claude.
Instagram markup changes often: everything here fails soft, and a login wall / checkpoint stops the run.
"""

from __future__ import annotations

import logging
import re
import time
from datetime import datetime
from pathlib import Path

from .. import db
from ..config import Context
from ..models import Item, SourceBlocked, now
from .browser import check_wall, chrome, dismiss_cookie_banner, first_page, human_pause, human_scroll

log = logging.getLogger(__name__)

FEED_URL = "https://www.instagram.com/?variant=following"
MAX_POSTS = 40
FIRST_RUN_MAX_POSTS = 20
FIRST_RUN_MAX_AGE_HOURS = 72
MAX_STORY_FRAMES = 40
STOP_AFTER_SEEN = 3
STOP_AFTER_OLD = 3  # consecutive posts older than the age limit (chronological feed only)
MAX_SCROLLS = 30
RESERVED = {"explore", "accounts", "direct", "reels", "reel", "stories", "p", "tv", "about", "legal", "privacy",
            "terms", "developer", "web", "challenge", "emails", "session", "help", "api", "graphql", "locations",
            "tags", "audio", "your_activity", "notifications", "create"}
UI_LINES = re.compile(
    r"^(like|likes|comment|comments|share|save|more|follow|following|reply|see translation|"
    r"view all \d+ comments|add a comment…?|\d+[,.]?\d*\s*[kKmM]?\s*likes?|liked by .*|original audio|"
    r"\d+\s*[smhdw]|\d+ (?:minutes?|hours?|days?|weeks?) ago|•|·|verified|sponsored|suggested for you|"
    r"paid partnership.*|audio is muted\.?|…)$",
    re.I,
)

EXTRACT_JS = """
(el) => {
  // "Liked by <someone> and 2,278 others" links are likers, not tags: flag them so they are ignored.
  const social = (a) => {
    let n = a.parentElement;
    for (let i = 0; i < 4 && n && n !== el; i++, n = n.parentElement) {
      const t = (n.innerText || '').trim();
      if (t.length < 300 && /^(liked by|followed by|vind ik leuk|gefällt)/i.test(t)) return true;
    }
    return false;
  };
  const anchors = Array.from(el.querySelectorAll('a[href]')).map(a => ({
      href: a.getAttribute('href'), text: (a.innerText || '').trim(), social: social(a)}));
  const header = el.querySelector('header');
  const headerAnchors = header ? Array.from(header.querySelectorAll('a[href]')).map(a => ({
      href: a.getAttribute('href'), text: (a.innerText || '').trim()})) : [];
  const t = el.querySelector('time');
  return {anchors, headerAnchors, datetime: t ? t.getAttribute('datetime') : null,
          text: el.innerText || '', header: header ? header.innerText : ''};
}
"""

PERMA = re.compile(r"^(?:https://www\.instagram\.com)?/(?:[\w.]+/)?(p|reel|tv)/([\w-]+)")
PROFILE = re.compile(r"^(?:https://www\.instagram\.com)?/([A-Za-z0-9._]{1,30})/?$")
MENTION = re.compile(r"(?<![\w@])@([A-Za-z0-9._]{2,30})")
STORY = re.compile(r"/stories/([A-Za-z0-9._]+)/(\d+)")


# ---- pure parsing (tested against fixtures) ------------------------------

def canonical_permalink(href: str) -> str | None:
    m = PERMA.match(href or "")
    if not m:
        return None
    kind = "p" if m.group(1) == "tv" else m.group(1)
    return f"https://www.instagram.com/{kind}/{m.group(2)}/"


def profile_handle(href: str) -> str | None:
    m = PROFILE.match(href or "")
    if not m:
        return None
    h = m.group(1).lower().rstrip(".")
    return None if h in RESERVED else h


def clean_caption(text: str) -> str:
    lines = [ln.strip() for ln in (text or "").splitlines()]
    return "\n".join(ln for ln in lines if ln and not UI_LINES.match(ln))


def parse_post(raw: dict) -> dict | None:
    """raw = output of EXTRACT_JS. Returns permalink, author, collaborators, tagged, location, text, datetime."""
    permalink = next((canonical_permalink(a["href"]) for a in raw.get("anchors", [])
                      if canonical_permalink(a["href"])), None)
    if not permalink:
        return None
    header_handles = []
    location = ""
    for a in raw.get("headerAnchors") or []:
        h = profile_handle(a["href"])
        if h and h not in header_handles:
            header_handles.append(h)
        if "/explore/locations/" in (a["href"] or "") and a.get("text"):
            location = a["text"]
    body_handles = []
    for a in raw.get("anchors", []):
        if a.get("social"):
            continue  # someone who liked the post, not a tag
        h = profile_handle(a["href"])
        if h and h not in header_handles and h not in body_handles:
            body_handles.append(h)
        if not location and "/explore/locations/" in (a["href"] or "") and a.get("text"):
            location = a["text"]
    author = header_handles[0] if header_handles else (body_handles[0] if body_handles else "")
    text = clean_caption(raw.get("text", ""))
    mentions = [h.lower().rstrip(".") for h in MENTION.findall(text)]
    tagged = []
    for h in header_handles[1:] + body_handles + mentions:
        if h != author and h not in tagged and h not in RESERVED:
            tagged.append(h)
    if location:
        text = f"Location: {location}\n{text}"
    return {"permalink": permalink, "author": author, "collaborators": header_handles[1:], "tagged": tagged,
            "location": location, "text": text, "datetime": raw.get("datetime")}


def post_age_hours(dt: str | None, ref: datetime) -> float | None:
    """Age of a post from its <time datetime="..."> (UTC ISO string); None when unknown."""
    if not dt:
        return None
    try:
        t = datetime.fromisoformat(dt.replace("Z", "+00:00"))
    except ValueError:
        return None
    return (ref - t).total_seconds() / 3600


def is_chronological(datetimes: list[str]) -> bool:
    """True when the feed runs newest-first (the Following feed); For you mixes old and new."""
    ts = [d for d in datetimes if d][:8]
    if len(ts) < 3:
        return True
    parsed = [datetime.fromisoformat(d.replace("Z", "+00:00")) for d in ts]
    out_of_order = sum(1 for a, b in zip(parsed, parsed[1:]) if b > a)
    return out_of_order <= 1  # allow one blip (e.g. a collab post)


def parse_story_url(url: str) -> tuple[str, str] | None:
    m = STORY.search(url or "")
    return (m.group(1).lower(), f"https://www.instagram.com/stories/{m.group(1)}/{m.group(2)}/") if m else None


# ---- rate limit ------------------------------------------------------------

def _stamp_path(ctx: Context) -> Path:
    return ctx.settings.data_dir / "ig_last_run.txt"


def minutes_since_last_run(ctx: Context) -> float | None:
    p = _stamp_path(ctx)
    try:
        last = datetime.fromisoformat(p.read_text().strip())
    except (OSError, ValueError):
        return None
    return (now() - last).total_seconds() / 60


def _stamp(ctx: Context) -> None:
    p = _stamp_path(ctx)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(now().isoformat())


# ---- live collection -------------------------------------------------------

def collect(ctx: Context, conn, counts: dict, force: bool = False, dry_run: bool = False) -> list[Item]:
    since = minutes_since_last_run(ctx)
    if since is not None and since < ctx.settings.ig_min_minutes and not force:
        log.info("instagram: last run %.0f min ago, skipping (min %.0f min between runs; --force overrides)", since,
                 ctx.settings.ig_min_minutes)
        counts["skipped_rate_limit"] = 1
        return []
    _stamp(ctx)
    shots = ctx.settings.screenshots_dir / "ig" / now().strftime("%Y-%m-%d")
    shots.mkdir(parents=True, exist_ok=True)
    first_run = db.ig_seen_count(conn) == 0
    limit = FIRST_RUN_MAX_POSTS if first_run else MAX_POSTS
    items: list[Item] = []
    with chrome(ctx.settings) as browser:
        page = first_page(browser)
        page.goto(FEED_URL, wait_until="domcontentloaded", timeout=60_000)
        human_pause(4, 7)
        dismiss_cookie_banner(page)
        _raise_if_wall(page)
        counts["following_feed"] = _ensure_following_feed(page)
        max_age = FIRST_RUN_MAX_AGE_HOURS if first_run else ctx.settings.ig_max_age_hours
        items += collect_feed(page, conn, shots, limit, counts, max_age)
        _raise_if_wall(page)
        try:
            page.mouse.wheel(0, -100_000)
            human_pause(2, 4)
            items += collect_stories(page, conn, shots, counts)
        except SourceBlocked:
            raise
        except Exception as e:  # noqa: BLE001
            log.warning("stories failed (feed results kept): %s", e)
            counts["stories_error"] = str(e)[:200]
    counts["posts_new"] = sum(1 for i in items if i.source == "instagram")
    counts["stories_new"] = sum(1 for i in items if i.source == "instagram_story")
    return items


def _raise_if_wall(page) -> None:
    reason = check_wall(page)
    if reason:
        raise SourceBlocked(f"Instagram {reason}. Open `ade-radar ig-login` and clear it by hand")


def _ensure_following_feed(page) -> bool:
    """Make sure the chronological Following feed is shown (not For you). Returns True when confirmed."""
    if "variant=following" in page.url:
        return True
    log.info("instagram: not on the Following feed (%s), switching via the Instagram logo menu", page.url)
    try:
        page.get_by_role("link", name=re.compile(r"^Instagram$")).first.click(timeout=3000)
        human_pause()
        page.get_by_text(re.compile(r"^Following$")).first.click(timeout=3000)
        human_pause(3, 5)
    except Exception:  # noqa: BLE001
        pass
    if "variant=following" in page.url:
        return True
    log.warning("could not confirm the Following feed; relying on post dates instead")
    return False


def collect_feed(page, conn, shots: Path, limit: int, counts: dict, max_age_hours: float) -> list[Item]:
    """Newest-first walk of the feed. Stops at already-seen posts or, on a chronological feed, at posts older
    than max_age_hours. Old posts are never captured, whichever feed Instagram shows."""
    items: list[Item] = []
    done: set[str] = set()
    datetimes: list[str] = []
    seen_streak = old_streak = 0
    idle_scrolls = scrolls = 0
    checked = 0
    ref = datetime.now().astimezone()
    while len(items) < limit and idle_scrolls < 6 and scrolls < MAX_SCROLLS:
        progressed = False
        # Element handles stay bound to one post; nth-based locators shift when Instagram recycles the
        # feed's DOM while scrolling, which could pair a screenshot with the wrong post.
        for art in page.query_selector_all("article"):
            try:
                raw = art.evaluate(EXTRACT_JS)
            except Exception:  # noqa: BLE001
                continue
            post = parse_post(raw)
            if not post or post["permalink"] in done:
                continue
            done.add(post["permalink"])
            datetimes.append(post["datetime"] or "")
            progressed = True
            checked += 1
            chronological = is_chronological(datetimes)
            if db.ig_is_seen(conn, post["permalink"]):
                seen_streak += 1
                if seen_streak >= STOP_AFTER_SEEN and chronological:
                    log.info("instagram: reached last seen posts")
                    return _done(items, counts, checked, chronological)
                continue
            seen_streak = 0
            age = post_age_hours(post["datetime"], ref)
            if age is not None and age > max_age_hours:
                counts["posts_too_old"] = counts.get("posts_too_old", 0) + 1
                old_streak += 1
                if old_streak >= STOP_AFTER_OLD and chronological:
                    log.info("instagram: reached posts older than %.0fh", max_age_hours)
                    return _done(items, counts, checked, chronological)
                continue
            old_streak = 0
            items.append(_capture_post(page, art, post, shots))
            if len(items) >= limit:
                break
        if _caught_up(page):
            break
        human_scroll(page)
        scrolls += 1
        idle_scrolls = 0 if progressed else idle_scrolls + 1
        if (reason := check_wall(page)):
            raise SourceBlocked(f"Instagram {reason}")
    return _done(items, counts, checked, is_chronological(datetimes))


def _done(items: list[Item], counts: dict, checked: int, chronological: bool) -> list[Item]:
    counts["posts_checked"] = checked
    counts["feed_chronological"] = chronological
    if not chronological:
        log.warning("instagram: feed is not newest-first (For you?); only posts newer than the age limit were kept")
    return items


def _caught_up(page) -> bool:
    try:
        return page.get_by_text(re.compile(r"You'?re all caught up", re.I)).count() > 0
    except Exception:  # noqa: BLE001
        return False


EXPAND_CAPTION_JS = """
(el) => {
  const more = Array.from(el.querySelectorAll('span, div[role="button"], button')).find(x =>
      x.children.length === 0 && /^(… ?)?more$/.test((x.innerText || '').trim()));
  if (!more) return false;
  more.click();
  return true;
}
"""


def _capture_post(page, art, post: dict, shots: Path) -> Item:
    try:
        if art.evaluate(EXPAND_CAPTION_JS):
            time.sleep(0.6)
            if page.locator('[role="dialog"] article').count():
                page.keyboard.press("Escape")  # the click opened the post instead of expanding it
                time.sleep(0.5)
            fuller = parse_post(art.evaluate(EXTRACT_JS))
            if fuller and fuller["permalink"] == post["permalink"]:
                post = fuller
    except Exception:  # noqa: BLE001
        pass
    path = shots / (post["permalink"].rstrip("/").split("/")[-1] + ".jpg")
    try:
        art.scroll_into_view_if_needed(timeout=3000)
        time.sleep(1.2)
        art.screenshot(path=str(path), type="jpeg", quality=80, timeout=10_000)
        paths = [str(path)]
    except Exception as e:  # noqa: BLE001
        log.warning("screenshot failed for %s: %s", post["permalink"], e)
        paths = []
    return Item(source="instagram", external_id=post["permalink"], url=post["permalink"], author=post["author"],
                text=post["text"], screenshot_paths=paths, tagged=post["tagged"], posted_at=post["datetime"] or "",
                raw={"collaborators": post["collaborators"], "location": post["location"]})


def _story_next_button(page):
    """The viewer's own 'Next' arrow (svg aria-label='Next' inside a clickable element), if visible."""
    for loc in (page.get_by_role("button", name=re.compile(r"^Next$", re.I)),
                page.locator('svg[aria-label="Next"]')):
        try:
            for i in range(loc.count()):
                el = loc.nth(i)
                if el.is_visible():
                    return el
        except Exception:  # noqa: BLE001
            pass
    return None


def _wait_url_change(page, url: str, seconds: float) -> bool:
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if page.url.rstrip("/") != url.rstrip("/"):
            return True
        time.sleep(0.2)
    return False


def advance_story(page, url: str) -> str:
    """Move to the next story frame. Returns how: 'auto', 'key', 'button' or '' when stuck.
    The tray button keeps keyboard focus after the click, so keys went to the tray instead of the viewer;
    focus is dropped first. The Space key is not used at all (it 'clicks' whatever button has focus)."""
    if page.url.rstrip("/") != url.rstrip("/"):
        return "auto"  # the story advanced by itself while we were capturing
    try:
        page.evaluate("() => { if (document.activeElement) document.activeElement.blur(); }")
    except Exception:  # noqa: BLE001
        pass
    page.keyboard.press("ArrowRight")
    if _wait_url_change(page, url, 3):
        return "key"
    btn = _story_next_button(page)
    if btn is not None:
        try:
            btn.click(timeout=2000)
            if _wait_url_change(page, url, 3):
                return "button"
        except Exception:  # noqa: BLE001
            pass
    return ""


def collect_stories(page, conn, shots: Path, counts: dict) -> list[Item]:
    tray = page.get_by_role("button", name=re.compile(r"^Story by", re.I))
    n = tray.count()
    counts["story_tray"] = n
    if n == 0:
        log.info("instagram: no stories in tray (or tray markup changed)")
        return []
    unseen = page.get_by_role("button", name=re.compile(r"Story by .*not seen", re.I))
    target = unseen.first if unseen.count() else None
    if target is None:
        return []
    target.click(timeout=5000)
    human_pause(2, 3)
    for label in ("View story", "View Story"):
        try:
            page.get_by_role("button", name=label).click(timeout=1500)
        except Exception:  # noqa: BLE001
            pass
    items: list[Item] = []
    frames = 0
    stop = "max frames"
    for _ in range(MAX_STORY_FRAMES):
        parsed = parse_story_url(page.url)
        if not parsed:
            stop = "viewer closed (all stories watched)"
            break
        author, url = parsed
        frames += 1
        if not db.ig_is_seen(conn, url) and all(i.external_id != url for i in items):
            time.sleep(0.8)  # let the frame render
            path = shots / f"story_{author}_{url.rstrip('/').split('/')[-1]}.jpg"
            page.screenshot(path=str(path), type="jpeg", quality=80)
            text = ""
            try:
                text = page.locator("section").first.inner_text(timeout=1500)[:1500]
            except Exception:  # noqa: BLE001
                pass
            mentions = sorted({h.lower() for h in MENTION.findall(text)} - {author})
            items.append(Item(source="instagram_story", external_id=url, url=url, author=author,
                              text=clean_caption(text), screenshot_paths=[str(path)], tagged=mentions))
        if (reason := check_wall(page)):
            raise SourceBlocked(f"Instagram {reason}")
        if not advance_story(page, url):
            stop = "stuck (could not move to the next story)"
            try:
                page.screenshot(path=str(shots / "story_stuck.jpg"), type="jpeg", quality=70)
            except Exception:  # noqa: BLE001
                pass
            break
        human_pause(0.6, 1.4)
    counts["story_frames"] = frames
    counts["story_stop"] = stop
    log.info("instagram stories: %d frames viewed, %d new, stopped: %s", frames, len(items), stop)
    try:
        page.keyboard.press("Escape")
    except Exception:  # noqa: BLE001
        pass
    return items


# ---- discovery -------------------------------------------------------------

def record_suggestions(conn, ctx: Context, items: list[Item]) -> None:
    known = {a.handle for a in ctx.accounts if a.handle}
    known |= {r["author"] for r in conn.execute("SELECT DISTINCT author FROM ig_seen") if r["author"]}
    for it in items:
        for h in it.tagged:
            if h and h not in known and h != it.author:
                db.note_suggestion(conn, h, it.author)


# ---- manual helpers --------------------------------------------------------

def login(ctx: Context) -> None:
    with chrome(ctx.settings, fit_window=True) as browser:
        page = first_page(browser)
        page.goto("https://www.instagram.com/", wait_until="domcontentloaded")
        human_pause(3, 4)
        dismiss_cookie_banner(page)
        print("Chrome is open with the radar profile.\n"
              "Log in by hand with the SEPARATE Instagram account (never your personal one),\n"
              "accept or decline the cookie banner (scroll down inside it for the buttons),\n"
              "dismiss any 'save login info' / notification prompts, then come back here.")
        input("Press Enter here when you are logged in to save the session and close Chrome... ")


def inspect(ctx: Context) -> Path:
    """Save the live Following feed (HTML, screenshot, first articles) as fixtures for development."""
    out = ctx.settings.root / "tests" / "fixtures" / "instagram" / now().strftime("live_%Y%m%d_%H%M")
    out.mkdir(parents=True, exist_ok=True)
    with chrome(ctx.settings) as browser:
        page = first_page(browser)
        page.goto(FEED_URL, wait_until="domcontentloaded", timeout=60_000)
        human_pause(5, 7)
        dismiss_cookie_banner(page)
        page.screenshot(path=str(out / "feed.png"))
        (out / "feed.html").write_text(page.content(), encoding="utf-8")
        import json

        raws = []
        for art in page.query_selector_all("article")[:5]:
            raws.append(art.evaluate(EXTRACT_JS))
        (out / "articles.json").write_text(json.dumps(raws, indent=2, ensure_ascii=False), encoding="utf-8")
        parsed = [parse_post(r) for r in raws]
        (out / "parsed.json").write_text(json.dumps(parsed, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"tray story buttons: {page.get_by_role('button', name=re.compile('^Story by', re.I)).count()}")
        print(f"wall check: {check_wall(page)}")
        print(f"url: {page.url}  (Following feed: {'yes' if 'variant=following' in page.url else 'NO'})")
        print(f"newest-first: {is_chronological([p['datetime'] for p in parsed if p])}")
        for p in parsed:
            print(p and {k: p[k] for k in ('datetime', 'permalink', 'author', 'tagged')})
    return out
