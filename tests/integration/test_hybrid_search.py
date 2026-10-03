"""Hybrid retrieval against real Postgres — both arms, their fusion, and the permission clause.

The permission tests are the point. Hybrid doubled the number of places a RESTRICTED passage
could leak from: the predicate now has to be in the semantic arm *and* the lexical arm, and a
FULL OUTER JOIN means a row returned by either one reaches the caller. A leak in the lexical arm
alone would be invisible to every test written for the semantic one.

Vectors here are stubbed — the SQL is what is under test, not the model. The lexical arm is not
stubbed, because its behaviour comes from `pg_search`'s BM25 index and scoring, which exist only
in Postgres and cannot be imitated in SQLite.
"""

import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from src.db.models import Document as DocumentORM
from src.db.models import DocumentChunk as ChunkORM
from src.db.models import User as UserORM
from src.modules.document_pipeline.embedding.provider import EmbeddingProvider
from src.modules.retrieval.service import RetrievalService, SearchMode

DIM = 768


def _vec(*leading: float) -> list[float]:
    return list(leading) + [0.0] * (DIM - len(leading))


class StubProvider(EmbeddingProvider):
    def __init__(self, vector: list[float] | None = None):
        self.vector = vector or _vec(1.0)

    @property
    def model_name(self) -> str:
        return "stub/model"

    @property
    def dimensions(self) -> int:
        return DIM

    @property
    def max_sequence_tokens(self) -> int:
        return 512

    def embed_passages(self, texts):
        return [self.vector for _ in texts]

    def embed_query(self, text):
        return self.vector

    def count_tokens(self, texts):
        return [max(1, len(t) // 4) for t in texts]


@pytest.fixture
def service():
    return RetrievalService(provider=StubProvider())


def _user(session: Session) -> uuid.UUID:
    uid = uuid.uuid4()
    session.add(UserORM(
        user_id=uid, email=f"h-{uid.hex[:8]}@a-pag.org", full_name="Hybrid Test",
        hashed_password="not-a-real-hash", role="USER",
    ))
    session.flush()
    return uid


def _doc(session: Session, classification="PUBLIC", owner=None) -> DocumentORM:
    doc = DocumentORM(
        document_id=uuid.uuid4(), filename=f"{classification.lower()}-{uuid.uuid4().hex[:6]}.pdf",
        file_size=2048, status="LIVE", classification=classification, uploader_user_id=owner,
    )
    session.add(doc)
    session.flush()
    return doc


def _chunk(session, doc, vector, body, index=0, heading=None):
    session.add(ChunkORM(
        chunk_id=uuid.uuid4(), document_id=doc.document_id, scale="section",
        chunk_index=index, text=body, char_count=len(body), embedding=vector,
        section_heading=heading,
    ))
    session.flush()


def texts(hits):
    return [h.text for h in hits]


# ==============================================================================
# The tsvector arm is gone, not dormant
# ==============================================================================

def test_chunks_carry_no_tsvector_column(db_session: Session):
    """`0015` built a tsvector column, a GIN index and a trigger for the lexical arm; `0016`
    replaced that arm with BM25 and `0020` removed the machinery once nothing read it.

    This asserts the removal rather than the old behaviour, because the failure worth catching
    now is the opposite one: a trigger recomputing a tsvector on every chunk INSERT for a column
    no query reads, which is free at this corpus size and is not free during a bulk ingest.
    `documents.search_vector` is a different column from `0006` and is still live."""
    present = db_session.execute(
        text(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = 'document_chunks' AND column_name = 'search_vector'"
        )
    ).scalar_one_or_none()
    assert present is None, "0020 should have dropped document_chunks.search_vector"

    trigger = db_session.execute(
        text(
            "SELECT tgname FROM pg_trigger "
            "WHERE tgname = 'trg_document_chunks_search_vector_update'"
        )
    ).scalar_one_or_none()
    assert trigger is None, "the tsvector trigger outlived the column it maintained"


def test_the_section_heading_is_indexed_too(db_session: Session):
    """BM25 indexes `text` and `section_heading` as separate fields and ORs them, because only
    1,056 of 2,475 headed chunks repeat their heading in the body — searching the body alone
    silently ignores headings."""
    doc = _doc(db_session)
    _chunk(db_session, doc, _vec(1.0), "Body text with no distinctive words.",
           heading="Penalties and Prosecution")

    hits, _ = service_search(db_session, "prosecution", SearchMode.LEXICAL)
    assert any("Body text" in t for t in texts(hits))
    db_session.rollback()


def service_search(db, q, mode, user_id=None, is_admin=True, limit=10):
    return RetrievalService(provider=StubProvider()).search(
        db, q, user_id=user_id, is_admin=is_admin, limit=limit, mode=mode
    )


# ==============================================================================
# The arms behave differently — the reason for having two
# ==============================================================================

def test_lexical_finds_an_exact_term_the_vector_arm_cannot(db_session: Session):
    """The gap hybrid exists to close. Every chunk here has an identical vector, so the semantic
    arm cannot distinguish them at all — only the lexical arm can pick out the passage that
    actually contains the identifier."""
    doc = _doc(db_session)
    _chunk(db_session, doc, _vec(1.0), "Refer to GRAPSTAGETHREE for escalation steps.", 0)
    _chunk(db_session, doc, _vec(1.0), "Unrelated commentary about seasonal variation.", 1)
    _chunk(db_session, doc, _vec(1.0), "Further unrelated commentary about budgets.", 2)

    hits, usage = service_search(db_session, "GRAPSTAGETHREE", SearchMode.LEXICAL)

    assert len(hits) == 1
    assert "GRAPSTAGETHREE" in hits[0].text
    assert usage.lexical_hits == 1
    db_session.rollback()


def test_semantic_returns_everything_embedded_regardless_of_words(db_session: Session):
    """The mirror image: with no shared keyword the lexical arm finds nothing, while the vector
    arm still ranks every embedded passage."""
    doc = _doc(db_session)
    for i in range(3):
        _chunk(db_session, doc, _vec(1.0), f"Passage {i} concerning ambient conditions.", i)

    sem, _ = service_search(db_session, "zzzznomatchzzzz", SearchMode.SEMANTIC)
    lex, _ = service_search(db_session, "zzzznomatchzzzz", SearchMode.LEXICAL)

    assert len(sem) == 3
    assert lex == []
    db_session.rollback()


# ==============================================================================
# Fusion
# ==============================================================================

def test_hybrid_includes_a_passage_only_one_arm_found(db_session: Session):
    """The FULL OUTER JOIN. An INNER JOIN would quietly reduce hybrid to "what both arms agree
    on" — a narrower result set than either arm alone, which is the opposite of the intent."""
    doc = _doc(db_session)
    # Matches lexically; its vector is orthogonal to the query so the semantic arm ranks it last.
    _chunk(db_session, doc, _vec(0.0, 1.0), "A passage mentioning GRAPSTAGETHREE explicitly.", 0)
    for i in range(1, 4):
        _chunk(db_session, doc, _vec(1.0), f"Near passage {i} with no identifier.", i)

    hits, usage = service_search(db_session, "GRAPSTAGETHREE", SearchMode.HYBRID, limit=10)

    assert any("GRAPSTAGETHREE" in t for t in texts(hits))
    assert usage.lexical_hits >= 1
    assert usage.semantic_hits >= 1
    db_session.rollback()


def test_hybrid_falls_back_cleanly_when_one_arm_is_empty(db_session: Session):
    """A paraphrase query matches nothing lexically. Hybrid must then equal semantic rather than
    returning nothing."""
    doc = _doc(db_session)
    for i in range(3):
        _chunk(db_session, doc, _vec(1.0), f"Passage {i} about ambient conditions.", i)

    hyb, usage = service_search(db_session, "zzzznomatchzzzz", SearchMode.HYBRID)
    sem, _ = service_search(db_session, "zzzznomatchzzzz", SearchMode.SEMANTIC)

    assert texts(hyb) == texts(sem)
    assert usage.lexical_hits == 0
    db_session.rollback()


def test_agreement_between_arms_outranks_a_single_arm(db_session: Session):
    """RRF's central property, end to end: a passage both arms rank well beats one that only the
    vector arm ranks first."""
    doc = _doc(db_session)
    _chunk(db_session, doc, _vec(1.0), "Nearest vector, says nothing about the term.", 0)
    _chunk(db_session, doc, _vec(0.99, 0.01), "Second nearest, and mentions GRAPSTAGETHREE.", 1)

    hits, _ = service_search(db_session, "GRAPSTAGETHREE", SearchMode.HYBRID, limit=2)

    assert "GRAPSTAGETHREE" in hits[0].text, "the passage both arms liked should win"
    db_session.rollback()


def test_results_carry_the_rank_each_arm_gave_them(db_session: Session):
    """So "why did this come back?" has an answer."""
    doc = _doc(db_session)
    _chunk(db_session, doc, _vec(1.0), "A passage mentioning GRAPSTAGETHREE.", 0)

    hits, _ = service_search(db_session, "GRAPSTAGETHREE", SearchMode.HYBRID)

    assert hits[0].semantic_rank == 1
    assert hits[0].lexical_rank == 1
    db_session.rollback()


def test_ordering_is_stable_across_identical_queries(db_session: Session):
    """RRF ties are common — scores come from a small set of rank reciprocals. Without a
    deterministic tiebreak the same query returns a different order each time, which reads as a
    bug to whoever is using it."""
    doc = _doc(db_session)
    for i in range(6):
        _chunk(db_session, doc, _vec(1.0), f"Interchangeable passage {i}.", i)

    first = [h.chunk_id for h in service_search(db_session, "passage", SearchMode.HYBRID)[0]]
    for _ in range(3):
        assert [h.chunk_id for h in
                service_search(db_session, "passage", SearchMode.HYBRID)[0]] == first
    db_session.rollback()


def test_limit_is_respected_in_every_mode(db_session: Session):
    doc = _doc(db_session)
    for i in range(12):
        _chunk(db_session, doc, _vec(1.0), f"Enforcement passage number {i}.", i)

    for mode in SearchMode:
        hits, _ = service_search(db_session, "enforcement", mode, limit=3)
        assert len(hits) <= 3, f"{mode.value} returned more than the limit"
    db_session.rollback()


def test_usage_reports_the_mode_that_ran(db_session: Session):
    doc = _doc(db_session)
    _chunk(db_session, doc, _vec(1.0), "Enforcement passage.", 0)

    for mode in SearchMode:
        _, usage = service_search(db_session, "enforcement", mode)
        assert usage.mode == mode.value
    db_session.rollback()


# ==============================================================================
# Permissions — in BOTH arms
# ==============================================================================

def test_lexical_arm_does_not_leak_a_restricted_passage(db_session: Session):
    """Hybrid doubled the places a restricted passage can escape from. A predicate present in the
    semantic arm but missing from the lexical one would pass every test written for the former."""
    owner, stranger = _user(db_session), _user(db_session)
    _chunk(db_session, _doc(db_session, "RESTRICTED", owner=owner), _vec(1.0),
           "Restricted passage mentioning GRAPSTAGETHREE.", 0)

    hits, _ = service_search(db_session, "GRAPSTAGETHREE", SearchMode.LEXICAL,
                             user_id=stranger, is_admin=False)

    assert hits == []
    db_session.rollback()


def test_hybrid_does_not_leak_a_restricted_passage(db_session: Session):
    owner, stranger = _user(db_session), _user(db_session)
    _chunk(db_session, _doc(db_session, "RESTRICTED", owner=owner), _vec(1.0),
           "Restricted passage mentioning GRAPSTAGETHREE.", 0)
    _chunk(db_session, _doc(db_session, "PUBLIC"), _vec(0.5, 0.5), "Public passage.", 0)

    hits, _ = service_search(db_session, "GRAPSTAGETHREE", SearchMode.HYBRID,
                             user_id=stranger, is_admin=False)

    assert not any("Restricted" in t for t in texts(hits))
    db_session.rollback()


def test_a_restricted_passage_does_not_consume_a_slot_in_either_arm(db_session: Session):
    """Filtering after fusion would let the restricted passage win the only slot, and the caller
    would receive nothing with no way to tell why."""
    owner, stranger = _user(db_session), _user(db_session)
    _chunk(db_session, _doc(db_session, "RESTRICTED", owner=owner), _vec(1.0),
           "Restricted and nearest, mentioning GRAPSTAGETHREE.", 0)
    _chunk(db_session, _doc(db_session, "PUBLIC"), _vec(0.9, 0.1),
           "Public passage also mentioning GRAPSTAGETHREE.", 0)

    hits, _ = service_search(db_session, "GRAPSTAGETHREE", SearchMode.HYBRID,
                             user_id=stranger, is_admin=False, limit=1)

    assert len(hits) == 1
    assert "Public" in hits[0].text
    db_session.rollback()


def test_owner_sees_their_own_restricted_passage_lexically(db_session: Session):
    owner = _user(db_session)
    _chunk(db_session, _doc(db_session, "RESTRICTED", owner=owner), _vec(1.0),
           "My restricted passage mentioning GRAPSTAGETHREE.", 0)

    hits, _ = service_search(db_session, "GRAPSTAGETHREE", SearchMode.LEXICAL,
                             user_id=owner, is_admin=False)

    assert len(hits) == 1
    db_session.rollback()


def test_admin_sees_restricted_passages_in_hybrid(db_session: Session):
    _chunk(db_session, _doc(db_session, "RESTRICTED", owner=_user(db_session)), _vec(1.0),
           "Restricted passage mentioning GRAPSTAGETHREE.", 0)

    hits, _ = service_search(db_session, "GRAPSTAGETHREE", SearchMode.HYBRID,
                             user_id=_user(db_session), is_admin=True)

    assert len(hits) == 1
    db_session.rollback()


def test_soft_deleted_documents_are_excluded_from_both_arms(db_session: Session):
    from datetime import UTC, datetime

    gone = _doc(db_session)
    gone.deleted_at = datetime.now(UTC)
    db_session.flush()
    _chunk(db_session, gone, _vec(1.0), "Deleted passage mentioning GRAPSTAGETHREE.", 0)

    for mode in SearchMode:
        hits, _ = service_search(db_session, "GRAPSTAGETHREE", mode)
        assert hits == [], f"{mode.value} returned a soft-deleted document"
    db_session.rollback()


def test_unembedded_chunks_are_still_findable_lexically(db_session: Session):
    """A deliberate asymmetry worth pinning: the semantic arm requires a vector, the lexical arm
    does not. A chunk awaiting embedding is therefore already searchable by word, which is a
    small recall win during a re-embed rather than a bug."""
    doc = _doc(db_session)
    _chunk(db_session, doc, None, "Unembedded passage mentioning GRAPSTAGETHREE.", 0)

    lex, _ = service_search(db_session, "GRAPSTAGETHREE", SearchMode.LEXICAL)
    sem, _ = service_search(db_session, "GRAPSTAGETHREE", SearchMode.SEMANTIC)

    assert len(lex) == 1
    assert sem == []
    db_session.rollback()
