"""Add documents.purged_at for permanent (bytes-erased) deletion.

DELETE was previously a soft-delete: it set `deleted_at`, hid the row from list/search, and
left the promoted object sitting in the `raw/` bucket forever. That was misleading — the UI
told users "this can't be undone" while the file was still fully intact in object storage,
and there was no path at all for actually reclaiming the bytes.

DELETE is now permanent: the object is removed from `raw/`, `purged_at` is stamped, and
`sha256`/`raw_path` are nulled. Nulling sha256 is load-bearing, not cosmetic — the dedup
index `uq_documents_active_sha256` is partial on `sha256 IS NOT NULL`, so clearing it is what
allows the same file to be re-uploaded after an erase (otherwise the re-upload would be
flagged DUPLICATE against a document whose bytes no longer exist). The erased hash is
preserved in the append-only audit_log `details` payload instead, so the historical record of
what was destroyed survives even though the row no longer carries it.

The row itself is kept as a tombstone rather than deleted: `supersedes_id` chains from newer
versions point at it, and audit_log rows reference its document_id (no FK, but the history is
meaningless if the id resolves to nothing).

Revision ID: 0010_permanent_delete
Revises: 0009_collapse_user_roles
"""

import sqlalchemy as sa
from alembic import op

revision = "0010_permanent_delete"
down_revision = "0009_collapse_user_roles"
branch_labels = None
depends_on = None


def _column_exists(inspector, table: str, name: str) -> bool:
    return any(c["name"] == name for c in inspector.get_columns(table))


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if not _column_exists(inspector, "documents", "purged_at"):
        op.add_column(
            "documents",
            sa.Column("purged_at", sa.DateTime(timezone=True), nullable=True),
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if _column_exists(inspector, "documents", "purged_at"):
        op.drop_column("documents", "purged_at")
