from pathlib import Path

import pytest

from ade_radar import db
from ade_radar.config import Context, Settings, load_accounts, load_taste
from ade_radar.notify.whatsapp import SendResult

REPO = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def ctx(tmp_path):
    s = Settings(root=tmp_path, recipients={"jack": "31600000001", "friend1": "31600000002"},
                 wa_token="test-token-123456", wa_phone_number_id="111", wa_waba_id="222", ntfy_topic="")
    taste = load_taste(REPO)
    # Routing tests need one person with Jack's taste and one with none, whatever config/taste.yaml says now.
    empty = {k: [] for k in ("must_see", "like", "brands_like", "genres_like", "genres_avoid", "venues_like")}
    taste.people = {"jack": taste.people["jack"], "friend1": empty}
    return Context(settings=s, taste=taste, accounts=load_accounts(REPO))


@pytest.fixture
def conn(ctx):
    c = db.connect(ctx.settings.db_path)
    yield c
    c.close()


class FakeWA:
    def __init__(self, fail_codes=None):
        self.sent = []
        self.fail = fail_codes or []

    def send_template(self, to, params=None, template=None, lang=None):
        if self.fail:
            code = self.fail.pop(0)
            return SendResult(False, error=f"code {code}", code=code)
        self.sent.append((to, params))
        return SendResult(True, message_id=f"wamid.{len(self.sent)}")


@pytest.fixture
def fake_wa():
    return FakeWA()
