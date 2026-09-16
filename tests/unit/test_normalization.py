"""Normalization stage tests — cleaning, language tagging, and the quality gate."""

import uuid

import pytest

from src.modules.document_pipeline.extraction.models import (
    ExtractedTable,
    ExtractedUnit,
    ExtractionResult,
    Heading,
)
from src.modules.document_pipeline.formats import DOCX_MIME, PDF_MIME, PPTX_MIME
from src.modules.document_pipeline.normalization.models import (
    EMPTY_TEXT,
    LOW_TEXT_DENSITY,
    NormalizationResult,
)
from src.modules.document_pipeline.normalization.quality_checker import QualityChecker
from src.modules.document_pipeline.normalization.service import NormalizationService
from src.modules.document_pipeline.normalization.text_cleaner import TextCleaner

DOC_ID = uuid.UUID("11111111-2222-3333-4444-555555555555")


def extraction(
    units: list[tuple[int, str, str]],
    mime_type: str = PDF_MIME,
    headings: list[Heading] | None = None,
    tables: list[ExtractedTable] | None = None,
) -> ExtractionResult:
    return ExtractionResult(
        document_id=DOC_ID,
        mime_type=mime_type,
        units=[ExtractedUnit(index=i, label=label, text=text) for i, label, text in units],
        headings=headings or [],
        tables=tables or [],
    )


@pytest.fixture
def service() -> NormalizationService:
    return NormalizationService()


# ==============================================================================
# Text cleaning
# ==============================================================================

def test_cleaner_collapses_whitespace_without_touching_content():
    """Runs of spaces and tabs collapse to one space, trailing space goes, and a run of blank
    lines collapses to a single paragraph break — the words themselves are untouched."""
    cleaned = TextCleaner().clean("Section  A\t\tScope   \n\n\n\nApplies to  all districts")

    assert cleaned == "Section A Scope\n\nApplies to all districts"


def test_cleaner_rejoins_words_hyphenated_across_lines():
    """A PDF breaking 'management' across a line must not leave 'manage- ment' in the index."""
    assert TextCleaner().clean("stubble manage-\nment plan") == "stubble management plan"


def test_cleaner_keeps_real_hyphens_intact():
    """Only a lowercase continuation is a broken word — 'Delhi-NCR' and 'Section 4-A' are not."""
    assert "Delhi-\nNCR" in TextCleaner().clean("Delhi-\nNCR region")
    assert "4-\nA" in TextCleaner().clean("Section 4-\nA applies")


def test_cleaner_removes_invisible_characters():
    """Zero-width characters survive extraction and silently break exact matching later."""
    cleaned = TextCleaner().clean("stub\u200bble﻿ burn­ing")

    assert cleaned == "stubble burning"


def test_cleaner_normalizes_unicode_compatibility_forms():
    """A ligature and a non-breaking space must match their plain equivalents."""
    cleaned = TextCleaner().clean("ofﬁce report")

    assert cleaned == "office report"


def test_cleaner_handles_empty_text():
    assert TextCleaner().clean("") == ""


# ==============================================================================
# Quality gate
# ==============================================================================

def test_document_with_no_text_fails_the_gate(service):
    result = service.normalize(extraction([(1, "Page 1", "")]))

    assert result.quality.passed is False
    assert EMPTY_TEXT in result.quality.failures


def test_scanned_pdf_is_flagged_as_low_text_density(service):
    """The safety net for the decision not to run OCR: a scan has no text layer, so it extracts
    to nearly nothing and must be flagged rather than indexed as an empty document."""
    result = service.normalize(extraction([
        (1, "Page 1", "Annexure"),
        (2, "Page 2", "3"),
        (3, "Page 3", ""),
    ]))

    assert result.quality.passed is False
    assert LOW_TEXT_DENSITY in result.quality.failures
    assert result.quality.details["chars_per_page"] < 50


def test_real_text_pdf_passes_the_gate(service):
    body = "This directive sets binding obligations on district authorities. " * 5
    result = service.normalize(extraction([(1, "Page 1", body)]))

    assert result.quality.passed is True
    assert result.quality.failures == []


@pytest.mark.parametrize("mime_type", [PPTX_MIME, DOCX_MIME])
def test_sparse_office_documents_are_not_flagged(service, mime_type):
    """Density is a PDF-only check. A deck of mostly visuals or a one-line memo is a legitimate
    document, not a failed extraction — flagging those would make the gate noise."""
    result = service.normalize(extraction([(1, "Slide 1", "Q3 Review")], mime_type=mime_type))

    assert result.quality.passed is True


def test_density_threshold_is_configurable():
    checker = QualityChecker(min_pdf_chars_per_page=5)
    service = NormalizationService(quality_checker=checker)

    result = service.normalize(extraction([(1, "Page 1", "Short but ok")]))

    assert result.quality.passed is True


# ==============================================================================
# Structure is carried through, not re-derived
# ==============================================================================

def test_headings_and_tables_survive_normalization(service):
    headings = [Heading(text="Section  A", level=2, unit_index=1)]
    tables = [ExtractedTable(unit_index=1, rows=[["District", "Target"], ["Ludhiana", "40%"]])]
    body = "Applies to all districts within the NCR airshed. " * 3

    result = service.normalize(
        extraction([(1, "Page 1", body)], headings=headings, tables=tables)
    )

    # Headings get the same cleaning as body text, so they still match their own occurrence.
    assert result.headings[0].text == "Section A"
    assert result.headings[0].level == 2
    assert result.tables[0].rows == [["District", "Target"], ["Ludhiana", "40%"]]


def test_unit_labels_are_preserved_for_citation(service):
    """A passage has to stay traceable to the page or slide it came from."""
    body = "Applies to all districts within the NCR airshed region and beyond. " * 2
    result = service.normalize(extraction([(1, "Page 1", body), (2, "Page 2", body)]))

    assert [u.label for u in result.units] == ["Page 1", "Page 2"]
    assert [u.index for u in result.units] == [1, 2]


# ==============================================================================
# Language
# ==============================================================================

def test_language_is_detected_for_substantial_english_text(service):
    body = (
        "This directive sets out binding obligations on district authorities across the region, "
        "and describes the penalties that apply when those obligations are not met."
    )
    result = service.normalize(extraction([(1, "Page 1", body)]))

    assert result.language == "en"


def test_language_is_left_unset_for_text_too_short_to_judge(service):
    """A wrong language tag is worse than none — it would misroute tokenization later."""
    result = service.normalize(extraction([(1, "Slide 1", "Q3")], mime_type=PPTX_MIME))

    assert result.language is None


def test_language_failure_leaves_the_tag_unset_instead_of_raising(monkeypatch):
    """A missing language tag is a nuisance; a document stranded at NORMALIZATION_FAILED because
    language detection had a bad day is an incident. The detector absorbs its own failures."""
    import src.modules.document_pipeline.normalization.service as service_module

    def exploding_detect(_text):
        raise RuntimeError("model unavailable")

    monkeypatch.setattr(service_module, "detect", exploding_detect, raising=False)
    monkeypatch.setitem(
        __import__("sys").modules,
        "langdetect",
        type("m", (), {"detect": exploding_detect, "DetectorFactory": type("d", (), {"seed": 0})}),
    )

    detector = service_module.LanguageDetector()

    assert detector.detect("This is a long enough sample of English text to be classified.") is None


# ==============================================================================
# The stored artifact
# ==============================================================================

def test_result_survives_a_json_round_trip(service):
    body = "This directive sets out binding obligations on district authorities. " * 3
    tables = [ExtractedTable(unit_index=1, rows=[["a", "b"]])]
    result = service.normalize(extraction([(1, "Page 1", body)], tables=tables))

    restored = NormalizationResult.model_validate_json(result.model_dump_json())

    assert restored.document_id == result.document_id
    assert restored.full_text == result.full_text
    assert restored.language == result.language
    assert restored.quality.passed == result.quality.passed
    assert [t.rows for t in restored.tables] == [t.rows for t in result.tables]
