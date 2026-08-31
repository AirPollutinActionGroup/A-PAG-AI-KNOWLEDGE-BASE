"""SQLAlchemy 2.0 ORM Models for Document Ingestion Pipeline."""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    Uuid,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from src.db.enums import Classification, DocumentStatus, JobStage, JobStatus

# JSON type that uses PostgreSQL JSONB in production and standard JSON in SQLite
JSONType = JSON().with_variant(JSONB, "postgresql")


class Base(DeclarativeBase):
    """Base class for all SQLAlchemy ORM models."""


class Document(Base):
    """Core document entity."""

    __tablename__ = "documents"

    document_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    filename: Mapped[str] = mapped_column(String(255), nullable=False)
    uploader_user_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), nullable=True
    )
    file_size: Mapped[int] = mapped_column(BigInteger, nullable=False)
    sha256: Mapped[str | None] = mapped_column(
        String(64), nullable=True, index=True
    )
    status: Mapped[str] = mapped_column(
        String(50),
        nullable=False,
        default=DocumentStatus.UPLOADED.value,
        server_default=DocumentStatus.UPLOADED.value,
        index=True,
    )
    classification: Mapped[str] = mapped_column(
        String(50),
        nullable=False,
        default=Classification.NULL.value,
        server_default=Classification.NULL.value,
    )

    # Versioning
    version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default="1"
    )
    supersedes_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("documents.document_id", ondelete="SET NULL"),
        nullable=True,
    )

    # Storage paths
    quarantine_path: Mapped[str | None] = mapped_column(String(500), nullable=True)
    raw_path: Mapped[str | None] = mapped_column(String(500), nullable=True)

    # Rejection & scan metadata
    rejection_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    scan_flags: Mapped[dict[str, Any] | None] = mapped_column(JSONType, nullable=True)

    # Timestamps (TIMESTAMPTZ)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=func.now(),
        server_default=func.now(),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=func.now(),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    # Relationships
    jobs: Mapped[list["Job"]] = relationship(
        "Job", back_populates="document", cascade="all, delete-orphan"
    )

    __table_args__ = (
        CheckConstraint(
            "status IN ('UPLOADED', 'QUARANTINED', 'VALIDATED', 'VALIDATION_FAILED', 'REJECTED', 'AWAITING_CLASSIFICATION', 'DUPLICATE', 'LIVE', 'SUPERSEDED', 'ARCHIVED')",
            name="chk_documents_status",
        ),
        CheckConstraint(
            "classification IN ('PUBLIC', 'RESTRICTED', 'NULL')",
            name="chk_documents_classification",
        ),
        Index(
            "uq_documents_active_sha256",
            "sha256",
            unique=True,
            postgresql_where=text("status NOT IN ('SUPERSEDED', 'ARCHIVED', 'REJECTED', 'DUPLICATE')"),
        ),
    )


class Job(Base):
    """Postgres-backed Job queue table for SKIP LOCKED asynchronous workers."""

    __tablename__ = "jobs"

    job_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    document_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("documents.document_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    stage: Mapped[str] = mapped_column(
        String(50),
        nullable=False,
        default=JobStage.SCAN.value,
        server_default=JobStage.SCAN.value,
    )
    status: Mapped[str] = mapped_column(
        String(50),
        nullable=False,
        default=JobStatus.PENDING.value,
        server_default=JobStatus.PENDING.value,
    )
    priority: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    retry_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    max_retries: Mapped[int] = mapped_column(
        Integer, nullable=False, default=3, server_default="3"
    )

    # Scheduling & Execution
    scheduled_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=func.now(),
        server_default=func.now(),
        nullable=False,
    )
    started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    finished_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    worker_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # Errors
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_details: Mapped[dict[str, Any] | None] = mapped_column(
        JSONType, nullable=True
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=func.now(),
        server_default=func.now(),
        nullable=False,
    )

    # Relationships
    document: Mapped["Document"] = relationship("Document", back_populates="jobs")

    __table_args__ = (
        CheckConstraint(
            "stage IN ('SCAN', 'EXTRACT', 'NORMALIZE')",
            name="chk_jobs_stage",
        ),
        CheckConstraint(
            "status IN ('PENDING', 'RUNNING', 'COMPLETED', 'FAILED')",
            name="chk_jobs_status",
        ),
        Index(
            "idx_jobs_pickup",
            "stage",
            "status",
            "scheduled_at",
            postgresql_where=text("status = 'PENDING'"),
        ),
    )


class AuditLog(Base):
    """Immutable audit trail for all document state transitions."""

    __tablename__ = "audit_log"

    event_id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True
    )
    document_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), nullable=False, index=True
    )
    user_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    event_type: Mapped[str] = mapped_column(
        String(100), nullable=False, index=True
    )
    event_time: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=func.now(),
        server_default=func.now(),
        nullable=False,
    )
    details: Mapped[dict[str, Any] | None] = mapped_column(JSONType, nullable=True)
    correlation_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), nullable=True
    )

    __table_args__ = (
        CheckConstraint(
            "event_type IN ('DOCUMENT_UPLOADED', 'DOCUMENT_QUARANTINED', 'VALIDATION_PASSED', 'VALIDATION_FAILED', 'DOCUMENT_PROMOTED', 'DOCUMENT_REJECTED', 'DOCUMENT_SUPERSEDED', 'DOCUMENT_ARCHIVED')",
            name="chk_audit_log_event_type",
        ),
    )
