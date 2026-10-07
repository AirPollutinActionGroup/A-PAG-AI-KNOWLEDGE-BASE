"""Scan Job Handler for Stage 2 (Validation/Threat Scan) & Stage 3 (Promote/Reject).

Decouples heavy processing from the synchronous upload request path so it can be
driven either inline or by asynchronous background workers.
"""

import logging
import threading
import uuid
from typing import Any

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from src.db.enums import AuditEventType, Classification, JobStage, JobStatus
from src.db.models import Job as JobORM
from src.modules.audit.service import AuditService
from src.modules.auth.access import is_stricter
from src.modules.document_pipeline.models import (
    DocumentStatus,
    UploadRequest,
    UploadResponse,
)
from src.modules.document_pipeline.repository import (
    DocumentRepository,
    InMemoryDocumentRepository,
)
from src.modules.document_pipeline.storage_keys import (
    build_quarantine_key,
    build_raw_key,
    quarantine_key_for,
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
        allow_repeat: bool = False,
    ) -> None:
        """Best-effort audit log write. Prevents duplicate audit events on re-try.

        `allow_repeat` switches that guard off. The guard is right for stage events — a job can
        be reaped and re-run, and a document is still only extracted once, so a second
        EXTRACTION_COMPLETED row would be an artifact of the queue rather than a fact about the
        document. It is wrong for DOCUMENT_RECLASSIFIED, where a second tier change is a
        genuinely different event: dropping it would leave the log asserting a tier the document
        no longer carries, which is worse than no row at all.
        """
        if self._db is None:
            logger.debug(
                "Audit skipped (no db session): doc_id=%s event=%s",
                document_id, event_type.value,
            )
            return
        try:
            # Idempotency guard: do not write duplicate audit events for the same document and event_type
            if not allow_repeat:
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
                        "Audit duplicate ignored: doc_id=%s event=%s",
                        document_id, event_type.value,
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

    def _escalate_tier_if_stricter(
        self,
        canonical: Any,
        incoming: Classification | None,
        *,
        duplicate_id: uuid.UUID,
        sha256: str,
        correlation_id: uuid.UUID | None,
    ) -> bool:
        """Raises the canonical document's tier when a duplicate arrives more restricted.

        Dedup matches on SHA-256 alone, so the second copy is discarded and its classification is
        discarded with it. That is survivable in one direction and not the other. A file already
        in the corpus as PUBLIC, uploaded again as RESTRICTED, used to stay PUBLIC — org-wide
        visible, and still eligible to cross the Data Boundary Gateway to an external model. The
        tier nobody chose won, and nothing recorded that two tiers had ever disagreed. A folder
        connector makes that a one-second action, which is what turned a latent hole into a
        likely one.

        **Escalation only.** A duplicate arriving PUBLIC against a RESTRICTED canonical changes
        nothing: a copy turning up somewhere public is not evidence that the contents stopped
        being sensitive, and honouring that direction would make re-uploading a file a way to
        declassify it.

        Returns whether it moved, so the caller can say so. Logged at WARNING because it is a
        correct outcome that someone should still see — a file dropped in the wrong place pulls a
        genuinely public document out of everyone's view until an ADMIN reverses it through
        `POST /documents/{id}/classify`.
        """
        if not is_stricter(incoming, canonical.classification):
            return False

        previous = canonical.classification.value if canonical.classification else None
        new_tier = Classification(incoming)

        # Applied before the audit write, not after. `_audit` swallows its own failures by
        # design, so writing the record first would make the protective change conditional on
        # something deliberately non-fatal.
        canonical.classification = new_tier
        self.repo.update_document(canonical)

        self._audit(
            canonical.id,
            AuditEventType.DOCUMENT_RECLASSIFIED,
            details={
                "old_tier": previous,
                "new_tier": new_tier.value,
                # Deliberately no `reclassified_by`. The endpoint records a named user because a
                # person asked for the change; nobody asked for this one, and naming an actor
                # would make the log claim something untrue.
                "reason": "dedup-escalation",
                "duplicate_document_id": str(duplicate_id),
                "sha256": sha256,
            },
            correlation_id=correlation_id,
            allow_repeat=True,
        )

        logger.warning(
            "TIER ESCALATED by duplicate: canonical=%s %s -> %s duplicate=%s sha256=%s",
            canonical.id, previous, new_tier.value, duplicate_id, sha256,
        )
        return True

    def process(
        self,
        document_id: uuid.UUID,
        correlation_id: uuid.UUID | None = None,
        request_meta: UploadRequest | None = None,
    ) -> UploadResponse:
        """Processes a quarantined document: validation -> threat scan -> promote/reject."""
        corr_id = correlation_id or uuid.uuid4()

        # 1. Fetch document from repository
        doc = self.repo.get_by_id(document_id)
        if not doc:
            logger.error("Scan job failed: doc_id=%s not found in repository", document_id)
            return UploadResponse(
                document_id=document_id,
                filename="unknown",
                status=DocumentStatus.VALIDATION_FAILED,
                quarantine_key=build_quarantine_key(document_id, "application/pdf"),
                rejection_reason="DOCUMENT_NOT_FOUND: Document ID not registered in database.",
                message="Document record not found.",
            )

        filename = doc.filename
        quarantine_key = quarantine_key_for(doc)

        # Idempotency guard: If document is already promoted or finalized, return existing state.
        # VALIDATED/EXTRACTED/NORMALIZATION_FAILED are all "already past this stage" — re-running
        # a retried/reaped SCAN job on one of them must not re-validate or re-promote it.
        if doc.status in (
            DocumentStatus.VALIDATED,
            DocumentStatus.EXTRACTED,
            DocumentStatus.EXTRACTION_FAILED,
            DocumentStatus.NORMALIZATION_FAILED,
            DocumentStatus.AWAITING_CLASSIFICATION,
            DocumentStatus.LIVE,
        ):
            logger.info("Scan job: doc_id=%s already in %s, skipping re-scan", document_id, doc.status.value)
            return UploadResponse(
                document_id=document_id,
                filename=filename,
                status=doc.status,
                quarantine_key=quarantine_key,
                checksum=doc.checksum,
                was_duplicate=False,
                message="Document already processed and promoted.",
            )
        if doc.status in (DocumentStatus.REJECTED, DocumentStatus.DUPLICATE):
            logger.info("Scan job: doc_id=%s already finalized as %s", document_id, doc.status.value)
            return UploadResponse(
                document_id=document_id,
                filename=filename,
                status=doc.status,
                quarantine_key=quarantine_key,
                checksum=doc.checksum,
                rejection_reason=doc.rejection_reason,
                was_duplicate=(doc.status == DocumentStatus.DUPLICATE),
                message=f"Document already finalized: {doc.rejection_reason}",
            )

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
        validation = self.validator.validate_document(data, mime_type=doc.mime_type)
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
            # Cleared so a later DELETE doesn't try to remove an object that no longer exists
            # (see the promotion branch below, which clears it the same way on success).
            doc.quarantine_path = None
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
                # Let the stricter of the two tiers win before this copy is dropped. The
                # duplicate is discarded either way; only the canonical document moves.
                escalated = self._escalate_tier_if_stricter(
                    existing_doc,
                    doc.classification,
                    duplicate_id=document_id,
                    sha256=validation.sha256,
                    correlation_id=corr_id,
                )
                doc.status = DocumentStatus.DUPLICATE
                # Cleared for the same reason as the rejection branch above: the object is about
                # to be deleted, so nothing should keep pointing at it.
                doc.quarantine_path = None
                self.repo.update_document(doc)
                self.buckets.storage.delete_object(self.buckets.quarantine, quarantine_key)

                # AUDIT: Duplicate detected → rejected
                self._audit(document_id, AuditEventType.DOCUMENT_REJECTED, details={
                    "filename": filename, "reason": "DUPLICATE",
                    "canonical_document_id": str(existing_doc.id),
                    "sha256": validation.sha256,
                    "canonical_tier_escalated": escalated,
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
                    canonical_tier_escalated=escalated,
                    message=(
                        f"Duplicate document detected (matches canonical document ID: "
                        f"{existing_doc.id})."
                        + (
                            f" The canonical document was raised to "
                            f"{existing_doc.classification.value} because this copy was filed "
                            f"more restricted."
                            if escalated else ""
                        )
                    ),
                )

            # Versioning: check if this supersedes an older document.
            #
            # Read straight off `request_meta` rather than via a defaulted UploadRequest. The
            # default object made this look live when it is not: the SCAN worker is a separate
            # process that only receives a document id, so `request_meta` is None on every
            # production path and `supersedes_doc_id` was always None. The behaviour is
            # unchanged — what changes is that it now says so. See KNOWN_DEBTS.
            supersedes_id = request_meta.supersedes_doc_id if request_meta else None
            keep_previous = request_meta.keep_previous_version if request_meta else True
            if supersedes_id:
                prior_doc = self.repo.get_by_id(supersedes_id)
                if prior_doc:
                    doc.version = prior_doc.version + 1
                    doc.supersedes_id = prior_doc.id

                    # Update old version status based on keep_previous_version flag
                    if keep_previous:
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
            raw_key = build_raw_key(validation.sha256, doc.mime_type)
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

            # VALIDATED, not AWAITING_CLASSIFICATION: promotion hands off to the EXTRACT stage
            # next, not straight to classification. AWAITING_CLASSIFICATION is now set by
            # NormalizationJobHandler once normalization actually succeeds.
            doc.status = DocumentStatus.VALIDATED
            doc.raw_path = f"{self.buckets.raw}/{raw_key}"
            doc.quarantine_path = None
            doc.page_count = validation.page_count
            try:
                self.repo.update_document(doc)
            except IntegrityError:
                # Lost a race against another worker promoting the same sha256 concurrently.
                # The DB partial unique index (uq_documents_active_sha256) is the real guard;
                # the in-process lock above only protects a single worker process.
                if self._db is not None:
                    self._db.rollback()
                doc.status = DocumentStatus.DUPLICATE
                doc.raw_path = None
                self.repo.update_document(doc)

                # Note: the shared/content-addressed raw object (raw_key = sha256.pdf) is left in
                # place — it belongs to the canonical document that won the race, not this one.
                canonical = self.repo.get_by_checksum(validation.sha256)

                # Same escalation as the lock-held branch above, because this is the same
                # situation reached by a different route: two copies, disagreeing tiers, and the
                # loser about to be discarded. Guarding on the id matters here and not there —
                # `doc` has just been written as DUPLICATE and the Postgres query filters only
                # SUPERSEDED/ARCHIVED, so this lookup can hand back the very document that lost.
                escalated = False
                if canonical is not None and canonical.id != document_id:
                    escalated = self._escalate_tier_if_stricter(
                        canonical,
                        doc.classification,
                        duplicate_id=document_id,
                        sha256=validation.sha256,
                        correlation_id=corr_id,
                    )

                self._audit(document_id, AuditEventType.DOCUMENT_REJECTED, details={
                    "filename": filename, "reason": "DUPLICATE_RACE",
                    "canonical_document_id": str(canonical.id) if canonical else None,
                    "sha256": validation.sha256,
                    "canonical_tier_escalated": escalated,
                }, correlation_id=corr_id)

                logger.info(
                    "DUPLICATE (race): corr_id=%s doc_id=%s sha256=%s",
                    corr_id, document_id, validation.sha256,
                )

                return UploadResponse(
                    document_id=canonical.id if canonical else document_id,
                    filename=filename,
                    status=DocumentStatus.DUPLICATE,
                    quarantine_key=quarantine_key,
                    checksum=validation.sha256,
                    was_duplicate=True,
                    canonical_tier_escalated=escalated,
                    message="Duplicate document detected (concurrent upload of identical content).",
                )

            # AUDIT: Validation passed + promoted to raw
            self._audit(document_id, AuditEventType.VALIDATION_PASSED, details={
                "sha256": validation.sha256, "page_count": validation.page_count,
                "file_size_bytes": validation.file_size_bytes,
            }, correlation_id=corr_id)
            self._audit(document_id, AuditEventType.DOCUMENT_PROMOTED, details={
                "raw_path": doc.raw_path, "sha256": validation.sha256,
                "version": doc.version,
            }, correlation_id=corr_id)

            # Enqueue the next stage — mirrors how UploadService.receive() enqueues the SCAN job.
            if self._db is not None:
                self._db.add(JobORM(
                    job_id=uuid.uuid4(),
                    document_id=document_id,
                    stage=JobStage.EXTRACT.value,
                    status=JobStatus.PENDING.value,
                ))
                self._db.commit()

            logger.info(
                "PROMOTED: corr_id=%s doc_id=%s -> %s sha256=%s",
                corr_id, document_id, doc.raw_path, validation.sha256,
            )

            return UploadResponse(
                document_id=document_id,
                filename=filename,
                status=DocumentStatus.VALIDATED,
                quarantine_key=quarantine_key,
                checksum=validation.sha256,
                was_duplicate=False,
                message="Document passed all checks and was promoted to raw storage.",
            )
