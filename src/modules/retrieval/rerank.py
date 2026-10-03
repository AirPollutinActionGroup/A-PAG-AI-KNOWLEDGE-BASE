"""Reranking: read the candidates properly, then keep the best few.

Hybrid retrieval is a *recall* device. Both arms score a passage without ever looking at the
query and the passage together — the semantic arm compares two vectors that were computed
independently, and BM25 counts term overlap. Neither can tell that a passage mentioning
"timelines" and "FGD" is about a *different* plant's timelines than the one asked about.

A cross-encoder can, because it reads the query and the passage in one pass and scores the pair.
That is also why it cannot replace retrieval: scoring every chunk in the corpus against every
query is quadratic and absurd. So the shape is the one the architecture specifies — retrieve
widely, then read carefully:

    hybrid search (~50 candidates)  ->  cross-encoder  ->  best 8

**What this costs, and why the model choice is a latency decision.** Reranking is paid on every
query, in the request path, on CPU. A model that orders better but adds two seconds makes the
product worse. The model is configuration (`RERANK_MODEL`) for the same reason the embedding
model is, but unlike the embedding model there is no dimension to match and no re-embed to do —
swapping it changes ordering from the next query onward and nothing else.

**What it does not change.** The grounding gate still reads raw cosine similarity, not the
rerank score. A cross-encoder emits an unbounded logit whose scale is the model's own; it says
"this passage beats that one", not "the corpus contains an answer". Those are different
questions, and only the second one decides whether to say "I don't know" — see `assess()`.
"""

import logging
import threading
from abc import ABC, abstractmethod

from src.core.config import settings

logger = logging.getLogger(__name__)


class Reranker(ABC):
    """Scores (query, passage) pairs. Higher is more relevant."""

    @property
    @abstractmethod
    def model_name(self) -> str:
        ...

    @abstractmethod
    def scores(self, query: str, passages: list[str]) -> list[float]:
        """One score per passage, in the order given."""


class RerankUnavailable(RuntimeError):
    """The model could not be loaded or run. Distinct from "ran and changed nothing"."""


class CrossEncoderReranker(Reranker):
    """fastembed's ONNX cross-encoder.

    fastembed is already in this image for embeddings, so this adds a model rather than a second
    inference runtime, and stays consistent with every other model decision here: ONNX, CPU, no
    PyTorch.
    """

    def __init__(self, model_name: str | None = None):
        self._name = model_name or settings.RERANK_MODEL
        self._model = None
        self._lock = threading.Lock()

    @property
    def model_name(self) -> str:
        return self._name

    def _load(self):
        if self._model is not None:
            return self._model
        with self._lock:
            if self._model is not None:
                return self._model
            try:
                from fastembed.rerank.cross_encoder import TextCrossEncoder
            except ImportError as e:  # pragma: no cover - depends on the deployed image
                raise RerankUnavailable("fastembed reranking is not available.") from e
            try:
                self._model = TextCrossEncoder(model_name=self._name)
            except Exception as e:
                raise RerankUnavailable(f"Could not load reranker {self._name!r}: {e}") from e
            logger.info("Reranker ready: %s", self._name)
            return self._model

    def scores(self, query: str, passages: list[str]) -> list[float]:
        if not passages:
            return []
        model = self._load()
        try:
            return [float(s) for s in model.rerank(query, passages)]
        except Exception as e:
            raise RerankUnavailable(f"Reranking failed: {e}") from e


_reranker: Reranker | None = None
_lock = threading.Lock()
_failed = False


def get_reranker() -> Reranker | None:
    """The process's reranker, or None when it is switched off or cannot be loaded.

    Returns None rather than raising for the same reason the OCR engine does: reranking improves
    an ordering that is already useful without it. A model that fails to load should cost the
    deployment its *best* results, not all of them — search must still answer.
    """
    global _reranker, _failed

    if not settings.RERANK_ENABLED or _failed:
        return None
    if _reranker is not None:
        return _reranker

    with _lock:
        if _reranker is not None:
            return _reranker
        candidate = CrossEncoderReranker()
        try:
            candidate._load()
        except RerankUnavailable as e:
            # ERROR, once: silently serving un-reranked results would look like the reranker
            # working badly rather than not running at all.
            logger.error("Reranker unavailable, search will return fused order: %s", e)
            _failed = True
            return None
        _reranker = candidate
        return _reranker


def reset_reranker() -> None:
    """Drops the cached model. For tests, which substitute their own."""
    global _reranker, _failed
    with _lock:
        _reranker = None
        _failed = False
