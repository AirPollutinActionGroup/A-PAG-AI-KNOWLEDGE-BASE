"""Add document_id to the BM25 index, so a scoped question can be filtered inside pg_search.

Scoping a question to one document made the lexical arm fail outright:

    psycopg2.errors.InternalError_: bitmap cursor source was never initialized

The `@@@` operator runs as a pg_search custom scan. Adding an ordinary SQL predicate alongside
it -- `document_id IN (...)` -- breaks that scan for some queries and not others, because the
planner's choice depends on the terms: "who signed this memorandum" was fine and "who signed the
FGD extension memorandum" was not. It is not about which table carries the predicate (chunk or
document, both fail) and `enable_bitmapscan = off` does not avoid it.

The supported way to restrict a pg_search query is to do it *inside* the query, with a `must`
clause over an indexed field -- which requires the field to be in the index. Hence this.

The semantic arm is unaffected and keeps its ordinary WHERE predicate: it is plain pgvector with
no custom scan to confuse.

Revision ID: 0019_bm25_document_id
Revises: 0018_chunk_document_title

Revision id kept short on purpose: `alembic_version.version_num` is VARCHAR(32), and a longer
id fails the final version-bump statement, rolling back the whole migration (see CLAUDE.md).
"""

from alembic import op

revision = "0019_bm25_document_id"
down_revision = "0018_chunk_document_title"
branch_labels = None
depends_on = None

INDEX = "idx_document_chunks_bm25"


def upgrade() -> None:
    # Rebuilt rather than altered: pg_search fixes an index's columns at creation.
    op.execute(f"DROP INDEX IF EXISTS {INDEX}")
    op.execute(f"""
        CREATE INDEX IF NOT EXISTS {INDEX}
        ON document_chunks
        USING bm25 (chunk_id, text, section_heading, document_title, document_id)
        WITH (key_field='chunk_id')
    """)


def downgrade() -> None:
    op.execute(f"DROP INDEX IF EXISTS {INDEX}")
    op.execute(f"""
        CREATE INDEX IF NOT EXISTS {INDEX}
        ON document_chunks
        USING bm25 (chunk_id, text, section_heading, document_title)
        WITH (key_field='chunk_id')
    """)
