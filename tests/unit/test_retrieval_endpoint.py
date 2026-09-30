"""Retrieval endpoint contract — no model is loaded and no database is touched.

The similarity ordering and the permission SQL are exercised against real Postgres in
`tests/integration/`; what is testable here is the endpoint's own behaviour: input validation,
that the caller's identity reaches the service rather than being trusted from the request, and
that the response carries the citation fields a caller needs.
"""

import uuid
from types import SimpleNamespace

import pytest
from starlette.testclient import TestClient

from src.api.v1.retrieval import get_retrieval_service
from src.api.v1.router import app
from src.db.engine import get_db
from src.db.enums import UserRole
from src.modules.auth.dependencies import get_current_user
from src.modules.retrieval.models import RetrievedChunk, TokenUsage

client = TestClient(app)


class _FakeUser:
    def __init__(self, role=UserRole.USER.value, user_id=None):
        self.user_id = user_id or uuid.uuid4()
        self.role = role
        self.is_active = True
        self.email = f"{self.user_id}@a-pag.org"


class FakeService:
    """Records how the endpoint called it. The arguments are the contract under test."""

    def __init__(self, results=None):
        self.calls: list[dict] = []
        self._results = results if results is not None else []

    @property
    def model_name(self) -> str:
        return "fake/model"

    def search(self, db, query, *, user_id, is_admin, limit, mode=None):
        self.calls.append(
            {"query": query, "user_id": user_id, "is_admin": is_admin, "limit": limit,
             "mode": getattr(mode, "value", mode)}
        )
        usage = TokenUsage(
            query_tokens=len(query.split()),
            context_tokens=sum(r.token_count for r in self._results),
            max_sequence_tokens=512,
            truncated_results=sum(1 for r in self._results if r.truncated),
            model="fake/model",
        )
        return self._results, usage


def _chunk(text="District targets for 2027.", **kw):
    defaults = {
        "chunk_id": uuid.uuid4(), "document_id": uuid.uuid4(), "filename": "directive.pdf",
        "text": text, "page_number": 4, "section_heading": "3. Obligations",
        "is_table": False, "score": 0.016, "token_count": 120, "truncated": False,
        # Above SEARCH_MIN_SIMILARITY, so the grounding gate lets these through. On-topic
        # questions score 0.69-0.84 against this corpus.
        "similarity": 0.72, "semantic_rank": 1,
    }
    return RetrievedChunk(**{**defaults, **kw})


@pytest.fixture
def stack():
    """Overrides saved and restored rather than popped — other modules install their own at
    import time, and unconditionally popping leaves them falling through to a real database."""
    service = FakeService()
    user = _FakeUser()

    previous = {
        dep: app.dependency_overrides.get(dep)
        for dep in (get_retrieval_service, get_current_user, get_db)
    }
    app.dependency_overrides[get_retrieval_service] = lambda: service
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_db] = lambda: iter([None])
    try:
        yield SimpleNamespace(service=service, user=user)
    finally:
        for dep, override in previous.items():
            if override is None:
                app.dependency_overrides.pop(dep, None)
            else:
                app.dependency_overrides[dep] = override


# ==============================================================================
# Input validation
# ==============================================================================

@pytest.mark.parametrize("q", ["", "   ", "\t\n"])
def test_blank_query_is_rejected(stack, q):
    """An all-whitespace query would otherwise be embedded into a meaningless vector and return
    the arbitrary nearest passages, which looks like an answer."""
    r = client.get("/api/v1/search", params={"q": q})

    assert r.status_code == 422
    assert stack.service.calls == [], "the model must not be run on a blank query"


def test_query_is_trimmed_before_embedding(stack):
    client.get("/api/v1/search", params={"q": "  enforcement obligations  "})

    assert stack.service.calls[0]["query"] == "enforcement obligations"


@pytest.mark.parametrize("limit,expected_status", [(0, 422), (51, 422), (1, 200), (50, 200)])
def test_limit_is_bounded(stack, limit, expected_status):
    """An unbounded top-k is a cheap way to pull the whole corpus out one request at a time."""
    r = client.get("/api/v1/search", params={"q": "targets", "limit": limit})
    assert r.status_code == expected_status


def test_default_limit_is_applied(stack):
    client.get("/api/v1/search", params={"q": "targets"})
    assert stack.service.calls[0]["limit"] == 10


# ==============================================================================
# Identity reaches the service — it is never taken from the request
# ==============================================================================

def test_the_authenticated_user_is_passed_to_the_service(stack):
    """The permission filter is only as good as the identity it is given. Were this read from a
    query parameter, any caller could ask for someone else's restricted passages."""
    client.get("/api/v1/search", params={"q": "targets"})

    call = stack.service.calls[0]
    assert call["user_id"] == stack.user.user_id
    assert call["is_admin"] is False


def test_admin_role_is_propagated(stack):
    app.dependency_overrides[get_current_user] = lambda: _FakeUser(role=UserRole.ADMIN.value)

    client.get("/api/v1/search", params={"q": "targets"})

    assert stack.service.calls[0]["is_admin"] is True


def test_a_user_id_in_the_query_string_is_ignored(stack):
    """Guards against the identity ever being sourced from the request."""
    other = uuid.uuid4()

    client.get("/api/v1/search", params={"q": "targets", "user_id": str(other), "is_admin": "true"})

    call = stack.service.calls[0]
    assert call["user_id"] == stack.user.user_id
    assert call["is_admin"] is False


def test_search_requires_authentication():
    """With no override installed, the real dependency runs and must reject an unauthenticated
    request rather than searching as nobody."""
    previous = app.dependency_overrides.pop(get_current_user, None)
    try:
        r = client.get("/api/v1/search", params={"q": "targets"})
        assert r.status_code in (401, 403)
    finally:
        if previous is not None:
            app.dependency_overrides[get_current_user] = previous


# ==============================================================================
# Response shape — the citation contract
# ==============================================================================

def test_results_carry_their_citation(stack):
    """A passage without its source is not usable in an answer: the point of chunking was that a
    citation can name the page and section, not just the file."""
    stack.service._results = [_chunk()]

    body = client.get("/api/v1/search", params={"q": "targets"}).json()

    assert body["count"] == 1
    hit = body["results"][0]
    for field in ("chunk_id", "document_id", "filename", "text",
                  "page_number", "section_heading", "is_table", "score"):
        assert field in hit, f"missing citation field: {field}"
    assert hit["page_number"] == 4
    assert hit["section_heading"] == "3. Obligations"


def test_a_passage_with_no_attributable_heading_is_still_returned(stack):
    """Extraction leaves `section_heading` NULL where it cannot attribute a table to one heading
    (KNOWN_DEBTS.md #19). That passage must still be retrievable — the page number still cites it."""
    stack.service._results = [_chunk(section_heading=None, is_table=True)]

    hit = client.get("/api/v1/search", params={"q": "budget"}).json()["results"][0]

    assert hit["section_heading"] is None
    assert hit["is_table"] is True
    assert hit["page_number"] == 4


def test_no_matches_is_an_empty_result_not_an_error(stack):
    """An empty corpus and a bad query are different things, and neither is a server error."""
    stack.service._results = []

    r = client.get("/api/v1/search", params={"q": "nothing matches this"})

    assert r.status_code == 200
    body = r.json()
    assert body["count"] == 0
    assert body["results"] == []
    assert body["query"] == "nothing matches this"
    assert body["usage"]["context_tokens"] == 0, "no passages means no context"


def test_the_echoed_query_is_the_trimmed_one(stack):
    body = client.get("/api/v1/search", params={"q": "  targets  "}).json()
    assert body["query"] == "targets"


# ==============================================================================
# Token usage
# ==============================================================================

def test_usage_is_reported_with_the_results(stack):
    """The context size is what decides whether these passages fit in a future LLM prompt, so it
    is worth showing now, while chunk sizing can still be changed cheaply."""
    stack.service._results = [_chunk(), _chunk()]

    usage = client.get("/api/v1/search", params={"q": "district targets"}).json()["usage"]

    assert usage["context_tokens"] == 240, "the sum of the returned passages"
    assert usage["query_tokens"] > 0
    assert usage["max_sequence_tokens"] == 512
    assert usage["model"] == "fake/model"


def test_a_truncated_passage_is_flagged_to_the_caller(stack):
    """A passage longer than the model's window was embedded only up to the cap, so a search for
    something mentioned only in its tail will not find it. Saying so lets the reader distinguish
    that from the corpus genuinely not containing the answer."""
    stack.service._results = [_chunk(token_count=780, truncated=True)]

    body = client.get("/api/v1/search", params={"q": "batch normalization"}).json()

    assert body["results"][0]["truncated"] is True
    assert body["results"][0]["token_count"] == 780
    assert body["usage"]["truncated_results"] == 1


# ==============================================================================
# "I don't know" — the grounding gate
# ==============================================================================

def test_an_off_topic_question_returns_nothing_rather_than_the_nearest_passage(stack):
    """Vector search always returns *something* — there is no such thing as no nearest
    neighbour. Without this gate, asking about cake returns the nearest policy passage with a
    page citation and every appearance of confidence, which is worse than an empty answer: it is
    the apparatus of an answer without the substance."""
    stack.service._results = [_chunk(similarity=0.46, text="Unrelated policy text.")]

    body = client.get("/api/v1/search", params={"q": "best chocolate cake recipe"}).json()

    assert body["grounded"] is False
    assert body["count"] == 0
    assert body["results"] == [], "the passages must be withheld, not merely flagged"
    assert body["best_similarity"] == pytest.approx(0.46)


def test_an_on_topic_question_is_grounded(stack):
    stack.service._results = [_chunk(similarity=0.74)]

    body = client.get("/api/v1/search", params={"q": "enforcement obligations"}).json()

    assert body["grounded"] is True
    assert body["count"] == 1


def test_an_exact_word_match_is_grounded_even_at_low_similarity(stack):
    """If the words are literally in a document, the corpus contains them, whatever the
    embedding thinks. Rare identifiers — a statute number, a district name — are exactly the
    case where similarity is low and the match is real.

    Note the fixture carries the term in its *text*: a lexical rank alone is no longer enough,
    because BM25 ranks partial matches (see the override tests at the end of this file)."""
    stack.service._results = [
        _chunk(similarity=0.41, semantic_rank=None, lexical_rank=1,
               text="Escalation follows GRAPSTAGETHREE as notified.")
    ]

    body = client.get("/api/v1/search", params={"q": "GRAPSTAGETHREE"}).json()

    assert body["grounded"] is True
    assert body["count"] == 1


def test_no_results_at_all_is_not_grounded(stack):
    stack.service._results = []

    body = client.get("/api/v1/search", params={"q": "anything"}).json()

    assert body["grounded"] is False
    assert body["best_similarity"] is None


# ==============================================================================
# The lexical override — a regression BM25 introduced
# ==============================================================================

def test_a_weak_lexical_hit_does_not_ground_an_off_topic_question(stack):
    """The bug BM25 introduced. The override accepted *any* lexical hit, which was sound while
    the lexical arm required every term to match. BM25 scores partial matches, so nearly every
    question returned hits and the gate stopped firing: "what is the best chocolate cake recipe"
    came back grounded at 0.434 similarity against a corpus of power-plant filings."""
    stack.service._results = [
        _chunk(similarity=0.43, lexical_rank=1,
               text="Flue gas desulphurisation timelines for thermal plants.")
    ]

    body = client.get(
        "/api/v1/search", params={"q": "what is the best chocolate cake recipe"}
    ).json()

    assert body["grounded"] is False
    assert body["results"] == []


def test_a_literal_match_still_grounds_a_rare_identifier(stack):
    """What the override is actually for: a statute number or code where the embedding is lost
    but the string is plainly in the document."""
    stack.service._results = [
        _chunk(similarity=0.41, lexical_rank=1,
               text="Escalation follows GRAPSTAGETHREE as notified.")
    ]

    body = client.get("/api/v1/search", params={"q": "GRAPSTAGETHREE"}).json()

    assert body["grounded"] is True
    assert body["count"] == 1


def test_the_override_needs_every_informative_word(stack):
    """A passage containing half the question is not a literal match. "penalties" alone must not
    ground "penalties for cake decorating"."""
    stack.service._results = [
        _chunk(similarity=0.40, lexical_rank=1, text="Penalties apply to non-compliant units.")
    ]

    body = client.get("/api/v1/search", params={"q": "penalties for cake decorating"}).json()

    assert body["grounded"] is False


def test_stopwords_do_not_block_a_literal_match(stack):
    """"what are the FGD timelines" must still match a passage saying "FGD timelines" — the
    override checks informative words, not every token."""
    stack.service._results = [
        _chunk(similarity=0.42, lexical_rank=1, text="FGD timelines are set out in the annexure.")
    ]

    body = client.get("/api/v1/search", params={"q": "what are the FGD timelines"}).json()

    assert body["grounded"] is True
