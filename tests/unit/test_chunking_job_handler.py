"""Chunking job handler tests — the stage's wiring into the pipeline.

Covers what the chunker's own tests can't: status transitions, persistence to document_chunks,
idempotency under retries, and the transient-vs-permanent split the worker depends on to decide
whether to retry.
"""

import uuid

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from src.db.enums import AuditEventType
from src.db.models import AuditLog, Base
from src.db.models import DocumentChunk as ChunkORM
from src.modules.document_pipeline.chunking_job_handler import ChunkingJobHandler
from src.modules.document_pipeline.extraction.models import ExtractedTable, Heading
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

BODY = "This directive binds district authorities across the region. " * 4


@pytest.fixture
def stack(tmp_path):
    """Storage + repo + SQLite session wired into a handler, the way the worker wires it."""
    storage = LocalFileSystemStorage(base_dir=str(tmp_path))
    buckets = BucketManager(storage=storage)
    repo = InMemoryDocumentRepository()
    engine = create_engine(f"sqlite:///{tmp_path / 'chunks.db'}")
    Base.metadata.create_all(bind=engine)
    session = Session(engine)
    handler = ChunkingJobHandler(
        bucket_manager=buckets, repository=repo, db_session=session
    )
    try:
        yield type("Stack", (), {
            "storage": storage, "buckets": buckets, "repo": repo,
            "session": session, "handler": handler, "engine": engine,
        })
    finally:
        session.close()


def seed(stack, units=None, headings=None, tables=None,
         status=DocumentStatus.AWAITING_CLASSIFICATION):
    """Creates a document in the state chunking expects, with its normalized artifact written."""
    doc_id = uuid.uuid4()
    normalized = NormalizationResult(
        document_id=doc_id,
        mime_type=PDF_MIME,
        language="en",
        units=[
            NormalizedUnit(index=i, label=lbl, text=txt)
            for i, lbl, txt in (units or [(1, "Page 1", BODY)])
        ],
        headings=headings or [],
        tables=tables or [],
        quality=QualityCheckResult(passed=True),
    )
    stack.storage.put_object(
        stack.buckets.normalized,
        normalized_key_for(doc_id),
        normalized.model_dump_json().encode("utf-8"),
        content_type="application/json",
    )
    stack.repo.create(DocumentDTO(
        id=doc_id,
        filename="directive.pdf",
        size=4096,
        mime_type=PDF_MIME,
        status=status,
        checksum="d" * 64,
        raw_path=f"apag-raw/{'d' * 64}.pdf",
    ))
    return doc_id


# ==============================================================================
# Happy path
# ==============================================================================

def test_chunking_reaches_chunked_and_persists_passages(stack):
    doc_id = seed(stack)

    outcome = stack.handler.process(doc_id)

    assert outcome.status == DocumentStatus.CHUNKED
    assert outcome.chunk_count > 0
    assert stack.repo.get_by_id(doc_id).status == DocumentStatus.CHUNKED

    rows = stack.session.query(ChunkORM).filter(ChunkORM.document_id == doc_id).all()
    assert len(rows) == outcome.chunk_count


def test_persisted_chunks_carry_citation_metadata(stack):
    """page_number, section_heading and is_table are the whole reason chunks live in a table
    rather than a JSON blob — a passage that cannot say where it came from is unusable."""
    doc_id = seed(
        stack,
        units=[(3, "Page 3", "4. Enforcement\nThe authority shall act without delay.")],
        headings=[Heading(text="4. Enforcement", level=1, unit_index=3)],
        tables=[ExtractedTable(unit_index=3, rows=[["District", "Target"], ["Patna", "40%"]])],
    )

    stack.handler.process(doc_id)
    rows = stack.handler.load_chunks(doc_id)

    assert all(r.page_number == 3 for r in rows)
    assert all(r.section_heading == "4. Enforcement" for r in rows)
    assert any(r.is_table for r in rows)
    assert all(r.char_count == len(r.text) for r in rows)


def test_chunk_indices_are_stored_in_document_order(stack):
    doc_id = seed(stack, units=[(1, "Page 1", BODY), (2, "Page 2", BODY)])

    stack.handler.process(doc_id)
    rows = stack.handler.load_chunks(doc_id)

    assert [r.chunk_index for r in rows] == list(range(len(rows)))


def test_chunking_writes_audit_event(stack):
    doc_id = seed(stack)

    stack.handler.process(doc_id)

    event = stack.session.query(AuditLog).filter(AuditLog.document_id == doc_id).one()
    assert event.event_type == AuditEventType.CHUNKING_COMPLETED.value
    assert event.details["chunk_count"] > 0


# ==============================================================================
# Idempotency — jobs get retried and reaped
# ==============================================================================

def test_rerunning_replaces_rather_than_duplicates_chunks(stack):
    """A retry that appended would double every passage, and the unique index would reject it.
    Replacing also means re-chunking after a rule change leaves nothing from the old rules."""
    doc_id = seed(stack)
    first = stack.handler.process(doc_id)

    # Put it back so the guard lets the second run through, as a reaped job would.
    doc = stack.repo.get_by_id(doc_id)
    doc.status = DocumentStatus.AWAITING_CLASSIFICATION
    stack.repo.update_document(doc)

    second = stack.handler.process(doc_id)

    assert second.chunk_count == first.chunk_count
    rows = stack.session.query(ChunkORM).filter(ChunkORM.document_id == doc_id).all()
    assert len(rows) == first.chunk_count


@pytest.mark.parametrize(
    "status",
    [
        DocumentStatus.QUARANTINED,
        DocumentStatus.EXTRACTED,
        DocumentStatus.REJECTED,
        DocumentStatus.CHUNKED,
        DocumentStatus.NORMALIZATION_FAILED,
    ],
)
def test_documents_not_awaiting_chunking_are_skipped(stack, status):
    doc_id = seed(stack, status=status)

    outcome = stack.handler.process(doc_id)

    assert outcome.status == status
    assert stack.session.query(ChunkORM).filter(ChunkORM.document_id == doc_id).count() == 0


# ==============================================================================
# Failure modes — the worker's retry decision depends on this split
# ==============================================================================

def test_missing_normalized_artifact_is_transient(stack):
    """Normalization wrote the artifact before setting the status, so a read failure is a storage
    blip the retry can get past — not a reason to fail the document."""
    doc_id = seed(stack)
    stack.storage.delete_object(stack.buckets.normalized, normalized_key_for(doc_id))

    outcome = stack.handler.process(doc_id)

    assert outcome.transient is True
    assert "STORAGE_ERROR" in outcome.failure_reason
    # Status untouched, so the retry still finds a chunkable document.
    assert stack.repo.get_by_id(doc_id).status == DocumentStatus.AWAITING_CLASSIFICATION


def test_document_yielding_no_chunks_fails_permanently(stack):
    """The quality gate already rejects empty documents, so reaching here with nothing means this
    stage disagreed with content the gate passed — a bug worth failing loudly for, rather than
    marking a document indexed with no passages to retrieve."""
    doc_id = seed(stack, units=[(1, "Page 1", "   ")])

    outcome = stack.handler.process(doc_id)

    assert outcome.status == DocumentStatus.CHUNKING_FAILED
    assert outcome.transient is False
    assert "NO_CHUNKS_PRODUCED" in outcome.failure_reason
    assert stack.repo.get_by_id(doc_id).rejection_reason.startswith("NO_CHUNKS_PRODUCED")


def test_unknown_document_fails_permanently(stack):
    outcome = stack.handler.process(uuid.uuid4())

    assert outcome.status == DocumentStatus.CHUNKING_FAILED
    assert "DOCUMENT_NOT_FOUND" in outcome.failure_reason
