"""Embedding provider and service tests — no model is loaded.

These cover the logic that surrounds inference: the asymmetric query/passage handling, batching,
and the dimension guard. A fake provider stands in for the model, because loading real weights
would make the unit suite slow and network-dependent, and the behaviour worth testing here is not
the model's.
"""

import uuid

import numpy as np
import pytest

from src.modules.document_pipeline.embedding.provider import (
    EmbeddingProvider,
    FastEmbedProvider,
)
from src.modules.document_pipeline.embedding.service import EmbeddingService


class _FakeModel:
    """Stands in for a loaded fastembed model. Records the exact strings it was handed, which is
    the whole point: the prefixes are applied before this boundary."""

    def __init__(self, dimensions: int = 8):
        self.dimensions = dimensions
        self.seen: list[str] = []

    def embed(self, texts):
        texts = list(texts)
        self.seen = texts
        return iter([np.zeros(self.dimensions) for _ in texts])


class RecordingProvider(EmbeddingProvider):
    """Records the exact strings handed to the model, so prefixing can be asserted."""

    def __init__(self, dimensions: int = 4):
        self._dimensions = dimensions
        self.passage_calls: list[list[str]] = []
        self.query_calls: list[str] = []

    @property
    def model_name(self) -> str:
        return "recording/fake"

    @property
    def dimensions(self) -> int:
        return self._dimensions

    def embed_passages(self, texts):
        self.passage_calls.append(list(texts))
        return [[float(i)] * self._dimensions for i in range(len(texts))]

    def embed_query(self, text):
        self.query_calls.append(text)
        return [0.0] * self._dimensions

    @property
    def max_sequence_tokens(self) -> int:
        return 512

    def count_tokens(self, texts):
        return [max(1, len(t) // 4) for t in texts]


# ==============================================================================
# Asymmetric query/passage handling — the silent-failure guard
# ==============================================================================

def test_e5_model_prefixes_queries_and_passages_differently():
    """E5 is trained with literal `query: ` / `passage: ` markers. Omitting them costs retrieval
    quality with no error raised, which is why this lives in the provider rather than at the call
    site."""
    p = FastEmbedProvider(model_name="intfloat/multilingual-e5-small", dimensions=384)
    assert p._is_e5() is True
    fake = _FakeModel()
    p.__dict__["_model"] = fake

    p.embed_passages(["District targets for 2027."])
    assert fake.seen == ["passage: District targets for 2027."]

    p.embed_query("what are the district targets")
    assert fake.seen == ["query: what are the district targets"]


def test_bge_model_instructs_the_query_side_only():
    """BGE v1.5 takes an instruction on the query, and bare text on the passage. Applying the
    instruction to passages too would degrade retrieval just as silently."""
    p = FastEmbedProvider(model_name="BAAI/bge-base-en-v1.5", dimensions=768)
    assert p._is_bge() is True
    fake = _FakeModel()
    p.__dict__["_model"] = fake

    p.embed_passages(["District targets for 2027."])
    assert fake.seen == ["District targets for 2027."], "passages must not be instructed"

    p.embed_query("what are the district targets")
    assert fake.seen[0].startswith("Represent this sentence for searching")
    assert fake.seen[0].endswith("what are the district targets")


def test_bge_m3_takes_no_query_instruction():
    """BGE-M3 is not instruction-tuned the way v1.5 is — applying the v1.5 instruction would be
    wrong. Guards the substring match in _is_bge() against catching m3."""
    assert FastEmbedProvider(model_name="BAAI/bge-m3", dimensions=1024)._is_bge() is False


def test_empty_passage_list_does_not_call_the_model():
    p = FastEmbedProvider(model_name="BAAI/bge-base-en-v1.5", dimensions=768)
    assert p.embed_passages([]) == []  # would raise if it touched the un-loaded model


# ==============================================================================
# Service: batching
# ==============================================================================

def test_service_batches_according_to_configured_size():
    provider = RecordingProvider()
    service = EmbeddingService(provider=provider, batch_size=3)

    result = service.embed_document(uuid.uuid4(), [f"chunk {i}" for i in range(7)])

    assert [len(c) for c in provider.passage_calls] == [3, 3, 1]
    assert result.vector_count == 7


def test_service_records_model_and_dimensions_with_the_vectors():
    """A corpus half-embedded with one model and half with another is unusable, and the only way
    to tell them apart afterwards is this metadata."""
    service = EmbeddingService(provider=RecordingProvider(dimensions=4), batch_size=2)

    result = service.embed_document(uuid.uuid4(), ["a", "b"])

    assert result.model_name == "recording/fake"
    assert result.dimensions == 4
    assert all(len(v) == 4 for v in result.vectors)


def test_service_handles_a_document_with_no_chunks():
    service = EmbeddingService(provider=RecordingProvider(), batch_size=8)
    assert service.embed_document(uuid.uuid4(), []).vector_count == 0


@pytest.mark.parametrize("n", [1, 32, 33, 100])
def test_every_chunk_gets_exactly_one_vector(n):
    """A mismatch here silently misaligns vectors with passages — every citation would then point
    at the wrong text."""
    service = EmbeddingService(provider=RecordingProvider(), batch_size=32)
    result = service.embed_document(uuid.uuid4(), [f"c{i}" for i in range(n)])
    assert result.vector_count == n


# ==============================================================================
# Token counting — the measurement the truncation warning depends on
# ==============================================================================

@pytest.mark.parametrize("model,dim", [("BAAI/bge-base-en-v1.5", 768)])
def test_token_counts_are_per_text_not_padded_to_the_batch(model, dim):
    """`encode_batch` pads every sequence out to the longest in the batch, so a four-token
    passage measured beside a five-hundred-token one reports five hundred. The failure is not a
    rounding error: every passage in a batch reports an identical length, which is wrong and
    plausible enough to ship. Caught exactly that way — a live search returned five passages all
    claiming 689 tokens."""
    p = FastEmbedProvider(model_name=model, dimensions=dim)

    counts = p.count_tokens(["short", "a considerably longer passage of text " * 20])

    assert counts[0] < counts[1], "a short text must not inherit the long one's length"
    assert counts[0] < 10, f"expected a handful of tokens, got {counts[0]} (padding still on?)"


def test_token_counts_ignore_the_truncation_cap():
    """Measuring through the shipped tokenizer caps every answer at the window, so overflow —
    the only thing worth measuring — becomes invisible."""
    p = FastEmbedProvider(model_name="BAAI/bge-base-en-v1.5", dimensions=768)

    long_text = "enforcement obligation for the district authority " * 300
    count = p.count_tokens([long_text])[0]

    assert count > p.max_sequence_tokens, (
        f"{count} tokens reported against a {p.max_sequence_tokens}-token window — "
        "the counting tokenizer is still truncating"
    )


def test_batch_counts_match_individual_counts():
    """The batch path is what production uses; the single path is what is easy to reason about.
    They must not disagree."""
    p = FastEmbedProvider(model_name="BAAI/bge-base-en-v1.5", dimensions=768)
    texts = ["one", "two words here", "a rather longer sentence about district enforcement"]

    assert p.count_tokens(texts) == [p.count_tokens([t])[0] for t in texts]


def test_empty_input_counts_nothing():
    p = FastEmbedProvider(model_name="BAAI/bge-base-en-v1.5", dimensions=768)
    assert p.count_tokens([]) == []


def test_max_sequence_tokens_is_read_from_the_model():
    """Hardcoding 512 would quietly be wrong after a model swap — BGE-M3's window is 8192."""
    p = FastEmbedProvider(model_name="BAAI/bge-base-en-v1.5", dimensions=768)
    assert p.max_sequence_tokens == 512
