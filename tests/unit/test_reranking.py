"""Reranking — the reordering, and what happens when the model is not there.

No cross-encoder runs here. A fake stands in, because what is under test is the machinery around
it: that a wider pool is fetched, that the order is actually applied, that ties are stable, that
the pre-rerank position survives for the caller to see, and — most importantly — that a missing
or broken model costs the *best* ordering rather than the answer.

The wider pool is the part that is easy to get wrong and impossible to notice. Reranking cannot
recover a passage the SQL never selected, and measured on the real corpus the passage that best
answered a question routinely sat at fusion rank 9, 16 or 23 — outside any top 8. A reranker
wired to reorder `limit` rows instead of `RERANK_CANDIDATES` rows would look like it was working
and would find none of them.
"""

import uuid

import pytest

from src.core import config
from src.modules.retrieval.rerank import Reranker, RerankUnavailable


class FakeReranker(Reranker):
    """Scores by a caller-supplied table, or by position, and counts its calls."""

    def __init__(self, table: dict[str, float] | None = None, fail: bool = False,
                 wrong_count: bool = False):
        self.table = table or {}
        self.fail = fail
        self.wrong_count = wrong_count
        self.calls: list[tuple[str, list[str]]] = []

    @property
    def model_name(self) -> str:
        return "fake/cross-encoder"

    def scores(self, query: str, passages: list[str]) -> list[float]:
        self.calls.append((query, passages))
        if self.fail:
            raise RerankUnavailable("model is down")
        if self.wrong_count:
            return [1.0] * (len(passages) - 1)
        return [self.table.get(p, 0.0) for p in passages]


def rows(*texts):
    """Rows shaped like what the SQL returns — only `text` matters to the reranker."""
    return [{"text": t, "chunk_id": uuid.uuid4()} for t in texts]


def rerank(service_cls, reranker, query, data, limit):
    """Calls the private reordering step directly: it is the unit, and driving it through
    `search()` would need a database for no extra coverage."""
    return service_cls._rerank(reranker, query, data, limit)


@pytest.fixture
def svc():
    from src.modules.retrieval.service import RetrievalService
    return RetrievalService


# ==============================================================================
# The reordering
# ==============================================================================

def test_passages_are_returned_in_cross_encoder_order(svc):
    data = rows("weak", "strong", "middling")
    r = FakeReranker({"weak": 0.1, "strong": 9.0, "middling": 1.0})

    kept, scores, _fusion = rerank(svc, r, "q", data, 3)

    assert [row["text"] for row in kept] == ["strong", "middling", "weak"]
    assert scores == [9.0, 1.0, 0.1]


def test_the_pre_rerank_position_is_reported(svc):
    """The movement is the interesting part. On the real corpus the best passage was frequently
    9th, 16th or 23rd by fusion, and "was #16" is how someone sees the reranker earning its
    latency instead of taking it on trust."""
    data = rows("a", "b", "c", "d")
    r = FakeReranker({"a": 0.0, "b": 0.0, "c": 5.0, "d": 0.0})

    kept, _scores, fusion = rerank(svc, r, "q", data, 2)

    assert kept[0]["text"] == "c"
    assert fusion[0] == 3, "c was third before reranking"


def test_only_the_requested_number_is_kept(svc):
    data = rows(*[f"p{i}" for i in range(20)])
    r = FakeReranker({f"p{i}": float(i) for i in range(20)})

    kept, scores, fusion = rerank(svc, r, "q", data, 5)

    assert len(kept) == len(scores) == len(fusion) == 5
    assert [row["text"] for row in kept] == ["p19", "p18", "p17", "p16", "p15"]


def test_ties_keep_the_fused_order(svc):
    """A repeated query must return a repeated order — the same guarantee the SQL tiebreak
    gives. An unstable result list reads as a bug to whoever is using it."""
    data = rows("first", "second", "third")
    r = FakeReranker({"first": 1.0, "second": 1.0, "third": 1.0})

    kept, _s, fusion = rerank(svc, r, "q", data, 3)

    assert [row["text"] for row in kept] == ["first", "second", "third"]
    assert fusion == [1, 2, 3]


def test_passages_are_truncated_for_scoring_only(svc, monkeypatch):
    """A cross-encoder's cost scales with tokens, and a long table contributes its relevance in
    the first few hundred characters. The full text must still be what is returned and cited."""
    monkeypatch.setattr(config.settings, "RERANK_MAX_CHARS", 10)
    long_text = "A" * 500
    data = rows(long_text)
    r = FakeReranker()

    kept, _s, _f = rerank(svc, r, "q", data, 1)

    _query, sent = r.calls[0]
    assert sent == ["A" * 10], "the model should see the truncated passage"
    assert kept[0]["text"] == long_text, "the caller must still get the whole passage"


# ==============================================================================
# Degrading — the reranker is an improvement, not a dependency
# ==============================================================================

def test_without_a_reranker_the_fused_order_survives(svc):
    data = rows("a", "b", "c")

    kept, scores, fusion = rerank(svc, None, "q", data, 2)

    assert [row["text"] for row in kept] == ["a", "b"]
    assert scores == [None, None]
    assert fusion == [None, None], "no score means it did not run, not that it changed nothing"


def test_a_broken_model_does_not_fail_the_search(svc):
    """Search must still answer. Losing the reranker should cost the best ordering, not the
    results."""
    data = rows("a", "b", "c")
    r = FakeReranker(fail=True)

    kept, scores, _f = rerank(svc, r, "q", data, 2)

    assert [row["text"] for row in kept] == ["a", "b"]
    assert scores == [None, None]


def test_a_mismatched_score_count_is_rejected(svc):
    """Zipping N rows against N-1 scores would silently misattribute every score to the wrong
    passage — an ordering that looks plausible and is wrong throughout."""
    data = rows("a", "b", "c")
    r = FakeReranker(wrong_count=True)

    kept, scores, _f = rerank(svc, r, "q", data, 3)

    assert [row["text"] for row in kept] == ["a", "b", "c"]
    assert scores == [None, None, None]


def test_no_rows_is_not_an_error(svc):
    kept, scores, fusion = rerank(svc, FakeReranker(), "q", [], 5)

    assert kept == [] and scores == [] and fusion == []


# ==============================================================================
# The switch
# ==============================================================================

@pytest.mark.rerank
def test_the_switch_turns_it_off(monkeypatch):
    """`RERANK_ENABLED=false` must mean off, including the model never being loaded — the point
    of the switch is a deployment that does not pay for it at all."""
    from src.modules.retrieval import rerank as rr

    monkeypatch.setattr(config.settings, "RERANK_ENABLED", False)
    rr.reset_reranker()

    assert rr.get_reranker() is None


@pytest.mark.rerank
def test_a_model_that_cannot_load_is_reported_once_then_skipped(monkeypatch):
    """Retrying a failed model load on every query would turn one bad config into a per-request
    stall."""
    from src.modules.retrieval import rerank as rr

    monkeypatch.setattr(config.settings, "RERANK_ENABLED", True)
    rr.reset_reranker()

    attempts = {"n": 0}

    def boom(self):
        attempts["n"] += 1
        raise RerankUnavailable("no such model")

    monkeypatch.setattr(rr.CrossEncoderReranker, "_load", boom)

    assert rr.get_reranker() is None
    assert rr.get_reranker() is None
    assert attempts["n"] == 1, "the failure should be cached, not retried per call"

    rr.reset_reranker()


# ==============================================================================
# Scoping a question to particular documents
# ==============================================================================

def test_the_scope_is_a_predicate_not_a_post_filter():
    """It has to sit in the WHERE clause with the permission predicate, before ORDER BY/LIMIT.

    Filtering after ranking would let passages from other documents consume top-k slots, so a
    question scoped to one document would come back with fewer results than asked for — and the
    reader could not tell whether the document was thin or the filter was narrow. That is the
    same bug the permission clause is in SQL to avoid.
    """
    import inspect

    from src.modules.retrieval.service import RetrievalService

    source = inspect.getsource(RetrievalService._visible)
    assert "document_ids" in source
    assert "DocumentORM.document_id.in_" in source

    for arm in (RetrievalService._semantic_cte, RetrievalService._lexical_cte):
        arm_source = inspect.getsource(arm)
        assert "document_ids" in arm_source, f"{arm.__name__} ignores the scope"


def test_the_scope_never_replaces_the_permission_check():
    """Naming a document id you may not see must still return nothing. The scope narrows what
    a permitted search covers; it is not a way to reach past the permission clause."""
    import inspect

    from src.modules.retrieval.service import RetrievalService

    source = inspect.getsource(RetrievalService._visible)
    permission_at = source.index("visible_documents_clause")
    scope_at = source.index("DocumentORM.document_id.in_")
    assert permission_at < scope_at, "the permission clause must be unconditional"
    assert "if document_ids" in source, "the scope is the only conditional part"
