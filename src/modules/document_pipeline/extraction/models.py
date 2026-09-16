"""Data contracts for the extraction stage.

An `ExtractionResult` is what gets serialized to `extracted/{document_id}.json` and handed to
normalization. It is deliberately format-neutral: by the time it exists, nothing downstream needs
to know whether the bytes were a PDF or a spreadsheet.
"""

import uuid

from pydantic import BaseModel, Field, computed_field

# Set on every result today. The field exists so a future OCR path (or a swapped-in layout-aware
# parser) is distinguishable in stored artifacts without a schema change — see KNOWN_DEBTS.md.
METHOD_NATIVE = "NATIVE"


class ExtractedUnit(BaseModel):
    """One page / slide / worksheet — whatever the format's natural division is.

    DOCX has no such division (Word text reflows, there are no pages until it is rendered — the
    same reason `page_count` is None for DOCX at validation), so a Word file produces exactly one
    unit covering the whole document body.
    """

    index: int
    label: str
    text: str


class ExtractedTable(BaseModel):
    """A table as a grid of cell strings, kept separate from the flowing text.

    Tables are carried as structured rows rather than flattened into prose because a table
    rendered as a sentence is unusable for a factual question — the design docs call this out
    explicitly as a retrieval requirement.
    """

    unit_index: int
    rows: list[list[str]]

    @computed_field
    @property
    def row_count(self) -> int:
        return len(self.rows)


class Heading(BaseModel):
    """A heading the *format itself* identified (a Word style, a slide title, a sheet name), or
    for PDF, one inferred from relative font size — see `PdfTextExtractor`."""

    text: str
    level: int
    unit_index: int


class ExtractionResult(BaseModel):
    """The stored `extraction.json` artifact."""

    document_id: uuid.UUID
    mime_type: str
    extraction_method: str = METHOD_NATIVE
    units: list[ExtractedUnit] = Field(default_factory=list)
    headings: list[Heading] = Field(default_factory=list)
    tables: list[ExtractedTable] = Field(default_factory=list)

    @computed_field
    @property
    def unit_count(self) -> int:
        return len(self.units)

    @computed_field
    @property
    def full_text(self) -> str:
        """Units joined in order. Computed rather than stored so it can never drift out of sync
        with `units` after a round trip through JSON."""
        return "\n\n".join(unit.text for unit in self.units if unit.text)

    @computed_field
    @property
    def char_count(self) -> int:
        return len(self.full_text)


class ExtractedContent(BaseModel):
    """What an individual extractor returns — the document-level fields (id, mime type) are the
    service's to fill in, so extractors stay unaware of the pipeline around them."""

    units: list[ExtractedUnit] = Field(default_factory=list)
    headings: list[Heading] = Field(default_factory=list)
    tables: list[ExtractedTable] = Field(default_factory=list)


class ExtractionError(Exception):
    """Raised by an extractor when a file cannot be read at all.

    Distinct from "read fine, but there was barely any text in it" — that is a quality judgement
    the normalization stage's gate makes, not a failure to extract.
    """
