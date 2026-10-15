"""Shared data types. Every source returns list[Item]."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from datetime import datetime

from .config import TZ

TICKET_STATUSES = ["presale_signup", "on_sale", "sold_out", "waitlist", "second_date", "resale", "unknown"]
URGENCIES = ["urgent", "normal", "ignore"]


class SourceBlocked(Exception):
    """A source hit a login wall / checkpoint / rate limit: stop this run, no retry until the next schedule."""


def now() -> datetime:
    return datetime.now(TZ)


@dataclass
class Item:
    source: str  # instagram | instagram_story | gmail | ade
    external_id: str  # permalink / story url / gmail uid / ADE event id
    url: str = ""
    author: str = ""
    text: str = ""
    screenshot_paths: list[str] = field(default_factory=list)
    tagged: list[str] = field(default_factory=list)  # tagged / collab / mentioned handles
    links: list[str] = field(default_factory=list)
    posted_at: str = ""
    raw: dict = field(default_factory=dict)

    @property
    def id(self) -> str:
        return hashlib.sha1(f"{self.source}:{self.external_id}".encode()).hexdigest()[:16]

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False, sort_keys=True)


@dataclass
class Classification:
    is_ade_related: bool = False
    is_event_announcement: bool = False
    event_name: str | None = None
    artists: list[str] = field(default_factory=list)
    date: str | None = None  # YYYY-MM-DD
    time: str | None = None  # HH:MM
    venue: str | None = None
    ticket_status: str = "unknown"
    links: list[str] = field(default_factory=list)
    urgency: str = "ignore"
    relevant_for: list[str] = field(default_factory=list)
    reason: str = ""
    model: str = "rules"
    event_account: str | None = None  # Instagram handle of whoever runs the event (may differ from the poster)

    @classmethod
    def from_dict(cls, d: dict, model: str = "rules") -> "Classification":
        def s(v):
            if v is None:
                return None
            v = str(v).strip()
            return None if v.lower() in ("", "null", "none", "tba", "unknown") else v

        status = str(d.get("ticket_status") or "unknown")
        urgency = str(d.get("urgency") or "ignore")
        return cls(
            is_ade_related=bool(d.get("is_ade_related")),
            is_event_announcement=bool(d.get("is_event_announcement")),
            event_name=s(d.get("event_name")),
            artists=[str(a).strip() for a in d.get("artists") or [] if str(a).strip()],
            date=s(d.get("date")),
            time=s(d.get("time")),
            venue=s(d.get("venue")),
            ticket_status=status if status in TICKET_STATUSES else "unknown",
            links=[str(u) for u in d.get("links") or [] if str(u).startswith("http")],
            urgency=urgency if urgency in URGENCIES else "ignore",
            relevant_for=[str(p).lower() for p in d.get("relevant_for") or []],
            reason=str(d.get("reason") or ""),
            model=model,
            event_account=(s(d.get("event_account")) or "").lstrip("@").lower() or None,
        )

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False, sort_keys=True)
