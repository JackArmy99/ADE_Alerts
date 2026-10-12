"""WhatsApp via the Meta Cloud API. Templates only (no webhooks, so no known 24h window)."""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass

import requests

from ..config import Settings

log = logging.getLogger(__name__)
GRAPH = "https://graph.facebook.com"
MAX_PARAM = 150

HINTS = {
    131030: "recipient is not on the test number's allow list (add + verify it in Meta API Setup)",
    131049: "Meta held back a marketing message (per-user marketing limit); a Utility template avoids this",
    132001: "template does not exist in this language: check WHATSAPP_TEMPLATE_LANG with check-template",
    132000: "wrong number of template parameters",
    132015: "template is paused",
    132016: "template is disabled",
    190: "access token invalid or expired",
}


class WhatsAppError(Exception):
    def __init__(self, message: str, code: int | None = None, status: int | None = None):
        super().__init__(message)
        self.code = code
        self.status = status


def clean_param(s: str, limit: int = MAX_PARAM) -> str:
    """Meta rules: no newlines/tabs, max 4 consecutive spaces, non-empty. We also keep it short."""
    s = re.sub(r"[\r\n\t]+", " | ", str(s or "")).strip(" |")
    s = re.sub(r" {2,}", " ", s)
    if not s:
        s = "TBA"
    if len(s) > limit:
        s = s[: limit - 1].rstrip() + "…"
    return s


def fit_params(params: list[str], n: int, limit: int = MAX_PARAM) -> list[str]:
    """Fit our 4 values (event, when, where, details+link) into a template with n body parameters.
    Extra values are joined into the last one; the final value (which ends with the link) is kept whole and
    the text before it is shortened instead."""
    if n <= 0 or len(params) <= n:
        return params
    head, tail = params[: n - 1], params[n - 1:]
    last, before = tail[-1], " | ".join(tail[:-1])
    room = limit - len(last) - 3
    if room < 4:
        return head + [clean_param(last, limit)]
    if len(before) > room:
        before = before[: room - 1].rstrip() + "…"
    return head + [f"{before} | {last}"]


def detail_param(detail: str, link: str | None, limit: int = MAX_PARAM) -> str:
    """Detail text plus link; the link survives truncation, the text is shortened instead."""
    detail = clean_param(detail, 10_000)
    if not link:
        return clean_param(detail, limit)
    link = link.strip()
    room = limit - len(link) - 2
    if room < 10:
        return clean_param(link, max(limit, len(link)))
    if len(detail) > room:
        detail = detail[: room - 1].rstrip() + "…"
    return f"{detail}: {link}"


@dataclass
class SendResult:
    ok: bool
    message_id: str = ""
    error: str = ""
    code: int | None = None


class WhatsApp:
    def __init__(self, s: Settings, session: requests.Session | None = None, sleep=time.sleep):
        self.s = s
        self.http = session or requests.Session()
        self.sleep = sleep

    @property
    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self.s.wa_token}", "Content-Type": "application/json"}

    def template_payload(self, to: str, template: str, lang: str, params: list[str] | None) -> dict:
        tpl: dict = {"name": template, "language": {"code": lang}}
        if params:
            tpl["components"] = [{
                "type": "body",
                "parameters": [{"type": "text", "text": clean_param(p, 1024)} for p in params],
            }]
        return {"messaging_product": "whatsapp", "to": to, "type": "template", "template": tpl}

    def send_template(self, to: str, params: list[str] | None = None, template: str | None = None,
                      lang: str | None = None) -> SendResult:
        template = template or self.s.wa_template
        if template == "hello_world":
            lang, params = lang or "en_US", None
        elif params:
            params = fit_params(params, self.s.wa_body_params)
        payload = self.template_payload(to, template, lang or self.s.wa_template_lang, params)
        url = f"{GRAPH}/{self.s.wa_api_version}/{self.s.wa_phone_number_id}/messages"
        return self._post(url, payload)

    def _post(self, url: str, payload: dict, attempts: int = 4) -> SendResult:
        last = SendResult(False, error="not sent")
        for i in range(attempts):
            try:
                r = self.http.post(url, headers=self._headers, json=payload, timeout=30)
            except requests.RequestException as e:
                last = SendResult(False, error=f"network error: {type(e).__name__}")
                log.warning("WhatsApp %s (attempt %d)", last.error, i + 1)
                self.sleep(2 ** (i + 1))
                continue
            if r.ok:
                mid = ((r.json().get("messages") or [{}])[0]).get("id", "")
                return SendResult(True, message_id=mid)
            code, msg = _meta_error(r)
            hint = HINTS.get(code or 0, "")
            last = SendResult(False, error=f"HTTP {r.status_code} code {code}: {msg}" + (f" ({hint})" if hint else ""),
                              code=code)
            log.error("WhatsApp send failed: %s", last.error)
            if r.status_code == 429 or r.status_code >= 500:
                self.sleep(2 ** (i + 1))
                continue
            break
        return last

    def get_template(self, name: str | None = None) -> list[dict]:
        name = name or self.s.wa_template
        url = f"{GRAPH}/{self.s.wa_api_version}/{self.s.wa_waba_id}/message_templates"
        r = self.http.get(url, headers=self._headers, params={"name": name}, timeout=30)
        if not r.ok:
            code, msg = _meta_error(r)
            raise WhatsAppError(f"HTTP {r.status_code} code {code}: {msg}", code, r.status_code)
        return [t for t in r.json().get("data", []) if t.get("name") == name]


def _meta_error(r: requests.Response) -> tuple[int | None, str]:
    try:
        err = r.json().get("error", {})
        details = (err.get("error_data") or {}).get("details", "")
        return err.get("code"), " ".join(x for x in (err.get("message", ""), details) if x)
    except ValueError:
        return None, r.text[:300]


def body_param_count(template: dict) -> int:
    for c in template.get("components", []):
        if c.get("type", "").upper() == "BODY":
            return len(set(re.findall(r"\{\{(\w+)\}\}", c.get("text", ""))))
    return 0
