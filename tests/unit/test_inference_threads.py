"""The thread count reaches both models.

On a 2-vCPU Azure VM, onnxruntime sizes its pool from physical cores and each vCPU there is a
hyperthread, so the default is one thread. During a bulk ingest the embedding worker ran at ~100%
CPU while the VM was ~45% idle, and nothing in any log said so. `INFERENCE_THREADS` exists to say
it, and a setting that does not reach the library fixes nothing, which is what these pin.
"""

from typing import ClassVar

import numpy as np
import pytest

from src.core.config import settings


class FakeEmbedding:
    seen: ClassVar[dict] = {}

    def __init__(self, model_name, **kwargs):
        FakeEmbedding.seen = {"model_name": model_name, **kwargs}

    def embed(self, texts):
        return iter([np.zeros(settings.EMBEDDING_DIMENSIONS, dtype="float32") for _ in texts])


class FakeCrossEncoder:
    seen: ClassVar[dict] = {}

    def __init__(self, model_name, **kwargs):
        FakeCrossEncoder.seen = {"model_name": model_name, **kwargs}


@pytest.fixture
def fakes(monkeypatch):
    import fastembed
    import fastembed.rerank.cross_encoder as ce

    monkeypatch.setattr(fastembed, "TextEmbedding", FakeEmbedding)
    monkeypatch.setattr(ce, "TextCrossEncoder", FakeCrossEncoder)
    FakeEmbedding.seen, FakeCrossEncoder.seen = {}, {}


def test_the_default_leaves_the_choice_to_the_library(fakes, monkeypatch):
    """0 means "do not override", passed as None. Passing 0 through would ask for zero threads."""
    from src.modules.document_pipeline.embedding.provider import FastEmbedProvider

    monkeypatch.setattr(settings, "INFERENCE_THREADS", 0)
    _ = FastEmbedProvider()._model

    assert FakeEmbedding.seen["threads"] is None


def test_a_set_value_reaches_the_embedding_model(fakes, monkeypatch):
    from src.modules.document_pipeline.embedding.provider import FastEmbedProvider

    monkeypatch.setattr(settings, "INFERENCE_THREADS", 2)
    _ = FastEmbedProvider()._model

    assert FakeEmbedding.seen["threads"] == 2


def test_a_set_value_reaches_the_reranker(fakes, monkeypatch):
    """The API embeds every query and reranks every result on the same small VM, so it has the
    same single-thread default and the same cost."""
    from src.modules.retrieval.rerank import CrossEncoderReranker as Reranker

    monkeypatch.setattr(settings, "INFERENCE_THREADS", 2)
    Reranker()._load()

    assert FakeCrossEncoder.seen["threads"] == 2


def test_the_reranker_default_is_also_left_to_the_library(fakes, monkeypatch):
    from src.modules.retrieval.rerank import CrossEncoderReranker as Reranker

    monkeypatch.setattr(settings, "INFERENCE_THREADS", 0)
    Reranker()._load()

    assert FakeCrossEncoder.seen["threads"] is None
