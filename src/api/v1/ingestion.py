"""FastAPI Ingestion Endpoints (Stage 1 & Stage 3).
Handles document upload, quarantine validation, status queries, and versioning.
"""

import uuid

from fastapi import APIRouter, File, Form, HTTPException, UploadFile, status

from src.core.config import settings
from src.modules.document_pipeline.models import (
    Classification,
    UploadRequest,
    UploadResponse,
)
from src.modules.document_pipeline.repository import InMemoryDocumentRepository
from src.modules.document_pipeline.upload_service import UploadService
from src.storage.bucket_manager import BucketManager

router = APIRouter(prefix="/documents", tags=["Document Ingestion Pipeline"])

# Shared singleton services for API layer
_buckets = BucketManager(
    endpoint=settings.MINIO_ENDPOINT,
    access_key=settings.MINIO_ACCESS_KEY,
    secret_key=settings.MINIO_SECRET_KEY,
    secure=settings.MINIO_SECURE,
)
_repo = InMemoryDocumentRepository()
_upload_service = UploadService(bucket_manager=_buckets, repository=_repo)


@router.post(
    "/upload",
    response_model=UploadResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Upload PDF document to quarantine and process ingestion checks",
)
async def upload_document(
    file: UploadFile = File(..., description="PDF document binary stream"),
    classification: Classification = Form(
        Classification.PUBLIC,
        description="2-Tier security classification (PUBLIC / RESTRICTED)",
    ),
    supersedes_doc_id: uuid.UUID | None = Form(
        None,
        description="UUID of earlier document version if updating an existing policy",
    ),
    keep_previous_version: bool = Form(
        True,
        description="True to keep old version as SUPERSEDED; False to ARCHIVE",
    ),
):
    """Stage 1: Ingests PDF into quarantine, runs validation, and promotes or rejects."""
    # 1. MIME type validation check
    if file.content_type != "application/pdf":
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail=f"Invalid MIME type '{file.content_type}'. Only 'application/pdf' documents are accepted.",
        )

    # 2. Read contents
    content = await file.read()

    req_meta = UploadRequest(
        classification=classification,
        supersedes_doc_id=supersedes_doc_id,
        keep_previous_version=keep_previous_version,
    )

    # 3. Process through upload service
    result = _upload_service.upload(
        filename=file.filename or "unknown.pdf",
        data=content,
        request_meta=req_meta,
    )

    if result.status == "REJECTED":
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Validation failed: {result.rejection_reason}",
        )

    return result


@router.get(
    "/{document_id}/status",
    summary="Query document processing and validation status",
)
async def get_document_status(document_id: uuid.UUID):
    """Returns the current lifecycle status, storage location, and version details."""
    doc = _repo.get_by_id(document_id)
    if not doc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Document with ID {document_id} not found.",
        )
    return doc


@router.get(
    "",
    summary="List all ingested documents",
)
async def list_documents():
    """Lists all ingested documents recorded in the repository."""
    return _repo.get_all()
