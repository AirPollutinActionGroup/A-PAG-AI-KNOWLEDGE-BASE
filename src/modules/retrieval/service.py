"""Semantic retrieval: embed the question, rank passages by cosine distance, in one SQL statement.

**The permission predicate lives in the WHERE clause, and that is the whole design.**

`src/api/v1/ingestion.py`'s single-document endpoints check visibility on a document already in
hand, which is fine. Filtering a *result set* after the query is not: for paginated listing it lets
invisible rows consume page slots and leaks their count via `total`, and for top-k retrieval it is
a correctness failure — a RESTRICTED passage removed after ranking has already taken its slot, so
`limit=5` returns four results, or none, and the caller cannot distinguish a thin corpus from a
withheld answer.

The predicate itself comes from `src/modules/auth/access.py` rather than being written here, so
this query and the document list/search cannot drift apart on what "visible" means.
`tests/integration/test_vector_search.py` pinned the shape before the endpoint existed;
`test_retrieval_service.py` exercises the shipped query.

The question is embedded with `embed_query()`, never `embed_passages()`. Several model families
are asymmetric, and using the passage form for a query degrades retrieval with no error raised —
see the provider's module docstring.
"""

import logging
import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from src.db.models import Document as DocumentORM
from src.db.models import DocumentChunk as ChunkORM
from src.modules.auth.access import is_admin, visible_documents_clause
from src.modules.document_pipeline.embedding.provider import EmbeddingProvider
from src.modules.retrieval.models import RetrievedChunk, TokenUsage

logger = logging.getLogger(__name__)

__all__ = ["RetrievalService", "is_admin"]


class RetrievalService:
    """Turns a natural-language question into ranked, citable passages."""

    def __init__(self, provider: EmbeddingProvider):
        self._provider = provider

    @property
    def model_name(self) -> str:
        return self._provider.model_name

    def search(
        self,
        db: Session,
        query: str,
        *,
        user_id: uuid.UUID | None,
        is_admin: bool,
        limit: int = 10,
    ) -> tuple[list[RetrievedChunk], TokenUsage]:
        """Returns the `limit` passages nearest to `query` that this user may see, and what the
        query cost in tokens."""
        vector = self._provider.embed_query(query)

        # `cosine_distance` emits pgvector's `<=>`, which is the operator the HNSW index was built
        # for (`vector_cosine_ops`). Any other distance function here would still return correct
        # results, but silently as a sequential scan over every chunk in the corpus.
        distance = ChunkORM.embedding.cosine_distance(vector)

        stmt = (
            select(
                ChunkORM.chunk_id,
                ChunkORM.document_id,
                DocumentORM.filename,
                ChunkORM.text,
                ChunkORM.page_number,
                ChunkORM.section_heading,
                ChunkORM.is_table,
                # Callers expect a score where bigger is better; `<=>` is a distance, where
                # smaller is. Returning it raw would invert every caller's reading of a result.
                (1 - distance).label("score"),
            )
            .join(DocumentORM, DocumentORM.document_id == ChunkORM.document_id)
            .where(
                ChunkORM.embedding.is_not(None),
                DocumentORM.deleted_at.is_(None),
                visible_documents_clause(DocumentORM, user_id, viewer_is_admin=is_admin),
            )
            .order_by(distance)
            .limit(limit)
        )

        rows = db.execute(stmt).mappings().all()

        # Counted here rather than stored on the row: the number belongs to whichever model is
        # configured now, and a stored count would go stale the moment the model changed.
        window = self._provider.max_sequence_tokens
        passage_tokens = self._provider.count_tokens([r["text"] for r in rows])
        query_tokens = self._provider.count_tokens([query])[0] if query else 0

        results = [
            RetrievedChunk(**row, token_count=n, truncated=n > window)
            for row, n in zip(rows, passage_tokens, strict=True)
        ]
        usage = TokenUsage(
            query_tokens=query_tokens,
            context_tokens=sum(passage_tokens),
            max_sequence_tokens=window,
            truncated_results=sum(1 for r in results if r.truncated),
            model=self._provider.model_name,
        )

        logger.info(
            "Retrieval: results=%d limit=%d admin=%s model=%s context_tokens=%d truncated=%d",
            len(rows), limit, is_admin, self._provider.model_name,
            usage.context_tokens, usage.truncated_results,
        )
        return results, usage
