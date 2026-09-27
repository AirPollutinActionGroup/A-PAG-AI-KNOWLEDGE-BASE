"""Embedding providers.

`embed_passages()` and `embed_query()` are separate methods on purpose, and the split is
load-bearing rather than stylistic.

Several embedding families are **asymmetric**: they are trained to see different text on the
query side than on the stored-document side, and they quietly under-perform when fed the same
shape for both. E5 models require literal `query: ` / `passage: ` prefixes. BGE models take an
instruction on the query side only. The failure mode is the dangerous kind — no exception, no
warning, just several points of retrieval quality gone, which surfaces much later as "search
isn't very good" with no obvious cause.

Keeping that knowledge inside the provider means a retrieval endpoint cannot forget it, and
changing model family edits one class instead of every call site.
"""

import logging
from abc import ABC, abstractmethod
from functools import cached_property

from src.core.config import settings

logger = logging.getLogger(__name__)

# Query-side treatment per model family. Passage side is bare for BGE and prefixed for E5.
#
# BGE v1.5's instruction is the one published by its authors; changing the wording changes
# retrieval behaviour, so it is quoted verbatim rather than paraphrased.
_BGE_QUERY_INSTRUCTION = "Represent this sentence for searching relevant passages: "
_E5_QUERY_PREFIX = "query: "
_E5_PASSAGE_PREFIX = "passage: "


class EmbeddingProvider(ABC):
    """Turns text into vectors. Knows nothing about storage, jobs or documents."""

    @property
    @abstractmethod
    def model_name(self) -> str:
        """Identifier recorded alongside stored vectors, so a corpus embedded with one model is
        distinguishable from one embedded with another."""

    @property
    @abstractmethod
    def dimensions(self) -> int:
        """Must match the migrated `vector(N)` column, or every insert fails."""

    @abstractmethod
    def embed_passages(self, texts: list[str]) -> list[list[float]]:
        """Embeds stored document text."""

    @abstractmethod
    def embed_query(self, text: str) -> list[float]:
        """Embeds a search query. Not interchangeable with embed_passages — see module docstring."""


class FastEmbedProvider(EmbeddingProvider):
    """ONNX-backed embeddings via fastembed.

    fastembed rather than sentence-transformers because it runs quantized ONNX with no PyTorch
    dependency — the same reasoning that kept Docling out of the extraction stage. The model is
    loaded lazily so importing this module (in tests, or in a worker running a different stage)
    does not pull hundreds of megabytes into memory.
    """

    def __init__(self, model_name: str | None = None, dimensions: int | None = None):
        self._model_name = model_name or settings.EMBEDDING_MODEL
        self._declared_dimensions = dimensions or settings.EMBEDDING_DIMENSIONS

    @cached_property
    def _model(self):
        from fastembed import TextEmbedding

        logger.info("Loading embedding model: %s", self._model_name)
        model = TextEmbedding(self._model_name)

        # Fail loudly at load rather than at insert. A model whose output width disagrees with the
        # migrated column produces a constraint error per chunk, deep inside a worker, with a
        # message that does not name the real cause.
        actual = next(iter(model.embed(["dimension probe"]))).shape[0]
        if actual != self._declared_dimensions:
            raise ValueError(
                f"Model '{self._model_name}' emits {actual}-dim vectors but "
                f"EMBEDDING_DIMENSIONS is {self._declared_dimensions}. The database column is "
                f"vector({self._declared_dimensions}); changing model width needs a migration "
                f"and a re-embed, not a config edit."
            )
        return model

    @property
    def model_name(self) -> str:
        return self._model_name

    @property
    def dimensions(self) -> int:
        return self._declared_dimensions

    def _is_e5(self) -> bool:
        return "e5" in self._model_name.lower()

    def _is_bge(self) -> bool:
        name = self._model_name.lower()
        return "bge" in name and "m3" not in name  # BGE-M3 needs no query instruction

    def embed_passages(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        prepared = [f"{_E5_PASSAGE_PREFIX}{t}" for t in texts] if self._is_e5() else texts
        return [vector.tolist() for vector in self._model.embed(prepared)]

    def embed_query(self, text: str) -> list[float]:
        if self._is_e5():
            text = f"{_E5_QUERY_PREFIX}{text}"
        elif self._is_bge():
            text = f"{_BGE_QUERY_INSTRUCTION}{text}"
        return next(iter(self._model.embed([text]))).tolist()
