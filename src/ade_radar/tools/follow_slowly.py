"""Follow Tier 2 accounts slowly from the separate Instagram account.

1. `ade-radar follow-slowly --propose` looks up handles for Tier 2 names (gently) and writes
   config/handles_tier2.txt with every proposal marked '?'. Nothing is followed.
2. Jack checks each line: remove the '?' to approve, delete or '#' the line to skip.
3. `ade-radar follow-slowly --file config/handles_tier2.txt` follows approved lines, 10 to 15 per hour with
   random waits, and stops at any Instagram warning.
"""

from __future__ import annotations

import json
import logging
import random
import re
import time
from pathlib import Path

from .. import db
from ..config import Context
from ..normalize import compact
from ..notify import ntfy
from ..sources.browser import check_wall, chrome, first_page, human_pause

log = logging.getLogger(__name__)
IG_APP_ID = "936619743392459"


def parse_handles_file(text: str) -> list[str]:
    out = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith(("#", "?")):
            continue
        h = line.split()[0].lstrip("@").lower()
        if re.fullmatch(r"[a-z0-9._]{1,30}", h) and h not in out:
            out.append(h)
    return out


def propose(ctx: Context, out_file: Path) -> None:
    lines = ["# Tier 2 handles. Remove the leading '?' on lines you have CHECKED; '#' or delete to skip.",
             "# Then run: ade-radar follow-slowly --file " + str(out_file.relative_to(ctx.settings.root)), ""]
    tier2 = [a for a in ctx.accounts if a.tier == 2]
    with chrome(ctx.settings) as browser:
        page = first_page(browser)
        page.goto("https://www.instagram.com/", wait_until="domcontentloaded")
        human_pause(3, 5)
        if (reason := check_wall(page)):
            print(f"Instagram: {reason}. Run ade-radar ig-login first.")
            return
        for a in tier2:
            if a.handle:
                lines.append(f"{a.handle:<30} # {a.name} (from accounts.yaml)")
                continue
            users = _search(page, a.name)
            best = _pick(a.name, users)
            if best:
                u = best
                info = f"{u.get('full_name', '')} | {'verified' if u.get('is_verified') else 'not verified'}"
                lines.append(f"?{u['username']:<29} # {a.name} | {info}")
            else:
                lines.append(f"# ??? {a.name}: no match found, look it up by hand")
            time.sleep(random.uniform(15, 30))
            if (reason := check_wall(page)):
                lines.append(f"# stopped: {reason}")
                break
    out_file.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"Wrote {out_file}. Check every line before following.")


def _search(page, query: str) -> list[dict]:
    try:
        data = page.evaluate(
            """async ([q, appId]) => {
                const r = await fetch('/web/search/topsearch/?context=blended&query=' + encodeURIComponent(q),
                    {credentials: 'include', headers: {'X-IG-App-ID': appId}});
                return r.ok ? await r.json() : null;
            }""",
            [query, IG_APP_ID],
        )
    except Exception as e:  # noqa: BLE001
        log.warning("search failed for %s: %s", query, e)
        return []
    return [u.get("user", {}) for u in (data or {}).get("users", [])]


def _pick(name: str, users: list[dict]) -> dict | None:
    if not users:
        return None
    n = compact(name)
    scored = []
    for i, u in enumerate(users[:8]):
        score = 10 - i
        if compact(u.get("full_name", "")) == n:
            score += 10
        if n and n in compact(u.get("username", "")):
            score += 6
        if u.get("is_verified"):
            score += 5
        scored.append((score, u))
    return max(scored, key=lambda x: x[0])[1]


def follow(ctx: Context, handles_file: Path, per_hour: tuple[int, int] = (10, 15)) -> None:
    handles = parse_handles_file(handles_file.read_text(encoding="utf-8"))
    conn = db.connect(ctx.settings.db_path)
    done = set(json.loads(db.kv_get(conn, "followed_handles", "[]")))
    todo = [h for h in handles if h not in done]
    print(f"{len(todo)} to follow ({len(done)} already done). Ctrl+C to stop at any time.")
    rate = random.randint(*per_hour)
    with chrome(ctx.settings) as browser:
        page = first_page(browser)
        for i, h in enumerate(todo):
            page.goto(f"https://www.instagram.com/{h}/", wait_until="domcontentloaded")
            human_pause(3, 6)
            if (reason := check_wall(page)):
                _stop(ctx, f"wall before following @{h}: {reason}")
                break
            result = _click_follow(page)
            human_pause(2, 4)
            if (reason := check_wall(page)):
                _stop(ctx, f"warning after following @{h}: {reason}")
                break
            print(f"[{i + 1}/{len(todo)}] @{h}: {result}")
            if result in ("followed", "already"):
                done.add(h)
                db.kv_set(conn, "followed_handles", json.dumps(sorted(done)))
                conn.commit()
            elif result == "blocked":
                _stop(ctx, f"follow of @{h} did not stick (possible action block)")
                break
            if i + 1 < len(todo):
                wait = random.uniform(3600 / rate * 0.75, 3600 / rate * 1.25)
                print(f"   waiting {wait / 60:.1f} min")
                time.sleep(wait)
    conn.close()


def _click_follow(page) -> str:
    following = page.get_by_role("button", name=re.compile(r"^(Following|Requested)$"))
    if following.count():
        return "already"
    btn = page.get_by_role("button", name=re.compile(r"^Follow( Back)?$"))
    if not btn.count():
        return "no follow button (check by hand)"
    btn.first.click(timeout=5000)
    time.sleep(random.uniform(2, 4))
    if page.get_by_role("button", name=re.compile(r"^(Following|Requested)$")).count():
        return "followed"
    return "blocked"


def _stop(ctx: Context, msg: str) -> None:
    print("STOPPED: " + msg)
    ntfy.error(ctx.settings, "follow-slowly stopped: " + msg)
