"""Canonical events: merge the same event seen via Instagram, Gmail and the ADE programme."""

from __future__ import annotations

import json
from difflib import SequenceMatcher

from .db import ts
from .models import Classification
from .normalize import compact, equal_names, night_of, norm


def _similar(a: str | None, b: str | None, threshold: float = 0.85) -> bool:
    if not a or not b:
        return False
    a, b = norm(a), norm(b)
    if min(len(a), len(b)) < 8:
        return a == b
    return SequenceMatcher(None, a, b).ratio() >= threshold


def _venue_match(a: str | None, b: str | None) -> bool:
    if not a or not b:
        return False
    a, b = compact(a), compact(b)
    return a == b or (min(len(a), len(b)) >= 5 and (a in b or b in a))


def _artist_overlap(a: list[str], b: list[str]) -> int:
    return sum(1 for x in a if any(equal_names(x, y) for y in b))


def is_same_event(ev: dict, cls: Classification, ade_id: str | None = None) -> bool:
    if ade_id and ev.get("ade_id"):
        return ev["ade_id"] == ade_id
    ev_artists = json.loads(ev.get("artists") or "[]")
    n1, n2 = night_of(ev.get("date"), ev.get("time")), night_of(cls.date, cls.time)
    if n1 and n2:
        if n1 != n2:
            return False
        if _venue_match(ev.get("venue"), cls.venue) or _artist_overlap(ev_artists, cls.artists) >= 1:
            return True
    return _similar(ev.get("title"), cls.event_name) and not (n1 and n2 and n1 != n2)


def upsert_event(conn, cls: Classification, item_id: str, ade_id: str | None = None) -> tuple[int, bool]:
    """Find or create the canonical event for a classification. Returns (event_id, is_new)."""
    has_identity = cls.event_name or cls.date or cls.venue or cls.artists or ade_id
    if has_identity:
        for row in conn.execute("SELECT * FROM events ORDER BY id"):
            ev = dict(row)
            if is_same_event(ev, cls, ade_id):
                _merge(conn, ev, cls, item_id, ade_id)
                return ev["id"], False
    cur = conn.execute(
        "INSERT INTO events (title, venue, date, time, artists, ticket_status, sources, ade_id, created_at, updated_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?)",
        (cls.event_name, cls.venue, cls.date, cls.time, json.dumps(cls.artists, ensure_ascii=False),
         cls.ticket_status, json.dumps([item_id]), ade_id, ts(), ts()),
    )
    return cur.lastrowid, True


def _merge(conn, ev: dict, cls: Classification, item_id: str, ade_id: str | None) -> None:
    artists = json.loads(ev.get("artists") or "[]")
    for a in cls.artists:
        if not any(equal_names(a, b) for b in artists):
            artists.append(a)
    sources = json.loads(ev.get("sources") or "[]")
    if item_id not in sources:
        sources.append(item_id)
    status = cls.ticket_status if cls.ticket_status != "unknown" else ev.get("ticket_status")
    conn.execute(
        "UPDATE events SET title=?, venue=?, date=?, time=?, artists=?, ticket_status=?, sources=?, ade_id=?,"
        " updated_at=? WHERE id=?",
        (ev.get("title") or cls.event_name, ev.get("venue") or cls.venue, ev.get("date") or cls.date,
         ev.get("time") or cls.time, json.dumps(artists, ensure_ascii=False), status, json.dumps(sources),
         ev.get("ade_id") or ade_id, ts(), ev["id"]),
    )
