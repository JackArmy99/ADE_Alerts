from ade_radar.notify.whatsapp import WhatsApp, body_param_count, clean_param, detail_param


def test_clean_param_rules():
    assert clean_param("a\nb\tc") == "a | b | c"
    assert "     " not in clean_param("a      b")
    assert clean_param("") == "TBA"
    assert len(clean_param("x" * 400)) == 150


def test_detail_param_keeps_link():
    link = "https://dice.fm/event/abc123-social-pleasure-ade"
    p = detail_param("URGENT: presale sign-up open " + "blah " * 50, link)
    assert p.endswith(link) and len(p) <= 150 and p.startswith("URGENT")


class Resp:
    def __init__(self, status, body):
        self.status_code, self._body = status, body
        self.ok = 200 <= status < 300
        self.text = str(body)

    def json(self):
        return self._body


class Session:
    def __init__(self, responses):
        self.responses, self.calls = list(responses), []

    def post(self, url, headers=None, json=None, timeout=None):
        self.calls.append((url, headers, json))
        return self.responses.pop(0)


def test_send_template_payload_and_retry(ctx):
    sess = Session([Resp(500, {"error": {"code": 2, "message": "boom"}}),
                    Resp(200, {"messages": [{"id": "wamid.X"}]})])
    wa = WhatsApp(ctx.settings, session=sess, sleep=lambda s: None)
    r = wa.send_template("31600000001", ["A", "B", "C", "D"])
    assert r.ok and r.message_id == "wamid.X" and len(sess.calls) == 2
    url, headers, payload = sess.calls[-1]
    assert url == "https://graph.facebook.com/v25.0/111/messages"
    assert headers["Authorization"] == "Bearer test-token-123456"
    assert payload["type"] == "template" and payload["template"]["name"] == "ade_alert"
    assert payload["template"]["language"]["code"] == "en"
    assert [p["text"] for p in payload["template"]["components"][0]["parameters"]] == ["A", "B", "C", "D"]


def test_hello_world_has_no_params_and_en_us(ctx):
    sess = Session([Resp(200, {"messages": [{"id": "w"}]})])
    WhatsApp(ctx.settings, session=sess).send_template("1", ["x"], template="hello_world")
    tpl = sess.calls[0][2]["template"]
    assert tpl["language"]["code"] == "en_US" and "components" not in tpl


def test_not_on_allow_list_no_retry(ctx):
    sess = Session([Resp(400, {"error": {"code": 131030, "message": "Recipient not in allowed list"}})])
    r = WhatsApp(ctx.settings, session=sess, sleep=lambda s: None).send_template("1", ["a", "b", "c", "d"])
    assert not r.ok and r.code == 131030 and "allow list" in r.error and len(sess.calls) == 1


def test_body_param_count():
    t = {"components": [{"type": "HEADER", "text": "Up the scouse"},
                        {"type": "BODY", "text": "Event: {{1}} When: {{2}} Where: {{3}} Details: {{4}}"}]}
    assert body_param_count(t) == 4
