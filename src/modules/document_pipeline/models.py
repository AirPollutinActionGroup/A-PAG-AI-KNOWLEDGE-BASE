"""Domain models, enums, and data contracts for the document ingestion pipeline."""

import uuid
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, Field

# Single source of truth for enums — imported from db layer
from src.db.enums import Classification, DocumentStatus


class UploadRequest(BaseModel):
    """Payload metadata for document upload."""

    classification: Classification = Classification.PUBLIC
    supersedes_doc_id: uuid.UUID | None = None
    keep_previous_version: bool = True
    owner_id: uuid.UUID | None = None


class UploadResponse(BaseModel):
    """Immediate 202 response contract returned to clients upon upload."""

    document_id: uuid.UUID
    filename: str
    status: DocumentStatus
    quarantine_key: str
    checksum: str | None = None
    rejection_reason: str | None = None
    was_duplicate: bool = False
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
    page_count: int = 0
    file_size_bytes: int = 0
    mime_type: str = ""
    rejection_reason: str | None = None
    scan_result: ScanResult | None = None


class Document(BaseModel):
    """Core domain representation of an ingested document."""

    id: uuid.UUID = Field(default_factory=uuid.uuid4)
    filename: str
    owner_id: uuid.UUID | None = None
    size: int
    checksum: str | None = None
    status: DocumentStatus = DocumentStatus.UPLOADED
    classification: Classification | None = None
    version: int = 1
    supersedes_id: uuid.UUID | None = None
    quarantine_path: str | None = None
    raw_path: str | None = None
    rejection_reason: str | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
