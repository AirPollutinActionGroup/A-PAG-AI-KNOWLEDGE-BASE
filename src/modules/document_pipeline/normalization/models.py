"""Data contracts for the normalization stage."""

import uuid

from pydantic import BaseModel, Field, computed_field

from src.modules.document_pipeline.extraction.models import ExtractedTable, Heading

# Quality gate failure codes.
EMPTY_TEXT = "EMPTY_TEXT"
LOW_TEXT_DENSITY = "LOW_TEXT_DENSITY"


class NormalizedUnit(BaseModel):
    """An extracted unit after cleaning. Indices and labels are carried through unchanged so a
    passage can still be cited back to the page or slide it came from."""

    index: int
    label: str
    text: str


class QualityCheckResult(BaseModel):
    """Outcome of the gate that decides whether normalized content is fit to move on.

    Deliberately few checks. Two more were considered and left out because they would fire on
    perfectly good documents: "has headings" (a one-page memo legitimately has none) and "tables
    have uniform row widths" (merged cells make ragged rows normal). A gate that cries wolf gets
    ignored, which is worse than no gate.
    """

    passed: bool
    failures: list[str] = Field(default_factory=list)
    details: dict[str, float | int | str] = Field(default_factory=dict)


class NormalizationResult(BaseModel):
    """The stored `normalized.json` artifact — the input the future chunking stage will read."""

    document_id: uuid.UUID
    mime_type: str
    language: str | None = None
    units: list[NormalizedUnit] = Field(default_factory=list)
    headings: list[Heading] = Field(default_factory=list)
    tables: list[ExtractedTable] = Field(default_factory=list)
    quality: QualityCheckResult

    @computed_field
    @property
    def unit_count(self) -> int:
        return len(self.units)

    @computed_field
    @property
    def full_text(self) -> str:
        return "\n\n".join(unit.text for unit in self.units if unit.text)

    @computed_field
    @property
    def char_count(self) -> int:
        return len(self.full_text)
