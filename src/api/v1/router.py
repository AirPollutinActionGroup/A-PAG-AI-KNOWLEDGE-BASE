"""FastAPI Application router aggregation and UI delivery."""

from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import HTMLResponse

from src.api.v1.ingestion import router as ingestion_router

app = FastAPI(
    title="A-PAG AI Knowledge Base API",
    description="Clean, sovereign document ingestion pipeline for policy PDFs.",
    version="1.0.0",
)

# API v1 routes
app.include_router(ingestion_router, prefix="/api/v1")

STATIC_DIR = Path(__file__).resolve().parent.parent.parent / "static"
INDEX_HTML = STATIC_DIR / "index.html"


@app.get("/", response_class=HTMLResponse, tags=["Studio UI"])
async def root_ui():
    """Interactive Ingestion Pipeline Testing Studio."""
    if INDEX_HTML.exists():
        return HTMLResponse(content=INDEX_HTML.read_text(encoding="utf-8"))
    return HTMLResponse(content="<h1>A-PAG AI Knowledge Base API Online</h1><p><a href='/docs'>View Swagger Docs</a></p>")


@app.get("/health", tags=["System"])
async def health_check():
    """Health check probe endpoint."""
    return {"status": "healthy", "service": "apag-ai-knowledge-base"}
