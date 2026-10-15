"""Turn a decision into idempotent WhatsApp template alerts."""

from __future__ import annotations

import hashlib
import json
import logging
import re
from dataclasses import dataclass
from datetime import timedelta

from .config import Context
from .db import ts
from .match import Decision
from .models import Classification, Item, now
from .normalize import compact, pretty_when
from .notify import ntfy
from .notify.whatsapp import WhatsApp, clean_param, detail_param, fit_params

log = logging.getLogger(__name__)

STATUS_TEXT = {
    "presale_signup": "presale sign-up open",
    "on_sale": "tickets on sale",
    "sold_out": "sold out",
    "waitlist": "waitlist open",
    "second_date": "extra date added",
    "resale": "resale open",
}
TICKET_HOSTS = ("dice.fm", "ra.co", "shotgun.live", "ticketmaster", "eventbrite", "seetickets", "ticketswap",
                "linktr.ee", "fourvenues", "tixr", "eventix", "paylogic", "weeztix", "ticketkantoor", "forms.",
                "typeform", "laylo", "amsterdam-dance-event.nl")
TEMPLATE_TEXT = ("Up the scouse\nNew ADE event spotted by the radar. Event: {0} When: {1} Where: {2} "
                 "Details: {3} Check the link and grab tickets fast, these sell out.")


def reason_hash(event_id: int | str, kind: str) -> str:
    return hashlib.sha1(f"{event_id}|{kind}".encode()).hexdigest()[:12]


def best_link(item: Item, cls: Classification) -> str:
    links = [u for u in [*cls.links, *item.links] if u.startswith("http") and "cdninstagram" not in u
             and "fbcdn" not in u and "unsubscribe" not in u.lower()]
    for u in links:
        if any(h in u.lower() for h in TICKET_HOSTS):
            return u
    profile = f"https://www.instagram.com/{item.author}/" if item.source.startswith("instagram") and item.author \
        else ""
    if profile and re.search(r"link\s*in\s*(?:our\s*)?bio", item.text or "", re.I):
        return profile
    if item.source == "instagram_story" and profile:
        return profile  # story links expire after 24h; the account page does not
    if item.url:
        return item.url
    if links:
        return links[0]
    return profile or "https://www.amsterdam-dance-event.nl/en/program/"


def template_params(item: Item, cls: Classification, d: Decision, detail: str | None = None,
                    emoji: str = "") -> list[str]:
    name = cls.event_name or ""
    extra = [a for a in cls.artists if compact(a) not in compact(name)][:3]
    if name and extra:
        p1 = f"{name} ({', '.join(extra)})"
    elif name:
        p1 = name
    elif cls.artists:
        p1 = ", ".join(cls.artists[:4])
    else:
        p1 = f"Post by @{item.author}" if item.author else "New ADE item"
    if emoji:
        p1 = f"{emoji} {p1}"
    if detail is None:
        detail = STATUS_TEXT.get(cls.ticket_status, "new announcement")
        who = f"@{item.author}" if item.source.startswith("instagram") and item.author else ""
        if who and who.lower() not in p1.lower():
            detail += f" ({who})"
    if d.gold:
        detail = "GOLD - POP-UP: " + detail
    elif d.urgency == "urgent":
        detail = "URGENT: " + detail
    return [clean_param(p1), clean_param(pretty_when(cls.date, cls.time)), clean_param(cls.venue or "TBA"),
            detail_param(detail, best_link(item, cls))]


def render(params: list[str], n_params: int = 4) -> str:
    """Readable preview of what WhatsApp will show, fitted to the template's parameter count."""
    if n_params == 4 and len(params) == 4:
        return TEMPLATE_TEXT.format(*params)
    return "\n".join(f"{{{{{i}}}}} {p}" for i, p in enumerate(fit_params(params, n_params), 1))


@dataclass
class AlertStats:
    sent: int = 0
    failed: int = 0
    skipped: int = 0
    dry_run: int = 0

    def as_dict(self) -> dict:
        return dict(self.__dict__)


def _already(conn, item_id: str, rh: str, recipient: str) -> bool:
    row = conn.execute("SELECT 1 FROM alerts WHERE reason_hash=? AND recipient=? AND status='sent'",
                       (rh, recipient)).fetchone()
    if row:
        return True
    row = conn.execute("SELECT status FROM alerts WHERE item_id=? AND reason_hash=? AND recipient=?",
                       (item_id, rh, recipient)).fetchone()
    return bool(row and row["status"] in ("sent", "dry_run"))


def sent_today(conn, recipient: str) -> int:
    day = now().date().isoformat()
    return conn.execute("SELECT COUNT(*) FROM alerts WHERE recipient=? AND status='sent' AND urgency!='urgent'"
                        " AND substr(sent_at,1,10)=?", (recipient, day)).fetchone()[0]


def _record(conn, item_id, rh, recipient, urgency, status, mid="", error="", params=None):
    conn.execute(
        "INSERT INTO alerts (item_id, reason_hash, recipient, urgency, sent_at, wa_message_id, status, error,"
        " params, attempts) VALUES (?,?,?,?,?,?,?,?,?,?) ON CONFLICT(item_id, reason_hash, recipient) DO UPDATE SET"
        " urgency=excluded.urgency, sent_at=excluded.sent_at, wa_message_id=excluded.wa_message_id,"
        " status=excluded.status, error=excluded.error, params=COALESCE(excluded.params, alerts.params),"
        " attempts=alerts.attempts + excluded.attempts",
        (item_id, rh, recipient, urgency, ts(), mid, status, error[:500],
         json.dumps(params, ensure_ascii=False) if params else None, 0 if status == "dry_run" else 1),
    )


MAX_ATTEMPTS = 5
RETRY_HOURS = 24


def retry_failed(conn, ctx: Context, wa: WhatsApp, stats: AlertStats) -> None:
    """Resend alerts that failed earlier (WhatsApp down, token issue, Meta holding back a message)."""
    s = ctx.settings
    since = (now() - timedelta(hours=RETRY_HOURS)).isoformat(timespec="seconds")
    rows = conn.execute("SELECT * FROM alerts WHERE status='failed' AND attempts < ? AND sent_at >= ?"
                        " AND params IS NOT NULL", (MAX_ATTEMPTS, since)).fetchall()
    for r in rows:
        if _already(conn, "", r["reason_hash"], r["recipient"]):
            _record(conn, r["item_id"], r["reason_hash"], r["recipient"], r["urgency"], "superseded")
            continue
        phone = s.recipients.get(r["recipient"])
        if not phone:
            continue
        params = json.loads(r["params"])
        res = wa.send_template(phone, params)
        if res.ok:
            stats.sent += 1
            _record(conn, r["item_id"], r["reason_hash"], r["recipient"], r["urgency"], "sent", res.message_id)
            log.info("retried alert sent to %s: %s", r["recipient"], params[0])
        else:
            stats.failed += 1
            _record(conn, r["item_id"], r["reason_hash"], r["recipient"], r["urgency"], "failed", error=res.error)
        conn.commit()


def dispatch(conn, ctx: Context, wa: WhatsApp | None, item: Item, cls: Classification, d: Decision,
             event_id: int | str, stats: AlertStats, dry_run: bool, detail: str | None = None) -> None:
    s = ctx.settings
    rh = reason_hash(event_id, d.kind)
    params = template_params(item, cls, d, detail, emoji=s.alert_emoji)
    cap = int(ctx.taste.constraints.get("max_alerts_per_person_per_day") or 15)
    sent_now = 0
    # Sent to anyone in an earlier run? Then it is old news: people added later get no catch-up. Someone whose
    # send failed was meant to get it, so they still do.
    old_news = conn.execute("SELECT 1 FROM alerts WHERE reason_hash=? AND status='sent'", (rh,)).fetchone()
    for person in d.recipients:
        tried = conn.execute("SELECT 1 FROM alerts WHERE reason_hash=? AND recipient=? AND status='failed'",
                             (rh, person)).fetchone()
        if (old_news and not tried) or _already(conn, item.id, rh, person):
            stats.skipped += 1
            continue
        if d.urgency != "urgent" and sent_today(conn, person) >= cap:
            log.info("daily cap reached for %s, skipping normal alert", person)
            stats.skipped += 1
            continue
        phone = s.recipients.get(person)
        if dry_run:
            print(f"\n--- [dry-run] WhatsApp to {person} ({d.urgency}) ---\n{render(params, s.wa_body_params)}\n")
            log.info("[dry-run] WhatsApp to %s (%s): %s", person, d.urgency, " | ".join(params))
            _record(conn, item.id, rh, person, d.urgency, "dry_run")
            stats.dry_run += 1
            continue
        if not phone or wa is None:
            log.warning("no phone number for %s; alert not sent", person)
            stats.skipped += 1
            continue
        res = wa.send_template(phone, params)
        if res.ok:
            stats.sent += 1
            sent_now += 1
            _record(conn, item.id, rh, person, d.urgency, "sent", res.message_id, params=params)
            log.info("alert sent to %s: %s", person, params[0])
        else:
            stats.failed += 1
            first_failure = conn.execute("SELECT 1 FROM alerts WHERE item_id=? AND reason_hash=? AND recipient=?",
                                         (item.id, rh, person)).fetchone() is None
            _record(conn, item.id, rh, person, d.urgency, "failed", error=res.error, params=params)
            if first_failure:
                # Don't lose the alert while WhatsApp refuses it (e.g. 131049 marketing limit); retried next run.
                ntfy.send(s, f"For {person}. WhatsApp failed ({res.error[:120]}).\n\n{render(params, s.wa_body_params)}",
                          title=f"ADE {d.urgency} (WhatsApp failed): {params[0][:50]}",
                          priority="urgent" if d.urgency == "urgent" else "default")
        conn.commit()
    if not dry_run and s.wa_template == "hello_world" and sent_now:
        # Template not approved yet: hello_world carries no details, so mirror the alert to ntfy.
        ntfy.send(s, render(params, s.wa_body_params), title=f"ADE {d.urgency}: {params[0][:60]}",
                  priority="urgent" if d.urgency == "urgent" else "default")
