"""Deterministic matching against taste.yaml and accounts.yaml, and the final alert decision."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .config import Account, Context, Taste
from .models import Classification, Item
from .normalize import NamePattern, compact, norm, text_tokens


@dataclass
class Hit:
    name: str
    level: str  # must_see | like | brand | venue


@dataclass
class MatchResult:
    hits: dict[str, list[Hit]] = field(default_factory=dict)  # person -> hits
    urgent_keywords: list[str] = field(default_factory=list)
    ade_related: bool = False
    account: Account | None = None

    @property
    def persons(self) -> set[str]:
        return {p for p, h in self.hits.items() if any(x.level != "venue" for x in h)}

    @property
    def priority_urgent(self) -> bool:
        return bool(self.account and self.account.priority == "urgent")

    def must_see_for(self, person: str) -> list[str]:
        return [h.name for h in self.hits.get(person, []) if h.level == "must_see"]


class Matcher:
    def __init__(self, taste: Taste, accounts: list[Account]):
        self.taste = taste
        self.accounts = accounts
        alias_by_key = {compact(k): v for k, v in taste.aliases.items()}
        self.people: dict[str, list[tuple[NamePattern, str]]] = {}
        for person, prof in taste.people.items():
            pats = []
            for level, key in (("must_see", "must_see"), ("like", "like"), ("brand", "brands_like"),
                               ("venue", "venues_like")):
                for name in prof.get(key, []):
                    pats.append((NamePattern(name, alias_by_key.get(compact(name))), level))
            self.people[person] = pats
        self.urgent_res = [(k, _kw_regex(k)) for k in taste.urgent_keywords]
        self.ade_res = [_kw_regex(k, case_sensitive=(k == "ADE")) for k in taste.ade_keywords]

    def account_for(self, handle: str | None) -> Account | None:
        return account_for_handle(handle, self.accounts)

    def match(self, text: str, author: str = "", extra_names: list[str] | None = None) -> MatchResult:
        full = " ".join([text or "", " ".join(extra_names or [])])
        normed = norm(full)
        tokens = text_tokens(full + " " + (author or ""))
        res = MatchResult(account=self.account_for(author))
        for person, pats in self.people.items():
            hits = []
            for pat, level in pats:
                if pat.search(full, normed, tokens):
                    hits.append(Hit(pat.name, level))
            # The author's own account counts as a mention (e.g. a post by @joshbaker).
            if res.account and not any(compact(h.name) == compact(res.account.name) for h in hits):
                for pat, level in pats:
                    if compact(pat.name) == compact(res.account.name):
                        hits.append(Hit(pat.name, level))
            if hits:
                res.hits[person] = _best_levels(hits)
        res.urgent_keywords = [k for k, r in self.urgent_res if r.search(full)]
        res.ade_related = any(r.search(full) for r in self.ade_res)
        return res


def _kw_regex(k: str, case_sensitive: bool = False) -> re.Pattern:
    body = re.escape(k).replace(r"\ ", r"[\s\-]*")
    return re.compile(rf"(?<!\w){body}(?!\w)", 0 if case_sensitive else re.I)


def _best_levels(hits: list[Hit]) -> list[Hit]:
    order = {"must_see": 0, "like": 1, "brand": 2, "venue": 3}
    seen: dict[str, Hit] = {}
    for h in sorted(hits, key=lambda h: order[h.level]):
        seen.setdefault(compact(h.name), h)
    return list(seen.values())


def account_for_handle(handle: str | None, accounts: list[Account]) -> Account | None:
    if not handle:
        return None
    h = handle.lstrip("@").lower()
    hc = compact(h)
    for a in accounts:
        if a.handle and a.handle == h:
            return a
    for a in accounts:
        for name in re.split(r"\s*/\s*", a.name):
            nc = compact(name)
            if len(nc) >= 5 and (hc == nc or (hc.startswith(nc) and len(hc) - len(nc) <= 8)):
                return a
    return None


# ---- decision ----------------------------------------------------------

@dataclass
class Decision:
    send: bool
    urgency: str = "ignore"
    recipients: list[str] = field(default_factory=list)
    kind: str = ""
    reason: str = ""


def decide(item: Item, cls: Classification, m: MatchResult, ctx: Context) -> Decision:
    known = list(ctx.settings.recipients) or list(ctx.taste.people)
    used_claude = cls.model != "rules"
    ade = cls.is_ade_related if used_claude else (m.ade_related or item.source == "ade")
    if item.source == "ade":
        ade = True
    if not ade:
        return Decision(False, reason="not ADE related")

    persons = ({p for p in cls.relevant_for if p in known} | m.persons) & set(known)
    urgent = cls.urgency == "urgent"
    why = []
    if m.urgent_keywords and (persons or m.account or cls.is_event_announcement):
        urgent = True
        why.append("keywords: " + ", ".join(m.urgent_keywords[:3]))
    if m.priority_urgent and item.source.startswith("instagram"):
        urgent = True
        why.append(f"priority account {m.account.name}")
    if cls.ticket_status in ("presale_signup", "waitlist", "second_date", "resale") and (persons or m.account):
        urgent = True

    if urgent:
        if m.priority_urgent:
            recipients = known
        else:
            recipients = sorted(persons) or [ctx.settings.owner]
        kind = cls.ticket_status if cls.ticket_status != "unknown" else "urgent"
        return Decision(True, "urgent", _ordered(recipients, known), kind, "; ".join(why) or cls.reason)

    if cls.urgency == "ignore" and used_claude and not persons:
        return Decision(False, reason=cls.reason or "ignored by classifier")
    if not persons:
        return Decision(False, reason="no matching taste")
    if used_claude and not cls.is_event_announcement and cls.urgency == "ignore":
        return Decision(False, reason=cls.reason or "not an announcement")
    kind = cls.ticket_status if cls.ticket_status in ("sold_out",) else "announce"
    return Decision(True, "normal", _ordered(persons, known), kind, cls.reason)


def _ordered(names, known: list[str]) -> list[str]:
    s = set(names)
    return [k for k in known if k in s]
