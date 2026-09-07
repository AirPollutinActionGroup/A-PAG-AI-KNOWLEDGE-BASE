"""Add users, departments, document ownership/metadata columns, and full-text search.

Revision ID: 0006_users_departments
Revises: 0005_fix_classification_nullable
Create Date: 2026-09-02 12:00:00.000000

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import TSVECTOR

# revision identifiers, used by Alembic.
# NOTE: alembic_version.version_num defaults to VARCHAR(32) — keep every revision id <=32 chars
# (this one already renamed once after hitting that exact truncation error mid-deploy).
revision: str = "0006_users_departments"
down_revision: str | None = "0005_fix_classification_nullable"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing_tables = set(inspector.get_table_names())
    # Guards against a case seen in dev: integration test fixtures call
    # Base.metadata.create_all() (SQLAlchemy, not Alembic) against a schema that predates this
    # migration's ORM changes — which happily creates the *new* tables (departments/users) since
    # they don't exist yet, but can't retrofit new *columns* onto the already-existing
    # `documents` table. Running this migration afterwards would then hit "table already exists"
    # on departments/users. Checking first makes upgrade() safe to run in both orders.

    # 1. departments
    if "departments" not in existing_tables:
        op.create_table(
            "departments",
            sa.Column("department_id", sa.Uuid(as_uuid=True), primary_key=True),
            sa.Column("name", sa.String(120), nullable=False, unique=True),
            sa.Column("slug", sa.String(120), nullable=False, unique=True),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        )

    # 2. users
    if "users" not in existing_tables:
        op.create_table(
            "users",
            sa.Column("user_id", sa.Uuid(as_uuid=True), primary_key=True),
            # Uniqueness is carried by the unique index created below, not by a column-level
            # `unique=True` — that would emit a separate UNIQUE constraint (users_email_key)
            # *in addition to* ix_users_email, giving a freshly-migrated database two objects
            # where models.py (`unique=True, index=True`) declares one. That drift made
            # `alembic check` fail on a fresh database while passing on a dev database built
            # by Base.metadata.create_all().
            sa.Column("email", sa.String(255), nullable=False),
            sa.Column("full_name", sa.String(255), nullable=False),
            sa.Column("hashed_password", sa.String(255), nullable=False),
            sa.Column(
                "department_id",
                sa.Uuid(as_uuid=True),
                sa.ForeignKey("departments.department_id", ondelete="SET NULL"),
                nullable=True,
            ),
            sa.Column("role", sa.String(20), nullable=False, server_default="VIEWER"),
            sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
            sa.Column("last_login_at", sa.DateTime(timezone=True), nullable=True),
            sa.CheckConstraint("role IN ('ADMIN', 'CONTRIBUTOR', 'VIEWER')", name="chk_users_role"),
        )
        op.create_index("ix_users_email", "users", ["email"], unique=True)

    # 3. documents: ownership + descriptive metadata
    doc_columns = {c["name"] for c in inspector.get_columns("documents")}

    def _add_column_if_missing(name: str, column: sa.Column) -> None:
        if name not in doc_columns:
            op.add_column("documents", column)

    _add_column_if_missing(
        "department_id",
        sa.Column(
            "department_id",
            sa.Uuid(as_uuid=True),
            sa.ForeignKey("departments.department_id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    _add_column_if_missing("title", sa.Column("title", sa.String(500), nullable=True))
    _add_column_if_missing("description", sa.Column("description", sa.Text(), nullable=True))
    _add_column_if_missing("doc_type", sa.Column("doc_type", sa.String(30), nullable=True))
    _add_column_if_missing(
        "mime_type",
        sa.Column("mime_type", sa.String(100), nullable=False, server_default="application/pdf"),
    )
    _add_column_if_missing("page_count", sa.Column("page_count", sa.Integer(), nullable=True))
    _add_column_if_missing("upload_batch_id", sa.Column("upload_batch_id", sa.Uuid(as_uuid=True), nullable=True))
    _add_column_if_missing("deleted_at", sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True))

    existing_indexes = {ix["name"] for ix in inspector.get_indexes("documents")}
    if "ix_documents_upload_batch_id" not in existing_indexes:
        op.create_index("ix_documents_upload_batch_id", "documents", ["upload_batch_id"])

    existing_checks = {c["name"] for c in inspector.get_check_constraints("documents")}
    if "chk_documents_doc_type" not in existing_checks:
        op.create_check_constraint(
            "chk_documents_doc_type",
            "documents",
            "doc_type IS NULL OR doc_type IN ('POLICY', 'REPORT', 'DATASET', 'LEGAL', 'OTHER')",
        )

    # 4. Real FK on uploader_user_id, now that `users` exists.
    existing_fks = {fk["name"] for fk in inspector.get_foreign_keys("documents")}
    if "fk_documents_uploader_user_id" not in existing_fks:
        op.create_foreign_key(
            "fk_documents_uploader_user_id",
            "documents",
            "users",
            ["uploader_user_id"],
            ["user_id"],
            ondelete="SET NULL",
        )

    # 5. Full-text search: generated tsvector kept in sync by trigger (works for ORM writes
    # and any raw SQL/psql edits alike — a SQLAlchemy-only computed column would not).
    _add_column_if_missing("search_vector", sa.Column("search_vector", TSVECTOR(), nullable=True))
    op.execute(
        r"""
        CREATE OR REPLACE FUNCTION documents_search_vector_update() RETURNS trigger AS $$
        BEGIN
            -- Postgres's default text-search parser does not split on '_'/'-' (a filename like
            -- 'caqm_directive_2026.pdf' indexes as one opaque token), so replace those with
            -- spaces before tokenizing filenames so individual words become searchable.
            NEW.search_vector :=
                setweight(to_tsvector('english', coalesce(NEW.title, '')), 'A') ||
                setweight(to_tsvector('english', regexp_replace(coalesce(NEW.filename, ''), '[_\-.]', ' ', 'g')), 'B') ||
                setweight(to_tsvector('english', coalesce(NEW.description, '')), 'C');
            RETURN NEW;
        END
        $$ LANGUAGE plpgsql;
        """
    )
    op.execute("DROP TRIGGER IF EXISTS trg_documents_search_vector_update ON documents")
    op.execute(
        """
        CREATE TRIGGER trg_documents_search_vector_update
        BEFORE INSERT OR UPDATE OF title, filename, description ON documents
        FOR EACH ROW EXECUTE FUNCTION documents_search_vector_update();
        """
    )
    op.execute("UPDATE documents SET title = title")  # backfill existing rows via the trigger

    existing_indexes = {ix["name"] for ix in inspector.get_indexes("documents")}
    if "idx_documents_search_vector" not in existing_indexes:
        op.create_index(
            "idx_documents_search_vector", "documents", ["search_vector"], postgresql_using="gin"
        )


def downgrade() -> None:
    op.drop_index("idx_documents_search_vector", table_name="documents")
    op.execute("DROP TRIGGER IF EXISTS trg_documents_search_vector_update ON documents")
    op.execute("DROP FUNCTION IF EXISTS documents_search_vector_update")
    op.drop_column("documents", "search_vector")

    op.drop_constraint("fk_documents_uploader_user_id", "documents", type_="foreignkey")

    op.drop_constraint("chk_documents_doc_type", "documents", type_="check")
    op.drop_index("ix_documents_upload_batch_id", table_name="documents")
    op.drop_column("documents", "deleted_at")
    op.drop_column("documents", "upload_batch_id")
    op.drop_column("documents", "page_count")
    op.drop_column("documents", "mime_type")
    op.drop_column("documents", "doc_type")
    op.drop_column("documents", "description")
    op.drop_column("documents", "title")
    op.drop_column("documents", "department_id")

    op.drop_index("ix_users_email", table_name="users")
    op.drop_table("users")
    op.drop_table("departments")
