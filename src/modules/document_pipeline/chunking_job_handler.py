"""Chunking Job Handler for Stage 6.

Reads a normalized document, splits it into retrievable passages, and writes them to
`document_chunks`. Embedding is a separate stage on purpose: chunks are independent of whichever
embedding model is in use, so changing that model later is a re-run of one stage rather than a
re-chunk of the whole corpus.
"""

import logging
import uuid
from typing import Any

from sqlalchemy import delete
from sqlalchemy.orm import Session

from src.db.enums import AuditEventType
from src.db.models import DocumentChunk as ChunkORM
from src.modules.audit.service import AuditService
from src.modules.document_pipeline.chunking.models import ChunkingResult
from src.modules.document_pipeline.chunking.service import ChunkingService
from src.modules.document_pipeline.models import DocumentStatus
from src.modules.document_pipeline.normalization.models import NormalizationResult
from src.modules.document_pipeline.repository import (
    DocumentRepository,
    InMemoryDocumentRepository,
)
from src.modules.document_pipeline.storage_keys import normalized_key_for
from src.storage.bucket_manager import BucketManager

logger = logging.getLogger(__name__)


class ChunkingOutcome:
    """Result of one chunking job, in the terms the worker needs for retry vs. fail."""

    def __init__(
        self,
        document_id: uuid.UUID,
        status: DocumentStatus,
        message: str,
        chunk_count: int = 0,
        failure_reason: str | None = None,
        transient: bool = False,
    ):
        self.document_id = document_id
        self.status = status
        self.message = message
        self.chunk_count = chunk_count
        self.failure_reason = failure_reason
        self.transient = transient


class ChunkingJobHandler:
    """Splits a normalized document into passages and persists them."""

    def __init__(
        self,
        bucket_manager: BucketManager | None = None,
        repository: DocumentRepository | None = None,
        chunking_service: ChunkingService | None = None,
        db_session: Session | None = None,
    ):
        self.buckets = bucket_manager or BucketManager()
        self.repo = repository or InMemoryDocumentRepository()
        self.chunker = chunking_service or ChunkingService()
        self._db = db_session

    def _audit(
        self,
        document_id: uuid.UUID,
        event_type: AuditEventType,
        details: dict[str, Any] | None = None,
        correlation_id: uuid.UUID | None = None,
    ) -> None:
        """Best-effort, de-duplicated audit write. Never blocks the pipeline."""
        if self._db is None:
            return
        try:
            from src.db.models import AuditLog as AuditORM

            existing = (
                self._db.query(AuditORM)
                .filter(
                    AuditORM.document_id == document_id,
                    AuditORM.event_type == event_type.value,
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
                "AUDIT WRITE FAILED: doc_id=%s event=%s — compliance gap, investigate",
                document_id, event_type.value,
            )

    def process(
        self,
        document_id: uuid.UUID,
        correlation_id: uuid.UUID | None = None,
    ) -> ChunkingOutcome:
        corr_id = correlation_id or uuid.uuid4()

        doc = self.repo.get_by_id(document_id)
        if not doc:
            return ChunkingOutcome(
                document_id=document_id,
                status=DocumentStatus.CHUNKING_FAILED,
                message="Document record not found.",
                failure_reason="DOCUMENT_NOT_FOUND: Document ID not registered in database.",
            )

        # Idempotency guard: only a document that has finished normalizing is chunkable.
        if doc.status != DocumentStatus.AWAITING_CLASSIFICATION:
            logger.info(
                "Chunking job: doc_id=%s is %s, not AWAITING_CLASSIFICATION — skipping",
                document_id, doc.status.value,
            )
            return ChunkingOutcome(
                document_id=document_id,
                status=doc.status,
                message=f"Document is {doc.status.value}; chunking not applicable.",
            )

        try:
            raw = self.buckets.storage.get_object(
                self.buckets.normalized, normalized_key_for(document_id)
            )
            normalized = NormalizationResult.model_validate_json(raw.decode("utf-8"))
        except Exception as e:
            # Normalization wrote this artifact before setting the status, so a read failure is a
            # storage problem rather than a bad document — a retry can get past it.
            logger.error("Failed to read normalized artifact for doc_id=%s: %s", document_id, e)
            return ChunkingOutcome(
                document_id=document_id,
                status=doc.status,
                message="Could not read the normalized artifact.",
                failure_reason=f"STORAGE_ERROR: Failed to read normalized artifact ({e}).",
                transient=True,
            )

        result = self.chunker.chunk(normalized)

        if not result.chunks:
            # Normalization's quality gate already rejects empty documents, so reaching here with
            # nothing means the chunker disagreed with content the gate passed. That is a bug in
            # this stage, not a bad document — fail loudly rather than mark a document indexed
            # with no passages to retrieve.
            return self._fail(
                doc, corr_id,
                "NO_CHUNKS_PRODUCED: Normalized document yielded no passages.",
            )

        try:
            self._persist(document_id, result)
        except Exception as e:
            logger.exception("Failed to persist chunks for doc_id=%s", document_id)
            return ChunkingOutcome(
                document_id=document_id,
                status=doc.status,
                message="Database error writing chunks.",
                failure_reason=f"STORAGE_ERROR: Failed to persist chunks ({e}).",
                transient=True,
            )

        doc.status = DocumentStatus.CHUNKED
        self.repo.update_document(doc)

        self._audit(document_id, AuditEventType.CHUNKING_COMPLETED, details={
            "chunk_count": result.chunk_count,
            "table_chunk_count": result.table_chunk_count,
            "total_chars": result.total_chars,
        }, correlation_id=corr_id)

        logger.info(
            "CHUNKED: corr_id=%s doc_id=%s chunks=%d (%d tables) chars=%d",
            corr_id, document_id, result.chunk_count,
            result.table_chunk_count, result.total_chars,
        )
        return ChunkingOutcome(
            document_id=document_id,
            status=DocumentStatus.CHUNKED,
            message=f"Document split into {result.chunk_count} passages.",
            chunk_count=result.chunk_count,
        )

    def _persist(self, document_id: uuid.UUID, result: ChunkingResult) -> None:
        """Replaces this document's chunks in one transaction.

        Deletes first because a retry that half-succeeded would otherwise collide with the unique
        (document_id, scale, chunk_index) index, and because re-chunking after a rule change must
        not leave passages from the old rules behind.
        """
        if self._db is None:
            return

        self._db.execute(
            delete(ChunkORM).where(ChunkORM.document_id == document_id)
        )
        self._db.add_all([
            ChunkORM(
                chunk_id=uuid.uuid4(),
                document_id=document_id,
                scale=chunk.scale,
                chunk_index=chunk.index,
                text=chunk.text,
                page_number=chunk.page_number,
                section_heading=chunk.section_heading,
                is_table=chunk.is_table,
                char_count=chunk.char_count,
            )
            for chunk in result.chunks
        ])
        self._db.commit()

    def _fail(self, doc, corr_id: uuid.UUID, reason: str) -> ChunkingOutcome:
        """Stops a document that could not be chunked. Not retried — the input is unchanged, so
        a retry produces the identical result."""
        doc.status = DocumentStatus.CHUNKING_FAILED
        doc.rejection_reason = reason
        self.repo.update_document(doc)

        self._audit(doc.id, AuditEventType.CHUNKING_FAILED, details={
            "filename": doc.filename,
            "reason": reason,
        }, correlation_id=corr_id)

        logger.warning("CHUNKING FAILED: corr_id=%s doc_id=%s reason=%s", corr_id, doc.id, reason)
        return ChunkingOutcome(
            document_id=doc.id,
            status=DocumentStatus.CHUNKING_FAILED,
            message=f"Chunking rejected the document: {reason}",
            failure_reason=reason,
        )

    def load_chunks(self, document_id: uuid.UUID) -> list[ChunkORM]:
        """Reads a document's stored chunks back — for the embedding stage and for tests."""
        if self._db is None:
            return []
        return (
            self._db.query(ChunkORM)
            .filter(ChunkORM.document_id == document_id)
            .order_by(ChunkORM.chunk_index)
            .all()
        )
