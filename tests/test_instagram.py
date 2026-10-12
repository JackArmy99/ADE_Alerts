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
    assert "Presale sign-up now" in first["text"] and "1,234 likes" not in first["text"]
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
