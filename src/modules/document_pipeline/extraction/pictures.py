"""Reading the words and numbers inside pictures in Word and PowerPoint files.

A deck's table is often a screenshot: the numbers are pixels, and without reading them "average max
SOx 1209 mg/Nm3" on slide 9 of "TPPs Delhi NCR" cannot be found by anyone who asks for it. Our own
extractors read what a format *states* (a slide's text boxes, a Word table); a picture states
nothing, so something has to look at it. `picture_source.py` finds the pictures, and
`picture_tables.py` reads each -- the grid from its ruling lines, the words with the OCR that
already reads scanned pages.

Whatever comes out of a picture is **marked as such**, in the passage text the reader and the
model both see: OCR measured 0.99 confidence on this corpus and still drops word boundaries and
misreads sideways text, and a figure in a government submission should be checked against the
original. A table's first line says so; a figure's label does.

Switched off by `PICTURES_ENABLED` or by `OCR_ENABLED=false`, or when img2table is not installed:
pictures are then simply not read and a document extracts exactly as before, with one WARNING
saying why.
"""

import hashlib
import logging
import threading
from abc import ABC, abstractmethod
from collections import Counter

from pydantic import BaseModel, Field

from src.core.config import settings
from src.modules.document_pipeline.extraction.models import (
    ExtractedContent,
    ExtractedFigure,
    ExtractedTable,
)
from src.modules.document_pipeline.formats import DOCX_MIME, PPTX_MIME

logger = logging.getLogger(__name__)

# Formats whose pictures are read. PDF is deliberately not here: its pages are read by the PDF
# extractor and OCR, and a picture inside a PDF page is already part of what OCR sees.
PICTURE_FORMATS = (PPTX_MIME, DOCX_MIME)

# A picture repeated on this many slides or pages is a logo or a template ornament.
_REPEATED = 3

# Said on the first line of every table read from a picture, where the chunker keeps it as the
# table's caption and repeats it in every row group.
TABLE_NOTICE = "table read from a picture by OCR; check against the original before citing"


class PictureText(BaseModel):
    """What one picture said: the text on it, and any table it is a picture of."""

    unit_index: int
    text: str = ""
    caption: str | None = None
    heading: str | None = None
    tables: list[list[list[str]]] = Field(default_factory=list)


class PictureReader(ABC):
    """Reads the pictures in one Office file."""

    @property
    @abstractmethod
    def name(self) -> str:
        """Identifies the reader in logs."""

    @abstractmethod
    def read(self, data: bytes, mime_type: str) -> list[PictureText]:
        """Returns what each worthwhile picture in the file says. Never raises for one bad
        picture; may raise if the whole file cannot be opened, which the caller treats as 'no
        pictures read' and not as a failed document."""


class OfficePictureReader(PictureReader):
    """Finds the pictures in a Word or PowerPoint file and reads each with `TableImageReader`."""

    def __init__(self, table_reader=None):
        self._table_reader = table_reader
        self._lock = threading.Lock()

    @property
    def name(self) -> str:
        return "img2table+rapidocr"

    def _reader(self):
        if self._table_reader is not None:
            return self._table_reader
        with self._lock:
            if self._table_reader is None:
                from src.modules.document_pipeline.extraction.picture_tables import (
                    TableImageReader,
                )

                self._table_reader = TableImageReader(
                    threads=settings.OCR_THREADS,
                    min_confidence=settings.OCR_MIN_CONFIDENCE,
                    cell_min_confidence=settings.PICTURE_TABLE_MIN_CONFIDENCE,
                )
            return self._table_reader

    def read(self, data: bytes, mime_type: str) -> list[PictureText]:
        from src.modules.document_pipeline.extraction.picture_source import (
            RASTER_EXTENSIONS,
            VECTOR_EXTENSIONS,
            decode_picture,
            find_pictures,
            rasterise_vectors,
        )

        found = find_pictures(data, mime_type)
        digest = [hashlib.sha256(p.blob).hexdigest() for p in found]
        # A picture used again and again is a logo, found by its bytes.
        units_with = Counter(digest)
        seen: set[str] = set()
        wanted = []
        for p, d in zip(found, digest, strict=True):
            if units_with[d] >= _REPEATED or d in seen:
                continue
            seen.add(d)
            if p.extension in RASTER_EXTENSIONS or p.extension in VECTOR_EXTENSIONS:
                wanted.append((p, d))

        vectors = {i: (p.blob, p.extension) for i, (p, _) in enumerate(wanted)
                   if p.extension in VECTOR_EXTENSIONS}
        rendered = rasterise_vectors(vectors)
        if vectors and not rendered:
            logger.warning(
                "%d vector picture(s) (EMF/WMF) were not read: emf2svg and rsvg-convert are not "
                "installed here (the image installs them; a development machine needs them too)",
                len(vectors),
            )
        elif len(rendered) < len(vectors):
            logger.warning("%d of %d vector picture(s) could not be rendered",
                           len(vectors) - len(rendered), len(vectors))

        results: list[PictureText] = []
        skipped_over_limit = 0
        for i, (picture, _) in enumerate(wanted):
            labels_blob = None
            if picture.extension in VECTOR_EXTENSIONS:
                drawn = rendered.get(i)
                blob, labels_blob = (drawn.png, drawn.labels) if drawn else (None, None)
            else:
                blob = picture.blob
            if blob is None:
                continue
            image = decode_picture(blob)
            if image is None or min(image.shape[:2]) < settings.PICTURE_MIN_EDGE_PX:
                continue
            if len(results) >= settings.PICTURE_MAX_PER_DOCUMENT:
                skipped_over_limit += 1
                continue
            labels = decode_picture(labels_blob) if labels_blob else None
            try:
                reading = (self._reader().read(image, labels_image=labels) if labels is not None
                           else self._reader().read(image))
            except Exception as e:
                logger.warning("Could not read a picture on unit %s: %s", picture.unit_index, e)
                continue
            tables = [_with_notice(t.rows, t.title) for t in reading.tables]
            text = "\n".join(reading.lines)
            if not tables and not text:
                continue
            results.append(PictureText(
                unit_index=picture.unit_index, heading=picture.heading, tables=tables, text=text,
            ))

        if skipped_over_limit:
            logger.warning(
                "%d picture(s) past the per-document limit (%d) were not read",
                skipped_over_limit, settings.PICTURE_MAX_PER_DOCUMENT,
            )
        return results


def _with_notice(rows: list[list[str]], title: str) -> list[list[str]]:
    """The table with a first line naming it and saying where it came from. One filled cell on a
    row of several is what the chunker reads as a title, so it stays the caption, above the real
    header and repeated with it in every row group."""
    width = max((len(r) for r in rows), default=1)
    caption = f"{title.strip()} ({TABLE_NOTICE})" if title.strip() else f"({TABLE_NOTICE})"
    return [[caption] + [""] * (width - 1), *rows]


def merge_pictures(content: ExtractedContent, pictures: list[PictureText]) -> None:
    """Adds what pictures said to an extraction: tables as tables, other text as figures.

    Each lands under the section it sits in -- a slide's title, or for Word the heading it was
    found under -- so a citation names where the picture actually was.
    """
    titles: dict[int, str] = {}
    per_unit: Counter[int] = Counter()
    for heading in content.headings:
        titles.setdefault(heading.unit_index, heading.text)
        per_unit[heading.unit_index] += 1
    known = {unit.index for unit in content.units}
    own_headings = {h.text.strip(): h.text for h in content.headings}

    for picture in pictures:
        unit = picture.unit_index if picture.unit_index in known else min(known, default=None)
        if unit is None:
            continue
        # A unit with one heading -- a slide, with its title -- is that heading's section. A unit
        # with several is a Word file's single unit, where "the unit's heading" would be the
        # first one in the document; there the heading the picture was found under is used,
        # matched against ours so the chunker can find the section by its exact text.
        seen = own_headings.get(picture.heading.strip(), picture.heading) if picture.heading else None
        section = seen if per_unit[unit] > 1 else (titles.get(unit) or seen)

        for rows in picture.tables:
            content.tables.append(ExtractedTable(unit_index=unit, rows=rows, heading=section))
        if picture.text.strip() or picture.caption:
            content.figures.append(ExtractedFigure(
                unit_index=unit, text=picture.text.strip(), caption=picture.caption,
                heading=section,
            ))


_reader: PictureReader | None = None
_reader_lock = threading.Lock()
_reader_failed = False


def get_picture_reader() -> PictureReader | None:
    """The process's picture reader, or None when pictures are not to be read.

    None rather than an exception, for the same reason as `get_ocr_engine`: this is an
    enhancement, and a deployment without it should extract every document exactly as it did
    before it existed and say so once, not turn a missing dependency into a dead pipeline.
    """
    global _reader, _reader_failed

    if not settings.PICTURES_ENABLED or not settings.OCR_ENABLED or _reader_failed:
        return None
    if _reader is not None:
        return _reader
    with _reader_lock:
        if _reader is not None:
            return _reader
        try:
            import cv2  # noqa: F401
            import img2table  # noqa: F401
            import rapidocr  # noqa: F401
        except ImportError as e:
            logger.warning(
                "Pictures in Word and PowerPoint files are not read: %s is not installed. Set "
                "PICTURES_ENABLED=false to silence this.", e.name,
            )
            _reader_failed = True
            return None
        _reader = OfficePictureReader()
        return _reader


def reset_picture_reader() -> None:
    """For tests: forget the cached reader and any recorded failure."""
    global _reader, _reader_failed
    _reader, _reader_failed = None, False
