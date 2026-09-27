"""DTOs for semantic retrieval.

A result is a *passage*, not a document: the whole point of chunking was that a citation should
point at the specific text that answers the question, not at a 200-page PDF. The citation fields
therefore travel with the text rather than being looked up afterwards.
"""

import uuid

from pydantic import BaseModel, Field


class RetrievedChunk(BaseModel):
    """One passage, with everything needed to cite it."""

    chunk_id: uuid.UUID
    document_id: uuid.UUID
    filename: str
    text: str

    # The citation contract established at chunking time. `page_number` is a per-format unit
    # (page / slide / worksheet), and `section_heading` is None where extraction could not
    # attribute the passage to one heading — see KNOWN_DEBTS.md #19 on why a missing heading is
    # preferred to a guessed one.
    page_number: int | None = None
    section_heading: str | None = None
    is_table: bool = False

    # Cosine similarity in [0, 1]-ish: 1.0 is identical direction. Derived from pgvector's `<=>`
    # distance operator as `1 - distance`, so it reads the way callers expect a score to read.
    score: float


class SearchResponse(BaseModel):
    query: str
    count: int
    results: list[RetrievedChunk] = Field(default_factory=list)
