"""FastAPI Application router aggregation and UI delivery."""

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded

from src.api.v1.ask import router as ask_router
from src.api.v1.auth import router as auth_router
from src.api.v1.ingestion import limiter as upload_limiter
from src.api.v1.ingestion import router as ingestion_router
from src.api.v1.retrieval import router as retrieval_router

logger = logging.getLogger(__name__)

@asynccontextmanager
async def lifespan(_app: FastAPI):
    """Load the retrieval models before the first question rather than during it.

    Both the embedding model and the cross-encoder are built lazily on first use, which put
    their entire load time in front of whoever asked first: a measured 36 seconds for a question
    that takes 2.5 once warm. That reads as a broken deployment, and it is paid again after
    every restart and every deploy.

    Warming is best-effort on purpose. A model that cannot load must not stop the API starting —
    retrieval degrades to un-reranked results and generation to a 503, both of which are states
    the callers already handle, whereas a container that will not boot is not.
    """
    import asyncio

    def _warm():
        try:
            from src.api.v1.retrieval import get_retrieval_service
            get_retrieval_service()
            logger.info("Retrieval models warm")
        except Exception as e:
            logger.error("Could not warm retrieval models: %s", e)
        try:
            from src.modules.retrieval.rerank import get_reranker
            get_reranker()
        except Exception as e:
            logger.error("Could not warm the reranker: %s", e)

    # In a thread: these are CPU-bound model loads, and awaiting them on the event loop would
    # hold the health check down for the duration and fail the container's start period.
    asyncio.get_running_loop().run_in_executor(None, _warm)
    yield


app = FastAPI(
    title="A-PAG AI Knowledge Base API",
    description="Clean, sovereign document ingestion pipeline for policy PDFs.",
    version="1.0.0",
    lifespan=lifespan,
)

app.state.limiter = upload_limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)  # type: ignore[arg-type]

# API v1 routes
app.include_router(auth_router, prefix="/api/v1")
app.include_router(ingestion_router, prefix="/api/v1")
app.include_router(retrieval_router, prefix="/api/v1")
app.include_router(ask_router, prefix="/api/v1")

STATIC_DIR = Path(__file__).resolve().parent.parent.parent / "static"
INDEX_HTML = STATIC_DIR / "index.html"
SEARCH_HTML = STATIC_DIR / "search.html"

# Brand assets (the A-PAG logo). Mounted rather than served by a hand-written route so adding an
# icon later needs no code. Deliberately a *subdirectory* of static/ and not static/ itself: the
# pages are served by the explicit routes below, and mounting the parent would additionally
# expose them at a second URL for no reason.
ASSETS_DIR = STATIC_DIR / "assets"
if ASSETS_DIR.is_dir():
    app.mount("/assets", StaticFiles(directory=str(ASSETS_DIR)), name="assets")


@app.get("/", response_class=HTMLResponse, tags=["Studio UI"])
async def root_ui():
    """Interactive Ingestion Pipeline Testing Studio."""
    if INDEX_HTML.exists():
        return HTMLResponse(content=INDEX_HTML.read_text(encoding="utf-8"))
    return HTMLResponse(content="<h1>A-PAG AI Knowledge Base API Online</h1><p><a href='/docs'>View Swagger Docs</a></p>")


@app.get("/search", response_class=HTMLResponse, tags=["Search UI"])
async def search_ui():
    """Semantic search over the ingested corpus.

    Served as a static page rather than through a build step, matching the Studio UI: this is an
    internal tool for ~50 people, and a toolchain is a thing to maintain. It is a separate page
    from `/` because the Studio UI is about getting documents *in* and predates auth and
    multi-file upload (`KNOWN_DEBTS.md` #7); this one is about getting answers out.
    """
    if SEARCH_HTML.exists():
        return HTMLResponse(content=SEARCH_HTML.read_text(encoding="utf-8"))
    return HTMLResponse(content="<h1>Search UI not found</h1>", status_code=404)


@app.get("/health", tags=["System"])
async def health_check():
    """Health check probe endpoint."""
    return {"status": "healthy", "service": "apag-ai-knowledge-base"}
