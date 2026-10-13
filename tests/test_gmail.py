from ade_radar.sources.gmail import parse_message

from .conftest import FIXTURES


def test_parse_ra_mail(tmp_path):
    raw = (FIXTURES / "gmail" / "ra_just_announced.eml").read_bytes()
    item = parse_message(raw, "7:42", tmp_path)
    assert item.source == "gmail" and item.external_id == "7:42"
    assert item.author == "Resident Advisor <noreply@ra.co>"
    assert item.text.startswith("Subject: Just announced: Josh Baker presents You&Me - ADE")
    assert "Gashouder" in item.text
    assert item.links == ["https://ra.co/events/2222222"]  # unsubscribe and pixels dropped
    assert item.screenshot_paths == []


def test_security_mails_never_reach_claude():
    from ade_radar.models import Item
    from ade_radar.sources.gmail import is_security_mail
    assert is_security_mail(Item("gmail", "1", author="Google <no-reply@accounts.google.com>",
                                 raw={"subject": "Security alert"}))
    assert is_security_mail(Item("gmail", "2", author="Instagram <security@mail.instagram.com>",
                                 raw={"subject": "123456 is your Instagram code"}))
    assert is_security_mail(Item("gmail", "3", author="DICE <hello@dice.fm>", raw={"subject": "Reset your password"}))
    assert not is_security_mail(Item("gmail", "4", author="Resident Advisor <noreply@ra.co>",
                                     raw={"subject": "Just announced: Josh Baker presents You&Me - ADE"}))
