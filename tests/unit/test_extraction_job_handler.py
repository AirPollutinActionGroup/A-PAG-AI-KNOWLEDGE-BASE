"""Extraction job handler tests — the stage's wiring into the pipeline.

Covers what the extractors' own tests can't: status transitions, the handoff to NORMALIZE,
idempotency under retries, and the transient-vs-permanent failure split the worker depends on to
decide whether to retry.
"""

import uuid

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from src.db.enums import AuditEventType, JobStage, JobStatus
from src.db.models import AuditLog, Base
from src.db.models import Job as JobORM
from src.modules.document_pipeline.extraction.models import ExtractionResult
from src.modules.document_pipeline.extraction_job_handler import ExtractionJobHandler
from src.modules.document_pipeline.models import Document as DocumentDTO
from src.modules.document_pipeline.models import DocumentStatus
from src.modules.document_pipeline.repository import InMemoryDocumentRepository
from src.modules.document_pipeline.storage_keys import extraction_key_for
from src.storage.bucket_manager import BucketManager
from src.storage.object_storage import LocalFileSystemStorage
from tests.unit.test_extraction import build_docx, build_pdf

DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


def make_stack(tmp_path, db=None):
    storage = LocalFileSystemStorage(base_dir=str(tmp_path))
    buckets = BucketManager(storage=storage)
    repo = InMemoryDocumentRepository()
    handler = ExtractionJobHandler(bucket_manager=buckets, repository=repo, db_session=db)
    return storage, buckets, repo, handler


def seed_promoted_doc(repo, buckets, storage, data: bytes, mime_type: str) -> DocumentDTO:
    """Creates a document in the state extraction expects: VALIDATED, with bytes in raw/."""
    doc_id = uuid.uuid4()
    raw_key = f"{'a' * 64}{'.docx' if 'word' in mime_type else '.pdf'}"
    storage.put_object(buckets.raw, raw_key, data, content_type=mime_type)
    return repo.create(DocumentDTO(
        id=doc_id,
        filename="policy.docx" if "word" in mime_type else "policy.pdf",
        size=len(data),
        mime_type=mime_type,
        status=DocumentStatus.VALIDATED,
        checksum="a" * 64,
        raw_path=f"{buckets.raw}/{raw_key}",
    ))


# ==============================================================================
# Happy path
# ==============================================================================

def test_extraction_promotes_status_and_writes_artifact(tmp_path):
    storage, buckets, repo, handler = make_stack(tmp_path)
    doc = seed_promoted_doc(repo, buckets, storage, build_docx(), DOCX_MIME)

    outcome = handler.process(doc.id)

    assert outcome.status == DocumentStatus.EXTRACTED
    assert repo.get_by_id(doc.id).status == DocumentStatus.EXTRACTED

    key = extraction_key_for(doc.id)
    assert storage.object_exists(buckets.extracted, key)
    stored = ExtractionResult.model_validate_json(
        storage.get_object(buckets.extracted, key).decode("utf-8")
    )
    assert stored.document_id == doc.id
    assert "stubble management" in stored.full_text
    assert stored.tables and stored.headings


def test_raw_object_is_left_in_place(tmp_path):
    """raw/ is the system of record — extraction reads from it and must never consume it."""
    storage, buckets, repo, handler = make_stack(tmp_path)
    doc = seed_promoted_doc(repo, buckets, storage, build_docx(), DOCX_MIME)

    handler.process(doc.id)

    assert storage.object_exists(buckets.raw, doc.raw_path.rsplit("/", 1)[-1])


def test_extraction_enqueues_normalize_job(tmp_path):
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)

    with Session(engine) as db:
        storage, buckets, repo, handler = make_stack(tmp_path, db=db)
        doc = seed_promoted_doc(repo, buckets, storage, build_docx(), DOCX_MIME)

        handler.process(doc.id)

        jobs = db.query(JobORM).filter(JobORM.document_id == doc.id).all()
        assert [j.stage for j in jobs] == [JobStage.NORMALIZE.value]
        assert jobs[0].status == JobStatus.PENDING.value


def test_extraction_writes_audit_event(tmp_path):
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)

    with Session(engine) as db:
        storage, buckets, repo, handler = make_stack(tmp_path, db=db)
        doc = seed_promoted_doc(repo, buckets, storage, build_docx(), DOCX_MIME)

        handler.process(doc.id)

        events = db.query(AuditLog).filter(AuditLog.document_id == doc.id).all()
        assert [e.event_type for e in events] == [AuditEventType.EXTRACTION_COMPLETED.value]
        assert events[0].details["char_count"] > 0
        assert events[0].details["extraction_method"] == "NATIVE"


# ==============================================================================
# Idempotency — jobs get retried and reaped
# ==============================================================================

def test_rerunning_an_already_extracted_document_is_a_noop(tmp_path):
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)

    with Session(engine) as db:
        storage, buckets, repo, handler = make_stack(tmp_path, db=db)
        doc = seed_promoted_doc(repo, buckets, storage, build_docx(), DOCX_MIME)

        handler.process(doc.id)
        second = handler.process(doc.id)

        assert second.status == DocumentStatus.EXTRACTED
        assert second.failure_reason is None
        # A second NORMALIZE job would mean the document gets normalized twice.
        assert db.query(JobORM).filter(JobORM.document_id == doc.id).count() == 1


@pytest.mark.parametrize(
    "status",
    [DocumentStatus.QUARANTINED, DocumentStatus.REJECTED, DocumentStatus.AWAITING_CLASSIFICATION],
)
def test_documents_not_awaiting_extraction_are_skipped(tmp_path, status):
    storage, buckets, repo, handler = make_stack(tmp_path)
    doc = seed_promoted_doc(repo, buckets, storage, build_docx(), DOCX_MIME)
    doc.status = status
    repo.update_document(doc)

    outcome = handler.process(doc.id)

    assert outcome.status == status
    assert not storage.object_exists(buckets.extracted, extraction_key_for(doc.id))


# ==============================================================================
# Failure modes — the worker's retry decision depends on this split
# ==============================================================================

def test_unreadable_file_fails_permanently(tmp_path):
    """A file that can't be parsed fails identically on every retry, so it must not be retried."""
    storage, buckets, repo, handler = make_stack(tmp_path)
    doc = seed_promoted_doc(
        repo, buckets, storage, b"%PDF-1.4\nnot actually a pdf", "application/pdf"
    )

    outcome = handler.process(doc.id)

    assert outcome.status == DocumentStatus.EXTRACTION_FAILED
    assert outcome.transient is False
    assert "EXTRACTION_FAILED" in outcome.failure_reason
    assert repo.get_by_id(doc.id).status == DocumentStatus.EXTRACTION_FAILED


def test_missing_raw_object_is_transient(tmp_path):
    """The raw object is still recorded — a read failure is a storage blip, and the retry the
    worker schedules can succeed."""
    storage, buckets, repo, handler = make_stack(tmp_path)
    doc = seed_promoted_doc(repo, buckets, storage, build_docx(), DOCX_MIME)
    storage.delete_object(buckets.raw, doc.raw_path.rsplit("/", 1)[-1])

    outcome = handler.process(doc.id)

    assert outcome.transient is True
    assert "STORAGE_ERROR" in outcome.failure_reason
    # Status is left alone so the retry still finds a VALIDATED document to work on.
    assert repo.get_by_id(doc.id).status == DocumentStatus.VALIDATED


def test_document_without_raw_path_fails_permanently(tmp_path):
    storage, buckets, repo, handler = make_stack(tmp_path)
    doc = seed_promoted_doc(repo, buckets, storage, build_docx(), DOCX_MIME)
    doc.raw_path = None
    repo.update_document(doc)

    outcome = handler.process(doc.id)

    assert outcome.status == DocumentStatus.EXTRACTION_FAILED
    assert "MISSING_RAW_OBJECT" in outcome.failure_reason


def test_unknown_document_fails_permanently(tmp_path):
    _, _, _, handler = make_stack(tmp_path)

    outcome = handler.process(uuid.uuid4())

    assert outcome.status == DocumentStatus.EXTRACTION_FAILED
    assert "DOCUMENT_NOT_FOUND" in outcome.failure_reason


def test_failed_extraction_does_not_queue_normalization(tmp_path):
    """The pipeline stops at a failure rather than normalizing content that was never read."""
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)

    with Session(engine) as db:
        storage, buckets, repo, handler = make_stack(tmp_path, db=db)
        doc = seed_promoted_doc(repo, buckets, storage, b"%PDF-1.4\nbroken", "application/pdf")

        handler.process(doc.id)

        assert db.query(JobORM).filter(JobORM.document_id == doc.id).count() == 0


# ==============================================================================
# Reading the artifact back — the handoff the normalization stage depends on
# ==============================================================================

def test_stored_artifact_can_be_loaded_back(tmp_path):
    storage, buckets, repo, handler = make_stack(tmp_path)
    pdf = build_pdf([[("Heading", 20, 72, 720), ("body text here", 10, 72, 690)]])
    doc = seed_promoted_doc(repo, buckets, storage, pdf, "application/pdf")

    handler.process(doc.id)
    loaded = handler.load_result(doc.id)

    assert loaded.document_id == doc.id
    assert "body text here" in loaded.full_text
    assert loaded.headings[0].text == "Heading"
