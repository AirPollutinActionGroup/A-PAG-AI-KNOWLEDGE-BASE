"""Embedding service — the seam the job handler talks to."""

import logging
import uuid

from src.core.config import settings
from src.modules.document_pipeline.embedding.models import EmbeddingResult
from src.modules.document_pipeline.embedding.provider import (
    EmbeddingProvider,
    FastEmbedProvider,
)

logger = logging.getLogger(__name__)


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
        token_counts: list[int] = []
        for start in range(0, len(texts), self.batch_size):
            batch = texts[start : start + self.batch_size]
            vectors.extend(self.provider.embed_passages(batch))
            # Measured on the same batch that was just embedded, so the count always describes
            # the text the model actually saw.
            token_counts.extend(self.provider.count_tokens(batch))

        result = EmbeddingResult(
            document_id=document_id,
            model_name=self.provider.model_name,
            dimensions=self.provider.dimensions,
            vectors=vectors,
            max_sequence_tokens=self.provider.max_sequence_tokens,
            token_counts=token_counts,
        )

        if result.truncated_count:
            # WARNING, not an error: the vectors are still usable and the document is still
            # findable. What is lost is the tail of a few passages, and a search for something
            # only mentioned there will quietly fail to find it.
            logger.warning(
                "Embedding truncated %d/%d passage(s) at the %d-token window "
                "(%d tokens discarded): doc_id=%s",
                result.truncated_count, len(texts), result.max_sequence_tokens,
                result.truncated_tokens, document_id,
            )

        return result
