"""Normalization Job Handler for Stage 5.

The last stage currently built. A document that passes here reaches AWAITING_CLASSIFICATION,
which means exactly what it says — normalization is done and classification is the next thing
that needs to happen to it (Phase 5, not yet built). Nothing is queued after this.
"""

import logging
import uuid
from typing import Any

from sqlalchemy.orm import Session

from src.db.enums import AuditEventType
from src.modules.audit.service import AuditService
from src.modules.document_pipeline.extraction.models import ExtractionResult
from src.modules.document_pipeline.models import DocumentStatus
from src.modules.document_pipeline.normalization.models import NormalizationResult
from src.modules.document_pipeline.normalization.service import NormalizationService
from src.modules.document_pipeline.repository import (
    DocumentRepository,
    InMemoryDocumentRepository,
)
from src.modules.document_pipeline.storage_keys import (
    extraction_key_for,
    normalized_key_for,
)
from src.storage.bucket_manager import BucketManager

logger = logging.getLogger(__name__)


class NormalizationOutcome:
    """Result of one normalization job, in the terms the worker needs for retry vs. fail."""

    def __init__(
        self,
        document_id: uuid.UUID,
        status: DocumentStatus,
        message: str,
        normalized_key: str | None = None,
        failure_reason: str | None = None,
        transient: bool = False,
    ):
        self.document_id = document_id
        self.status = status
        self.message = message
        self.normalized_key = normalized_key
        self.failure_reason = failure_reason
        self.transient = transient


class NormalizationJobHandler:
    """Cleans a document's extracted text, gates it on quality, and finalizes its status."""

    def __init__(
        self,
        bucket_manager: BucketManager | None = None,
        repository: DocumentRepository | None = None,
        normalization_service: NormalizationService | None = None,
        db_session: Session | None = None,
    ):
        self.buckets = bucket_manager or BucketManager()
        self.repo = repository or InMemoryDocumentRepository()
        self.normalizer = normalization_service or NormalizationService()
        self._db = db_session

    def _audit(
        self,
        document_id: uuid.UUID,
        event_type: AuditEventType,
        details: dict[str, Any] | None = None,
        correlation_id: uuid.UUID | None = None,
    ) -> None:
        """Best-effort, de-duplicated audit write — same contract as the other handlers."""
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
    ) -> NormalizationOutcome:
        corr_id = correlation_id or uuid.uuid4()

        doc = self.repo.get_by_id(document_id)
        if not doc:
            return NormalizationOutcome(
                document_id=document_id,
                status=DocumentStatus.NORMALIZATION_FAILED,
                message="Document record not found.",
                failure_reason="DOCUMENT_NOT_FOUND: Document ID not registered in database.",
            )

        # Idempotency guard: only a document that has just been extracted is normalizable.
        if doc.status != DocumentStatus.EXTRACTED:
            logger.info(
                "Normalization job: doc_id=%s is %s, not EXTRACTED — skipping",
                document_id, doc.status.value,
            )
            return NormalizationOutcome(
                document_id=document_id,
                status=doc.status,
                message=f"Document is {doc.status.value}; normalization not applicable.",
                normalized_key=normalized_key_for(document_id),
            )

        try:
            raw = self.buckets.storage.get_object(
                self.buckets.extracted, extraction_key_for(document_id)
            )
            extraction = ExtractionResult.model_validate_json(raw.decode("utf-8"))
        except Exception as e:
            # The artifact should be there — extraction wrote it before setting EXTRACTED. A read
            # failure is therefore a storage problem, which a retry can get past.
            logger.error("Failed to read extraction artifact for doc_id=%s: %s", document_id, e)
            return NormalizationOutcome(
                document_id=document_id,
                status=doc.status,
                message="Could not read the extraction artifact.",
                failure_reason=f"STORAGE_ERROR: Failed to read extraction artifact ({e}).",
                transient=True,
            )

        result = self.normalizer.normalize(extraction)

        if not result.quality.passed:
            return self._fail(doc, corr_id, result)

        normalized_key = normalized_key_for(document_id)
        try:
            self.buckets.storage.put_object(
                bucket_name=self.buckets.normalized,
                object_name=normalized_key,
                data=result.model_dump_json().encode("utf-8"),
                content_type="application/json",
            )
        except Exception as e:
            logger.exception("Failed to write normalized artifact for doc_id=%s", document_id)
            return NormalizationOutcome(
                document_id=document_id,
                status=doc.status,
                message="Storage error writing the normalized result.",
                failure_reason=f"STORAGE_ERROR: Failed to write normalized artifact ({e}).",
                transient=True,
            )

        doc.status = DocumentStatus.AWAITING_CLASSIFICATION
        self.repo.update_document(doc)

        self._audit(document_id, AuditEventType.NORMALIZATION_COMPLETED, details={
            "normalized_key": normalized_key,
            "char_count": result.char_count,
            "unit_count": result.unit_count,
            "language": result.language,
            "table_count": len(result.tables),
        }, correlation_id=corr_id)

        logger.info(
            "NORMALIZED: corr_id=%s doc_id=%s chars=%d language=%s -> %s",
            corr_id, document_id, result.char_count, result.language, normalized_key,
        )
        return NormalizationOutcome(
            document_id=document_id,
            status=DocumentStatus.AWAITING_CLASSIFICATION,
            message="Document normalized and awaiting classification.",
            normalized_key=normalized_key,
        )

    def _fail(self, doc, corr_id: uuid.UUID, result: NormalizationResult) -> NormalizationOutcome:
        """Stops a document that failed the quality gate.

        Not retried: the content was read correctly, it just isn't usable — retrying produces the
        identical result. The failure reason names the specific check so someone can see whether
        it is a scan that needs OCR or a genuinely empty document.
        """
        reason = f"QUALITY_CHECK_FAILED: {', '.join(result.quality.failures)}"
        doc.status = DocumentStatus.NORMALIZATION_FAILED
        doc.rejection_reason = reason
        self.repo.update_document(doc)

        self._audit(doc.id, AuditEventType.NORMALIZATION_FAILED, details={
            "filename": doc.filename,
            "failures": result.quality.failures,
            **{k: v for k, v in result.quality.details.items()},
        }, correlation_id=corr_id)

        logger.warning(
            "NORMALIZATION FAILED: corr_id=%s doc_id=%s failures=%s details=%s",
            corr_id, doc.id, result.quality.failures, result.quality.details,
        )
        return NormalizationOutcome(
            document_id=doc.id,
            status=DocumentStatus.NORMALIZATION_FAILED,
            message=f"Normalization rejected the document: {reason}",
            failure_reason=reason,
        )

    def load_result(self, document_id: uuid.UUID) -> NormalizationResult:
        """Reads a stored normalized artifact back — for the future chunking stage and for tests."""
        raw = self.buckets.storage.get_object(
            self.buckets.normalized, normalized_key_for(document_id)
        )
        return NormalizationResult.model_validate_json(raw.decode("utf-8"))
