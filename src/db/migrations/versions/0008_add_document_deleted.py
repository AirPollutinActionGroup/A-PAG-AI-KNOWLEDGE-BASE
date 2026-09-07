"""Add DOCUMENT_DELETED audit event type, for the new soft-delete endpoint.

`documents.deleted_at` already existed (migration 0006) but nothing set it — this is the
migration that makes the column load-bearing. Soft-delete only sets `deleted_at`; existing
`list_paginated`/`search` queries already filter `WHERE deleted_at IS NULL`, so a deleted
document simply stops appearing without touching any other query path.

Revision ID: 0008_add_document_deleted
Revises: 0007_owner_scoped_access
"""

import sqlalchemy as sa
from alembic import op

revision = "0008_add_document_deleted"
down_revision = "0007_owner_scoped_access"
branch_labels = None
depends_on = None

OLD_VALUES = (
    "DOCUMENT_UPLOADED", "DOCUMENT_QUARANTINED", "VALIDATION_PASSED", "VALIDATION_FAILED",
    "DOCUMENT_PROMOTED", "DOCUMENT_REJECTED", "DOCUMENT_SUPERSEDED", "DOCUMENT_ARCHIVED",
)
NEW_VALUES = OLD_VALUES + ("DOCUMENT_DELETED",)


def _constraint_exists(inspector, table: str, name: str) -> bool:
    return any(c.get("name") == name for c in inspector.get_check_constraints(table))


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if _constraint_exists(inspector, "audit_log", "chk_audit_log_event_type"):
        op.drop_constraint("chk_audit_log_event_type", "audit_log", type_="check")
    values = ", ".join(f"'{v}'" for v in NEW_VALUES)
    op.create_check_constraint(
        "chk_audit_log_event_type", "audit_log", f"event_type IN ({values})",
    )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if _constraint_exists(inspector, "audit_log", "chk_audit_log_event_type"):
        op.drop_constraint("chk_audit_log_event_type", "audit_log", type_="check")
    values = ", ".join(f"'{v}'" for v in OLD_VALUES)
    op.create_check_constraint(
        "chk_audit_log_event_type", "audit_log", f"event_type IN ({values})",
    )
