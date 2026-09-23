"""Chunker tests — the splitting rules themselves, with no storage or database around them.

These are the tests that decide retrieval quality. A chunk boundary in the wrong place separates
an obligation from the condition that qualifies it, and the resulting answer is confidently wrong
while still carrying a citation — which is worse than no answer.
"""

import uuid

import pytest

from src.modules.document_pipeline.chunking.chunker import Chunker
from src.modules.document_pipeline.chunking.models import SECTION_SCALE
from src.modules.document_pipeline.chunking.service import ChunkingService
from src.modules.document_pipeline.extraction.models import ExtractedTable, Heading
from src.modules.document_pipeline.normalization.models import (
    NormalizationResult,
    NormalizedUnit,
    QualityCheckResult,
)

PDF_MIME = "application/pdf"


def build(units, headings=None, tables=None) -> NormalizationResult:
    """Assembles the artifact chunking reads, from (index, label, text) tuples."""
    return NormalizationResult(
        document_id=uuid.uuid4(),
        mime_type=PDF_MIME,
        language="en",
        units=[NormalizedUnit(index=i, label=lbl, text=txt) for i, lbl, txt in units],
        headings=headings or [],
        tables=tables or [],
        quality=QualityCheckResult(passed=True),
    )


def sentences(n: int, word: str = "obligation") -> str:
    """n distinct sentences, so splits can be located precisely in assertions."""
    return " ".join(f"This is {word} number {i} for the district authority." for i in range(n))


# ==============================================================================
# Structure: cutting at the document's own boundaries
# ==============================================================================

def test_each_heading_starts_a_new_chunk():
    result = build(
        units=[(1, "Page 1", "1. Scope\nApplies to all districts.\n\n2. Penalties\nGraduated fines apply.")],
        headings=[
            Heading(text="1. Scope", level=1, unit_index=1),
            Heading(text="2. Penalties", level=1, unit_index=1),
        ],
    )

    chunks = Chunker().chunk(result)

    assert len(chunks) == 2
    assert chunks[0].section_heading == "1. Scope"
    assert chunks[1].section_heading == "2. Penalties"
    assert "Graduated fines" in chunks[1].text


def test_chunks_carry_the_page_they_came_from():
    """Without this a citation can only name the document, which is not good enough for a
    government submission."""
    result = build(units=[(1, "Page 1", "First page text."), (2, "Page 2", "Second page text.")])

    chunks = Chunker().chunk(result)

    assert [c.page_number for c in chunks] == [1, 2]


def test_section_heading_carries_across_a_page_break():
    """A section opened on page 1 still governs its prose where it runs onto page 2 — the
    citation should keep naming that section rather than going blank."""
    result = build(
        units=[
            (1, "Page 1", "3. Enforcement\nThe authority shall act."),
            (2, "Page 2", "Continued enforcement provisions apply."),
        ],
        headings=[Heading(text="3. Enforcement", level=1, unit_index=1)],
    )

    chunks = Chunker().chunk(result)

    assert all(c.section_heading == "3. Enforcement" for c in chunks)


def test_text_before_the_first_heading_is_kept():
    """Preambles and covering paragraphs are content, not padding."""
    result = build(
        units=[(1, "Page 1", "Issued under the 2026 framework.\n\n1. Scope\nApplies widely.")],
        headings=[Heading(text="1. Scope", level=1, unit_index=1)],
    )

    chunks = Chunker().chunk(result)

    assert "Issued under the 2026 framework." in chunks[0].text
    assert chunks[0].section_heading is None


def test_document_with_no_headings_still_chunks():
    """A one-page memo legitimately has no headings — the quality gate deliberately does not
    flag that, so the chunker must not choke on it."""
    result = build(units=[(1, "Page 1", "A short memo with no structure at all.")])

    chunks = Chunker().chunk(result)

    assert len(chunks) == 1
    assert chunks[0].section_heading is None


def test_heading_not_found_in_text_is_skipped_not_guessed():
    """Cleaning can alter spacing, and the PDF extractor infers headings from font size. If the
    heading text cannot be located, degrade to unit-level chunking rather than cut blindly."""
    result = build(
        units=[(1, "Page 1", "Body text that never mentions the heading.")],
        headings=[Heading(text="Nonexistent Heading", level=1, unit_index=1)],
    )

    chunks = Chunker().chunk(result)

    assert len(chunks) == 1
    assert chunks[0].text == "Body text that never mentions the heading."


def test_empty_units_are_skipped():
    """Blank pages and image-only slides must not become empty chunks."""
    result = build(units=[(1, "Page 1", "Real content."), (2, "Page 2", "   ")])

    chunks = Chunker().chunk(result)

    assert len(chunks) == 1


# ==============================================================================
# Sizing
# ==============================================================================

def test_long_section_is_split():
    result = build(units=[(1, "Page 1", sentences(120))])

    chunks = Chunker().chunk(result)

    assert len(chunks) > 1
    assert all(c.char_count <= 2000 for c in chunks)


def test_split_prefers_paragraph_boundaries():
    """Paragraphs are the strongest boundary available below a heading."""
    para_a = sentences(20, "alpha")
    para_b = sentences(20, "beta")
    result = build(units=[(1, "Page 1", f"{para_a}\n\n{para_b}")])

    chunks = Chunker(target_chars=len(para_a) + 50, max_chars=len(para_a) + 100).chunk(result)

    assert len(chunks) == 2
    assert "alpha" in chunks[0].text and "beta" not in chunks[0].text


def test_splitting_does_not_shatter_into_fragments():
    """Sentence-level splitting without repacking would emit one chunk per sentence, which is
    far too small to carry meaning."""
    result = build(units=[(1, "Page 1", sentences(60))])

    chunks = Chunker().chunk(result)

    assert all(c.char_count > 300 for c in chunks[:-1])


def test_text_with_no_boundaries_is_hard_wrapped():
    """Pathological input must still terminate rather than emit one oversized chunk."""
    result = build(units=[(1, "Page 1", "x" * 7000)])

    chunks = Chunker().chunk(result)

    assert len(chunks) > 1
    assert all(c.char_count <= 2000 for c in chunks)


def test_short_document_is_one_chunk():
    result = build(units=[(1, "Page 1", "Brief directive text.")])
    assert len(Chunker().chunk(result)) == 1


# ==============================================================================
# Tables
# ==============================================================================

def test_table_becomes_its_own_chunk():
    result = build(
        units=[(1, "Page 1", "See the table below.")],
        tables=[ExtractedTable(unit_index=1, rows=[["District", "Target"], ["Patna", "40%"]])],
    )

    chunks = Chunker().chunk(result)

    table_chunks = [c for c in chunks if c.is_table]
    assert len(table_chunks) == 1
    assert "District" in table_chunks[0].text and "Patna" in table_chunks[0].text


def test_large_table_splits_into_row_groups_repeating_the_header():
    """The rule that makes split tables usable. Without the repeated header, group 7 of a budget
    sheet is a wall of numbers with no idea what the columns mean."""
    rows = [["District", "Target", "Deadline"]]
    rows += [[f"District {i}", f"{i}%", "2027-01-31"] for i in range(200)]
    result = build(
        units=[(1, "Page 1", "Annexure A.")],
        tables=[ExtractedTable(unit_index=1, rows=rows)],
    )

    table_chunks = [c for c in Chunker().chunk(result) if c.is_table]

    assert len(table_chunks) > 1
    assert all(c.text.startswith("District | Target | Deadline") for c in table_chunks)


def test_table_rows_are_never_split_mid_row():
    rows = [["A", "B"]] + [[f"value-{i}", f"other-{i}"] for i in range(300)]
    result = build(
        units=[(1, "Page 1", "Table.")],
        tables=[ExtractedTable(unit_index=1, rows=rows)],
    )

    for chunk in (c for c in Chunker().chunk(result) if c.is_table):
        for line in chunk.text.split("\n")[1:]:
            assert line.count("|") == 1, f"row was cut mid-way: {line!r}"


def test_table_chunks_inherit_the_surrounding_section():
    result = build(
        units=[(1, "Page 1", "5. Targets\nAs set out below.")],
        headings=[Heading(text="5. Targets", level=1, unit_index=1)],
        tables=[ExtractedTable(unit_index=1, rows=[["District", "Target"], ["Patna", "40%"]])],
    )

    table_chunk = next(c for c in Chunker().chunk(result) if c.is_table)

    assert table_chunk.section_heading == "5. Targets"


def test_table_in_a_multi_heading_unit_claims_no_section():
    """Regression: a DOCX is a single unit, so every heading and table shares unit_index=1 and a
    table's position within it is unknown. Attributing it to whichever heading happened to come
    last silently mis-cited every table in a multi-section Word document. An absent citation is
    recoverable; a confident wrong one is not."""
    result = build(
        units=[(1, "Body", "1. Scope\nApplies widely.\n\n2. Penalties\nFines apply.")],
        headings=[
            Heading(text="1. Scope", level=1, unit_index=1),
            Heading(text="2. Penalties", level=1, unit_index=1),
        ],
        tables=[ExtractedTable(unit_index=1, rows=[["District", "Target"], ["Patna", "40%"]])],
    )

    table_chunk = next(c for c in Chunker().chunk(result) if c.is_table)

    assert table_chunk.section_heading is None
    # The page is still known, so the citation degrades rather than disappearing.
    assert table_chunk.page_number == 1


def test_table_in_a_single_heading_unit_keeps_its_section():
    """The PPTX/XLSX case: one slide or worksheet, one title, and the table plainly belongs to
    it. Refusing to attribute here would throw away a citation we genuinely have."""
    result = build(
        units=[(2, "Slide 2", "5. Targets\nAs set out below.")],
        headings=[Heading(text="5. Targets", level=1, unit_index=2)],
        tables=[ExtractedTable(unit_index=2, rows=[["District", "Target"], ["Patna", "40%"]])],
    )

    table_chunk = next(c for c in Chunker().chunk(result) if c.is_table)

    assert table_chunk.section_heading == "5. Targets"


def test_empty_table_produces_nothing():
    result = build(
        units=[(1, "Page 1", "Text.")],
        tables=[ExtractedTable(unit_index=1, rows=[])],
    )
    assert not [c for c in Chunker().chunk(result) if c.is_table]


# ==============================================================================
# Contract
# ==============================================================================

def test_chunk_indices_are_contiguous_from_zero():
    """The unique (document_id, scale, chunk_index) index depends on this, and so does
    reassembling passages in document order."""
    result = build(
        units=[(1, "Page 1", sentences(80)), (2, "Page 2", sentences(80))],
        tables=[ExtractedTable(unit_index=1, rows=[["A", "B"], ["1", "2"]])],
    )

    chunks = Chunker().chunk(result)

    assert [c.index for c in chunks] == list(range(len(chunks)))


def test_every_chunk_carries_the_scale():
    result = build(units=[(1, "Page 1", "Text.")])
    assert all(c.scale == SECTION_SCALE for c in Chunker().chunk(result))


def test_no_chunk_is_empty_or_whitespace():
    result = build(units=[(1, "Page 1", "Real text.\n\n\n\n   \n\nMore text.")])
    assert all(c.text.strip() for c in Chunker().chunk(result))


@pytest.mark.parametrize("n_sentences", [1, 5, 50, 200])
def test_service_returns_a_populated_result(n_sentences):
    result = build(units=[(1, "Page 1", sentences(n_sentences))])

    out = ChunkingService().chunk(result)

    assert out.document_id == result.document_id
    assert out.chunk_count == len(out.chunks) > 0
    assert out.total_chars > 0
