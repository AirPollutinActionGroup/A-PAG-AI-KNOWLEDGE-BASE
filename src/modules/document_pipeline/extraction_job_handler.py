"""Extraction Job Handler for Stage 4.

Reads a promoted document back out of `raw/`, extracts its text, writes the result to
`extracted/`, and hands off to the NORMALIZE stage — the same shape as `ScanJobHandler`, and
idempotent for the same reason: jobs get retried and reaped, so re-running one must be safe.
"""

import logging
import uuid
from typing import Any

from sqlalchemy.orm import Session

from src.db.enums import AuditEventType, JobStage, JobStatus
from src.db.models import Job as JobORM
from src.modules.audit.service import AuditService
from src.modules.document_pipeline.extraction.models import (
    ExtractionError,
    ExtractionResult,
)
from src.modules.document_pipeline.extraction.service import ExtractionService
from src.modules.document_pipeline.models import DocumentStatus
from src.modules.document_pipeline.repository import (
    DocumentRepository,
    InMemoryDocumentRepository,
)
from src.modules.document_pipeline.storage_keys import extraction_key_for
from src.storage.bucket_manager import BucketManager

logger = logging.getLogger(__name__)


class ExtractionOutcome:
    """Result of one extraction job, in the terms the worker needs to decide retry vs. fail."""

    def __init__(
        self,
        document_id: uuid.UUID,
        status: DocumentStatus,
        message: str,
        extraction_key: str | None = None,
        failure_reason: str | None = None,
        transient: bool = False,
    ):
        self.document_id = document_id
        self.status = status
        self.message = message
        self.extraction_key = extraction_key
        self.failure_reason = failure_reason
        self.transient = transient


class ExtractionJobHandler:
    """Extracts text from a promoted document and queues it for normalization."""

    def __init__(
        self,
        bucket_manager: BucketManager | None = None,
        repository: DocumentRepository | None = None,
        extraction_service: ExtractionService | None = None,
        db_session: Session | None = None,
    ):
        self.buckets = bucket_manager or BucketManager()
        self.repo = repository or InMemoryDocumentRepository()
        self.extractor = extraction_service or ExtractionService()
        self._db = db_session

    def _audit(
        self,
        document_id: uuid.UUID,
        event_type: AuditEventType,
        details: dict[str, Any] | None = None,
        correlation_id: uuid.UUID | None = None,
    ) -> None:
        """Best-effort, de-duplicated audit write — same contract as the other handlers: a
        failure here is logged loudly but never blocks the pipeline."""
        if self._db is None:
            logger.debug(
                "Audit skipped (no db session): doc_id=%s event=%s", document_id, event_type.value
            )
            return
        try:
            from src.db.models import AuditLog

            existing = (
                self._db.query(AuditLog)
                .filter(
                    AuditLog.document_id == document_id,
                    AuditLog.event_type == event_type.value,
                )
                .first()
            )
            if existing:
                logger.debug(
                    "Audit duplicate ignored: doc_id=%s event=%s", document_id, event_type.value
                )
                return

            AuditService.log_event(
                db=self._db,
                document_id=document_id,
                event_type=event_type,
                details=details,
                correlation_id=correlation_id,
            )
        except Exception:
            logger.exception(
                "AUDIT WRITE FAILED: doc_id=%s event=%s — compliance gap, investigate immediately",
                document_id, event_type.value,
            )

    def process(
        self,
        document_id: uuid.UUID,
        correlation_id: uuid.UUID | None = None,
    ) -> ExtractionOutcome:
        corr_id = correlation_id or uuid.uuid4()

        doc = self.repo.get_by_id(document_id)
        if not doc:
            return ExtractionOutcome(
                document_id=document_id,
                status=DocumentStatus.EXTRACTION_FAILED,
                message="Document record not found.",
                failure_reason="DOCUMENT_NOT_FOUND: Document ID not registered in database.",
            )

        # Idempotency guard: a retried or reaped job must not redo work already done.
        if doc.status != DocumentStatus.VALIDATED:
            logger.info(
                "Extraction job: doc_id=%s is %s, not VALIDATED — skipping",
                document_id, doc.status.value,
            )
            return ExtractionOutcome(
                document_id=document_id,
                status=doc.status,
                message=f"Document is {doc.status.value}; extraction not applicable.",
                extraction_key=extraction_key_for(document_id),
            )

        if not doc.raw_path:
            return self._fail(
                doc, corr_id,
                "MISSING_RAW_OBJECT: Document is VALIDATED but has no raw_path to read.",
            )

        raw_key = doc.raw_path.rsplit("/", 1)[-1]
        try:
            data = self.buckets.storage.get_object(self.buckets.raw, raw_key)
        except Exception as e:
            # Storage blips are transient — the raw object is still there, a retry can succeed.
            logger.error("Failed to read raw object key=%s: %s", raw_key, e)
            return ExtractionOutcome(
                document_id=document_id,
                status=doc.status,
                message="Could not read the promoted file from raw storage.",
                failure_reason=f"STORAGE_ERROR: Failed to read raw object ({e}).",
                transient=True,
            )

        try:
            result = self.extractor.extract(document_id, data, doc.mime_type)
        except ExtractionError as e:
            return self._fail(doc, corr_id, f"EXTRACTION_FAILED: {e}")

        extraction_key = extraction_key_for(document_id)
        try:
            self.buckets.storage.put_object(
                bucket_name=self.buckets.extracted,
                object_name=extraction_key,
                data=result.model_dump_json().encode("utf-8"),
                content_type="application/json",
            )
        except Exception as e:
            logger.exception("Failed to write extraction artifact for doc_id=%s", document_id)
            return ExtractionOutcome(
                document_id=document_id,
                status=doc.status,
                message="Storage error writing the extraction result.",
                failure_reason=f"STORAGE_ERROR: Failed to write extraction artifact ({e}).",
                transient=True,
            )

        doc.status = DocumentStatus.EXTRACTED
        self.repo.update_document(doc)

        self._audit(document_id, AuditEventType.EXTRACTION_COMPLETED, details={
            "extraction_key": extraction_key,
            "unit_count": result.unit_count,
            "char_count": result.char_count,
            "table_count": len(result.tables),
            "heading_count": len(result.headings),
            "extraction_method": result.extraction_method,
        }, correlation_id=corr_id)

        self._enqueue_normalize(document_id)

        logger.info(
            "EXTRACTED: corr_id=%s doc_id=%s units=%d chars=%d -> %s",
            corr_id, document_id, result.unit_count, result.char_count, extraction_key,
        )
        return ExtractionOutcome(
            document_id=document_id,
            status=DocumentStatus.EXTRACTED,
            message="Document text extracted and queued for normalization.",
            extraction_key=extraction_key,
        )

    def _fail(self, doc, corr_id: uuid.UUID, reason: str) -> ExtractionOutcome:
        """Marks a document permanently un-extractable. No NORMALIZE job is queued — the
        pipeline stops here until someone looks at it."""
        doc.status = DocumentStatus.EXTRACTION_FAILED
        doc.rejection_reason = reason
        self.repo.update_document(doc)

        self._audit(doc.id, AuditEventType.EXTRACTION_FAILED, details={
            "filename": doc.filename, "reason": reason,
        }, correlation_id=corr_id)

        logger.warning("EXTRACTION FAILED: corr_id=%s doc_id=%s reason=%s", corr_id, doc.id, reason)
        return ExtractionOutcome(
            document_id=doc.id,
            status=DocumentStatus.EXTRACTION_FAILED,
            message=f"Extraction failed: {reason}",
            failure_reason=reason,
        )

    def _enqueue_normalize(self, document_id: uuid.UUID) -> None:
        if self._db is None:
            return
        self._db.add(JobORM(
            job_id=uuid.uuid4(),
            document_id=document_id,
            stage=JobStage.NORMALIZE.value,
            status=JobStatus.PENDING.value,
        ))
        self._db.commit()

    def load_result(self, document_id: uuid.UUID) -> ExtractionResult:
        """Reads a stored extraction artifact back — used by the normalization stage."""
        raw = self.buckets.storage.get_object(
            self.buckets.extracted, extraction_key_for(document_id)
        )
        return ExtractionResult.model_validate_json(raw.decode("utf-8"))
