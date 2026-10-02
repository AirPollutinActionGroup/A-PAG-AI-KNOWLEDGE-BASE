"""Add document_chunks.document_title and put it in the BM25 index.

You could not ask for a document by name. "Summarise the MoP OM dated 20 November" answered
"the passages do not contain a document titled..." while `MoP OM dated 20 November 2024 (2).pdf`
sat in the corpus, because retrieval searches chunk *text* and the filename is not in it:

    chunks in that document whose text contains "MoP OM":  0 of 2

`documents.search_vector` has indexed title and filename since 0006, but that serves
`GET /documents/search` -- a list of documents -- not chunk retrieval, so an answer could never
be built from it. Asking for a named document is the most natural question a person asks, and it
structurally could not work.

Denormalised onto the chunk rather than joined at query time, because a `bm25` index covers one
table: to match on a title, the title has to be in the indexed row. The cost is that renaming a
document leaves its chunks stale until re-chunked. Documents are not renamed here -- supersede
changes status, not filename -- and the backfill below is a single UPDATE if that ever changes.

Revision ID: 0018_chunk_document_title
Revises: 0017_extraction_method

Revision id kept short on purpose: `alembic_version.version_num` is VARCHAR(32), and a longer
id fails the final version-bump statement, rolling back the whole migration (see CLAUDE.md).
"""

import sqlalchemy as sa
from alembic import op

revision = "0018_chunk_document_title"
down_revision = "0017_extraction_method"
branch_labels = None
depends_on = None

INDEX = "idx_document_chunks_bm25"


def _has_column(table: str, column: str) -> bool:
    inspector = sa.inspect(op.get_bind())
    return any(c["name"] == column for c in inspector.get_columns(table))


def upgrade() -> None:
    if not _has_column("document_chunks", "document_title"):
        op.add_column(
            "document_chunks",
            sa.Column("document_title", sa.Text(), nullable=True),
        )

    # Backfill from the document's own title, falling back to the filename with its directory
    # path stripped -- the path is an artefact of how a folder was ingested and matching on it
    # would make every document in "Thermal Power Plants/References/" a hit for each other.
    op.execute("""
        UPDATE document_chunks c
        SET document_title = COALESCE(
            NULLIF(d.title, ''),
            regexp_replace(d.filename, '^.*/', '')
        )
        FROM documents d
        WHERE d.document_id = c.document_id
          AND c.document_title IS DISTINCT FROM COALESCE(
              NULLIF(d.title, ''), regexp_replace(d.filename, '^.*/', '')
          )
    """)

    # The index has to be rebuilt: pg_search fixes its indexed columns at creation.
    op.execute(f"DROP INDEX IF EXISTS {INDEX}")
    op.execute(f"""
        CREATE INDEX IF NOT EXISTS {INDEX}
        ON document_chunks
        USING bm25 (chunk_id, text, section_heading, document_title)
        WITH (key_field='chunk_id')
    """)


def downgrade() -> None:
    op.execute(f"DROP INDEX IF EXISTS {INDEX}")
    op.execute(f"""
        CREATE INDEX IF NOT EXISTS {INDEX}
        ON document_chunks
        USING bm25 (chunk_id, text, section_heading)
        WITH (key_field='chunk_id')
    """)
    op.drop_column("document_chunks", "document_title")
