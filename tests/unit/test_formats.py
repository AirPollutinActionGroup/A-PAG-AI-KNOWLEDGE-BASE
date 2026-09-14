"""Format registry and multi-format validation tests.

OOXML fixtures are built in-process rather than committed as binaries: a .docx is just a zip with
particular entries, so constructing one here makes the thing under test readable in the diff, and
lets a malformed case be expressed as one changed line instead of an opaque blob.
"""

import io
import zipfile

import pytest

from src.modules.document_pipeline.formats import (
    DOCX_MIME,
    PDF_MIME,
    PPTX_MIME,
    XLSX_MIME,
    detect_format,
    spec_for,
)
from src.modules.document_pipeline.storage_keys import build_raw_key, extension_for
from src.modules.document_pipeline.validation import FileValidator

CONTENT_TYPES = "[Content_Types].xml"

_MINIMAL_PARTS = {
    DOCX_MIME: ["word/document.xml"],
    XLSX_MIME: ["xl/workbook.xml", "xl/worksheets/sheet1.xml"],
    PPTX_MIME: ["ppt/presentation.xml", "ppt/slides/slide1.xml"],
}


def build_ooxml(mime: str, extra: dict[str, bytes] | None = None, omit: str | None = None) -> bytes:
    """Builds a structurally valid OOXML container for `mime`."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        if omit != CONTENT_TYPES:
            archive.writestr(CONTENT_TYPES, b"<Types/>")
        archive.writestr("_rels/.rels", b"<Relationships/>")
        for part in _MINIMAL_PARTS[mime]:
            if part != omit:
                archive.writestr(part, b"<xml/>")
        for name, payload in (extra or {}).items():
            archive.writestr(name, payload)
    return buffer.getvalue()


MINIMAL_PDF = b"%PDF-1.4\n1 0 obj\n<< /Title (Test) >>\nendobj\n%%EOF"


# ==============================================================================
# Format detection — resolved from bytes, not from what the client claimed
# ==============================================================================

@pytest.mark.parametrize("mime", [DOCX_MIME, XLSX_MIME, PPTX_MIME])
def test_detect_identifies_each_ooxml_format(mime):
    assert detect_format(build_ooxml(mime)).mime_type == mime


def test_detect_identifies_pdf():
    assert detect_format(MINIMAL_PDF).mime_type == PDF_MIME


def test_detect_ignores_the_declared_type():
    """A browser sending application/octet-stream for a real .docx must still upload."""
    assert detect_format(build_ooxml(DOCX_MIME)).mime_type == DOCX_MIME


def test_detect_rejects_unknown_and_plain_zip():
    assert detect_format(b"\x89PNG\r\n\x1a\n") is None
    plain_zip = io.BytesIO()
    with zipfile.ZipFile(plain_zip, "w") as archive:
        archive.writestr("notes.txt", b"hello")
    assert detect_format(plain_zip.getvalue()) is None


# ==============================================================================
# Validation — the happy path per format
# ==============================================================================

@pytest.mark.parametrize(
    ("mime", "expected_units"),
    [(DOCX_MIME, None), (XLSX_MIME, 1), (PPTX_MIME, 1)],
)
def test_valid_ooxml_passes_with_expected_unit_count(mime, expected_units):
    res = FileValidator().validate(build_ooxml(mime), declared_mime_type=mime)
    assert res.is_valid, res.rejection_reason
    assert res.sha256
    # DOCX has no page count until it is rendered, so None is the honest answer.
    assert res.page_count == expected_units


def test_xlsx_counts_multiple_sheets():
    data = build_ooxml(
        XLSX_MIME,
        extra={"xl/worksheets/sheet2.xml": b"<xml/>", "xl/worksheets/sheet3.xml": b"<xml/>"},
    )
    res = FileValidator().validate(data, declared_mime_type=XLSX_MIME)
    assert res.is_valid and res.page_count == 3


def test_pptx_counts_multiple_slides():
    data = build_ooxml(PPTX_MIME, extra={"ppt/slides/slide2.xml": b"<xml/>"})
    res = FileValidator().validate(data, declared_mime_type=PPTX_MIME)
    assert res.is_valid and res.page_count == 2


# ==============================================================================
# Validation — rejections
# ==============================================================================

def test_macro_enabled_file_is_rejected():
    data = build_ooxml(DOCX_MIME, extra={"word/vbaProject.bin": b"\x00macro"})
    res = FileValidator().validate(data, declared_mime_type=DOCX_MIME)
    assert not res.is_valid
    assert "MACRO_ENABLED_DOCUMENT" in res.rejection_reason


def test_renamed_file_is_rejected_by_content():
    """An .xlsx uploaded as a .docx must not slip through on its declared type alone."""
    res = FileValidator().validate(build_ooxml(XLSX_MIME), declared_mime_type=DOCX_MIME)
    assert not res.is_valid
    assert "CORRUPTED_OOXML_STRUCTURE" in res.rejection_reason
    assert "Excel" in res.rejection_reason


def test_path_traversal_entry_is_rejected():
    data = build_ooxml(DOCX_MIME, extra={"../../evil.xml": b"<xml/>"})
    res = FileValidator().validate(data, declared_mime_type=DOCX_MIME)
    assert not res.is_valid
    assert "UNSAFE_ARCHIVE_ENTRY" in res.rejection_reason


def test_missing_content_types_is_rejected():
    data = build_ooxml(DOCX_MIME, omit=CONTENT_TYPES)
    res = FileValidator().validate(data, declared_mime_type=DOCX_MIME)
    assert not res.is_valid
    assert "CORRUPTED_OOXML_STRUCTURE" in res.rejection_reason


def test_truncated_archive_is_rejected():
    data = build_ooxml(DOCX_MIME)[:40]
    res = FileValidator().validate(data, declared_mime_type=DOCX_MIME)
    assert not res.is_valid
    assert "CORRUPTED_OOXML_STRUCTURE" in res.rejection_reason


def test_password_protected_office_file_is_rejected_with_actionable_reason():
    """Encrypted OOXML is an OLE container, not a zip — it needs its own message."""
    ole = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 512
    res = FileValidator().validate(ole, declared_mime_type=DOCX_MIME)
    assert not res.is_valid
    assert "ENCRYPTED_OOXML" in res.rejection_reason


def test_unsupported_mime_is_rejected():
    res = FileValidator().validate(build_ooxml(DOCX_MIME), declared_mime_type="image/png")
    assert not res.is_valid
    assert "INVALID_MIME_TYPE" in res.rejection_reason


# ==============================================================================
# Storage keys derive from the registry, so formats stay in one place
# ==============================================================================

@pytest.mark.parametrize(
    ("mime", "extension"),
    [
        (PDF_MIME, ".pdf"),
        (DOCX_MIME, ".docx"),
        (XLSX_MIME, ".xlsx"),
        (PPTX_MIME, ".pptx"),
    ],
)
def test_storage_keys_use_the_registered_extension(mime, extension):
    assert extension_for(mime) == extension
    assert build_raw_key("abc123", mime) == f"abc123{extension}"


def test_every_registered_format_has_a_distinct_extension():
    extensions = [spec_for(m).extension for m in (PDF_MIME, DOCX_MIME, XLSX_MIME, PPTX_MIME)]
    assert len(set(extensions)) == len(extensions)
