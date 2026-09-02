"""FastAPI Ingestion Endpoints (Stage 1 & Stage 3).
Handles asynchronous document upload, quarantine validation, status queries, and versioning.
"""

import logging
import uuid
from typing import Any

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile, status
from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from src.core.config import settings
from src.db.engine import get_db
from src.db.models import AuditLog as AuditORM
from src.modules.document_pipeline.models import (
    Classification,
    UploadRequest,
    UploadResponse,
)
from src.modules.document_pipeline.repository import (
    DocumentRepository,
    PostgreSQLDocumentRepository,
)
from src.modules.document_pipeline.upload_service import (
    MAX_FILE_SIZE_BYTES,
    UploadService,
)
from src.storage.bucket_manager import BucketManager

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/documents", tags=["Document Ingestion Pipeline"])

# Shared storage bucket manager singleton
_buckets = BucketManager(
    endpoint=settings.MINIO_ENDPOINT,
    access_key=settings.MINIO_ACCESS_KEY,
    secret_key=settings.MINIO_SECRET_KEY,
    secure=settings.MINIO_SECURE,
)


def get_document_repository(db: Session = Depends(get_db)) -> DocumentRepository:
    """Dependency provider returning production PostgreSQL document repository."""
    return PostgreSQLDocumentRepository(db)


def get_upload_service(
    repo: DocumentRepository = Depends(get_document_repository),
    db: Session = Depends(get_db),
) -> UploadService:
    """Dependency provider returning UploadService with wired repo, storage, and audit session."""
    return UploadService(bucket_manager=_buckets, repository=repo, db_session=db)


@router.post(
    "/upload",
    response_model=UploadResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Upload PDF document to quarantine for asynchronous processing",
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
    upload_service: UploadService = Depends(get_upload_service),
):
    """Stage 1: Fast-path asynchronous upload — accepts PDF, places in quarantine, enqueues SCAN job, and returns 202."""
    # 1. MIME type validation check
    if file.content_type != "application/pdf":
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail=f"Invalid MIME type '{file.content_type}'. Only 'application/pdf' documents are accepted.",
        )

    # 2. Read contents
    content = await file.read()

    # 3. Fast-path size checks
    if len(content) == 0:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="EMPTY_FILE: Document payload is 0 bytes.",
        )

    if len(content) > MAX_FILE_SIZE_BYTES:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"OVERSIZED_FILE: Document exceeds maximum limit of {MAX_FILE_SIZE_BYTES // (1024 * 1024)} MB.",
        )

    req_meta = UploadRequest(
        classification=classification,
        supersedes_doc_id=supersedes_doc_id,
        keep_previous_version=keep_previous_version,
    )

    # 4. Fast-path quarantine landing + job enqueue
    result = upload_service.receive(
        filename=file.filename or "unknown.pdf",
        data=content,
        request_meta=req_meta,
    )

    return result


@router.get(
    "/{document_id}/status",
    summary="Query document processing status and latest lifecycle audit event",
)
async def get_document_status(
    document_id: uuid.UUID,
    repo: DocumentRepository = Depends(get_document_repository),
    db: Session = Depends(get_db),
):
    """Returns the current lifecycle status, metadata, and latest audit trail event."""
    doc = repo.get_by_id(document_id)
    if not doc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Document with ID {document_id} not found.",
        )

    # Fetch latest audit log event
    latest_event = None
    try:
        audit_entry = (
            db.query(AuditORM)
            .filter(AuditORM.document_id == document_id)
            .order_by(desc(AuditORM.event_time))
            .first()
        )
        if audit_entry:
            latest_event = {
                "event_type": audit_entry.event_type,
                "event_time": audit_entry.event_time.isoformat() if audit_entry.event_time else None,
                "details": audit_entry.details,
            }
    except Exception as e:
        logger.debug("Could not query audit log for status: %s", e)

    return {
        "document_id": doc.id,
        "filename": doc.filename,
        "status": doc.status.value,
        "classification": doc.classification.value if doc.classification else None,
        "version": doc.version,
        "rejection_reason": doc.rejection_reason,
        "raw_path": doc.raw_path,
        "created_at": doc.created_at.isoformat() if doc.created_at else None,
        "updated_at": doc.updated_at.isoformat() if doc.updated_at else None,
        "latest_event": latest_event,
    }


@router.get(
    "",
    summary="List all ingested documents",
)
async def list_documents(
    repo: DocumentRepository = Depends(get_document_repository),
):
    """Lists all ingested documents recorded in the repository."""
    return repo.get_all()


@router.get(
    "/test-preset/{preset_name}",
    summary="Get binary fixture for testing presets",
)
async def get_test_preset(preset_name: str):
    """Returns actual binary test PDF fixtures for the interactive studio."""
    from pathlib import Path
    from fastapi.responses import Response

    fixtures_dir = Path(__file__).resolve().parent.parent.parent.parent / "tests" / "fixtures" / "pdfs"

    fixture_map = {
        "valid_v1": ("01_standard_digital_policy.pdf", "caqm_directive_2026_v1.pdf"),
        "corrupt_header": ("05_corrupted_header_missing.pdf", "broken_header.pdf"),
        "truncated_eof": ("06_truncated_eof_missing.pdf", "truncated_stream.pdf"),
        "fake_exe": ("07_disguised_fake_binary.pdf", "disguised_malware.pdf"),
        "malicious": ("08_malicious_script_exploit.pdf", "threat_exploit_sample.pdf"),
        "encrypted": ("10_password_protected.pdf", "password_protected_confidential.pdf"),
    }

    if preset_name not in fixture_map:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Test preset '{preset_name}' not found.",
        )

    file_name, download_name = fixture_map[preset_name]
    file_path = fixtures_dir / file_name

    if not file_path.exists():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Fixture file '{file_name}' not found on disk.",
        )

    return Response(
        content=file_path.read_bytes(),
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{download_name}"'},
    )
