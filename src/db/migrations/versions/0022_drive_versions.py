"""Track the documents each Drive file has produced, and record restores.

**`drive_file_versions`.** An edited Drive file arrives as a new document rather than overwriting
the old one, so a citation made last week still resolves to the text that was cited. Then the old
version has to leave search, or a document edited most days returns five near-identical copies --
which matters as soon as the sync runs nightly on its own.

It cannot leave at the moment the new one is imported. That document has only been *received*: it
may still fail extraction, or turn out at scan to be a duplicate, and retiring the old version
first would leave the file with nothing visible at all. So each version is `PENDING` until its
document settles, and the previous one is retired only once the new one is `CURRENT`. That
decision is made by a later run than the one that imported it, so it has to be stored rather than
held in memory.

**`drive_files.drive_md5`.** Drive's content checksum for ordinary files, preferred over the
modified time where it exists: a rename or a move can bump the modified time without changing a
byte, and on a nightly sync that would re-import and re-embed an unchanged document. Google-native
files have no checksum, because they have no bytes until exported -- and their exports are not
byte-stable (measured: two exports of the same unchanged Sheet hashed differently), so for those
the modified time remains the only honest signal.

**`DOCUMENT_RESTORED`.** The reverse of a soft delete. Without it the log can say a document left
the knowledge base but never that it came back, and an audit read later would contradict search.

Existing `drive_files` rows that point at a document are backfilled as that file's `CURRENT`
version, so the first reconciliation after this migration starts from the truth rather than from
an empty history it would read as "nothing to retire".

Revision ID: 0022_drive_versions
Revises: 0021_drive_sync

Revision id kept to 19 characters: `alembic_version.version_num` is VARCHAR(32) (see CLAUDE.md).
"""

import sqlalchemy as sa
from alembic import op

revision = "0022_drive_versions"
down_revision = "0021_drive_sync"
branch_labels = None
depends_on = None

OLD_EVENT_VALUES = (
    "DOCUMENT_UPLOADED", "DOCUMENT_QUARANTINED", "VALIDATION_PASSED", "VALIDATION_FAILED",
    "DOCUMENT_PROMOTED", "DOCUMENT_REJECTED", "DOCUMENT_SUPERSEDED", "DOCUMENT_ARCHIVED",
    "DOCUMENT_DELETED", "EXTRACTION_COMPLETED", "EXTRACTION_FAILED",
    "NORMALIZATION_COMPLETED", "NORMALIZATION_FAILED", "DOCUMENT_RECLASSIFIED",
    "CHUNKING_COMPLETED", "CHUNKING_FAILED",
    "EMBEDDING_COMPLETED", "EMBEDDING_FAILED", "EMBEDDING_SKIPPED",
)
NEW_EVENT_VALUES = OLD_EVENT_VALUES + ("DOCUMENT_RESTORED",)


def _set_event_check(values: tuple[str, ...]) -> None:
    op.drop_constraint("chk_audit_log_event_type", "audit_log", type_="check")
    rendered = ", ".join(f"'{v}'" for v in values)
    op.create_check_constraint(
        "chk_audit_log_event_type", "audit_log", f"event_type IN ({rendered})"
    )


def upgrade() -> None:
    _set_event_check(NEW_EVENT_VALUES)

    op.add_column("drive_files", sa.Column("drive_md5", sa.String(length=32), nullable=True))

    op.create_table(
        "drive_file_versions",
        sa.Column("version_id", sa.Uuid(as_uuid=True), primary_key=True),
        sa.Column("drive_file_id", sa.String(length=128), nullable=False),
        sa.Column("document_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("drive_modified_time", sa.String(length=64), nullable=True),
        sa.Column("drive_md5", sa.String(length=32), nullable=True),
        sa.Column("state", sa.String(length=32), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column(
            "imported_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("settled_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["drive_file_id"], ["drive_files.drive_file_id"],
            name="fk_drive_file_versions_file", ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["document_id"], ["documents.document_id"],
            name="fk_drive_file_versions_document", ondelete="CASCADE",
        ),
        sa.CheckConstraint(
            "state IN ('PENDING', 'CURRENT', 'SUPERSEDED', 'DUPLICATE', 'FAILED', 'REMOVED')",
            name="chk_drive_file_versions_state",
        ),
    )
    op.create_index("idx_drive_file_versions_file", "drive_file_versions", ["drive_file_id"])
    op.create_index("idx_drive_file_versions_document", "drive_file_versions", ["document_id"])

    # Start from the truth: every file already imported has exactly one version, and it is the
    # current one. `gen_random_uuid()` is core Postgres since 13.
    op.execute(
        """
        INSERT INTO drive_file_versions
            (version_id, drive_file_id, document_id, drive_modified_time, state,
             imported_at, settled_at)
        SELECT gen_random_uuid(), drive_file_id, document_id, drive_modified_time,
               CASE WHEN state = 'REMOVED' THEN 'REMOVED' ELSE 'CURRENT' END,
               created_at, now()
        FROM drive_files
        WHERE document_id IS NOT NULL
        """
    )


def downgrade() -> None:
    op.drop_index("idx_drive_file_versions_document", table_name="drive_file_versions")
    op.drop_index("idx_drive_file_versions_file", table_name="drive_file_versions")
    op.drop_table("drive_file_versions")
    op.drop_column("drive_files", "drive_md5")
    # The audit constraint is deliberately left widened, as in 0012-0014. Narrowing it would mean
    # deleting every DOCUMENT_RESTORED row first, because Postgres validates existing rows when a
    # CHECK is created, and migration 0003 makes this log append-only so that it cannot be
    # rewritten. One spare value in a typo guard is cheaper than a hole in the audit trail.
