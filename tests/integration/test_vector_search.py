"""pgvector integration tests — real Postgres, real similarity search.

These cannot be unit tests: SQLite has no vector type and no distance operators, so the behaviour
that matters here only exists against a real server.

The permission tests are the important ones. The existing list/search endpoints filter in Python
*after* SQL returns, which means RESTRICTED rows consume page slots and the returned `total` leaks
their count. For vector retrieval that pattern is worse — a RESTRICTED chunk would occupy a top-k
slot and push out a result the user is allowed to see. These tests pin the correct shape: the
permission predicate belongs in the WHERE clause, before ORDER BY and LIMIT.
"""

import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DataError
from sqlalchemy.orm import Session

from src.db.models import Document as DocumentORM
from src.db.models import DocumentChunk as ChunkORM
from src.db.models import User as UserORM

DIM = 768


def _vec(seed: float) -> list[float]:
    """A deterministic unit-ish vector. Distinct seeds give distinct directions, so cosine
    ordering is predictable without needing a real model."""
    return [seed] + [0.0] * (DIM - 1)


def _make_user(session: Session) -> uuid.UUID:
    """`documents.uploader_user_id` carries a real FK (migration 0006), so ownership tests need
    an actual users row rather than a fabricated UUID."""
    user_id = uuid.uuid4()
    session.add(UserORM(
        user_id=user_id,
        email=f"vec-{user_id.hex[:8]}@a-pag.org",
        full_name="Vector Test User",
        hashed_password="not-a-real-hash",
        role="USER",
    ))
    session.flush()
    return user_id


def _make_doc(session: Session, classification: str = "PUBLIC", owner=None) -> DocumentORM:
    doc = DocumentORM(
        document_id=uuid.uuid4(),
        filename=f"{classification.lower()}-{uuid.uuid4().hex[:8]}.pdf",
        file_size=1024,
        status="CHUNKED",
        classification=classification,
        uploader_user_id=owner,
    )
    session.add(doc)
    session.flush()
    return doc


def _add_chunk(session, doc, index: int, vector, text_body="Passage about enforcement."):
    chunk = ChunkORM(
        chunk_id=uuid.uuid4(),
        document_id=doc.document_id,
        scale="section",
        chunk_index=index,
        text=text_body,
        char_count=len(text_body),
        embedding=vector,
    )
    session.add(chunk)
    return chunk


# ==============================================================================
# The extension and column actually work
# ==============================================================================

def test_pgvector_extension_is_available(db_session: Session):
    version = db_session.execute(
        text("SELECT extversion FROM pg_extension WHERE extname = 'vector'")
    ).scalar_one_or_none()
    assert version is not None, "migration 0014 enables this; the image must be a pgvector build"


def test_embedding_round_trips_at_the_declared_dimension(db_session: Session):
    doc = _make_doc(db_session)
    _add_chunk(db_session, doc, 0, _vec(1.0))
    db_session.flush()

    stored = db_session.query(ChunkORM).filter(ChunkORM.document_id == doc.document_id).one()
    assert len(stored.embedding) == DIM

    db_session.rollback()


def test_wrong_dimension_is_rejected_by_the_database(db_session: Session):
    """The column width is the last line of defence against a model swap that skipped its
    migration — FastEmbedProvider probes at load, but this is what catches it if that is bypassed."""
    doc = _make_doc(db_session)
    _add_chunk(db_session, doc, 0, [0.5] * (DIM + 128))

    with pytest.raises(DataError):
        db_session.flush()

    db_session.rollback()


def test_chunks_may_exist_before_they_are_embedded(db_session: Session):
    """Chunking and embedding are separate stages; a chunk with a NULL vector is a normal
    intermediate state, not an error."""
    doc = _make_doc(db_session)
    _add_chunk(db_session, doc, 0, None)
    db_session.flush()

    stored = db_session.query(ChunkORM).filter(ChunkORM.document_id == doc.document_id).one()
    assert stored.embedding is None

    db_session.rollback()


# ==============================================================================
# Similarity ordering
# ==============================================================================

def test_cosine_search_returns_nearest_first(db_session: Session):
    doc = _make_doc(db_session)
    near = [1.0, 0.1] + [0.0] * (DIM - 2)
    far = [0.0, 1.0] + [0.0] * (DIM - 2)
    _add_chunk(db_session, doc, 0, near, "near passage")
    _add_chunk(db_session, doc, 1, far, "far passage")
    db_session.flush()

    query = [1.0, 0.0] + [0.0] * (DIM - 2)
    rows = db_session.execute(
        text(
            "SELECT text FROM document_chunks "
            "WHERE document_id = :doc AND embedding IS NOT NULL "
            "ORDER BY embedding <=> CAST(:q AS vector) LIMIT 2"
        ),
        {"doc": doc.document_id, "q": str(query)},
    ).scalars().all()

    assert rows[0] == "near passage"

    db_session.rollback()


def test_unembedded_chunks_do_not_appear_in_similarity_results(db_session: Session):
    """A NULL vector must be excluded explicitly — otherwise a half-embedded document silently
    returns fewer results than requested, which looks like poor recall rather than a bug."""
    doc = _make_doc(db_session)
    _add_chunk(db_session, doc, 0, _vec(1.0), "embedded")
    _add_chunk(db_session, doc, 1, None, "not embedded")
    db_session.flush()

    rows = db_session.execute(
        text(
            "SELECT text FROM document_chunks "
            "WHERE document_id = :doc AND embedding IS NOT NULL "
            "ORDER BY embedding <=> CAST(:q AS vector)"
        ),
        {"doc": doc.document_id, "q": str(_vec(1.0))},
    ).scalars().all()

    assert rows == ["embedded"]

    db_session.rollback()


# ==============================================================================
# Permission filtering must happen in SQL, not after it
# ==============================================================================

SIMILARITY_SQL = text(
    """
    SELECT c.text
    FROM document_chunks c
    JOIN documents d ON d.document_id = c.document_id
    WHERE c.embedding IS NOT NULL
      AND d.deleted_at IS NULL
      AND (d.classification <> 'RESTRICTED' OR d.uploader_user_id = :uid OR :is_admin)
    ORDER BY c.embedding <=> CAST(:q AS vector)
    LIMIT :k
    """
)


def test_restricted_chunk_is_excluded_from_results(db_session: Session):
    owner, stranger = _make_user(db_session), _make_user(db_session)
    restricted = _make_doc(db_session, "RESTRICTED", owner=owner)
    _add_chunk(db_session, restricted, 0, _vec(1.0), "restricted passage")
    db_session.flush()

    rows = db_session.execute(
        SIMILARITY_SQL,
        {"q": str(_vec(1.0)), "uid": stranger, "is_admin": False, "k": 10},
    ).scalars().all()

    assert "restricted passage" not in rows

    db_session.rollback()


def test_restricted_chunk_does_not_consume_a_top_k_slot(db_session: Session):
    """The failure this whole approach exists to prevent. Filtering after the query would let a
    RESTRICTED chunk win the nearest slot and push a legitimate result out of a LIMIT 1 — the user
    gets nothing back and has no way to know why."""
    owner, stranger = _make_user(db_session), _make_user(db_session)
    restricted = _make_doc(db_session, "RESTRICTED", owner=owner)
    public = _make_doc(db_session, "PUBLIC")

    nearest = [1.0, 0.0] + [0.0] * (DIM - 2)
    second = [0.9, 0.1] + [0.0] * (DIM - 2)
    _add_chunk(db_session, restricted, 0, nearest, "restricted nearest")
    _add_chunk(db_session, public, 0, second, "public second")
    db_session.flush()

    rows = db_session.execute(
        SIMILARITY_SQL,
        {"q": str(nearest), "uid": stranger, "is_admin": False, "k": 1},
    ).scalars().all()

    assert rows == ["public second"], "the restricted chunk must not occupy the slot"

    db_session.rollback()


def test_owner_sees_their_own_restricted_chunk(db_session: Session):
    owner = _make_user(db_session)
    restricted = _make_doc(db_session, "RESTRICTED", owner=owner)
    _add_chunk(db_session, restricted, 0, _vec(1.0), "my restricted passage")
    db_session.flush()

    rows = db_session.execute(
        SIMILARITY_SQL,
        {"q": str(_vec(1.0)), "uid": owner, "is_admin": False, "k": 10},
    ).scalars().all()

    assert "my restricted passage" in rows

    db_session.rollback()


def test_admin_sees_restricted_chunks(db_session: Session):
    restricted = _make_doc(db_session, "RESTRICTED", owner=_make_user(db_session))
    _add_chunk(db_session, restricted, 0, _vec(1.0), "restricted passage")
    db_session.flush()

    rows = db_session.execute(
        SIMILARITY_SQL,
        {"q": str(_vec(1.0)), "uid": _make_user(db_session), "is_admin": True, "k": 10},
    ).scalars().all()

    assert "restricted passage" in rows

    db_session.rollback()
