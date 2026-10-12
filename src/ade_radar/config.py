"""Settings from .env plus the YAML config in config/."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml
from dotenv import load_dotenv

TZ = ZoneInfo("Europe/Amsterdam")


def find_root() -> Path:
    """Project root: $ADE_RADAR_HOME, else the nearest parent of cwd (or of this file) with config/taste.yaml."""
    env = os.environ.get("ADE_RADAR_HOME")
    if env:
        return Path(env).resolve()
    for start in (Path.cwd(), Path(__file__).resolve()):
        for p in [start, *start.parents]:
            if (p / "config" / "taste.yaml").exists():
                return p
    return Path.cwd()


def _csv(value: str | None) -> list[str]:
    return [v.strip() for v in (value or "").split(",") if v.strip()]


@dataclass
class Settings:
    root: Path
    anthropic_api_key: str = ""
    claude_model: str = "claude-haiku-5-5"
    claude_max_calls: int = 60
    wa_token: str = ""
    wa_phone_number_id: str = ""
    wa_waba_id: str = ""
    wa_api_version: str = "v25.0"
    wa_template: str = "ade_alert"
    wa_template_lang: str = "en"
    recipients: dict[str, str] = field(default_factory=dict)  # name -> phone
    gmail_address: str = ""
    gmail_app_password: str = ""
    gmail_label: str = "ADE"
    ntfy_topic: str = ""
    ig_profile_dir: Path = Path("data/ig_profile")

    @property
    def data_dir(self) -> Path:
        return self.root / "data"

    @property
    def db_path(self) -> Path:
        return self.data_dir / "radar.sqlite"

    @property
    def screenshots_dir(self) -> Path:
        return self.data_dir / "screenshots"

    @property
    def logs_dir(self) -> Path:
        return self.data_dir / "logs"

    @property
    def owner(self) -> str:
        """First recipient is Jack (heartbeat and digest go only to him)."""
        return next(iter(self.recipients), "jack")

    def secrets(self) -> list[str]:
        return [s for s in (self.anthropic_api_key, self.wa_token, self.gmail_app_password, self.ntfy_topic) if s]


def load_settings(root: Path | None = None) -> Settings:
    root = root or find_root()
    load_dotenv(root / ".env")
    e = os.environ.get
    phones = _csv(e("WHATSAPP_RECIPIENTS"))
    names = _csv(e("RECIPIENT_NAMES"))
    if len(phones) > 5:
        raise ValueError("WHATSAPP_RECIPIENTS: the Meta test number allows at most 5 recipients")
    recipients = {}
    for i, phone in enumerate(phones):
        name = names[i].lower() if i < len(names) else f"person{i + 1}"
        recipients[name] = phone.lstrip("+").replace(" ", "")
    ig_dir = Path(e("IG_PROFILE_DIR") or "data/ig_profile")
    return Settings(
        root=root,
        anthropic_api_key=e("ANTHROPIC_API_KEY", ""),
        claude_model=e("CLAUDE_MODEL") or "claude-haiku-5-5",
        claude_max_calls=int(e("CLAUDE_MAX_CALLS_PER_RUN") or 60),
        wa_token=e("WHATSAPP_TOKEN", ""),
        wa_phone_number_id=e("WHATSAPP_PHONE_NUMBER_ID", ""),
        wa_waba_id=e("WHATSAPP_WABA_ID", ""),
        wa_api_version=e("WHATSAPP_API_VERSION") or "v25.0",
        wa_template=e("WHATSAPP_TEMPLATE") or "ade_alert",
        wa_template_lang=e("WHATSAPP_TEMPLATE_LANG") or "en",
        recipients=recipients,
        gmail_address=e("GMAIL_ADDRESS", ""),
        gmail_app_password=(e("GMAIL_APP_PASSWORD") or "").replace(" ", ""),
        gmail_label=e("GMAIL_LABEL") or "ADE",
        ntfy_topic=e("NTFY_TOPIC", ""),
        ig_profile_dir=ig_dir if ig_dir.is_absolute() else root / ig_dir,
    )


@dataclass
class Account:
    name: str
    type: str = ""
    tier: int = 0
    priority: str = "normal"
    handle: str | None = None
    note: str = ""


@dataclass
class Taste:
    people: dict[str, dict]
    aliases: dict[str, list[str]]
    urgent_keywords: list[str]
    ade_keywords: list[str]
    constraints: dict
    raw_text: str


def load_taste(root: Path) -> Taste:
    path = root / "config" / "taste.yaml"
    text = path.read_text(encoding="utf-8")
    data = yaml.safe_load(text) or {}
    people = {str(k).lower(): (v or {}) for k, v in (data.get("people") or {}).items()}
    for p in people.values():
        for key in ("must_see", "like", "brands_like", "genres_like", "genres_avoid", "venues_like"):
            p[key] = [str(x) for x in (p.get(key) or [])]
    return Taste(
        people=people,
        aliases={str(k): [str(a) for a in v] for k, v in (data.get("aliases") or {}).items()},
        urgent_keywords=[str(k) for k in data.get("urgent_keywords") or []],
        ade_keywords=[str(k) for k in data.get("ade_keywords") or []],
        constraints=data.get("constraints") or {},
        raw_text=text,
    )


def load_accounts(root: Path) -> list[Account]:
    data = yaml.safe_load((root / "config" / "accounts.yaml").read_text(encoding="utf-8")) or {}
    out = []
    for a in data.get("accounts") or []:
        out.append(
            Account(
                name=str(a["name"]),
                type=a.get("type", ""),
                tier=int(a.get("tier") or 0),
                priority=a.get("priority") or "normal",
                handle=(str(a["handle"]).lstrip("@").lower() if a.get("handle") else None),
                note=a.get("note", ""),
            )
        )
    return out


@dataclass
class Context:
    settings: Settings
    taste: Taste
    accounts: list[Account]


@lru_cache(maxsize=1)
def load_context() -> Context:
    s = load_settings()
    return Context(settings=s, taste=load_taste(s.root), accounts=load_accounts(s.root))
