"""Scan Worker for Stage 2 (Validation/Threat Scan) & Stage 3 (Promote/Reject)."""

import logging
from typing import Callable

from sqlalchemy.orm import Session

from src.core.config import settings
from src.core.errors import PermanentProcessingError, TransientProcessingError
from src.db.engine import SessionLocal
from src.modules.document_pipeline.models import DocumentStatus
from src.modules.document_pipeline.repository import PostgreSQLDocumentRepository
from src.modules.document_pipeline.scan_job_handler import ScanJobHandler
from src.modules.document_pipeline.validation import ValidationService
from src.storage.bucket_manager import BucketManager
from src.workers.base_worker import BaseWorker, JobItem

logger = logging.getLogger(__name__)


class ScanWorker(BaseWorker):
    """Background worker dedicated to SCAN stage processing."""

    def __init__(
        self,
        session_factory: Callable[[], Session] | None = None,
        bucket_manager: BucketManager | None = None,
        validation_service: ValidationService | None = None,
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
            stage="SCAN",
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
        self.validation_service = validation_service or ValidationService()

    def process_job(self, job: JobItem) -> None:
        """Processes a claimed SCAN job by delegating to ScanJobHandler."""
        with self.session_factory() as session:
            repo = PostgreSQLDocumentRepository(session)
            handler = ScanJobHandler(
                bucket_manager=self.bucket_manager,
                repository=repo,
                validation_service=self.validation_service,
                db_session=session,
            )

            response = handler.process(document_id=job.document_id)

            if response.status == DocumentStatus.VALIDATION_FAILED:
                if response.rejection_reason and "DOCUMENT_NOT_FOUND" in response.rejection_reason:
                    raise PermanentProcessingError(response.rejection_reason)
                if response.rejection_reason and "STORAGE_ERROR" in response.rejection_reason:
                    raise PermanentProcessingError(response.rejection_reason)
                raise PermanentProcessingError(response.rejection_reason or "Validation failed")

            logger.info(
                "Scan job processed successfully: job_id=%s doc_id=%s final_status=%s",
                job.job_id, job.document_id, response.status.value,
            )
