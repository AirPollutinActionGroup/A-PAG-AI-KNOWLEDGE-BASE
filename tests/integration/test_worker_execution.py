"""PostgreSQL integration tests for worker lifecycle, retries, reaper, and idempotency."""

import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import io
import pypdf
import pytest
from sqlalchemy.orm import Session, sessionmaker

from src.core.errors import TransientProcessingError
from src.db.models import AuditLog as AuditORM, Document as DocumentORM, Job as JobORM
from src.modules.document_pipeline.models import DocumentStatus, ValidationResult
from src.storage.bucket_manager import BucketManager
from src.storage.object_storage import LocalFileSystemStorage
from src.workers.scan_worker import ScanWorker


def _make_valid_pdf_bytes(title: str | None = None) -> bytes:
    writer = pypdf.PdfWriter()
    writer.add_blank_page(width=595, height=842)
    writer.add_metadata({"/Title": title or f"Integration Test Document {uuid.uuid4()}"})
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


@pytest.fixture
def test_storage(tmp_path):
    storage = LocalFileSystemStorage(base_dir=str(tmp_path / "storage"))
    buckets = BucketManager(storage=storage)
    return buckets


@pytest.fixture(autouse=True)
def clean_jobs_tables(postgres_engine):
    """Ensures jobs and documents tables are clean before and after each test."""
    SessionLocal = sessionmaker(bind=postgres_engine)
    with SessionLocal() as session:
        session.query(JobORM).delete()
        session.query(DocumentORM).delete()
        session.commit()
    yield
    with SessionLocal() as session:
        session.query(JobORM).delete()
        session.query(DocumentORM).delete()
        session.commit()


def test_worker_completes_successful_job(postgres_engine, test_storage):
    """Verifies that a valid PDF job is claimed, processed, document promoted, and job marked COMPLETED."""
    SessionLocal = sessionmaker(bind=postgres_engine)
    doc_id = uuid.uuid4()
    job_id = uuid.uuid4()
    pdf_bytes = _make_valid_pdf_bytes()

    # Put in quarantine
    test_storage.storage.put_object(test_storage.quarantine, f"{doc_id}.pdf", pdf_bytes)

    with SessionLocal() as session:
        doc = DocumentORM(
            document_id=doc_id,
            filename="valid.pdf",
            file_size=len(pdf_bytes),
            status="QUARANTINED",
            quarantine_path=f"{test_storage.quarantine}/{doc_id}.pdf",
        )
        job = JobORM(
            job_id=job_id,
            document_id=doc_id,
            stage="SCAN",
            status="PENDING",
        )
        session.add_all([doc, job])
        session.commit()

    worker = ScanWorker(
        session_factory=SessionLocal,
        bucket_manager=test_storage,
        worker_id="test-scan-worker",
    )
    claimed = worker.run_once()

    assert claimed is not None
    assert claimed.job_id == job_id

    # Verify DB state
    with SessionLocal() as session:
        final_job = session.query(JobORM).filter(JobORM.job_id == job_id).first()
        final_doc = session.query(DocumentORM).filter(DocumentORM.document_id == doc_id).first()

        assert final_job.status == "COMPLETED"
        assert final_job.finished_at is not None
        assert final_doc.status == "AWAITING_CLASSIFICATION"
        assert final_doc.sha256 is not None
        assert final_doc.raw_path is not None


def test_worker_marks_permanent_failure_as_failed(postgres_engine, test_storage):
    """Verifies that a non-existent document results in job marked FAILED permanently with error message."""
    SessionLocal = sessionmaker(bind=postgres_engine)
    missing_doc_id = uuid.uuid4()
    job_id = uuid.uuid4()

    # Create orphan job without document in storage or db
    with SessionLocal() as session:
        doc = DocumentORM(
            document_id=missing_doc_id,
            filename="missing.pdf",
            file_size=1024,
            status="QUARANTINED",
        )
        job = JobORM(
            job_id=job_id,
            document_id=missing_doc_id,
            stage="SCAN",
            status="PENDING",
        )
        session.add_all([doc, job])
        session.commit()

    worker = ScanWorker(
        session_factory=SessionLocal,
        bucket_manager=test_storage,
        worker_id="test-fail-worker",
    )
    worker.run_once()

    with SessionLocal() as session:
        final_job = session.query(JobORM).filter(JobORM.job_id == job_id).first()
        assert final_job.status == "FAILED"
        assert final_job.finished_at is not None
        assert "STORAGE_ERROR" in final_job.error_message or "Failed" in final_job.error_message


def test_worker_retries_transient_failure(postgres_engine, test_storage):
    """Verifies that transient failures schedule backoff retry with status=PENDING and incremented retry_count."""
    SessionLocal = sessionmaker(bind=postgres_engine)
    doc_id = uuid.uuid4()
    job_id = uuid.uuid4()

    with SessionLocal() as session:
        doc = DocumentORM(
            document_id=doc_id,
            filename="transient.pdf",
            file_size=1024,
            status="QUARANTINED",
        )
        job = JobORM(
            job_id=job_id,
            document_id=doc_id,
            stage="SCAN",
            status="PENDING",
            retry_count=0,
        )
        session.add_all([doc, job])
        session.commit()

    # Mock process_job to simulate transient ClamAV / storage timeout
    worker = ScanWorker(
        session_factory=SessionLocal,
        bucket_manager=test_storage,
        backoff_base_seconds=5,
    )
    worker.process_job = MagicMock(side_effect=TransientProcessingError("ClamAV daemon timeout"))

    worker.run_once()

    with SessionLocal() as session:
        retried_job = session.query(JobORM).filter(JobORM.job_id == job_id).first()
        assert retried_job.status == "PENDING"
        assert retried_job.retry_count == 1
        assert retried_job.worker_id is None
        assert retried_job.lease_expires_at is None
        assert retried_job.scheduled_at > datetime.now(timezone.utc)
        assert "ClamAV daemon timeout" in retried_job.error_message


def test_worker_gives_up_after_max_retries(postgres_engine, test_storage):
    """Verifies that exceeding MAX_RETRIES transitions job to status=FAILED permanently."""
    SessionLocal = sessionmaker(bind=postgres_engine)
    doc_id = uuid.uuid4()
    job_id = uuid.uuid4()

    with SessionLocal() as session:
        doc = DocumentORM(
            document_id=doc_id,
            filename="exhaust.pdf",
            file_size=1024,
            status="QUARANTINED",
        )
        job = JobORM(
            job_id=job_id,
            document_id=doc_id,
            stage="SCAN",
            status="PENDING",
            retry_count=3,
            max_retries=3,
        )
        session.add_all([doc, job])
        session.commit()

    worker = ScanWorker(
        session_factory=SessionLocal,
        bucket_manager=test_storage,
        max_retries=3,
    )
    worker.process_job = MagicMock(side_effect=TransientProcessingError("Persistent network outage"))

    worker.run_once()

    with SessionLocal() as session:
        failed_job = session.query(JobORM).filter(JobORM.job_id == job_id).first()
        assert failed_job.status == "FAILED"
        assert failed_job.finished_at is not None
        assert "Max retries (3) exceeded" in failed_job.error_message


def test_reaper_reclaims_stuck_running_job(postgres_engine):
    """Verifies that the reaper finds stuck jobs with expired leases and resets them to PENDING."""
    SessionLocal = sessionmaker(bind=postgres_engine)
    doc_id = uuid.uuid4()
    job_id = uuid.uuid4()
    past_lease = datetime.now(timezone.utc) - timedelta(minutes=5)

    with SessionLocal() as session:
        doc = DocumentORM(
            document_id=doc_id,
            filename="stuck.pdf",
            file_size=1024,
            status="QUARANTINED",
        )
        job = JobORM(
            job_id=job_id,
            document_id=doc_id,
            stage="SCAN",
            status="RUNNING",
            worker_id="crashed-worker",
            lease_expires_at=past_lease,
            retry_count=0,
        )
        session.add_all([doc, job])
        session.commit()

    worker = ScanWorker(session_factory=SessionLocal)
    reaped_count = worker.reap_stuck_jobs()

    assert reaped_count >= 1

    with SessionLocal() as session:
        reclaimed_job = session.query(JobORM).filter(JobORM.job_id == job_id).first()
        assert reclaimed_job.status == "PENDING"
        assert reclaimed_job.retry_count == 1
        assert reclaimed_job.worker_id is None
        assert reclaimed_job.lease_expires_at is None


def test_reaper_ignores_active_running_job(postgres_engine):
    """Verifies that the reaper does NOT touch active jobs whose leases are still valid in the future."""
    SessionLocal = sessionmaker(bind=postgres_engine)
    doc_id = uuid.uuid4()
    job_id = uuid.uuid4()
    future_lease = datetime.now(timezone.utc) + timedelta(minutes=5)

    with SessionLocal() as session:
        doc = DocumentORM(
            document_id=doc_id,
            filename="active.pdf",
            file_size=1024,
            status="QUARANTINED",
        )
        job = JobORM(
            job_id=job_id,
            document_id=doc_id,
            stage="SCAN",
            status="RUNNING",
            worker_id="live-worker",
            lease_expires_at=future_lease,
            retry_count=0,
        )
        session.add_all([doc, job])
        session.commit()

    worker = ScanWorker(session_factory=SessionLocal)
    worker.reap_stuck_jobs()

    with SessionLocal() as session:
        active_job = session.query(JobORM).filter(JobORM.job_id == job_id).first()
        assert active_job.status == "RUNNING"
        assert active_job.worker_id == "live-worker"
        assert active_job.retry_count == 0


def test_worker_idempotent_on_retry(postgres_engine, test_storage):
    """Verifies that running the worker twice on the same document produces no duplicate audits or raw files."""
    SessionLocal = sessionmaker(bind=postgres_engine)
    doc_id = uuid.uuid4()
    job_id1 = uuid.uuid4()
    job_id2 = uuid.uuid4()
    pdf_bytes = _make_valid_pdf_bytes()

    test_storage.storage.put_object(test_storage.quarantine, f"{doc_id}.pdf", pdf_bytes)

    with SessionLocal() as session:
        doc = DocumentORM(
            document_id=doc_id,
            filename="idempotent.pdf",
            file_size=len(pdf_bytes),
            status="QUARANTINED",
            quarantine_path=f"{test_storage.quarantine}/{doc_id}.pdf",
        )
        job1 = JobORM(job_id=job_id1, document_id=doc_id, stage="SCAN", status="PENDING")
        job2 = JobORM(job_id=job_id2, document_id=doc_id, stage="SCAN", status="PENDING")
        session.add_all([doc, job1, job2])
        session.commit()

    worker = ScanWorker(session_factory=SessionLocal, bucket_manager=test_storage)

    # First attempt: processes and promotes document
    worker.run_once()
    # Second attempt (simulating duplicate job pickup / retry)
    worker.run_once()

    with SessionLocal() as session:
        # Both jobs should reach terminal state
        j1 = session.query(JobORM).filter(JobORM.job_id == job_id1).first()
        j2 = session.query(JobORM).filter(JobORM.job_id == job_id2).first()
        assert j1.status == "COMPLETED"
        assert j2.status == "COMPLETED"

        # Check audit log: DOCUMENT_PROMOTED must appear exactly ONCE
        promoted_audits = (
            session.query(AuditORM)
            .filter(AuditORM.document_id == doc_id, AuditORM.event_type == "DOCUMENT_PROMOTED")
            .all()
        )
        assert len(promoted_audits) == 1, f"Expected 1 DOCUMENT_PROMOTED audit, found {len(promoted_audits)}"
