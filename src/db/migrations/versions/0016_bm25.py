"""Add a real BM25 index over chunk bodies, alongside the existing tsvector.

`0015` gave the lexical arm a Postgres `tsvector` and `ts_rank`. That works, but `ts_rank` is not
BM25: it has no document-length normalisation and no IDF saturation, so a long document scores
higher merely for being long, and a term appearing twenty times counts roughly twenty times
rather than saturating. BM25 is what "sparse retrieval" means everywhere else, and what the
published hybrid-search numbers were measured with.

`pg_search` (ParadeDB) implements BM25 inside Postgres on a Tantivy index, so this stays one
database. The Postgres image moves from `pgvector/pgvector:pg16` to
`paradedb/paradedb:0.25.10-pg16` — **the same Postgres 16.15**, so the data directory is
unchanged and this is not a version upgrade. That image carries pgvector too; `pg_search` in fact
declares a hard dependency on it.

**The tsvector is deliberately left in place.** Two lexical implementations now exist so they can
be compared on this corpus rather than swapped on reputation. Whichever loses gets removed in a
follow-up, and until that measurement exists, keeping the old one is what makes the change
reversible.

Revision ID: 0016_bm25
Revises: 0015_chunk_fts
"""

import sqlalchemy as sa
from alembic import op

revision = "0016_bm25"
down_revision = "0015_chunk_fts"
branch_labels = None
depends_on = None

INDEX = "idx_document_chunks_bm25"


def _has_extension(bind, name: str) -> bool:
    return bool(
        bind.execute(
            sa.text("SELECT 1 FROM pg_available_extensions WHERE name = :n"), {"n": name}
        ).scalar()
    )


def upgrade() -> None:
    bind = op.get_bind()

    if not _has_extension(bind, "pg_search"):
        # Fail with the actual cause. Without this the next statement fails on `USING bm25` with
        # "access method does not exist", which names the symptom and not the missing image.
        raise RuntimeError(
            "The pg_search extension is not available on this server. The Postgres image must be "
            "paradedb/paradedb:0.25.10-pg16 (same Postgres 16.15 as the pgvector image it "
            "replaces) — see docker-compose.yml."
        )

    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_search")

    # `key_field` must be the table's unique key; pg_search returns scores keyed by it.
    # `section_heading` is indexed alongside the body for the same reason the tsvector weights it:
    # a passage under "4. Penalties" is about penalties. Field boosting, if wanted later, is a
    # query-side concern and needs no reindex.
    op.execute(f"""
        CREATE INDEX IF NOT EXISTS {INDEX}
        ON document_chunks
        USING bm25 (chunk_id, text, section_heading)
        WITH (key_field='chunk_id')
    """)


def downgrade() -> None:
    op.execute(f"DROP INDEX IF EXISTS {INDEX}")
    # The extensions are left installed: other objects may depend on them, and dropping an
    # extension is a heavier and less reversible act than this migration should take on itself.
