"""Suite-wide defaults.

Reranking is **off by default in tests**, and deliberately so. `RERANK_ENABLED` defaults to true
because that is right in production, but a test that builds a `RetrievalService` and calls
`search()` would otherwise resolve the process reranker — downloading an 80MB cross-encoder on a
cold machine and loading it into every test session. That turns a fast suite into a slow one and
makes it depend on the network, which is exactly what CI should not do.

Tests that are *about* reranking inject a fake (see `test_reranking.py`) or flip the setting back
on for the duration, so nothing here hides the behaviour — it only stops every unrelated test
paying for a model it does not exercise.
"""

import pytest

from src.core.config import settings


@pytest.fixture(autouse=True)
def _no_rerank_by_default(request, monkeypatch):
    # Tests marked `rerank` opt back in and manage the setting themselves.
    if request.node.get_closest_marker("rerank"):
        return
    monkeypatch.setattr(settings, "RERANK_ENABLED", False)

    # Also drop any engine a previous test cached, so a test that did opt in cannot leave a
    # loaded model behind for the next one to pick up by surprise.
    from src.modules.retrieval import rerank as rr
    rr.reset_reranker()


def pytest_configure(config):
    config.addinivalue_line(
        "markers", "rerank: test exercises the reranker and manages RERANK_ENABLED itself"
    )
