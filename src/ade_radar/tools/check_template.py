"""check-template and test-whatsapp."""

from __future__ import annotations

import json

from ..config import Context
from ..notify.whatsapp import WhatsApp, WhatsAppError, body_param_count

SAMPLE = ["Social Pleasure ADE pop-up (Paige Tomlinson)", "Thu 22 Oct 23:00", "A'DAM Toren",
          "TEST: presale sign-up open: https://www.instagram.com/socialpleasure/"]


def check_template(ctx: Context, name: str | None = None) -> dict | None:
    s = ctx.settings
    wa = WhatsApp(s)
    try:
        found = wa.get_template(name or (s.wa_template if s.wa_template != "hello_world" else "ade_alert"))
    except WhatsAppError as e:
        print(f"Meta API error: {e}")
        return None
    if not found:
        print(f"Template {name or 'ade_alert'} not found on this WhatsApp Business Account.")
        return None
    for t in found:
        print(f"name={t.get('name')}  status={t.get('status')}  language={t.get('language')}  "
              f"category={t.get('category')}  body params={body_param_count(t)}")
        print(json.dumps(t.get("components", []), indent=2, ensure_ascii=False))
    t0 = found[0]
    n = body_param_count(t0)
    if n != s.wa_body_params:
        print(f"!! Body has {n} parameters: set WHATSAPP_TEMPLATE_PARAMS={n} in .env"
              + (" (details + link then go on the last line)" if n == 3 else ""))
    if str(t0.get("category", "")).upper() == "MARKETING":
        print("!! Category is MARKETING: Meta may hold back some marketing messages per person (error 131049)"
              " and people can mute them. A UTILITY template is more reliable for alerts.")
    if t0.get("language") != s.wa_template_lang:
        print(f"!! Set WHATSAPP_TEMPLATE_LANG={t0.get('language')} in .env (currently {s.wa_template_lang})")
    active = [t for t in found if t.get("status") == "APPROVED"]
    if active:
        t = active[0]
        if s.wa_template != t.get("name"):
            print(f"!! Template is approved: set WHATSAPP_TEMPLATE={t.get('name')} in .env")
        return t
    print("Not approved yet: keep WHATSAPP_TEMPLATE=hello_world for now (alerts are mirrored to ntfy).")
    return None


def test_whatsapp(ctx: Context, only: str | None = None) -> None:
    s = ctx.settings
    wa = WhatsApp(s)
    targets = {k: v for k, v in s.recipients.items() if not only or k == only.lower()}
    if not targets:
        print(f"No recipients matched ({only!r}). Check WHATSAPP_RECIPIENTS / RECIPIENT_NAMES in .env.")
        return
    try:
        approved = [t for t in wa.get_template("ade_alert") if t.get("status") == "APPROVED"]
    except WhatsAppError as e:
        print(f"could not read template status: {e}")
        approved = []
    for name, phone in targets.items():
        r = wa.send_template(phone, template="hello_world", lang="en_US")
        print(f"hello_world -> {name}: {'OK ' + r.message_id if r.ok else 'FAILED ' + r.error}")
        if approved:
            t = approved[0]
            r = wa.send_template(phone, SAMPLE, template="ade_alert", lang=t.get("language"))
            print(f"ade_alert   -> {name}: {'OK ' + r.message_id if r.ok else 'FAILED ' + r.error}")
