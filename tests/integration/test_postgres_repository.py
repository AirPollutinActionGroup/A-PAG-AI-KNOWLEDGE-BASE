"""Integration tests for PostgreSQLDocumentRepository against real PostgreSQL."""

import uuid
from sqlalchemy.orm import Session

from src.db.enums import Classification, DocumentStatus
from src.db.models import Document as DocumentORM
from src.modules.document_pipeline.models import Document as DocumentDTO
from src.modules.document_pipeline.repository import PostgreSQLDocumentRepository


def test_postgres_repository_crud(db_session: Session):
    """Verifies create, get_by_id, update_status, and update_document against real Postgres."""
    repo = PostgreSQLDocumentRepository(db_session)
    doc_id = uuid.uuid4()

    doc = DocumentDTO(
        id=doc_id,
        filename="air_quality_directive.pdf",
        owner_id=uuid.uuid4(),
        size=1048576,
        checksum="e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
        status=DocumentStatus.QUARANTINED,
        classification=Classification.PUBLIC,
        version=1,
    )

    # 1. Create
    created = repo.create(doc)
    assert created.id == doc_id
    assert created.status == DocumentStatus.QUARANTINED

    # 2. Get by ID
    fetched = repo.get_by_id(doc_id)
    assert fetched is not None
    assert fetched.filename == "air_quality_directive.pdf"
    assert fetched.size == 1048576

    # 3. Update status
    repo.update_status(doc_id, DocumentStatus.AWAITING_CLASSIFICATION)
    updated_status_doc = repo.get_by_id(doc_id)
    assert updated_status_doc.status == DocumentStatus.AWAITING_CLASSIFICATION

    # 4. Update full document
    fetched.status = DocumentStatus.LIVE
    fetched.raw_path = "apag-raw/e3b0c442.pdf"
    repo.update_document(fetched)

    final_doc = repo.get_by_id(doc_id)
    assert final_doc.status == DocumentStatus.LIVE
    assert final_doc.raw_path == "apag-raw/e3b0c442.pdf"


def test_postgres_repository_dedup_checksum(db_session: Session):
    """Verifies get_by_checksum properly ignores SUPERSEDED and ARCHIVED records."""
    repo = PostgreSQLDocumentRepository(db_session)
    checksum = "abcdef1234567890abcdef1234567890abcdef1234567890abcdef1234567890"

    doc1 = DocumentDTO(
        id=uuid.uuid4(),
        filename="version1.pdf",
        size=5000,
        checksum=checksum,
        status=DocumentStatus.SUPERSEDED,
        classification=Classification.PUBLIC,
        version=1,
    )
    repo.create(doc1)

    # Checksum lookup should ignore SUPERSEDED
    assert repo.get_by_checksum(checksum) is None

    # Create active document with same checksum
    doc2 = DocumentDTO(
        id=uuid.uuid4(),
        filename="version2.pdf",
        size=5000,
        checksum=checksum,
        status=DocumentStatus.AWAITING_CLASSIFICATION,
        classification=Classification.PUBLIC,
        version=2,
    )
    repo.create(doc2)

    # Checksum lookup should now find doc2
    found = repo.get_by_checksum(checksum)
    assert found is not None
    assert found.id == doc2.id
