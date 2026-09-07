"""FastAPI Ingestion Endpoints (Stage 1 & Stage 3).
Handles asynchronous document upload, quarantine validation, status queries, and versioning.
"""

import logging
import uuid

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    Request,
    UploadFile,
    status,
)
from fastapi.responses import Response
from slowapi import Limiter
from slowapi.util import get_remote_address
from sqlalchemy import desc
from sqlalchemy.orm import Session

from src.core.config import settings
from src.db.engine import get_db
from src.db.enums import AuditEventType, UserRole
from src.db.models import AuditLog as AuditORM
from src.db.models import User
from src.modules.audit.service import AuditService
from src.modules.auth.dependencies import get_current_user
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
limiter = Limiter(key_func=get_remote_address)

# Shared storage bucket manager singleton (backend resolved from settings.STORAGE_BACKEND)
_buckets = BucketManager()


def get_document_repository(db: Session = Depends(get_db)) -> DocumentRepository:
    """Dependency provider returning production PostgreSQL document repository."""
    return PostgreSQLDocumentRepository(db)


def get_upload_service(
    repo: DocumentRepository = Depends(get_document_repository),
    db: Session = Depends(get_db),
) -> UploadService:
    """Dependency provider returning UploadService with wired repo, storage, and audit session."""
    return UploadService(bucket_manager=_buckets, repository=repo, db_session=db)


def _can_view(doc, current_user: User) -> bool:
    """PUBLIC docs are visible org-wide; RESTRICTED docs are visible only to the uploader
    and to ADMINs. Deliberately a 2-tier, owner-scoped model (the Google-Drive
    "private to me" vs "anyone in the org" split) — see ARCHITECTURE.md §6b before adding
    department tiers or per-document ACLs; that's a Phase 6+ concern, not needed at
    50-person scale."""
    if doc.classification != Classification.RESTRICTED:
        return True
    if current_user.role == UserRole.ADMIN.value:
        return True
    return doc.owner_id is not None and doc.owner_id == current_user.user_id


def _uploader_emails(db: Session, docs) -> dict[uuid.UUID | None, str]:
    """Resolves uploader_user_id -> email for a page of documents in one query, so the API
    can show a human-readable uploader instead of a raw UUID."""
    owner_ids = {d.owner_id for d in docs if d.owner_id is not None}
    if not owner_ids:
        return {}
    rows = db.query(User.user_id, User.email).filter(User.user_id.in_(owner_ids)).all()
    return {row[0]: row[1] for row in rows}


def _with_uploader(doc, emails: dict[uuid.UUID | None, str]) -> dict:
    """Serializes a document DTO with `uploaded_by` (email) alongside the raw owner id."""
    payload = doc.model_dump()
    payload["uploaded_by"] = emails.get(doc.owner_id)
    return payload


@router.post(
    "/upload",
    response_model=list[UploadResponse],
    status_code=status.HTTP_202_ACCEPTED,
    summary="Upload one or more PDF documents to quarantine for asynchronous processing",
)
@limiter.limit(settings.UPLOAD_RATE_LIMIT)
async def upload_documents(
    request: Request,
    files: list[UploadFile] = File(..., description="One or more PDF document binary streams"),
    classification: Classification = Form(
        Classification.PUBLIC,
        description="2-Tier security classification (PUBLIC / RESTRICTED)",
    ),
    description: str | None = Form(
        None,
        description="Optional notes about the document — indexed for full-text search",
    ),
    supersedes_doc_id: uuid.UUID | None = Form(
        None,
        description="UUID of earlier document version if updating an existing policy (single-file only)",
    ),
    keep_previous_version: bool = Form(
        True,
        description="True to keep old version as SUPERSEDED; False to ARCHIVE",
    ),
    upload_service: UploadService = Depends(get_upload_service),
    current_user: User = Depends(get_current_user),
):
    """Stage 1: Fast-path asynchronous upload — accepts up to MAX_FILES_PER_UPLOAD PDFs,
    places each in quarantine, enqueues a SCAN job per file, and returns 202 with one
    UploadResponse per file. All files in one request share an upload_batch_id."""
    if not files:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="NO_FILES: At least one file is required.")

    if len(files) > settings.MAX_FILES_PER_UPLOAD:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"TOO_MANY_FILES: Maximum {settings.MAX_FILES_PER_UPLOAD} files per upload request.",
        )

    if len(files) > 1 and supersedes_doc_id is not None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="INVALID_REQUEST: supersedes_doc_id is only valid for a single-file upload.",
        )

    batch_id = uuid.uuid4()
    contents: list[tuple[UploadFile, bytes]] = []
    total_size = 0

    for file in files:
        if file.content_type != "application/pdf":
            raise HTTPException(
                status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
                detail=f"Invalid MIME type '{file.content_type}' for '{file.filename}'. Only 'application/pdf' documents are accepted.",
            )
        data = await file.read()
        if len(data) == 0:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"EMPTY_FILE: '{file.filename}' is 0 bytes.",
            )
        if len(data) > MAX_FILE_SIZE_BYTES:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"OVERSIZED_FILE: '{file.filename}' exceeds {MAX_FILE_SIZE_BYTES // (1024 * 1024)} MB.",
            )
        total_size += len(data)
        if total_size > settings.MAX_BATCH_SIZE_BYTES:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"BATCH_TOO_LARGE: Combined upload exceeds {settings.MAX_BATCH_SIZE_BYTES // (1024 * 1024)} MB.",
            )
        contents.append((file, data))

    results: list[UploadResponse] = []
    for file, data in contents:
        req_meta = UploadRequest(
            classification=classification,
            description=description,
            supersedes_doc_id=supersedes_doc_id,
            keep_previous_version=keep_previous_version,
            owner_id=current_user.user_id,
            upload_batch_id=batch_id,
        )
        result = upload_service.receive(
            filename=file.filename or "unknown.pdf",
            data=data,
            request_meta=req_meta,
        )
        results.append(result)

    return results


@router.get(
    "/{document_id}/status",
    summary="Query document processing status and latest lifecycle audit event",
)
async def get_document_status(
    document_id: uuid.UUID,
    repo: DocumentRepository = Depends(get_document_repository),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Returns the current lifecycle status, metadata, and latest audit trail event."""
    doc = repo.get_by_id(document_id)
    if not doc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Document with ID {document_id} not found.",
        )
    if not _can_view(doc, current_user):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Document with ID {document_id} not found.")

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
        "title": doc.title,
        "description": doc.description,
        "status": doc.status.value,
        "classification": doc.classification.value if doc.classification else None,
        "uploaded_by": _uploader_emails(db, [doc]).get(doc.owner_id),
        "version": doc.version,
        "supersedes_id": doc.supersedes_id,
        "rejection_reason": doc.rejection_reason,
        "raw_path": doc.raw_path,
        "upload_batch_id": doc.upload_batch_id,
        "created_at": doc.created_at.isoformat() if doc.created_at else None,
        "updated_at": doc.updated_at.isoformat() if doc.updated_at else None,
        "latest_event": latest_event,
    }


@router.get(
    "/{document_id}/download",
    summary="Download the promoted PDF content of a document",
)
async def download_document(
    document_id: uuid.UUID,
    repo: DocumentRepository = Depends(get_document_repository),
    current_user: User = Depends(get_current_user),
):
    """Streams the actual PDF bytes from the raw/ bucket. Only documents that have been
    promoted (raw_path set) have content to serve — quarantined/rejected documents don't."""
    doc = repo.get_by_id(document_id)
    if not doc or not _can_view(doc, current_user):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Document with ID {document_id} not found.")
    # 410 rather than 404: this document demonstrably existed and was deliberately destroyed.
    # A superseding version may still link back to it, so the distinction is useful.
    if doc.purged_at is not None:
        raise HTTPException(
            status_code=status.HTTP_410_GONE,
            detail="GONE: This document was permanently deleted and its content no longer exists.",
        )
    if not doc.raw_path:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"NOT_AVAILABLE: Document is '{doc.status.value}' and has no promoted content to download.",
        )
    data = _buckets.storage.get_object(bucket_name=_buckets.raw, object_name=doc.raw_path.split("/", 1)[-1])
    return Response(
        content=data,
        media_type="application/pdf",
        headers={"Content-Disposition": f'inline; filename="{doc.filename}"'},
    )


@router.delete(
    "/{document_id}",
    summary="Permanently delete a document and erase its bytes (owner or ADMIN only)",
)
async def delete_document(
    document_id: uuid.UUID,
    repo: DocumentRepository = Depends(get_document_repository),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Irreversible: erases the promoted object from `raw/`, then tombstones the row
    (deleted_at + purged_at stamped, sha256/raw_path nulled). The row itself is kept because
    supersedes chains and audit_log rows reference this document_id — a tombstone keeps that
    history resolvable, while the actual file content is gone for good.

    Order of operations is deliberate: audit first, then storage, then DB. If the storage
    delete fails the row is left un-tombstoned, so the document keeps showing as live and the
    delete can be retried — better than reporting success while bytes remain in the bucket.
    """
    doc = repo.get_by_id(document_id)
    if not doc or not _can_view(doc, current_user):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Document with ID {document_id} not found.")

    is_owner = doc.owner_id is not None and doc.owner_id == current_user.user_id
    if not (is_owner or current_user.role == UserRole.ADMIN.value):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="FORBIDDEN: Only the uploader or an ADMIN may delete this document.",
        )

    if doc.purged_at is not None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Document with ID {document_id} not found.")

    # Audit before destroying anything — this is the only surviving record of what the
    # erased bytes were, so it must not depend on the delete succeeding.
    try:
        AuditService.log_event(
            db=db,
            document_id=document_id,
            event_type=AuditEventType.DOCUMENT_DELETED,
            details={
                "deleted_by": str(current_user.user_id),
                "permanent": True,
                "erased_sha256": doc.checksum,
                "erased_filename": doc.filename,
            },
            user_id=str(current_user.user_id),
        )
    except Exception:
        logger.exception("AUDIT WRITE FAILED: doc_id=%s event=DOCUMENT_DELETED", document_id)

    for bucket, path in ((_buckets.raw, doc.raw_path), (_buckets.quarantine, doc.quarantine_path)):
        if not path:
            continue
        if not _buckets.storage.delete_object(bucket_name=bucket, object_name=path.split("/", 1)[-1]):
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="STORAGE_ERROR: Could not erase the stored file. Nothing was deleted — please retry.",
            )

    if not repo.purge(document_id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Document with ID {document_id} not found.")

    return {"document_id": document_id, "status": "DELETED", "permanent": True}


@router.get(
    "",
    summary="List ingested documents visible to the current user (paginated)",
)
async def list_documents(
    limit: int = 50,
    offset: int = 0,
    repo: DocumentRepository = Depends(get_document_repository),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Lists documents visible to the caller (PUBLIC + own/ADMIN RESTRICTED)."""
    limit = max(1, min(limit, 200))
    offset = max(0, offset)
    docs, total = repo.list_paginated(limit=limit, offset=offset)
    visible = [d for d in docs if _can_view(d, current_user)]
    emails = _uploader_emails(db, visible)
    return {
        "total": total,
        "limit": limit,
        "offset": offset,
        "documents": [_with_uploader(d, emails) for d in visible],
    }


@router.get(
    "/search",
    summary="Full-text search over document title, filename, and description",
)
async def search_documents(
    q: str,
    limit: int = 50,
    offset: int = 0,
    repo: DocumentRepository = Depends(get_document_repository),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if not q or not q.strip():
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="EMPTY_QUERY: q is required.")
    limit = max(1, min(limit, 200))
    offset = max(0, offset)
    docs, total = repo.search(q.strip(), limit=limit, offset=offset)
    visible = [d for d in docs if _can_view(d, current_user)]
    emails = _uploader_emails(db, visible)
    return {
        "total": total,
        "limit": limit,
        "offset": offset,
        "query": q,
        "documents": [_with_uploader(d, emails) for d in visible],
    }


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
