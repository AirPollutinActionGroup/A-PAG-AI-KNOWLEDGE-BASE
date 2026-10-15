"""Per-format text extractors.

One `TextExtractor` per supported format, mirroring how `formats.py` keeps per-format validation
knowledge in one registry. The shared principle: **read what the format already states, don't
infer it.** Office formats carry their structure explicitly (a Word heading is a named style, a
slide title is a title placeholder, a worksheet is already a grid), so headings and tables come
out of the file rather than out of a model. PDF is the one format with no semantic structure at
all — just positioned glyphs — so it gets a small, clearly-bounded font-size heuristic and
pdfplumber's ruling-line table detection.

A PDF page with no text layer is read by OCR (`ocr.py`), decided page by page; a scan that OCR
cannot read either produces almost no text, which the normalization quality gate flags rather
than letting near-empty content through silently.
"""

import io
import logging
import re
from abc import ABC, abstractmethod
from collections import Counter
from typing import Any

from src.core.config import settings
from src.modules.document_pipeline.extraction.models import (
    ExtractedContent,
    ExtractedTable,
    ExtractedUnit,
    ExtractionError,
    Heading,
)
from src.modules.document_pipeline.extraction.ocr import (
    METHOD_NATIVE,
    METHOD_OCR,
    OcrEngine,
    OcrUnavailable,
    get_ocr_engine,
)
from src.modules.document_pipeline.formats import (
    DOCX_MIME,
    PDF_MIME,
    PPTX_MIME,
    XLSX_MIME,
)

logger = logging.getLogger(__name__)

# A PDF line qualifies as a heading if it is meaningfully larger than body text and short enough
# to be a title rather than a sentence that happens to be set large.
_HEADING_SIZE_RATIO = 1.15
_HEADING_MAX_CHARS = 120

# The subject line of a government memo, notice or direction: "Subject: Extension of timelines
# for ...", "Sub.: ...". A scanned page has no font sizes, so the size heuristic finds no heading
# on it at all, and every passage from a scan was cited under "no section recorded". This line is
# what the document says it is about, written by its author, which makes it the one heading a
# scan reliably carries. The 162-page CPCB set has 26 of them, one per direction.
_SUBJECT_LINE = re.compile(r"^\s*(subject|sub)\s*[.:\-]+\s*\S", re.IGNORECASE)
_SUBJECT_MAX_CHARS = 200


class TextExtractor(ABC):
    """Extracts readable content from one format's bytes."""

    @abstractmethod
    def extract(self, data: bytes) -> ExtractedContent:
        """Returns the file's text, tables and headings, or raises `ExtractionError`."""


def _clean_cell(value: object) -> str:
    return str(value).strip() if value is not None else ""


def _table_has_content(rows: list[list[str]]) -> bool:
    return any(any(cell for cell in row) for row in rows)


class PdfTextExtractor(TextExtractor):
    """PDF via pdfplumber — text layer, ruling-line tables, font-size heading heuristic, and an
    OCR fallback for pages that have no text layer at all.

    The fallback is decided **per page**, not per document. A government PDF is routinely a typed
    covering letter with a scanned annexure stapled behind it, and a per-document switch reads
    such a file one way and loses the other half either way. Per page, each is read however it
    was produced.
    """

    def __init__(self, ocr: OcrEngine | None = None):
        # An explicit engine is for tests. In production it is resolved lazily on the first page
        # that needs it, so an extraction worker that never meets a scan never loads a model.
        self._ocr = ocr

    def _engine(self) -> OcrEngine | None:
        # The setting is authoritative even over an injected engine. Otherwise OCR_ENABLED=false
        # would mean "off, unless something handed us an engine", which is not a switch.
        if not settings.OCR_ENABLED:
            return None
        return self._ocr if self._ocr is not None else get_ocr_engine()

    def extract(self, data: bytes) -> ExtractedContent:
        import pdfplumber

        units: list[ExtractedUnit] = []
        tables: list[ExtractedTable] = []
        headings: list[Heading] = []
        ocr_pages: list[int] = []
        skipped_pages: list[int] = []
        unreadable_lines: dict[int, int] = {}
        subject_headings: list[Heading] = []
        # (text, font size, page index) for every line, collected across the whole document so
        # the body size is measured document-wide rather than per page — a page that is entirely
        # a heading would otherwise make that heading look like body text.
        lines: list[tuple[str, float, int]] = []

        ocr_budget = settings.OCR_MAX_PAGES
        try:
            with pdfplumber.open(io.BytesIO(data)) as pdf:
                for index, page in enumerate(pdf.pages, start=1):
                    text = page.extract_text() or ""
                    method = METHOD_NATIVE

                    if self._needs_ocr(page, text):
                        if ocr_budget <= 0:
                            # Recorded, never silent. A document truncated without a trace looks
                            # exactly like one that was short, and the gap is unfindable.
                            skipped_pages.append(index)
                            logger.warning(
                                "OCR page budget (%d) exhausted; page %d left unread",
                                settings.OCR_MAX_PAGES, index,
                            )
                        else:
                            read, unreadable = self._ocr_page(page, index)
                            if unreadable:
                                unreadable_lines[index] = unreadable
                            if read:
                                text = read
                                method = METHOD_OCR
                                ocr_pages.append(index)
                                ocr_budget -= 1
                                subject_headings.extend(self._subject_headings(read, index))

                    units.append(
                        ExtractedUnit(index=index, label=f"Page {index}", text=text, method=method)
                    )

                    # Both of these read the text layer, so on an OCR'd page they find nothing —
                    # a scan has no ruling-line geometry and no font sizes. Running them anyway
                    # costs little and keeps one code path for hybrid documents.
                    for raw_table in page.extract_tables():
                        rows = [[_clean_cell(cell) for cell in row] for row in raw_table]
                        if _table_has_content(rows):
                            tables.append(ExtractedTable(unit_index=index, rows=rows))

                    lines.extend(self._page_lines(page, index))
        except ExtractionError:
            raise
        except Exception as e:
            raise ExtractionError(f"Could not read PDF content: {e}") from e

        if ocr_pages:
            logger.info(
                "OCR read %d of %d page(s): %s%s",
                len(ocr_pages), len(units),
                ", ".join(str(p) for p in ocr_pages[:10]),
                "…" if len(ocr_pages) > 10 else "",
            )

        # Size-inferred headings come from the text layer and subject lines from OCR'd pages, so
        # the two never describe the same page; sorted by page so the chunker meets them in
        # reading order.
        headings = sorted(
            self._infer_headings(lines) + subject_headings, key=lambda h: h.unit_index
        )
        return ExtractedContent(
            units=units,
            headings=headings,
            tables=tables,
            ocr_pages=ocr_pages,
            ocr_skipped_pages=skipped_pages,
            ocr_unreadable_lines=unreadable_lines,
        )

    @staticmethod
    def _subject_headings(text: str, page_index: int) -> list[Heading]:
        """A memo's "Subject:" line, as the heading of the scanned page it opens. Only the first
        line of a wrapped subject is used, because the chunker locates a heading by finding its
        exact text in the page, and a subject joined across a line break is not in it."""
        found = []
        for line in text.splitlines():
            line = line.strip()
            if not _SUBJECT_LINE.match(line) or len(line) > _SUBJECT_MAX_CHARS:
                continue
            # A word hyphenated across the line break ("Flue Gas De-" / "sulphurisation") is
            # rejoined in the page text by the cleaner, so a heading ending "De-" would match
            # nothing and the memo's subject was silently not used. Stop before the fragment.
            if line.endswith("-") and " " in line:
                line = line.rsplit(" ", 1)[0].rstrip()
            found.append(Heading(text=line, level=1, unit_index=page_index))
        return found

    @staticmethod
    def _needs_ocr(page, text: str) -> bool:
        """A page worth photographing: almost no text, but something drawn on it.

        Both halves matter. Without the text test an OCR pass would run over a typed corpus for
        nothing; without the image test every genuinely blank separator page would cost four
        seconds to confirm it is blank. A scan can be one full-page image or dozens of strips —
        one document here puts 49 images on three pages — so the test is presence, not size.
        """
        if len(text.strip()) >= settings.OCR_MIN_NATIVE_CHARS:
            return False
        try:
            return bool(page.images)
        except Exception:
            return False

    def _ocr_page(self, page, index: int) -> tuple[str, int]:
        """Renders one page and reads it: (text, lines found but too uncertain to keep).

        The text is "" if OCR is off, unavailable, or found nothing — in every one of those
        cases the page keeps its (empty) native text, and the normalization quality gate decides
        what an empty document means. That judgement belongs there, not here."""
        engine = self._engine()
        if engine is None:
            return "", 0
        try:
            image = page.to_image(resolution=settings.OCR_RESOLUTION).original
            read = engine.read_page(image)
        except OcrUnavailable as e:
            logger.error("OCR failed on page %d: %s", index, e)
            return "", 0
        except Exception as e:
            # Rendering can fail on a malformed page. One bad page must not lose the other 161.
            logger.warning("Could not render page %d for OCR: %s", index, e)
            return "", 0
        return "\n".join(line.text for line in read.lines), read.unreadable

    @staticmethod
    def _page_lines(page, page_index: int) -> list[tuple[str, float, int]]:
        """Groups a page's words into lines, each tagged with its largest font size."""
        try:
            words = page.extract_words(extra_attrs=["size"])
        except Exception:
            # Heading inference is a nicety; losing it must never fail the extraction itself.
            return []

        by_top: dict[int, list[dict]] = {}
        for word in words:
            # Round the vertical position so words on the same visual line group together despite
            # sub-pixel differences.
            by_top.setdefault(round(float(word.get("top", 0))), []).append(word)

        lines = []
        for _, line_words in sorted(by_top.items()):
            text = " ".join(w.get("text", "") for w in line_words).strip()
            if not text:
                continue
            size = max(float(w.get("size", 0) or 0) for w in line_words)
            lines.append((text, size, page_index))
        return lines

    @staticmethod
    def _infer_headings(lines: list[tuple[str, float, int]]) -> list[Heading]:
        """Treats lines notably larger than the document's most common font size as headings.

        This is the one place the pipeline infers structure instead of reading it, and it is
        deliberately conservative: a false negative just means a heading is treated as body text,
        which costs nothing downstream beyond slightly coarser chunk boundaries.
        """
        if not lines:
            return []

        # Weighted by characters, not by line count: body text is whichever size carries the most
        # *text*, which is what "body" means. Counting lines instead ties on short documents — a
        # page with one heading and one paragraph has one line of each, and the tie can hand
        # "body size" to the heading, after which nothing is large enough to be a heading at all.
        size_weights: Counter[float] = Counter()
        for text, size, _ in lines:
            if size > 0:
                size_weights[round(size, 1)] += len(text)
        if not size_weights:
            return []
        body_size = size_weights.most_common(1)[0][0]

        # Distinct heading sizes, largest first, become levels 1, 2, 3...
        heading_sizes = sorted(
            {round(size, 1) for _, size, _ in lines if size >= body_size * _HEADING_SIZE_RATIO},
            reverse=True,
        )
        levels = {size: level for level, size in enumerate(heading_sizes, start=1)}

        headings = []
        for text, size, page_index in lines:
            level = levels.get(round(size, 1))
            if level is not None and len(text) <= _HEADING_MAX_CHARS:
                headings.append(Heading(text=text, level=level, unit_index=page_index))
        return headings


class DocxTextExtractor(TextExtractor):
    """DOCX via python-docx. Headings come from Word's own paragraph styles.

    The body is read **in document order**, paragraphs and tables interleaved, so each table
    records the heading it sits under. Reading every paragraph and then every table lost that, and
    a Word file with more than one heading cited all its tables under none (#19). Text boxes and
    footnotes are read too: footnotes here are mostly source URLs, which are what a citation needs.
    """

    def extract(self, data: bytes) -> ExtractedContent:
        from docx import Document as DocxDocument
        from docx.table import Table as DocxTable

        paragraphs: list[str] = []
        headings: list[Heading] = []
        tables: list[ExtractedTable] = []
        try:
            document = DocxDocument(io.BytesIO(data))
            current: str | None = None
            for block in document.iter_inner_content():
                if isinstance(block, DocxTable):
                    rows = _docx_rows(block)
                    if _table_has_content(rows):
                        tables.append(ExtractedTable(unit_index=1, rows=rows, heading=current))
                    continue
                text = block.text.strip()
                if text:
                    paragraphs.append(text)
                    level = self._heading_level(block)
                    if level is not None:
                        headings.append(Heading(text=text, level=level, unit_index=1))
                        current = text
                paragraphs.extend(_docx_text_boxes(block._p))

            notes = _docx_footnotes(document)
            if notes:
                paragraphs.append("Notes:\n" + "\n".join(notes))
        except Exception as e:
            raise ExtractionError(f"Could not read Word document content: {e}") from e

        # One unit: a .docx has no pages until it is rendered, so there is nothing honest to
        # divide it by (the same reasoning that makes page_count None for DOCX at validation).
        units = [ExtractedUnit(index=1, label="Document", text="\n".join(paragraphs))]
        return ExtractedContent(units=units, headings=headings, tables=tables)

    @staticmethod
    def _heading_level(paragraph) -> int | None:
        """Word names built-in heading styles 'Heading 1'...'Heading 9'. Title is treated as
        level 1 since that is what it means structurally."""
        style_name = (getattr(paragraph.style, "name", "") or "").strip()
        if style_name.lower() == "title":
            return 1
        if not style_name.lower().startswith("heading"):
            return None
        tail = style_name[len("heading"):].strip()
        return int(tail) if tail.isdigit() else 1


_W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_MC_FALLBACK = "{http://schemas.openxmlformats.org/markup-compatibility/2006}Fallback"


def _docx_rows(table) -> list[list[str]]:
    """A Word table's cells, with a horizontally merged cell kept once.

    python-docx returns a cell merged across three columns three times, so its text appeared
    three times in the row. Vertical merges are kept repeated on purpose: each row of a row group
    should still say which merged label it belongs to.
    """
    rows = []
    for row in table.rows:
        cells, previous = [], None
        for cell in row.cells:
            if previous is not None and cell._tc is previous:
                continue
            previous = cell._tc
            cells.append(_clean_cell(cell.text))
        rows.append(cells)
    return rows


def _docx_text_boxes(paragraph_element) -> list[str]:
    """Text inside text boxes anchored in this paragraph, which `paragraph.text` does not include.

    A text box is stored twice for compatibility -- a modern shape and a VML fallback holding the
    same text -- so anything under `mc:Fallback` is skipped, or every box would be read twice.
    """
    found: list[str] = []
    for box in paragraph_element.iter(f"{{{_W_NS}}}txbxContent"):
        if any(ancestor.tag == _MC_FALLBACK for ancestor in box.iterancestors()):
            continue
        for p in box.iter(f"{{{_W_NS}}}p"):
            text = "".join(t.text or "" for t in p.iter(f"{{{_W_NS}}}t")).strip()
            if text and text not in found:
                found.append(text)
    return found


def _docx_footnotes(document) -> list[str]:
    """The document's footnotes and endnotes, in order. Separator entries are Word's own
    furniture (the rule above the notes) and are skipped."""
    from lxml import etree

    notes: list[str] = []
    for part in document.part.package.iter_parts():
        name = str(part.partname)
        if name not in ("/word/footnotes.xml", "/word/endnotes.xml"):
            continue
        root = etree.fromstring(part.blob, parser=etree.XMLParser(resolve_entities=False))
        for note in root:
            if note.get(f"{{{_W_NS}}}type") in ("separator", "continuationSeparator",
                                                "continuationNotice"):
                continue
            text = " ".join(
                "".join(t.text or "" for t in p.iter(f"{{{_W_NS}}}t")).strip()
                for p in note.iter(f"{{{_W_NS}}}p")
            ).strip()
            if text:
                notes.append(text)
    return notes


class PptxTextExtractor(TextExtractor):
    """PPTX via python-pptx. One unit per slide, and each slide stands alone.

    Shapes are read recursively, because grouped shapes are how decks draw timelines and labelled
    diagrams: reading only top-level shapes lost the whole MoEF&CC deadline history on one slide
    of "TPPs Delhi NCR". Speaker notes are kept (they carry source URLs, and sometimes the point
    of the slide). Hidden slides are skipped -- they are drafts or discarded numbers -- without
    renumbering the rest, so slide N in a citation is slide N in the file.
    """

    def extract(self, data: bytes) -> ExtractedContent:
        from pptx import Presentation

        units: list[ExtractedUnit] = []
        headings: list[Heading] = []
        tables: list[ExtractedTable] = []
        hidden: list[int] = []

        try:
            presentation = Presentation(io.BytesIO(data))
            for index, slide in enumerate(presentation.slides, start=1):
                if slide._element.get("show") == "0":
                    hidden.append(index)
                    continue

                title_shape, title = self._slide_title(slide)
                # The title's shape goes first, whatever its place in the slide's drawing order,
                # so the chunker meets the heading before the text it heads.
                texts = [title_shape.text_frame.text.strip()] if title_shape is not None else []
                slide_tables: list[ExtractedTable] = []
                title_element = title_shape._element if title_shape is not None else None
                for shape in _walk_shapes(slide.shapes):
                    # Compared by XML element: python-pptx builds a new wrapper object on every
                    # iteration, so `shape is title_shape` is never true.
                    if shape._element is title_element:
                        continue
                    if getattr(shape, "has_table", False):
                        rows = [
                            [_clean_cell(cell.text) for cell in row.cells]
                            for row in shape.table.rows
                        ]
                        if _table_has_content(rows):
                            slide_tables.append(ExtractedTable(
                                unit_index=index, rows=rows, heading=title or None,
                            ))
                    elif getattr(shape, "has_text_frame", False):
                        text = shape.text_frame.text.strip()
                        if text:
                            texts.append(text)

                notes = self._notes(slide)
                if notes:
                    texts.append("Notes:\n" + notes)
                if title:
                    headings.append(Heading(text=title, level=1, unit_index=index))
                tables.extend(slide_tables)
                units.append(
                    ExtractedUnit(index=index, label=f"Slide {index}", text="\n".join(texts))
                )
        except Exception as e:
            raise ExtractionError(f"Could not read PowerPoint content: {e}") from e

        if hidden:
            logger.info("Skipped %d hidden slide(s): %s", len(hidden), hidden)
        return ExtractedContent(units=units, headings=headings, tables=tables)

    @staticmethod
    def _slide_title(slide) -> tuple[Any, str]:
        """The slide's title and the shape it came from.

        The title placeholder when it has text; otherwise the first line of the topmost text box.
        Decks routinely type a title into an ordinary text box, and without this fallback such a
        slide had no heading of its own.
        """
        try:
            placeholder = slide.shapes.title
        except Exception:
            placeholder = None
        if placeholder is not None and getattr(placeholder, "has_text_frame", False):
            text = placeholder.text_frame.text.strip()
            if text:
                return placeholder, text

        candidates = []
        for order, shape in enumerate(slide.shapes):
            if not getattr(shape, "has_text_frame", False):
                continue
            first = next(
                (line.strip() for line in shape.text_frame.text.splitlines() if line.strip()), ""
            )
            if len(first) >= 4 and len(first) <= _HEADING_MAX_CHARS:
                top = shape.top if shape.top is not None else 10**12
                left = shape.left if shape.left is not None else 10**12
                candidates.append((top, left, order, first, shape))
        if not candidates:
            return None, ""
        # The title is the first line; the shape's other lines stay in the body under it.
        _, _, _, first, shape = min(candidates, key=lambda c: c[:3])
        return shape, first

    @staticmethod
    def _notes(slide) -> str:
        try:
            if not slide.has_notes_slide:
                return ""
            frame = slide.notes_slide.notes_text_frame
        except Exception:
            return ""
        return frame.text.strip() if frame is not None else ""


def _walk_shapes(shapes):
    """Every shape on a slide, descending into groups, in the slide's own order."""
    from pptx.enum.shapes import MSO_SHAPE_TYPE

    for shape in shapes:
        if shape.shape_type == MSO_SHAPE_TYPE.GROUP:
            yield from _walk_shapes(shape.shapes)
        else:
            yield shape


# Above this, a workbook is read in openpyxl's streaming mode, which cannot see merged ranges.
# Below it the full load costs memory in proportion to the file and nothing else: the corpus's
# largest spreadsheet is 325 KB.
_XLSX_FULL_LOAD_MAX_BYTES = 8 * 1024 * 1024


class XlsxTextExtractor(TextExtractor):
    """XLSX via openpyxl. A worksheet is already a grid, so each sheet is a table.

    Opened with `data_only=True`: cached computed values are what a reader cares about, and it
    also means formula source (`=cmd|...`) never reaches the extracted text.

    The sheet's text is still produced -- the quality gate and language detection read it -- but
    marked `text_is_table`, so it is chunked once, as the table, not again as prose.
    """

    def extract(self, data: bytes) -> ExtractedContent:
        from openpyxl import load_workbook

        units: list[ExtractedUnit] = []
        headings: list[Heading] = []
        tables: list[ExtractedTable] = []

        full = len(data) <= _XLSX_FULL_LOAD_MAX_BYTES
        workbook = None
        try:
            workbook = load_workbook(io.BytesIO(data), read_only=not full, data_only=True)
            for index, worksheet in enumerate(workbook.worksheets, start=1):
                fills = _vertical_merge_fills(worksheet) if full else {}
                rows = []
                for r, row in enumerate(worksheet.iter_rows(), start=1):
                    rows.append([
                        fills.get((r, c), _xlsx_cell(cell)) for c, cell in enumerate(row, start=1)
                    ])
                rows = [row for row in rows if any(row)]

                # The sheet name is the only structural label a spreadsheet carries.
                headings.append(Heading(text=worksheet.title, level=1, unit_index=index))
                has_table = _table_has_content(rows)
                if has_table:
                    tables.append(
                        ExtractedTable(unit_index=index, rows=rows, heading=worksheet.title)
                    )

                text = "\n".join("\t".join(row).rstrip() for row in rows)
                units.append(
                    ExtractedUnit(
                        index=index, label=f"Sheet: {worksheet.title}", text=text,
                        text_is_table=has_table,
                    )
                )
        except Exception as e:
            raise ExtractionError(f"Could not read spreadsheet content: {e}") from e
        finally:
            if workbook is not None:
                try:
                    workbook.close()
                except Exception:
                    logger.debug("Workbook close failed; read-only handle will be collected")

        return ExtractedContent(units=units, headings=headings, tables=tables)


def _xlsx_cell(cell) -> str:
    """A cell as a person reads it, not as Python prints it.

    `str()` wrote a phone number as "9910257052.0" and a date as "2024-01-01 00:00:00", which
    neither a reader nor the word index matches against "9910257052" or "2024-01-01".
    """
    import datetime

    value = getattr(cell, "value", None)
    if value is None:
        return ""
    if isinstance(value, bool):
        return str(value).upper()
    if isinstance(value, datetime.datetime):
        if value.time() == datetime.time(0, 0):
            return value.date().isoformat()
        return value.isoformat(sep=" ", timespec="minutes")
    if isinstance(value, datetime.date | datetime.time):
        return value.isoformat()
    if isinstance(value, float):
        if "%" in (getattr(cell, "number_format", "") or ""):
            return f"{value * 100:.4g}%"
        if value.is_integer() and abs(value) < 1e15:
            return str(int(value))
        return f"{value:.12g}"
    return str(value).strip()


def _vertical_merge_fills(worksheet) -> dict[tuple[int, int], str]:
    """Values to write into cells hidden under a merged range, by (row, column).

    Only downwards: a label merged across several rows is written into each of them, so every row
    of a row group still says which label it belongs to. A merge across columns is a spanning
    header and stays in its first cell, or it would be repeated across the row.
    """
    fills: dict[tuple[int, int], str] = {}
    for merged in getattr(worksheet, "merged_cells", None) or []:
        if merged.max_row == merged.min_row:
            continue
        value = _xlsx_cell(worksheet.cell(row=merged.min_row, column=merged.min_col))
        if not value:
            continue
        for row in range(merged.min_row + 1, merged.max_row + 1):
            fills[(row, merged.min_col)] = value
    return fills


EXTRACTORS: dict[str, TextExtractor] = {
    PDF_MIME: PdfTextExtractor(),
    DOCX_MIME: DocxTextExtractor(),
    XLSX_MIME: XlsxTextExtractor(),
    PPTX_MIME: PptxTextExtractor(),
}
