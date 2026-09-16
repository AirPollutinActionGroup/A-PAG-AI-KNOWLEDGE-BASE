"""Normalization job handler tests — status transitions, the quality gate's effect on the
pipeline, and idempotency under retries."""

import uuid

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from src.db.enums import AuditEventType
from src.db.models import AuditLog, Base
from src.db.models import Job as JobORM
from src.modules.document_pipeline.extraction.models import (
    ExtractedTable,
    ExtractedUnit,
    ExtractionResult,
    Heading,
)
from src.modules.document_pipeline.formats import PDF_MIME
from src.modules.document_pipeline.models import Document as DocumentDTO
from src.modules.document_pipeline.models import DocumentStatus
from src.modules.document_pipeline.normalization_job_handler import (
    NormalizationJobHandler,
)
from src.modules.document_pipeline.repository import InMemoryDocumentRepository
from src.modules.document_pipeline.storage_keys import (
    extraction_key_for,
    normalized_key_for,
)
from src.storage.bucket_manager import BucketManager
from src.storage.object_storage import LocalFileSystemStorage

BODY = "This directive sets binding obligations on district authorities in the region. " * 3


def make_stack(tmp_path, db=None):
    storage = LocalFileSystemStorage(base_dir=str(tmp_path))
    buckets = BucketManager(storage=storage)
    repo = InMemoryDocumentRepository()
    handler = NormalizationJobHandler(bucket_manager=buckets, repository=repo, db_session=db)
    return storage, buckets, repo, handler


def seed_extracted_doc(repo, buckets, storage, units, mime_type=PDF_MIME, tables=None):
    """Creates a document in the state normalization expects: EXTRACTED, with its artifact
    written to extracted/."""
    doc_id = uuid.uuid4()
    extraction = ExtractionResult(
        document_id=doc_id,
        mime_type=mime_type,
        units=[ExtractedUnit(index=i, label=label, text=text) for i, label, text in units],
        headings=[Heading(text="Section  A", level=1, unit_index=1)],
        tables=tables or [ExtractedTable(unit_index=1, rows=[["District", "Target"]])],
    )
    storage.put_object(
        buckets.extracted,
        extraction_key_for(doc_id),
        extraction.model_dump_json().encode("utf-8"),
        content_type="application/json",
    )
    doc = repo.create(DocumentDTO(
        id=doc_id,
        filename="directive.pdf",
        size=1024,
        mime_type=mime_type,
        status=DocumentStatus.EXTRACTED,
        checksum="b" * 64,
        raw_path=f"{buckets.raw}/{'b' * 64}.pdf",
    ))
    return doc


# ==============================================================================
# Happy path
# ==============================================================================

def test_normalization_reaches_awaiting_classification(tmp_path):
    """AWAITING_CLASSIFICATION now means what it says: normalization is done and classification
    is the next thing this document needs."""
    storage, buckets, repo, handler = make_stack(tmp_path)
    doc = seed_extracted_doc(repo, buckets, storage, [(1, "Page 1", BODY)])

    outcome = handler.process(doc.id)

    assert outcome.status == DocumentStatus.AWAITING_CLASSIFICATION
    assert repo.get_by_id(doc.id).status == DocumentStatus.AWAITING_CLASSIFICATION
    assert storage.object_exists(buckets.normalized, normalized_key_for(doc.id))


def test_normalized_artifact_carries_cleaned_text_and_structure(tmp_path):
    storage, buckets, repo, handler = make_stack(tmp_path)
    doc = seed_extracted_doc(
        repo, buckets, storage,
        [(1, "Page 1", f"Air  Quality   Directive\n\n\n\n{BODY}")],
    )

    handler.process(doc.id)
    result = handler.load_result(doc.id)

    assert "Air Quality Directive" in result.full_text
    assert "\n\n\n" not in result.full_text
    assert result.headings[0].text == "Section A"
    assert result.tables[0].rows == [["District", "Target"]]
    assert result.language == "en"
    assert result.quality.passed is True


def test_normalization_is_the_last_stage_and_queues_nothing(tmp_path):
    """Classification isn't built yet — the pipeline deliberately stops here rather than
    enqueuing a stage no worker consumes."""
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)

    with Session(engine) as db:
        storage, buckets, repo, handler = make_stack(tmp_path, db=db)
        doc = seed_extracted_doc(repo, buckets, storage, [(1, "Page 1", BODY)])

        handler.process(doc.id)

        assert db.query(JobORM).filter(JobORM.document_id == doc.id).count() == 0


def test_normalization_writes_audit_event(tmp_path):
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)

    with Session(engine) as db:
        storage, buckets, repo, handler = make_stack(tmp_path, db=db)
        doc = seed_extracted_doc(repo, buckets, storage, [(1, "Page 1", BODY)])

        handler.process(doc.id)

        events = db.query(AuditLog).filter(AuditLog.document_id == doc.id).all()
        assert [e.event_type for e in events] == [AuditEventType.NORMALIZATION_COMPLETED.value]
        assert events[0].details["language"] == "en"


# ==============================================================================
# The quality gate's effect on the pipeline
# ==============================================================================

def test_scanned_pdf_is_stopped_at_normalization_failed(tmp_path):
    """The end-to-end consequence of having no OCR: a scan doesn't quietly become an empty
    document in the knowledge base, it stops with a reason naming the check that caught it."""
    storage, buckets, repo, handler = make_stack(tmp_path)
    doc = seed_extracted_doc(
        repo, buckets, storage, [(1, "Page 1", "Annexure"), (2, "Page 2", "")]
    )

    outcome = handler.process(doc.id)

    assert outcome.status == DocumentStatus.NORMALIZATION_FAILED
    assert "LOW_TEXT_DENSITY" in outcome.failure_reason
    assert repo.get_by_id(doc.id).rejection_reason.startswith("QUALITY_CHECK_FAILED")
    # Nothing was written — failing content must not reach the normalized bucket.
    assert not storage.object_exists(buckets.normalized, normalized_key_for(doc.id))


def test_quality_failure_is_permanent_not_retried(tmp_path):
    """Retrying produces the identical verdict, so the worker must not schedule one."""
    storage, buckets, repo, handler = make_stack(tmp_path)
    doc = seed_extracted_doc(repo, buckets, storage, [(1, "Page 1", "")])

    outcome = handler.process(doc.id)

    assert outcome.transient is False
    assert "EMPTY_TEXT" in outcome.failure_reason


def test_quality_failure_writes_audit_event_with_the_numbers(tmp_path):
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)

    with Session(engine) as db:
        storage, buckets, repo, handler = make_stack(tmp_path, db=db)
        doc = seed_extracted_doc(repo, buckets, storage, [(1, "Page 1", "x")])

        handler.process(doc.id)

        event = db.query(AuditLog).filter(AuditLog.document_id == doc.id).one()
        assert event.event_type == AuditEventType.NORMALIZATION_FAILED.value
        assert "LOW_TEXT_DENSITY" in event.details["failures"]
        assert event.details["char_count"] == 1


# ==============================================================================
# Idempotency and failure modes
# ==============================================================================

def test_rerunning_a_normalized_document_is_a_noop(tmp_path):
    storage, buckets, repo, handler = make_stack(tmp_path)
    doc = seed_extracted_doc(repo, buckets, storage, [(1, "Page 1", BODY)])

    handler.process(doc.id)
    second = handler.process(doc.id)

    assert second.status == DocumentStatus.AWAITING_CLASSIFICATION
    assert second.failure_reason is None


@pytest.mark.parametrize(
    "status",
    [DocumentStatus.VALIDATED, DocumentStatus.REJECTED, DocumentStatus.NORMALIZATION_FAILED],
)
def test_documents_not_awaiting_normalization_are_skipped(tmp_path, status):
    storage, buckets, repo, handler = make_stack(tmp_path)
    doc = seed_extracted_doc(repo, buckets, storage, [(1, "Page 1", BODY)])
    doc.status = status
    repo.update_document(doc)

    outcome = handler.process(doc.id)

    assert outcome.status == status
    assert not storage.object_exists(buckets.normalized, normalized_key_for(doc.id))


def test_missing_extraction_artifact_is_transient(tmp_path):
    """Extraction wrote the artifact before setting EXTRACTED, so if it can't be read now that's
    a storage problem a retry can get past — not a reason to fail the document."""
    storage, buckets, repo, handler = make_stack(tmp_path)
    doc = seed_extracted_doc(repo, buckets, storage, [(1, "Page 1", BODY)])
    storage.delete_object(buckets.extracted, extraction_key_for(doc.id))

    outcome = handler.process(doc.id)

    assert outcome.transient is True
    assert "STORAGE_ERROR" in outcome.failure_reason
    assert repo.get_by_id(doc.id).status == DocumentStatus.EXTRACTED


def test_unknown_document_fails_permanently(tmp_path):
    _, _, _, handler = make_stack(tmp_path)

    outcome = handler.process(uuid.uuid4())

    assert outcome.status == DocumentStatus.NORMALIZATION_FAILED
    assert "DOCUMENT_NOT_FOUND" in outcome.failure_reason
