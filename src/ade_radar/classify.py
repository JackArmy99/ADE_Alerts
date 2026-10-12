"""Claude Haiku classification of posts, stories and mails (vision for flyers)."""

from __future__ import annotations

import base64
import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

import anthropic

from .config import Context
from .models import TICKET_STATUSES, Classification, Item

log = logging.getLogger(__name__)

MAX_IMAGE_BYTES = 4_500_000
MAX_TEXT = 6000

INSTRUCTIONS = """You classify social media posts, Instagram stories and emails for a personal alert bot.
A group of friends is going to Amsterdam Dance Event (ADE) 2026, 21 to 25 October 2026, and wants to know
immediately when an event they care about is announced, especially presales and sign-ups, because these sell
out in minutes. Today's date and the source are given with each item. Read flyer images carefully: lineups,
dates, venues and ticket info are often only in the image.

Return ONLY one JSON object with these fields:
- is_ade_related: true if the item is about an event in Amsterdam (or nearby, e.g. Halfweg) during
  21-25 Oct 2026 / ADE week. Past events, other cities and other dates are false.
- is_event_announcement: true if it announces or updates a specific event (lineup, tickets, sign-up, set times,
  sold out, extra date). Recaps, memes and generic promo are false.
- event_name, artists (lineup names without country codes), date (YYYY-MM-DD), time (HH:MM, start), venue:
  null / [] when unknown. Never guess a date or venue that is not shown.
- ticket_status: presale_signup | on_sale | sold_out | waitlist | second_date | resale | unknown
- links: ticket / sign-up / info URLs visible in the text (not image CDN links).
- urgency: "urgent" when ANY of: presale, pre-registration, sign-up, register, waitlist / waiting list,
  second date / extra show / added date, extra tickets / final release, resale / ticketswap,
  "link in bio" + ADE, secret / special guest reveal for a must_see artist, or any ADE-related post by an
  account listed as priority urgent below. "normal" for other ADE event announcements that match someone's
  taste. "ignore" for everything else.
- relevant_for: names of the people (keys under `people` in the taste file) whose must_see, like or
  brands_like lists (or clearly matching genres) fit this event. Use only names from the taste file.
- reason: one short sentence.

Accounts listed as priority urgent (any ADE-related post from them is urgent):
{priority_accounts}

Taste file (config/taste.yaml):
```yaml
{taste}
```"""

SCHEMA = {
    "type": "object",
    "properties": {
        "is_ade_related": {"type": "boolean"},
        "is_event_announcement": {"type": "boolean"},
        "event_name": {"anyOf": [{"type": "string"}, {"type": "null"}]},
        "artists": {"type": "array", "items": {"type": "string"}},
        "date": {"anyOf": [{"type": "string"}, {"type": "null"}]},
        "time": {"anyOf": [{"type": "string"}, {"type": "null"}]},
        "venue": {"anyOf": [{"type": "string"}, {"type": "null"}]},
        "ticket_status": {"type": "string", "enum": TICKET_STATUSES},
        "links": {"type": "array", "items": {"type": "string"}},
        "urgency": {"type": "string", "enum": ["urgent", "normal", "ignore"]},
        "relevant_for": {"type": "array", "items": {"type": "string"}},
        "reason": {"type": "string"},
    },
    "required": ["is_ade_related", "is_event_announcement", "event_name", "artists", "date", "time", "venue",
                 "ticket_status", "links", "urgency", "relevant_for", "reason"],
    "additionalProperties": False,
}


@dataclass
class Usage:
    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read: int = 0
    cache_write: int = 0
    errors: int = 0
    skipped_cap: int = 0

    def as_dict(self) -> dict:
        return dict(self.__dict__)


@dataclass
class Classifier:
    ctx: Context
    max_calls: int | None = None
    client: object | None = None
    usage: Usage = field(default_factory=Usage)
    _use_schema: bool = True

    def __post_init__(self):
        s = self.ctx.settings
        if self.max_calls is None:
            self.max_calls = s.claude_max_calls
        if self.client is None and s.anthropic_api_key:
            self.client = anthropic.Anthropic(api_key=s.anthropic_api_key, max_retries=3, timeout=120)
        prio = [f"- {a.name}" + (f" (@{a.handle})" if a.handle else "") for a in self.ctx.accounts
                if a.priority == "urgent"]
        self.system_text = INSTRUCTIONS.format(priority_accounts="\n".join(prio) or "- none",
                                               taste=self.ctx.taste.raw_text.strip())

    @property
    def available(self) -> bool:
        return self.client is not None and self.usage.calls < (self.max_calls or 0)

    def classify(self, item: Item, today: str) -> Classification | None:
        """Returns None when Claude is unavailable (no key, cap reached, or API error); caller falls back to rules."""
        if self.client is None:
            return None
        if self.usage.calls >= (self.max_calls or 0):
            self.usage.skipped_cap += 1
            return None
        content = build_content(item, today)
        self.usage.calls += 1
        try:
            resp = self._create(content)
        except anthropic.BadRequestError as e:
            if self._use_schema:
                log.warning("Claude rejected the JSON schema (%s); retrying without it", e.message)
                self._use_schema = False
                try:
                    resp = self._create(content)
                except anthropic.APIError as e2:
                    return self._fail(e2)
            else:
                return self._fail(e)
        except anthropic.APIError as e:
            return self._fail(e)
        u = resp.usage
        self.usage.input_tokens += u.input_tokens or 0
        self.usage.output_tokens += u.output_tokens or 0
        self.usage.cache_read += getattr(u, "cache_read_input_tokens", 0) or 0
        self.usage.cache_write += getattr(u, "cache_creation_input_tokens", 0) or 0
        if resp.stop_reason == "refusal":
            log.warning("Claude refused item %s", item.external_id)
            self.usage.errors += 1
            return None
        text = "".join(b.text for b in resp.content if b.type == "text")
        data = extract_json(text)
        if data is None:
            log.warning("Claude returned no JSON for %s: %r", item.external_id, text[:200])
            self.usage.errors += 1
            return None
        return Classification.from_dict(data, model=self.ctx.settings.claude_model)

    def _create(self, content: list[dict]):
        kwargs = dict(
            model=self.ctx.settings.claude_model,
            max_tokens=4000,
            system=[{"type": "text", "text": self.system_text, "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user", "content": content}],
        )
        output_config: dict = {"effort": "low"}
        if self._use_schema:
            output_config["format"] = {"type": "json_schema", "schema": SCHEMA}
        kwargs["output_config"] = output_config
        return self.client.messages.create(**kwargs)

    def _fail(self, e: Exception) -> None:
        self.usage.errors += 1
        log.error("Claude call failed: %s", getattr(e, "message", type(e).__name__))
        return None


def build_content(item: Item, today: str) -> list[dict]:
    content: list[dict] = []
    for p in item.screenshot_paths[:4]:
        block = image_block(Path(p))
        if block:
            content.append(block)
    lines = [
        f"Today: {today}",
        f"Source: {item.source}",
        f"Author: @{item.author}" if item.author and item.source.startswith("instagram") else f"From: {item.author}",
        f"URL: {item.url}" if item.url else "",
        f"Tagged / mentioned: {', '.join('@' + t for t in item.tagged)}" if item.tagged else "",
        f"Posted: {item.posted_at}" if item.posted_at else "",
        "Links: " + " ".join(item.links[:15]) if item.links else "",
        "Text:",
        (item.text or "(no text, see image)")[:MAX_TEXT],
    ]
    content.append({"type": "text", "text": "\n".join(x for x in lines if x)})
    return content


def image_block(path: Path) -> dict | None:
    try:
        data = path.read_bytes()
    except OSError:
        return None
    if len(data) > MAX_IMAGE_BYTES:
        log.warning("image too large for Claude, skipped: %s", path.name)
        return None
    suffix = path.suffix.lower()
    media = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".gif": "image/gif",
             ".webp": "image/webp"}.get(suffix)
    if not media:
        return None
    return {"type": "image", "source": {"type": "base64", "media_type": media,
                                        "data": base64.standard_b64encode(data).decode("ascii")}}


def extract_json(text: str) -> dict | None:
    text = text.strip()
    try:
        return json.loads(text)
    except ValueError:
        pass
    m = re.search(r"\{.*\}", text, re.S)
    if m:
        try:
            return json.loads(m.group(0))
        except ValueError:
            return None
    return None


def rules_classification(item: Item, m) -> Classification:
    """Fallback when Claude is unavailable: deterministic signals only."""
    persons = sorted(m.persons)
    names = [h.name for hits in m.hits.values() for h in hits if h.level in ("must_see", "like")]
    status = "unknown"
    kws = " ".join(m.urgent_keywords).lower()
    if any(k in kws for k in ("presale", "pre-sale", "regist", "sign")):
        status = "presale_signup"
    elif "waitlist" in kws or "waiting list" in kws:
        status = "waitlist"
    elif any(k in kws for k in ("second date", "extra date", "added date", "new date")):
        status = "second_date"
    elif "resale" in kws or "ticketswap" in kws:
        status = "resale"
    elif re.search(r"sold[\s-]*out", item.text or "", re.I):
        status = "sold_out"
    return Classification(
        is_ade_related=m.ade_related,
        is_event_announcement=bool(persons) and m.ade_related,
        event_name=None,
        artists=sorted(set(names)),
        ticket_status=status,
        links=[u for u in item.links if u.startswith("http")][:5],
        urgency="urgent" if (m.urgent_keywords and m.ade_related) else ("normal" if persons else "ignore"),
        relevant_for=persons,
        reason="rules: " + ", ".join(sorted(set(names))[:5]),
        model="rules",
    )
