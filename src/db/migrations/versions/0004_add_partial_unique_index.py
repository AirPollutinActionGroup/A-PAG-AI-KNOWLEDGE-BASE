"""Add partial unique index on documents sha256 for active deduplication race protection.

Revision ID: 0004_add_partial_unique_index
Revises: 0003_revoke_audit_log_writes
Create Date: 2026-09-01 16:00:00.000000

"""
from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "0004_add_partial_unique_index"
down_revision: str | None = "0003_revoke_audit_log_writes"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Create partial unique index to guarantee race-safe deduplication at database level
    op.create_index(
        "uq_documents_active_sha256",
        "documents",
        ["sha256"],
        unique=True,
        postgresql_where=sa.text(
            "status NOT IN ('SUPERSEDED', 'ARCHIVED', 'REJECTED', 'DUPLICATE') AND sha256 IS NOT NULL"
        ),
    )


def downgrade() -> None:
    op.drop_index("uq_documents_active_sha256", table_name="documents")
