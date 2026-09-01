"""Make classification column nullable and update check constraint to PUBLIC/RESTRICTED.

Revision ID: 0005_fix_classification_nullable
Revises: 0004_add_partial_unique_index
Create Date: 2026-09-01 16:05:00.000000

"""
from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "0005_fix_classification_nullable"
down_revision: str | None = "0004_add_partial_unique_index"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # 1. Update any existing rows with 'NULL' string to true SQL NULL
    op.execute("UPDATE documents SET classification = NULL WHERE classification = 'NULL'")

    # 2. Drop old check constraint that allowed 'NULL' string
    op.drop_constraint("chk_documents_classification", "documents", type_="check")

    # 3. Alter classification column to be nullable with no default
    op.alter_column(
        "documents",
        "classification",
        existing_type=sa.String(length=50),
        nullable=True,
        server_default=None,
    )

    # 4. Create new check constraint allowing NULL or 'PUBLIC', 'RESTRICTED'
    op.create_check_constraint(
        "chk_documents_classification",
        "documents",
        "classification IS NULL OR classification IN ('PUBLIC', 'RESTRICTED')",
    )


def downgrade() -> None:
    op.drop_constraint("chk_documents_classification", "documents", type_="check")
    op.execute("UPDATE documents SET classification = 'NULL' WHERE classification IS NULL")
    op.alter_column(
        "documents",
        "classification",
        existing_type=sa.String(length=50),
        nullable=False,
        server_default="NULL",
    )
    op.create_check_constraint(
        "chk_documents_classification",
        "documents",
        "classification IN ('PUBLIC', 'RESTRICTED', 'NULL')",
    )
