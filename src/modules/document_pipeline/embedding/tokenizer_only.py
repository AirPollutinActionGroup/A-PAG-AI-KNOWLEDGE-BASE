"""Counting tokens with the embedding model's tokenizer and none of its weights.

The chunking stage sizes passages in tokens, and it needs exactly two things from the model: how
long a text is, and how long a text may be. Both come from `tokenizer.json`, which is ~700KB.
Until now it got them by constructing the whole `FastEmbedProvider`, which builds the ONNX session
for a stage that performs no inference: 500MB resident measured here, against 95MB for this.

**Exactness is the requirement, not an aspiration.** `KNOWN_DEBTS.md` #28 was a budget that
approximated the tokenizer and silently let 2.9% of chunks overflow the model's window. A lighter
counter that disagreed with the real one by even a token here and there would bring that back
without an error. So this does not reimplement anything: it loads the tokenizer through
fastembed's own `load_tokenizer`, the routine the full model uses, and applies the same two
policy changes `FastEmbedProvider.count_tokens` does. `tests/unit/test_tokenizer_only.py` holds
the two to identical counts on real text whenever the model is available locally.

Locating the files uses fastembed's documented `lazy_load=True`, which resolves the model
directory without building the session. That directory is read from a private attribute, which a
fastembed upgrade could rename, so failing to find it is expected to be possible and is handled
by the caller falling back to the full provider: more memory, never a wrong count.
"""

import logging
from pathlib import Path

from src.core.config import settings

logger = logging.getLogger(__name__)

# Only used if a tokenizer reports no truncation policy at all; every supported model does.
# Same value, same reason, as in provider.py.
_DEFAULT_MAX_TOKENS = 512


def untruncated(tokenizer):
    """A copy of `tokenizer` with the length cap and batch padding removed.

    Two policies have to be off, and either left on corrupts the count silently. Truncation caps
    every answer at the window, so an audit through it reports zero overflow while passages are
    being cut. Padding makes `encode_batch` report the longest text's length for every text in the
    batch (the bug of `KNOWN_DEBTS.md` #29). Rebuilt from the serialized state, so the vocabulary
    and merges are identical.
    """
    from tokenizers import Tokenizer

    copy = Tokenizer.from_str(tokenizer.to_str())
    copy.no_truncation()
    copy.no_padding()
    return copy


class TokenizerOnlyCounter:
    """A `TokenCounter` (see `chunking/sizing.py`) backed by the tokenizer alone."""

    def __init__(self, model_name: str | None = None, model_dir: Path | None = None):
        """`model_dir` is for tests and for callers who already know where the files are;
        otherwise the configured model's directory is located through fastembed."""
        from fastembed.common.preprocessor_utils import load_tokenizer

        if model_dir is None:
            from fastembed import TextEmbedding

            located = TextEmbedding(model_name or settings.EMBEDDING_MODEL, lazy_load=True)
            model_dir = Path(located.model._model_dir)

        shipped, _ = load_tokenizer(Path(model_dir))
        truncation = shipped.truncation
        self._max_sequence_tokens = (
            int(truncation["max_length"]) if truncation else _DEFAULT_MAX_TOKENS
        )
        self._tokenizer = untruncated(shipped)

    @property
    def max_sequence_tokens(self) -> int:
        return self._max_sequence_tokens

    def count_tokens(self, texts: list[str]) -> list[int]:
        if not texts:
            return []
        return [len(e.ids) for e in self._tokenizer.encode_batch(texts)]
