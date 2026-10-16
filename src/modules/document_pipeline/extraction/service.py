"""Extraction service — picks the right extractor for a document's format and runs it."""

import logging
import uuid

from src.modules.document_pipeline.extraction.extractors import (
    EXTRACTORS,
    TextExtractor,
)
from src.modules.document_pipeline.extraction.models import (
    ExtractionError,
    ExtractionResult,
)
from src.modules.document_pipeline.extraction.pictures import (
    PICTURE_FORMATS,
    PictureReader,
    get_picture_reader,
    merge_pictures,
)
from src.modules.document_pipeline.formats import FORMATS

logger = logging.getLogger(__name__)


class ExtractionService:
    """Runs the extractor registered for a document's MIME type."""

    def __init__(
        self,
        extractors: dict[str, TextExtractor] | None = None,
        picture_reader: PictureReader | None = None,
    ):
        self.extractors = extractors if extractors is not None else EXTRACTORS
        # An explicit reader is for tests. In production it is resolved lazily on the first
        # document with pictures, so a worker that never meets one never loads Docling.
        self._picture_reader = picture_reader

    def extract(self, document_id: uuid.UUID, data: bytes, mime_type: str) -> ExtractionResult:
        """Extracts content, or raises `ExtractionError` if the format is unsupported or the
        bytes can't be read."""
        extractor = self.extractors.get(mime_type)
        if extractor is None:
            raise ExtractionError(f"No extractor registered for '{mime_type}'.")

        content = extractor.extract(data)
        if mime_type in PICTURE_FORMATS:
            self._read_pictures(document_id, data, mime_type, content)
        result = ExtractionResult(
            document_id=document_id,
            mime_type=mime_type,
            units=content.units,
            headings=content.headings,
            tables=content.tables,
            ocr_pages=content.ocr_pages,
            ocr_skipped_pages=content.ocr_skipped_pages,
            ocr_unreadable_lines=content.ocr_unreadable_lines,
            figures=content.figures,
        )
        # Derived from the pages rather than passed in, so the recorded method cannot disagree
        # with the units it describes.
        result.extraction_method = result.resolve_method()

        logger.info(
            "Extraction complete: doc_id=%s mime=%s method=%s units=%d chars=%d tables=%d "
            "headings=%d ocr_pages=%d",
            document_id, mime_type, result.extraction_method, result.unit_count,
            result.char_count, len(result.tables), len(result.headings), result.ocr_page_count,
        )
        if content.ocr_skipped_pages:
            logger.warning(
                "doc_id=%s: %d page(s) past the OCR budget were left unread: %s",
                document_id, len(content.ocr_skipped_pages), content.ocr_skipped_pages[:20],
            )
        if content.ocr_unreadable_lines:
            logger.warning(
                "doc_id=%s: %d OCR line(s) could not be read and were left out, by page: %s",
                document_id, sum(content.ocr_unreadable_lines.values()),
                content.ocr_unreadable_lines,
            )
        return result

    def _read_pictures(self, document_id: uuid.UUID, data: bytes, mime_type: str, content) -> None:
        """Adds what the document's pictures say. Never fails the document: its text and tables
        are already read, and a picture that cannot be is a gap, not a reason to lose them."""
        reader = self._picture_reader or get_picture_reader()
        if reader is None:
            return
        try:
            pictures = reader.read(data, mime_type)
        except Exception as e:
            logger.warning("doc_id=%s: pictures could not be read (%s); extracting without them",
                           document_id, e)
            return
        merge_pictures(content, pictures)
        if pictures:
            logger.info("doc_id=%s: read %d picture(s) with %s",
                        document_id, len(pictures), reader.name)


def unextractable_formats() -> set[str]:
    """Formats a user can upload but nothing here can read.

    Should always be empty — a file accepted at validation with no way to extract it would sit at
    EXTRACTION_FAILED forever. Asserted by a test so adding a format to `formats.py` without an
    extractor fails loudly at CI rather than in production.
    """
    return set(FORMATS) - set(EXTRACTORS)
