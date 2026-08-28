"""Revoke UPDATE and DELETE on audit_log to enforce append-only immutability.

Revision ID: 0003_revoke_audit_log_writes
Revises: 0002_add_check_constraints
Create Date: 2026-08-28 16:22:00.000000

"""
from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0003_revoke_audit_log_writes"
down_revision: str | None = "0002_add_check_constraints"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Revoke UPDATE and DELETE permissions on the audit_log table to guarantee immutability
    op.execute("REVOKE UPDATE, DELETE ON audit_log FROM PUBLIC")


def downgrade() -> None:
    # Restore permissions on rollback
    op.execute("GRANT UPDATE, DELETE ON audit_log TO PUBLIC")
