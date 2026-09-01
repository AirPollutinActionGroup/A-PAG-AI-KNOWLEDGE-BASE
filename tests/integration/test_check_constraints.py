"""Integration tests for database CHECK constraints under real PostgreSQL."""

import uuid
import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from src.db.models import Document as DocumentORM, Job as JobORM, AuditLog as AuditORM


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

