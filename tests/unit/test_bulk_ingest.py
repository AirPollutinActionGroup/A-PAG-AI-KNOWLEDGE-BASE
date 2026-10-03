"""Bulk ingest — discovery and per-file outcome handling.

The behaviour worth pinning is what happens to an archive that isn't uniform, because a real one
never is: lock files beside open documents, a .docx that is actually a spreadsheet, something
unreadable, something empty. The rule throughout is that one bad file must not end the run — a
few thousand documents will contain something surprising, and discovering at file 1,900 that
nothing since file 12 was ingested is the failure this is designed against.
"""

import uuid
from types import SimpleNamespace

import pytest

import bulk_ingest
from bulk_ingest import Tally, discover, ingest_one
from src.modules.document_pipeline.formats import PDF_MIME
from src.modules.document_pipeline.models import Classification, UploadRequest

PDF_BYTES = b"%PDF-1.7\n%\xe2\xe3\xcf\xd3\ntrailer\n%%EOF\n"


class FakeService:
    """Records what reached UploadService.receive(), and can be told to fail."""

    def __init__(self, fail_on: str | None = None, duplicate: bool = False):
        self.calls: list[dict] = []
        self._fail_on = fail_on
        self._duplicate = duplicate

    def receive(self, *, filename, data, request_meta, mime_type):
        if self._fail_on and self._fail_on in filename:
            raise RuntimeError("storage unavailable")
        self.calls.append({"filename": filename, "size": len(data), "mime": mime_type,
                           "meta": request_meta})
        return SimpleNamespace(was_duplicate=self._duplicate, document_id=uuid.uuid4())


@pytest.fixture
def meta():
    return UploadRequest(
        classification=Classification.PUBLIC,
        owner_id=uuid.uuid4(),
        upload_batch_id=uuid.uuid4(),
    )


# ==============================================================================
# Discovery
# ==============================================================================

def test_discovery_recurses_into_subdirectories(tmp_path):
    """Archives arrive as nested folders, not a flat list."""
    (tmp_path / "a").mkdir()
    (tmp_path / "a" / "b").mkdir()
    (tmp_path / "top.pdf").write_bytes(PDF_BYTES)
    (tmp_path / "a" / "mid.docx").write_bytes(b"PK\x03\x04")
    (tmp_path / "a" / "b" / "deep.xlsx").write_bytes(b"PK\x03\x04")

    assert {p.name for p in discover(tmp_path)} == {"top.pdf", "mid.docx", "deep.xlsx"}


def test_discovery_ignores_office_lock_files(tmp_path):
    """Word and Excel write `~$name.docx` beside a document that is currently open. They are
    valid ZIPs, so without this they are ingested as mangled copies of whatever a colleague
    happened to have open when the archive was copied."""
    (tmp_path / "report.docx").write_bytes(b"PK\x03\x04")
    (tmp_path / "~$report.docx").write_bytes(b"PK\x03\x04")

    assert [p.name for p in discover(tmp_path)] == ["report.docx"]


def test_discovery_ignores_other_file_types(tmp_path):
    """The suffix filter only avoids reading every file in a tree to find out it is not a
    document; content still decides. Without it a folder of videos is read into memory one by
    one."""
    (tmp_path / "keep.pdf").write_bytes(PDF_BYTES)
    for junk in ("notes.txt", "clip.mp4", "archive.zip", "photo.jpg"):
        (tmp_path / junk).write_bytes(b"x")

    assert [p.name for p in discover(tmp_path)] == ["keep.pdf"]


def test_discovery_accepts_a_single_file(tmp_path):
    one = tmp_path / "solo.pdf"
    one.write_bytes(PDF_BYTES)
    assert discover(one) == [one]


def test_discovery_is_ordered(tmp_path):
    """A stable order makes a resumed run progress through the archive the same way, so
    '[1900/4000]' means the same thing on the second attempt."""
    for name in ("c.pdf", "a.pdf", "b.pdf"):
        (tmp_path / name).write_bytes(PDF_BYTES)

    assert [p.name for p in discover(tmp_path)] == ["a.pdf", "b.pdf", "c.pdf"]


# ==============================================================================
# Per-file outcomes
# ==============================================================================

def test_a_valid_file_is_queued(tmp_path, meta):
    path = tmp_path / "policy.pdf"
    path.write_bytes(PDF_BYTES)
    service, tally = FakeService(), Tally()

    ingest_one(service, path, meta, tally)

    assert tally.queued == [str(path)]
    assert service.calls[0]["mime"] == PDF_MIME


def test_format_is_decided_by_content_not_extension(tmp_path, meta):
    """The same rule the API boundary uses: a .docx that is really a PDF is stored as a PDF."""
    path = tmp_path / "mislabelled.docx"
    path.write_bytes(PDF_BYTES)
    service, tally = FakeService(), Tally()

    ingest_one(service, path, meta, tally)

    assert service.calls[0]["mime"] == PDF_MIME
    assert tally.queued


def test_an_unrecognised_file_is_recorded_not_raised(tmp_path, meta):
    path = tmp_path / "pretend.pdf"
    path.write_bytes(b"this is just text")
    service, tally = FakeService(), Tally()

    ingest_one(service, path, meta, tally)

    assert tally.unsupported and not tally.queued
    assert service.calls == [], "an unrecognised file must never reach storage"


def test_an_empty_file_is_skipped(tmp_path, meta):
    path = tmp_path / "empty.pdf"
    path.write_bytes(b"")
    service, tally = FakeService(), Tally()

    ingest_one(service, path, meta, tally)

    assert tally.skipped_empty == [str(path)]
    assert service.calls == []


def test_a_duplicate_is_counted_separately(tmp_path, meta):
    """Re-running an interrupted import is the normal way to resume it, so 'already in the
    corpus' is an expected outcome and not a failure."""
    path = tmp_path / "again.pdf"
    path.write_bytes(PDF_BYTES)
    tally = Tally()

    ingest_one(FakeService(duplicate=True), path, meta, tally)

    assert tally.duplicates == [str(path)]
    assert tally.queued == []


def test_one_failing_file_does_not_stop_the_run(tmp_path, meta):
    """The property the whole design rests on. A real archive contains something surprising, and
    an exception escaping here would abandon every file after it."""
    good_a, bad, good_b = (tmp_path / n for n in ("a.pdf", "explodes.pdf", "b.pdf"))
    for p in (good_a, bad, good_b):
        p.write_bytes(PDF_BYTES)
    service, tally = FakeService(fail_on="explodes"), Tally()

    for p in (good_a, bad, good_b):
        ingest_one(service, p, meta, tally)

    assert len(tally.queued) == 2
    assert len(tally.failed) == 1
    assert "storage unavailable" in tally.failed[0][1]


def test_an_unreadable_file_is_recorded(tmp_path, meta, monkeypatch):
    path = tmp_path / "locked.pdf"
    path.write_bytes(PDF_BYTES)

    def refuse(self, *a, **k):
        raise PermissionError("in use by another process")

    monkeypatch.setattr("pathlib.Path.read_bytes", refuse)
    tally = Tally()

    ingest_one(FakeService(), path, meta, tally)

    assert tally.failed and "in use" in tally.failed[0][1]


# ==============================================================================
# Batch metadata
# ==============================================================================

def test_every_document_carries_the_same_batch_id_and_owner(tmp_path, meta):
    """One batch id across a run is what makes the import findable, countable, and — if it was a
    mistake — actionable as a unit afterwards."""
    paths = []
    for name in ("one.pdf", "two.pdf", "three.pdf"):
        p = tmp_path / name
        p.write_bytes(PDF_BYTES)
        paths.append(p)
    service, tally = FakeService(), Tally()

    for p in paths:
        ingest_one(service, p, meta, tally)

    assert {c["meta"].upload_batch_id for c in service.calls} == {meta.upload_batch_id}
    assert {c["meta"].owner_id for c in service.calls} == {meta.owner_id}


def test_the_tally_counts_every_file_exactly_once(tmp_path, meta):
    """`seen` is what the summary reports; if it drifts from the number of files walked, the run
    is quietly losing documents."""
    (tmp_path / "ok.pdf").write_bytes(PDF_BYTES)
    (tmp_path / "empty.pdf").write_bytes(b"")
    (tmp_path / "junk.pdf").write_bytes(b"nope")
    service, tally = FakeService(), Tally()

    for p in discover(tmp_path):
        ingest_one(service, p, meta, tally)

    assert tally.seen == 3


def test_candidate_suffixes_come_from_the_format_registry():
    """Adding a format must not require editing this script — the registry is the one place
    formats are declared."""
    assert bulk_ingest.CANDIDATE_SUFFIXES == {".pdf", ".docx", ".xlsx", ".pptx"}
