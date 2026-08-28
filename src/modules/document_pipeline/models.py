"""Domain models, enums, and data contracts for the document ingestion pipeline."""

import uuid
from datetime import UTC, datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class DocumentStatus(str, Enum):
    """Document lifecycle states."""

    UPLOADED = "UPLOADED"
    QUARANTINED = "QUARANTINED"
    VALIDATED = "VALIDATED"
    VALIDATION_FAILED = "VALIDATION_FAILED"
    REJECTED = "REJECTED"
    AWAITING_CLASSIFICATION = "AWAITING_CLASSIFICATION"
    DUPLICATE = "DUPLICATE"
    LIVE = "LIVE"
    SUPERSEDED = "SUPERSEDED"
    ARCHIVED = "ARCHIVED"


class Classification(str, Enum):
    """2-tier data access classification."""

    PUBLIC = "PUBLIC"
    RESTRICTED = "RESTRICTED"
    NULL = "NULL"


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
    classification: Classification = Classification.NULL
    version: int = 1
    supersedes_id: uuid.UUID | None = None
    quarantine_path: str | None = None
    raw_path: str | None = None
    rejection_reason: str | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
