"""09:00 heartbeat and suggested-follows digest (Jack only)."""

from __future__ import annotations

import json
import statistics
from datetime import datetime
from email.utils import parseaddr
from datetime import timedelta

from . import db
from .config import Context
from .models import now
from .notify import ntfy
from .notify.whatsapp import WhatsApp, clean_param, detail_param

SOURCES = ("instagram", "gmail", "ade")


def _domain(author: str) -> str:
    addr = parseaddr(author or "")[1].lower()
    host = addr.split("@")[-1] if "@" in addr else ""
    parts = host.split(".")
    return ".".join(parts[-2:]) if len(parts) >= 2 else (host or "unknown")


def email_report(conn) -> str:
    """Per email sender: how many events the mails brought, how often the email was first, and how many hours
    after the first source (Instagram / ADE programme) the email reached the radar."""
    by_id = {r["id"]: r for r in conn.execute("SELECT id, source, author, first_seen FROM items")}
    stats: dict[str, dict] = {}
    for r in conn.execute("SELECT id, author FROM items WHERE source='gmail'"):
        d = stats.setdefault(_domain(r["author"]), {"mails": 0, "alerts": 0, "events": 0, "first": 0, "lags": []})
        d["mails"] += 1
        if conn.execute("SELECT 1 FROM alerts WHERE item_id=? AND status='sent' LIMIT 1", (r["id"],)).fetchone():
            d["alerts"] += 1
    for ev in conn.execute("SELECT sources FROM events"):
        rows = [by_id[i] for i in json.loads(ev["sources"] or "[]") if i in by_id]
        mails = [r for r in rows if r["source"] == "gmail"]
        others = [r for r in rows if r["source"] != "gmail"]
        for m in mails:
            d = stats.setdefault(_domain(m["author"]), {"mails": 0, "alerts": 0, "events": 0, "first": 0, "lags": []})
            d["events"] += 1
            if not others:
                d["first"] += 1
                continue
            t_mail = datetime.fromisoformat(m["first_seen"])
            t_first = min(datetime.fromisoformat(o["first_seen"]) for o in others)
            if t_mail <= t_first:
                d["first"] += 1
            else:
                d["lags"].append((t_mail - t_first).total_seconds() / 3600)
    if not stats:
        return "Email report: no ADE emails processed yet."
    lines = ["Email report (since the radar started):"]
    for dom, d in sorted(stats.items(), key=lambda x: -x[1]["mails"]):
        lag = f", otherwise median {statistics.median(d['lags']):.1f}h later" if d["lags"] else ""
        lines.append(f"{dom}: {d['mails']} mails, {d['alerts']} led to an alert; matched {d['events']} events, "
                     f"first source for {d['first']}{lag}")
    return "\n".join(lines)


def heartbeat_summary(conn) -> tuple[str, bool]:
    since = (now() - timedelta(hours=24)).isoformat(timespec="seconds")
    rows = conn.execute("SELECT * FROM runs WHERE started>=? AND finished IS NOT NULL", (since,)).fetchall()
    parts, healthy = [], True
    totals = {"posts": 0, "mails": 0, "claude": 0}
    for src in SOURCES:
        rs = [r for r in rows if r["source"] == src]
        ok = sum(1 for r in rs if r["ok"])
        bad = len(rs) - ok
        if bad and ok == 0 or not rs:
            healthy = False
        parts.append(f"{src} {ok}/{len(rs)} ok")
        for r in rs:
            c = json.loads(r["counts"] or "{}")
            totals["posts"] += int(c.get("posts_checked", 0) or 0) + int(c.get("stories_new", 0) or 0)
            totals["mails"] += int(c.get("mails_new", 0) or 0)
            totals["claude"] += int(c.get("claude_calls", 0) or 0)
    sent = conn.execute("SELECT COUNT(*) FROM alerts WHERE status='sent' AND sent_at>=?", (since,)).fetchone()[0]
    today_eur, total_eur = db.spend_so_far(conn)
    text = (f"{', '.join(parts)}. IG posts/stories checked {totals['posts']}, mails {totals['mails']}, "
            f"alerts sent {sent}, Claude calls {totals['claude']}, Claude spend EUR {today_eur:.2f} today / "
            f"EUR {total_eur:.2f} total")
    return text, healthy


def heartbeat(ctx: Context, dry_run: bool = False) -> str:
    s = ctx.settings
    conn = db.connect(s.db_path)
    text, healthy = heartbeat_summary(conn)
    conn.close()
    status = "all OK" if healthy else "CHECK LAPTOP"
    ntfy.send(s, text, title=f"ade-radar heartbeat: {status}", priority="default" if healthy else "high",
              dry_run=dry_run)
    params = ["Radar heartbeat (not an event)", now().strftime("%a %d %b %H:%M"), status, detail_param(text, None)]
    _to_owner(ctx, params, dry_run)
    return text


def digest(ctx: Context, dry_run: bool = False, limit: int = 15) -> str:
    """09:00 digest for Jack: ADE teasers (mentions without details) and suggested follows."""
    s = ctx.settings
    conn = db.connect(s.db_path)
    out = [teaser_digest(conn, ctx, dry_run, limit), follows_digest(conn, ctx, dry_run, limit)]
    report = email_report(conn)
    ntfy.send(s, report, title="ade-radar: email report", dry_run=dry_run)
    out.append(report)
    if not dry_run:
        conn.commit()
    conn.close()
    return "\n\n".join(x for x in out if x) or "nothing new for the digest"


def teaser_digest(conn, ctx: Context, dry_run: bool, limit: int) -> str:
    rows = conn.execute("SELECT * FROM teasers WHERE digested_at IS NULL ORDER BY created_at LIMIT ?",
                        (limit,)).fetchall()
    if not rows:
        return ""
    lines = [f"@{r['author']}: {r['summary']} {r['url']}".strip() for r in rows]
    full = "ADE teasers and programme updates:\n" + "\n".join(lines)
    ntfy.send(ctx.settings, full, title=f"ade-radar: {len(rows)} ADE teasers", dry_run=dry_run)
    short = "; ".join(f"@{r['author']}" for r in rows)
    params = ["ADE teasers and programme updates", now().strftime("%a %d %b"), f"{len(rows)} items",
              clean_param("Full list in ntfy. " + short)]
    _to_owner(ctx, params, dry_run)
    if not dry_run:
        conn.executemany("UPDATE teasers SET digested_at=? WHERE item_id=?", [(db.ts(), r["item_id"]) for r in rows])
    return full


def follows_digest(conn, ctx: Context, dry_run: bool, limit: int) -> str:
    rows = conn.execute("SELECT * FROM suggested_follows WHERE suggested_at IS NULL ORDER BY times_seen DESC,"
                        " first_seen LIMIT ?", (limit,)).fetchall()
    if not rows:
        return ""
    lines = [f"@{r['handle']} x{r['times_seen']} via {', '.join('@' + v for v in (r['seen_via'] or '').split(',')[:3] if v)}"
             for r in rows]
    full = "Suggested follows:\n" + "\n".join(lines)
    ntfy.send(ctx.settings, full, title=f"ade-radar: {len(rows)} suggested follows", dry_run=dry_run)
    short = "; ".join(f"@{r['handle']} x{r['times_seen']}" for r in rows)
    params = ["Suggested follows (not an event)", now().strftime("%a %d %b"), f"{len(rows)} new accounts",
              clean_param(short)]
    _to_owner(ctx, params, dry_run)
    if not dry_run:
        conn.executemany("UPDATE suggested_follows SET suggested_at=? WHERE handle=?",
                         [(db.ts(), r["handle"]) for r in rows])
    return full


def _to_owner(ctx: Context, params: list[str], dry_run: bool) -> None:
    s = ctx.settings
    owner = s.owner
    if dry_run:
        print(f"[dry-run WhatsApp to {owner}] " + " | ".join(params))
        return
    phone = s.recipients.get(owner)
    if phone and s.wa_token:
        WhatsApp(s).send_template(phone, params)
