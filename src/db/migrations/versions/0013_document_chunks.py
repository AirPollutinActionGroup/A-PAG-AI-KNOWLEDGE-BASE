"""Add document_chunks plus the CHUNKED/CHUNKING_FAILED statuses and CHUNK job stage.

Chunking splits a normalized document into the passages retrieval will actually return. Those
passages live in Postgres rather than object storage — unlike every other pipeline artifact —
because they are queried rather than merely stored: the next stage adds an embedding column to
this same table, and a similarity search cannot join against JSON sitting in a bucket.

`page_number`, `section_heading` and `is_table` are the citation contract. A passage that cannot
say which section, page or table it came from is unusable in a government submission, and that
provenance is only knowable here, while the document's structure is still in hand.

`scale` ships holding a single value, and that is deliberate. The best chunk size depends on the
question being asked, which is unknown at indexing time; the eventual fix is to index at several
granularities and fuse the results. Carrying `scale` in the unique key from the start makes adding
a second granularity an INSERT rather than a migration, a backfill and a retrieval rewrite.

Revision ID: 0013_document_chunks
Revises: 0012_reclassification

Revision id kept short on purpose: `alembic_version.version_num` is VARCHAR(32), and a longer id
fails the final version-bump statement, rolling back the whole migration (see CLAUDE.md).
"""

import sqlalchemy as sa
from alembic import op

revision = "0013_document_chunks"
down_revision = "0012_reclassification"
branch_labels = None
depends_on = None

OLD_STATUS_VALUES = (
    "UPLOADED", "QUARANTINED", "VALIDATED", "VALIDATION_FAILED", "REJECTED",
    "EXTRACTED", "EXTRACTION_FAILED", "NORMALIZATION_FAILED",
    "AWAITING_CLASSIFICATION", "DUPLICATE", "LIVE", "SUPERSEDED", "ARCHIVED",
)
NEW_STATUS_VALUES = (
    "UPLOADED", "QUARANTINED", "VALIDATED", "VALIDATION_FAILED", "REJECTED",
    "EXTRACTED", "EXTRACTION_FAILED", "NORMALIZATION_FAILED",
    "AWAITING_CLASSIFICATION", "CHUNKED", "CHUNKING_FAILED",
    "DUPLICATE", "LIVE", "SUPERSEDED", "ARCHIVED",
)

OLD_STAGE_VALUES = ("SCAN", "EXTRACT", "NORMALIZE")
NEW_STAGE_VALUES = ("SCAN", "EXTRACT", "NORMALIZE", "CHUNK")

OLD_EVENT_VALUES = (
    "DOCUMENT_UPLOADED", "DOCUMENT_QUARANTINED", "VALIDATION_PASSED", "VALIDATION_FAILED",
    "DOCUMENT_PROMOTED", "DOCUMENT_REJECTED", "DOCUMENT_SUPERSEDED", "DOCUMENT_ARCHIVED",
    "DOCUMENT_DELETED", "EXTRACTION_COMPLETED", "EXTRACTION_FAILED",
    "NORMALIZATION_COMPLETED", "NORMALIZATION_FAILED", "DOCUMENT_RECLASSIFIED",
)
NEW_EVENT_VALUES = OLD_EVENT_VALUES + ("CHUNKING_COMPLETED", "CHUNKING_FAILED")


def _constraint_exists(inspector, table: str, name: str) -> bool:
    return any(c.get("name") == name for c in inspector.get_check_constraints(table))


def _set_check(inspector, table: str, name: str, column: str, values: tuple[str, ...]) -> None:
    if _constraint_exists(inspector, table, name):
        op.drop_constraint(name, table, type_="check")
    rendered = ", ".join(f"'{v}'" for v in values)
    op.create_check_constraint(name, table, f"{column} IN ({rendered})")


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    _set_check(inspector, "documents", "chk_documents_status", "status", NEW_STATUS_VALUES)
    _set_check(inspector, "jobs", "chk_jobs_stage", "stage", NEW_STAGE_VALUES)
    _set_check(inspector, "audit_log", "chk_audit_log_event_type", "event_type", NEW_EVENT_VALUES)

    # Guarded because integration fixtures can call Base.metadata.create_all() ahead of Alembic
    # and create the table first — same reasoning as migration 0006.
    if "document_chunks" not in inspector.get_table_names():
        op.create_table(
            "document_chunks",
            sa.Column("chunk_id", sa.Uuid(as_uuid=True), primary_key=True),
            sa.Column(
                "document_id",
                sa.Uuid(as_uuid=True),
                sa.ForeignKey("documents.document_id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("scale", sa.String(length=32), nullable=False, server_default="section"),
            sa.Column("chunk_index", sa.Integer(), nullable=False),
            sa.Column("text", sa.Text(), nullable=False),
            sa.Column("page_number", sa.Integer(), nullable=True),
            sa.Column("section_heading", sa.Text(), nullable=True),
            sa.Column(
                "is_table", sa.Boolean(), nullable=False, server_default=sa.text("false")
            ),
            sa.Column("char_count", sa.Integer(), nullable=False),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                server_default=sa.func.now(),
                nullable=False,
            ),
        )
        op.create_index(
            "ix_document_chunks_document_id", "document_chunks", ["document_id"]
        )
        # Re-running the stage must not silently double a document's passages.
        op.create_index(
            "uq_document_chunks_position",
            "document_chunks",
            ["document_id", "scale", "chunk_index"],
            unique=True,
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if "document_chunks" in inspector.get_table_names():
        op.drop_table("document_chunks")

    # Rows sitting at a status the narrowed constraint would reject have to move first. Back to
    # AWAITING_CLASSIFICATION, which is exactly where chunking picks them up again.
    op.execute(
        "UPDATE documents SET status = 'AWAITING_CLASSIFICATION' "
        "WHERE status IN ('CHUNKED', 'CHUNKING_FAILED')"
    )
    op.execute("DELETE FROM jobs WHERE stage = 'CHUNK'")

    _set_check(inspector, "documents", "chk_documents_status", "status", OLD_STATUS_VALUES)
    _set_check(inspector, "jobs", "chk_jobs_stage", "stage", OLD_STAGE_VALUES)

    # The audit event constraint is deliberately NOT narrowed back — same reasoning as 0012.
    # Doing so would require deleting CHUNKING_* rows first, and migration 0003 makes audit_log
    # append-only precisely so it cannot be rewritten. One extra permitted value in a typo guard
    # is a far smaller price than a hole in the audit trail.
