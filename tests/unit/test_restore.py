"""Restoring a deleted document, and finding the deleted documents you may restore.

Run through the real endpoints and the SQL repository (on SQLite), and every result read back
from a fresh session: the in-memory repository makes a write visible the moment it is made,
which is how a delete that never persisted passed every test for ten days (`KNOWN_DEBTS.md` #43).
"""

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from starlette.testclient import TestClient

from src.api.v1.ingestion import get_document_repository
from src.api.v1.router import app
from src.db.engine import get_db
from src.db.enums import UserRole
from src.db.models import AuditLog, Base
from src.db.models import Document as DocORM
from src.db.models import DriveFile as DriveFileORM
from src.db.models import DriveFileVersion as VersionORM
from src.db.models import User as UserORM
from src.modules.auth.dependencies import get_current_user
from src.modules.document_pipeline.formats import PDF_MIME

client = TestClient(app)


def _user(role=UserRole.USER.value, email=None):
    return SimpleNamespace(user_id=uuid.uuid4(), role=role,
                           email=email or f"{uuid.uuid4().hex[:8]}@a-pag.org", is_active=True)


@pytest.fixture
def world(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'restore.db'}")
    Base.metadata.create_all(bind=engine)
    owner, other, admin = _user(), _user(), _user(UserRole.ADMIN.value)
    with Session(engine) as s:
        for u in (owner, other, admin):
            s.add(UserORM(user_id=u.user_id, email=u.email, hashed_password="x",
                          full_name="x", role=u.role))
        s.commit()

    as_user = {"current": owner}

    def _get_db():
        with Session(engine) as session:
            yield session

    saved = dict(app.dependency_overrides)
    app.dependency_overrides.pop(get_document_repository, None)  # the real, SQL-backed one
    app.dependency_overrides[get_db] = _get_db
    app.dependency_overrides[get_current_user] = lambda: as_user["current"]

    def add(name, *, owner_id=None, deleted=False, purged=False, status="LIVE",
            tier="PUBLIC", sha=None):
        doc_id = uuid.uuid4()
        now = datetime.now(UTC)
        with Session(engine) as s:
            s.add(DocORM(document_id=doc_id, filename=name, file_size=10, status=status,
                         classification=tier, mime_type=PDF_MIME, sha256=sha,
                         uploader_user_id=owner_id or owner.user_id,
                         deleted_at=now if deleted or purged else None,
                         purged_at=now if purged else None))
            s.commit()
        return doc_id

    def from_drive(doc_id, file_state):
        with Session(engine) as s:
            fid = uuid.uuid4().hex
            s.add(DriveFileORM(drive_file_id=fid, document_id=doc_id, state=file_state,
                               drive_name="x.pdf", classification="PUBLIC"))
            s.add(VersionORM(drive_file_id=fid, document_id=doc_id, state="CURRENT"))
            s.commit()

    def deleted_at(doc_id):
        with Session(engine) as s:
            return s.get(DocORM, doc_id).deleted_at

    def events(doc_id, kind):
        with Session(engine) as s:
            return s.execute(select(AuditLog).where(
                AuditLog.document_id == doc_id, AuditLog.event_type == kind)).scalars().all()

    try:
        yield SimpleNamespace(engine=engine, owner=owner, other=other, admin=admin, as_user=as_user,
                              add=add, from_drive=from_drive, deleted_at=deleted_at, events=events)
    finally:
        app.dependency_overrides.clear()
        app.dependency_overrides.update(saved)


def _restore(doc_id):
    return client.post(f"/api/v1/documents/{doc_id}/restore")


# ------------------------------------------------------------------ restore

def test_the_owner_restores_a_deleted_document(world):
    doc = world.add("minutes.pdf", deleted=True)

    r = _restore(doc)

    assert r.status_code == 200, r.text
    assert world.deleted_at(doc) is None, "back in search, read from a fresh session"
    [row] = world.events(doc, "DOCUMENT_RESTORED")
    assert row.details["restored_by"] == str(world.owner.user_id)


def test_an_admin_may_restore_anyones(world):
    doc = world.add("minutes.pdf", deleted=True)
    world.as_user["current"] = world.admin
    assert _restore(doc).status_code == 200


def test_someone_else_may_not_restore_a_public_document_they_can_see(world):
    doc = world.add("minutes.pdf", deleted=True)
    world.as_user["current"] = world.other
    assert _restore(doc).status_code == 403
    assert world.deleted_at(doc) is not None


def test_a_restricted_document_is_not_found_for_someone_else(world):
    """404, not 403: confirming it exists would leak it."""
    doc = world.add("board.pdf", deleted=True, tier="RESTRICTED")
    world.as_user["current"] = world.other
    assert _restore(doc).status_code == 404


def test_a_document_that_is_not_deleted_is_refused(world):
    assert _restore(world.add("minutes.pdf")).status_code == 409


def test_a_permanently_erased_document_cannot_come_back(world):
    assert _restore(world.add("minutes.pdf", purged=True)).status_code == 410


def test_an_older_version_of_an_edited_file_is_not_restorable(world):
    doc = world.add("v1.pdf", deleted=True, status="SUPERSEDED")
    assert _restore(doc).status_code == 409


def test_a_file_removed_from_drive_comes_back_through_drive(world):
    doc = world.add("drive.pdf", deleted=True)
    world.from_drive(doc, "REMOVED")

    r = _restore(doc)

    assert r.status_code == 409
    assert "FROM_DRIVE" in r.json()["detail"]
    assert world.deleted_at(doc) is not None


def test_a_drive_document_deleted_in_the_app_is_restored_in_the_app(world):
    """Its file never left the folder, so the sync sees nothing to do; the app is the only way
    back."""
    doc = world.add("drive.pdf", deleted=True)
    world.from_drive(doc, "IMPORTED")
    assert _restore(doc).status_code == 200
    assert world.deleted_at(doc) is None


# ------------------------------------------------------------- the list

def _deleted_names():
    r = client.get("/api/v1/documents/deleted")
    assert r.status_code == 200, r.text
    return {d["filename"]: d for d in r.json()["documents"]}


def test_you_see_your_own_deleted_documents_and_nothing_else(world):
    world.add("mine.pdf", deleted=True)
    world.add("mine_live.pdf")
    world.add("theirs.pdf", deleted=True, owner_id=world.other.user_id)
    world.add("erased.pdf", purged=True)
    world.add("old_version.pdf", deleted=True, status="SUPERSEDED")

    assert set(_deleted_names()) == {"mine.pdf"}


def test_an_admin_sees_everyones(world):
    world.add("mine.pdf", deleted=True)
    world.add("theirs.pdf", deleted=True, owner_id=world.other.user_id)
    world.as_user["current"] = world.admin
    assert set(_deleted_names()) == {"mine.pdf", "theirs.pdf"}


def test_the_list_says_which_ones_come_back_through_drive(world):
    gone = world.add("gone_from_drive.pdf", deleted=True)
    world.from_drive(gone, "REMOVED")
    world.add("uploaded.pdf", deleted=True)

    listed = _deleted_names()

    assert listed["gone_from_drive.pdf"]["from_drive"] is True
    assert listed["uploaded.pdf"]["from_drive"] is False


# --------------------------------------------- re-uploading a deleted file

def _refused_reupload(world, canonical):
    dup = world.add("again.pdf", status="DUPLICATE")
    with Session(world.engine) as s:
        s.add(AuditLog(document_id=dup, event_type="DOCUMENT_REJECTED",
                       details={"reason": "DUPLICATE", "canonical_document_id": str(canonical)}))
        s.commit()
    return client.get(f"/api/v1/documents/{dup}/status").json()["duplicate_of"]


def test_a_reupload_of_a_deleted_file_says_so_and_offers_restore(world):
    original = world.add("minutes.pdf", deleted=True, sha="a" * 64)

    found = _refused_reupload(world, original)

    assert found["document_id"] == str(original)
    assert found["deleted"] is True
    assert found["can_restore"] is True


def test_a_reupload_of_a_live_file_points_at_it_without_offering_restore(world):
    original = world.add("minutes.pdf", sha="b" * 64)
    found = _refused_reupload(world, original)
    assert found["deleted"] is False
    assert found["can_restore"] is False


def test_a_reupload_matching_someone_elses_restricted_file_reveals_nothing(world):
    original = world.add("board.pdf", deleted=True, tier="RESTRICTED",
                         owner_id=world.other.user_id, sha="c" * 64)
    assert _refused_reupload(world, original) is None
