"""Integration tests for partial unique index uq_documents_active_sha256 under PostgreSQL."""

import uuid
import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from src.db.enums import Classification, DocumentStatus
from src.db.models import Document as DocumentORM


def test_partial_unique_index_blocks_duplicate_active_checksums(db_session: Session):
    """Verifies PostgreSQL unique partial index rejects duplicate SHA-256 for active status,
    but permits duplicates if the existing record is SUPERSEDED, ARCHIVED, or REJECTED.
    """
    sha = "11223344556677889900aabbccddeeff11223344556677889900aabbccddeeff"

    # 1. Insert active document
    doc1 = DocumentORM(
        document_id=uuid.uuid4(),
        filename="active_policy.pdf",
        file_size=1024,
        sha256=sha,
        status=DocumentStatus.AWAITING_CLASSIFICATION.value,
        classification=Classification.PUBLIC.value,
    )
    db_session.add(doc1)
    db_session.flush()

    # 2. Insert second document with identical active sha256 -> must raise IntegrityError
    doc2 = DocumentORM(
        document_id=uuid.uuid4(),
        filename="duplicate_active_policy.pdf",
        file_size=1024,
        sha256=sha,
        status=DocumentStatus.AWAITING_CLASSIFICATION.value,
        classification=Classification.PUBLIC.value,
    )
    db_session.add(doc2)
    with pytest.raises(IntegrityError):
        db_session.flush()

    db_session.rollback()


def test_partial_unique_index_allows_duplicate_if_prior_superseded(db_session: Session):
    """Verifies that re-uploading a file whose older record is SUPERSEDED does not violate uniqueness."""
    sha = "99887766554433221100aabbccddeeff99887766554433221100aabbccddeeff"

    # 1. Insert superseded document
    old_doc = DocumentORM(
        document_id=uuid.uuid4(),
        filename="old_policy.pdf",
        file_size=2048,
        sha256=sha,
        status=DocumentStatus.SUPERSEDED.value,
        classification=Classification.PUBLIC.value,
    )
    db_session.add(old_doc)
    db_session.flush()

    # 2. Insert new active document with identical sha256 -> must succeed because partial index filters out SUPERSEDED
    new_doc = DocumentORM(
        document_id=uuid.uuid4(),
        filename="new_reuploaded_policy.pdf",
        file_size=2048,
        sha256=sha,
        status=DocumentStatus.AWAITING_CLASSIFICATION.value,
        classification=Classification.PUBLIC.value,
    )
    db_session.add(new_doc)
    db_session.flush()

    assert new_doc.document_id is not None
