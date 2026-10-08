"""The Drive sync worker's schedule, and how it behaves around a run.

Nearly all of this is about *when*. The schedule is a pure function (`compute_due`), so midnight,
a missed midnight and a failed midnight are all testable by passing in a clock rather than
waiting for one. The worker is driven with an injected clock and an injected sync, against a real
SQLite `drive_sync_state`, so the state it persists between runs is what is asserted on.
"""

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from src.core.config import settings
from src.db.models import Base, DriveSyncState
from src.modules.connectors.drive.service import SyncAlreadyRunning, SyncOutcome
from src.workers.drive_sync_worker import (
    DriveSyncWorker,
    compute_due,
    next_slot_after,
    parse_at,
)

IST = ZoneInfo("Asia/Kolkata")


def ist(text: str) -> datetime:
    """'2026-10-08 09:00' in India, as an aware datetime."""
    return datetime.fromisoformat(text).replace(tzinfo=IST)


def due(now, *, last_success=None, last_attempt=None, failed=False, at="00:00",
        interval=900, retry=3600):
    return compute_due(
        now=now, last_success=last_success, last_attempt=last_attempt, last_failed=failed,
        at=at, tz=IST, interval_seconds=interval, retry_seconds=retry,
    )


# ==============================================================================
# The schedule
# ==============================================================================

@pytest.mark.parametrize("now,expected", [
    ("2026-10-08 15:30", "2026-10-09 00:00"),
    ("2026-10-08 23:59", "2026-10-09 00:00"),
    # Exactly at the slot means that slot has been taken; the next one is tomorrow.
    ("2026-10-09 00:00", "2026-10-10 00:00"),
    ("2026-10-09 00:01", "2026-10-10 00:00"),
])
def test_the_next_slot_is_the_next_midnight_in_india(now, expected):
    assert next_slot_after(ist(now), "00:00", IST) == ist(expected)


def test_midnight_in_india_is_not_midnight_in_utc():
    """The container's clock is UTC. "00:00" with no zone would fire at 05:30 in India."""
    six_pm_utc = datetime(2026, 10, 8, 18, 0, tzinfo=UTC)  # 23:30 IST
    slot = next_slot_after(six_pm_utc, "00:00", IST)
    assert slot.astimezone(UTC) == datetime(2026, 10, 8, 18, 30, tzinfo=UTC)


def test_a_fresh_deployment_syncs_straight_away():
    """Never having succeeded is not a reason to sit idle until midnight."""
    now = ist("2026-10-08 15:30")
    assert due(now) == now


def test_after_a_success_the_next_run_is_the_next_midnight():
    assert due(ist("2026-10-09 09:00"), last_success=ist("2026-10-09 00:00")) == \
        ist("2026-10-10 00:00")


def test_a_missed_midnight_is_caught_up_on_start():
    """The VM was off at midnight. The last success was the night before, so today's slot has
    passed with nothing after it -- due already, rather than skipped until tomorrow."""
    now = ist("2026-10-09 09:00")
    assert due(now, last_success=ist("2026-10-08 00:00")) <= now


def test_a_failure_retries_within_the_hour_rather_than_tomorrow():
    attempt = ist("2026-10-09 00:00")
    assert due(ist("2026-10-09 00:05"), last_success=ist("2026-10-08 00:00"),
               last_attempt=attempt, failed=True) == attempt + timedelta(hours=1)


def test_an_empty_time_falls_back_to_an_interval():
    last = ist("2026-10-09 09:00")
    assert due(ist("2026-10-09 09:01"), last_success=last, at="", interval=900) == \
        last + timedelta(seconds=900)


@pytest.mark.parametrize("bad", ["24:00", "12:60", "noon", "0000", ""])
def test_a_malformed_time_is_refused_rather_than_guessed(bad):
    with pytest.raises(ValueError, match="HH:MM"):
        parse_at(bad)


# ==============================================================================
# The worker around a run
# ==============================================================================

class Clock:
    def __init__(self, start: datetime):
        self.now = start

    def __call__(self) -> datetime:
        return self.now


@pytest.fixture
def sessions(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'worker.db'}")
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine)


def state(sessions) -> dict:
    with sessions() as s:
        return {r.key: r.value for r in s.query(DriveSyncState).all()}


def worker(sessions, clock, sync, tmp_path, enabled=True):
    return DriveSyncWorker(
        session_factory=sessions, sync=sync, clock=clock,
        heartbeat_file=str(tmp_path / "alive"), enabled=enabled,
    )


@pytest.fixture(autouse=True)
def midnight_ist(monkeypatch):
    monkeypatch.setattr(settings, "GDRIVE_SYNC_AT", "00:00")
    monkeypatch.setattr(settings, "GDRIVE_SYNC_TIMEZONE", "Asia/Kolkata")
    monkeypatch.setattr(settings, "GDRIVE_SYNC_RETRY_SECONDS", 3600)


def test_it_runs_once_and_waits_for_the_next_midnight(sessions, tmp_path):
    calls = []
    clock = Clock(ist("2026-10-08 15:30"))
    w = worker(sessions, clock, lambda: calls.append(1) or SyncOutcome(files_found=2), tmp_path)

    assert w.run_once() is True, "fresh deployment: due straight away"
    assert w.run_once() is False, "and not again until midnight"

    clock.now = ist("2026-10-08 23:59")
    assert w.run_once() is False
    clock.now = ist("2026-10-09 00:00")
    assert w.run_once() is True
    assert len(calls) == 2


def test_a_success_is_recorded_where_anyone_can_read_it(sessions, tmp_path):
    clock = Clock(ist("2026-10-08 15:30"))
    worker(sessions, clock, lambda: SyncOutcome(files_found=3), tmp_path).run_once()

    saved = state(sessions)
    assert datetime.fromisoformat(saved["last_success_at"]) == clock.now
    assert saved.get("last_error") is None
    assert '"files_found": 3' in saved["last_summary"]


def test_a_failure_is_recorded_and_retried_after_the_retry_interval(sessions, tmp_path):
    clock = Clock(ist("2026-10-09 00:00"))

    def boom():
        raise RuntimeError("Drive API 503")

    w = worker(sessions, clock, boom, tmp_path)
    assert w.run_once() is True

    saved = state(sessions)
    assert "Drive API 503" in saved["last_error"]
    assert "last_success_at" not in saved or saved["last_success_at"] is None
    assert w.due_at() == clock.now + timedelta(hours=1)


def test_a_success_after_a_failure_clears_the_error(sessions, tmp_path):
    clock = Clock(ist("2026-10-09 00:00"))
    outcomes = iter([RuntimeError("blip"), SyncOutcome()])

    def flaky():
        result = next(outcomes)
        if isinstance(result, Exception):
            raise result
        return result

    w = worker(sessions, clock, flaky, tmp_path)
    w.run_once()
    clock.now += timedelta(hours=1)
    assert w.run_once() is True
    assert state(sessions).get("last_error") is None
    assert w.due_at() == ist("2026-10-10 00:00")


def test_a_sync_already_running_by_hand_is_not_a_failure(sessions, tmp_path):
    """Somebody ran drive_sync.py at 23:59 and it is still going. Not an error to retry in an
    hour -- the worker simply tries again on its next tick."""
    def busy():
        raise SyncAlreadyRunning("another sync is running")

    w = worker(sessions, Clock(ist("2026-10-09 00:00")), busy, tmp_path)
    assert w.run_once() is False
    assert state(sessions).get("last_error") is None


def _one_loop(w):
    """Runs `run()` for exactly one iteration of its loop."""
    w._shutdown.wait = lambda _timeout: w._shutdown.set()
    w.run()


def test_a_disabled_worker_idles_healthily_and_never_syncs(sessions, tmp_path):
    """Exiting instead would put the container in a restart loop, which reads as a fault."""
    calls = []
    w = worker(sessions, Clock(ist("2026-10-08 15:30")), lambda: calls.append(1), tmp_path,
               enabled=False)
    _one_loop(w)
    assert calls == []
    assert (tmp_path / "alive").exists(), "still beating, so the healthcheck passes"


def test_an_enabled_worker_syncs_from_its_loop(sessions, tmp_path):
    calls = []
    w = worker(sessions, Clock(ist("2026-10-08 15:30")),
               lambda: calls.append(1) or SyncOutcome(), tmp_path)
    _one_loop(w)
    assert calls == [1]
    assert (tmp_path / "alive").exists()
