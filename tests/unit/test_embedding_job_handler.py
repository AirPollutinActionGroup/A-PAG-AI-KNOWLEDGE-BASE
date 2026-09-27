"""Embedding job handler tests — the stage's wiring into the pipeline.

Covers what the provider's own tests can't: status transitions, the language gate, vector
persistence, idempotency under retries, and the transient-vs-permanent split the worker relies on
to decide whether to retry. No real model is loaded — a fake provider stands in, because the
behaviour under test is the handler's, not the model's.
"""

import uuid

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from src.db.enums import AuditEventType
from src.db.models import AuditLog, Base
from src.db.models import DocumentChunk as ChunkORM
from src.modules.document_pipeline.embedding.provider import EmbeddingProvider
from src.modules.document_pipeline.embedding.service import EmbeddingService
from src.modules.document_pipeline.embedding_job_handler import EmbeddingJobHandler
from src.modules.document_pipeline.extraction.models import Heading
from src.modules.document_pipeline.formats import PDF_MIME
from src.modules.document_pipeline.models import Document as DocumentDTO
from src.modules.document_pipeline.models import DocumentStatus
from src.modules.document_pipeline.normalization.models import (
    NormalizationResult,
    NormalizedUnit,
    QualityCheckResult,
)
from src.modules.document_pipeline.repository import InMemoryDocumentRepository
from src.modules.document_pipeline.storage_keys import normalized_key_for
from src.storage.bucket_manager import BucketManager
from src.storage.object_storage import LocalFileSystemStorage

DIM = 8


class FakeProvider(EmbeddingProvider):
    def __init__(self, dimensions: int = DIM, fail_with: Exception | None = None):
        self._dimensions = dimensions
        self._fail_with = fail_with

    @property
    def model_name(self) -> str:
        return "fake/model"

    @property
    def dimensions(self) -> int:
        return self._dimensions

    def embed_passages(self, texts):
        if self._fail_with:
            raise self._fail_with
        return [[0.1] * self._dimensions for _ in texts]

    def embed_query(self, text):
        return [0.1] * self._dimensions


@pytest.fixture
def stack(tmp_path):
    """Storage + repo + SQLite session wired into a handler, the way the worker wires it."""
    storage = LocalFileSystemStorage(base_dir=str(tmp_path))
    buckets = BucketManager(storage=storage)
    repo = InMemoryDocumentRepository()
    engine = create_engine(f"sqlite:///{tmp_path / 'embed.db'}")
    Base.metadata.create_all(bind=engine)
    session = Session(engine)

    def build(provider=None):
        return EmbeddingJobHandler(
            bucket_manager=buckets,
            repository=repo,
            embedding_service=EmbeddingService(provider=provider or FakeProvider(), batch_size=4),
            db_session=session,
        )

    try:
        yield type("Stack", (), {
            "storage": storage, "buckets": buckets, "repo": repo,
            "session": session, "build": staticmethod(build),
            "handler": build(),
        })
    finally:
        session.close()


def seed(stack, n_chunks=3, language="en", status=DocumentStatus.CHUNKED, write_artifact=True):
    """Creates a document in the state embedding expects: CHUNKED, chunks persisted, and the
    normalized artifact present so the language gate can read it."""
    doc_id = uuid.uuid4()

    if write_artifact:
        normalized = NormalizationResult(
            document_id=doc_id,
            mime_type=PDF_MIME,
            language=language,
            units=[NormalizedUnit(index=1, label="Page 1", text="Body text.")],
            headings=[Heading(text="1. Scope", level=1, unit_index=1)],
            quality=QualityCheckResult(passed=True),
        )
        stack.storage.put_object(
            stack.buckets.normalized,
            normalized_key_for(doc_id),
            normalized.model_dump_json().encode("utf-8"),
            content_type="application/json",
        )

    stack.repo.create(DocumentDTO(
        id=doc_id, filename="directive.pdf", size=2048, mime_type=PDF_MIME,
        status=status, checksum="e" * 64, raw_path=f"apag-raw/{'e' * 64}.pdf",
    ))
    for i in range(n_chunks):
        stack.session.add(ChunkORM(
            chunk_id=uuid.uuid4(), document_id=doc_id, scale="section",
            chunk_index=i, text=f"Passage {i} about district enforcement obligations.",
            char_count=48,
        ))
    stack.session.commit()
    return doc_id


# ==============================================================================
# Happy path
# ==============================================================================

def test_embedding_reaches_live_and_persists_vectors(stack):
    doc_id = seed(stack, n_chunks=3)

    outcome = stack.handler.process(doc_id)

    assert outcome.status == DocumentStatus.LIVE
    assert outcome.vector_count == 3
    assert stack.repo.get_by_id(doc_id).status == DocumentStatus.LIVE

    rows = stack.session.query(ChunkORM).filter(ChunkORM.document_id == doc_id).all()
    assert all(r.embedding is not None for r in rows)
    assert all(len(r.embedding) == DIM for r in rows)


def test_batching_covers_every_chunk(stack):
    """Batch size is 4 in this fixture, so 9 chunks exercises the partial final batch. A vector
    lost at a batch boundary would silently misalign every citation after it."""
    doc_id = seed(stack, n_chunks=9)

    outcome = stack.handler.process(doc_id)

    assert outcome.vector_count == 9
    rows = stack.session.query(ChunkORM).filter(ChunkORM.document_id == doc_id).all()
    assert all(r.embedding is not None for r in rows)


def test_embedding_writes_audit_event_naming_the_model(stack):
    """Which model produced a corpus matters the moment models are swapped — half a corpus
    embedded with each is unusable, and this row is how you tell them apart."""
    doc_id = seed(stack)

    stack.handler.process(doc_id)

    event = stack.session.query(AuditLog).filter(AuditLog.document_id == doc_id).one()
    assert event.event_type == AuditEventType.EMBEDDING_COMPLETED.value
    assert event.details["model"] == "fake/model"
    assert event.details["dimensions"] == DIM


# ==============================================================================
# The language gate
# ==============================================================================

def test_non_english_document_is_skipped_not_embedded(stack):
    """An English model turns Devanagari into unknown tokens and emits vectors that match nothing.
    Embedding anyway would leave the document in the index but invisible, with no signal that it
    is missing — a recorded gap can be found and fixed; a silent one cannot."""
    doc_id = seed(stack, language="hi")

    outcome = stack.handler.process(doc_id)

    assert outcome.status == DocumentStatus.SKIPPED_UNSUPPORTED_LANGUAGE
    assert outcome.failure_reason is None, "skipping is not a failure — it must not be retried"

    rows = stack.session.query(ChunkORM).filter(ChunkORM.document_id == doc_id).all()
    assert all(r.embedding is None for r in rows), "skipped documents must not be indexed"


def test_skipped_document_is_recorded_with_its_language(stack):
    """The skip has to be queryable: 'which documents are waiting on a multilingual model' must
    have an answer."""
    doc_id = seed(stack, language="hi")

    stack.handler.process(doc_id)

    event = stack.session.query(AuditLog).filter(AuditLog.document_id == doc_id).one()
    assert event.event_type == AuditEventType.EMBEDDING_SKIPPED.value
    assert event.details["language"] == "hi"


def test_unknown_language_is_skipped_rather_than_guessed(stack):
    """Refusing is the safe direction when the language can't be determined."""
    doc_id = seed(stack, write_artifact=False)

    assert stack.handler.process(doc_id).status == DocumentStatus.SKIPPED_UNSUPPORTED_LANGUAGE


def test_language_gate_can_be_disabled(stack, monkeypatch):
    """Switching to a multilingual model must not require code changes — only config."""
    from src.core import config
    monkeypatch.setattr(config.settings, "EMBEDDING_SKIP_NON_ENGLISH", False)
    doc_id = seed(stack, language="hi")

    assert stack.handler.process(doc_id).status == DocumentStatus.LIVE


# ==============================================================================
# Idempotency and failure modes
# ==============================================================================

@pytest.mark.parametrize(
    "status",
    [
        DocumentStatus.AWAITING_CLASSIFICATION,
        DocumentStatus.EXTRACTED,
        DocumentStatus.REJECTED,
        DocumentStatus.LIVE,
    ],
)
def test_documents_not_chunked_are_skipped(stack, status):
    doc_id = seed(stack, status=status)

    outcome = stack.handler.process(doc_id)

    assert outcome.status == status
    rows = stack.session.query(ChunkORM).filter(ChunkORM.document_id == doc_id).all()
    assert all(r.embedding is None for r in rows)


def test_inference_failure_is_transient(stack):
    """Model load and memory pressure are recoverable — the worker should retry rather than
    condemn the document."""
    doc_id = seed(stack)
    handler = stack.build(provider=FakeProvider(fail_with=RuntimeError("onnx session failed")))

    outcome = handler.process(doc_id)

    assert outcome.transient is True
    assert "EMBEDDING_ERROR" in outcome.failure_reason
    # Status untouched, so the retry still finds a chunked document.
    assert stack.repo.get_by_id(doc_id).status == DocumentStatus.CHUNKED


def test_dimension_mismatch_fails_permanently(stack):
    """A model whose width disagrees with the column is a misconfiguration — retrying with the
    same config produces the identical error, so looping on it wastes the queue."""
    doc_id = seed(stack)
    handler = stack.build(provider=FakeProvider(fail_with=ValueError("emits 1024-dim vectors")))

    outcome = handler.process(doc_id)

    assert outcome.transient is False
    assert "EMBEDDING_CONFIG_ERROR" in outcome.failure_reason
    assert stack.repo.get_by_id(doc_id).status == DocumentStatus.EMBEDDING_FAILED


def test_chunked_document_with_no_chunks_fails_permanently(stack):
    doc_id = seed(stack, n_chunks=0)

    outcome = stack.handler.process(doc_id)

    assert outcome.status == DocumentStatus.EMBEDDING_FAILED
    assert "NO_CHUNKS_FOUND" in outcome.failure_reason


def test_unknown_document_fails_permanently(stack):
    outcome = stack.handler.process(uuid.uuid4())

    assert outcome.status == DocumentStatus.EMBEDDING_FAILED
    assert "DOCUMENT_NOT_FOUND" in outcome.failure_reason
