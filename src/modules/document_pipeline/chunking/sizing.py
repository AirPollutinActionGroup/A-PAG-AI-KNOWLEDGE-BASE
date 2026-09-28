"""How big a passage is allowed to be, measured in the unit that actually constrains the model.

Chunking decides *where* to cut — at the document's own headings. This module decides *how big*
a piece may be, which is a separate question that was previously answered in the wrong unit.

**The bug this exists to fix.** The budget was `CHUNK_MAX_CHARS = 2000` characters, while the
embedding model's window is 512 **tokens**. Those agree only at a particular characters-per-token
ratio, and text does not hold still: this corpus averages 4.11 chars/token (so 512 tokens is
~2100 characters, and a 2000-character budget fits with 5% to spare), but passages containing
code, terminal output or ASCII tables run about 3.15, where 512 tokens is only ~1600 characters.
Those overflowed. fastembed then truncated them silently — the stored text was whole, the vector
covered only its head, and a search for anything in the tail could not match. 83 of 2,816 chunks
(2.9%) were in that state, and every one was *within* the character budget. The chunker obeyed
its rules perfectly; the rules could not see what they were constraining.

`CLAUDE.md` predicted this failure for Devanagari, which runs 2-3x more tokens per character than
English. It arrived first in English technical prose.

**Why chunking may depend on a tokenizer at all.** The stages are deliberately separate so that
changing embedding model is a re-embed rather than a re-chunk. `TokenCounter` is deliberately
narrower than the embedding provider: it exposes tokenization and a window size, nothing about
vectors. The dependency it introduces is real and was always there — `CHUNK_MAX_CHARS = 2000` was
itself chosen with a 512-token window in mind. Writing it as a character constant did not remove
the coupling; it hid it, which is how the constant went on being wrong without anyone noticing.
"""

from typing import Protocol, runtime_checkable

from src.core.config import settings


@runtime_checkable
class TokenCounter(Protocol):
    """Just enough of a tokenizer for sizing decisions.

    `EmbeddingProvider` satisfies this structurally, so the chunking stage can be handed the
    configured provider without importing the embedding service or knowing it produces vectors.
    """

    def count_tokens(self, texts: list[str]) -> list[int]: ...

    @property
    def max_sequence_tokens(self) -> int: ...


class SizeBudget:
    """Measures text and says what fits. Subclasses differ only in the unit."""

    target: int
    maximum: int
    unit: str

    def measure(self, text: str) -> int:
        raise NotImplementedError

    def measure_all(self, texts: list[str]) -> list[int]:
        """Batched by default so a token-based budget can amortise one call over many pieces."""
        return [self.measure(t) for t in texts]

    def fits(self, text: str) -> bool:
        return self.measure(text) <= self.maximum


class CharacterBudget(SizeBudget):
    """The original behaviour, kept as the fallback when no tokenizer is available.

    Still used by unit tests, which should not have to load a model to exercise splitting logic,
    and as the degraded mode if a worker cannot construct a counter. It is a proxy, and the
    truncation it allows is the reason this class is no longer the default.
    """

    unit = "chars"

    def __init__(self, target: int | None = None, maximum: int | None = None):
        self.target = target or settings.CHUNK_TARGET_CHARS
        self.maximum = maximum or settings.CHUNK_MAX_CHARS

    def measure(self, text: str) -> int:
        return len(text)


class TokenBudget(SizeBudget):
    """Sizes against the model's real window.

    `maximum` is the smaller of the configured ceiling and what the model can actually read, so
    neither a generous setting nor a model swap can produce a chunk the model would truncate.
    Swapping to a model with a larger window (BGE-M3's is 8192) therefore does not silently
    produce 8000-token chunks: a passage that large is a poor retrieval unit regardless of what
    the model can ingest, because precision falls as a chunk covers more distinct ideas.

    Counts include the tokenizer's special tokens ([CLS]/[SEP]), because `count_tokens` measures
    what the model is actually handed — the number that has to fit.
    """

    unit = "tokens"

    def __init__(
        self,
        counter: TokenCounter,
        target: int | None = None,
        maximum: int | None = None,
    ):
        self._counter = counter
        configured_max = maximum or settings.CHUNK_MAX_TOKENS
        self.maximum = min(configured_max, counter.max_sequence_tokens)
        self.target = min(target or settings.CHUNK_TARGET_TOKENS, self.maximum)

        # Measuring is the expensive part of splitting, and recursion re-measures the same
        # strings. The memo is per-budget, and a budget is per-document, so it cannot grow
        # without bound across a run.
        self._memo: dict[str, int] = {}

    def measure(self, text: str) -> int:
        cached = self._memo.get(text)
        if cached is None:
            cached = self._counter.count_tokens([text])[0]
            self._memo[text] = cached
        return cached

    def measure_all(self, texts: list[str]) -> list[int]:
        unseen = [t for t in dict.fromkeys(texts) if t not in self._memo]
        if unseen:
            for t, n in zip(unseen, self._counter.count_tokens(unseen), strict=True):
                self._memo[t] = n
        return [self._memo[t] for t in texts]
