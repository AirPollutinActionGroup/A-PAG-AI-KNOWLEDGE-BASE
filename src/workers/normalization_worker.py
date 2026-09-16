"""Normalization Worker for Stage 5 (cleaning and quality gating)."""

import logging
from collections.abc import Callable

from sqlalchemy.orm import Session

from src.core.config import settings
from src.core.errors import PermanentProcessingError, TransientProcessingError
from src.db.engine import SessionLocal
from src.modules.document_pipeline.normalization.service import NormalizationService
from src.modules.document_pipeline.normalization_job_handler import (
    NormalizationJobHandler,
)
from src.modules.document_pipeline.repository import PostgreSQLDocumentRepository
from src.storage.bucket_manager import BucketManager
from src.workers.base_worker import BaseWorker, JobItem

logger = logging.getLogger(__name__)


class NormalizationWorker(BaseWorker):
    """Background worker dedicated to NORMALIZE stage processing."""

    def __init__(
        self,
        session_factory: Callable[[], Session] | None = None,
        bucket_manager: BucketManager | None = None,
        normalization_service: NormalizationService | None = None,
        poll_interval: float | None = None,
        lease_seconds: int | None = None,
        max_retries: int | None = None,
        backoff_base_seconds: int | None = None,
        backoff_max_seconds: int | None = None,
        reaper_interval_seconds: int | None = None,
        worker_id: str | None = None,
        heartbeat_file: str | None = None,
    ):
        super().__init__(
            stage="NORMALIZE",
            session_factory=session_factory or SessionLocal,
            poll_interval=poll_interval or settings.WORKER_POLL_INTERVAL_SECONDS,
            lease_seconds=lease_seconds or settings.SCAN_WORKER_LEASE_SECONDS,
            max_retries=max_retries or settings.MAX_JOB_RETRIES,
            backoff_base_seconds=backoff_base_seconds or settings.RETRY_BACKOFF_BASE_SECONDS,
            backoff_max_seconds=backoff_max_seconds or settings.RETRY_BACKOFF_MAX_SECONDS,
            reaper_interval_seconds=reaper_interval_seconds or settings.REAPER_INTERVAL_SECONDS,
            worker_id=worker_id,
            heartbeat_file=heartbeat_file or settings.WORKER_HEARTBEAT_FILE,
        )
        self.bucket_manager = bucket_manager or BucketManager()
        self.normalization_service = normalization_service or NormalizationService()

    def process_job(self, job: JobItem) -> None:
        """Processes a claimed NORMALIZE job by delegating to NormalizationJobHandler."""
        with self.session_factory() as session:
            repo = PostgreSQLDocumentRepository(session)
            handler = NormalizationJobHandler(
                bucket_manager=self.bucket_manager,
                repository=repo,
                normalization_service=self.normalization_service,
                db_session=session,
            )

            outcome = handler.process(document_id=job.document_id)

            if outcome.failure_reason:
                # A quality-gate rejection is permanent — the content was read correctly, it just
                # isn't usable, and a retry produces the identical verdict.
                if outcome.transient:
                    raise TransientProcessingError(outcome.failure_reason)
                raise PermanentProcessingError(outcome.failure_reason)

            logger.info(
                "Normalization job processed: job_id=%s doc_id=%s final_status=%s",
                job.job_id, job.document_id, outcome.status.value,
            )
