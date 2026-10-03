"""Counting tokens without loading the embedding model.

The chunking stage sizes passages in tokens and never runs inference, so it should not carry the
ONNX session (~400MB measured). What it must not do in exchange is count differently: a budget that
approximates the tokenizer is the bug of `KNOWN_DEBTS.md` #28, where 2.9% of chunks silently
overflowed the model's window. These tests hold the lighter counter to the same two rules the full
provider applies, and hold the fallback to "more memory, never a different count".

Most of this runs offline against a small synthetic tokenizer. The one test that compares against
the real model skips when the model is not cached, which is the case in CI.
"""

import json

import pytest

from src.modules.document_pipeline.chunking.service import ChunkingService
from src.modules.document_pipeline.chunking.sizing import CharacterBudget, TokenBudget
from src.modules.document_pipeline.embedding.tokenizer_only import (
    TokenizerOnlyCounter,
    untruncated,
)

WINDOW = 8


@pytest.fixture
def model_dir(tmp_path):
    """A real `tokenizers` tokenizer in the layout fastembed's loader expects, small enough to
    reason about: whitespace-split words, an 8-token window, and a [PAD] token."""
    from tokenizers import Tokenizer, models, pre_tokenizers

    vocab = {"[PAD]": 0, "[UNK]": 1, **{w: i + 2 for i, w in enumerate(
        ["the", "a", "plant", "must", "comply", "by", "category", "fgd", "emission", "norms", "and", "in"])}}
    tok = Tokenizer(models.WordLevel(vocab, unk_token="[UNK]"))
    tok.pre_tokenizer = pre_tokenizers.Whitespace()
    tok.save(str(tmp_path / "tokenizer.json"))
    (tmp_path / "tokenizer_config.json").write_text(
        json.dumps({"model_max_length": WINDOW, "pad_token": "[PAD]"}))
    (tmp_path / "special_tokens_map.json").write_text(
        json.dumps({"pad_token": "[PAD]", "unk_token": "[UNK]"}))
    return tmp_path


def test_it_reports_the_models_window(model_dir):
    """Read from the tokenizer's own truncation policy, not hardcoded, so a model swap to a
    larger window is picked up without editing this."""
    assert TokenizerOnlyCounter(model_dir=model_dir).max_sequence_tokens == WINDOW


def test_a_text_longer_than_the_window_reports_its_true_length(model_dir):
    """Truncation off. Through the shipped tokenizer this would return 8 and an audit would
    report zero overflow while passages were being cut."""
    counter = TokenizerOnlyCounter(model_dir=model_dir)

    assert counter.count_tokens([" ".join(["plant"] * 40)]) == [40]


def test_a_batch_is_not_padded_to_its_longest_member(model_dir):
    """Padding off. Padded, every text in a batch reports the longest text's length, which is
    wrong and plausible enough to go unnoticed (`KNOWN_DEBTS.md` #29)."""
    counter = TokenizerOnlyCounter(model_dir=model_dir)

    assert counter.count_tokens(["the", "the plant must comply by category fgd"]) == [1, 7]


def test_an_empty_list_is_an_empty_answer(model_dir):
    assert TokenizerOnlyCounter(model_dir=model_dir).count_tokens([]) == []


def test_the_shared_helper_leaves_the_original_tokenizer_alone(model_dir):
    """`FastEmbedProvider` and this counter both call `untruncated`; it must not switch off the
    policies on the tokenizer it was handed, which the embedding path still relies on."""
    from fastembed.common.preprocessor_utils import load_tokenizer

    shipped, _ = load_tokenizer(model_dir)
    copy = untruncated(shipped)

    assert shipped.truncation is not None and shipped.padding is not None
    assert copy.truncation is None and copy.padding is None


def test_it_drives_a_token_budget_the_way_the_provider_does(model_dir):
    """The chunking stage's real use: `TokenBudget` takes the counter and caps at its window."""
    budget = TokenBudget(TokenizerOnlyCounter(model_dir=model_dir))

    assert budget.maximum <= WINDOW
    assert budget.measure("the plant must comply") == 4
    assert budget.fits("the plant")
    assert not budget.fits(" ".join(["plant"] * 20))


# ------------------------------------------------------------------ the fallback chain

class _Boom:
    def __init__(self, *a, **k):
        raise RuntimeError("cannot load")


def test_failing_to_find_the_tokenizer_falls_back_to_the_full_provider(monkeypatch):
    """More memory is acceptable; a different count is not. The caller must land on the full
    provider, which counts exactly, and not on characters, which do not."""
    import src.modules.document_pipeline.embedding.provider as provider_mod
    import src.modules.document_pipeline.embedding.tokenizer_only as light_mod

    class FakeProvider:
        max_sequence_tokens = 512

        def __init__(self, *a, **k):
            pass

        def count_tokens(self, texts):
            return [len(t.split()) for t in texts]

    monkeypatch.setattr(light_mod, "TokenizerOnlyCounter", _Boom)
    monkeypatch.setattr(provider_mod, "FastEmbedProvider", FakeProvider)

    service = ChunkingService.with_model_tokenizer()

    assert isinstance(service.chunker.budget, TokenBudget)


def test_failing_both_ways_still_starts_on_the_character_budget(monkeypatch):
    """The worker must still come up: a stopped pipeline is worse than a coarser budget."""
    import src.modules.document_pipeline.embedding.provider as provider_mod
    import src.modules.document_pipeline.embedding.tokenizer_only as light_mod

    monkeypatch.setattr(light_mod, "TokenizerOnlyCounter", _Boom)
    monkeypatch.setattr(provider_mod, "FastEmbedProvider", _Boom)

    service = ChunkingService.with_model_tokenizer()

    assert isinstance(service.chunker.budget, CharacterBudget)


def test_the_normal_path_does_not_build_the_full_provider(monkeypatch):
    """The point of the change. If this loaded the provider too, nothing would have been saved."""
    import src.modules.document_pipeline.embedding.provider as provider_mod
    import src.modules.document_pipeline.embedding.tokenizer_only as light_mod

    class FakeLight:
        max_sequence_tokens = 512

        def __init__(self, *a, **k):
            pass

        def count_tokens(self, texts):
            return [len(t.split()) for t in texts]

    monkeypatch.setattr(light_mod, "TokenizerOnlyCounter", FakeLight)
    monkeypatch.setattr(provider_mod, "FastEmbedProvider", _Boom)  # would raise if constructed

    service = ChunkingService.with_model_tokenizer()

    assert isinstance(service.chunker.budget, TokenBudget)


# ------------------------------------------------------------------ against the real model

def _real_model_available():
    try:
        TokenizerOnlyCounter()
        return True
    except Exception:
        return False


@pytest.mark.skipif(not _real_model_available(), reason="embedding model not cached locally")
def test_counts_match_the_full_provider_on_real_text():
    """The test that justifies the whole change. Measured once over every chunk in a 4,459-chunk
    corpus plus awkward inputs: zero mismatches. This keeps a sample of that on every run where
    the model happens to be present."""
    from src.modules.document_pipeline.embedding.provider import FastEmbedProvider

    full, light = FastEmbedProvider(), TokenizerOnlyCounter()
    samples = [
        "", "a", "S.O. 3305 (E) dated 07.12.2015",
        "Category A plants must comply by 31 December 2024, Category B by 31 December 2025.",
        "1,02,040 MW | 2,04,160 MW | Rs. 0.20 | 31st December 2024",
        "भारत सरकार",
        "café “quoted” ₹0.20", "word " * 3000,
    ]

    assert light.max_sequence_tokens == full.max_sequence_tokens
    assert light.count_tokens(samples) == full.count_tokens(samples)
