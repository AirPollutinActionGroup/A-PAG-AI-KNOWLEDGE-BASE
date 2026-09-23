"""Database Enums for status, stage, and event tracking."""

from enum import Enum


class DocumentStatus(str, Enum):
    """Document lifecycle states.

    VALIDATED means "promoted to raw/, EXTRACT job pending" — set by ScanJobHandler on
    successful promotion. AWAITING_CLASSIFICATION means normalization succeeded (set by
    NormalizationJobHandler), not promotion — the pipeline runs
    QUARANTINED -> VALIDATED -> EXTRACTED -> AWAITING_CLASSIFICATION, with EXTRACTION_FAILED /
    NORMALIZATION_FAILED as the failure branches of the two middle stages.

    There is no separate classification state: a document's sensitivity tier is chosen by the
    uploader at upload time (defaulting to PUBLIC) and can be changed afterwards through the
    reclassification endpoint, so nothing in the pipeline ever waits on a human decision.
    """

    UPLOADED = "UPLOADED"
    QUARANTINED = "QUARANTINED"
    VALIDATED = "VALIDATED"
    VALIDATION_FAILED = "VALIDATION_FAILED"
    REJECTED = "REJECTED"
    EXTRACTED = "EXTRACTED"
    EXTRACTION_FAILED = "EXTRACTION_FAILED"
    NORMALIZATION_FAILED = "NORMALIZATION_FAILED"
    AWAITING_CLASSIFICATION = "AWAITING_CLASSIFICATION"
    DUPLICATE = "DUPLICATE"
    LIVE = "LIVE"
    SUPERSEDED = "SUPERSEDED"
    ARCHIVED = "ARCHIVED"


class Classification(str, Enum):
    """2-tier data access classification. PUBLIC = any authenticated employee;
    RESTRICTED = uploader (owner-scoped) + ADMIN role only."""

    PUBLIC = "PUBLIC"
    RESTRICTED = "RESTRICTED"


class UserRole(str, Enum):
    """Application-wide role. Deliberately 2-tier: ADMIN (sees/deletes every document,
    RESTRICTED included) vs USER (everyone else — upload, search, and manage only their own
    documents). A prior CONTRIBUTOR/VIEWER split existed but was never actually enforced
    anywhere in the code, so it was collapsed — see KNOWN_DEBTS.md."""

    ADMIN = "ADMIN"
    USER = "USER"


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
    DOCUMENT_DELETED = "DOCUMENT_DELETED"
    EXTRACTION_COMPLETED = "EXTRACTION_COMPLETED"
    EXTRACTION_FAILED = "EXTRACTION_FAILED"
    NORMALIZATION_COMPLETED = "NORMALIZATION_COMPLETED"
    NORMALIZATION_FAILED = "NORMALIZATION_FAILED"
    # The initial tier is recorded in DOCUMENT_QUARANTINED's details at upload; this marks a
    # later change to it, which is the event worth being able to find on its own.
    DOCUMENT_RECLASSIFIED = "DOCUMENT_RECLASSIFIED"
