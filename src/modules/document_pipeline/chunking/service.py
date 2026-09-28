"""Chunking service — the seam the job handler talks to."""

import logging

from src.modules.document_pipeline.chunking.chunker import Chunker
from src.modules.document_pipeline.chunking.models import ChunkingResult
from src.modules.document_pipeline.chunking.sizing import (
    CharacterBudget,
    TokenBudget,
    TokenCounter,
)
from src.modules.document_pipeline.normalization.models import NormalizationResult

logger = logging.getLogger(__name__)


class ChunkingService:
    """Turns a normalized document into retrievable passages.

    Pure: no storage, no database, no awareness of job state. That is what lets the chunking
    rules be tested against a hand-built NormalizationResult with no stack around them.
    """

    def __init__(
        self,
        chunker: Chunker | None = None,
        token_counter: TokenCounter | None = None,
    ):
        if chunker is not None:
            self.chunker = chunker
        elif token_counter is not None:
            self.chunker = Chunker(budget=TokenBudget(token_counter))
        else:
            self.chunker = Chunker(budget=CharacterBudget())

    @classmethod
    def with_model_tokenizer(cls) -> "ChunkingService":
        """Builds a service that sizes against the configured embedding model's real window.

        Falls back to the character budget if the model cannot be loaded. That trade-off is
        deliberate: a chunking worker that refuses to start because a tokenizer is unavailable
        stops the pipeline outright, whereas the character proxy is what shipped until now and
        is wrong only for unusually dense passages. The fallback is logged at WARNING because it
        silently reintroduces the truncation this budget exists to prevent — see
        KNOWN_DEBTS.md #28.
        """
        try:
            from src.core.config import settings
            from src.modules.document_pipeline.embedding.provider import (
                FastEmbedProvider,
            )

            provider = FastEmbedProvider(
                model_name=settings.EMBEDDING_MODEL,
                dimensions=settings.EMBEDDING_DIMENSIONS,
            )
            budget = TokenBudget(provider)
            logger.info(
                "Chunking against the model's window: max=%d target=%d tokens (%s)",
                budget.maximum, budget.target, settings.EMBEDDING_MODEL,
            )
            return cls(chunker=Chunker(budget=budget))
        except Exception as e:
            logger.warning(
                "Could not load a tokenizer (%s). Falling back to the character budget, which "
                "can overflow the model's window on dense passages.", e,
            )
            return cls()

    def chunk(self, normalized: NormalizationResult) -> ChunkingResult:
        return ChunkingResult(
            document_id=normalized.document_id,
            chunks=self.chunker.chunk(normalized),
        )
