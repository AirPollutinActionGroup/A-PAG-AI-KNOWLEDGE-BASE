"""Normalization service — clean, tag the language, gate on quality.

This stage never opens the original file. It only sees the extraction artifact, which is why it
has no per-format branching: by this point a PDF and a spreadsheet look the same.

Structure is carried through rather than re-derived. Extraction already read headings and tables
out of what each format states directly (Word styles, slide titles, sheet names) or, for PDF,
inferred them once from font size — re-deriving here would mean guessing from plain text what was
already known precisely upstream.
"""

import logging

from src.modules.document_pipeline.extraction.models import ExtractionResult
from src.modules.document_pipeline.normalization.models import (
    NormalizationResult,
    NormalizedUnit,
)
from src.modules.document_pipeline.normalization.quality_checker import QualityChecker
from src.modules.document_pipeline.normalization.text_cleaner import TextCleaner

logger = logging.getLogger(__name__)

# Language detection needs a reasonable sample; a handful of characters produces noise.
_MIN_CHARS_FOR_LANGUAGE = 40


class LanguageDetector:
    """Detects the document's dominant language, or returns None when it can't say."""

    def detect(self, text: str) -> str | None:
        sample = text.strip()
        if len(sample) < _MIN_CHARS_FOR_LANGUAGE:
            return None
        try:
            from langdetect import DetectorFactory, detect

            # langdetect is randomized by default, so the same document could be tagged
            # differently on a re-run. Seeding makes the stored artifact reproducible.
            DetectorFactory.seed = 0
            return detect(sample)
        except Exception as e:
            # A missing language tag is a nuisance; a failed document is an incident.
            logger.warning("Language detection failed, leaving it unset: %s", e)
            return None


class NormalizationService:
    """Turns an `ExtractionResult` into a `NormalizationResult`."""

    def __init__(
        self,
        cleaner: TextCleaner | None = None,
        quality_checker: QualityChecker | None = None,
        language_detector: LanguageDetector | None = None,
    ):
        self.cleaner = cleaner or TextCleaner()
        self.quality_checker = quality_checker or QualityChecker()
        self.language_detector = language_detector or LanguageDetector()

    def normalize(self, extraction: ExtractionResult) -> NormalizationResult:
        units = [
            NormalizedUnit(
                index=unit.index,
                label=unit.label,
                text=self.cleaner.clean(unit.text),
            )
            for unit in extraction.units
        ]

        # Headings get the same cleaning as body text so a heading matches its own occurrence in
        # the text rather than differing by an invisible character.
        headings = [
            heading.model_copy(update={"text": self.cleaner.clean(heading.text)})
            for heading in extraction.headings
        ]
        headings = [heading for heading in headings if heading.text]

        quality = self.quality_checker.check(units, extraction.mime_type)
        full_text = "\n\n".join(unit.text for unit in units if unit.text)
        language = self.language_detector.detect(full_text)

        result = NormalizationResult(
            document_id=extraction.document_id,
            mime_type=extraction.mime_type,
            language=language,
            units=units,
            headings=headings,
            tables=extraction.tables,
            quality=quality,
        )
        logger.info(
            "Normalization complete: doc_id=%s chars=%d language=%s passed=%s failures=%s",
            result.document_id, result.char_count, language, quality.passed, quality.failures,
        )
        return result
