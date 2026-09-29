"""`GET /documents/{id}/chunks` — reading a document's passages in the UI.

This endpoint exists because a browser can only render one of the four supported formats. A
.docx, .xlsx or .pptx is a zip archive, so for three formats out of four the passages *are* the
preview — and they are the more truthful one either way, since they show what the search index
actually holds rather than what the file looks like.

The security test is the one that matters. Passage text is the document's content, so an
endpoint that returns chunks without re-checking the parent document's classification leaks
exactly what RESTRICTED exists to protect — and it would do so through a path no retrieval test
covers, because retrieval filters in SQL while this reads by document id.

Self-contained, in the style of test_reclassification.py: it installs its own overrides and
restores whatever was there, so it runs in either order alongside test_ingestion_pipeline.py.
"""

import uuid
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from starlette.testclient import TestClient

from src.api.v1.ingestion import get_document_repository
from src.api.v1.router import app
from src.db.engine import get_db
from src.db.enums import UserRole
from src.db.models import Base, DocumentChunk
from src.modules.auth.dependencies import get_current_user
from src.modules.document_pipeline.formats import PDF_MIME
from src.modules.document_pipeline.models import Classification, DocumentStatus
from src.modules.document_pipeline.models import Document as DocumentDTO
from src.modules.document_pipeline.repository import InMemoryDocumentRepository

client = TestClient(app)


class _FakeUser:
    def __init__(self, role: str = UserRole.USER.value, user_id: uuid.UUID | None = None):
        self.user_id = user_id or uuid.uuid4()
        self.role = role
        self.is_active = True
        self.email = f"{self.user_id}@a-pag.org"


@pytest.fixture
def stack(tmp_path):
    repo = InMemoryDocumentRepository()
    user = _FakeUser()
    engine = create_engine(f"sqlite:///{tmp_path / 'chunks.db'}")
    Base.metadata.create_all(bind=engine)

    def _get_db():
        with Session(engine) as session:
            yield session

    previous = {
        dep: app.dependency_overrides.get(dep)
        for dep in (get_document_repository, get_current_user, get_db)
    }
    app.dependency_overrides[get_document_repository] = lambda: repo
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_db] = _get_db
    try:
        yield SimpleNamespace(repo=repo, engine=engine, user=user)
    finally:
        for dep, override in previous.items():
            if override is None:
                app.dependency_overrides.pop(dep, None)
            else:
                app.dependency_overrides[dep] = override


def seed_doc(stack, classification=Classification.PUBLIC, owner_id=None, **kwargs):
    return stack.repo.create(
        DocumentDTO(
            id=uuid.uuid4(),
            filename="policy/directive.pdf",
            size=2048,
            mime_type=PDF_MIME,
            status=kwargs.pop("status", DocumentStatus.LIVE),
            classification=classification,
            owner_id=owner_id if owner_id is not None else stack.user.user_id,
            checksum="c" * 64,
            raw_path=f"apag-raw/{'c' * 64}.pdf",
            **kwargs,
        )
    )


def seed_chunks(stack, document_id, specs):
    """`specs` is a list of dicts overriding the defaults for one chunk."""
    with Session(stack.engine) as session:
        for i, spec in enumerate(specs):
            body = {
                "chunk_index": i,
                "text": f"Passage {i}.",
                "page_number": i + 1,
                "section_heading": f"{i + 1}. Heading",
                "is_table": False,
                "char_count": 11,
                "embedding": None,
                **spec,
            }
            session.add(DocumentChunk(document_id=document_id, scale="section", **body))
        session.commit()


def get_chunks(doc_id, **params):
    return client.get(f"/api/v1/documents/{doc_id}/chunks", params=params)


# ==============================================================================
# Permission — a chunk carries the document's content, so it carries its tier
# ==============================================================================

def test_another_users_restricted_passages_are_not_served(stack):
    """The leak this endpoint could introduce. Retrieval filters in SQL and would never return
    these; reading by document id bypasses that entirely unless the check is repeated here."""
    doc = seed_doc(stack, classification=Classification.RESTRICTED, owner_id=uuid.uuid4())
    seed_chunks(stack, doc.id, [{"text": "Draft position, not for circulation."}])

    resp = get_chunks(doc.id)

    assert resp.status_code == 404, "a restricted document must not even confirm it exists"
    assert "not for circulation" not in resp.text


def test_the_owner_can_read_their_own_restricted_passages(stack):
    doc = seed_doc(stack, classification=Classification.RESTRICTED, owner_id=stack.user.user_id)
    seed_chunks(stack, doc.id, [{"text": "Draft position."}])

    resp = get_chunks(doc.id)

    assert resp.status_code == 200
    assert resp.json()["chunks"][0]["text"] == "Draft position."


def test_an_admin_can_read_anyones_restricted_passages(stack):
    doc = seed_doc(stack, classification=Classification.RESTRICTED, owner_id=uuid.uuid4())
    seed_chunks(stack, doc.id, [{"text": "Draft position."}])
    app.dependency_overrides[get_current_user] = lambda: _FakeUser(role=UserRole.ADMIN.value)

    assert get_chunks(doc.id).status_code == 200


def test_an_unknown_document_is_404(stack):
    assert get_chunks(uuid.uuid4()).status_code == 404


# ==============================================================================
# What comes back
# ==============================================================================

def test_passages_come_back_in_reading_order(stack):
    """The panel renders them top to bottom as the document reads. Insertion order is not
    guaranteed by the database, so the ordering has to be asked for."""
    doc = seed_doc(stack)
    seed_chunks(stack, doc.id, [{"text": f"Para {i}."} for i in range(5)])

    body = get_chunks(doc.id).json()

    assert [c["chunk_index"] for c in body["chunks"]] == [0, 1, 2, 3, 4]
    assert body["total"] == 5


def test_citation_metadata_is_carried_through(stack):
    """Page and section are what make a passage quotable, and the panel shows both."""
    doc = seed_doc(stack)
    seed_chunks(stack, doc.id, [
        {"page_number": 7, "section_heading": "4.2 Penalties", "is_table": True, "char_count": 340},
    ])

    c = get_chunks(doc.id).json()["chunks"][0]

    assert c["page_number"] == 7
    assert c["section_heading"] == "4.2 Penalties"
    assert c["is_table"] is True
    assert c["char_count"] == 340


def test_an_unembedded_passage_is_reported_as_such(stack):
    """A chunk with no vector is in the table but invisible to meaning-based search. The panel
    marks it, because "we have no answer" and "we never indexed it" are different problems."""
    doc = seed_doc(stack)
    seed_chunks(stack, doc.id, [
        {"embedding": [0.1] * 768},
        {"embedding": None},
    ])

    chunks = get_chunks(doc.id).json()["chunks"]

    assert chunks[0]["embedded"] is True
    assert chunks[1]["embedded"] is False


def test_a_document_with_no_passages_returns_an_empty_list(stack):
    """What a scanned PDF looks like: it validated, it has bytes, and nothing was read out of
    it. The panel shows this as "no passages" rather than as an error."""
    doc = seed_doc(stack, status=DocumentStatus.NORMALIZATION_FAILED)

    body = get_chunks(doc.id).json()

    assert body["total"] == 0
    assert body["chunks"] == []
    assert body["status"] == "NORMALIZATION_FAILED"


def test_paging_reports_the_full_total(stack):
    """A 1,300-page PDF has more passages than anyone will scroll. The page is capped, but the
    count must be the real one or the panel understates what is in the document."""
    doc = seed_doc(stack)
    seed_chunks(stack, doc.id, [{} for _ in range(12)])

    body = get_chunks(doc.id, limit=5, offset=10).json()

    assert body["total"] == 12
    assert [c["chunk_index"] for c in body["chunks"]] == [10, 11]


def test_the_limit_is_capped(stack):
    doc = seed_doc(stack)
    seed_chunks(stack, doc.id, [{}])

    assert get_chunks(doc.id, limit=99999).json()["limit"] == 1000
