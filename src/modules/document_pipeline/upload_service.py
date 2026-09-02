"""Upload Service implementing Stage 1: Upload & Quarantine Landing.
2-bucket architecture: apag-quarantine -> apag-raw.
"""

import logging
import uuid
from typing import Any

from sqlalchemy.orm import Session

from src.db.enums import AuditEventType
from src.db.models import Job as JobORM
from src.modules.audit.service import AuditService
from src.modules.document_pipeline.models import (
    Document,
    DocumentStatus,
    UploadRequest,
    UploadResponse,
)
from src.modules.document_pipeline.repository import (
    DocumentRepository,
    InMemoryDocumentRepository,
)
from src.storage.bucket_manager import BucketManager

logger = logging.getLogger(__name__)

# Fast-path constraints
MAX_FILE_SIZE_BYTES = 100 * 1024 * 1024  # 100 MB


class UploadService:
    """Fast-path asynchronous ingestion receiver."""

    def __init__(
        self,
        bucket_manager: BucketManager | None = None,
        repository: DocumentRepository | None = None,
        db_session: Session | None = None,
    ):
        self.buckets = bucket_manager or BucketManager()
        self.repo = repository or InMemoryDocumentRepository()
        self._db = db_session

    def _audit(
        self,
        document_id: uuid.UUID,
        event_type: AuditEventType,
        details: dict[str, Any] | None = None,
        correlation_id: uuid.UUID | None = None,
    ) -> None:
        """Best-effort audit log write. Logs error on failure, never blocks pipeline."""
        if self._db is None:
            logger.debug(
                "Audit skipped (no db session): doc_id=%s event=%s",
                document_id, event_type.value,
            )
            return
        try:
            AuditService.log_event(
                db=self._db,
                document_id=document_id,
                event_type=event_type,
                details=details,
                correlation_id=correlation_id,
            )
        except Exception:
            logger.error(
                "AUDIT WRITE FAILED: doc_id=%s event=%s — compliance gap, investigate immediately",
                document_id, event_type.value, exc_info=True,
            )

    def receive(
        self,
        filename: str,
        data: bytes,
        request_meta: UploadRequest | None = None,
        correlation_id: uuid.UUID | None = None,
    ) -> UploadResponse:
        """Stage 1: Fast API path — writes PDF to quarantine, creates DB record + job, and returns 202."""
        meta = request_meta or UploadRequest()
        document_id = uuid.uuid4()
        corr_id = correlation_id or uuid.uuid4()
        quarantine_key = f"{document_id}.pdf"

        logger.info(
            "Upload received: corr_id=%s doc_id=%s filename=%s size=%d bytes",
            corr_id, document_id, filename, len(data),
        )

        # 1. Immediate Quarantine Landing
        self.buckets.storage.put_object(
            bucket_name=self.buckets.quarantine,
            object_name=quarantine_key,
            data=data,
            content_type="application/pdf",
        )
        logger.debug("Quarantined: corr_id=%s doc_id=%s key=%s", corr_id, document_id, quarantine_key)

        # 2. Register Document entity in repository
        doc = Document(
            id=document_id,
            filename=filename,
            owner_id=meta.owner_id,
            size=len(data),
            status=DocumentStatus.QUARANTINED,
            classification=meta.classification,
            version=1,
            quarantine_path=f"{self.buckets.quarantine}/{quarantine_key}",
        )
        self.repo.create(doc)

        # 3. Enqueue Job in jobs queue table if database session is active
        if self._db is not None:
            job = JobORM(
                job_id=uuid.uuid4(),
                document_id=document_id,
                stage="SCAN",
                status="PENDING",
            )
            self._db.add(job)
            self._db.commit()

        # 4. AUDIT: Document received and quarantined
        self._audit(
            document_id,
            AuditEventType.DOCUMENT_QUARANTINED,
            details={
                "filename": filename,
                "size_bytes": len(data),
                "classification": meta.classification.value if meta.classification else None,
                "quarantine_key": quarantine_key,
            },
            correlation_id=corr_id,
        )

        return UploadResponse(
            document_id=document_id,
            filename=filename,
            status=DocumentStatus.QUARANTINED,
            quarantine_key=quarantine_key,
            checksum=None,
            was_duplicate=False,
            status_url=f"/api/v1/documents/{document_id}/status",
            correlation_id=corr_id,
            message="Document accepted for asynchronous scanning and processing.",
        )
