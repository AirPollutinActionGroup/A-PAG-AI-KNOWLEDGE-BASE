"""Add EXTRACTED/EXTRACTION_FAILED/NORMALIZATION_FAILED statuses and their audit event types.

Phase 4 (Extraction & Normalization) inserts two new stages between promotion and
classification. `AWAITING_CLASSIFICATION` previously got set by `ScanJobHandler` right at
promotion — a placeholder for these two stages not existing yet. It now means "normalization
succeeded", set by the new `NormalizationJobHandler`. The existing, previously-unused
`VALIDATED` status (defined since migration 0002, never assigned by any code) is repurposed to
mean "promoted to raw/, EXTRACT job pending" — the pipeline is now
QUARANTINED -> VALIDATED -> EXTRACTED -> AWAITING_CLASSIFICATION, with EXTRACTION_FAILED /
NORMALIZATION_FAILED as the two new failure branches.

Revision ID: 0011_extract_normalize_statuses
Revises: 0010_permanent_delete

Note the deliberately abbreviated revision id: `alembic_version.version_num` is VARCHAR(32), so
a longer, more descriptive id fails the final version-bump statement and rolls back the whole
migration (see CLAUDE.md).
"""

import sqlalchemy as sa
from alembic import op

revision = "0011_extract_normalize_statuses"
down_revision = "0010_permanent_delete"
branch_labels = None
depends_on = None

OLD_STATUS_VALUES = (
    "UPLOADED", "QUARANTINED", "VALIDATED", "VALIDATION_FAILED", "REJECTED",
    "AWAITING_CLASSIFICATION", "DUPLICATE", "LIVE", "SUPERSEDED", "ARCHIVED",
)
NEW_STATUS_VALUES = (
    "UPLOADED", "QUARANTINED", "VALIDATED", "VALIDATION_FAILED", "REJECTED",
    "EXTRACTED", "EXTRACTION_FAILED", "NORMALIZATION_FAILED",
    "AWAITING_CLASSIFICATION", "DUPLICATE", "LIVE", "SUPERSEDED", "ARCHIVED",
)

OLD_EVENT_VALUES = (
    "DOCUMENT_UPLOADED", "DOCUMENT_QUARANTINED", "VALIDATION_PASSED", "VALIDATION_FAILED",
    "DOCUMENT_PROMOTED", "DOCUMENT_REJECTED", "DOCUMENT_SUPERSEDED", "DOCUMENT_ARCHIVED",
    "DOCUMENT_DELETED",
)
NEW_EVENT_VALUES = OLD_EVENT_VALUES + (
    "EXTRACTION_COMPLETED", "EXTRACTION_FAILED", "NORMALIZATION_COMPLETED", "NORMALIZATION_FAILED",
)


def _constraint_exists(inspector, table: str, name: str) -> bool:
    return any(c.get("name") == name for c in inspector.get_check_constraints(table))


def _set_status_constraint(inspector, values: tuple[str, ...]) -> None:
    if _constraint_exists(inspector, "documents", "chk_documents_status"):
        op.drop_constraint("chk_documents_status", "documents", type_="check")
    rendered = ", ".join(f"'{v}'" for v in values)
    op.create_check_constraint("chk_documents_status", "documents", f"status IN ({rendered})")


def _set_event_type_constraint(inspector, values: tuple[str, ...]) -> None:
    if _constraint_exists(inspector, "audit_log", "chk_audit_log_event_type"):
        op.drop_constraint("chk_audit_log_event_type", "audit_log", type_="check")
    rendered = ", ".join(f"'{v}'" for v in values)
    op.create_check_constraint(
        "chk_audit_log_event_type", "audit_log", f"event_type IN ({rendered})"
    )


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    _set_status_constraint(inspector, NEW_STATUS_VALUES)
    _set_event_type_constraint(inspector, NEW_EVENT_VALUES)


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    _set_status_constraint(inspector, OLD_STATUS_VALUES)
    _set_event_type_constraint(inspector, OLD_EVENT_VALUES)
