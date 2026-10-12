from ade_radar import db
from ade_radar.dedupe import upsert_event
from ade_radar.models import Classification
from ade_radar.reports import heartbeat_summary


def test_dedupe_same_night_and_venue(conn):
    a = Classification(event_name="Josh Baker Gashouder", date="2026-10-25", time="12:00", venue="Gashouder",
                       artists=["Josh Baker"])
    b = Classification(event_name="You&Me ADE", date="2026-10-25", venue="Gashouder Westergas")
    c = Classification(event_name="Late one", date="2026-10-26", time="02:00", venue="Gashouder")
    d = Classification(event_name="Other", date="2026-10-24", venue="Pllek")
    e1, new1 = upsert_event(conn, a, "i1")
    e2, new2 = upsert_event(conn, b, "i2")
    e3, _ = upsert_event(conn, c, "i3")   # 02:00 on the 26th = night of the 25th
    e4, _ = upsert_event(conn, d, "i4")
    assert new1 and not new2 and e1 == e2 == e3 and e4 != e1


def test_events_without_identity_never_merge(conn):
    e1, _ = upsert_event(conn, Classification(), "i1")
    e2, _ = upsert_event(conn, Classification(), "i2")
    assert e1 != e2


def test_heartbeat_summary(conn):
    r = db.start_run(conn, "instagram")
    db.finish_run(conn, r, True, {"posts_checked": 12, "claude_calls": 5})
    r = db.start_run(conn, "gmail")
    db.finish_run(conn, r, False, {}, "boom")
    text, healthy = heartbeat_summary(conn)
    assert "instagram 1/1 ok" in text and "gmail 0/1 ok" in text and "Claude calls 5" in text
    assert not healthy
