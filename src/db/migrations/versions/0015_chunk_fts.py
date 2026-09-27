"""Add a full-text index over chunk bodies, so retrieval can be lexical as well as semantic.

Until now the only tsvector in the schema was `documents.search_vector`, built from title,
filename and description by migration `0006` — **metadata only**. No document's body text was ever
searchable by word. Vector search covers meaning, but embeddings are poor at exact identifiers:
"Section 114", "CAQM Act 2021", "GRAP Stage III", a specific district name. Those are tokens a
lexical index matches exactly and an embedding blurs into whatever it is semantically near, which
for policy documents is a real gap rather than a refinement.

Weighting: the passage body is 'A' and its section heading 'B'. The heading is a strong signal —
a passage under "4. Penalties" is about penalties — but it is short, and weighting it above the
body would let a heading match outrank a passage that discusses the term throughout.

The trigger fires on INSERT and on UPDATE of the two source columns only, so the embedding stage
writing a vector back to the row does not needlessly recompute the tsvector.

Revision ID: 0015_chunk_fts
Revises: 0014_embeddings

Revision id kept short: `alembic_version.version_num` is VARCHAR(32) (see CLAUDE.md).
"""

import sqlalchemy as sa
from alembic import op

revision = "0015_chunk_fts"
down_revision = "0014_embeddings"
branch_labels = None
depends_on = None

TRIGGER_FN = "document_chunks_search_vector_update"
TRIGGER = "trg_document_chunks_search_vector_update"
INDEX = "idx_document_chunks_search_vector"


def _column_exists(inspector, table: str, name: str) -> bool:
    return any(c["name"] == name for c in inspector.get_columns(table))


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if not _column_exists(inspector, "document_chunks", "search_vector"):
        op.execute("ALTER TABLE document_chunks ADD COLUMN search_vector tsvector")

    # 'english' matches what `documents.search_vector` uses. It stems and drops stopwords, which
    # is right for prose; it is also why an exact-identifier match still benefits from the vector
    # arm rather than replacing it.
    op.execute(f"""
        CREATE OR REPLACE FUNCTION {TRIGGER_FN}() RETURNS trigger AS $$
        BEGIN
            NEW.search_vector :=
                setweight(to_tsvector('english', coalesce(NEW.text, '')), 'A') ||
                setweight(to_tsvector('english', coalesce(NEW.section_heading, '')), 'B');
            RETURN NEW;
        END
        $$ LANGUAGE plpgsql;
    """)

    op.execute(f"DROP TRIGGER IF EXISTS {TRIGGER} ON document_chunks")
    op.execute(f"""
        CREATE TRIGGER {TRIGGER}
        BEFORE INSERT OR UPDATE OF text, section_heading ON document_chunks
        FOR EACH ROW EXECUTE FUNCTION {TRIGGER_FN}();
    """)

    # Backfill through the trigger. This rewrites every existing row, which is cheap at the
    # current corpus size and would not be at the scale of a bulk archive import — run the
    # migration before such an import, not after.
    op.execute("UPDATE document_chunks SET text = text WHERE search_vector IS NULL")

    # Built normally rather than CONCURRENTLY, for the same reason as `0014`'s HNSW index:
    # CREATE INDEX CONCURRENTLY cannot run inside a transaction and Alembic wraps migrations in
    # one. A later rebuild on a large table must use CONCURRENTLY or it locks out writes.
    op.execute(
        f"CREATE INDEX IF NOT EXISTS {INDEX} ON document_chunks USING gin (search_vector)"
    )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    op.execute(f"DROP INDEX IF EXISTS {INDEX}")
    op.execute(f"DROP TRIGGER IF EXISTS {TRIGGER} ON document_chunks")
    op.execute(f"DROP FUNCTION IF EXISTS {TRIGGER_FN}")
    if _column_exists(inspector, "document_chunks", "search_vector"):
        op.drop_column("document_chunks", "search_vector")
