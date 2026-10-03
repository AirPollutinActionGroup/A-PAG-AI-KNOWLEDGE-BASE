"""Drop the dead `document_chunks.search_vector`, its GIN index and its trigger.

Migration `0016` moved the lexical arm from `ts_rank` to BM25 (`pg_search`). The tsvector
machinery `0015` built was deliberately left in place so the switch stayed revertible while BM25
had only been measured against 16 sample queries — evidence, but not an evaluation set.

It has since been measured properly: BM25 is the lexical arm in all four configurations of
`run_eval.py --compare`, and the question that justified keeping an escape hatch — whether
`ts_rank`'s recall loss was real — was answered on 100 questions, not 16. Nothing reads this
column. `grep search_vector` now returns only `documents.search_vector`, which is a different
column built by `0006` over title/filename/description and is still live behind
`GET /documents?q=`.

**Why now rather than later.** The cost is not the disk: the trigger recomputes a tsvector on
every chunk INSERT and UPDATE of `text`, and the GIN index is maintained for a column no query
reads. That is invisible at 2,860 chunks and is not invisible during a bulk archive ingest — and
a bulk ingest is the next thing that happens on the deployment box. `KNOWN_DEBTS.md` #35 named
exactly this trigger: "once BM25 has served real queries without a reason to go back, or
immediately before the bulk ingest, whichever comes first."

**The downgrade genuinely restores it**, trigger, function, index, column and backfill — if it
only dropped things, the revertibility this column was kept for would have quietly expired at the
moment it was removed. It reinstates `0015`'s state, not an empty column.

Order matters on the way down: the column has to exist before the trigger that writes it, and the
backfill has to run before the index so the index is built once over populated rows rather than
maintained row by row through the UPDATE.

Revision ID: 0020_drop_chunk_tsvector
Revises: 0019_bm25_document_id

Revision id kept to 23 characters: `alembic_version.version_num` is VARCHAR(32) (see CLAUDE.md).
"""

import sqlalchemy as sa
from alembic import op

revision = "0020_drop_chunk_tsvector"
down_revision = "0019_bm25_document_id"
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

    # Trigger before function: dropping a function a trigger still depends on needs CASCADE,
    # which would be a blunter instrument than this migration wants to be holding.
    op.execute(f"DROP TRIGGER IF EXISTS {TRIGGER} ON document_chunks")
    op.execute(f"DROP FUNCTION IF EXISTS {TRIGGER_FN}()")
    op.execute(f"DROP INDEX IF EXISTS {INDEX}")
    if _column_exists(inspector, "document_chunks", "search_vector"):
        op.drop_column("document_chunks", "search_vector")


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if not _column_exists(inspector, "document_chunks", "search_vector"):
        op.execute("ALTER TABLE document_chunks ADD COLUMN search_vector tsvector")

    # Verbatim from `0015`: body weighted 'A', heading 'B'. A heading is a strong signal but a
    # short one, and weighting it above the body would let a heading match outrank a passage that
    # discusses the term throughout.
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

    op.execute("UPDATE document_chunks SET text = text WHERE search_vector IS NULL")

    # Built normally rather than CONCURRENTLY, as in `0014`/`0015`: CREATE INDEX CONCURRENTLY
    # cannot run inside a transaction and Alembic wraps migrations in one.
    op.execute(
        f"CREATE INDEX IF NOT EXISTS {INDEX} ON document_chunks USING gin (search_vector)"
    )
