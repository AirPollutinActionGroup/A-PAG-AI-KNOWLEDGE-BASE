"""Integration Test Fixtures with PostgreSQL.

Provides:
- postgres_engine: Session-scoped SQLAlchemy Engine backed by PostgresContainer
  (or local Postgres fallback if Docker daemon is not active).
- db_session: Function-scoped SQLAlchemy Session with automatic transaction rollback
  so individual tests never contaminate each other.
"""

from collections.abc import Generator
import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from src.core.config import settings
from src.db.models import Base


def _get_postgres_url() -> tuple[str, any]:
    """Attempts to spin up a PostgresContainer via testcontainers.
    Falls back to settings.DATABASE_URL if Docker is unavailable.
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
    except Exception:
        # Fallback to local / environment postgres if docker daemon is inactive
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
        try:
            container.stop()
        except Exception:
            pass


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
    try:
        if transaction.is_active:
            transaction.rollback()
    except Exception:
        pass
    connection.close()
