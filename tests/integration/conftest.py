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

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from src.core.config import settings
from src.db.models import Base


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

        container = PostgresContainer("postgres:16-alpine")
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

    # Create tables once for the test session
    try:
        Base.metadata.create_all(bind=engine)
    except Exception as e:
        pytest.skip(f"PostgreSQL not reachable ({e}). Ensure Docker or local Postgres is running.")

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
