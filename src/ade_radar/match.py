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

ACTIONABLE = ("presale_signup", "waitlist", "second_date", "resale", "on_sale")


@dataclass
class Decision:
    send: bool
    urgency: str = "ignore"
    recipients: list[str] = field(default_factory=list)
    kind: str = ""
    reason: str = ""
    gold: bool = False  # pop-up with a must_see artist: the top category


DEFAULT_POPUP = ["pop-up", "pop up", "popup", "secret location", "secret show", "secret set", "special guest",
                 "surprise set", "location tba"]


def popup_regex(taste: Taste) -> re.Pattern:
    words = taste.popup_keywords or DEFAULT_POPUP
    parts = (re.escape(w).replace(r"\ ", " ").replace(" ", r"[\s\-]*") for w in words)
    return re.compile("|".join(rf"(?<!\w){p}(?!\w)" for p in parts), re.I)


def is_popup(item: Item, cls: Classification, taste: Taste) -> bool:
    text = " ".join([item.text or "", cls.event_name or "", cls.reason or ""])
    return bool(popup_regex(taste).search(text))


def is_gold(item: Item, cls: Classification, m: MatchResult, persons: set[str], taste: Taste) -> bool:
    """Pop-up / secret show + a must_see artist (or a priority-urgent account) = gold standard."""
    if not is_popup(item, cls, taste):
        return False
    must = {compact(n) for p in persons for n in m.must_see_for(p)}
    must |= {compact(n) for p in persons for n in taste.people.get(p, {}).get("must_see", [])
             if any(compact(a) == compact(n) for a in cls.artists)}
    return m.priority_urgent or bool(must)


_MONTHS = (r"jan(?:uary|uari)?|feb(?:ruary|ruari)?|mar(?:ch)?|maart|apr(?:il)?|may|mei|june?|juni|july?|juli|"
           r"aug(?:ust|ustus)?|sep(?:t|tember)?|nov(?:ember)?|dec(?:ember)?")
OTHER_MONTH = re.compile(rf"\b\d{{1,2}}(?:st|nd|rd|th)?\s+(?:{_MONTHS})\b|\b(?:{_MONTHS})\s+\d{{1,2}}(?:st|nd|rd|th)?\b|"
                         rf"\b(?:{_MONTHS})\s+20\d\d\b", re.I)
OCTOBER = re.compile(r"\b(?:oct(?:ober)?|okt(?:ober)?)\b|\b2[1-5][./-]10\b|\bADE\b", re.I)


def outside_ade_week(item: Item, cls: Classification, taste: Taste) -> str:
    """Reason text when the item is clearly about a date outside ADE week, else ''.
    Known date: its night must fall in festival_dates (after-midnight sets count for the previous night).
    No date: a day+month that is not October (e.g. '14 March', 'March 2027') with no October / ADE mention."""
    from .normalize import night_of, parse_date

    days = [parse_date(d) for d in (taste.constraints.get("festival_dates") or ["2026-10-21", "2026-10-25"])]
    start, end = days[0], days[-1]
    night = night_of(cls.date, cls.time)
    if night is not None:
        return "" if start <= night <= end else f"outside ADE week ({cls.date})"
    text = " ".join([item.text or "", cls.event_name or ""])
    if OTHER_MONTH.search(text) and not OCTOBER.search(text):
        return "outside ADE week (mentions another month)"
    return ""


TIMETABLE = re.compile(r"\b(?:time\s*-?\s*tables?|set\s*-?\s*times?|running\s+orders?|stage\s+times?|"
                       r"set\s+schedule|tijdschema|blokkenschema)\b", re.I)
NEW_CHANCE = ("presale_signup", "waitlist", "second_date", "resale")


def is_timetable(item: Item, cls: Classification) -> bool:
    """Timetable / set-times posts are not alert worthy, unless they also open a new way to get tickets."""
    if cls.ticket_status in NEW_CHANCE:
        return False
    return bool(TIMETABLE.search(" ".join([item.text or "", cls.event_name or "", cls.reason or ""])))


def decide(item: Item, cls: Classification, m: MatchResult, ctx: Context) -> Decision:
    d = _decide(item, cls, m, ctx)
    if d.send and ctx.settings.alert_everyone:
        d.recipients = list(ctx.settings.recipients) or d.recipients  # the whole group gets every alert
    return d


def _decide(item: Item, cls: Classification, m: MatchResult, ctx: Context) -> Decision:
    known = list(ctx.settings.recipients) or list(ctx.taste.people)
    used_claude = cls.model != "rules"
    ade = cls.is_ade_related if used_claude else (m.ade_related or item.source == "ade")
    if item.source == "ade":
        ade = True
    if not ade:
        return Decision(False, reason="not ADE related")
    outside = outside_ade_week(item, cls, ctx.taste)
    if outside:
        return Decision(False, reason=outside)
    if is_timetable(item, cls):
        return Decision(False, reason="timetable / set times (not alert worthy)")

    persons = ({p for p in cls.relevant_for if p in known} | m.persons) & set(known)
    # People sharing one taste block (mc: *jack, or recipients without their own block) get the same alerts,
    # even when Claude only named one of them.
    shared = [ctx.taste.people[p] for p in persons if p in ctx.taste.people]
    persons |= {p for p in known if any(ctx.taste.people.get(p) is t for t in shared)}
    why = []
    # Claude's "urgent" only counts with something to act on: a ticket signal, an urgent keyword, a priority
    # account, or a reveal for a must_see artist. "Playing ADE" on its own is not urgent.
    actionable = cls.ticket_status in ACTIONABLE
    must_see_reveal = cls.is_event_announcement and any(m.must_see_for(p) for p in persons)
    urgent = cls.urgency == "urgent" and (actionable or bool(m.urgent_keywords) or m.priority_urgent
                                          or must_see_reveal)
    if m.urgent_keywords and (persons or m.account or cls.is_event_announcement):
        urgent = True
        why.append("keywords: " + ", ".join(m.urgent_keywords[:3]))
    if m.priority_urgent and item.source.startswith("instagram"):
        urgent = True
        why.append(f"priority account {m.account.name}")
    if actionable and cls.ticket_status != "on_sale" and (persons or m.account):
        urgent = True

    if persons and is_popup(item, cls, ctx.taste):
        urgent = True  # any pop-up with one of the group's artists is urgent
    gold = is_gold(item, cls, m, persons, ctx.taste)
    if gold:
        urgent = True
        why.insert(0, "GOLD: pop-up with a must-see artist")
    if urgent:
        if m.priority_urgent:
            recipients = known
        else:
            recipients = sorted(persons) or [ctx.settings.owner]
        kind = cls.ticket_status if cls.ticket_status != "unknown" else "urgent"
        return Decision(True, "urgent", _ordered(recipients, known), kind, "; ".join(why) or cls.reason, gold=gold)

    if cls.urgency == "ignore" and used_claude and not persons:
        return Decision(False, reason=cls.reason or "ignored by classifier")
    if not persons:
        return Decision(False, reason="no matching taste")
    if used_claude and not cls.is_event_announcement and cls.urgency == "ignore":
        return Decision(False, reason=cls.reason or "not an announcement")
    if not has_details(item, cls):
        # "show at ADE" with no date, venue or link: goes to the 09:00 digest instead of WhatsApp
        return Decision(False, "teaser", _ordered(persons, known), "teaser", cls.reason or "ADE mention, no details")
    kind = cls.ticket_status if cls.ticket_status in ("sold_out",) else "announce"
    return Decision(True, "normal", _ordered(persons, known), kind, cls.reason)


def has_details(item: Item, cls: Classification) -> bool:
    if item.source == "ade":
        return True
    return bool(cls.date or cls.venue or cls.links or cls.ticket_status != "unknown")


def _ordered(names, known: list[str]) -> list[str]:
    s = set(names)
    return [k for k in known if k in s]
