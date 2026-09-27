"""Lease renewal — a job slower than its lease must not be mistaken for a dead worker.

The reaper recovers jobs whose worker died holding a lease, and infers death from an expired
lease. That inference quietly assumes every job finishes inside one lease period. Processing time
is a function of document size, so the assumption fails on exactly the documents that matter: a
1,351-page PDF in A-PAG's own corpus took 139s to extract against a 60s lease, and the reaper
returned the job to PENDING — incrementing `retry_count` — while the worker was succeeding. A
document slow enough to burn `max_retries` that way is marked FAILED with the work still running.

These tests pin the fix: while `process_job()` is running the lease keeps moving, so the reaper
leaves it alone; once the worker stops renewing, the reaper's recovery still works.
"""

import threading
import time
import uuid

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from src.db.models import Base
from src.db.models import Document as DocumentORM
from src.db.models import Job as JobORM
from src.workers.base_worker import BaseWorker, JobItem


@pytest.fixture
def session_factory():
    """A file-backed SQLite DB, not `:memory:` — the renewer runs on its own thread and opens its
    own session, which would not see an in-memory database belonging to another connection."""
    import tempfile
    from pathlib import Path

    tmp = Path(tempfile.mkdtemp()) / "lease.db"
    engine = create_engine(f"sqlite:///{tmp}")
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine)


class SlowWorker(BaseWorker):
    """Blocks inside process_job() until released, standing in for a large document."""

    def __init__(self, release: threading.Event, **kwargs):
        super().__init__(stage="SCAN", **kwargs)
        self._release = release
        self.entered = threading.Event()

    def process_job(self, job: JobItem) -> None:
        self.entered.set()
        self._release.wait(timeout=10.0)


def _seed(session_factory) -> uuid.UUID:
    doc_id, job_id = uuid.uuid4(), uuid.uuid4()
    with session_factory() as s:
        s.add_all([
            DocumentORM(document_id=doc_id, filename="big.pdf", file_size=21_620_478,
                        status="QUARANTINED"),
            JobORM(job_id=job_id, document_id=doc_id, stage="SCAN", status="PENDING"),
        ])
        s.commit()
    return job_id


def _lease_of(session_factory, job_id):
    """SQLAlchemy's `Uuid` type stores dash-less hex on SQLite, so that is what a raw query binds."""
    with session_factory() as s:
        return s.execute(
            text("SELECT lease_expires_at FROM jobs WHERE job_id = :j"), {"j": job_id.hex}
        ).scalar_one()


def test_lease_is_extended_while_the_job_is_still_running(session_factory):
    """The regression that matters: a job that outlives its lease keeps it, because the worker is
    demonstrably alive."""
    job_id = _seed(session_factory)
    release = threading.Event()
    worker = SlowWorker(
        release=release, session_factory=session_factory,
        worker_id="slow-1", lease_seconds=3,   # renew interval = 1s
    )

    job = worker.pick_job()
    assert job is not None
    first = _lease_of(session_factory, job_id)

    t = threading.Thread(target=worker.execute_job, args=(job,), daemon=True)
    t.start()
    assert worker.entered.wait(timeout=5.0), "process_job never started"

    time.sleep(2.5)  # longer than the renew interval, shorter than the test timeout
    extended = _lease_of(session_factory, job_id)
    assert extended > first, "lease must move forward while the job is still being worked on"

    release.set()
    t.join(timeout=5.0)


def test_a_job_slower_than_its_lease_is_not_reaped(session_factory):
    """End-to-end statement of the bug: run the reaper against a job that is still in progress and
    confirm it is left alone, with its retry count untouched."""
    job_id = _seed(session_factory)
    release = threading.Event()
    worker = SlowWorker(
        release=release, session_factory=session_factory,
        worker_id="slow-2", lease_seconds=3,
    )

    job = worker.pick_job()
    t = threading.Thread(target=worker.execute_job, args=(job,), daemon=True)
    t.start()
    assert worker.entered.wait(timeout=5.0)

    time.sleep(4.0)  # past the original lease; renewal should have carried it
    assert worker.reap_stuck_jobs() == 0, "a live, renewing worker must not be reaped"

    with session_factory() as s:
        status, retries = s.execute(
            text("SELECT status, retry_count FROM jobs WHERE job_id = :j"), {"j": job_id.hex}
        ).one()
    assert status == "RUNNING"
    assert retries == 0, "work that is progressing must not burn a retry"

    release.set()
    t.join(timeout=5.0)


def test_renewal_stops_once_the_job_finishes(session_factory):
    """The renewer must not outlive its job — a thread still extending the lease of a completed
    job would keep a dead worker's lease alive forever."""
    job_id = _seed(session_factory)
    release = threading.Event()
    release.set()  # return immediately
    worker = SlowWorker(
        release=release, session_factory=session_factory,
        worker_id="quick-1", lease_seconds=3,
    )

    worker.execute_job(worker.pick_job())

    with session_factory() as s:
        status, lease = s.execute(
            text("SELECT status, lease_expires_at FROM jobs WHERE job_id = :j"), {"j": job_id.hex}
        ).one()
    assert status == "COMPLETED"
    assert lease is None, "a finished job must not hold a lease"

    assert not any(
        t.name.startswith("lease-renew-") for t in threading.enumerate()
    ), "renewal thread outlived its job"


def test_a_genuinely_dead_worker_is_still_reaped(session_factory):
    """The guard must not disable recovery. Nothing renews this lease, so the reaper reclaims it
    exactly as before."""
    job_id = _seed(session_factory)
    worker = SlowWorker(
        release=threading.Event(), session_factory=session_factory,
        worker_id="dead-1", lease_seconds=3,
    )
    worker.pick_job()  # claimed, then the "worker" does nothing at all

    # Poll rather than sleep a fixed interval: SQLite's CURRENT_TIMESTAMP has one-second
    # resolution, so a 3s lease can land up to a second later than arithmetic suggests, and a
    # fixed sleep races it.
    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline:
        if worker.reap_stuck_jobs() == 1:
            break
        time.sleep(0.25)
    else:
        pytest.fail("an abandoned lease was never reaped")

    with session_factory() as s:
        status, retries = s.execute(
            text("SELECT status, retry_count FROM jobs WHERE job_id = :j"), {"j": job_id.hex}
        ).one()
    assert status == "PENDING"
    assert retries == 1
