"""Collapse UserRole from ADMIN/CONTRIBUTOR/VIEWER to ADMIN/USER.

CONTRIBUTOR and VIEWER were never actually distinguished anywhere in the application code —
every permission check in the codebase only ever tested `role == ADMIN`. Keeping three roles
implied a permission model that didn't exist, so this collapses the unused two-way split
into a single USER role. Existing CONTRIBUTOR/VIEWER rows are converted to USER; ADMIN rows
are untouched.

Revision ID: 0009_collapse_user_roles
Revises: 0008_add_document_deleted
"""

import sqlalchemy as sa
from alembic import op

revision = "0009_collapse_user_roles"
down_revision = "0008_add_document_deleted"
branch_labels = None
depends_on = None


def _constraint_exists(inspector, table: str, name: str) -> bool:
    return any(c.get("name") == name for c in inspector.get_check_constraints(table))


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if _constraint_exists(inspector, "users", "chk_users_role"):
        op.drop_constraint("chk_users_role", "users", type_="check")

    op.execute("UPDATE users SET role = 'USER' WHERE role IN ('CONTRIBUTOR', 'VIEWER')")
    op.alter_column("users", "role", server_default="USER")

    op.create_check_constraint("chk_users_role", "users", "role IN ('ADMIN', 'USER')")


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if _constraint_exists(inspector, "users", "chk_users_role"):
        op.drop_constraint("chk_users_role", "users", type_="check")

    # Data is not reversible (CONTRIBUTOR vs VIEWER distinction is lost) — every non-admin
    # row becomes VIEWER, the prior default.
    op.execute("UPDATE users SET role = 'VIEWER' WHERE role = 'USER'")
    op.alter_column("users", "role", server_default="VIEWER")

    op.create_check_constraint(
        "chk_users_role", "users", "role IN ('ADMIN', 'CONTRIBUTOR', 'VIEWER')"
    )
