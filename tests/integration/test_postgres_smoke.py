"""Smoke test for real PostgreSQL integration fixtures."""

from sqlalchemy import text
from sqlalchemy.orm import Session

from src.db.models import Document as DocORM, Job as JobORM, AuditLog as AuditORM


def test_postgres_connection_and_tables_exist(db_session: Session):
    """Verifies connection to real PostgreSQL, dialect verification, and table creation."""
    # Check dialect is postgresql
    bind = db_session.get_bind()
    assert bind.dialect.name == "postgresql"

    # Check basic query works
    res = db_session.execute(text("SELECT 1 AS num")).scalar()
    assert res == 1

    # Check tables can be queried
    docs_count = db_session.query(DocORM).count()
    jobs_count = db_session.query(JobORM).count()
    audit_count = db_session.query(AuditORM).count()

    assert docs_count >= 0
    assert jobs_count >= 0
    assert audit_count >= 0
