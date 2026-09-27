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

    @computed_field
    @property
    def vector_count(self) -> int:
        return len(self.vectors)
