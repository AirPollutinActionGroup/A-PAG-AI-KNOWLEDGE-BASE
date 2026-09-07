"""Unit tests for BaseWorker and ScanWorker skeleton."""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from src.core.errors import PermanentProcessingError
from src.db.models import Base
from src.db.models import Document as DocumentORM
from src.db.models import Job as JobORM
from src.storage.bucket_manager import BucketManager
from src.storage.object_storage import LocalFileSystemStorage
from src.workers.base_worker import JobItem
from src.workers.scan_worker import ScanWorker


@pytest.fixture
def sqlite_session_factory():
    """Provides in-memory SQLite session factory for worker unit tests."""
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    SessionLocal = sessionmaker(bind=engine)
    return SessionLocal


def test_worker_picks_up_pending_job(sqlite_session_factory):
    """Verifies worker picks up a PENDING job, updates status to RUNNING, and assigns worker_id."""
    doc_id = uuid.uuid4()
    job_id = uuid.uuid4()

    with sqlite_session_factory() as session:
        doc = DocumentORM(document_id=doc_id, filename="test.pdf", file_size=100, status="QUARANTINED")
        job = JobORM(job_id=job_id, document_id=doc_id, stage="SCAN", status="PENDING")
        session.add_all([doc, job])
        session.commit()

    worker = ScanWorker(session_factory=sqlite_session_factory, worker_id="test-worker-1", lease_seconds=60)
    claimed = worker.pick_job()

    assert claimed is not None
    assert claimed.job_id == job_id
    assert claimed.status == "RUNNING"
    assert claimed.worker_id == "test-worker-1"

    # Verify state in DB
    with sqlite_session_factory() as session:
        db_job = session.query(JobORM).filter(JobORM.job_id == job_id).first()
        assert db_job.status == "RUNNING"
        assert db_job.worker_id == "test-worker-1"
        assert db_job.started_at is not None


def test_worker_skips_running_jobs(sqlite_session_factory):
    """Verifies worker does NOT pick up jobs that are already in RUNNING or COMPLETED state."""
    doc_id = uuid.uuid4()
    job_id = uuid.uuid4()

    with sqlite_session_factory() as session:
        doc = DocumentORM(document_id=doc_id, filename="running.pdf", file_size=100, status="QUARANTINED")
        job = JobORM(job_id=job_id, document_id=doc_id, stage="SCAN", status="RUNNING", worker_id="other-worker")
        session.add_all([doc, job])
        session.commit()

    worker = ScanWorker(session_factory=sqlite_session_factory, worker_id="test-worker-2")
    claimed = worker.pick_job()
    assert claimed is None


def test_worker_respects_scheduled_at(sqlite_session_factory):
    """Verifies worker respects scheduled_at in the future (backoff delay)."""
    doc_id = uuid.uuid4()
    job_id = uuid.uuid4()
    future_time = datetime.now(UTC) + timedelta(minutes=5)

    with sqlite_session_factory() as session:
        doc = DocumentORM(document_id=doc_id, filename="delayed.pdf", file_size=100, status="QUARANTINED")
        job = JobORM(
            job_id=job_id,
            document_id=doc_id,
            stage="SCAN",
            status="PENDING",
            scheduled_at=future_time,
        )
        session.add_all([doc, job])
        session.commit()

    worker = ScanWorker(session_factory=sqlite_session_factory, worker_id="test-worker-3")
    claimed = worker.pick_job()
    assert claimed is None


def test_scan_worker_treats_missing_document_as_permanent_failure(tmp_path, sqlite_session_factory):
    """Verifies a job referencing a document_id with no matching `documents` row raises
    PermanentProcessingError (DOCUMENT_NOT_FOUND) — never retried, since no retry could fix it.

    Uses SQLite directly (FKs not enforced by default) rather than a real Postgres worker test,
    because jobs.document_id carries an ON DELETE CASCADE FK to documents in Postgres — a job
    can never actually reference a nonexistent document there, so this path is only reachable
    here or via a genuine InMemoryDocumentRepository/PostgreSQL repository divergence.
    """
    missing_doc_id = uuid.uuid4()
    job_id = uuid.uuid4()

    bucket_manager = BucketManager(storage=LocalFileSystemStorage(base_dir=str(tmp_path)))
    worker = ScanWorker(session_factory=sqlite_session_factory, bucket_manager=bucket_manager)

    job = JobItem(
        job_id=job_id,
        document_id=missing_doc_id,
        stage="SCAN",
        status="RUNNING",
        worker_id="test-worker",
        retry_count=0,
    )

    with pytest.raises(PermanentProcessingError, match="DOCUMENT_NOT_FOUND"):
        worker.process_job(job)


def test_worker_touches_heartbeat_file(tmp_path, sqlite_session_factory):
    """Verifies worker touches the liveness heartbeat file."""
    hb_file = tmp_path / "worker_alive"
    assert not hb_file.exists()

    worker = ScanWorker(
        session_factory=sqlite_session_factory,
        heartbeat_file=str(hb_file),
    )
    worker.touch_heartbeat()
    assert hb_file.exists()
