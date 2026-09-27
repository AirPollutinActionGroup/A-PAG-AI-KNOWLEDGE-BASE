"""Semantic retrieval: embed the question, rank passages by cosine distance, in one SQL statement.

**The permission predicate lives in the WHERE clause, and that is the whole design.**

`src/api/v1/ingestion.py`'s list/search endpoints filter in Python *after* SQL returns. For
paginated document listing that is untidy — restricted rows consume page slots and `total` leaks
their count. For top-k retrieval it is a correctness failure: a RESTRICTED chunk filtered
afterwards has already won its slot, so a `LIMIT 5` can return four results, or none, and the user
has no way to tell whether the corpus is thin or the answer was withheld. Filtering before
`ORDER BY`/`LIMIT` means top-k is computed over exactly the rows the caller may see.

`tests/integration/test_vector_search.py` pinned this shape before the endpoint existed.

The query is embedded with `embed_query()`, never `embed_passages()`. Several model families are
asymmetric, and using the passage form for a query degrades retrieval with no error raised — see
the provider's module docstring.
"""

import logging
import uuid

from sqlalchemy import text
from sqlalchemy.orm import Session

from src.db.enums import Classification, UserRole
from src.modules.document_pipeline.embedding.provider import EmbeddingProvider
from src.modules.retrieval.models import RetrievedChunk

logger = logging.getLogger(__name__)

# `<=>` is pgvector's cosine *distance*, so the HNSW index built with `vector_cosine_ops` is the
# one that serves this ordering. Using a different operator here would silently fall back to a
# sequential scan — correct results, but a full table scan per query.
_SEARCH_SQL = text("""
    SELECT
        c.chunk_id,
        c.document_id,
        d.filename,
        c.text,
        c.page_number,
        c.section_heading,
        c.is_table,
        1 - (c.embedding <=> CAST(:query_vector AS vector)) AS score
    FROM document_chunks c
    JOIN documents d ON d.document_id = c.document_id
    WHERE c.embedding IS NOT NULL
      AND d.deleted_at IS NULL
      AND (
            d.classification <> :restricted
         OR d.uploader_user_id = :user_id
         OR :is_admin
      )
    ORDER BY c.embedding <=> CAST(:query_vector AS vector)
    LIMIT :limit
""")


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
    ) -> list[RetrievedChunk]:
        """Returns the `limit` passages nearest to `query` that this user is allowed to see."""
        vector = self._provider.embed_query(query)

        rows = db.execute(
            _SEARCH_SQL,
            {
                # pgvector accepts the literal text form of a vector; `str(list[float])` is
                # already `[0.1, 0.2, ...]`, which is exactly that syntax.
                "query_vector": str(vector),
                "restricted": Classification.RESTRICTED.value,
                "user_id": user_id,
                "is_admin": is_admin,
                "limit": limit,
            },
        ).mappings().all()

        logger.info(
            "Retrieval: results=%d limit=%d admin=%s model=%s",
            len(rows), limit, is_admin, self._provider.model_name,
        )
        return [RetrievedChunk(**row) for row in rows]


def is_admin(role: str | None) -> bool:
    """Single place the admin check is spelled, so retrieval and `_can_view()` cannot drift."""
    return role == UserRole.ADMIN.value
