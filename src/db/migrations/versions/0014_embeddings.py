"""Enable pgvector and add document_chunks.embedding, plus the EMBED stage and its statuses.

The last ingestion stage: a document whose passages all carry a vector reaches LIVE and becomes
searchable. Vectors live on the chunk rows rather than in a separate store because retrieval needs
the text, its citation metadata and its vector in the same row — a similarity search cannot join
against JSON in a bucket.

`SKIPPED_UNSUPPORTED_LANGUAGE` is not a failure state. An English-only model turns Devanagari into
unknown tokens and emits vectors that match nothing, so such a document would sit in the index
invisible, with no signal that it is missing. Recording the skip makes that gap queryable and the
document re-runnable once a model covering its language is configured.

**Vector width is fixed here.** `vector(768)` matches `settings.EMBEDDING_DIMENSIONS`, which the
ORM reads, so `alembic check` fails if one changes without the other. Changing embedding model to
one with a different width therefore needs a migration *and* a full re-embed of the corpus — it is
not a configuration edit.

Revision ID: 0014_embeddings
Revises: 0013_document_chunks

Revision id kept short on purpose: `alembic_version.version_num` is VARCHAR(32), and a longer id
fails the final version-bump statement, rolling back the whole migration (see CLAUDE.md).
"""

import sqlalchemy as sa
from alembic import op

revision = "0014_embeddings"
down_revision = "0013_document_chunks"
branch_labels = None
depends_on = None

EMBEDDING_DIMENSIONS = 768

OLD_STATUS_VALUES = (
    "UPLOADED", "QUARANTINED", "VALIDATED", "VALIDATION_FAILED", "REJECTED",
    "EXTRACTED", "EXTRACTION_FAILED", "NORMALIZATION_FAILED",
    "AWAITING_CLASSIFICATION", "CHUNKED", "CHUNKING_FAILED",
    "DUPLICATE", "LIVE", "SUPERSEDED", "ARCHIVED",
)
NEW_STATUS_VALUES = (
    "UPLOADED", "QUARANTINED", "VALIDATED", "VALIDATION_FAILED", "REJECTED",
    "EXTRACTED", "EXTRACTION_FAILED", "NORMALIZATION_FAILED",
    "AWAITING_CLASSIFICATION", "CHUNKED", "CHUNKING_FAILED",
    "EMBEDDING_FAILED", "SKIPPED_UNSUPPORTED_LANGUAGE",
    "DUPLICATE", "LIVE", "SUPERSEDED", "ARCHIVED",
)

OLD_STAGE_VALUES = ("SCAN", "EXTRACT", "NORMALIZE", "CHUNK")
NEW_STAGE_VALUES = ("SCAN", "EXTRACT", "NORMALIZE", "CHUNK", "EMBED")

OLD_EVENT_VALUES = (
    "DOCUMENT_UPLOADED", "DOCUMENT_QUARANTINED", "VALIDATION_PASSED", "VALIDATION_FAILED",
    "DOCUMENT_PROMOTED", "DOCUMENT_REJECTED", "DOCUMENT_SUPERSEDED", "DOCUMENT_ARCHIVED",
    "DOCUMENT_DELETED", "EXTRACTION_COMPLETED", "EXTRACTION_FAILED",
    "NORMALIZATION_COMPLETED", "NORMALIZATION_FAILED", "DOCUMENT_RECLASSIFIED",
    "CHUNKING_COMPLETED", "CHUNKING_FAILED",
)
NEW_EVENT_VALUES = OLD_EVENT_VALUES + (
    "EMBEDDING_COMPLETED", "EMBEDDING_FAILED", "EMBEDDING_SKIPPED",
)


def _constraint_exists(inspector, table: str, name: str) -> bool:
    return any(c.get("name") == name for c in inspector.get_check_constraints(table))


def _column_exists(inspector, table: str, name: str) -> bool:
    return any(c["name"] == name for c in inspector.get_columns(table))


def _set_check(inspector, table: str, name: str, column: str, values: tuple[str, ...]) -> None:
    if _constraint_exists(inspector, table, name):
        op.drop_constraint(name, table, type_="check")
    rendered = ", ".join(f"'{v}'" for v in values)
    op.create_check_constraint(name, table, f"{column} IN ({rendered})")


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    _set_check(inspector, "documents", "chk_documents_status", "status", NEW_STATUS_VALUES)
    _set_check(inspector, "jobs", "chk_jobs_stage", "stage", NEW_STAGE_VALUES)
    _set_check(inspector, "audit_log", "chk_audit_log_event_type", "event_type", NEW_EVENT_VALUES)

    if not _column_exists(inspector, "document_chunks", "embedding"):
        op.execute(
            f"ALTER TABLE document_chunks ADD COLUMN embedding vector({EMBEDDING_DIMENSIONS})"
        )

    # Built normally rather than CONCURRENTLY: CREATE INDEX CONCURRENTLY cannot run inside a
    # transaction and Alembic wraps migrations in one. The table is near-empty at migration time,
    # so a blocking build costs nothing. A later rebuild on a populated table must use
    # CONCURRENTLY, or it will lock out writes for the duration.
    #
    # m=16 / ef_construction=64 are pgvector's defaults. ef_search is a runtime setting, so recall
    # can be tuned later without rebuilding.
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_document_chunks_embedding "
        "ON document_chunks USING hnsw (embedding vector_cosine_ops) "
        "WITH (m = 16, ef_construction = 64)"
    )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    op.execute("DROP INDEX IF EXISTS idx_document_chunks_embedding")
    if _column_exists(inspector, "document_chunks", "embedding"):
        op.drop_column("document_chunks", "embedding")

    # Back to CHUNKED, which is exactly where embedding picks them up again. LIVE is included
    # even though the old constraint permits it: with the embedding column gone a LIVE document
    # has no vectors and is not actually searchable, so leaving it LIVE would be a lie.
    op.execute(
        "UPDATE documents SET status = 'CHUNKED' "
        "WHERE status IN ('EMBEDDING_FAILED', 'SKIPPED_UNSUPPORTED_LANGUAGE', 'LIVE')"
    )
    op.execute("DELETE FROM jobs WHERE stage = 'EMBED'")

    _set_check(inspector, "documents", "chk_documents_status", "status", OLD_STATUS_VALUES)
    _set_check(inspector, "jobs", "chk_jobs_stage", "stage", OLD_STAGE_VALUES)

    # The extension is left installed: other objects may depend on it, and dropping it is a
    # heavier, less reversible act than this migration should take on its own.
    #
    # The audit event constraint is likewise not narrowed back — same reasoning as 0012 and 0013.
    # Narrowing requires deleting EMBEDDING_* rows first, and migration 0003 makes audit_log
    # append-only precisely so it cannot be rewritten.
