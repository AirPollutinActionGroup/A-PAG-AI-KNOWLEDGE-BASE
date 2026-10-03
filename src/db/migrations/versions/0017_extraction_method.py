"""Add documents.extraction_method — how a document's text was obtained.

NATIVE (read from the file's own text layer), OCR (read from pixels) or MIXED (both, page by
page). It is already written into the EXTRACTION_COMPLETED audit record, but the audit log is
append-only history, not a place to query current state from: answering "which documents needed
OCR" or showing a reader that a passage was machine-read meant replaying events per document.

This matters beyond bookkeeping. OCR on this corpus measures 0.99 mean confidence and still drops
word boundaries — `FGDinexisting plantswere` — which costs the lexical arm a term it can never
match. Someone about to quote a passage in a government submission should be able to see that it
was inferred from a photograph, and someone tuning retrieval should be able to find every document
in that category in one query.

Nullable, and deliberately not backfilled: every document ingested before this migration was read
natively, but writing NATIVE across them would be asserting a fact that was never recorded. NULL
says "not known", which is true, and the value appears the next time a document is extracted.

Revision ID: 0017_extraction_method
Revises: 0016_bm25

Revision id kept short on purpose: `alembic_version.version_num` is VARCHAR(32), and a longer
id fails the final version-bump statement, rolling back the whole migration (see CLAUDE.md).
"""

import sqlalchemy as sa
from alembic import op

revision = "0017_extraction_method"
down_revision = "0016_bm25"
branch_labels = None
depends_on = None

_CHECK = "chk_documents_extraction_method"
_VALUES = ("NATIVE", "OCR", "MIXED")


def _has_column(table: str, column: str) -> bool:
    inspector = sa.inspect(op.get_bind())
    return any(c["name"] == column for c in inspector.get_columns(table))


def upgrade() -> None:
    # Guarded for the same reason as 0006: integration fixtures can create tables ahead of
    # Alembic, and a migration that cannot be re-run against a partially-built schema is a
    # migration that fails in exactly the environment set up to catch its failures.
    if not _has_column("documents", "extraction_method"):
        op.add_column(
            "documents",
            sa.Column("extraction_method", sa.String(20), nullable=True),
        )

    # A typo guard, not a security boundary. Narrow because the set is closed: a fourth way to
    # read a document would be a pipeline change, and should arrive with its own migration.
    op.create_check_constraint(
        _CHECK,
        "documents",
        sa.text(
            "extraction_method IS NULL OR extraction_method IN ("
            + ", ".join(f"'{v}'" for v in _VALUES)
            + ")"
        ),
    )


def downgrade() -> None:
    op.drop_constraint(_CHECK, "documents", type_="check")
    op.drop_column("documents", "extraction_method")
