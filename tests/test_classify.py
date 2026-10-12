import json
from types import SimpleNamespace

import anthropic
import httpx2 as httpx

from ade_radar.classify import Classifier, build_content, extract_json
from ade_radar.models import Item

GOOD = {"is_ade_related": True, "is_event_announcement": True, "event_name": "Social Pleasure ADE pop-up",
        "artists": ["Paige Tomlinson"], "date": "2026-10-22", "time": "23:00", "venue": None,
        "ticket_status": "presale_signup", "links": [], "urgency": "urgent", "relevant_for": ["jack"],
        "reason": "presale sign-up"}


class FakeMessages:
    def __init__(self, replies):
        self.replies, self.calls = list(replies), []

    def create(self, **kw):
        self.calls.append(kw)
        r = self.replies.pop(0)
        if isinstance(r, Exception):
            raise r
        return SimpleNamespace(
            content=[SimpleNamespace(type="text", text=r)], stop_reason="end_turn",
            usage=SimpleNamespace(input_tokens=100, output_tokens=50, cache_read_input_tokens=80,
                                  cache_creation_input_tokens=0))


def make(ctx, replies, cap=60):
    msgs = FakeMessages(replies)
    return Classifier(ctx, max_calls=cap, client=SimpleNamespace(messages=msgs)), msgs


def test_classify_parses_and_caches_system(ctx):
    c, msgs = make(ctx, [json.dumps(GOOD)])
    cls = c.classify(Item("instagram", "p", author="socialpleasure", text="pop-up"), "Mon 12 Oct 2026")
    assert cls.urgency == "urgent" and cls.event_name == "Social Pleasure ADE pop-up" and cls.venue is None
    kw = msgs.calls[0]
    assert kw["model"] == "claude-haiku-5-5"
    assert kw["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert "Paige Tomlinson" in kw["system"][0]["text"]  # taste.yaml is in the cached prompt
    assert kw["output_config"]["format"]["type"] == "json_schema"
    assert c.usage.calls == 1 and c.usage.cache_read == 80


def test_cap_stops_calls(ctx):
    c, msgs = make(ctx, [json.dumps(GOOD)], cap=1)
    c.classify(Item("instagram", "a"), "today")
    assert c.classify(Item("instagram", "b"), "today") is None
    assert c.usage.skipped_cap == 1 and len(msgs.calls) == 1


def test_schema_rejected_falls_back(ctx):
    req = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    err = anthropic.BadRequestError("bad schema", response=httpx.Response(400, request=req), body=None)
    c, msgs = make(ctx, [err, "Here you go: " + json.dumps(GOOD)])
    cls = c.classify(Item("instagram", "p"), "today")
    assert cls is not None and cls.ticket_status == "presale_signup"
    assert "format" not in msgs.calls[1]["output_config"]


def test_build_content_includes_image(tmp_path):
    img = tmp_path / "x.jpg"
    img.write_bytes(b"\xff\xd8\xff" + b"0" * 100)
    content = build_content(Item("instagram", "p", author="a", text="hi", screenshot_paths=[str(img)]), "today")
    assert content[0]["type"] == "image" and content[0]["source"]["media_type"] == "image/jpeg"
    assert "Author: @a" in content[-1]["text"]


def test_extract_json():
    assert extract_json('noise {"a": 1} tail') == {"a": 1}
    assert extract_json("nothing") is None
