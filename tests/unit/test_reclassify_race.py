"""A tier change must survive a pipeline stage finishing with an older copy of the document.

The Postgres version of this test is in `tests/integration/test_postgres_repository.py`; this
one keeps the in-memory repository honest, since every unit test that touches a tier runs on it.
"""

import uuid

from src.db.enums import Classification, DocumentStatus
from src.modules.document_pipeline.models import Document as DocumentDTO
from src.modules.document_pipeline.repository import InMemoryDocumentRepository


def _repo_with_doc(tier: Classification):
    repo = InMemoryDocumentRepository()
    doc_id = uuid.uuid4()
    repo.create(DocumentDTO(
        id=doc_id, filename="board_note.pdf", owner_id=None, size=10,
        status=DocumentStatus.VALIDATED, classification=tier, version=1,
    ))
    return repo, doc_id


def test_a_stage_finishing_does_not_undo_a_reclassification():
    repo, doc_id = _repo_with_doc(Classification.PUBLIC)

    in_flight = repo.get_by_id(doc_id)
    repo.set_classification(doc_id, Classification.RESTRICTED)
    in_flight.status = DocumentStatus.EXTRACTED
    repo.update_document(in_flight)

    final = repo.get_by_id(doc_id)
    assert final.status == DocumentStatus.EXTRACTED
    assert final.classification == Classification.RESTRICTED


def test_update_document_cannot_change_a_tier():
    """Not even deliberately: the one way to change a tier is the one that is audited."""
    repo, doc_id = _repo_with_doc(Classification.RESTRICTED)

    doc = repo.get_by_id(doc_id)
    doc.classification = Classification.PUBLIC
    repo.update_document(doc)

    assert repo.get_by_id(doc_id).classification == Classification.RESTRICTED


def test_set_classification_on_a_missing_document_returns_none():
    assert InMemoryDocumentRepository().set_classification(
        uuid.uuid4(), Classification.RESTRICTED
    ) is None
