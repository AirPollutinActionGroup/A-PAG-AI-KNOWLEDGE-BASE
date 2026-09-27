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

    # The passage's true token length, and whether that exceeded the embedding model's window.
    # A truncated passage is stored whole but was embedded only up to the cap, so a search for
    # something mentioned solely in its tail will not find it. Surfacing it means the caller can
    # see that rather than concluding the corpus does not contain the answer.
    token_count: int = 0
    truncated: bool = False


class TokenUsage(BaseModel):
    """What this query cost the embedding model, and what a downstream LLM would be handed.

    There is no generation step yet, so `context_tokens` is a projection rather than a bill: it is
    the size of the context these passages would form if they were sent to a model. It is the
    number that decides whether a future answer fits in a prompt, which makes it worth showing
    now, while chunk sizing can still be changed cheaply.
    """

    query_tokens: int = 0
    context_tokens: int = 0
    max_sequence_tokens: int = 0
    truncated_results: int = 0
    model: str = ""


class SearchResponse(BaseModel):
    query: str
    count: int
    results: list[RetrievedChunk] = Field(default_factory=list)
    usage: TokenUsage = Field(default_factory=TokenUsage)
