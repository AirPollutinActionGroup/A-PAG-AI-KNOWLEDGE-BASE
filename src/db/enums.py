"""Database Enums for status, stage, and event tracking."""

from enum import Enum


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


class JobStage(str, Enum):
    """Pipeline job processing stages."""

    SCAN = "SCAN"
    EXTRACT = "EXTRACT"
    NORMALIZE = "NORMALIZE"


class JobStatus(str, Enum):
    """Pipeline job execution states."""

    PENDING = "PENDING"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


class AuditEventType(str, Enum):
    """Immutable audit trail event types."""

    DOCUMENT_UPLOADED = "DOCUMENT_UPLOADED"
    DOCUMENT_QUARANTINED = "DOCUMENT_QUARANTINED"
    VALIDATION_PASSED = "VALIDATION_PASSED"
    VALIDATION_FAILED = "VALIDATION_FAILED"
    DOCUMENT_PROMOTED = "DOCUMENT_PROMOTED"
    DOCUMENT_REJECTED = "DOCUMENT_REJECTED"
    DOCUMENT_SUPERSEDED = "DOCUMENT_SUPERSEDED"
    DOCUMENT_ARCHIVED = "DOCUMENT_ARCHIVED"
