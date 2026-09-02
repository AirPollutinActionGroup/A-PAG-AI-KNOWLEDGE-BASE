"""Base Worker implementation providing SKIP LOCKED polling, lease management, heartbeat, retry backoff, and reaper."""

import logging
import os
import signal
import sys
import time
import uuid
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from sqlalchemy import text
from sqlalchemy.orm import Session

from src.core.config import settings
from src.core.errors import PermanentProcessingError, TransientProcessingError
from src.db.engine import SessionLocal

logger = logging.getLogger(__name__)


class JobItem:
    """Lightweight representation of a claimed Job."""

    def __init__(
        self,
        job_id: uuid.UUID | str,
        document_id: uuid.UUID | str,
        stage: str,
        status: str,
        worker_id: str,
        retry_count: int,
        lease_expires_at: datetime | None = None,
    ):
        self.job_id = job_id if isinstance(job_id, uuid.UUID) else uuid.UUID(str(job_id))
        self.document_id = document_id if isinstance(document_id, uuid.UUID) else uuid.UUID(str(document_id))
        self.stage = stage
        self.status = status
        self.worker_id = worker_id
        self.retry_count = retry_count
        self.lease_expires_at = lease_expires_at


class BaseWorker(ABC):
    """Abstract base worker handling SKIP LOCKED database queue consumption, retries, and reaper."""

    def __init__(
        self,
        stage: str,
        session_factory: Callable[[], Session] | None = None,
        poll_interval: float | None = None,
        lease_seconds: int | None = None,
        max_retries: int | None = None,
        backoff_base_seconds: int | None = None,
        backoff_max_seconds: int | None = None,
        reaper_interval_seconds: int | None = None,
        worker_id: str | None = None,
        heartbeat_file: str | None = None,
    ):
        self.stage = stage
        self.session_factory = session_factory or SessionLocal
        self.poll_interval = poll_interval or settings.WORKER_POLL_INTERVAL_SECONDS
        self.lease_seconds = lease_seconds or settings.SCAN_WORKER_LEASE_SECONDS
        self.max_retries = max_retries or settings.MAX_JOB_RETRIES
        self.backoff_base_seconds = backoff_base_seconds or settings.RETRY_BACKOFF_BASE_SECONDS
        self.backoff_max_seconds = backoff_max_seconds or settings.RETRY_BACKOFF_MAX_SECONDS
        self.reaper_interval_seconds = reaper_interval_seconds or settings.REAPER_INTERVAL_SECONDS
        self.worker_id = worker_id or f"{stage.lower()}-{uuid.uuid4().hex[:8]}"
        self.heartbeat_file = heartbeat_file or settings.WORKER_HEARTBEAT_FILE
        self._shutdown_requested = False
        self._current_job: JobItem | None = None
        self._last_reaper_run: float = 0.0

    def touch_heartbeat(self) -> None:
        """Touches the heartbeat file to signal worker liveness."""
        if not self.heartbeat_file:
            return
        try:
            path = Path(self.heartbeat_file)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.touch(exist_ok=True)
        except Exception as e:
            logger.debug("Failed to touch heartbeat file: %s", e)

    def pick_job(self) -> JobItem | None:
        """Picks up a pending job from the database using SELECT ... FOR UPDATE SKIP LOCKED.
        Immediately commits transaction so lock is released and status is RUNNING.
        """
        with self.session_factory() as session:
            bind = session.get_bind()
            dialect_name = bind.dialect.name if bind else "postgresql"

            if dialect_name == "postgresql":
                # Native PostgreSQL atomic claim with SKIP LOCKED
                claim_sql = text("""
                    UPDATE jobs
                    SET status = 'RUNNING',
                        started_at = NOW(),
                        worker_id = :worker_id,
                        lease_expires_at = NOW() + (INTERVAL '1 second' * :lease_seconds)
                    WHERE job_id = (
                        SELECT job_id
                        FROM jobs
                        WHERE stage = :stage
                          AND status = 'PENDING'
                          AND scheduled_at <= NOW()
                        ORDER BY priority DESC, scheduled_at ASC
                        FOR UPDATE SKIP LOCKED
                        LIMIT 1
                    )
                    RETURNING job_id, document_id, stage, status, worker_id, retry_count, lease_expires_at;
                """)
                result = session.execute(
                    claim_sql,
                    {
                        "worker_id": self.worker_id,
                        "lease_seconds": self.lease_seconds,
                        "stage": self.stage,
                    },
                ).mappings().first()
            else:
                # SQLite fallback for in-memory test environments
                claim_sql = text("""
                    UPDATE jobs
                    SET status = 'RUNNING',
                        started_at = CURRENT_TIMESTAMP,
                        worker_id = :worker_id,
                        lease_expires_at = datetime(CURRENT_TIMESTAMP, '+' || :lease_seconds || ' seconds')
                    WHERE job_id = (
                        SELECT job_id
                        FROM jobs
                        WHERE stage = :stage
                          AND status = 'PENDING'
                          AND scheduled_at <= CURRENT_TIMESTAMP
                        ORDER BY priority DESC, scheduled_at ASC
                        LIMIT 1
                    )
                    RETURNING job_id, document_id, stage, status, worker_id, retry_count, lease_expires_at;
                """)
                result = session.execute(
                    claim_sql,
                    {
                        "worker_id": self.worker_id,
                        "lease_seconds": self.lease_seconds,
                        "stage": self.stage,
                    },
                ).mappings().first()

            session.commit()

            if not result:
                return None

            job = JobItem(
                job_id=result["job_id"],
                document_id=result["document_id"],
                stage=result["stage"],
                status=result["status"],
                worker_id=result["worker_id"],
                retry_count=result["retry_count"],
                lease_expires_at=result.get("lease_expires_at"),
            )
            logger.info(
                "Job claimed: stage=%s job_id=%s doc_id=%s worker_id=%s",
                self.stage, job.job_id, job.document_id, self.worker_id,
            )
            return job

    def reap_stuck_jobs(self) -> int:
        """Finds jobs where status='RUNNING' and lease_expires_at < NOW() and reclaims them."""
        with self.session_factory() as session:
            bind = session.get_bind()
            dialect_name = bind.dialect.name if bind else "postgresql"

            if dialect_name == "postgresql":
                # 1. Mark jobs that exceeded max_retries as FAILED
                fail_sql = text("""
                    UPDATE jobs
                    SET status = 'FAILED',
                        worker_id = NULL,
                        lease_expires_at = NULL,
                        finished_at = NOW(),
                        error_message = 'Job failed: exceeded maximum retries via lease expiry recovery.'
                    WHERE status = 'RUNNING'
                      AND lease_expires_at < NOW()
                      AND retry_count >= :max_retries;
                """)
                session.execute(fail_sql, {"max_retries": self.max_retries})

                # 2. Reset remaining expired jobs to PENDING with retry_count incremented
                reap_sql = text("""
                    UPDATE jobs
                    SET status = 'PENDING',
                        retry_count = retry_count + 1,
                        worker_id = NULL,
                        lease_expires_at = NULL,
                        scheduled_at = NOW() + INTERVAL '5 seconds'
                    WHERE status = 'RUNNING'
                      AND lease_expires_at < NOW()
                      AND retry_count < :max_retries
                    RETURNING job_id;
                """)
                reaped = session.execute(reap_sql, {"max_retries": self.max_retries}).fetchall()
            else:
                # SQLite fallback
                fail_sql = text("""
                    UPDATE jobs
                    SET status = 'FAILED',
                        worker_id = NULL,
                        lease_expires_at = NULL,
                        finished_at = CURRENT_TIMESTAMP,
                        error_message = 'Job failed: exceeded maximum retries via lease expiry recovery.'
                    WHERE status = 'RUNNING'
                      AND lease_expires_at < CURRENT_TIMESTAMP
                      AND retry_count >= :max_retries;
                """)
                session.execute(fail_sql, {"max_retries": self.max_retries})

                reap_sql = text("""
                    UPDATE jobs
                    SET status = 'PENDING',
                        retry_count = retry_count + 1,
                        worker_id = NULL,
                        lease_expires_at = NULL,
                        scheduled_at = datetime(CURRENT_TIMESTAMP, '+5 seconds')
                    WHERE status = 'RUNNING'
                      AND lease_expires_at < CURRENT_TIMESTAMP
                      AND retry_count < :max_retries
                    RETURNING job_id;
                """)
                reaped = session.execute(reap_sql, {"max_retries": self.max_retries}).fetchall()

            session.commit()
            if reaped:
                logger.warning("Reaper recovered %d stuck job(s): %s", len(reaped), [r[0] for r in reaped])
            return len(reaped)

    def _mark_job_completed(self, job: JobItem) -> None:
        """Marks a job as COMPLETED in a separate transaction."""
        with self.session_factory() as session:
            bind = session.get_bind()
            now_expr = "NOW()" if (bind and bind.dialect.name == "postgresql") else "CURRENT_TIMESTAMP"
            sql = text(f"""
                UPDATE jobs
                SET status = 'COMPLETED',
                    finished_at = {now_expr},
                    lease_expires_at = NULL
                WHERE job_id = :job_id
            """)
            session.execute(sql, {"job_id": job.job_id})
            session.commit()
        logger.info("Job completed: stage=%s job_id=%s doc_id=%s", self.stage, job.job_id, job.document_id)

    def _mark_job_failed(self, job: JobItem, error_message: str) -> None:
        """Marks a job as FAILED permanently in a separate transaction."""
        err_msg = str(error_message)[:500]
        with self.session_factory() as session:
            bind = session.get_bind()
            now_expr = "NOW()" if (bind and bind.dialect.name == "postgresql") else "CURRENT_TIMESTAMP"
            sql = text(f"""
                UPDATE jobs
                SET status = 'FAILED',
                    finished_at = {now_expr},
                    lease_expires_at = NULL,
                    error_message = :error_message
                WHERE job_id = :job_id
            """)
            session.execute(sql, {"job_id": job.job_id, "error_message": err_msg})
            session.commit()
        logger.warning("Job marked FAILED: stage=%s job_id=%s error=%s", self.stage, job.job_id, err_msg)

    def _handle_job_retry_or_fail(self, job: JobItem, error_message: str) -> None:
        """Handles transient failure: schedules retry with backoff or fails if max retries exceeded."""
        err_msg = str(error_message)[:500]
        if job.retry_count < self.max_retries:
            backoff_sec = min(
                self.backoff_max_seconds,
                self.backoff_base_seconds * (2 ** job.retry_count),
            )
            with self.session_factory() as session:
                bind = session.get_bind()
                if bind and bind.dialect.name == "postgresql":
                    sched_expr = f"NOW() + (INTERVAL '1 second' * :backoff_sec)"
                else:
                    sched_expr = f"datetime(CURRENT_TIMESTAMP, '+' || :backoff_sec || ' seconds')"

                sql = text(f"""
                    UPDATE jobs
                    SET status = 'PENDING',
                        retry_count = retry_count + 1,
                        scheduled_at = {sched_expr},
                        worker_id = NULL,
                        lease_expires_at = NULL,
                        error_message = :error_message
                    WHERE job_id = :job_id
                """)
                session.execute(sql, {
                    "job_id": job.job_id,
                    "backoff_sec": backoff_sec,
                    "error_message": err_msg,
                })
                session.commit()
            logger.warning(
                "Job scheduled for retry (%d/%d in %ds): stage=%s job_id=%s error=%s",
                job.retry_count + 1, self.max_retries, backoff_sec, self.stage, job.job_id, err_msg,
            )
        else:
            self._mark_job_failed(job, f"Max retries ({self.max_retries}) exceeded: {err_msg}")

    def execute_job(self, job: JobItem) -> None:
        """Executes a claimed job, handling state transitions and backoff retries."""
        try:
            self.process_job(job)
            self._mark_job_completed(job)
        except TransientProcessingError as e:
            logger.warning("Job %s transient failure: %s", job.job_id, e)
            self._handle_job_retry_or_fail(job, str(e))
        except PermanentProcessingError as e:
            logger.error("Job %s permanent failure: %s", job.job_id, e)
            self._mark_job_failed(job, str(e))
        except Exception as e:
            logger.exception("Job %s unexpected failure: %s", job.job_id, e)
            self._handle_job_retry_or_fail(job, f"Unexpected error: {e}")

    @abstractmethod
    def process_job(self, job: JobItem) -> None:
        """Concrete job processing logic implemented by stage-specific workers."""

    def run_once(self) -> JobItem | None:
        """Polls for a job, claims it, and handles execution."""
        # Periodically run dead-worker reaper
        now = time.time()
        if (now - self._last_reaper_run) >= self.reaper_interval_seconds:
            try:
                self.reap_stuck_jobs()
            except Exception as e:
                logger.error("Reaper run failed: %s", e)
            finally:
                self._last_reaper_run = now

        job = self.pick_job()
        if not job:
            return None

        self._current_job = job
        try:
            self.execute_job(job)
        finally:
            self._current_job = None
        return job

    def _handle_signal(self, signum: int, frame: Any) -> None:
        """Handles termination signals gracefully."""
        try:
            sig_name = signal.Signals(signum).name
        except Exception:
            sig_name = str(signum)
        logger.info("Received signal %s. Requesting graceful shutdown...", sig_name)
        self._shutdown_requested = True

    def run(self) -> None:
        """Main worker execution loop."""
        logger.info(
            "Starting %s worker process (worker_id=%s, poll_interval=%.1fs, lease=%ds)",
            self.stage, self.worker_id, self.poll_interval, self.lease_seconds,
        )

        try:
            signal.signal(signal.SIGINT, self._handle_signal)
            signal.signal(signal.SIGTERM, self._handle_signal)
        except Exception:
            pass

        while not self._shutdown_requested:
            self.touch_heartbeat()
            claimed = self.run_once()
            if not claimed:
                time.sleep(self.poll_interval)

        logger.info("Worker %s shut down cleanly.", self.worker_id)
