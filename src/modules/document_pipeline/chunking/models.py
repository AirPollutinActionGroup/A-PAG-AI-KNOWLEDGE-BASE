"""Data contracts for the chunking stage."""

import uuid

from pydantic import BaseModel, Field, computed_field

# The one granularity shipped today. Named rather than numbered because our boundaries are
# semantic (a heading-bounded section), not a sliding window of N tokens — see the module
# docstring in chunker.py.
SECTION_SCALE = "section"


class Chunk(BaseModel):
    """One retrievable passage, with everything a citation needs to point back at the source."""

    index: int
    text: str
    page_number: int | None = None
    section_heading: str | None = None
    is_table: bool = False
    scale: str = SECTION_SCALE

    @computed_field
    @property
    def char_count(self) -> int:
        return len(self.text)


class ChunkingResult(BaseModel):
    """Output of chunking one document."""

    document_id: uuid.UUID
    chunks: list[Chunk] = Field(default_factory=list)

    @computed_field
    @property
    def chunk_count(self) -> int:
        return len(self.chunks)

    @computed_field
    @property
    def table_chunk_count(self) -> int:
        return sum(1 for c in self.chunks if c.is_table)

    @computed_field
    @property
    def total_chars(self) -> int:
        return sum(len(c.text) for c in self.chunks)
