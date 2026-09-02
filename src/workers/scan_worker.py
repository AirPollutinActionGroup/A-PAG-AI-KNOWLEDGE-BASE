"""Scan Worker for Stage 2 (Validation/Threat Scan) & Stage 3 (Promote/Reject)."""

import logging
from typing import Callable

from sqlalchemy.orm import Session

from src.core.config import settings
from src.workers.base_worker import BaseWorker, JobItem

logger = logging.getLogger(__name__)


class ScanWorker(BaseWorker):
    """Background worker dedicated to SCAN stage processing."""

    def __init__(
        self,
        session_factory: Callable[[], Session] | None = None,
        poll_interval: float | None = None,
        lease_seconds: int | None = None,
        worker_id: str | None = None,
        heartbeat_file: str | None = None,
    ):
        super().__init__(
            stage="SCAN",
            session_factory=session_factory,
            poll_interval=poll_interval or settings.WORKER_POLL_INTERVAL_SECONDS,
            lease_seconds=lease_seconds or settings.SCAN_WORKER_LEASE_SECONDS,
            worker_id=worker_id,
            heartbeat_file=heartbeat_file or settings.WORKER_HEARTBEAT_FILE,
        )
