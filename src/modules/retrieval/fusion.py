"""Reciprocal Rank Fusion — combining the vector and lexical arms without comparing their scores.

The two arms produce numbers that are not on the same scale and never will be. Cosine similarity
is bounded in roughly [0, 1] and means "how close in meaning". `ts_rank` is unbounded, depends on
document length and term frequency, and means "how well these words match". Adding them, or
weighting them, requires a conversion factor that does not exist — and any constant chosen for one
corpus silently stops being right for another.

RRF sidesteps that by throwing the scores away and keeping only the **ranks**:

    score(d) = Σ  1 / (k + rank_i(d))

over each arm that returned the document. A passage ranked 1st by one arm and absent from the
other still scores well; a passage ranked mid-table by both can outrank it, which is the point —
agreement between two different notions of relevance is evidence.

`k = 60` is the value from Cormack, Clarke & Buettcher (2009), where RRF was introduced and shown
to beat the individual systems it fused, as well as Condorcet and score-based fusion. It is large
relative to the ranks that matter, which is what flattens the difference between 1st and 2nd place
so a single arm's confidence cannot dominate. Exposed as a constant rather than a tunable, because
tuning it needs a labelled evaluation set that does not exist yet — see `KNOWN_DEBTS.md` #30.
"""

RRF_K = 60

# Each arm fetches more candidates than the caller asked for, so fusion has material to work with:
# a passage ranked 8th by vector and 40th by lexical should be able to surface, and it cannot if
# each arm only returned 10 rows. The multiplier is deliberately modest — a larger pool costs a
# wider index scan on every query for steadily less benefit, since RRF weights tail ranks lightly.
CANDIDATE_MULTIPLIER = 5
MAX_CANDIDATES = 200


def candidate_pool(limit: int) -> int:
    """How many rows each arm should retrieve for a caller asking for `limit` results."""
    return min(MAX_CANDIDATES, max(limit, limit * CANDIDATE_MULTIPLIER))


def rrf_score(rank: int | None) -> float:
    """One arm's contribution. A passage the arm did not return contributes nothing rather than
    being penalised — absence is not evidence against, it is just silence."""
    return 0.0 if rank is None else 1.0 / (RRF_K + rank)
