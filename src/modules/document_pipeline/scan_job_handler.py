"""Scan Job Handler for Stage 2 (Validation/Threat Scan) & Stage 3 (Promote/Reject).

Decouples heavy processing from the synchronous upload request path so it can be
driven either inline or by asynchronous background workers.
"""

import logging
import threading
import uuid
from typing import Any

from sqlalchemy.orm import Session

from src.db.enums import AuditEventType
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
from src.modules.document_pipeline.validation import ValidationService
from src.storage.bucket_manager import BucketManager

logger = logging.getLogger(__name__)


class ScanJobHandler:
    """Executes document validation, threat scanning, deduplication, and promotion/rejection."""

    def __init__(
        self,
        bucket_manager: BucketManager | None = None,
        repository: DocumentRepository | None = None,
        validation_service: ValidationService | None = None,
        db_session: Session | None = None,
    ):
        self.buckets = bucket_manager or BucketManager()
        self.repo = repository or InMemoryDocumentRepository()
        self.validator = validation_service or ValidationService()
        self._promotion_lock = threading.Lock()
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

    def process(
        self,
        document_id: uuid.UUID,
        correlation_id: uuid.UUID | None = None,
        request_meta: UploadRequest | None = None,
    ) -> UploadResponse:
        """Processes a quarantined document: validation -> threat scan -> promote/reject."""
        corr_id = correlation_id or uuid.uuid4()
        meta = request_meta or UploadRequest()
        quarantine_key = f"{document_id}.pdf"

        # 1. Fetch document from repository
        doc = self.repo.get_by_id(document_id)
        if not doc:
            logger.error("Scan job failed: doc_id=%s not found in repository", document_id)
            return UploadResponse(
                document_id=document_id,
                filename="unknown.pdf",
                status=DocumentStatus.VALIDATION_FAILED,
                quarantine_key=quarantine_key,
                rejection_reason="DOCUMENT_NOT_FOUND: Document ID not registered in database.",
                message="Document record not found.",
            )

        filename = doc.filename

        # 2. Retrieve bytes from quarantine storage
        try:
            data = self.buckets.storage.get_object(self.buckets.quarantine, quarantine_key)
        except Exception as e:
            logger.error("Failed to read quarantined object key=%s: %s", quarantine_key, e)
            doc.status = DocumentStatus.VALIDATION_FAILED
            doc.rejection_reason = "STORAGE_ERROR: Failed to retrieve quarantined file."
            self.repo.update_document(doc)
            return UploadResponse(
                document_id=document_id,
                filename=filename,
                status=DocumentStatus.VALIDATION_FAILED,
                quarantine_key=quarantine_key,
                rejection_reason=doc.rejection_reason,
                message="Could not read file from quarantine storage.",
            )

        # -------------------------------------------------------------
        # STAGE 2: Fail-Fast Pre-checks & Threat Scan
        # -------------------------------------------------------------
        validation = self.validator.validate_document(data, mime_type="application/pdf")
        logger.info(
            "Validation result: corr_id=%s doc_id=%s valid=%s reason=%s",
            corr_id, document_id, validation.is_valid, validation.rejection_reason,
        )

        # -------------------------------------------------------------
        # STAGE 3: Promotion / Rejection / Deduplication / Versioning
        # -------------------------------------------------------------
        if not validation.is_valid:
            # Rejection Branch
            doc.status = DocumentStatus.REJECTED
            doc.rejection_reason = validation.rejection_reason
            self.repo.update_document(doc)

            # AUDIT: Validation failed → document rejected
            self._audit(document_id, AuditEventType.DOCUMENT_REJECTED, details={
                "filename": filename, "rejection_reason": validation.rejection_reason,
                "file_size_bytes": len(data),
            }, correlation_id=corr_id)

            # Purge infected/corrupt object from quarantine
            self.buckets.storage.delete_object(self.buckets.quarantine, quarantine_key)
            logger.warning(
                "REJECTED: corr_id=%s doc_id=%s reason=%s",
                corr_id, document_id, validation.rejection_reason,
            )

            return UploadResponse(
                document_id=document_id,
                filename=filename,
                status=DocumentStatus.REJECTED,
                quarantine_key=quarantine_key,
                checksum=None,
                rejection_reason=validation.rejection_reason,
                message=f"Upload rejected: {validation.rejection_reason}",
            )

        # Valid document: compute checksum
        doc.checksum = validation.sha256

        # Critical Section: Deduplication, Versioning & Promotion
        with self._promotion_lock:
            # Deduplication Check
            existing_doc = self.repo.get_by_checksum(validation.sha256)
            if existing_doc and existing_doc.id != document_id:
                doc.status = DocumentStatus.DUPLICATE
                self.repo.update_document(doc)
                self.buckets.storage.delete_object(self.buckets.quarantine, quarantine_key)

                # AUDIT: Duplicate detected → rejected
                self._audit(document_id, AuditEventType.DOCUMENT_REJECTED, details={
                    "filename": filename, "reason": "DUPLICATE",
                    "canonical_document_id": str(existing_doc.id),
                    "sha256": validation.sha256,
                }, correlation_id=corr_id)

                logger.info(
                    "DUPLICATE: corr_id=%s doc_id=%s matches canonical=%s sha256=%s",
                    corr_id, document_id, existing_doc.id, validation.sha256,
                )

                return UploadResponse(
                    document_id=existing_doc.id,
                    filename=filename,
                    status=DocumentStatus.DUPLICATE,
                    quarantine_key=quarantine_key,
                    checksum=validation.sha256,
                    was_duplicate=True,
                    message=f"Duplicate document detected (matches canonical document ID: {existing_doc.id}).",
                )

            # Versioning: Check if this supersedes an older document
            if meta.supersedes_doc_id:
                prior_doc = self.repo.get_by_id(meta.supersedes_doc_id)
                if prior_doc:
                    doc.version = prior_doc.version + 1
                    doc.supersedes_id = prior_doc.id

                    # Update old version status based on keep_previous_version flag
                    if meta.keep_previous_version:
                        prior_doc.status = DocumentStatus.SUPERSEDED
                    else:
                        prior_doc.status = DocumentStatus.ARCHIVED
                    self.repo.update_document(prior_doc)

                    # AUDIT: Prior document superseded
                    self._audit(prior_doc.id, AuditEventType.DOCUMENT_SUPERSEDED, details={
                        "superseded_by": str(document_id),
                        "new_version": doc.version,
                        "prior_status": prior_doc.status.value,
                    }, correlation_id=corr_id)

                    logger.info(
                        "VERSIONED: corr_id=%s doc_id=%s v%d supersedes=%s (prior now %s)",
                        corr_id, document_id, doc.version, prior_doc.id, prior_doc.status.value,
                    )

            # Promotion to Raw Bucket — wrapped in error recovery
            raw_key = f"{validation.sha256}.pdf"
            try:
                if not self.buckets.storage.object_exists(self.buckets.raw, raw_key):
                    self.buckets.storage.copy_object(
                        source_bucket=self.buckets.quarantine,
                        source_object=quarantine_key,
                        dest_bucket=self.buckets.raw,
                        dest_object=raw_key,
                    )

                # Remove from quarantine after successful promotion
                self.buckets.storage.delete_object(self.buckets.quarantine, quarantine_key)
            except Exception:
                logger.exception(
                    "PROMOTION FAILED: doc_id=%s — storage error during copy/delete. "
                    "Quarantine file preserved for retry.",
                    document_id,
                )
                doc.status = DocumentStatus.VALIDATION_FAILED
                doc.rejection_reason = "STORAGE_ERROR: Failed to promote file from quarantine to raw storage."
                self.repo.update_document(doc)

                return UploadResponse(
                    document_id=document_id,
                    filename=filename,
                    status=DocumentStatus.VALIDATION_FAILED,
                    quarantine_key=quarantine_key,
                    checksum=validation.sha256,
                    rejection_reason=doc.rejection_reason,
                    message="Storage error during promotion. File preserved in quarantine for retry.",
                )

            doc.status = DocumentStatus.AWAITING_CLASSIFICATION
            doc.raw_path = f"{self.buckets.raw}/{raw_key}"
            doc.quarantine_path = None
            self.repo.update_document(doc)

            # AUDIT: Validation passed + promoted to raw
            self._audit(document_id, AuditEventType.VALIDATION_PASSED, details={
                "sha256": validation.sha256, "page_count": validation.page_count,
                "file_size_bytes": validation.file_size_bytes,
            }, correlation_id=corr_id)
            self._audit(document_id, AuditEventType.DOCUMENT_PROMOTED, details={
                "raw_path": doc.raw_path, "sha256": validation.sha256,
                "version": doc.version,
            }, correlation_id=corr_id)

            logger.info(
                "PROMOTED: corr_id=%s doc_id=%s -> %s sha256=%s",
                corr_id, document_id, doc.raw_path, validation.sha256,
            )

            return UploadResponse(
                document_id=document_id,
                filename=filename,
                status=DocumentStatus.AWAITING_CLASSIFICATION,
                quarantine_key=quarantine_key,
                checksum=validation.sha256,
                was_duplicate=False,
                message="Document passed all checks and was promoted to raw storage.",
            )
