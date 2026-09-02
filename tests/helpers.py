"""Test helper utilities for driving asynchronous background workers in integration/unit tests."""

import time
import uuid
from typing import Callable

from sqlalchemy.orm import Session

from src.db.models import Job as JobORM
from src.modules.document_pipeline.models import (
    DocumentStatus,
    UploadRequest,
    UploadResponse,
)
from src.modules.document_pipeline.scan_job_handler import ScanJobHandler
from src.modules.document_pipeline.upload_service import UploadService
from src.storage.bucket_manager import BucketManager
from src.workers.scan_worker import ScanWorker


def run_scan_worker_for_document(
    session_factory: Callable[[], Session],
    document_id: uuid.UUID,
    bucket_manager: BucketManager | None = None,
    max_iterations: int = 10,
) -> JobORM:
    """Drives the SCAN worker until the specific document's job reaches a terminal state (COMPLETED or FAILED).
    Deterministic: only advances until the target document job is resolved.
    """
    worker = ScanWorker(
        session_factory=session_factory,
        bucket_manager=bucket_manager,
    )

    for _ in range(max_iterations):
        with session_factory() as session:
            job = session.query(JobORM).filter(JobORM.document_id == document_id, JobORM.stage == "SCAN").first()
            if job and job.status in ("COMPLETED", "FAILED"):
                return job

        # Execute one poll/claim cycle
        worker.run_once()

    with session_factory() as session:
        job = session.query(JobORM).filter(JobORM.document_id == document_id, JobORM.stage == "SCAN").first()
        if not job or job.status not in ("COMPLETED", "FAILED"):
            raise TimeoutError(f"Worker did not resolve job for document {document_id} within {max_iterations} iterations.")
        return job


def upload_and_process_sync(
    upload_service: UploadService,
    scan_handler: ScanJobHandler,
    filename: str,
    data: bytes,
    request_meta: UploadRequest | None = None,
) -> UploadResponse:
    """Synchronous helper for in-memory and unit tests: executes receive() and then immediately
    drives ScanJobHandler.process() to return the terminal UploadResponse.
    """
    meta = request_meta or UploadRequest()
    initial_resp = upload_service.receive(filename=filename, data=data, request_meta=meta)
    terminal_resp = scan_handler.process(
        document_id=initial_resp.document_id,
        correlation_id=initial_resp.correlation_id,
        request_meta=meta,
    )
    return terminal_resp
