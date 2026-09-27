"""Reciprocal Rank Fusion arithmetic.

Small, but the properties matter: RRF exists so two incomparable score scales never have to be
compared, and every one of these tests is really asserting that ranks — not scores — decide.
"""

import pytest

from src.modules.retrieval.fusion import (
    CANDIDATE_MULTIPLIER,
    MAX_CANDIDATES,
    RRF_K,
    candidate_pool,
    rrf_score,
)

# ==============================================================================
# The score
# ==============================================================================

def test_a_missing_rank_contributes_nothing():
    """An arm that did not return a passage stays silent rather than voting against it. Treating
    absence as a penalty would make a passage that one arm ranked first but the other never saw
    lose to one both arms ranked poorly — exactly backwards."""
    assert rrf_score(None) == 0.0


def test_better_ranks_score_higher():
    assert rrf_score(1) > rrf_score(2) > rrf_score(10) > rrf_score(100)


def test_the_constant_flattens_the_top_of_the_ranking():
    """k is large relative to the ranks that matter, so first place beats second by a little
    rather than a lot. That is what stops one confident arm from dominating the fusion."""
    gap_at_top = rrf_score(1) - rrf_score(2)
    assert gap_at_top < rrf_score(1) * 0.05, "1st should not massively outweigh 2nd"


def test_agreement_between_arms_beats_a_single_first_place():
    """The property the whole design rests on: two arms independently ranking something highly is
    stronger evidence than one arm ranking it top. A passage 2nd and 3rd across both arms should
    outrank a passage 1st in one arm and absent from the other."""
    both = rrf_score(2) + rrf_score(3)
    one_only = rrf_score(1) + rrf_score(None)
    assert both > one_only


def test_a_single_arm_hit_still_scores():
    """Equally, a passage only one arm found must remain a candidate — that is the recall the
    second index was added for."""
    assert rrf_score(1) + rrf_score(None) > 0


@pytest.mark.parametrize("rank", [1, 5, 50, 500])
def test_score_matches_the_definition(rank):
    assert rrf_score(rank) == pytest.approx(1.0 / (RRF_K + rank))


# ==============================================================================
# The candidate pool
# ==============================================================================

def test_each_arm_fetches_more_than_the_caller_asked_for():
    """Fusion needs material. If each arm returned exactly `limit` rows, a passage ranked 8th by
    one arm and 40th by the other could never surface, and hybrid would collapse toward whichever
    arm happened to rank it inside the window."""
    assert candidate_pool(10) == 10 * CANDIDATE_MULTIPLIER


def test_the_pool_is_capped():
    """An uncapped pool makes a large `limit` scan a wide slice of the index on every query, for
    steadily less benefit — RRF weights tail ranks lightly by construction."""
    assert candidate_pool(500) == MAX_CANDIDATES


def test_the_pool_is_never_smaller_than_the_request():
    for limit in (1, 10, 50, 200, 1000):
        assert candidate_pool(limit) >= min(limit, MAX_CANDIDATES)
