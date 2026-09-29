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
from src.modules.document_pipeline.formats import FORMATS

logger = logging.getLogger(__name__)


class ExtractionService:
    """Runs the extractor registered for a document's MIME type."""

    def __init__(self, extractors: dict[str, TextExtractor] | None = None):
        self.extractors = extractors if extractors is not None else EXTRACTORS

    def extract(self, document_id: uuid.UUID, data: bytes, mime_type: str) -> ExtractionResult:
        """Extracts content, or raises `ExtractionError` if the format is unsupported or the
        bytes can't be read."""
        extractor = self.extractors.get(mime_type)
        if extractor is None:
            raise ExtractionError(f"No extractor registered for '{mime_type}'.")

        content = extractor.extract(data)
        result = ExtractionResult(
            document_id=document_id,
            mime_type=mime_type,
            units=content.units,
            headings=content.headings,
            tables=content.tables,
            ocr_pages=content.ocr_pages,
            ocr_skipped_pages=content.ocr_skipped_pages,
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
        return result


def unextractable_formats() -> set[str]:
    """Formats a user can upload but nothing here can read.

    Should always be empty — a file accepted at validation with no way to extract it would sit at
    EXTRACTION_FAILED forever. Asserted by a test so adding a format to `formats.py` without an
    extractor fails loudly at CI rather than in production.
    """
    return set(FORMATS) - set(EXTRACTORS)
