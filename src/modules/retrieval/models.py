"""DTOs for semantic retrieval.

A result is a *passage*, not a document: the whole point of chunking was that a citation should
point at the specific text that answers the question, not at a 200-page PDF. The citation fields
therefore travel with the text rather than being looked up afterwards.
"""

import uuid

from pydantic import BaseModel, Field


class RetrievedChunk(BaseModel):
    """One passage, with everything needed to cite it."""

    chunk_id: uuid.UUID
    document_id: uuid.UUID
    filename: str
    text: str

    # The citation contract established at chunking time. `page_number` is a per-format unit
    # (page / slide / worksheet), and `section_heading` is None where extraction could not
    # attribute the passage to one heading — see KNOWN_DEBTS.md #19 on why a missing heading is
    # preferred to a guessed one.
    page_number: int | None = None
    section_heading: str | None = None
    is_table: bool = False

    # The fusion score: the sum of `1 / (RRF_K + rank)` over each arm that returned this passage.
    #
    # Deliberately NOT a similarity. It is small (around 0.016 for a single first place) and has
    # no absolute meaning — only the ordering within one result set does. Comparing scores across
    # two different queries is meaningless, and a low number is not a weak match. That is the
    # cost of never having to convert between cosine distance and ts_rank, which are not on a
    # common scale and cannot be made to be. See `fusion.py`.
    #
    # `semantic_rank` / `lexical_rank` below are what to read if you want to know how well a
    # passage actually did.
    score: float

    # The passage's true token length, and whether that exceeded the embedding model's window.
    # A truncated passage is stored whole but was embedded only up to the cap, so a search for
    # something mentioned solely in its tail will not find it. Surfacing it means the caller can
    # see that rather than concluding the corpus does not contain the answer.
    token_count: int = 0
    truncated: bool = False

    # Which arm found this passage, and where it placed. Null means that arm did not return it
    # at all. Kept because "why did this come back?" is a question people actually ask, and
    # "the lexical arm ranked it 3rd; the vector arm missed it" is a real answer — the kind that
    # tells you whether a disappointing result is a chunking problem or a model problem.
    semantic_rank: int | None = None
    lexical_rank: int | None = None

    # Raw cosine similarity from the semantic arm, 0-1, absolute and comparable across queries —
    # unlike `score`. This is what "did we actually find anything?" is decided on, because an RRF
    # score cannot answer it: the worst passage in the corpus scores the same as the best if it
    # happens to rank first. None when the semantic arm did not return this passage.
    similarity: float | None = None

    # The cross-encoder's score for (query, this passage), and where fusion had placed it before
    # reranking. Both are None when reranking is off or unavailable.
    #
    # `rerank_score` is an unbounded logit on the model's own scale: like `score`, it orders and
    # does not measure. It is emphatically not a similarity, and the grounding gate does not read
    # it — "this passage beats that one" and "the corpus contains an answer" are different
    # questions, and only the second decides whether to say "I don't know".
    #
    # `fusion_rank` is kept because the movement is the interesting part. On this corpus the
    # passage that best answered a question was routinely 9th, 16th or 24th by fusion — outside
    # any top-8 — and showing "was #16" is how someone can see the reranker earning its latency
    # rather than take it on trust.
    rerank_score: float | None = None
    fusion_rank: int | None = None


class TokenUsage(BaseModel):
    """What this query cost the embedding model, and what a downstream LLM would be handed.

    There is no generation step yet, so `context_tokens` is a projection rather than a bill: it is
    the size of the context these passages would form if they were sent to a model. It is the
    number that decides whether a future answer fits in a prompt, which makes it worth showing
    now, while chunk sizing can still be changed cheaply.
    """

    query_tokens: int = 0
    context_tokens: int = 0
    max_sequence_tokens: int = 0
    truncated_results: int = 0
    model: str = ""

    # Which mode ran, and how many of the returned passages each arm contributed. The two counts
    # overlap — a passage found by both is counted in each — so they are a picture of where the
    # results came from, not a partition of them.
    mode: str = "hybrid"
    semantic_hits: int = 0
    lexical_hits: int = 0


class SearchResponse(BaseModel):
    query: str
    count: int
    results: list[RetrievedChunk] = Field(default_factory=list)
    usage: TokenUsage = Field(default_factory=TokenUsage)

    # False when nothing cleared `SEARCH_MIN_SIMILARITY` — the corpus has nothing on this, and
    # saying so is more useful than presenting the nearest passages as though they were answers.
    # Vector search always returns *something*: there is no such thing as no nearest neighbour,
    # so without this a question about cake returns the nearest policy document with a citation
    # and full apparent confidence.
    grounded: bool = True
    best_similarity: float | None = None
