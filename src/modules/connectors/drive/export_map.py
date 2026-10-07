"""Which Drive files can be ingested, and what a Google-native one is converted to.

Google Docs, Sheets and Slides have **no bytes to download**. They are not files in Drive, they
are documents in Google's own store, and asking for their content directly returns an error.
They have to be exported, and the export format is the whole decision here: it is what the
extraction stage will later read structure out of.

Each goes to the Office format this pipeline already parses properly:

- Docs to **docx**, not text/plain or HTML. `extraction/extractors.py` reads Word paragraph
  styles as headings, and headings are what the chunker cuts on. Exporting to text throws that
  away, and every chunk boundary after it is then a guess.
- Sheets to **xlsx**, not CSV. A worksheet is already a grid; CSV flattens a workbook to a single
  sheet and drops the sheet names that become citation headings. `formats.py` does not accept CSV
  at all — it has no container structure to validate and carries a formula-injection profile of
  its own (see the README).
- Slides to **pptx**, not PDF. Slide titles are headings and the notes survive. A PDF export
  turns a deck into pages of pictures, which would then need OCR to read back text the original
  format was already carrying.

Anything else is downloaded as it is and handed to `detect_format()`, which decides from the
bytes. That keeps one source of truth about what is accepted: a format added to `formats.py`
becomes importable from Drive with no edit here.
"""

from src.modules.document_pipeline.formats import DOCX_MIME, PPTX_MIME, XLSX_MIME

FOLDER_MIME = "application/vnd.google-apps.folder"
SHORTCUT_MIME = "application/vnd.google-apps.shortcut"

# Google-native type -> (export MIME, extension to give the exported bytes).
GOOGLE_EXPORTS: dict[str, tuple[str, str]] = {
    "application/vnd.google-apps.document": (DOCX_MIME, ".docx"),
    "application/vnd.google-apps.spreadsheet": (XLSX_MIME, ".xlsx"),
    "application/vnd.google-apps.presentation": (PPTX_MIME, ".pptx"),
}

# Native types with no sensible export into anything this pipeline reads. Named rather than left
# to fall through, so the skip reason says what the file actually was instead of the much less
# useful "unsupported format".
UNEXPORTABLE = {
    "application/vnd.google-apps.form": "a Google Form",
    "application/vnd.google-apps.drawing": "a Google Drawing",
    "application/vnd.google-apps.map": "a Google My Map",
    "application/vnd.google-apps.site": "a Google Site",
    "application/vnd.google-apps.script": "an Apps Script project",
    "application/vnd.google-apps.jam": "a Jamboard",
    SHORTCUT_MIME: "a shortcut to another file",
}


def export_target(drive_mime: str) -> tuple[str, str] | None:
    """The (MIME, extension) a Google-native file should be exported to.

    None means this is an ordinary file that should simply be downloaded.
    """
    return GOOGLE_EXPORTS.get(drive_mime)


def unexportable_reason(drive_mime: str) -> str | None:
    """Why a Google-native file cannot be ingested, phrased for whoever reads the report."""
    what = UNEXPORTABLE.get(drive_mime)
    if what:
        return f"{what} — it has no document form this pipeline can read"
    if drive_mime.startswith("application/vnd.google-apps.") and drive_mime not in GOOGLE_EXPORTS:
        return f"an unrecognised Google-native type ({drive_mime})"
    return None
