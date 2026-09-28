"""Chunk sizing measured in tokens, not characters.

The bug this covers: the budget was `CHUNK_MAX_CHARS = 2000` while the model's window is 512
tokens. Those agree only at a particular characters-per-token ratio, and 2.9% of the real corpus
sat outside it — passages containing code, terminal output or ASCII tables, which run ~3.15
chars/token against a corpus mean of 4.11. Every one of them was *within* the character budget,
so the chunker was obeying rules that could not see what they constrained. fastembed then
truncated them with no error, leaving the stored text whole and the vector covering only its head.

No model is loaded. A fake counter stands in with a deliberately punishing ratio, because what is
under test is whether the chunker measures in the right unit — not how a particular tokenizer
behaves.
"""

import uuid

import pytest

from src.modules.document_pipeline.chunking.chunker import Chunker
from src.modules.document_pipeline.chunking.sizing import (
    CharacterBudget,
    TokenBudget,
)
from src.modules.document_pipeline.extraction.models import ExtractedTable, Heading
from src.modules.document_pipeline.formats import PDF_MIME
from src.modules.document_pipeline.normalization.models import (
    NormalizationResult,
    NormalizedUnit,
    QualityCheckResult,
)


class FakeCounter:
    """Tokenises by a fixed characters-per-token ratio, and records what it was asked.

    A ratio of 2.0 stands in for dense text (code, tables); the real corpus mean is 4.11.
    """

    def __init__(self, chars_per_token: float = 2.0, window: int = 100):
        self.chars_per_token = chars_per_token
        self._window = window
        self.calls: list[list[str]] = []

    @property
    def max_sequence_tokens(self) -> int:
        return self._window

    def count_tokens(self, texts: list[str]) -> list[int]:
        self.calls.append(list(texts))
        return [max(1, round(len(t) / self.chars_per_token)) for t in texts]


def build(units, headings=(), tables=()):
    return NormalizationResult(
        document_id=uuid.uuid4(),
        mime_type=PDF_MIME,
        language="en",
        units=[NormalizedUnit(index=i, label=lbl, text=txt) for i, lbl, txt in units],
        headings=list(headings),
        tables=list(tables),
        quality=QualityCheckResult(passed=True),
    )


def sentences(n, word="alpha"):
    return " ".join(f"This is sentence {i} about {word} and its obligations." for i in range(n))


# ==============================================================================
# The budget objects
# ==============================================================================

def test_token_budget_never_exceeds_the_model_window():
    """A generous setting must not be able to produce a chunk the model would truncate."""
    budget = TokenBudget(FakeCounter(window=100), target=9_000, maximum=10_000)

    assert budget.maximum == 100
    assert budget.target <= budget.maximum


def test_token_budget_respects_a_lower_configured_ceiling():
    """A model with a huge window (BGE-M3's is 8192) should not silently yield huge chunks —
    precision falls as one passage covers more distinct ideas, whatever the model can ingest."""
    budget = TokenBudget(FakeCounter(window=8192), target=400, maximum=500)

    assert budget.maximum == 500
    assert budget.target == 400


def test_token_budget_batches_and_caches_measurements():
    """Each measurement is a tokenizer call, and splitting re-measures the same strings."""
    counter = FakeCounter()
    budget = TokenBudget(counter, target=50, maximum=100)

    budget.measure_all(["alpha", "beta", "alpha"])
    budget.measure("alpha")

    assert len(counter.calls) == 1, "repeat measurements must come from the cache"
    assert sorted(counter.calls[0]) == ["alpha", "beta"], "duplicates should not be re-sent"


def test_character_budget_still_measures_characters():
    """Kept as the fallback for callers with no tokenizer."""
    budget = CharacterBudget(target=10, maximum=20)

    assert budget.measure("abcde") == 5
    assert budget.fits("a" * 20)
    assert not budget.fits("a" * 21)


# ==============================================================================
# The regression: dense text must not overflow
# ==============================================================================

def test_dense_text_is_split_to_fit_the_token_window():
    """The bug, directly. At 2 chars/token a 2000-character passage is 1000 tokens — double a
    500-token window — while sitting comfortably inside any character budget."""
    counter = FakeCounter(chars_per_token=2.0, window=100)
    chunker = Chunker(budget=TokenBudget(counter, target=80, maximum=100))

    chunks = chunker.chunk(build([(1, "Page 1", sentences(40))]))

    assert chunks, "expected the section to be split, not dropped"
    for c in chunks:
        assert counter.count_tokens([c.text])[0] <= 100, (
            f"chunk of {len(c.text)} chars exceeds the token window"
        )


def test_the_same_text_yields_more_chunks_under_a_denser_ratio():
    """Sizing must actually respond to density. Under a character budget these two cases are
    indistinguishable, which is exactly why the bug was invisible."""
    text = sentences(40)
    sparse = Chunker(budget=TokenBudget(FakeCounter(chars_per_token=4.0, window=100),
                                        target=80, maximum=100))
    dense = Chunker(budget=TokenBudget(FakeCounter(chars_per_token=2.0, window=100),
                                       target=80, maximum=100))

    n_sparse = len(sparse.chunk(build([(1, "Page 1", text)])))
    n_dense = len(dense.chunk(build([(1, "Page 1", text)])))

    assert n_dense > n_sparse


def test_a_character_budget_would_have_let_it_through():
    """Pins the old behaviour as broken, so this cannot silently regress. The same text under the
    shipped character budget produces a chunk far past a 100-token window."""
    chunker = Chunker(budget=CharacterBudget(target=1600, maximum=2000))

    chunks = chunker.chunk(build([(1, "Page 1", sentences(40))]))

    counter = FakeCounter(chars_per_token=2.0)
    assert any(counter.count_tokens([c.text])[0] > 100 for c in chunks), (
        "expected the character budget to overflow a token window — if this fails the fixture "
        "text is no longer dense enough to demonstrate the bug"
    )


# ==============================================================================
# Structure is still respected — sizing changed, cutting did not
# ==============================================================================

def test_headings_still_decide_the_cut_points():
    """Token sizing must not turn structure-aware chunking into fixed-size chunking."""
    chunker = Chunker(budget=TokenBudget(FakeCounter(chars_per_token=4.0, window=500),
                                         target=400, maximum=500))
    result = build(
        units=[(1, "Page 1", "1. Scope\nScope body text.\n\n2. Penalties\nPenalty body text.")],
        headings=[Heading(text="1. Scope", level=1, unit_index=1),
                  Heading(text="2. Penalties", level=1, unit_index=1)],
    )

    chunks = chunker.chunk(result)

    assert [c.section_heading for c in chunks] == ["1. Scope", "2. Penalties"]


def test_table_groups_still_repeat_the_header_under_a_token_budget():
    """The rule that makes a split table usable. Splitting a group by characters would produce
    exactly the fragment of numbers the repeated header exists to prevent."""
    rows = [["District", "Target", "Deadline"]]
    rows += [[f"District {i}", f"{i}%", "2027-01-31"] for i in range(200)]
    chunker = Chunker(budget=TokenBudget(FakeCounter(chars_per_token=2.0, window=120),
                                         target=100, maximum=120))

    table_chunks = [c for c in chunker.chunk(
        build([(1, "Page 1", "Annexure A.")], tables=[ExtractedTable(unit_index=1, rows=rows)])
    ) if c.is_table]

    assert len(table_chunks) > 1
    assert all(c.text.startswith("District | Target | Deadline") for c in table_chunks)


def test_table_groups_fit_the_token_window(db_session=None):
    counter = FakeCounter(chars_per_token=2.0, window=120)
    rows = [["District", "Target", "Deadline"]]
    rows += [[f"District {i}", f"{i}%", "2027-01-31"] for i in range(200)]
    chunker = Chunker(budget=TokenBudget(counter, target=100, maximum=120))

    chunks = chunker.chunk(
        build([(1, "Page 1", "Annexure A.")], tables=[ExtractedTable(unit_index=1, rows=rows)])
    )

    for c in chunks:
        assert counter.count_tokens([c.text])[0] <= 120


def test_a_single_oversized_row_is_kept_whole():
    """A row wider than the budget cannot be made to fit by grouping, and splitting it mid-row
    would produce numbers with no column names. A long chunk beats an uninterpretable one."""
    wide = "x" * 4000
    rows = [["District", "Notes"], ["Patna", wide]]
    chunker = Chunker(budget=TokenBudget(FakeCounter(chars_per_token=2.0, window=120),
                                         target=100, maximum=120))

    table_chunks = [c for c in chunker.chunk(
        build([(1, "Page 1", "Annexure.")], tables=[ExtractedTable(unit_index=1, rows=rows)])
    ) if c.is_table]

    assert any(wide in c.text for c in table_chunks), "the wide row must survive intact"


@pytest.mark.parametrize("ratio", [1.5, 2.0, 4.0, 6.0])
def test_every_chunk_fits_whatever_the_ratio(ratio):
    """The property that matters, across the range of densities real documents exhibit."""
    counter = FakeCounter(chars_per_token=ratio, window=100)
    chunker = Chunker(budget=TokenBudget(counter, target=80, maximum=100))

    chunks = chunker.chunk(build([(1, "Page 1", sentences(60))]))

    assert chunks
    for c in chunks:
        assert counter.count_tokens([c.text])[0] <= 100
