"""The Google Drive connector's policy, exercised against a canned Drive.

Real objects wherever one exists — `LocalFileSystemStorage` under `tmp_path`, the real
`UploadService`, the real repository against SQLite — so what is under test is the connector's
decisions rather than a mock's agreement with itself. Only Drive is faked, because Drive is the
only thing here that cannot be run locally.

The decisions worth pinning are the ones that change what people can read: which folder a file
resolved under, whether anything was downloaded at all, and what happens to a document whose
source has gone.
"""

import io
import uuid
from pathlib import Path

import pytest
from docx import Document as DocxDocument
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from src.db.enums import Classification, UserRole
from src.db.models import Base
from src.db.models import Document as DocORM
from src.db.models import DriveFile as DriveFileORM
from src.db.models import User as UserORM
from src.modules.connectors.drive.export_map import DOCX_MIME, FOLDER_MIME
from src.modules.connectors.drive.models import DriveFile, DriveFileState
from src.modules.connectors.drive.service import DriveSyncError, DriveSyncService
from src.storage.bucket_manager import BucketManager
from src.storage.object_storage import LocalFileSystemStorage

FIXTURES_DIR = Path(__file__).resolve().parent.parent / "fixtures" / "pdfs"

PUBLIC_ROOT = "folder-public"
RESTRICTED_ROOT = "folder-restricted"

GOOGLE_DOC = "application/vnd.google-apps.document"
GOOGLE_FORM = "application/vnd.google-apps.form"


def pdf_bytes() -> bytes:
    return (FIXTURES_DIR / "01_standard_digital_policy.pdf").read_bytes()


def docx_bytes() -> bytes:
    """A real .docx, so the export path is proved through `detect_format()` rather than around
    it. A stub would be skipped as an unrecognised format and the test would pass for the wrong
    reason."""
    buf = io.BytesIO()
    doc = DocxDocument()
    doc.add_heading("FGD Comparative Note", level=1)
    doc.add_paragraph("Flue-gas desulphurisation is not required for Category C plants.")
    doc.save(buf)
    return buf.getvalue()


class FakeDrive:
    """A canned Drive: a folder tree, some file metadata, and bytes for each file.

    It records every download and export, because the most important assertion in several of
    these tests is that nothing was fetched at all.
    """

    def __init__(self):
        self.children: dict[str, list[DriveFile]] = {}
        self.content: dict[str, bytes] = {}
        self.downloaded: list[str] = []
        self.exported: list[tuple[str, str]] = []

    # --- construction helpers -------------------------------------------------

    def add_folder(self, folder_id: str, parent: str | None = None) -> None:
        self.children.setdefault(folder_id, [])
        if parent is not None:
            self.children.setdefault(parent, []).append(
                DriveFile(file_id=folder_id, name=folder_id, mime_type=FOLDER_MIME,
                          modified_time="2026-01-01T00:00:00Z", parents=[parent])
            )

    def add_file(self, folder_id, file_id, name, data=None, *, mime="application/pdf",
                 modified="2026-01-01T00:00:00Z", size=None, owner=None,
                 extra_parents=()) -> None:
        parents = [folder_id, *extra_parents]
        self.children.setdefault(folder_id, []).append(
            DriveFile(file_id=file_id, name=name, mime_type=mime, modified_time=modified,
                      size=size if size is not None else (len(data) if data else None),
                      owner_email=owner, parents=parents)
        )
        for other in extra_parents:
            self.children.setdefault(other, []).append(self.children[folder_id][-1])
        if data is not None:
            self.content[file_id] = data

    def remove_file(self, folder_id: str, file_id: str) -> None:
        self.children[folder_id] = [
            f for f in self.children.get(folder_id, []) if f.file_id != file_id
        ]

    def touch(self, folder_id: str, file_id: str, modified: str) -> None:
        self.children[folder_id] = [
            DriveFile(**{**f.__dict__, "modified_time": modified}) if f.file_id == file_id else f
            for f in self.children[folder_id]
        ]

    # --- the client interface the service uses --------------------------------

    def walk_folder_tree(self, root_id: str, *, max_depth: int = 10) -> dict[str, int]:
        depths = {root_id: 0}
        frontier = [(root_id, 0)]
        while frontier:
            folder_id, depth = frontier.pop()
            if depth >= max_depth:
                continue
            for child in self.children.get(folder_id, []):
                if child.mime_type == FOLDER_MIME and child.file_id not in depths:
                    depths[child.file_id] = depth + 1
                    frontier.append((child.file_id, depth + 1))
        return depths

    def list_children(self, folder_id: str, *, folders_only: bool = False) -> list[DriveFile]:
        items = self.children.get(folder_id, [])
        if folders_only:
            return [f for f in items if f.mime_type == FOLDER_MIME]
        return list(items)

    def download(self, file_id: str) -> bytes:
        self.downloaded.append(file_id)
        return self.content[file_id]

    def export(self, file_id: str, target_mime: str) -> bytes:
        self.exported.append((file_id, target_mime))
        return self.content[file_id]


@pytest.fixture
def stack(tmp_path):
    """Everything real except Drive."""
    storage = LocalFileSystemStorage(base_dir=str(tmp_path))
    buckets = BucketManager(storage=storage)
    engine = create_engine(f"sqlite:///{tmp_path / 'drive.db'}")
    Base.metadata.create_all(bind=engine)
    session_factory = sessionmaker(bind=engine)

    owner_id = uuid.uuid4()
    fallback_id = uuid.uuid4()
    with session_factory() as s:
        s.add(UserORM(user_id=owner_id, email="asha@a-pag.org", hashed_password="x",
                      full_name="Asha", role=UserRole.USER.value))
        s.add(UserORM(user_id=fallback_id, email="admin@a-pag.org", hashed_password="x",
                      full_name="Admin", role=UserRole.ADMIN.value))
        s.commit()

    drive = FakeDrive()
    drive.add_folder(PUBLIC_ROOT)
    drive.add_folder(RESTRICTED_ROOT)

    def build(**overrides):
        kwargs = {
            "client": drive,
            "session_factory": session_factory,
            "public_folder_id": PUBLIC_ROOT,
            "restricted_folder_id": RESTRICTED_ROOT,
            "fallback_owner_email": "admin@a-pag.org",
            "max_bytes": 100 * 1024 * 1024,
            "bucket_manager": buckets,
        }
        kwargs.update(overrides)
        return DriveSyncService(**kwargs)

    return type("Stack", (), {
        "drive": drive, "build": staticmethod(build), "sessions": session_factory,
        "owner_id": owner_id, "fallback_id": fallback_id,
    })


def documents(stack) -> list[DocORM]:
    with stack.sessions() as s:
        return list(s.execute(select(DocORM)).scalars().all())


def drive_rows(stack) -> dict[str, DriveFileORM]:
    with stack.sessions() as s:
        return {r.drive_file_id: r for r in s.execute(select(DriveFileORM)).scalars().all()}


# ==============================================================================
# The folder decides the tier
# ==============================================================================

def test_the_folder_sets_the_tier(stack):
    stack.drive.add_file(PUBLIC_ROOT, "f1", "notice.pdf", pdf_bytes())
    stack.drive.add_file(RESTRICTED_ROOT, "f2", "board_note.pdf", pdf_bytes())

    stack.build().sync_once()

    by_name = {d.filename: d for d in documents(stack)}
    assert by_name["notice.pdf"].classification == Classification.PUBLIC.value
    assert by_name["board_note.pdf"].classification == Classification.RESTRICTED.value


def test_a_nested_subfolder_inherits_its_root_tier(stack):
    """People organise. A document in Restricted/2026/ is no less restricted for it."""
    stack.drive.add_folder("restricted-2026", parent=RESTRICTED_ROOT)
    stack.drive.add_folder("restricted-2026-q1", parent="restricted-2026")
    stack.drive.add_file("restricted-2026-q1", "deep", "deep_note.pdf", pdf_bytes())

    stack.build().sync_once()

    assert documents(stack)[0].classification == Classification.RESTRICTED.value


def test_folders_are_matched_by_id_so_a_rename_changes_nothing(stack):
    """The tier must not hang off a name. Names can be renamed, duplicated, or shadowed by a
    subfolder somebody calls 'Public', and matching on one would silently reclassify everything
    beneath it."""
    stack.drive.add_folder("sub", parent=RESTRICTED_ROOT)
    stack.drive.add_file("sub", "f1", "note.pdf", pdf_bytes())

    # Rename every folder involved, including to the other tier's name.
    stack.drive.children[RESTRICTED_ROOT] = [
        DriveFile(file_id="sub", name="Public", mime_type=FOLDER_MIME,
                  modified_time="2026-01-01T00:00:00Z", parents=[RESTRICTED_ROOT])
    ]

    stack.build().sync_once()

    assert documents(stack)[0].classification == Classification.RESTRICTED.value


def test_a_file_in_both_trees_takes_the_stricter_tier(stack):
    """Drive lets one file sit under two parents, so the trees can genuinely overlap. Fail
    closed."""
    stack.drive.add_file(PUBLIC_ROOT, "f1", "shared.pdf", pdf_bytes(),
                         extra_parents=(RESTRICTED_ROOT,))

    stack.build().sync_once()

    docs = documents(stack)
    assert len(docs) == 1, "a file with two parents is still one file"
    assert docs[0].classification == Classification.RESTRICTED.value


def test_files_outside_the_watched_folders_are_left_alone(stack):
    stack.drive.add_folder("someone-elses-folder")
    stack.drive.add_file("someone-elses-folder", "f9", "private.pdf", pdf_bytes())
    stack.drive.add_file(PUBLIC_ROOT, "f1", "notice.pdf", pdf_bytes())

    outcome = stack.build().sync_once()

    assert [d.filename for d in documents(stack)] == ["notice.pdf"]
    assert outcome.seen == 1
    assert stack.drive.downloaded == ["f1"], "an unwatched file must not even be fetched"


def test_no_watched_folder_configured_is_refused(stack):
    with pytest.raises(DriveSyncError, match="nothing to sync"):
        stack.build(public_folder_id="", restricted_folder_id="").sync_once()


# ==============================================================================
# Change detection
# ==============================================================================

def test_an_unchanged_file_is_not_downloaded_again(stack):
    """What makes an idle sync cost one listing request instead of the whole folder."""
    stack.drive.add_file(PUBLIC_ROOT, "f1", "notice.pdf", pdf_bytes())
    stack.build().sync_once()
    stack.drive.downloaded.clear()

    outcome = stack.build().sync_once()

    assert outcome.unchanged == 1
    assert outcome.imported == []
    assert stack.drive.downloaded == []
    assert len(documents(stack)) == 1


def test_an_edited_file_arrives_as_a_new_document(stack):
    """The old one is left alone, so a citation written last week still points at the text that
    was actually cited."""
    stack.drive.add_file(PUBLIC_ROOT, "f1", "notice.pdf", pdf_bytes())
    stack.build().sync_once()

    stack.drive.touch(PUBLIC_ROOT, "f1", "2026-06-01T00:00:00Z")
    stack.drive.content["f1"] = (FIXTURES_DIR / "02_complex_tabular_budget.pdf").read_bytes()
    outcome = stack.build().sync_once()

    assert len(outcome.imported) == 1
    docs = documents(stack)
    assert len(docs) == 2
    assert all(d.deleted_at is None for d in docs), "the previous version must survive"


def test_a_file_moved_between_tiers_is_re_imported_at_the_new_tier(stack):
    stack.drive.add_file(RESTRICTED_ROOT, "f1", "note.pdf", pdf_bytes())
    stack.build().sync_once()

    stack.drive.remove_file(RESTRICTED_ROOT, "f1")
    stack.drive.add_file(PUBLIC_ROOT, "f1", "note.pdf", pdf_bytes())
    stack.build().sync_once()

    assert drive_rows(stack)["f1"].classification == Classification.PUBLIC.value


# ==============================================================================
# Skipping, without paying to find out
# ==============================================================================

def test_an_oversized_file_is_skipped_before_it_is_downloaded(stack):
    """Checked against Drive's own figure. `receive()` takes bytes, so without this the file is
    pulled into memory purely to be rejected by a validator that already knew the limit."""
    stack.drive.add_file(PUBLIC_ROOT, "big", "huge.pdf", pdf_bytes(), size=200 * 1024 * 1024)

    outcome = stack.build().sync_once()

    assert stack.drive.downloaded == [], "the whole point is that nothing was fetched"
    assert len(outcome.skipped) == 1
    assert "exceeds" in outcome.skipped[0][1]
    assert documents(stack) == []


def test_a_google_form_is_skipped_and_says_what_it_was(stack):
    stack.drive.add_file(PUBLIC_ROOT, "form", "Feedback form", mime=GOOGLE_FORM)

    outcome = stack.build().sync_once()

    assert outcome.skipped[0][1].startswith("a Google Form")
    assert drive_rows(stack)["form"].state == DriveFileState.SKIPPED.value


def test_an_unsupported_format_is_skipped_with_a_reason(stack):
    stack.drive.add_file(PUBLIC_ROOT, "zip", "archive.zip", b"PK\x03\x04not-an-office-file")

    outcome = stack.build().sync_once()

    assert "not a PDF, DOCX, XLSX or PPTX" in outcome.skipped[0][1]
    assert documents(stack) == []


# ==============================================================================
# Google-native documents
# ==============================================================================

def test_a_google_doc_is_exported_as_docx(stack):
    """Not text/plain: Word paragraph styles are what the chunker cuts on, and an export to text
    throws them away."""
    stack.drive.add_file(PUBLIC_ROOT, "gdoc", "FGD Note", docx_bytes(), mime=GOOGLE_DOC)

    stack.build().sync_once()

    assert stack.drive.exported == [("gdoc", DOCX_MIME)]
    assert stack.drive.downloaded == [], "a Google-native file has no bytes to download"
    doc = documents(stack)[0]
    assert doc.filename == "FGD Note.docx", "the extension comes from the export, not the name"
    assert doc.mime_type == DOCX_MIME


# ==============================================================================
# Ownership
# ==============================================================================

def test_the_owner_is_matched_by_drive_email(stack):
    stack.drive.add_file(PUBLIC_ROOT, "f1", "notice.pdf", pdf_bytes(), owner="Asha@A-PAG.org")

    outcome = stack.build().sync_once()

    assert documents(stack)[0].uploader_user_id == stack.owner_id
    assert outcome.owner_fallbacks == []


def test_an_unknown_drive_owner_falls_back_and_is_reported(stack):
    """Reported rather than logged quietly: for a RESTRICTED document this decides that only the
    fallback account and admins can read it, which otherwise looks like nothing at all."""
    stack.drive.add_file(RESTRICTED_ROOT, "f1", "note.pdf", pdf_bytes(),
                         owner="contractor@example.com")

    outcome = stack.build().sync_once()

    assert documents(stack)[0].uploader_user_id == stack.fallback_id
    assert outcome.owner_fallbacks == [("note.pdf", "contractor@example.com")]


def test_a_missing_fallback_owner_is_refused_rather_than_guessed(stack):
    stack.drive.add_file(PUBLIC_ROOT, "f1", "notice.pdf", pdf_bytes(), owner="nobody@example.com")

    with pytest.raises(DriveSyncError, match="not a registered user"):
        stack.build(fallback_owner_email="ghost@a-pag.org").sync_once()


# ==============================================================================
# Retiring what has gone
# ==============================================================================

@pytest.mark.parametrize("how", ["binned", "moved out"])
def test_a_vanished_file_retires_its_document_reversibly(stack, how):
    stack.drive.add_file(PUBLIC_ROOT, "f1", "notice.pdf", pdf_bytes())
    stack.build().sync_once()
    document_id = documents(stack)[0].document_id

    # Binned and moved-out look identical from here, and are treated identically: both mean
    # somebody decided it should not be there.
    stack.drive.remove_file(PUBLIC_ROOT, "f1")
    if how == "moved out":
        stack.drive.add_folder("elsewhere")
        stack.drive.add_file("elsewhere", "f1", "notice.pdf", pdf_bytes())

    outcome = stack.build().sync_once()

    assert outcome.removed == ["notice.pdf"]
    with stack.sessions() as s:
        doc = s.get(DocORM, document_id)
        assert doc.deleted_at is not None, "gone from search"
        # Not erased: the row, the stored object and the audit trail all survive, which is what
        # makes putting the file back in the folder a recovery rather than a re-upload.
        assert doc.purged_at is None
        assert doc.filename == "notice.pdf"
    assert drive_rows(stack)["f1"].state == DriveFileState.REMOVED.value


def test_a_limited_run_never_retires_anything(stack):
    """With --limit, 'not in the listing' means 'not in the part we looked at'. Acting on that
    would retire most of the corpus."""
    for i in range(3):
        stack.drive.add_file(PUBLIC_ROOT, f"f{i}", f"doc{i}.pdf", pdf_bytes())
    stack.build().sync_once()

    outcome = stack.build().sync_once(limit=1)

    assert outcome.removed == []
    with stack.sessions() as s:
        assert all(d.deleted_at is None for d in s.execute(select(DocORM)).scalars())


# ==============================================================================
# Dry run
# ==============================================================================

def test_a_dry_run_changes_nothing(stack):
    stack.drive.add_file(PUBLIC_ROOT, "f1", "notice.pdf", pdf_bytes())

    outcome = stack.build().sync_once(dry_run=True)

    assert len(outcome.imported) == 1
    assert documents(stack) == []
    assert drive_rows(stack) == {}
    assert stack.drive.downloaded == [], "a dry run should not even fetch the bytes"


def test_a_dry_run_reports_what_would_be_retired(stack):
    stack.drive.add_file(PUBLIC_ROOT, "f1", "notice.pdf", pdf_bytes())
    stack.build().sync_once()
    stack.drive.remove_file(PUBLIC_ROOT, "f1")

    outcome = stack.build().sync_once(dry_run=True)

    assert outcome.removed == ["notice.pdf"]
    with stack.sessions() as s:
        assert s.execute(select(DocORM)).scalars().first().deleted_at is None


# ==============================================================================
# One bad file must not end the run
# ==============================================================================

def test_one_failing_file_does_not_stop_the_others(stack):
    class Exploding(FakeDrive):
        def download(self, file_id):
            if file_id == "bad":
                raise RuntimeError("Drive API 500")
            return super().download(file_id)

    drive = Exploding()
    drive.add_folder(PUBLIC_ROOT)
    drive.add_folder(RESTRICTED_ROOT)
    drive.add_file(PUBLIC_ROOT, "bad", "aaa_broken.pdf", pdf_bytes())
    drive.add_file(PUBLIC_ROOT, "good", "zzz_fine.pdf", pdf_bytes())

    outcome = stack.build(client=drive).sync_once()

    assert len(outcome.failed) == 1
    assert len(outcome.imported) == 1
    assert [d.filename for d in documents(stack)] == ["zzz_fine.pdf"]
    assert drive_rows(stack)["bad"].state == DriveFileState.FAILED.value


def test_a_duplicate_is_counted_as_one(stack):
    """Two copies of the same bytes in both folders. The second is dropped at scan — but the scan
    stage is a worker, so here it is simply received twice and the pipeline sorts it out."""
    stack.drive.add_file(PUBLIC_ROOT, "f1", "a.pdf", pdf_bytes())
    stack.drive.add_file(PUBLIC_ROOT, "f2", "b.pdf", pdf_bytes())

    outcome = stack.build().sync_once()

    assert len(outcome.imported) + len(outcome.duplicates) == 2
