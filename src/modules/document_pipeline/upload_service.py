"""Upload Service implementing Stage 1: Upload & Stage 3: Promote/Reject.
2-bucket architecture: apag-quarantine -> apag-raw.
"""

import threading
import uuid

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


class UploadService:
    """Orchestrates PDF upload, quarantine storage, validation, and promotion/rejection."""

    def __init__(
        self,
        bucket_manager: BucketManager | None = None,
        repository: DocumentRepository | None = None,
        validation_service: ValidationService | None = None,
    ):
        self.buckets = bucket_manager or BucketManager()
        self.repo = repository or InMemoryDocumentRepository()
        self.validator = validation_service or ValidationService()
        self._promotion_lock = threading.Lock()

    def upload(
        self,
        filename: str,
        data: bytes,
        request_meta: UploadRequest | None = None,
    ) -> UploadResponse:
        """Stage 1: Quarantine Upload -> Stage 2: Validation -> Stage 3: Promote/Reject."""
        meta = request_meta or UploadRequest()
        document_id = uuid.uuid4()
        quarantine_key = f"{document_id}.pdf"

        # -------------------------------------------------------------
        # STAGE 1: Immediate Quarantine Landing
        # -------------------------------------------------------------
        self.buckets.storage.put_object(
            bucket_name=self.buckets.quarantine,
            object_name=quarantine_key,
            data=data,
            content_type="application/pdf",
        )

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

        # -------------------------------------------------------------
        # STAGE 2: Fail-Fast Pre-checks & Threat Scan
        # -------------------------------------------------------------
        validation = self.validator.validate_document(data, mime_type="application/pdf")

        # -------------------------------------------------------------
        # STAGE 3: Promotion / Rejection / Deduplication / Versioning
        # -------------------------------------------------------------
        if not validation.is_valid:
            # Rejection Branch
            doc.status = DocumentStatus.REJECTED
            doc.rejection_reason = validation.rejection_reason
            self.repo.update_document(doc)

            # Purge infected/corrupt object from quarantine
            self.buckets.storage.delete_object(self.buckets.quarantine, quarantine_key)

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

            # Promotion to Raw Bucket
            raw_key = f"{validation.sha256}.pdf"
            if not self.buckets.storage.object_exists(self.buckets.raw, raw_key):
                self.buckets.storage.copy_object(
                    source_bucket=self.buckets.quarantine,
                    source_object=quarantine_key,
                    dest_bucket=self.buckets.raw,
                    dest_object=raw_key,
                )

            # Remove from quarantine after promotion
            self.buckets.storage.delete_object(self.buckets.quarantine, quarantine_key)

            doc.status = DocumentStatus.AWAITING_CLASSIFICATION
            doc.raw_path = f"{self.buckets.raw}/{raw_key}"
            doc.quarantine_path = None
            self.repo.update_document(doc)

            return UploadResponse(
                document_id=document_id,
                filename=filename,
                status=DocumentStatus.AWAITING_CLASSIFICATION,
                quarantine_key=quarantine_key,
                checksum=validation.sha256,
                was_duplicate=False,
                message="Document passed all checks and was promoted to raw storage.",
            )
