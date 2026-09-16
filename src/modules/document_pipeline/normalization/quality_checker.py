"""The quality gate — the last point where a document can be stopped before it is treated as
searchable knowledge.

This is where the deliberate decision not to run OCR gets its safety net. A scanned page has no
text layer, so it extracts to roughly nothing; without a check, that document would sail through
and sit in the knowledge base as a title with no content, answering no questions and silently
degrading retrieval. Flagging it makes the "these are all typed documents" assumption falsifiable
instead of load-bearing.
"""

import logging

from src.core.config import settings
from src.modules.document_pipeline.formats import PDF_MIME
from src.modules.document_pipeline.normalization.models import (
    EMPTY_TEXT,
    LOW_TEXT_DENSITY,
    NormalizedUnit,
    QualityCheckResult,
)

logger = logging.getLogger(__name__)


class QualityChecker:
    """Decides whether normalized content is fit to move on."""

    def __init__(self, min_pdf_chars_per_page: int | None = None):
        self.min_pdf_chars_per_page = (
            settings.MIN_PDF_CHARS_PER_PAGE
            if min_pdf_chars_per_page is None
            else min_pdf_chars_per_page
        )

    def check(self, units: list[NormalizedUnit], mime_type: str) -> QualityCheckResult:
        char_count = sum(len(unit.text) for unit in units)
        unit_count = len(units)
        details: dict[str, float | int | str] = {
            "char_count": char_count,
            "unit_count": unit_count,
        }
        failures: list[str] = []

        if char_count == 0:
            failures.append(EMPTY_TEXT)
        elif mime_type == PDF_MIME and unit_count > 0:
            # Scoped to PDF on purpose: it is the only format that can be a scan. A .pptx with a
            # few words per slide is a legitimate visual deck, and a .xlsx of sparse numbers is a
            # legitimate spreadsheet — neither is a failed extraction.
            chars_per_page = char_count / unit_count
            details["chars_per_page"] = round(chars_per_page, 1)
            if chars_per_page < self.min_pdf_chars_per_page:
                failures.append(LOW_TEXT_DENSITY)

        result = QualityCheckResult(passed=not failures, failures=failures, details=details)
        if failures:
            logger.warning("Quality gate failed: %s (%s)", failures, details)
        return result
