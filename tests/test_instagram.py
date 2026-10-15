import os
import re
from pathlib import Path

import pytest

from ade_radar.sources.browser import wall_reason
from ade_radar.sources.instagram import (EXTRACT_JS, canonical_permalink, clean_caption, parse_post,
                                         parse_story_url, profile_handle, record_suggestions)
from ade_radar.models import Item

from .conftest import FIXTURES

CHROMIUM = Path(os.environ.get("PLAYWRIGHT_BROWSERS_PATH", "/opt/pw-browsers")) / "chromium"


def test_permalink_and_handles():
    assert canonical_permalink("/p/DAbc123xyz/") == "https://www.instagram.com/p/DAbc123xyz/"
    assert canonical_permalink("/kettama/reel/C9Reel_99/?x=1") == "https://www.instagram.com/reel/C9Reel_99/"
    assert canonical_permalink("/explore/") is None
    assert profile_handle("/socialpleasure/") == "socialpleasure"
    assert profile_handle("/explore/") is None and profile_handle("/p/abc/") is None


def test_story_url():
    assert parse_story_url("https://www.instagram.com/stories/joshbaker/3412345678901234567/") == (
        "joshbaker", "https://www.instagram.com/stories/joshbaker/3412345678901234567/")
    assert parse_story_url("https://www.instagram.com/") is None


def test_clean_caption_drops_ui():
    assert clean_caption("Like\nComment\n1,234 likes\nADE pop-up\nView all 87 comments") == "ADE pop-up"


def test_wall_reason():
    assert wall_reason("https://www.instagram.com/accounts/login/?next=/", "", "")
    assert wall_reason("https://www.instagram.com/", "Try Again Later We restrict certain activity", "")
    assert wall_reason("https://www.instagram.com/?variant=following", "", "Following feed") is None


@pytest.mark.skipif(not CHROMIUM.exists(), reason="no local chromium")
def test_extract_js_on_fixture_page():
    from playwright.sync_api import sync_playwright

    html = (FIXTURES / "instagram" / "feed_synthetic.html").read_text(encoding="utf-8")
    with sync_playwright() as p:
        b = p.chromium.launch(executable_path=str(CHROMIUM), headless=True)
        page = b.new_page()
        page.set_content(html)
        raws = [a.evaluate(EXTRACT_JS) for a in page.locator("article").all()]
        tray = page.get_by_role("button", name=re.compile(r"Story by .*not seen", re.I)).count()
        b.close()
    first, second = parse_post(raws[0]), parse_post(raws[1])
    assert first["permalink"] == "https://www.instagram.com/p/DAbc123xyz/"
    assert first["author"] == "socialpleasure" and first["collaborators"] == ["paigetomlinson"]
    assert first["location"] == "A'DAM Toren"
    assert {"paigetomlinson", "bellaclaxton", "tinybrand.ams"} <= set(first["tagged"])
    assert "lammermusic" not in first["tagged"]  # "Liked by lammermusic" is a like, not a tag
    assert "Presale sign-up now" in first["text"] and "Liked by" not in first["text"]
    assert first["datetime"] == "2026-10-12T10:00:00.000Z"
    assert second["permalink"] == "https://www.instagram.com/reel/C9Reel_99/" and second["author"] == "kettama"
    assert tray == 1


def test_record_suggestions(conn, ctx):
    from ade_radar import db
    db.ig_mark_seen(conn, "https://www.instagram.com/p/1/", "paigetomlinson")
    items = [Item("instagram", "x", author="socialpleasure", tagged=["paigetomlinson", "tinybrand.ams"]),
             Item("instagram", "y", author="kettama", tagged=["tinybrand.ams", "dgtlfestival"])]
    record_suggestions(conn, ctx, items)
    rows = {r["handle"]: r for r in conn.execute("SELECT * FROM suggested_follows")}
    assert set(rows) == {"tinybrand.ams"}  # followed authors and accounts.yaml handles are not suggested
    assert rows["tinybrand.ams"]["times_seen"] == 2
    assert rows["tinybrand.ams"]["seen_via"] == "socialpleasure,kettama"


@pytest.mark.skipif(not CHROMIUM.exists(), reason="no local chromium")
def test_cookie_banner_dismissed_even_below_the_fold():
    from playwright.sync_api import sync_playwright
    from ade_radar.sources.browser import dismiss_cookie_banner

    html = ('<div style="height:2500px">Optional cookies</div>'
            '<button onclick="document.title=\'declined\'">Decline optional cookies</button>'
            '<button onclick="document.title=\'allowed\'">Allow all cookies</button>')
    with sync_playwright() as p:
        b = p.chromium.launch(executable_path=str(CHROMIUM), headless=True)
        page = b.new_page(viewport={"width": 800, "height": 600})
        page.set_content(html)
        assert dismiss_cookie_banner(page)
        assert page.title() == "declined"
        page.set_content("<p>no banner</p>")
        assert not dismiss_cookie_banner(page)
        b.close()


def test_post_age_and_chronology():
    from datetime import datetime, timezone
    from ade_radar.sources.instagram import is_chronological, post_age_hours

    ref = datetime(2026, 10, 12, 12, 0, tzinfo=timezone.utc)
    assert post_age_hours("2026-10-12T10:00:00.000Z", ref) == 2
    assert post_age_hours("2026-09-01T10:00:00.000Z", ref) > 48
    assert post_age_hours(None, ref) is None and post_age_hours("garbage", ref) is None
    newest_first = ["2026-10-12T10:00:00Z", "2026-10-12T08:00:00Z", "2026-10-11T20:00:00Z", "2026-10-10T09:00:00Z"]
    assert is_chronological(newest_first)
    mixed = ["2026-10-12T10:00:00Z", "2026-09-01T08:00:00Z", "2026-10-11T20:00:00Z", "2026-08-10T09:00:00Z",
             "2026-10-12T09:00:00Z"]
    assert not is_chronological(mixed)


@pytest.mark.skipif(not CHROMIUM.exists(), reason="no local chromium")
def test_collect_feed_on_fixture_skips_old_posts(conn, tmp_path, monkeypatch):
    """Runs the real feed walk against the fixture page: new post captured, old one skipped."""
    from playwright.sync_api import sync_playwright
    from ade_radar.sources import instagram

    monkeypatch.setattr(instagram, "human_scroll", lambda page: None)
    monkeypatch.setattr(instagram, "human_pause", lambda *a: None)
    monkeypatch.setattr(instagram.time, "sleep", lambda s: None)
    html = (FIXTURES / "instagram" / "feed_synthetic.html").read_text(encoding="utf-8")
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    html = html.replace("2026-10-12T10:00:00.000Z", now).replace("2026-10-11T20:00:00.000Z", "2020-01-01T00:00:00.000Z")
    html = html.replace("@tinybrand.ams</div>", "@tinybrand.ams <span onclick=\"this.parentNode.append(' Tickets: '"
                        " + 'dice.fm/ade'); this.remove()\">more</span></div>")
    counts = {}
    with sync_playwright() as p:
        b = p.chromium.launch(executable_path=str(CHROMIUM), headless=True)
        page = b.new_page()
        page.set_content(html)
        items = instagram.collect_feed(page, conn, tmp_path, 40, counts, 48)
        b.close()
    assert [i.external_id for i in items] == ["https://www.instagram.com/p/DAbc123xyz/"]
    assert items[0].author == "socialpleasure" and items[0].screenshot_paths
    assert "Tickets: dice.fm/ade" in items[0].text  # caption "more" expanded, same post kept
    assert counts["posts_checked"] == 2 and counts["posts_too_old"] == 1


STORY_VIEWER = """<html><body>
<button aria-label="Story by kettama, not seen" id="tray">kettama</button>
<div id="side" style="display:inline-block;vertical-align:top"><img width="160" height="280"
  src="data:image/gif;base64,R0lGODlhAQABAAAAACw="><div>@benwal</div></div>
<section style="display:inline-block"><div id="frame" style="display:inline-block"><time id="t" datetime="">2h</time>
<img width="300" height="500" src="data:image/gif;base64,R0lGODlhAQABAAAAACw="><div>@ownmention</div></div>
<div role="button" id="next"><svg aria-label="Next" width="20" height="20"></svg></div></section>
<script>
  const IDS = USE_IDS;
  const frames = [['kettama', '1001', '2026-10-15T08:00:00.000Z'], ['kettama', '1002', '2026-10-15T08:05:00.000Z'],
                  ['socialpleasure', '2001', '2026-10-15T07:00:00.000Z']];
  const path = f => '/stories/' + f[0] + '/' + (IDS ? f[1] + '/' : '');
  const show = i => { history.pushState({}, '', path(frames[i])); document.getElementById('t').setAttribute('datetime', frames[i][2]); };
  let i = 0;
  if (location.pathname.startsWith('/stories/')) show(0);
  const go = () => { i++; if (i < frames.length) { show(i); } else { history.pushState({}, '', '/'); } };
  // Like Instagram: arrow keys only work when focus is not on a tray button (the tray swallows them).
  document.addEventListener('keydown', e => {
    if (e.key === 'ArrowRight' && !(KEYS_BLOCKED || document.activeElement.id === 'tray')) go(); });
  document.getElementById('next').addEventListener('click', go);
  // The viewer appears a moment after the tray click.
  document.getElementById('tray').addEventListener('click', () =>
    setTimeout(() => { if (!location.pathname.startsWith('/stories/')) show(0); }, 1500));
  document.getElementById('tray').focus();
</script></body></html>"""


@pytest.mark.skipif(not CHROMIUM.exists(), reason="no local chromium")
@pytest.mark.parametrize("keys_blocked,start,use_ids", [
    (False, "stories/kettama/1001/", True), (True, "stories/kettama/1001/", True), (False, "", True),
    (False, "", False), (True, "stories/kettama/", False)])
def test_stories_walks_every_frame(conn, tmp_path, monkeypatch, keys_blocked, start, use_ids):
    from playwright.sync_api import sync_playwright
    from ade_radar.sources import instagram

    monkeypatch.setattr(instagram, "human_pause", lambda *a, **k: None)
    html = STORY_VIEWER.replace("KEYS_BLOCKED", "true" if keys_blocked else "false").replace(
        "USE_IDS", "true" if use_ids else "false")
    counts: dict = {}
    with sync_playwright() as p:
        b = p.chromium.launch(executable_path=str(CHROMIUM), headless=True)
        page = b.new_page()
        page.route("https://www.instagram.com/**", lambda r: r.fulfill(body=html, content_type="text/html"))
        page.goto("https://www.instagram.com/" + start)
        items = instagram.collect_stories(page, conn, tmp_path, counts)
        b.close()
    assert [i.author for i in items] == ["kettama", "kettama", "socialpleasure"], counts
    assert len({i.external_id for i in items}) == 3
    if use_ids:
        assert items[0].external_id == "https://www.instagram.com/stories/kettama/1001/"
    else:
        assert items[0].external_id == "https://www.instagram.com/stories/kettama/#2026-10-15T08:00:00.000Z"
    assert counts["story_stop"].startswith("viewer closed")
    # the preview card of another account beside the story is not part of it
    assert all("benwal" not in i.tagged and "ownmention" in i.tagged for i in items)
    assert all(Path(i.screenshot_paths[0]).exists() for i in items)
