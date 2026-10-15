"""Headed installed-Chrome with the persistent Instagram profile."""

from __future__ import annotations

import logging
import random
import re
import time
from contextlib import contextmanager

from ..config import Settings

log = logging.getLogger(__name__)

WALL_URL = re.compile(r"/accounts/login|/challenge|/checkpoint|/accounts/suspended|/auth_platform|/accounts/disabled")
WALL_TEXT = [
    "try again later",
    "we restrict certain activity",
    "suspicious activity",
    "your account has been suspended",
    "confirm it's you",
    "help us confirm",
    "probeer het later opnieuw",
    "action blocked",
]


# With the screen off or locked, Windows reports the Chrome window as hidden and Chrome stops painting it, so
# screenshots hang until they time out and the run is killed. These keep Chrome rendering in the background.
KEEP_RENDERING = ["--disable-features=CalculateNativeWinOcclusion", "--disable-backgrounding-occluded-windows",
                  "--disable-renderer-backgrounding", "--disable-background-timer-throttling"]


@contextmanager
def chrome(s: Settings, headless: bool = False, fit_window: bool = False):
    """fit_window: page follows the window size (manual login on small/scaled screens); otherwise a fixed
    1280x900 viewport so screenshots are consistent."""
    from playwright.sync_api import sync_playwright

    s.ig_profile_dir.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        try:
            ctx = p.chromium.launch_persistent_context(
                str(s.ig_profile_dir), channel="chrome", headless=headless,
                viewport=None if fit_window else {"width": 1280, "height": 900}, no_viewport=fit_window,
                locale="en-GB", timezone_id="Europe/Amsterdam",
                args=["--disable-blink-features=AutomationControlled", *KEEP_RENDERING]
                + (["--start-maximized"] if fit_window else []),
            )
        except Exception as e:  # noqa: BLE001
            raise RuntimeError(f"could not start installed Chrome (is Google Chrome installed?): {e}") from e
        ctx.set_default_timeout(20_000)  # fail fast instead of hanging until the 15-minute task limit
        try:
            yield ctx
        finally:
            try:
                ctx.close()
            except Exception:  # noqa: BLE001
                pass


COOKIE_BUTTONS = re.compile(r"^(Decline optional cookies|Only allow essential cookies|Allow all cookies|"
                            r"Optionele cookies weigeren|Alle cookies toestaan)$", re.I)


def dismiss_cookie_banner(page) -> bool:
    """Click through Instagram's cookie consent dialog (prefers declining optional cookies)."""
    try:
        btns = page.get_by_role("button", name=COOKIE_BUTTONS)
        if not btns.count():
            return False
        decline = page.get_by_role("button", name=re.compile(r"Decline optional|essential|weigeren", re.I))
        target = decline.first if decline.count() else btns.first
        target.scroll_into_view_if_needed(timeout=3000)
        target.click(timeout=5000)
        human_pause(1, 2)
        log.info("cookie banner dismissed")
        return True
    except Exception as e:  # noqa: BLE001
        log.warning("could not dismiss cookie banner: %s", e)
        return False


def first_page(ctx):
    return ctx.pages[0] if ctx.pages else ctx.new_page()


def wall_reason(url: str, dialog_text: str, body_text: str = "") -> str | None:
    """Pure check used by check_wall (and tests)."""
    if WALL_URL.search(url or ""):
        return f"redirected to {WALL_URL.search(url).group(0)}"
    d = (dialog_text or "").lower()
    for t in WALL_TEXT:
        if t in d:
            return f"dialog: {t}"
    b = (body_text or "").lower()
    if "log in" in b and "sign up" in b and "password" in b and "following" not in b:
        return "login wall"
    return None


def check_wall(page) -> str | None:
    try:
        dialogs = " ".join(page.locator('[role="dialog"]').all_inner_texts())
    except Exception:  # noqa: BLE001
        dialogs = ""
    try:
        body = page.locator("body").inner_text(timeout=3000)[:5000]
    except Exception:  # noqa: BLE001
        body = ""
    return wall_reason(page.url, dialogs, body)


def human_pause(lo: float = 1.2, hi: float = 3.2) -> None:
    time.sleep(random.uniform(lo, hi))


def human_scroll(page, lo: int = 450, hi: int = 900) -> None:
    total = random.randint(lo, hi)
    steps = random.randint(3, 6)
    for _ in range(steps):
        page.mouse.wheel(0, total / steps)
        time.sleep(random.uniform(0.08, 0.25))
    human_pause()
