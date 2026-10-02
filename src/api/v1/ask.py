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
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from src.api.v1.retrieval import get_retrieval_service
from src.db.engine import get_db
from src.db.models import User
from src.modules.auth.dependencies import get_current_user
from src.modules.gateway.service import DataBoundaryGateway
from src.modules.generation.followup import expand
from src.modules.generation.models import GeneratedAnswer
from src.modules.generation.provider import GenerationError, SarvamProvider
from src.modules.generation.service import AnswerService
from src.modules.retrieval.service import SearchMode, assess, is_admin

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/ask", tags=["Ask"])

_service: AnswerService | None = None


def get_answer_service() -> AnswerService:
    """A dependency rather than a module-level singleton, so tests can substitute a provider and
    importing this module does not require an API key to exist.

    The provider is wrapped in the gateway here and nowhere else. `AnswerService` holds the
    gateway, not the provider, so there is no assembly in which a caller reaches Sarvam without
    crossing the boundary.
    """
    global _service
    if _service is None:
        _service = AnswerService(gateway=DataBoundaryGateway(provider=SarvamProvider()))
    return _service


@router.post(
    "",
    response_model=GeneratedAnswer,
    summary="Ask a question and get a written answer with citations",
)
async def ask(
    q: str = Query(..., description="A natural-language question."),
    passages: int = Query(8, ge=1, le=20, description="How many passages to retrieve."),
    previous: str | None = Query(
        None,
        description="The previous question in this conversation. Used only to make a short "
                    "follow-up searchable; never shown to the model as the question.",
    ),
    document_id: list[uuid.UUID] | None = Query(
        None,
        description="Restrict the answer to these documents. Repeat for several. Omit to "
                    "search everything the caller may see.",
    ),
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

    # Scoping is explicit rather than inferred from the wording. "Is there confidential data
    # in this document" is a question about one document, and a system that guesses which one
    # from a pronoun will sometimes guess wrong and answer confidently from the whole corpus --
    # which is exactly what happened before this existed. The caller names the document; the
    # UI makes that a button rather than a thing to type.
    # "What about Category B?" has almost none of the words that would find the passage it
    # is about. The previous question supplies them — for the search only. The model still
    # receives the question as asked.
    search_query, expanded = expand(question, previous)

    try:
        results, _usage = retrieval.search(
            db,
            search_query,
            user_id=current_user.user_id,
            is_admin=is_admin(current_user.role),
            limit=passages,
            mode=SearchMode.HYBRID,
            document_ids=document_id or None,
        )
    except SQLAlchemyError as e:
        # Retrieval reaches a BM25 index that is maintained as rows change, and a query issued
        # while that index is being rebuilt can fail inside pg_search ("bitmap cursor source
        # was never initialized"). Seen once, during a bulk UPDATE over every chunk row, and
        # not reproducible afterwards.
        #
        # Caught rather than left to become a 500 because the distinction matters to whoever is
        # reading: a stack trace says "this is broken", while this says "ask again" — which is
        # the correct advice for a transient index state, and asking again is free.
        logger.exception("Retrieval failed for %r", question[:80])
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="SEARCH_UNAVAILABLE: The search index was busy. Please ask again.",
        ) from e

    grounded, _best = assess(results, question)

    # A scoped question supplies its own grounding. The gate exists because vector search always
    # returns *something*, so a question about cake comes back with the nearest policy passage
    # wearing a citation. But when the caller has pointed at a specific document and asked what
    # it says, they have already asserted the relevance the gate is there to check — and the
    # useful answer is "this document does not mention that", which the model can give from the
    # passages. "I don't know" in response to "what does THIS document say" reads as a failure
    # to look.
    if document_id and results:
        grounded = True

    if document_id and not results:
        # Distinct from "the corpus has nothing on this". The scope is the reason, and saying
        # so is the difference between the reader widening the search and concluding the
        # document does not cover it.
        logger.info("Scoped question returned nothing: %r in %s", question[:60], document_id)

    # Retrieval already decided the caller may *see* these. Whether they may *leave the
    # network* is a separate question, and it belongs to the gateway rather than here: this
    # endpoint's job is to say which tier each passage carries, and the boundary's job is to
    # decide what that means. Deciding it in two places is how two places drift apart.
    tiers = _tiers_for(db, results)

    try:
        answer = answers.answer(question, results, grounded=grounded, tiers=tiers)
        if expanded:
            answer.searched_for = search_query
        return answer
    except GenerationError as e:
        # A model outage must not look like an empty corpus. Retrieval worked; say so.
        logger.error("Generation failed for %r: %s", question[:80], e)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"ANSWER_UNAVAILABLE: {e} Retrieval still works — try /search.",
        ) from e


def _tiers_for(db: Session, results) -> list[str | None]:
    """Each passage's classification, positionally aligned with `results`.

    One query for the whole set rather than one per passage: the previous version issued a
    SELECT per result, which on a ten-passage answer was ten round trips to decide something
    the database could answer once.

    A document whose classification cannot be found yields None, and the gateway reads None as
    the highest tier rather than the lowest. A missing record is not evidence of safety.
    """
    from src.db.models import Document as DocumentORM

    ids = {r.document_id for r in results}
    if not ids:
        return []
    rows = db.query(DocumentORM.document_id, DocumentORM.classification).filter(
        DocumentORM.document_id.in_(ids)
    ).all()
    by_id = {row[0]: row[1] for row in rows}
    return [by_id.get(r.document_id) for r in results]
