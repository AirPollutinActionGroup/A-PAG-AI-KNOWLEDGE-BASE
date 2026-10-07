"""Add `drive_files` and `drive_sync_state` for the Google Drive connector.

`drive_files` records what the connector has seen and what it decided. It is a separate table
rather than columns on `documents` for two reasons: most documents have no Drive origin, so those
columns would be nulls on the busiest table in the schema; and a *skipped* Drive file — a Google
Form, something oversized, a file outside the watched folders — has no document to hang metadata
off, while being exactly the thing worth recording. A gap that is findable beats one rediscovered
on every sync, which is the same argument the quality gate makes for `LOW_TEXT_DENSITY`.

The primary key is Drive's own file id. It is stable across renames and moves, which is what
makes "have I already imported this?" answerable; a name is not. `drive_modified_time` is the
change detector rather than a content hash, deliberately: Google re-exports an unchanged Doc to
slightly different bytes each time, so hashing the export would re-import the whole corpus on
every run.

`document_id` is nullable with ON DELETE SET NULL. A skipped file never had a document, and a
purged document must not take this row with it — the record of having seen the file is the thing
that stops the next sync importing it again.

`drive_sync_state` is a one-row-per-key store, holding the change-feed page token the Phase B
worker needs. Created here rather than with that worker so it is a code change later, not a code
change plus a migration against a live database.

**No new status, stage or audit event.** A Drive file enters at `UploadService.receive()` and is
then an ordinary document; removal reuses the existing reversible soft delete and the existing
`DOCUMENT_DELETED` event. A connector that needed its own lifecycle vocabulary would be a sign it
had bypassed the pipeline rather than fed it.

Revision ID: 0021_drive_sync
Revises: 0020_drop_chunk_tsvector

Revision id kept to 15 characters: `alembic_version.version_num` is VARCHAR(32), and a longer id
fails the final version-bump statement and rolls back the whole migration (see CLAUDE.md).
"""

import sqlalchemy as sa
from alembic import op

revision = "0021_drive_sync"
down_revision = "0020_drop_chunk_tsvector"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "drive_files",
        sa.Column("drive_file_id", sa.String(length=128), primary_key=True),
        sa.Column("document_id", sa.Uuid(as_uuid=True), nullable=True),
        sa.Column("drive_modified_time", sa.String(length=64), nullable=True),
        sa.Column("drive_name", sa.String(length=500), nullable=True),
        sa.Column("drive_mime_type", sa.String(length=200), nullable=True),
        sa.Column("folder_id", sa.String(length=128), nullable=True),
        sa.Column("classification", sa.String(length=50), nullable=True),
        sa.Column("drive_owner_email", sa.String(length=320), nullable=True),
        sa.Column("state", sa.String(length=32), nullable=False),
        sa.Column("skip_reason", sa.Text(), nullable=True),
        sa.Column(
            "last_synced_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["document_id"],
            ["documents.document_id"],
            name="fk_drive_files_document_id",
            ondelete="SET NULL",
        ),
        sa.CheckConstraint(
            "state IN ('IMPORTED', 'SKIPPED', 'REMOVED', 'FAILED')",
            name="chk_drive_files_state",
        ),
    )
    op.create_index("idx_drive_files_document_id", "drive_files", ["document_id"])
    op.create_index("ix_drive_files_state", "drive_files", ["state"])

    op.create_table(
        "drive_sync_state",
        sa.Column("key", sa.String(length=64), primary_key=True),
        sa.Column("value", sa.Text(), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )


def downgrade() -> None:
    # A genuine reversal: both tables are the connector's own bookkeeping and nothing else reads
    # them. Documents already imported from Drive are untouched and stay in the corpus — they are
    # ordinary documents, which is the point of entering through `receive()`. What is lost is the
    # memory of which Drive file produced which document, so a connector re-enabled after a
    # downgrade would import the folder again and every file would land as a DUPLICATE at scan.
    # Noisy, not destructive.
    op.drop_table("drive_sync_state")
    op.drop_index("ix_drive_files_state", table_name="drive_files")
    op.drop_index("idx_drive_files_document_id", table_name="drive_files")
    op.drop_table("drive_files")
