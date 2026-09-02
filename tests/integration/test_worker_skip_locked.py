"""Integration tests for worker SKIP LOCKED concurrency and lease management under real PostgreSQL."""

import threading
import uuid
from datetime import datetime, timedelta, timezone
from concurrent.futures import ThreadPoolExecutor

from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from src.db.models import Document as DocumentORM, Job as JobORM
from src.workers.scan_worker import ScanWorker


def test_two_workers_do_not_pick_same_job(postgres_engine):
    """Verifies that under real PostgreSQL, two concurrent workers race safely with FOR UPDATE SKIP LOCKED
    and exactly ONE worker claims the single PENDING job.
    """
    SessionLocal = sessionmaker(bind=postgres_engine)
    doc_id = uuid.uuid4()
    job_id = uuid.uuid4()

    # 1. Insert Document and single PENDING Job
    with SessionLocal() as session:
        doc = DocumentORM(
            document_id=doc_id,
            filename="concurrency_test.pdf",
            file_size=1024,
            status="QUARANTINED",
        )
        job = JobORM(
            job_id=job_id,
            document_id=doc_id,
            stage="SCAN",
            status="PENDING",
            priority=10,
        )
        session.add(doc)
        session.add(job)
        session.commit()

    # 2. Spin up two ScanWorkers
    w1 = ScanWorker(session_factory=SessionLocal, worker_id="worker-alpha", lease_seconds=60)
    w2 = ScanWorker(session_factory=SessionLocal, worker_id="worker-beta", lease_seconds=60)

    barrier = threading.Barrier(2)
    claimed_results = []

    def worker_race(worker: ScanWorker):
        barrier.wait()
        job_item = worker.pick_job()
        return job_item

    with ThreadPoolExecutor(max_workers=2) as executor:
        f1 = executor.submit(worker_race, w1)
        f2 = executor.submit(worker_race, w2)
        claimed_results = [f1.result(), f2.result()]

    # 3. Assertions: Exactly one worker claimed the job, one received None
    claimed_jobs = [j for j in claimed_results if j is not None]
    assert len(claimed_jobs) == 1, f"Expected exactly 1 claim, got: {claimed_jobs}"
    assert claimed_results.count(None) == 1

    winner = claimed_jobs[0]
    assert winner.job_id == job_id
    assert winner.status == "RUNNING"
    assert winner.worker_id in ("worker-alpha", "worker-beta")

    # 4. Verify DB state has status=RUNNING and winner worker_id
    with SessionLocal() as session:
        final_job = session.query(JobORM).filter(JobORM.job_id == job_id).first()
        assert final_job.status == "RUNNING"
        assert final_job.worker_id == winner.worker_id
        assert final_job.started_at is not None
        assert final_job.lease_expires_at is not None

        # Clean up
        session.delete(final_job)
        session.delete(session.query(DocumentORM).filter(DocumentORM.document_id == doc_id).first())
        session.commit()
