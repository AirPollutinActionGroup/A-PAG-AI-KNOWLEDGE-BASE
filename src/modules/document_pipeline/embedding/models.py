"""Data contracts for the embedding stage."""

import uuid

from pydantic import BaseModel, Field, computed_field


class EmbeddingResult(BaseModel):
    """Vectors for one document's passages, in chunk_index order.

    Carries the model name and dimension alongside the vectors so a corpus embedded with one
    model is distinguishable from one embedded with another — which matters the moment a model
    is swapped and half the corpus has been re-embedded and half has not.
    """

    document_id: uuid.UUID
    model_name: str
    dimensions: int
    vectors: list[list[float]] = Field(default_factory=list)

    # Chunks are sized in *characters* (the tokenizer belongs to the model, which arrives a stage
    # after chunking), but the model's window is in *tokens*. Where the two disagree fastembed
    # truncates and the tail of that passage is never embedded — no exception, just a vector that
    # represents part of a text the database stores whole. Recording it makes the gap queryable,
    # the same way SKIPPED_UNSUPPORTED_LANGUAGE and LOW_TEXT_DENSITY do. See KNOWN_DEBTS.md #28.
    max_sequence_tokens: int = 0
    token_counts: list[int] = Field(default_factory=list)

    @computed_field
    @property
    def vector_count(self) -> int:
        return len(self.vectors)

    @computed_field
    @property
    def truncated_count(self) -> int:
        """How many passages were longer than the model could read."""
        return sum(1 for n in self.token_counts if n > self.max_sequence_tokens)

    @computed_field
    @property
    def truncated_tokens(self) -> int:
        """Total tokens discarded across those passages — the size of the blind spot."""
        return sum(
            n - self.max_sequence_tokens for n in self.token_counts
            if n > self.max_sequence_tokens
        )
