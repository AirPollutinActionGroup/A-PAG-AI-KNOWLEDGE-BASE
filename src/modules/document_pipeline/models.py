"""Domain models, enums, and data contracts for the document ingestion pipeline."""

import uuid
from datetime import UTC, date, datetime
from typing import Any

from pydantic import BaseModel, Field

# Single source of truth for enums — imported from db layer
from src.db.enums import Classification, DocumentStatus


class UploadRequest(BaseModel):
    """Payload metadata for document upload."""

    # No default, deliberately. It defaulted to PUBLIC, which was survivable while nothing left
    # the deployment — but the Data Boundary Gateway decides what may be sent to an external
    # model by reading exactly this field, so a forgotten classification meant a confidential
    # document was *eligible to be sent to Sarvam*. The gateway was doing its job correctly on
    # a value nobody had actually chosen.
    classification: Classification
    supersedes_doc_id: uuid.UUID | None = None
    keep_previous_version: bool = True
    owner_id: uuid.UUID | None = None
    title: str | None = None
    description: str | None = None
    document_date: date | None = None
    upload_batch_id: uuid.UUID | None = None


class ReclassifyRequest(BaseModel):
    """Payload for changing a document's sensitivity tier after upload.

    `classification` has no default: this endpoint exists to state a new tier, and a caller who
    names none has asked for nothing. `reason` is optional but worth supplying — a tier change is
    the kind of thing someone asks about months later, and the audit row is where they will look.
    """

    classification: Classification
    reason: str | None = None


class UploadResponse(BaseModel):
    """Immediate 202 response contract returned to clients upon upload."""

    document_id: uuid.UUID
    filename: str
    status: DocumentStatus
    quarantine_key: str
    checksum: str | None = None
    rejection_reason: str | None = None
    was_duplicate: bool = False
    status_url: str | None = None
    correlation_id: uuid.UUID | None = None
    upload_batch_id: uuid.UUID | None = None
    message: str


class ScanResult(BaseModel):
    """Threat and structural scanner result."""

    passed: bool
    threats_detected: list[str] = Field(default_factory=list)
    details: dict[str, Any] = Field(default_factory=dict)


class ValidationResult(BaseModel):
    """Output of all fail-fast validation checks."""

    is_valid: bool
    sha256: str = ""
    # None where the format has no meaningful count — a .docx has no page count until it is
    # rendered, so there is nothing honest to store.
    page_count: int | None = None
    file_size_bytes: int = 0
    mime_type: str = ""
    rejection_reason: str | None = None
    scan_result: ScanResult | None = None


class Document(BaseModel):
    """Core domain representation of an ingested document."""

    id: uuid.UUID = Field(default_factory=uuid.uuid4)
    filename: str
    owner_id: uuid.UUID | None = None
    title: str | None = None
    description: str | None = None
    mime_type: str = "application/pdf"
    page_count: int | None = None
    # NATIVE / OCR / MIXED, set by the extraction stage. None until a document has been
    # extracted, and None on everything ingested before OCR existed — that is "not known"
    # rather than "read natively", which nobody measured for those.
    extraction_method: str | None = None
    upload_batch_id: uuid.UUID | None = None
    size: int
    checksum: str | None = None
    status: DocumentStatus = DocumentStatus.UPLOADED
    classification: Classification | None = None
    # The date on the document itself, not the upload date (`created_at`). Set at the
    # classification gate; None where the document carries no discoverable date.
    document_date: date | None = None
    version: int = 1
    supersedes_id: uuid.UUID | None = None
    quarantine_path: str | None = None
    raw_path: str | None = None
    rejection_reason: str | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    deleted_at: datetime | None = None
    purged_at: datetime | None = None
