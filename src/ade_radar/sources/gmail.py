"""Gmail via IMAP: only the ADE label, read-only, processed UIDs tracked in SQLite."""

from __future__ import annotations

import email
import imaplib
import logging
import re
from datetime import timedelta
from email.message import EmailMessage
from email.policy import default as default_policy
from email.utils import parseaddr
from pathlib import Path

from bs4 import BeautifulSoup

from .. import db
from ..config import Context
from ..models import Item, SourceBlocked, now

log = logging.getLogger(__name__)

HOST = "imap.gmail.com"
FIRST_RUN_DAYS = 7
MAX_PER_RUN = 50
MAX_IMAGES = 3
MIN_IMAGE_BYTES = 8_000  # skip tracking pixels and tiny logos
# Account-security senders: never events, and they can contain login codes, so they never reach Claude.
SECURITY_SENDERS = re.compile(
    r"@(?:accounts\.google\.com|google\.com|mail\.instagram\.com|instagram\.com|facebookmail\.com|"
    r"meta\.com|support\.facebook\.com)>?$", re.I)
SECURITY_SUBJECT = re.compile(r"security alert|verification code|confirm your|sign[- ]in|log[- ]?in code|"
                              r"password|2-step|two-factor|passkey|new device", re.I)


def is_security_mail(item: Item) -> bool:
    sender = item.author.strip()
    subject = (item.raw or {}).get("subject", "")
    return bool(SECURITY_SENDERS.search(sender)) or bool(SECURITY_SUBJECT.search(subject))


SKIP_LINK = re.compile(r"unsubscribe|optout|opt-out|preferences|mailto:|privacy|/terms|facebook\.com/sharer|"
                       r"twitter\.com/intent|\.png|\.jpg|\.gif", re.I)


def parse_message(raw: bytes, uid: str, images_dir: Path | None = None) -> Item:
    msg: EmailMessage = email.message_from_bytes(raw, policy=default_policy)  # type: ignore[assignment]
    subject = str(msg.get("Subject", "")).strip()
    name, addr = parseaddr(str(msg.get("From", "")))
    sender = f"{name} <{addr}>" if name else addr
    plain, html = "", ""
    images: list[tuple[str, bytes]] = []
    for part in msg.walk():
        ctype = part.get_content_type()
        if part.is_multipart():
            continue
        try:
            if ctype == "text/plain" and not plain:
                plain = part.get_content()
            elif ctype == "text/html" and not html:
                html = part.get_content()
            elif ctype in ("image/png", "image/jpeg", "image/gif", "image/webp") and len(images) < MAX_IMAGES:
                data = part.get_payload(decode=True) or b""
                if len(data) >= MIN_IMAGE_BYTES:
                    images.append((ctype, data))
        except (LookupError, ValueError):
            continue
    links: list[str] = []
    if html:
        soup = BeautifulSoup(html, "html.parser")
        for a in soup.find_all("a", href=True):
            href = a["href"].strip()
            if href.startswith("http") and not SKIP_LINK.search(href) and href not in links:
                links.append(href)
        if not plain:
            for t in soup(["script", "style"]):
                t.decompose()
            plain = soup.get_text("\n")
    for u in re.findall(r"https?://[^\s<>\")\]]+", plain):
        if not SKIP_LINK.search(u) and u not in links:
            links.append(u)
    text = re.sub(r"\n\s*\n+", "\n\n", plain).strip()
    paths: list[str] = []
    if images_dir is not None and images:
        images_dir.mkdir(parents=True, exist_ok=True)
        for i, (ctype, data) in enumerate(images):
            ext = {"image/png": ".png", "image/jpeg": ".jpg", "image/gif": ".gif", "image/webp": ".webp"}[ctype]
            p = images_dir / f"mail_{uid}_{i}{ext}"
            p.write_bytes(data)
            paths.append(str(p))
    return Item(source="gmail", external_id=uid, url="", author=sender, text=f"Subject: {subject}\n\n{text}",
                screenshot_paths=paths, links=links[:25], posted_at=str(msg.get("Date", "")),
                raw={"subject": subject, "message_id": str(msg.get("Message-ID", ""))})


def collect(ctx: Context, conn, counts: dict) -> list[Item]:
    s = ctx.settings
    if not (s.gmail_address and s.gmail_app_password):
        raise RuntimeError("GMAIL_ADDRESS / GMAIL_APP_PASSWORD not set in .env")
    imap = imaplib.IMAP4_SSL(HOST, 993, timeout=60)
    try:
        try:
            imap.login(s.gmail_address, s.gmail_app_password)
        except imaplib.IMAP4.error as e:
            raise SourceBlocked(f"Gmail login failed (check the app password): {e}") from None
        typ, _ = imap.select(f'"{s.gmail_label}"', readonly=True)  # only the ADE label, never mark as read
        if typ != "OK":
            raise RuntimeError(f"Gmail label '{s.gmail_label}' not found (create the filter + label first)")
        validity = _uidvalidity(imap)
        key_v, key_u = "gmail_uidvalidity", "gmail_last_uid"
        if db.kv_get(conn, key_v) != validity:
            db.kv_set(conn, key_v, validity)
            db.kv_set(conn, key_u, "0")
        last_uid = int(db.kv_get(conn, key_u, "0") or 0)
        if last_uid == 0:
            since = (now() - timedelta(days=FIRST_RUN_DAYS)).strftime("%d-%b-%Y")
            typ, data = imap.uid("search", None, f"SINCE {since}")
        else:
            typ, data = imap.uid("search", None, f"UID {last_uid + 1}:*")
        uids = sorted(int(u) for u in (data[0] or b"").split() if int(u) > last_uid)
        counts["mails_found"] = len(uids)
        items = []
        img_dir = s.screenshots_dir / "gmail"
        for uid in uids[:MAX_PER_RUN]:
            typ, msg_data = imap.uid("fetch", str(uid), "(BODY.PEEK[])")
            raw = next((p[1] for p in msg_data if isinstance(p, tuple)), None)
            if raw:
                item = parse_message(raw, f"{validity}:{uid}", img_dir)
                if is_security_mail(item):
                    counts["mails_security_skipped"] = counts.get("mails_security_skipped", 0) + 1
                    log.info("gmail: skipped account/security mail from %s", item.author)
                else:
                    items.append(item)
            db.kv_set(conn, key_u, str(uid))
        counts["mails_new"] = len(items)
        return items
    finally:
        try:
            imap.logout()
        except Exception:  # noqa: BLE001
            pass


def _uidvalidity(imap) -> str:
    typ, data = imap.response("UIDVALIDITY")
    if data and data[0]:
        return data[0].decode() if isinstance(data[0], bytes) else str(data[0])
    return "0"
