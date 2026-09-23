"""Integration tests for database CHECK constraints under real PostgreSQL."""

import uuid

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from src.db.models import AuditLog as AuditORM
from src.db.models import Document as DocumentORM
from src.db.models import Job as JobORM


def test_postgres_rejects_invalid_document_status(db_session: Session):
    """Verifies Postgres rejects invalid document status not in chk_documents_status."""
    invalid_doc = DocumentORM(
        document_id=uuid.uuid4(),
        filename="invalid.pdf",
        file_size=1024,
        status="INVALID_STATUS_VALUE",
    )
    db_session.add(invalid_doc)
    with pytest.raises(IntegrityError):
        db_session.flush()

    db_session.rollback()


def test_postgres_rejects_invalid_job_stage(db_session: Session):
    """Verifies Postgres rejects invalid job stage not in chk_jobs_stage."""
    doc = DocumentORM(
        document_id=uuid.uuid4(),
        filename="valid.pdf",
        file_size=1024,
        status="UPLOADED",
    )
    db_session.add(doc)
    db_session.flush()

    invalid_job = JobORM(
        job_id=uuid.uuid4(),
        document_id=doc.document_id,
        stage="INVALID_STAGE",
        status="PENDING",
    )
    db_session.add(invalid_job)
    with pytest.raises(IntegrityError):
        db_session.flush()

    db_session.rollback()


def test_postgres_classification_nullable_and_constraints(db_session: Session):
    """Verifies classification accepts SQL NULL, PUBLIC, RESTRICTED, but rejects invalid strings."""
    # 1. SQL NULL is permitted
    doc_null = DocumentORM(
        document_id=uuid.uuid4(),
        filename="unclassified.pdf",
        file_size=1024,
        status="UPLOADED",
        classification=None,
    )
    db_session.add(doc_null)
    db_session.flush()
    assert doc_null.classification is None

    # 2. 'PUBLIC' and 'RESTRICTED' are permitted
    doc_pub = DocumentORM(
        document_id=uuid.uuid4(),
        filename="public.pdf",
        file_size=1024,
        status="UPLOADED",
        classification="PUBLIC",
    )
    doc_res = DocumentORM(
        document_id=uuid.uuid4(),
        filename="restricted.pdf",
        file_size=1024,
        status="UPLOADED",
        classification="RESTRICTED",
    )
    db_session.add_all([doc_pub, doc_res])
    db_session.flush()

    # 3. String 'NULL' or other invalid values are rejected by check constraint
    doc_bad = DocumentORM(
        document_id=uuid.uuid4(),
        filename="bad_class.pdf",
        file_size=1024,
        status="UPLOADED",
        classification="CONFIDENTIAL",  # Not in ('PUBLIC', 'RESTRICTED')
    )
    db_session.add(doc_bad)
    with pytest.raises(IntegrityError):
        db_session.flush()

    db_session.rollback()


def test_postgres_accepts_document_reclassified_audit_event(db_session: Session):
    """The CHECK constraint string is duplicated between src/db/models.py and the migration, and
    they drift silently — the ORM copy is what integration fixtures create tables from, the
    migration copy is what production actually has. An event type the database rejects means a
    tier change fails to be recorded, which is the one thing this audit row exists to prevent."""
    doc = DocumentORM(
        document_id=uuid.uuid4(),
        filename="reclassified.pdf",
        file_size=1024,
        status="AWAITING_CLASSIFICATION",
        classification="RESTRICTED",
    )
    db_session.add(doc)
    db_session.flush()

    event = AuditORM(
        document_id=doc.document_id,
        event_type="DOCUMENT_RECLASSIFIED",
        details={"old_tier": "PUBLIC", "new_tier": "RESTRICTED"},
    )
    db_session.add(event)
    db_session.flush()
    assert event.event_type == "DOCUMENT_RECLASSIFIED"

    db_session.rollback()


def test_postgres_stores_document_date_independently_of_created_at(db_session: Session):
    """document_date is the date on the document; created_at is when it was uploaded. If the
    column collapsed into a timestamp default, a 2019 policy would look current."""
    from datetime import date

    doc = DocumentORM(
        document_id=uuid.uuid4(),
        filename="old_policy.pdf",
        file_size=1024,
        status="AWAITING_CLASSIFICATION",
        document_date=date(2019, 3, 14),
    )
    db_session.add(doc)
    db_session.flush()

    assert doc.document_date == date(2019, 3, 14)
    assert doc.created_at.date() != doc.document_date

    db_session.rollback()



def test_postgres_accepts_chunking_statuses_and_stage(db_session: Session):
    """The CHECK strings are duplicated between src/db/models.py and the migration, and drift
    silently — the ORM copy is what integration fixtures build tables from, the migration copy is
    what production has. A rejected status means the chunking stage cannot record its own result."""
    doc = DocumentORM(
        document_id=uuid.uuid4(),
        filename="chunked.pdf",
        file_size=1024,
        status="CHUNKED",
    )
    db_session.add(doc)
    db_session.flush()
    assert doc.status == "CHUNKED"

    job = JobORM(
        job_id=uuid.uuid4(),
        document_id=doc.document_id,
        stage="CHUNK",
        status="PENDING",
    )
    db_session.add(job)
    db_session.flush()
    assert job.stage == "CHUNK"

    event = AuditORM(
        document_id=doc.document_id,
        event_type="CHUNKING_COMPLETED",
        details={"chunk_count": 12},
    )
    db_session.add(event)
    db_session.flush()
    assert event.event_type == "CHUNKING_COMPLETED"

    db_session.rollback()


def test_document_chunks_rejects_duplicate_position(db_session: Session):
    """Guards the retry path: re-running chunking must replace passages, never append them.
    Without this index a half-succeeded retry silently doubles a document's content."""
    from src.db.models import DocumentChunk as ChunkORM

    doc = DocumentORM(
        document_id=uuid.uuid4(),
        filename="dupe.pdf",
        file_size=1024,
        status="CHUNKED",
    )
    db_session.add(doc)
    db_session.flush()

    for _ in range(2):
        db_session.add(ChunkORM(
            chunk_id=uuid.uuid4(),
            document_id=doc.document_id,
            scale="section",
            chunk_index=0,
            text="Same position twice.",
            char_count=20,
        ))

    with pytest.raises(IntegrityError):
        db_session.flush()

    db_session.rollback()
