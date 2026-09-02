"""Integration test verifying asynchronous upload contract (Commit 4)."""

import io
import time
import uuid
import pypdf
import pytest
from sqlalchemy.orm import Session, sessionmaker
from starlette.testclient import TestClient

from src.api.v1.ingestion import get_document_repository, get_upload_service
from src.api.v1.router import app
from src.db.engine import get_db
from src.db.models import Document as DocumentORM, Job as JobORM
from src.modules.document_pipeline.repository import PostgreSQLDocumentRepository
from src.modules.document_pipeline.upload_service import UploadService
from src.storage.bucket_manager import BucketManager
from src.storage.object_storage import LocalFileSystemStorage


def _generate_valid_pdf_stream() -> bytes:
    writer = pypdf.PdfWriter()
    writer.add_blank_page(width=595, height=842)
    writer.add_metadata({"/Title": f"Async Timing Test {uuid.uuid4()}"})
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


def test_upload_returns_202_before_scan_completes(postgres_engine, tmp_path):
    """Proves that POST /upload returns 202 Accepted immediately with status=QUARANTINED
    and enqueues a PENDING job in Postgres without synchronously executing the scan.
    """
    SessionLocal = sessionmaker(bind=postgres_engine)
    test_storage = BucketManager(storage=LocalFileSystemStorage(base_dir=str(tmp_path / "async_storage")))

    def override_db():
        with SessionLocal() as session:
            yield session

    def override_repo():
        session = SessionLocal()
        return PostgreSQLDocumentRepository(session)

    def override_upload_service():
        session = SessionLocal()
        repo = PostgreSQLDocumentRepository(session)
        return UploadService(bucket_manager=test_storage, repository=repo, db_session=session)

    app.dependency_overrides[get_db] = override_db
    app.dependency_overrides[get_document_repository] = override_repo
    app.dependency_overrides[get_upload_service] = override_upload_service

    try:
        client = TestClient(app)
        pdf_bytes = _generate_valid_pdf_stream()

        start_time = time.perf_counter()
        response = client.post(
            "/api/v1/documents/upload",
            files={"file": ("async_test.pdf", pdf_bytes, "application/pdf")},
        )
        elapsed = time.perf_counter() - start_time

        # 1. Immediate 202 Accepted in under 500ms
        assert response.status_code == 202
        assert elapsed < 0.500, f"Upload took too long: {elapsed:.3f}s"

        body = response.json()
        assert "document_id" in body
        assert body["status"] == "QUARANTINED"
        assert "status_url" in body
        assert "correlation_id" in body

        doc_id = uuid.UUID(body["document_id"])

        # 2. Verify Job is PENDING in Postgres (proves scan has NOT executed)
        with SessionLocal() as session:
            job = session.query(JobORM).filter(JobORM.document_id == doc_id).first()
            assert job is not None
            assert job.stage == "SCAN"
            assert job.status == "PENDING"
            assert job.started_at is None
            assert job.finished_at is None

        # 3. Verify GET /status returns QUARANTINED and latest_event
        status_resp = client.get(f"/api/v1/documents/{doc_id}/status")
        assert status_resp.status_code == 200
        status_body = status_resp.json()
        assert status_body["status"] == "QUARANTINED"
        assert status_body["latest_event"] is not None
        assert status_body["latest_event"]["event_type"] == "DOCUMENT_QUARANTINED"
    finally:
        app.dependency_overrides.clear()
        if "doc_id" in locals():
            with SessionLocal() as session:
                session.query(JobORM).filter(JobORM.document_id == doc_id).delete()
                session.query(DocumentORM).filter(DocumentORM.document_id == doc_id).delete()
                session.commit()
