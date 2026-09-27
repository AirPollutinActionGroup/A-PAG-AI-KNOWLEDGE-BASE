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
from src.modules.retrieval.models import RetrievedChunk

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

    def search(self, db, query, *, user_id, is_admin, limit):
        self.calls.append(
            {"query": query, "user_id": user_id, "is_admin": is_admin, "limit": limit}
        )
        return self._results


def _chunk(text="District targets for 2027.", **kw):
    defaults = {
        "chunk_id": uuid.uuid4(), "document_id": uuid.uuid4(), "filename": "directive.pdf",
        "text": text, "page_number": 4, "section_heading": "3. Obligations",
        "is_table": False, "score": 0.82,
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
    assert r.json() == {"query": "nothing matches this", "count": 0, "results": []}


def test_the_echoed_query_is_the_trimmed_one(stack):
    body = client.get("/api/v1/search", params={"q": "  targets  "}).json()
    assert body["query"] == "targets"
