"""Semantic, lexical, and hybrid retrieval over embedded passages.

**The permission predicate lives in the WHERE clause of every arm, and that is the whole design.**

Filtering a result set after the query is a correctness failure for top-k, not merely untidy: a
RESTRICTED passage removed after ranking has already taken its slot, so `limit=5` returns four
results, or none, and the caller cannot distinguish a thin corpus from a withheld answer. The
predicate comes from `src/modules/auth/access.py` rather than being written here, so retrieval and
the document list/search cannot drift on what "visible" means. Because fusion runs two arms, the
clause appears twice — which is exactly why it is a shared function and not a string.

**Why hybrid.** Vector search matches meaning and is poor at exact identifiers: "Section 114",
"GRAP Stage III", a district name. An embedding places those near whatever they are semantically
similar to, which for a policy corpus is a real gap. A lexical index matches them exactly and is
in turn blind to paraphrase. The two fail differently, so fusing them covers more than either.

The arms' scores are never compared — see `fusion.py` for why ranks are used instead.

The question is embedded with `embed_query()`, never `embed_passages()`. Several model families
are asymmetric, and using the passage form for a query degrades retrieval with no error raised.
"""

import enum
import logging
import re
import uuid

from sqlalchemy import Float, cast, func, literal, select, text
from sqlalchemy.orm import Session

from src.core.config import settings
from src.db.models import Document as DocumentORM
from src.db.models import DocumentChunk as ChunkORM
from src.modules.auth.access import is_admin, visible_documents_clause
from src.modules.document_pipeline.embedding.provider import EmbeddingProvider
from src.modules.retrieval.fusion import RRF_K, candidate_pool
from src.modules.retrieval.models import RetrievedChunk, TokenUsage
from src.modules.retrieval.rerank import Reranker, RerankUnavailable, get_reranker

logger = logging.getLogger(__name__)

__all__ = ["RetrievalService", "SearchMode", "assess", "is_admin"]

# pg_search parses the query string itself: bare terms, "quoted phrases", +required, -excluded.
# Quoted phrases matter here — "GRAP Stage III" as a phrase is precisely the query a lexical arm
# exists to serve.


class SearchMode(str, enum.Enum):
    """Exposed rather than hidden so a result can be explained. When someone asks why a passage
    came back, the honest answer often is "the lexical arm found it and the vector arm did not",
    and being able to re-run one arm alone is how that gets established."""

    HYBRID = "hybrid"
    SEMANTIC = "semantic"
    LEXICAL = "lexical"


class RetrievalService:
    """Turns a natural-language question into ranked, citable passages."""

    def __init__(self, provider: EmbeddingProvider, reranker: Reranker | None = None):
        self._provider = provider

    @property
    def model_name(self) -> str:
        return self._provider.model_name

    # ------------------------------------------------------------------ arms

    def _visible(self, user_id, viewer_is_admin, document_ids=None):
        """Permission predicates, plus an optional scope to particular documents.

        The scope sits here, beside the permission clause and inside every arm, for the same
        reason the permission clause does: a passage removed after ranking has already taken a
        top-k slot, so filtering afterwards returns fewer results than asked for and the caller
        cannot tell whether the document was thin or the filter was narrow.

        It is never a substitute for the permission clause and is always applied with it — a
        caller naming a document id they may not see still gets nothing.
        """
        predicates = [
            DocumentORM.deleted_at.is_(None),
            visible_documents_clause(DocumentORM, user_id, viewer_is_admin=viewer_is_admin),
        ]
        if document_ids:
            predicates.append(DocumentORM.document_id.in_(list(document_ids)))
        return tuple(predicates)

    def _semantic_cte(self, vector, pool, user_id, viewer_is_admin, document_ids=None):
        distance = ChunkORM.embedding.cosine_distance(vector)
        return (
            select(
                ChunkORM.chunk_id.label("chunk_id"),
                func.row_number().over(order_by=distance).label("rank"),
                # Carried alongside the rank because it is the only absolute measure available:
                # ranks say which passage is nearest, similarity says whether "nearest" means
                # anything at all.
                (1 - distance).label("similarity"),
            )
            .join(DocumentORM, DocumentORM.document_id == ChunkORM.document_id)
            .where(
                ChunkORM.embedding.is_not(None),
                *self._visible(user_id, viewer_is_admin, document_ids),
            )
            .order_by(distance)
            .limit(pool)
            .cte("semantic_arm")
        )

    def _lexical_cte(self, query, pool, user_id, viewer_is_admin, document_ids=None):
        """BM25 over chunk bodies, via pg_search's `@@@` operator and `paradedb.score()`.

        This replaced a `tsvector`/`ts_rank` arm, and the reason was recall rather than ranking.
        `websearch_to_tsquery` builds a conjunction: every term must appear in the same chunk.
        Measured on this corpus, that returned **0** rows for "penalties for non-compliance",
        1 for "air quality targets" and 2 for "enforcement obligations" — the multi-word policy
        questions this system exists to answer. BM25 scores partial matches instead, ranking by
        how many terms hit and how rare they are, and returned a full page for all three.

        It also fixes the bias `ts_rank` is known for: no document-length normalisation, so long
        passages score high merely for being long. On the same queries `ts_rank` returned chunks
        averaging 1,237 characters against BM25's 892.

        Weaker partial matches do now enter the pool. That is handled where it should be — RRF
        ranks them low, and the grounding gate drops the result set entirely if nothing clears
        the similarity bar.
        """
        score = func.paradedb.score(ChunkORM.chunk_id)

        # Both indexed fields are searched, not just the body: only 1,056 of 2,475 headed chunks
        # repeat their heading in the text, so for the other 1,419 the heading is signal the body
        # does not carry. `@@@` applied to one column searches that column alone, which silently
        # ignored headings until this was measured.
        #
        # `paradedb.match()` rather than interpolating the query into pg_search's own syntax: it
        # takes the raw user string and tokenizes it, so a question containing a colon, a quote
        # or the word "OR" cannot be reinterpreted as query operators.
        should = (
            "  paradedb.match('text', :lexical_q),"
            "  paradedb.match('section_heading', :lexical_q),"
            # Lets a question name the document it is about. Without this, "summarise the MoP OM
            # dated 20 November" found nothing while that document sat in the corpus, because a
            # filename appears nowhere in the chunk text it indexes. See migration 0018.
            "  paradedb.match('document_title', :lexical_q)"
        )

        if document_ids:
            # The scope goes **inside** the pg_search query, not beside it in the WHERE clause.
            #
            # `@@@` runs as a custom scan, and an ordinary SQL predicate alongside it breaks
            # that scan for some queries and not others -- "who signed this memorandum" was
            # fine, "who signed the FGD extension memorandum" raised
            # `bitmap cursor source was never initialized`. It is not about which table carries
            # the predicate, and disabling bitmap scans does not avoid it. Filtering inside the
            # query is the supported way, and it needs document_id in the index: migration 0019.
            terms = ", ".join(
                # CAST(...) rather than `::uuid`: SQLAlchemy's text() parameter parser reads
                # the second colon of `:scope_0::uuid` as the start of another bind parameter
                # and then cannot find `scope_0` at all.
                f"paradedb.term('document_id', CAST(:scope_{i} AS uuid))"
                for i in range(len(document_ids))
            )
            clause = text(
                "document_chunks.chunk_id @@@ paradedb.boolean("
                f"  must => ARRAY[paradedb.boolean(should => ARRAY[{terms}])],"
                f"  should => ARRAY[{should}]"
                ")"
            ).bindparams(
                lexical_q=query,
                **{f"scope_{i}": str(d) for i, d in enumerate(document_ids)},
            )
        else:
            clause = text(
                "document_chunks.chunk_id @@@ paradedb.boolean(should => ARRAY["
                f"{should}"
                "])"
            ).bindparams(lexical_q=query)

        matches = clause

        return (
            select(
                ChunkORM.chunk_id.label("chunk_id"),
                func.row_number().over(order_by=score.desc()).label("rank"),
            )
            .join(DocumentORM, DocumentORM.document_id == ChunkORM.document_id)
            .where(
                matches,
                # Deliberately without `document_ids`: the scope is already inside `matches`,
                # and repeating it as a SQL predicate is the exact combination that breaks the
                # pg_search custom scan.
                *self._visible(user_id, viewer_is_admin),
            )
            .order_by(score.desc())
            .limit(pool)
            .cte("lexical_arm")
        )

    # --------------------------------------------------------------- search

    @staticmethod
    def _reranker() -> Reranker | None:
        return get_reranker()

    @staticmethod
    def _rerank(reranker, query, rows, limit):
        """Reorders `rows` by cross-encoder score and keeps the best `limit`.

        Returns the kept rows plus, aligned to them, each one's score and the position it held
        before reranking. Without a reranker this is the identity: the first `limit` rows, and
        no scores — a caller must be able to tell "reranking did not move this" from "reranking
        did not run", because the first is a result and the second is an outage.
        """
        if reranker is None or not rows:
            kept = rows[:limit]
            return kept, [None] * len(kept), [None] * len(kept)

        # Truncated for scoring only; the full text is still what gets returned and cited. A
        # cross-encoder costs tokens, and a long table contributes its relevance in the first
        # few hundred characters — the tail is rows, not subject matter.
        cap = settings.RERANK_MAX_CHARS
        passages = [row["text"][:cap] for row in rows]

        try:
            scores = reranker.scores(query, passages)
        except RerankUnavailable as e:
            # Degrade to the fused order rather than failing the search. A reranker is an
            # improvement on results that are already useful; losing it must cost the best
            # ordering, not the answer.
            logger.error("Rerank failed, falling back to fused order: %s", e)
            kept = rows[:limit]
            return kept, [None] * len(kept), [None] * len(kept)

        if len(scores) != len(rows):
            logger.error("Reranker returned %d scores for %d passages; keeping fused order",
                         len(scores), len(rows))
            kept = rows[:limit]
            return kept, [None] * len(kept), [None] * len(kept)

        # Descending by score; ties break on the fused position so a repeated query returns a
        # repeated order, the same guarantee the SQL tiebreak gives.
        order = sorted(range(len(rows)), key=lambda i: (-scores[i], i))[:limit]
        return (
            [rows[i] for i in order],
            [float(scores[i]) for i in order],
            [i + 1 for i in order],
        )

    def search(
        self,
        db: Session,
        query: str,
        *,
        user_id: uuid.UUID | None,
        is_admin: bool,
        limit: int = 10,
        mode: SearchMode = SearchMode.HYBRID,
        rerank: bool | None = None,
        document_ids: list[uuid.UUID] | None = None,
    ) -> tuple[list[RetrievedChunk], TokenUsage]:
        """Returns the `limit` passages best matching `query` that this user may see.

        With reranking on, the SQL fetches a wider pool (`RERANK_CANDIDATES`) and a cross-encoder
        chooses the `limit` best from it. The wider fetch is the point: the passage that actually
        answers a question is frequently well outside the top few by fusion, and no amount of
        reordering can recover a row that was never selected.
        """
        reranker = self._reranker() if (rerank is None or rerank) else None
        # Fetch wide enough for the reranker to have something to choose between, but never
        # narrower than the caller asked for.
        fetch = max(limit, settings.RERANK_CANDIDATES) if reranker else limit
        pool = candidate_pool(fetch)
        needs_vector = mode in (SearchMode.HYBRID, SearchMode.SEMANTIC)
        vector = self._provider.embed_query(query) if needs_vector else None

        sem = (
            self._semantic_cte(vector, pool, user_id, is_admin, document_ids)
            if needs_vector else None
        )
        lex = (
            self._lexical_cte(query, pool, user_id, is_admin, document_ids)
            if mode in (SearchMode.HYBRID, SearchMode.LEXICAL)
            else None
        )

        # `1 / (k + rank)` per arm, zero where the arm did not return the row. Cast to float so
        # integer division cannot quietly turn every contribution into 0.
        def contribution(cte):
            if cte is None:
                return literal(0.0)
            return func.coalesce(
                cast(1.0, Float) / (RRF_K + cast(cte.c.rank, Float)), 0.0
            )

        sem_rank = sem.c.rank if sem is not None else literal(None)
        lex_rank = lex.c.rank if lex is not None else literal(None)

        if sem is not None and lex is not None:
            # FULL OUTER JOIN: a passage found by only one arm must still be a candidate. An
            # INNER JOIN here would silently reduce hybrid search to "results both arms agree
            # on" — a narrower set than either arm alone, the opposite of the intent.
            joined = sem.join(lex, sem.c.chunk_id == lex.c.chunk_id, full=True)
            chunk_id = func.coalesce(sem.c.chunk_id, lex.c.chunk_id)
        else:
            single = sem if sem is not None else lex
            joined, chunk_id = single, single.c.chunk_id

        fused = (
            select(
                chunk_id.label("chunk_id"),
                (contribution(sem) + contribution(lex)).label("score"),
                sem_rank.label("semantic_rank"),
                lex_rank.label("lexical_rank"),
                (sem.c.similarity if sem is not None else literal(None)).label("similarity"),
            )
            .select_from(joined)
            .cte("fused")
        )

        stmt = (
            select(
                ChunkORM.chunk_id,
                ChunkORM.document_id,
                DocumentORM.filename,
                ChunkORM.text,
                ChunkORM.page_number,
                ChunkORM.section_heading,
                ChunkORM.is_table,
                fused.c.score,
                fused.c.semantic_rank,
                fused.c.lexical_rank,
                fused.c.similarity,
            )
            .select_from(fused)
            .join(ChunkORM, ChunkORM.chunk_id == fused.c.chunk_id)
            .join(DocumentORM, DocumentORM.document_id == ChunkORM.document_id)
            # Ties are common in RRF because scores come from a small set of rank reciprocals.
            # chunk_id is an arbitrary but stable tiebreak, so a repeated query returns a
            # repeated order — an unstable result list reads as a bug to whoever is using it.
            .order_by(fused.c.score.desc(), ChunkORM.chunk_id)
            .limit(fetch)
        )

        rows = db.execute(stmt).mappings().all()

        # Reranking happens here, on rows the permission predicate has already filtered — a
        # cross-encoder must never be shown a passage its caller may not see, and putting it
        # after the SQL means it cannot be.
        rows, rerank_scores, fusion_ranks = self._rerank(reranker, query, rows, limit)

        # Counted here rather than stored on the row: the number belongs to whichever model is
        # configured now, and a stored count would go stale the moment the model changed.
        window = self._provider.max_sequence_tokens
        passage_tokens = self._provider.count_tokens([r["text"] for r in rows])
        query_tokens = self._provider.count_tokens([query])[0] if query else 0

        results = [
            RetrievedChunk(
                chunk_id=row["chunk_id"],
                document_id=row["document_id"],
                filename=row["filename"],
                text=row["text"],
                page_number=row["page_number"],
                section_heading=row["section_heading"],
                is_table=row["is_table"],
                score=float(row["score"]),
                semantic_rank=row["semantic_rank"],
                lexical_rank=row["lexical_rank"],
                similarity=float(row["similarity"]) if row["similarity"] is not None else None,
                token_count=n,
                truncated=n > window,
                rerank_score=rerank_scores[i],
                fusion_rank=fusion_ranks[i],
            )
            for i, (row, n) in enumerate(zip(rows, passage_tokens, strict=True))
        ]
        usage = TokenUsage(
            query_tokens=query_tokens,
            context_tokens=sum(passage_tokens),
            max_sequence_tokens=window,
            truncated_results=sum(1 for r in results if r.truncated),
            model=self._provider.model_name,
            mode=mode.value,
            semantic_hits=sum(1 for r in results if r.semantic_rank is not None),
            lexical_hits=sum(1 for r in results if r.lexical_rank is not None),
        )

        logger.info(
            "Retrieval[%s]: results=%d limit=%d pool=%d admin=%s sem=%d lex=%d "
            "context_tokens=%d truncated=%d",
            mode.value, len(rows), limit, pool, is_admin,
            usage.semantic_hits, usage.lexical_hits,
            usage.context_tokens, usage.truncated_results,
        )
        if any(r.fusion_rank is not None and r.fusion_rank > limit for r in results):
            # Worth a line in the log: it means the reranker pulled in a passage that the
            # un-reranked endpoint would never have shown, which is the whole reason it exists.
            promoted = [r.fusion_rank for r in results if r.fusion_rank and r.fusion_rank > limit]
            logger.info("Rerank promoted %d passage(s) from beyond the cut: was %s",
                        len(promoted), promoted)
        return results, usage


# Words too common to carry meaning in a question. A short list on purpose: this is not the
# stopword handling retrieval needs — BM25 does that — it only decides which of the asker's words
# a passage must literally contain for the lexical override below to apply.
_UNINFORMATIVE = frozenset({
    "the", "a", "an", "of", "for", "and", "or", "in", "on", "to", "is", "are", "was", "were",
    "what", "which", "who", "whom", "when", "where", "why", "how", "does", "do", "did", "can",
    "could", "should", "would", "that", "this", "these", "those", "with", "from", "by", "at",
    "as", "be", "been", "being", "it", "its", "any", "all", "best", "about", "there", "their",
    "you", "your", "our", "we", "they", "them", "has", "have", "had", "will", "may", "must",
})


def informative_terms(query: str) -> list[str]:
    """The words of a question that would have to appear verbatim for a literal match to mean
    anything. Short words are dropped with the stopwords: a two-letter token matches everywhere."""
    return [
        t for t in re.findall(r"[\w-]+", query.lower())
        if len(t) > 2 and t not in _UNINFORMATIVE
    ]


def _has_literal_match(query: str, results: list[RetrievedChunk]) -> bool:
    """True when some returned passage contains *every* informative word of the question."""
    terms = informative_terms(query)
    if not terms:
        return False
    return any(
        all(term in chunk.text.lower() for term in terms)
        for chunk in results
        if chunk.lexical_rank is not None
    )


def assess(results: list[RetrievedChunk], query: str = "") -> tuple[bool, float | None]:
    """Did this query actually find anything, or just return its least-bad guess?

    Vector search always returns something — there is no such thing as no nearest neighbour —
    so without this check a question about cake comes back with the nearest policy passage,
    a citation, and every appearance of confidence. Cosine similarity is the only absolute
    measure available; the RRF score is built from ranks and cannot distinguish "best in the
    corpus" from "best of a bad lot".

    **The lexical override requires the words to actually be there.** It exists for rare
    identifiers — "GRAPSTAGETHREE", a statute number — where similarity is low and the string is
    plainly in the document. It originally accepted *any* lexical hit, which was sound while the
    lexical arm was a conjunction requiring every term to match. BM25 scores partial matches, so
    under it nearly every question returns lexical hits and the gate stopped firing: "what is the
    best chocolate cake recipe" came back grounded at 0.434 similarity against a corpus of
    power-plant filings. The override now checks what it always meant to — that some returned
    passage literally contains every informative word of the question.

    `query` defaults to empty so the override simply does not apply when a caller has no query to
    check against, which is the safe direction.
    """
    best = max((r.similarity for r in results if r.similarity is not None), default=None)

    if query and _has_literal_match(query, results):
        return True, best
    if best is None:
        return False, None
    return best >= settings.SEARCH_MIN_SIMILARITY, best
