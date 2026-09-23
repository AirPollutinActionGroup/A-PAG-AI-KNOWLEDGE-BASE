"""Reclassification and document_date tests.

A document's sensitivity tier is chosen by the uploader at upload and defaults to PUBLIC, so the
pipeline never blocks on a human. The realistic failure mode is therefore a forgotten RESTRICTED
flag — a confidential document sitting org-wide-visible until someone notices. These tests cover
the endpoint that fixes that, and the audit trail that makes the fix accountable.

Self-contained: builds its own TestClient overrides rather than borrowing the module-level
fixtures in test_ingestion_pipeline.py, and restores whatever was there on teardown so the two
files can run in either order.
"""

import uuid
from datetime import date
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from starlette.testclient import TestClient

from src.api.v1.ingestion import get_document_repository
from src.api.v1.router import app
from src.db.engine import get_db
from src.db.enums import AuditEventType, UserRole
from src.db.models import AuditLog, Base
from src.modules.auth.dependencies import get_current_user
from src.modules.document_pipeline.formats import PDF_MIME
from src.modules.document_pipeline.models import Classification, DocumentStatus
from src.modules.document_pipeline.models import Document as DocumentDTO
from src.modules.document_pipeline.repository import InMemoryDocumentRepository

client = TestClient(app)


class _FakeUser:
    """Stand-in for src.db.models.User — carries the identity fields the endpoint reads."""

    def __init__(self, role: str = UserRole.ADMIN.value, user_id: uuid.UUID | None = None):
        self.user_id = user_id or uuid.uuid4()
        self.role = role
        self.is_active = True
        self.email = f"{self.user_id}@a-pag.org"


@pytest.fixture
def stack(tmp_path):
    """Isolated repository + SQLite-backed audit session, wired into the API."""
    repo = InMemoryDocumentRepository()
    user = _FakeUser()
    engine = create_engine(f"sqlite:///{tmp_path / 'reclassify.db'}")
    Base.metadata.create_all(bind=engine)

    def _get_db():
        with Session(engine) as session:
            yield session

    # Saved and restored, not popped: test_ingestion_pipeline.py installs its own overrides at
    # module import, and this file sorts ahead of it. Popping unconditionally left those tests
    # falling through to a real Postgres connection.
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


def seed(stack, classification=Classification.PUBLIC, owner_id=None, **kwargs):
    """Creates a document as upload would have left it."""
    return stack.repo.create(
        DocumentDTO(
            id=uuid.uuid4(),
            filename="directive.pdf",
            size=2048,
            mime_type=PDF_MIME,
            status=kwargs.pop("status", DocumentStatus.AWAITING_CLASSIFICATION),
            classification=classification,
            owner_id=owner_id if owner_id is not None else stack.user.user_id,
            checksum="c" * 64,
            raw_path=f"apag-raw/{'c' * 64}.pdf",
            **kwargs,
        )
    )


def reclassify(doc_id, **body):
    return client.post(f"/api/v1/documents/{doc_id}/classify", json=body)


# ==============================================================================
# Changing a tier
# ==============================================================================

def test_tier_can_be_widened_to_restricted(stack):
    """The failure mode this endpoint exists for: a document uploaded without the RESTRICTED
    flag that turns out to be confidential."""
    doc = seed(stack, classification=Classification.PUBLIC)

    resp = reclassify(doc.id, classification="RESTRICTED", reason="Contains draft positions")

    assert resp.status_code == 200
    assert resp.json()["classification"] == "RESTRICTED"
    assert resp.json()["previous_classification"] == "PUBLIC"
    assert stack.repo.get_by_id(doc.id).classification == Classification.RESTRICTED


def test_tier_can_be_relaxed_to_public(stack):
    doc = seed(stack, classification=Classification.RESTRICTED)

    resp = reclassify(doc.id, classification="PUBLIC", reason="Published by the ministry")

    assert resp.status_code == 200
    assert stack.repo.get_by_id(doc.id).classification == Classification.PUBLIC


def test_reclassification_does_not_disturb_pipeline_status(stack):
    """A tier says who may read a document, not how far through processing it is. A document
    mid-pipeline must keep its place in the queue."""
    doc = seed(stack, status=DocumentStatus.EXTRACTED)

    reclassify(doc.id, classification="RESTRICTED")

    assert stack.repo.get_by_id(doc.id).status == DocumentStatus.EXTRACTED


def test_reclassification_is_attributable_in_the_audit_log(stack):
    """Someone will ask months later why a document became visible, or stopped being. The
    audit row is where they will look, so it has to carry who, from what, to what, and why."""
    doc = seed(stack, classification=Classification.PUBLIC)

    reclassify(doc.id, classification="RESTRICTED", reason="Legal flagged it")

    with Session(stack.engine) as session:
        event = session.query(AuditLog).filter(AuditLog.document_id == doc.id).one()

    assert event.event_type == AuditEventType.DOCUMENT_RECLASSIFIED.value
    assert event.details["reclassified_by"] == str(stack.user.user_id)
    assert event.details["old_tier"] == "PUBLIC"
    assert event.details["new_tier"] == "RESTRICTED"
    assert event.details["reason"] == "Legal flagged it"


# ==============================================================================
# Guards
# ==============================================================================

def test_reclassifying_to_the_same_tier_is_rejected(stack):
    """A no-op change would write a misleading audit row implying something happened."""
    doc = seed(stack, classification=Classification.PUBLIC)

    resp = reclassify(doc.id, classification="PUBLIC")

    assert resp.status_code == 409
    with Session(stack.engine) as session:
        assert session.query(AuditLog).filter(AuditLog.document_id == doc.id).count() == 0


def test_tier_is_required(stack):
    doc = seed(stack)
    assert reclassify(doc.id).status_code == 422


def test_non_owner_non_admin_cannot_reclassify(stack):
    """Same bar as delete: this changes who can read someone else's document."""
    doc = seed(stack, owner_id=uuid.uuid4())
    app.dependency_overrides[get_current_user] = lambda: _FakeUser(role=UserRole.USER.value)

    resp = reclassify(doc.id, classification="RESTRICTED")

    assert resp.status_code == 403
    assert stack.repo.get_by_id(doc.id).classification == Classification.PUBLIC


def test_purged_document_cannot_be_reclassified(stack):
    """A tombstone has no bytes left for a tier to govern."""
    doc = seed(stack)
    stack.repo.purge(doc.id)

    assert reclassify(doc.id, classification="RESTRICTED").status_code == 410


def test_unknown_document_returns_404(stack):
    assert reclassify(uuid.uuid4(), classification="RESTRICTED").status_code == 404


# ==============================================================================
# document_date — supplied at upload, kept distinct from created_at
# ==============================================================================

def test_document_date_survives_reclassification(stack):
    """Reclassification touches the tier and nothing else."""
    doc = seed(stack, document_date=date(2019, 3, 14))

    reclassify(doc.id, classification="RESTRICTED")

    assert stack.repo.get_by_id(doc.id).document_date == date(2019, 3, 14)


def test_document_date_defaults_to_none(stack):
    """Much of an archive has no reliably discoverable date. Null is honest; a guess would
    poison any later attempt to weight retrieval by recency."""
    doc = seed(stack)
    assert stack.repo.get_by_id(doc.id).document_date is None
