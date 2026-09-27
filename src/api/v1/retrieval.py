"""Semantic search endpoint — the first thing in this system that reads the vectors.

Stages 1–7 fill an index; this is what asks it a question. A result is a *passage* with its
citation, not a document, because that is what chunking was for.

Permission filtering happens in SQL inside `RetrievalService`, not on the list this returns. See
that module's docstring for why post-filtering is a correctness failure here rather than merely
untidy.
"""

import logging

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from src.core.config import settings
from src.db.engine import get_db
from src.db.models import User
from src.modules.auth.dependencies import get_current_user
from src.modules.document_pipeline.embedding.provider import FastEmbedProvider
from src.modules.retrieval.models import SearchResponse
from src.modules.retrieval.service import RetrievalService, is_admin

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/search", tags=["Search"])

# The model is loaded once per process and reused. Loading costs seconds and a few hundred MB, so
# doing it per request would dominate a 15ms query. It is deliberately *not* created at import
# time: that would make every process that imports this module — including test collection — pay
# for weights it may never use.
_service: RetrievalService | None = None


def get_retrieval_service() -> RetrievalService:
    """FastAPI dependency. Overridable in tests, which is why it is a dependency rather than a
    module-level singleton the endpoint reaches for directly."""
    global _service
    if _service is None:
        _service = RetrievalService(
            provider=FastEmbedProvider(
                model_name=settings.EMBEDDING_MODEL,
                dimensions=settings.EMBEDDING_DIMENSIONS,
            )
        )
    return _service


@router.get(
    "",
    response_model=SearchResponse,
    summary="Semantic search over document passages, with citations",
)
async def semantic_search(
    q: str = Query(..., description="A natural-language question."),
    limit: int = Query(10, ge=1, le=50),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    service: RetrievalService = Depends(get_retrieval_service),
) -> SearchResponse:
    """Returns the passages most semantically similar to `q` that the caller may see.

    Unlike `/documents/search`, which matches literal words in a document's title, filename and
    description, this matches *meaning* against the body text — a question about enforcement
    obligations finds the relevant clause without sharing any keyword with it.
    """
    query = q.strip()
    if not query:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="EMPTY_QUERY: q is required.",
        )

    results = service.search(
        db,
        query,
        user_id=current_user.user_id,
        is_admin=is_admin(current_user.role),
        limit=limit,
    )
    return SearchResponse(query=query, count=len(results), results=results)
