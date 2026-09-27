"""List and search must be scoped in the query, not filtered afterwards.

The endpoints used to fetch a page and then drop the rows the caller could not see. Three things
went wrong with that, and each has a test here:

1. `total` was the **unfiltered** count, so the response told the caller how many restricted
   documents exist — a number they are not entitled to.
2. Invisible rows **consumed slots** in the page, so `limit=10` could return three documents while
   more visible ones waited on the next page.
3. Pagination was incoherent: `offset` counted rows the caller could not see, so paging forward
   skipped visible documents unpredictably.

These run against `InMemoryDocumentRepository`, whose implementation applies the same
`can_view()` the SQL clause is proven equivalent to in `test_access_rule.py`.
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
from src.db.enums import Classification, DocumentStatus, UserRole
from src.db.models import Base
from src.modules.auth.dependencies import get_current_user
from src.modules.document_pipeline.models import Document as DocumentDTO
from src.modules.document_pipeline.repository import InMemoryDocumentRepository

client = TestClient(app)


class _FakeUser:
    def __init__(self, role=UserRole.USER.value, user_id=None):
        self.user_id = user_id or uuid.uuid4()
        self.role = role
        self.is_active = True
        self.email = f"{self.user_id}@a-pag.org"


@pytest.fixture
def stack(tmp_path):
    repo = InMemoryDocumentRepository()
    user = _FakeUser()
    # A real session: the endpoints resolve uploader emails through it, so a stub is not enough.
    engine = create_engine(f"sqlite:///{tmp_path / 'perm.db'}")
    Base.metadata.create_all(bind=engine)

    def _get_db():
        with Session(engine) as session:
            yield session

    # Saved and restored rather than popped: other modules install overrides at import time.
    previous = {
        dep: app.dependency_overrides.get(dep)
        for dep in (get_document_repository, get_current_user, get_db)
    }
    app.dependency_overrides[get_document_repository] = lambda: repo
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_db] = _get_db
    try:
        yield SimpleNamespace(repo=repo, user=user)
    finally:
        for dep, override in previous.items():
            if override is None:
                app.dependency_overrides.pop(dep, None)
            else:
                app.dependency_overrides[dep] = override


def seed(stack, classification=Classification.PUBLIC, owner_id=None, title="Air quality report"):
    return stack.repo.create(DocumentDTO(
        id=uuid.uuid4(), filename=f"{uuid.uuid4().hex[:8]}.pdf", size=1024,
        status=DocumentStatus.LIVE, classification=classification,
        owner_id=owner_id, title=title, checksum=uuid.uuid4().hex,
    ))


def _list(**params):
    return client.get("/api/v1/documents", params=params).json()


def _search(q="air", **params):
    return client.get("/api/v1/documents/search", params={"q": q, **params}).json()


# ==============================================================================
# The count leak
# ==============================================================================

def test_total_counts_only_visible_documents(stack):
    """The bug: `total` was the unfiltered count, so the response disclosed how many restricted
    documents exist even though none of them were returned."""
    stranger = uuid.uuid4()
    seed(stack)
    for _ in range(7):
        seed(stack, Classification.RESTRICTED, owner_id=stranger)

    body = _list(limit=50)

    assert body["total"] == 1, "total must not count documents the caller cannot see"
    assert len(body["documents"]) == 1


def test_search_total_counts_only_visible_documents(stack):
    stranger = uuid.uuid4()
    seed(stack, title="Air quality directive")
    for _ in range(4):
        seed(stack, Classification.RESTRICTED, owner_id=stranger, title="Air quality directive")

    body = _search("air")

    assert body["total"] == 1
    assert len(body["documents"]) == 1


# ==============================================================================
# Slot consumption
# ==============================================================================

def test_a_full_page_is_returned_despite_restricted_documents(stack):
    """The bug: restricted rows were fetched into the page and then dropped, so a `limit=5` could
    return one document while four visible ones sat on the next page."""
    stranger = uuid.uuid4()
    # Interleaved so a naive fetch-then-filter would pull mostly restricted rows into page one.
    for _ in range(5):
        seed(stack, Classification.RESTRICTED, owner_id=stranger)
        seed(stack)

    body = _list(limit=5)

    assert len(body["documents"]) == 5, "the page must be filled with visible documents"
    assert body["total"] == 5


def test_pagination_walks_every_visible_document_exactly_once(stack):
    """The consequence of scoping in the query: offsets count visible rows, so paging forward
    covers the set without gaps or repeats."""
    stranger = uuid.uuid4()
    for _ in range(6):
        seed(stack, Classification.RESTRICTED, owner_id=stranger)
        seed(stack)

    seen: list[str] = []
    for offset in (0, 2, 4):
        seen += [d["id"] for d in _list(limit=2, offset=offset)["documents"]]

    assert len(seen) == 6
    assert len(set(seen)) == 6, "no document should appear on two pages"
    assert _list(limit=50)["total"] == 6


# ==============================================================================
# The rule itself, through the endpoints
# ==============================================================================

def test_owner_sees_their_own_restricted_document(stack):
    seed(stack, Classification.RESTRICTED, owner_id=stack.user.user_id)

    body = _list()

    assert body["total"] == 1
    assert len(body["documents"]) == 1


def test_admin_sees_every_restricted_document(stack):
    app.dependency_overrides[get_current_user] = lambda: _FakeUser(role=UserRole.ADMIN.value)
    for _ in range(3):
        seed(stack, Classification.RESTRICTED, owner_id=uuid.uuid4())

    body = _list()

    assert body["total"] == 3


def test_restricted_documents_are_absent_from_search_results(stack):
    stranger = uuid.uuid4()
    seed(stack, Classification.RESTRICTED, owner_id=stranger, title="Air quality secret")

    body = _search("air")

    assert body["documents"] == []
    assert body["total"] == 0


def test_owner_can_search_their_own_restricted_document(stack):
    seed(stack, Classification.RESTRICTED, owner_id=stack.user.user_id, title="Air quality private")

    body = _search("air")

    assert body["total"] == 1


# ==============================================================================
# The scoping arguments cannot be forgotten
# ==============================================================================

def test_repository_refuses_to_list_without_a_viewer():
    """`viewer_id`/`viewer_is_admin` are required keyword arguments with no default, so a caller
    that forgets them fails loudly rather than defaulting to "everything" or "nothing"."""
    repo = InMemoryDocumentRepository()

    with pytest.raises(TypeError):
        repo.list_paginated(limit=10)

    with pytest.raises(TypeError):
        repo.search("air", limit=10)
