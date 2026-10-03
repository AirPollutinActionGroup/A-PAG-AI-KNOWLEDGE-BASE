"""Embedding Job Handler for Stage 7.

Reads a document's stored passages, embeds them, and writes the vectors back onto the same rows.
This is the last stage in the ingestion pipeline: a document that completes here reaches LIVE and
is searchable.
"""

import logging
import uuid
from typing import Any

from sqlalchemy.orm import Session

from src.core.config import settings
from src.db.enums import AuditEventType
from src.db.models import DocumentChunk as ChunkORM
from src.modules.audit.service import AuditService
from src.modules.document_pipeline.embedding.service import EmbeddingService
from src.modules.document_pipeline.models import DocumentStatus
from src.modules.document_pipeline.repository import (
    DocumentRepository,
    InMemoryDocumentRepository,
)
from src.modules.document_pipeline.storage_keys import normalized_key_for
from src.storage.bucket_manager import BucketManager

logger = logging.getLogger(__name__)

# Languages the configured model is expected to represent. langdetect returns ISO 639-1 codes.
_DEFAULT_LANGUAGE = "en"


def prose_ratio(text: str) -> float:
    """The share of characters sitting inside word-like tokens.

    A token counts when it is three or more characters long and contains no digit. The test
    never looks *inside* the token, and that is the whole design: an earlier version counted
    runs of three or more letters, which works in English and scores Devanagari at **0.034**,
    because matras are combining marks and break the run. A genuine Hindi document would have
    looked exactly like a spreadsheet and slipped past the language gate — the precise failure
    the gate exists to prevent.

    Measured across this corpus and a Devanagari sample:

        CEDS_SO2 Emissions.xlsx (the misdetected grid)   0.021
        lowest real document                             0.309
        typical real document                            0.62 - 0.75
        Hindi prose                                      0.758

    Fifteen times the separation between the grid and the thinnest real document, and Devanagari
    lands with the documents rather than with the grids.
    """
    if not text:
        return 0.0
    counted = sum(
        len(tok) for tok in text.split()
        if len(tok) >= 3 and not any(c.isdigit() for c in tok)
    )
    return counted / len(text)


_SUPPORTED_LANGUAGES = {"en"}


class EmbeddingOutcome:
    """Result of one embedding job, in the terms the worker needs for retry vs. fail."""

    def __init__(
        self,
        document_id: uuid.UUID,
        status: DocumentStatus,
        message: str,
        vector_count: int = 0,
        failure_reason: str | None = None,
        transient: bool = False,
    ):
        self.document_id = document_id
        self.status = status
        self.message = message
        self.vector_count = vector_count
        self.failure_reason = failure_reason
        self.transient = transient


class EmbeddingJobHandler:
    """Embeds a chunked document's passages and marks it searchable."""

    def __init__(
        self,
        bucket_manager: BucketManager | None = None,
        repository: DocumentRepository | None = None,
        embedding_service: EmbeddingService | None = None,
        db_session: Session | None = None,
    ):
        self.buckets = bucket_manager or BucketManager()
        self.repo = repository or InMemoryDocumentRepository()
        self._embedder = embedding_service
        self._db = db_session

    @property
    def embedder(self) -> EmbeddingService:
        """Built on first use so constructing the handler — in tests, or in a worker that ends up
        skipping the document — does not load a model into memory."""
        if self._embedder is None:
            self._embedder = EmbeddingService()
        return self._embedder

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
    ) -> EmbeddingOutcome:
        corr_id = correlation_id or uuid.uuid4()

        doc = self.repo.get_by_id(document_id)
        if not doc:
            return EmbeddingOutcome(
                document_id=document_id,
                status=DocumentStatus.EMBEDDING_FAILED,
                message="Document record not found.",
                failure_reason="DOCUMENT_NOT_FOUND: Document ID not registered in database.",
            )

        # Idempotency guard: only a chunked document is embeddable.
        if doc.status != DocumentStatus.CHUNKED:
            logger.info(
                "Embedding job: doc_id=%s is %s, not CHUNKED — skipping",
                document_id, doc.status.value,
            )
            return EmbeddingOutcome(
                document_id=document_id,
                status=doc.status,
                message=f"Document is {doc.status.value}; embedding not applicable.",
            )

        language = self._detected_language(document_id)
        if settings.EMBEDDING_SKIP_NON_ENGLISH and language not in _SUPPORTED_LANGUAGES:
            return self._skip_unsupported_language(doc, corr_id, language)

        if self._db is None:
            return EmbeddingOutcome(
                document_id=document_id,
                status=doc.status,
                message="No database session; cannot read chunks.",
                failure_reason="NO_DB_SESSION: Embedding requires a database session.",
                transient=True,
            )

        chunks = (
            self._db.query(ChunkORM)
            .filter(ChunkORM.document_id == document_id)
            .order_by(ChunkORM.chunk_index)
            .all()
        )
        if not chunks:
            # Chunking wrote rows before setting CHUNKED, so finding none means they were removed
            # between the two — not something a retry fixes by itself, but also not a bad document.
            return self._fail(
                doc, corr_id,
                "NO_CHUNKS_FOUND: Document is CHUNKED but has no passages to embed.",
            )

        try:
            result = self.embedder.embed_document(document_id, [c.text for c in chunks])
        except ValueError as e:
            # Dimension mismatch between model and column — a misconfiguration. Retrying with the
            # same configuration produces the identical error, so fail rather than loop.
            return self._fail(doc, corr_id, f"EMBEDDING_CONFIG_ERROR: {e}")
        except Exception as e:
            # Model load, memory pressure, a transient inference failure — a retry can get past it.
            logger.exception("Embedding failed for doc_id=%s", document_id)
            return EmbeddingOutcome(
                document_id=document_id,
                status=doc.status,
                message="Embedding inference failed.",
                failure_reason=f"EMBEDDING_ERROR: {e}",
                transient=True,
            )

        if len(result.vectors) != len(chunks):
            return self._fail(
                doc, corr_id,
                f"VECTOR_COUNT_MISMATCH: {len(result.vectors)} vectors for {len(chunks)} chunks.",
            )

        try:
            for chunk, vector in zip(chunks, result.vectors, strict=True):
                chunk.embedding = vector
            self._db.commit()
        except Exception as e:
            self._db.rollback()
            logger.exception("Failed to persist embeddings for doc_id=%s", document_id)
            return EmbeddingOutcome(
                document_id=document_id,
                status=doc.status,
                message="Database error writing embeddings.",
                failure_reason=f"STORAGE_ERROR: Failed to persist embeddings ({e}).",
                transient=True,
            )

        doc.status = DocumentStatus.LIVE
        self.repo.update_document(doc)

        self._audit(document_id, AuditEventType.EMBEDDING_COMPLETED, details={
            "vector_count": result.vector_count,
            "model": result.model_name,
            "dimensions": result.dimensions,
        }, correlation_id=corr_id)

        logger.info(
            "EMBEDDED: corr_id=%s doc_id=%s vectors=%d model=%s -> LIVE",
            corr_id, document_id, result.vector_count, result.model_name,
        )
        return EmbeddingOutcome(
            document_id=document_id,
            status=DocumentStatus.LIVE,
            message=f"Document embedded into {result.vector_count} vectors and is searchable.",
            vector_count=result.vector_count,
        )

    def _detected_language(self, document_id: uuid.UUID) -> str | None:
        """Reads the language normalization detected, if it is worth believing.

        Returns None when the artifact cannot be read, which the caller treats as unsupported —
        refusing to embed is the safe direction when the language is genuinely unknown.

        Returns the *configured* language instead when the document has too little running prose
        for a detection to mean anything. Language detection needs sentences; given a grid it
        answers anyway, and confidently. A 130,000-character emissions spreadsheet whose text is
        `em  country  units  X2000  X2001 ...` was detected as Croatian and skipped, taking 460
        chunks out of the index. See EMBEDDING_MIN_PROSE_RATIO for the measurement.
        """
        try:
            from src.modules.document_pipeline.normalization.models import (
                NormalizationResult,
            )

            raw = self.buckets.storage.get_object(
                self.buckets.normalized, normalized_key_for(document_id)
            )
            result = NormalizationResult.model_validate_json(raw.decode("utf-8"))
        except Exception:
            logger.warning("Could not read language for doc_id=%s", document_id)
            return None

        ratio = prose_ratio(result.full_text)
        if ratio < settings.EMBEDDING_MIN_PROSE_RATIO:
            logger.info(
                "doc_id=%s is %.1f%% prose — too little to trust a language detection of %r; "
                "embedding it rather than skipping it",
                document_id, ratio * 100, result.language,
            )
            return _DEFAULT_LANGUAGE

        return result.language

    def _skip_unsupported_language(self, doc, corr_id: uuid.UUID, language: str | None):
        """Records a document the configured model cannot represent.

        Not a failure: the document is intact, its passages are stored, and it becomes embeddable
        the moment a model covering its language is configured. Embedding it anyway would produce
        vectors that match nothing — leaving it in the index but invisible, with no signal that
        anything is wrong. A recorded gap can be queried and fixed; a silent one cannot.
        """
        doc.status = DocumentStatus.SKIPPED_UNSUPPORTED_LANGUAGE
        self.repo.update_document(doc)

        self._audit(doc.id, AuditEventType.EMBEDDING_SKIPPED, details={
            "filename": doc.filename,
            "language": language,
            "model": settings.EMBEDDING_MODEL,
        }, correlation_id=corr_id)

        logger.info(
            "EMBEDDING SKIPPED: corr_id=%s doc_id=%s language=%s model=%s",
            corr_id, doc.id, language, settings.EMBEDDING_MODEL,
        )
        return EmbeddingOutcome(
            document_id=doc.id,
            status=DocumentStatus.SKIPPED_UNSUPPORTED_LANGUAGE,
            message=f"Language '{language}' is not covered by the configured embedding model.",
        )

    def _fail(self, doc, corr_id: uuid.UUID, reason: str) -> EmbeddingOutcome:
        """Stops a document that cannot be embedded. Not retried — the input is unchanged, so a
        retry produces the identical result."""
        doc.status = DocumentStatus.EMBEDDING_FAILED
        doc.rejection_reason = reason
        self.repo.update_document(doc)

        self._audit(doc.id, AuditEventType.EMBEDDING_FAILED, details={
            "filename": doc.filename,
            "reason": reason,
        }, correlation_id=corr_id)

        logger.warning("EMBEDDING FAILED: corr_id=%s doc_id=%s reason=%s", corr_id, doc.id, reason)
        return EmbeddingOutcome(
            document_id=doc.id,
            status=DocumentStatus.EMBEDDING_FAILED,
            message=f"Embedding rejected the document: {reason}",
            failure_reason=reason,
        )
