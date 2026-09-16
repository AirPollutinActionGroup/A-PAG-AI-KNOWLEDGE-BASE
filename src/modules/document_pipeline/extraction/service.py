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
        )
        logger.info(
            "Extraction complete: doc_id=%s mime=%s units=%d chars=%d tables=%d headings=%d",
            document_id, mime_type, result.unit_count, result.char_count,
            len(result.tables), len(result.headings),
        )
        return result


def unextractable_formats() -> set[str]:
    """Formats a user can upload but nothing here can read.

    Should always be empty — a file accepted at validation with no way to extract it would sit at
    EXTRACTION_FAILED forever. Asserted by a test so adding a format to `formats.py` without an
    extractor fails loudly at CI rather than in production.
    """
    return set(FORMATS) - set(EXTRACTORS)
