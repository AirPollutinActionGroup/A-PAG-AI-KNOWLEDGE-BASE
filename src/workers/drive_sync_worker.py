"""Runs the Drive sync on a schedule: daily at a wall-clock time, by default midnight in India.

**Not a `BaseWorker`.** Every other worker here claims rows from the `jobs` table, and all of
`BaseWorker` is that machinery -- SKIP LOCKED claims, leases, a reaper. Nothing enqueues a Drive
sync; it happens because the clock says so. This is the first timer-driven process in the
codebase, so it borrows only the parts of `BaseWorker` that still apply: the heartbeat file the
container healthcheck watches, and shutting down cleanly on SIGTERM.

**A time of day, not an interval.** "Every 24 hours" is anchored to whenever the container last
started, so one restart at 4pm would move the nightly sync to 4pm permanently. The schedule is
`GDRIVE_SYNC_AT` in `GDRIVE_SYNC_TIMEZONE`, explicitly, because the container's clock is UTC and
"00:00" with no zone would fire at 05:30 in India.

**A missed run is caught up, not skipped.** When the last success is stored rather than held in
memory, "the VM was off at midnight" becomes visible: on start, if the most recent scheduled slot
passed with no successful run after it, the sync runs straight away. The state lives in
`drive_sync_state`, so it survives restarts and can be read by anyone with database access.

**A failure retries within the hour**, not at the next midnight -- a network blip should not cost
a day. A configuration fault fails the same way each hour, which is one cheap logged attempt.
"""

import json
import logging
import signal
import threading
from collections.abc import Callable
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy.orm import Session

from src.core.config import settings
from src.db.engine import SessionLocal
from src.db.models import DriveSyncState
from src.modules.connectors.drive.service import SyncAlreadyRunning, SyncOutcome

logger = logging.getLogger(__name__)

# How often the loop wakes. Comfortably inside the 30s heartbeat window the compose healthcheck
# enforces, and cheap: each wake is a clock comparison and, when due, one small database read.
TICK_SECONDS = 15.0


def parse_at(value: str) -> tuple[int, int]:
    """Parses an `HH:MM` time into (hour, minute). Refuses anything else rather than guess."""
    try:
        hour_s, minute_s = value.strip().split(":")
        hour, minute = int(hour_s), int(minute_s)
    except ValueError as e:
        raise ValueError(f"GDRIVE_SYNC_AT must be HH:MM, got {value!r}") from e
    if not (0 <= hour < 24 and 0 <= minute < 60):
        raise ValueError(f"GDRIVE_SYNC_AT must be HH:MM, got {value!r}")
    return hour, minute


def next_slot_after(moment: datetime, at: str, tz: ZoneInfo) -> datetime:
    """The first scheduled time strictly after `moment`.

    Arithmetic is done on the local wall clock, so in a zone with daylight saving "00:00" stays
    midnight across the change rather than drifting by an hour.
    """
    hour, minute = parse_at(at)
    local = moment.astimezone(tz)
    slot = local.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if slot <= local:
        slot += timedelta(days=1)
    return slot


def compute_due(
    *,
    now: datetime,
    last_success: datetime | None,
    last_attempt: datetime | None,
    last_failed: bool,
    at: str,
    tz: ZoneInfo,
    interval_seconds: int,
    retry_seconds: int,
) -> datetime:
    """When the next sync should run. Pure, so the schedule can be tested without waiting.

    - Never succeeded: now. A fresh deployment should not sit idle until midnight.
    - Last attempt failed: retry after `retry_seconds`.
    - Otherwise the first slot after the last success -- which, if the machine was off at that
      slot, is already in the past, and so is due now. That is the whole catch-up rule.
    """
    if last_failed and last_attempt is not None:
        return last_attempt + timedelta(seconds=retry_seconds)
    if last_success is None:
        return now
    if not at:
        return last_success + timedelta(seconds=interval_seconds)
    return next_slot_after(last_success, at, tz)


class _StateStore:
    """`drive_sync_state`, as a few named values."""

    def __init__(self, session_factory: Callable[[], Session]):
        self._sessions = session_factory

    def read(self) -> dict[str, str | None]:
        with self._sessions() as s:
            return {row.key: row.value for row in s.query(DriveSyncState).all()}

    def write(self, **values: str | None) -> None:
        with self._sessions() as s:
            for key, value in values.items():
                s.merge(DriveSyncState(key=key, value=value))
            s.commit()


def _parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _default_sync() -> SyncOutcome:
    """Builds everything from configuration and runs one pass. Imported lazily so that a worker
    with the connector switched off never loads credentials or opens an HTTP client."""
    from src.modules.connectors.drive.client import DriveClient
    from src.modules.connectors.drive.credentials import DriveCredentials
    from src.modules.connectors.drive.service import DriveSyncService

    credentials = DriveCredentials(settings.GDRIVE_CREDENTIALS_FILE)
    with DriveClient(credentials) as client:
        return DriveSyncService(
            client=client,
            session_factory=SessionLocal,
            public_folder_id=settings.GDRIVE_PUBLIC_FOLDER_ID,
            restricted_folder_id=settings.GDRIVE_RESTRICTED_FOLDER_ID,
            fallback_owner_email=settings.GDRIVE_FALLBACK_OWNER_EMAIL,
            max_bytes=settings.GDRIVE_MAX_FILE_BYTES,
        ).sync_once()


def summarise(outcome: SyncOutcome) -> dict[str, Any]:
    """Counts, not names: this is written to the database and the log on every run, and file
    names belong in the audit trail rather than in a status blob."""
    data = asdict(outcome)
    return {k: (len(v) if isinstance(v, list) else v) for k, v in data.items()}


class DriveSyncWorker:
    def __init__(
        self,
        session_factory: Callable[[], Session] | None = None,
        sync: Callable[[], SyncOutcome] | None = None,
        clock: Callable[[], datetime] | None = None,
        heartbeat_file: str | None = None,
        enabled: bool | None = None,
    ):
        self._state = _StateStore(session_factory or SessionLocal)
        self._sync = sync or _default_sync
        self._clock = clock or (lambda: datetime.now(UTC))
        self._heartbeat = heartbeat_file or settings.WORKER_HEARTBEAT_FILE
        self._enabled = settings.GDRIVE_ENABLED if enabled is None else enabled
        self._tz = ZoneInfo(settings.GDRIVE_SYNC_TIMEZONE)
        self._at = (settings.GDRIVE_SYNC_AT or "").strip()
        if self._at:
            parse_at(self._at)  # fail at boot on a malformed schedule, not at the first slot
        self._shutdown = threading.Event()

    # ------------------------------------------------------------------ liveness

    def touch_heartbeat(self) -> None:
        if not self._heartbeat:
            return
        try:
            path = Path(self._heartbeat)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.touch(exist_ok=True)
        except OSError as e:
            logger.debug("Heartbeat touch failed: %s", e)

    def _beat_while(self, done: threading.Event) -> None:
        """Keeps the heartbeat fresh during a sync, which can outlast the healthcheck window.
        The same reasoning as BaseWorker's lease renewer: a long job is not a dead worker."""
        while not done.wait(TICK_SECONDS):
            self.touch_heartbeat()

    # ------------------------------------------------------------------ schedule

    def due_at(self) -> datetime:
        state = self._state.read()
        last_success = _parse_time(state.get("last_success_at"))
        last_attempt = _parse_time(state.get("last_attempt_at"))
        return compute_due(
            now=self._clock(),
            last_success=last_success,
            last_attempt=last_attempt,
            last_failed=bool(state.get("last_error")) and (
                last_success is None or (last_attempt is not None and last_attempt > last_success)
            ),
            at=self._at,
            tz=self._tz,
            interval_seconds=settings.GDRIVE_SYNC_INTERVAL_SECONDS,
            retry_seconds=settings.GDRIVE_SYNC_RETRY_SECONDS,
        )

    def run_once(self) -> bool:
        """Runs a sync if one is due. Returns whether it ran."""
        due = self.due_at()
        if self._clock() < due:
            return False

        started = self._clock()
        logger.info("Drive sync starting (due %s)", due.astimezone(self._tz).isoformat())
        done = threading.Event()
        beater = threading.Thread(target=self._beat_while, args=(done,), daemon=True)
        beater.start()
        try:
            outcome = self._sync()
        except SyncAlreadyRunning as e:
            # Someone is running it by hand. Not a failure, and not a reason to wait an hour.
            logger.info("%s", e)
            return False
        except Exception as e:
            logger.exception("Drive sync failed; retrying in %ss",
                             settings.GDRIVE_SYNC_RETRY_SECONDS)
            self._state.write(
                last_attempt_at=started.isoformat(),
                last_error=f"{type(e).__name__}: {e}"[:1000],
            )
            return True
        finally:
            done.set()
            beater.join(timeout=5)

        summary = summarise(outcome)
        self._state.write(
            last_attempt_at=started.isoformat(),
            last_success_at=started.isoformat(),
            last_error=None,
            last_summary=json.dumps(summary),
        )
        logger.info("Drive sync finished: %s", summary)
        nxt = self.due_at()
        logger.info("Next Drive sync at %s", nxt.astimezone(self._tz).isoformat())
        return True

    # ---------------------------------------------------------------------- loop

    def _handle_signal(self, signum: int, _frame: Any) -> None:
        logger.info("Received signal %s; stopping after the current step.", signum)
        self._shutdown.set()

    def run(self) -> None:
        try:
            signal.signal(signal.SIGINT, self._handle_signal)
            signal.signal(signal.SIGTERM, self._handle_signal)
        except (ValueError, OSError, RuntimeError) as e:
            logger.debug("Signal handlers not installed (not on main thread?): %s", e)

        if not self._enabled:
            # Idle rather than exit. Exiting would put the container into a restart loop, which
            # reads as a fault; a connector that is simply off should look healthy and quiet.
            logger.info("GDRIVE_ENABLED is false; the Drive sync worker is idle.")
        else:
            schedule = (f"daily at {self._at} {self._tz.key}" if self._at
                        else f"every {settings.GDRIVE_SYNC_INTERVAL_SECONDS}s")
            logger.info("Drive sync worker started, %s.", schedule)

        while not self._shutdown.is_set():
            self.touch_heartbeat()
            if self._enabled:
                try:
                    self.run_once()
                except Exception:
                    # Reading the schedule itself failed -- the database is unreachable, say.
                    # Keep the loop alive; the next tick tries again.
                    logger.exception("Drive sync scheduler error")
            self._shutdown.wait(TICK_SECONDS)

        logger.info("Drive sync worker stopped.")
