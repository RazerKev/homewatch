from datetime import datetime, timedelta, timezone
from app.health import HomeStatus, evaluate

NOW = datetime(2026, 10, 15, 12, 0, tzinfo=timezone.utc)
FIVE_MIN = timedelta(minutes=5)

def test_never_checked_in_is_pending():
    status = evaluate(now=NOW, last_heartbeat_at=None, last_healthy_at=None, offline_after=FIVE_MIN)
    assert status == HomeStatus.PENDING 

def test_heartbeat_thirty_seconds_ago_is_ok():
    status = evaluate(now=NOW, last_heartbeat_at=NOW - timedelta(seconds=30), last_healthy_at=NOW - timedelta(seconds=30), offline_after=FIVE_MIN)
    assert status == HomeStatus.OK

def test_no_heartbeat_for_ten_minutes_is_offline():
    status = evaluate(now=NOW, last_heartbeat_at=NOW - timedelta(minutes=10), last_healthy_at=NOW - timedelta(minutes=10), offline_after=FIVE_MIN)
    assert status == HomeStatus.OFFLINE

def test_heartbeat_exactly_five_minutes_ago_is_ok():
    status = evaluate(now=NOW, last_heartbeat_at=NOW - timedelta(minutes=5), last_healthy_at=NOW - timedelta(minutes=5), offline_after=FIVE_MIN)
    assert status == HomeStatus.OK

def test_hub_alive_but_home_assistant_silent_is_degraded():
    status = evaluate(now=NOW, last_heartbeat_at=NOW - timedelta(seconds=30), last_healthy_at=NOW - timedelta(minutes=10), offline_after=FIVE_MIN)
    assert status == HomeStatus.DEGRADED

def test_old_heartbeat_right_after_watchdog_starts_is_ok():
    eight_hours_ago = NOW - timedelta(hours=8)
    status = evaluate(
        now=NOW,
        last_heartbeat_at=eight_hours_ago,
        last_healthy_at=eight_hours_ago,
        offline_after=FIVE_MIN,
        watching_since=NOW - timedelta(seconds=5), # the watchdog started 5 seconds ago
    )
    assert status == HomeStatus.OK

def test_still_silent_six_minutes_after_watchdog_starts_is_offline():
    eight_hours_ago = NOW - timedelta(hours=8)
    status = evaluate(
        now=NOW,
        last_heartbeat_at=eight_hours_ago,
        last_healthy_at=eight_hours_ago,
        offline_after=FIVE_MIN,
        watching_since=NOW - timedelta(minutes=6),
    )
    assert status == HomeStatus.OFFLINE

def test_real_degraded_still_shows_once_watchdog_has_been_running():
    status = evaluate(
        now=NOW,
        last_heartbeat_at=NOW - timedelta(seconds=30),
        last_healthy_at=NOW - timedelta(minutes=10),
        offline_after=FIVE_MIN,
        watching_since=NOW - timedelta(hours=1),
    )
    assert status == HomeStatus.DEGRADED