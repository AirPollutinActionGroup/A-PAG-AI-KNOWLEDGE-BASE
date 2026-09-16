"""Extraction Worker for Stage 4 (text extraction)."""

import logging
from collections.abc import Callable

from sqlalchemy.orm import Session

from src.core.config import settings
from src.core.errors import PermanentProcessingError, TransientProcessingError
from src.db.engine import SessionLocal
from src.modules.document_pipeline.extraction.service import ExtractionService
from src.modules.document_pipeline.extraction_job_handler import ExtractionJobHandler
from src.modules.document_pipeline.models import DocumentStatus
from src.modules.document_pipeline.repository import PostgreSQLDocumentRepository
from src.storage.bucket_manager import BucketManager
from src.workers.base_worker import BaseWorker, JobItem

logger = logging.getLogger(__name__)


class ExtractionWorker(BaseWorker):
    """Background worker dedicated to EXTRACT stage processing."""

    def __init__(
        self,
        session_factory: Callable[[], Session] | None = None,
        bucket_manager: BucketManager | None = None,
        extraction_service: ExtractionService | None = None,
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
            stage="EXTRACT",
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
        self.extraction_service = extraction_service or ExtractionService()

    def process_job(self, job: JobItem) -> None:
        """Processes a claimed EXTRACT job by delegating to ExtractionJobHandler."""
        with self.session_factory() as session:
            repo = PostgreSQLDocumentRepository(session)
            handler = ExtractionJobHandler(
                bucket_manager=self.bucket_manager,
                repository=repo,
                extraction_service=self.extraction_service,
                db_session=session,
            )

            outcome = handler.process(document_id=job.document_id)

            if outcome.failure_reason:
                # A storage blip leaves the raw object untouched, so a retry can succeed; a file
                # that cannot be parsed will fail identically every time.
                if outcome.transient:
                    raise TransientProcessingError(outcome.failure_reason)
                raise PermanentProcessingError(outcome.failure_reason)

            logger.info(
                "Extraction job processed: job_id=%s doc_id=%s final_status=%s",
                job.job_id, job.document_id, outcome.status.value,
            )

        if outcome.status not in (DocumentStatus.EXTRACTED, DocumentStatus.VALIDATED):
            logger.info(
                "Extraction job was a no-op: doc_id=%s already %s",
                job.document_id, outcome.status.value,
            )
