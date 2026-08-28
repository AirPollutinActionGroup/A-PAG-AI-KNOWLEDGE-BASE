"""FastAPI Application router aggregation."""

from fastapi import FastAPI

from src.api.v1.ingestion import router as ingestion_router

app = FastAPI(
    title="A-PAG AI Knowledge Base API",
    description="Clean, sovereign document ingestion pipeline for policy PDFs.",
    version="1.0.0",
)

# API v1 routes
app.include_router(ingestion_router, prefix="/api/v1")


@app.get("/health", tags=["System"])
async def health_check():
    """Health check probe endpoint."""
    return {"status": "healthy", "service": "apag-ai-knowledge-base"}
