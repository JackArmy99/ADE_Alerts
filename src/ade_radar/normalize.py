"""Name and text normalisation for deterministic matching."""

from __future__ import annotations

import re
import unicodedata
from datetime import date, datetime, timedelta

_COUNTRY = re.compile(r"\s*\((?:[A-Z]{2,3}(?:[/,]\s*[A-Z]{2,3})*)\)")
_SUFFIX = re.compile(r"\b(?:live|all night long|extended(?: set)?|dj set|\(live\))\s*$", re.I)
_SPLIT = re.compile(r"\s+(?:b2b|b3b|vs\.?|x|presents|pres\.?|&|ft\.?|feat\.?)\s+|\s*[,+/|&\n]\s*", re.I)
SHORT = 4  # names this short only match exactly


def strip_accents(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c))


def norm(s: str) -> str:
    """casefold, strip accents, non-alphanumerics to spaces, collapse."""
    s = strip_accents(s).casefold().replace("&", " and ")
    s = re.sub(r"[^0-9a-z]+", " ", s)
    return " ".join(s.split())


def compact(s: str) -> str:
    return norm(s).replace(" ", "")


def clean_artist(name: str) -> str:
    """'Amelie Lens (BE)' -> 'Amelie Lens'; drops 'live', 'all night long', 'extended'."""
    name = _COUNTRY.sub("", name).strip(" -–—:")
    prev = None
    while prev != name:
        prev = name
        name = _SUFFIX.sub("", name).strip(" -–—:()")
    return name.strip()


def split_lineup(s: str) -> list[str]:
    parts = [clean_artist(p) for p in _SPLIT.split(_COUNTRY.sub("", s))]
    return [p for p in parts if p]


def is_short(name: str) -> bool:
    return len(compact(name)) <= SHORT


class NamePattern:
    """Matches one taste name (plus aliases) inside free text."""

    def __init__(self, name: str, aliases: list[str] | None = None):
        self.name = name
        variants = {clean_artist(name), *(aliases or [])}
        self.short = is_short(name)
        if self.short:
            # Exact, case-sensitive, word-bounded match of the name as written (e.g. "MK", "PAX").
            self.regexes = [re.compile(rf"(?<![\w]){re.escape(v.strip('.'))}(?![\w])") for v in variants]
            self.tokens = set()
        else:
            self.regexes = [re.compile(rf"(?:^| ){re.escape(norm(v))}(?: |$)") for v in variants if norm(v)]
            self.tokens = {compact(v) for v in variants if compact(v)}

    def search(self, raw_text: str, normed: str, tokens: set[str]) -> bool:
        if self.short:
            return any(r.search(raw_text) for r in self.regexes)
        if any(r.search(normed) for r in self.regexes):
            return True
        # handles and hashtags: @joshbaker, #kettama, @paigetomlinsonmusic
        for t in self.tokens:
            if t in tokens:
                return True
            if len(t) >= 6 and any(tok.startswith(t) and len(tok) - len(t) <= 6 for tok in tokens):
                return True
        return False


def text_tokens(raw_text: str) -> set[str]:
    return {compact(t) for t in re.findall(r"[@#]?[\w.]+", strip_accents(raw_text)) if t}


def equal_names(a: str, b: str) -> bool:
    return compact(clean_artist(a)) == compact(clean_artist(b))


# ---- dates -------------------------------------------------------------

def parse_date(s: str | None) -> date | None:
    if not s:
        return None
    try:
        return date.fromisoformat(s[:10])
    except ValueError:
        return None


def night_of(d: str | None, t: str | None) -> date | None:
    """Events that start after midnight (before 06:00) belong to the previous night."""
    dd = parse_date(d)
    if dd is None:
        return None
    m = re.match(r"(\d{1,2})[:.](\d{2})", t or "")
    if m and int(m.group(1)) < 6:
        return dd - timedelta(days=1)
    return dd


def pretty_when(d: str | None, t: str | None) -> str:
    dd = parse_date(d)
    if dd is None:
        return "TBA"
    out = datetime(dd.year, dd.month, dd.day).strftime("%a %d %b").replace(" 0", " ")
    if t:
        out += f" {t}"
        n = night_of(d, t)
        if n and n != dd:
            out += f" ({n.strftime('%a')} night)"
    return out
