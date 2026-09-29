"""`POST /api/v1/ask` — a written answer over the corpus, with citations.

The difference from `/search`: that returns passages and leaves the reading to you; this reads
them and writes an answer. Everything else is the same pipeline — the same hybrid retrieval, the
same permission filtering in SQL, the same grounding gate.

The gate is what makes this safe to expose. It runs *before* the model is called, so a question
the corpus cannot answer never reaches Sarvam: no cost, and no opportunity to write a confident
paragraph from nothing. When it fails, this endpoint returns the same "I don't have anything on
that" as `/search`, and no text leaves the deployment.
"""

import logging

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from src.api.v1.retrieval import get_retrieval_service
from src.core.config import settings
from src.db.engine import get_db
from src.db.enums import Classification
from src.db.models import User
from src.modules.auth.dependencies import get_current_user
from src.modules.generation.models import GeneratedAnswer
from src.modules.generation.provider import GenerationError, SarvamProvider
from src.modules.generation.service import AnswerService
from src.modules.retrieval.service import SearchMode, assess, is_admin

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/ask", tags=["Ask"])

_service: AnswerService | None = None


def get_answer_service() -> AnswerService:
    """A dependency rather than a module-level singleton, so tests can substitute a provider and
    importing this module does not require an API key to exist."""
    global _service
    if _service is None:
        _service = AnswerService(provider=SarvamProvider())
    return _service


@router.post(
    "",
    response_model=GeneratedAnswer,
    summary="Ask a question and get a written answer with citations",
)
async def ask(
    q: str = Query(..., description="A natural-language question."),
    passages: int = Query(8, ge=1, le=20, description="How many passages to retrieve."),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    retrieval=Depends(get_retrieval_service),
    answers: AnswerService = Depends(get_answer_service),
) -> GeneratedAnswer:
    """Retrieves passages, then writes an answer from them and nothing else.

    Every claim carries the passage it came from. A marker pointing at a passage the model was not
    given is stripped and reported in `invalid_markers` — that is fabricated provenance, and the
    most dangerous error this system can make, because a citation is what makes a reader stop
    checking.
    """
    question = q.strip()
    if not question:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="EMPTY_QUERY: q is required.",
        )

    results, _usage = retrieval.search(
        db,
        question,
        user_id=current_user.user_id,
        is_admin=is_admin(current_user.role),
        limit=passages,
        mode=SearchMode.HYBRID,
    )
    grounded, _best = assess(results, question)

    # Retrieval already decided the caller may see these. This is the separate question of
    # whether they may leave the network — see GENERATION_INCLUDE_RESTRICTED.
    # Architecture §3 and §5: external inference is for non-restricted content, and a request
    # takes the highest tier present across every passage — one restricted passage makes the
    # whole request restricted. Rather than refusing outright, the restricted passages are
    # withheld and the caller is told how many, so an answer built from part of the evidence is
    # never mistaken for the whole of it.
    withheld = 0
    if not settings.GENERATION_INCLUDE_RESTRICTED:
        kept = [r for r in results if _is_public(db, r.document_id)]
        withheld = len(results) - len(kept)
        if withheld:
            logger.info("Withheld %d restricted passage(s) from generation", withheld)
        results = kept
        grounded = grounded and bool(results)

    try:
        answer = answers.answer(question, results, grounded=grounded)
        answer.excluded_restricted = withheld
        return answer
    except GenerationError as e:
        # A model outage must not look like an empty corpus. Retrieval worked; say so.
        logger.error("Generation failed for %r: %s", question[:80], e)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"ANSWER_UNAVAILABLE: {e} Retrieval still works — try /search.",
        ) from e


def _is_public(db: Session, document_id) -> bool:
    from src.db.models import Document as DocumentORM

    classification = db.query(DocumentORM.classification).filter(
        DocumentORM.document_id == document_id
    ).scalar()
    return classification != Classification.RESTRICTED.value
