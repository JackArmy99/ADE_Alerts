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
