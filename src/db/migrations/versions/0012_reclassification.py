"""Add the DOCUMENT_RECLASSIFIED audit event type and documents.document_date.

A document's sensitivity tier is chosen by the uploader at upload time and defaults to PUBLIC,
so there is no pipeline state that waits on a classification decision and no new DocumentStatus
here. What was missing is the ability to *change* a tier afterwards and have that change be
attributable: the initial choice is already captured in DOCUMENT_QUARANTINED's details, but a
later correction had nowhere to be recorded. DOCUMENT_RECLASSIFIED is that record.

`document_date` is the date printed on the document, as distinct from `created_at`, which is
when it was uploaded. Without the distinction a 2019 policy ingested today is indistinguishable
from a current one, and any later attempt to weight retrieval by how recent a source is would be
reading the wrong number. Nullable, because much of an archive has no reliably discoverable date
and a guess is worse than an absence.

Revision ID: 0012_reclassification
Revises: 0011_extract_normalize_statuses

Revision id kept short on purpose: `alembic_version.version_num` is VARCHAR(32), and a longer
id fails the final version-bump statement, rolling back the whole migration (see CLAUDE.md).
"""

import sqlalchemy as sa
from alembic import op

revision = "0012_reclassification"
down_revision = "0011_extract_normalize_statuses"
branch_labels = None
depends_on = None

OLD_EVENT_VALUES = (
    "DOCUMENT_UPLOADED", "DOCUMENT_QUARANTINED", "VALIDATION_PASSED", "VALIDATION_FAILED",
    "DOCUMENT_PROMOTED", "DOCUMENT_REJECTED", "DOCUMENT_SUPERSEDED", "DOCUMENT_ARCHIVED",
    "DOCUMENT_DELETED", "EXTRACTION_COMPLETED", "EXTRACTION_FAILED",
    "NORMALIZATION_COMPLETED", "NORMALIZATION_FAILED",
)
NEW_EVENT_VALUES = OLD_EVENT_VALUES + ("DOCUMENT_RECLASSIFIED",)


def _constraint_exists(inspector, table: str, name: str) -> bool:
    return any(c.get("name") == name for c in inspector.get_check_constraints(table))


def _column_exists(inspector, table: str, name: str) -> bool:
    return any(c["name"] == name for c in inspector.get_columns(table))


def _set_event_type_constraint(inspector, values: tuple[str, ...]) -> None:
    if _constraint_exists(inspector, "audit_log", "chk_audit_log_event_type"):
        op.drop_constraint("chk_audit_log_event_type", "audit_log", type_="check")
    rendered = ", ".join(f"'{v}'" for v in values)
    op.create_check_constraint(
        "chk_audit_log_event_type", "audit_log", f"event_type IN ({rendered})"
    )


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    _set_event_type_constraint(inspector, NEW_EVENT_VALUES)

    # Guarded because integration fixtures can call Base.metadata.create_all() ahead of Alembic
    # and land the column first — same reasoning as migration 0006.
    if not _column_exists(inspector, "documents", "document_date"):
        op.add_column("documents", sa.Column("document_date", sa.Date(), nullable=True))


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if _column_exists(inspector, "documents", "document_date"):
        op.drop_column("documents", "document_date")

    # The audit event constraint is deliberately NOT narrowed back.
    #
    # Narrowing it would require deleting every DOCUMENT_RECLASSIFIED row first, because Postgres
    # validates existing rows when creating a CHECK constraint. Migration 0003 revokes DELETE on
    # audit_log from PUBLIC precisely so this log cannot be rewritten — and while a migration
    # running as the table owner could delete anyway, doing so would destroy the record of who
    # changed a document's tier and when. An append-only log is not reversible; that is the point.
    #
    # The cost of leaving it is one extra permitted value in a constraint whose job is catching
    # typos. That is a far smaller price than a hole in the audit trail.
