"""Embedding service — the seam the job handler talks to."""

import uuid

from src.core.config import settings
from src.modules.document_pipeline.embedding.models import EmbeddingResult
from src.modules.document_pipeline.embedding.provider import (
    EmbeddingProvider,
    FastEmbedProvider,
)


class EmbeddingService:
    """Embeds passage text in batches.

    Pure: no storage, no database, no job state. Batching happens here rather than in the handler
    because batch size is a property of inference throughput, not of how chunks are persisted.
    """

    def __init__(
        self,
        provider: EmbeddingProvider | None = None,
        batch_size: int | None = None,
    ):
        self.provider = provider or FastEmbedProvider()
        self.batch_size = batch_size or settings.EMBEDDING_BATCH_SIZE

    def embed_document(self, document_id: uuid.UUID, texts: list[str]) -> EmbeddingResult:
        vectors: list[list[float]] = []
        for start in range(0, len(texts), self.batch_size):
            vectors.extend(self.provider.embed_passages(texts[start : start + self.batch_size]))

        return EmbeddingResult(
            document_id=document_id,
            model_name=self.provider.model_name,
            dimensions=self.provider.dimensions,
            vectors=vectors,
        )
