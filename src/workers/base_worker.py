"""Base Worker implementation providing SKIP LOCKED polling, lease management, and heartbeat."""

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
    """Abstract base worker handling SKIP LOCKED database queue consumption."""

    def __init__(
        self,
        stage: str,
        session_factory: Callable[[], Session] | None = None,
        poll_interval: float | None = None,
        lease_seconds: int | None = None,
        worker_id: str | None = None,
        heartbeat_file: str | None = None,
    ):
        self.stage = stage
        self.session_factory = session_factory or SessionLocal
        self.poll_interval = poll_interval or settings.WORKER_POLL_INTERVAL_SECONDS
        self.lease_seconds = lease_seconds or settings.SCAN_WORKER_LEASE_SECONDS
        self.worker_id = worker_id or f"{stage.lower()}-{uuid.uuid4().hex[:8]}"
        self.heartbeat_file = heartbeat_file or settings.WORKER_HEARTBEAT_FILE
        self._shutdown_requested = False
        self._current_job: JobItem | None = None

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
                # SQLite fallback for test environments without FOR UPDATE SKIP LOCKED
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

    def run_once(self) -> JobItem | None:
        """Polls for a job, claims it, and handles execution skeleton."""
        job = self.pick_job()
        if not job:
            return None

        self._current_job = job
        try:
            self.execute_job(job)
        finally:
            self._current_job = None
        return job

    def execute_job(self, job: JobItem) -> None:
        """Skeleton execution hook — extended in Commit 3."""
        logger.debug(
            "Worker %s claimed job %s (doc_id=%s); execution deferred to handler.",
            self.worker_id, job.job_id, job.document_id,
        )

    def _handle_signal(self, signum: int, frame: Any) -> None:
        """Handles termination signals gracefully."""
        sig_name = signal.Signals(signum).name
        logger.info("Received signal %s. Requesting graceful shutdown...", sig_name)
        self._shutdown_requested = True

    def run(self) -> None:
        """Main worker execution loop."""
        logger.info(
            "Starting %s worker process (worker_id=%s, poll_interval=%.1fs, lease=%ds)",
            self.stage, self.worker_id, self.poll_interval, self.lease_seconds,
        )

        # Register termination signal handlers
        try:
            signal.signal(signal.SIGINT, self._handle_signal)
            signal.signal(signal.SIGTERM, self._handle_signal)
        except Exception:
            # Signal handling on Windows threads
            pass

        while not self._shutdown_requested:
            self.touch_heartbeat()
            claimed = self.run_once()
            if not claimed:
                time.sleep(self.poll_interval)

        logger.info("Worker %s shut down cleanly.", self.worker_id)
