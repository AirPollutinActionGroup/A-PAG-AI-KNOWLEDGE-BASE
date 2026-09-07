"""Owner-scoped RESTRICTED access; drop departments and doc_type.

Replaces the department-based RESTRICTED model with an owner-scoped one: a RESTRICTED
document is visible to its uploader and to ADMINs, nobody else. Departments were never
populated (0 rows, 0 users assigned, 0 documents assigned), which made RESTRICTED
effectively admin-only — including for the uploader — so this both simplifies the schema
and fixes that behavior.

`documents.doc_type` is dropped too: content category (POLICY/REPORT/…) cannot be derived
from a file, and the pipeline already reserves `AWAITING_CLASSIFICATION` for the future
automatic-classification stage that will populate it properly. Re-add it there, not here.

Revision id must stay <= 32 chars — `alembic_version.version_num` is VARCHAR(32).

Revision ID: 0007_owner_scoped_access
Revises: 0006_users_departments
"""

import sqlalchemy as sa
from alembic import op

revision = "0007_owner_scoped_access"
down_revision = "0006_users_departments"
branch_labels = None
depends_on = None


def _has_table(inspector, name: str) -> bool:
    return name in inspector.get_table_names()


def _has_column(inspector, table: str, column: str) -> bool:
    if not _has_table(inspector, table):
        return False
    return any(c["name"] == column for c in inspector.get_columns(table))


def _constraint_names(inspector, table: str) -> set[str]:
    if not _has_table(inspector, table):
        return set()
    names: set[str] = set()
    for c in inspector.get_check_constraints(table):
        if c.get("name"):
            names.add(c["name"])
    for fk in inspector.get_foreign_keys(table):
        if fk.get("name"):
            names.add(fk["name"])
    return names


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    # 1. documents.doc_type + its CHECK constraint. Postgres drops constraints that depend
    #    solely on a dropped column, so the explicit constraint drop is belt-and-braces for
    #    backends that don't (and is skipped when the constraint isn't there).
    if "chk_documents_doc_type" in _constraint_names(inspector, "documents"):
        op.drop_constraint("chk_documents_doc_type", "documents", type_="check")
    if _has_column(inspector, "documents", "doc_type"):
        op.drop_column("documents", "doc_type")

    # 2. documents.department_id — its FK (documents_department_id_fkey) goes with the column.
    if _has_column(inspector, "documents", "department_id"):
        op.drop_column("documents", "department_id")

    # 3. users.department_id — same, FK drops with the column.
    if _has_column(inspector, "users", "department_id"):
        op.drop_column("users", "department_id")

    # 4. departments table
    inspector = sa.inspect(bind)
    if _has_table(inspector, "departments"):
        op.drop_table("departments")


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if not _has_table(inspector, "departments"):
        op.create_table(
            "departments",
            sa.Column("department_id", sa.Uuid(as_uuid=True), primary_key=True),
            sa.Column("name", sa.String(120), nullable=False, unique=True),
            sa.Column("slug", sa.String(120), nullable=False, unique=True),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                server_default=sa.func.now(),
                nullable=False,
            ),
        )

    if not _has_column(inspector, "users", "department_id"):
        op.add_column("users", sa.Column("department_id", sa.Uuid(as_uuid=True), nullable=True))
        op.create_foreign_key(
            "fk_users_department",
            "users",
            "departments",
            ["department_id"],
            ["department_id"],
            ondelete="SET NULL",
        )

    if not _has_column(inspector, "documents", "department_id"):
        op.add_column("documents", sa.Column("department_id", sa.Uuid(as_uuid=True), nullable=True))
        op.create_foreign_key(
            "fk_documents_department",
            "documents",
            "departments",
            ["department_id"],
            ["department_id"],
            ondelete="SET NULL",
        )

    if not _has_column(inspector, "documents", "doc_type"):
        op.add_column("documents", sa.Column("doc_type", sa.String(30), nullable=True))
        op.create_check_constraint(
            "chk_documents_doc_type",
            "documents",
            "doc_type IS NULL OR doc_type IN ('POLICY', 'REPORT', 'DATASET', 'LEGAL', 'OTHER')",
        )
