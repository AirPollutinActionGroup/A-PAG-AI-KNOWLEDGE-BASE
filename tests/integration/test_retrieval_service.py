"""RetrievalService against real Postgres — the permission SQL and the ranking.

`test_vector_search.py` pinned the *shape* of the query before the endpoint existed. These tests
run the shipped service, so the two cannot drift: if someone rewrites the SQL to post-filter, the
"does not consume a top-k slot" test here fails.

No model is loaded. A stub provider returns fixed vectors, because what is under test is the SQL,
not the embedding.
"""

import uuid

import pytest
from sqlalchemy.orm import Session

from src.db.models import Document as DocumentORM
from src.db.models import DocumentChunk as ChunkORM
from src.db.models import User as UserORM
from src.modules.document_pipeline.embedding.provider import EmbeddingProvider
from src.modules.retrieval.service import RetrievalService

DIM = 768


def _vec(*leading: float) -> list[float]:
    return list(leading) + [0.0] * (DIM - len(leading))


class StubProvider(EmbeddingProvider):
    """Returns whatever vector the test asks for, and records that `embed_query` was the method
    used — the asymmetric-model trap the provider split exists to prevent."""

    def __init__(self, vector: list[float] | None = None):
        self.vector = vector or _vec(1.0)
        self.query_calls: list[str] = []
        self.passage_calls: list[list[str]] = []

    @property
    def model_name(self) -> str:
        return "stub/model"

    @property
    def dimensions(self) -> int:
        return DIM

    def embed_passages(self, texts):
        self.passage_calls.append(list(texts))
        return [self.vector for _ in texts]

    def embed_query(self, text):
        self.query_calls.append(text)
        return self.vector

    @property
    def max_sequence_tokens(self) -> int:
        return 512

    def count_tokens(self, texts):
        return [max(1, len(t) // 4) for t in texts]


def _user(session: Session) -> uuid.UUID:
    uid = uuid.uuid4()
    session.add(UserORM(
        user_id=uid, email=f"r-{uid.hex[:8]}@a-pag.org", full_name="Retrieval Test",
        hashed_password="not-a-real-hash", role="USER",
    ))
    session.flush()
    return uid


def _doc(session: Session, classification="PUBLIC", owner=None, deleted_at=None) -> DocumentORM:
    doc = DocumentORM(
        document_id=uuid.uuid4(), filename=f"{classification.lower()}-{uuid.uuid4().hex[:6]}.pdf",
        file_size=2048, status="LIVE", classification=classification,
        uploader_user_id=owner, deleted_at=deleted_at,
    )
    session.add(doc)
    session.flush()
    return doc


def _chunk(session, doc, vector, text_body, index=0, **kw):
    session.add(ChunkORM(
        chunk_id=uuid.uuid4(), document_id=doc.document_id, scale="section",
        chunk_index=index, text=text_body, char_count=len(text_body), embedding=vector, **kw,
    ))


@pytest.fixture
def service():
    return RetrievalService(provider=StubProvider())


# ==============================================================================
# Ranking
# ==============================================================================

def test_results_are_ordered_by_similarity(db_session: Session, service):
    doc = _doc(db_session)
    _chunk(db_session, doc, _vec(0.0, 1.0), "unrelated", 0)
    _chunk(db_session, doc, _vec(1.0, 0.1), "close match", 1)
    _chunk(db_session, doc, _vec(1.0, 0.6), "middling", 2)
    db_session.flush()

    hits, _ = service.search(db_session, "q", user_id=_user(db_session), is_admin=False, limit=10)

    assert [h.text for h in hits][:2] == ["close match", "middling"]
    assert hits[0].score > hits[1].score, "score must decrease with distance"

    db_session.rollback()


def test_score_is_a_similarity_not_a_distance(db_session: Session, service):
    """`<=>` returns distance, where smaller is better. Returning it raw would invert every
    caller's idea of a good result, silently."""
    doc = _doc(db_session)
    _chunk(db_session, doc, _vec(1.0), "identical direction", 0)
    db_session.flush()

    hit = service.search(db_session, "q", user_id=_user(db_session), is_admin=False, limit=1)[0][0]

    assert hit.score == pytest.approx(1.0, abs=1e-6)

    db_session.rollback()


def test_the_query_is_embedded_as_a_query_not_as_a_passage(db_session: Session):
    """Asymmetric models need the query form. Using the passage form costs retrieval quality with
    no error raised, so only a test of which method was called can catch it."""
    provider = StubProvider()
    svc = RetrievalService(provider=provider)

    svc.search(db_session, "what are the targets", user_id=None, is_admin=True, limit=5)

    assert provider.query_calls == ["what are the targets"]
    assert provider.passage_calls == [], "a query must never be embedded as a passage"

    db_session.rollback()


def test_limit_is_respected(db_session: Session, service):
    doc = _doc(db_session)
    for i in range(12):
        _chunk(db_session, doc, _vec(1.0, i / 100), f"passage {i}", i)
    db_session.flush()

    assert len(service.search(db_session, "q", user_id=None, is_admin=True, limit=3)[0]) == 3

    db_session.rollback()


# ==============================================================================
# Exclusions
# ==============================================================================

def test_unembedded_chunks_are_excluded(db_session: Session, service):
    """A chunk awaiting embedding is a normal intermediate state, not a result."""
    doc = _doc(db_session)
    _chunk(db_session, doc, _vec(1.0), "embedded", 0)
    _chunk(db_session, doc, None, "not yet embedded", 1)
    db_session.flush()

    hits, _ = service.search(db_session, "q", user_id=None, is_admin=True, limit=10)

    assert [h.text for h in hits] == ["embedded"]

    db_session.rollback()


def test_soft_deleted_documents_are_excluded(db_session: Session, service):
    """Deletion is a `deleted_at` stamp, so the chunks and their vectors are still in the table.
    Omitting this predicate would make deleted documents searchable."""
    from datetime import UTC, datetime

    live = _doc(db_session)
    gone = _doc(db_session, deleted_at=datetime.now(UTC))
    _chunk(db_session, live, _vec(1.0, 0.5), "live passage", 0)
    _chunk(db_session, gone, _vec(1.0), "deleted passage", 0)
    db_session.flush()

    hits, _ = service.search(db_session, "q", user_id=None, is_admin=True, limit=10)

    assert [h.text for h in hits] == ["live passage"]

    db_session.rollback()


# ==============================================================================
# Permissions — enforced in SQL, before ORDER BY / LIMIT
# ==============================================================================

def test_restricted_passage_is_hidden_from_a_stranger(db_session: Session, service):
    owner, stranger = _user(db_session), _user(db_session)
    _chunk(db_session, _doc(db_session, "RESTRICTED", owner=owner), _vec(1.0), "secret", 0)
    db_session.flush()

    hits, _ = service.search(db_session, "q", user_id=stranger, is_admin=False, limit=10)

    assert hits == []

    db_session.rollback()


def test_restricted_passage_does_not_consume_a_top_k_slot(db_session: Session, service):
    """The failure this design exists to prevent. Filtering in Python after the query would let
    the restricted chunk win the only slot, and the caller would receive an empty result with no
    way to tell whether the corpus was thin or the answer was withheld."""
    owner, stranger = _user(db_session), _user(db_session)
    _chunk(db_session, _doc(db_session, "RESTRICTED", owner=owner), _vec(1.0), "nearest", 0)
    _chunk(db_session, _doc(db_session, "PUBLIC"), _vec(0.9, 0.1), "second nearest", 0)
    db_session.flush()

    hits, _ = service.search(db_session, "q", user_id=stranger, is_admin=False, limit=1)

    assert [h.text for h in hits] == ["second nearest"]

    db_session.rollback()


def test_owner_sees_their_own_restricted_passage(db_session: Session, service):
    owner = _user(db_session)
    _chunk(db_session, _doc(db_session, "RESTRICTED", owner=owner), _vec(1.0), "mine", 0)
    db_session.flush()

    hits, _ = service.search(db_session, "q", user_id=owner, is_admin=False, limit=10)

    assert [h.text for h in hits] == ["mine"]

    db_session.rollback()


def test_admin_sees_restricted_passages(db_session: Session, service):
    _chunk(db_session, _doc(db_session, "RESTRICTED", owner=_user(db_session)), _vec(1.0), "any", 0)
    db_session.flush()

    hits, _ = service.search(db_session, "q", user_id=_user(db_session), is_admin=True, limit=10)

    assert [h.text for h in hits] == ["any"]

    db_session.rollback()


def test_public_passages_are_visible_to_everyone(db_session: Session, service):
    _chunk(db_session, _doc(db_session, "PUBLIC"), _vec(1.0), "org-wide", 0)
    db_session.flush()

    hits, _ = service.search(db_session, "q", user_id=_user(db_session), is_admin=False, limit=10)

    assert [h.text for h in hits] == ["org-wide"]

    db_session.rollback()


# ==============================================================================
# Citation metadata survives the round trip
# ==============================================================================

def test_citation_fields_are_returned(db_session: Session, service):
    doc = _doc(db_session)
    _chunk(db_session, doc, _vec(1.0), "Table of district targets.", 0,
           page_number=7, section_heading="4. Targets", is_table=True)
    db_session.flush()

    hit = service.search(db_session, "q", user_id=None, is_admin=True, limit=1)[0][0]

    assert hit.document_id == doc.document_id
    assert hit.filename == doc.filename
    assert hit.page_number == 7
    assert hit.section_heading == "4. Targets"
    assert hit.is_table is True

    db_session.rollback()


def test_a_null_heading_round_trips_as_none(db_session: Session, service):
    """Extraction leaves this NULL rather than guessing (KNOWN_DEBTS.md #19); the passage is still
    citable by page."""
    doc = _doc(db_session)
    _chunk(db_session, doc, _vec(1.0), "orphan table row", 0, page_number=3, section_heading=None)
    db_session.flush()

    hit = service.search(db_session, "q", user_id=None, is_admin=True, limit=1)[0][0]

    assert hit.section_heading is None
    assert hit.page_number == 3

    db_session.rollback()
