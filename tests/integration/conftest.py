"""Integration Test Fixtures with PostgreSQL.

Provides:
- postgres_engine: Session-scoped SQLAlchemy Engine backed by PostgresContainer
  (or local Postgres fallback if Docker daemon is not active).
- db_session: Function-scoped SQLAlchemy Session with automatic transaction rollback
  so individual tests never contaminate each other.
"""

import logging
import os
from collections.abc import Generator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from src.core.config import settings


def _get_postgres_url() -> tuple[str, any]:
    """Spins up a throwaway PostgresContainer via testcontainers.

    The fallback to settings.DATABASE_URL is DESTRUCTIVE and therefore opt-in: several
    tests here (test_worker_execution.py, test_worker_skip_locked.py) clear the whole
    `documents`/`jobs` tables to get a clean queue, which is harmless in a disposable
    container but wipes real data when pointed at a development database. That fallback
    used to be silent — when `testcontainers` wasn't installed, every `pytest tests/`
    run quietly deleted the dev database's documents.

    Set APAG_ALLOW_DESTRUCTIVE_DB_TESTS=1 to knowingly run against DATABASE_URL.
    """
    try:
        try:
            from testcontainers.community.postgres import PostgresContainer
        except ImportError:
            from testcontainers.postgres import PostgresContainer

        # ParadeDB, not plain postgres or the pgvector image: this schema needs *both* the
        # vector extension (migration 0014) and pg_search for BM25 (0016). ParadeDB ships both on
        # the same Postgres 16.15 the pgvector image ran, so this is not a version change.
        #
        # A fresh container initialises its own data directory, so ParadeDB's entrypoint puts
        # pg_search into shared_preload_libraries for us. docker-compose has to pass that as an
        # explicit flag instead, because its volume was initialised by the older image and that
        # init never re-runs.
        container = PostgresContainer("paradedb/paradedb:0.25.10-pg16")
        container.start()
        db_url = container.get_connection_url()
        return db_url, container
    except Exception as e:
        if os.environ.get("APAG_ALLOW_DESTRUCTIVE_DB_TESTS") != "1":
            pytest.skip(
                "No throwaway Postgres available "
                f"({type(e).__name__}: {e}). Integration tests clear the documents/jobs "
                "tables, so they refuse to run against DATABASE_URL by default. "
                "Fix: `uv sync` (installs testcontainers) and start Docker. "
                "To deliberately run against DATABASE_URL and accept the data loss, set "
                "APAG_ALLOW_DESTRUCTIVE_DB_TESTS=1.",
                allow_module_level=True,
            )
        return settings.DATABASE_URL, None


@pytest.fixture(scope="session")
def postgres_engine():
    """Session-scoped PostgreSQL engine with schema initialized."""
    db_url, container = _get_postgres_url()
    engine = create_engine(
        db_url,
        pool_pre_ping=True,
        echo=False,
    )

    # The ORM declares a pgvector column, so the extension has to exist before create_all().
    # Alembic does this in migration 0014; these tests build the schema directly from metadata.
    try:
        with engine.begin() as conn:
            conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
    except Exception as e:
        pytest.skip(
            f"Could not enable the pgvector extension ({e}). The server must be a ParadeDB "
            f"build — see docker-compose.yml."
        )

    # Build the schema by running the real migrations, not `Base.metadata.create_all()`.
    #
    # create_all() builds tables from ORM metadata, and a great deal of this schema's behaviour
    # is not in the ORM: the audit_log immutability triggers (0003), the partial unique index
    # that is the actual dedup guarantee (0004), the `documents` tsvector trigger (0006), and
    # the BM25 index the lexical arm is built on (0016, 0018, 0019). Under create_all() every one
    # of those is absent from the test database, so tests written against them pass or fail for
    # the wrong reasons — a lexical-search test finds nothing because the index it queries was
    # never created, which looks exactly like a broken query. Running migrations makes the test
    # schema the schema that ships.
    #
    # Deliberately NOT caught-and-skipped: a schema that cannot be built is a broken schema, and
    # skipping here would turn a real failure into a silently green run with zero integration
    # coverage. Only genuine unreachability (handled above and in _get_postgres_url) skips.
    alembic_cfg = Config(str(Path(__file__).resolve().parents[2] / "alembic.ini"))
    alembic_cfg.set_main_option("sqlalchemy.url", db_url)
    # Set explicitly so env.py does not fall back to settings.DATABASE_URL — that would run the
    # migrations against the developer's real database instead of the throwaway container.
    command.upgrade(alembic_cfg, "head")

    yield engine

    engine.dispose()
    if container:
        # Logged rather than silently swallowed: a repeatedly-failing stop() leaks dead
        # Postgres containers across test runs, which is worth being able to see.
        try:
            container.stop()
        except Exception as e:
            logging.getLogger(__name__).warning("Failed to stop test container: %s", e)


@pytest.fixture
def db_session(postgres_engine) -> Generator[Session, None, None]:
    """Function-scoped database session wrapped in an isolated transaction.
    Rolls back automatically at the conclusion of each test.
    """
    connection = postgres_engine.connect()
    transaction = connection.begin()
    SessionLocal = sessionmaker(bind=connection)
    session = SessionLocal()

    yield session

    session.close()
    # A rollback failure here usually means the test's own code already committed and ended
    # this transaction (see KNOWN_DEBTS.md #10) — not fatal, but not worth hiding either.
    try:
        if transaction.is_active:
            transaction.rollback()
    except Exception as e:
        logging.getLogger(__name__).warning("Test transaction rollback failed: %s", e)
    connection.close()
