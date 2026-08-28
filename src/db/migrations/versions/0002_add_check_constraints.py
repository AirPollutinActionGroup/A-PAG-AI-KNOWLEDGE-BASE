"""Add check constraints for documents, jobs, and audit_log enums.

Revision ID: 0002_add_check_constraints
Revises: 0001_initial_schema
Create Date: 2026-08-28 16:20:00.000000

"""
from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0002_add_check_constraints"
down_revision: str | None = "0001_initial_schema"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # 1. Documents status & classification check constraints
    op.create_check_constraint(
        "chk_documents_status",
        "documents",
        "status IN ('UPLOADED', 'QUARANTINED', 'VALIDATED', 'VALIDATION_FAILED', 'REJECTED', 'AWAITING_CLASSIFICATION', 'DUPLICATE', 'LIVE', 'SUPERSEDED', 'ARCHIVED')",
    )
    op.create_check_constraint(
        "chk_documents_classification",
        "documents",
        "classification IN ('PUBLIC', 'RESTRICTED', 'NULL')",
    )

    # 2. Jobs stage & status check constraints
    op.create_check_constraint(
        "chk_jobs_stage",
        "jobs",
        "stage IN ('SCAN', 'EXTRACT', 'NORMALIZE')",
    )
    op.create_check_constraint(
        "chk_jobs_status",
        "jobs",
        "status IN ('PENDING', 'RUNNING', 'COMPLETED', 'FAILED')",
    )

    # 3. Audit log event_type check constraint
    op.create_check_constraint(
        "chk_audit_log_event_type",
        "audit_log",
        "event_type IN ('DOCUMENT_UPLOADED', 'DOCUMENT_QUARANTINED', 'VALIDATION_PASSED', 'VALIDATION_FAILED', 'DOCUMENT_PROMOTED', 'DOCUMENT_REJECTED', 'DOCUMENT_SUPERSEDED', 'DOCUMENT_ARCHIVED')",
    )


def downgrade() -> None:
    op.drop_constraint("chk_audit_log_event_type", "audit_log", type_="check")
    op.drop_constraint("chk_jobs_status", "jobs", type_="check")
    op.drop_constraint("chk_jobs_stage", "jobs", type_="check")
    op.drop_constraint("chk_documents_classification", "documents", type_="check")
    op.drop_constraint("chk_documents_status", "documents", type_="check")
