"""ntfy.sh backup channel for heartbeat and errors."""

from __future__ import annotations

import logging

import requests

from ..config import Settings

log = logging.getLogger(__name__)


def send(s: Settings, message: str, title: str = "ade-radar", priority: str = "default",
         tags: str = "", dry_run: bool = False) -> bool:
    if dry_run:
        print(f"[dry-run ntfy] {title}: {message}")
        return True
    if not s.ntfy_topic:
        log.warning("NTFY_TOPIC not set; ntfy message dropped: %s", title)
        return False
    headers = {"Title": title.encode("ascii", "ignore").decode(), "Priority": priority}
    if tags:
        headers["Tags"] = tags
    try:
        r = requests.post(f"https://ntfy.sh/{s.ntfy_topic}", data=message.encode("utf-8"), headers=headers,
                          timeout=20)
        if not r.ok:
            log.error("ntfy failed: HTTP %s", r.status_code)
        return r.ok
    except requests.RequestException as e:
        log.error("ntfy failed: %s", type(e).__name__)
        return False


def error(s: Settings, message: str, dry_run: bool = False) -> bool:
    return send(s, message, title="ade-radar ERROR", priority="high", tags="warning", dry_run=dry_run)
