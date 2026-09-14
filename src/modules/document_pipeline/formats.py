"""Supported upload formats and their structural checks.

Two container families, not four formats: PDF has its own binary structure, while DOCX, XLSX and
PPTX are all Office Open XML — identical ZIP containers that differ only in which XML part they
carry inside. So there are exactly two structural checks here, and the OOXML spec entries differ
only by data.

Checks are deliberately minimal. The threat model is trusted internal uploaders on a private
network, and nothing in this system renders or executes an uploaded file — the same reasoning that
keeps malware scanning heuristic-only (see KNOWN_DEBTS.md #8). What remains is either required for
the pipeline to work at all (identifying the format, rejecting a file we cannot open before it is
promoted and a later stage trips over it) or costs a legitimate uploader nothing:

  * Macro rejection is the one check that guards a real downstream risk. The "never executed"
    argument that covers PDFs does not extend to Office macros, because a downloaded .docx is
    eventually opened in Word by a person, and that does execute them. A trusted colleague can
    still forward a file they received from outside without knowing what is buried in it.
  * The archive-entry check only ever fires on a path no real Office file contains.
"""

import io
import zipfile
from collections.abc import Callable
from dataclasses import dataclass

import pypdf

PDF_MIME = "application/pdf"
DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
PPTX_MIME = "application/vnd.openxmlformats-officedocument.presentationml.presentation"

_PDF_MAGIC = b"%PDF-"
_PDF_TRAILER = b"%%EOF"
_ZIP_MAGIC = b"PK\x03\x04"
# Password-protected OOXML is not a zip at all — it is an OLE/CFB container. Legacy .doc/.xls/.ppt
# use the same container, so this one signature covers both cases with one actionable message.
_OLE_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"

MAX_PDF_PAGES = 5000
_CONTENT_TYPES_PART = "[Content_Types].xml"


@dataclass(frozen=True)
class StructuralResult:
    ok: bool
    rejection_reason: str | None = None
    unit_count: int | None = None


@dataclass(frozen=True)
class FormatSpec:
    mime_type: str
    extension: str
    label: str
    # The part that identifies an OOXML container as this specific format. None for PDF.
    zip_marker: str | None
    # Entry-name prefix counted to produce unit_count, e.g. slides or worksheets. None where the
    # format has no meaningful count.
    unit_prefix: str | None
    # What unit_count means, for display. DOCX is None: Word text reflows, so a page count does not
    # exist until the document is rendered, and app.xml's <Pages> is a stale authoring artifact.
    unit_label: str | None
    # Cheap identity check: are these bytes plausibly this container at all? Runs before the threat
    # scan, so a disguised binary is reported as corrupt rather than handed to a parser.
    container_check: Callable[[bytes, "FormatSpec"], StructuralResult]
    # Deep inspection, and the source of unit_count. Runs after the threat scan, so known-malicious
    # content is reported as malicious rather than as whatever the parser happens to choke on first.
    structural_check: Callable[[bytes, "FormatSpec"], StructuralResult]


def _pdf_container(data: bytes, spec: FormatSpec) -> StructuralResult:
    if not data.startswith(_PDF_MAGIC):
        return StructuralResult(False, "CORRUPTED_PDF_STRUCTURE: Missing '%PDF-' header marker.")

    trailer_window = data[-1024:] if len(data) >= 1024 else data
    if _PDF_TRAILER not in trailer_window:
        return StructuralResult(
            False, "CORRUPTED_PDF_STRUCTURE: Missing '%%EOF' end-of-file trailer marker."
        )
    return StructuralResult(True)


def _pdf_structure(data: bytes, spec: FormatSpec) -> StructuralResult:
    # No pre-parse byte-scan for "/Encrypt": that token also occurs inside unencrypted content
    # streams. pypdf's reader.is_encrypted is the authoritative check.
    try:
        reader = pypdf.PdfReader(io.BytesIO(data))
        if reader.is_encrypted:
            return StructuralResult(
                False, "ENCRYPTED_PDF: Password-protected or encrypted PDFs are not supported."
            )
        page_count = len(reader.pages)
    except Exception as e:
        err_str = str(e).lower()
        if "encrypt" in err_str or "password" in err_str:
            return StructuralResult(
                False, "ENCRYPTED_PDF: Password-protected or encrypted PDFs are not supported."
            )
        return StructuralResult(
            False, f"CORRUPTED_PDF_STRUCTURE: Malformed internal PDF structure ({e!s})."
        )

    if page_count > MAX_PDF_PAGES:
        return StructuralResult(
            False, f"PAGE_LIMIT_EXCEEDED: PDF has {page_count} pages (Max: {MAX_PDF_PAGES})."
        )
    return StructuralResult(True, unit_count=page_count)


def _is_unsafe_entry(name: str) -> bool:
    """Archive entries that would escape the extraction root once a later stage unpacks them."""
    normalised = name.replace("\\", "/")
    return (
        normalised.startswith("/")
        or ".." in normalised.split("/")
        or (len(normalised) > 1 and normalised[1] == ":")
    )


def _ooxml_names(data: bytes) -> list[str] | None:
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            return archive.namelist()
    except (zipfile.BadZipFile, OSError):
        return None


def _ooxml_container(data: bytes, spec: FormatSpec) -> StructuralResult:
    if data.startswith(_OLE_MAGIC):
        return StructuralResult(
            False,
            "ENCRYPTED_OOXML: This is a password-protected or legacy Office file. Remove the "
            "password or re-save it as .docx/.xlsx/.pptx and upload again.",
        )

    names = _ooxml_names(data)
    if names is None:
        return StructuralResult(
            False, "CORRUPTED_OOXML_STRUCTURE: Not a readable Office file (unreadable archive)."
        )

    if _CONTENT_TYPES_PART not in names:
        return StructuralResult(
            False,
            f"CORRUPTED_OOXML_STRUCTURE: Missing '{_CONTENT_TYPES_PART}' — not a valid Office file.",
        )
    return StructuralResult(True)


def _ooxml_structure(data: bytes, spec: FormatSpec) -> StructuralResult:
    names = _ooxml_names(data)
    if names is None:
        return StructuralResult(
            False, "CORRUPTED_OOXML_STRUCTURE: Not a readable Office file (unreadable archive)."
        )

    if any(name.rsplit("/", 1)[-1] == "vbaProject.bin" for name in names):
        return StructuralResult(
            False,
            "MACRO_ENABLED_DOCUMENT: This file contains macros. Re-save it without macros "
            "(.docx/.xlsx/.pptx) and upload again.",
        )

    unsafe = next((name for name in names if _is_unsafe_entry(name)), None)
    if unsafe is not None:
        return StructuralResult(
            False, f"UNSAFE_ARCHIVE_ENTRY: Office file contains an illegal entry path ({unsafe!r})."
        )

    # The declared type has to match what is actually inside, so a renamed file is caught here
    # rather than by a later stage that expects a different XML part.
    if spec.zip_marker not in names:
        actual = next(
            (other.label for other in _OOXML_SPECS if other.zip_marker in names),
            None,
        )
        detail = f"file is actually {actual}" if actual else "content does not match any known type"
        return StructuralResult(
            False,
            f"CORRUPTED_OOXML_STRUCTURE: Expected {spec.label} (missing '{spec.zip_marker}') — {detail}.",
        )

    unit_count = None
    if spec.unit_prefix is not None:
        unit_count = sum(1 for name in names if name.startswith(spec.unit_prefix))
    return StructuralResult(True, unit_count=unit_count)


FORMATS: dict[str, FormatSpec] = {
    PDF_MIME: FormatSpec(
        mime_type=PDF_MIME,
        extension=".pdf",
        label="PDF",
        zip_marker=None,
        unit_prefix=None,
        unit_label="pages",
        container_check=_pdf_container,
        structural_check=_pdf_structure,
    ),
    DOCX_MIME: FormatSpec(
        mime_type=DOCX_MIME,
        extension=".docx",
        label="Word document (.docx)",
        zip_marker="word/document.xml",
        unit_prefix=None,
        unit_label=None,
        container_check=_ooxml_container,
        structural_check=_ooxml_structure,
    ),
    XLSX_MIME: FormatSpec(
        mime_type=XLSX_MIME,
        extension=".xlsx",
        label="Excel workbook (.xlsx)",
        zip_marker="xl/workbook.xml",
        unit_prefix="xl/worksheets/sheet",
        unit_label="sheets",
        container_check=_ooxml_container,
        structural_check=_ooxml_structure,
    ),
    PPTX_MIME: FormatSpec(
        mime_type=PPTX_MIME,
        extension=".pptx",
        label="PowerPoint deck (.pptx)",
        zip_marker="ppt/presentation.xml",
        unit_prefix="ppt/slides/slide",
        unit_label="slides",
        container_check=_ooxml_container,
        structural_check=_ooxml_structure,
    ),
}

_OOXML_SPECS = [spec for spec in FORMATS.values() if spec.zip_marker is not None]

SUPPORTED_LABELS = ", ".join(spec.label for spec in FORMATS.values())
SUPPORTED_EXTENSIONS = [spec.extension for spec in FORMATS.values()]
_OOXML_EXTENSIONS = ", ".join(spec.extension for spec in _OOXML_SPECS)


def spec_for(mime_type: str) -> FormatSpec | None:
    return FORMATS.get(mime_type)


def detect_format(data: bytes) -> FormatSpec | None:
    """Identifies a format from its bytes, ignoring whatever the client declared.

    Browsers are unreliable about Office MIME types — drag-and-drop and some operating systems send
    `application/octet-stream` for a perfectly good .docx. Deciding from content means a valid file
    uploads regardless, and a mislabelled one is still identified correctly.
    """
    if data.startswith(_PDF_MAGIC):
        return FORMATS[PDF_MIME]
    if data.startswith(_ZIP_MAGIC):
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as archive:
                names = archive.namelist()
        except (zipfile.BadZipFile, OSError):
            return None
        return next((spec for spec in _OOXML_SPECS if spec.zip_marker in names), None)
    return None


def describe_unsupported(data: bytes, declared_mime: str | None = None) -> str:
    """Explains why a file could not be accepted, in terms the uploader can act on."""
    if data.startswith(_OLE_MAGIC):
        return (
            "This is a password-protected or legacy Office file (.doc/.xls/.ppt). Re-save it as "
            f"{_OOXML_EXTENSIONS} and upload again."
        )
    declared = f" (declared '{declared_mime}')" if declared_mime else ""
    return f"Unsupported file type{declared}. Accepted formats: {SUPPORTED_LABELS}."
