"""Unit Test Suite for Document Ingestion Pipeline (Stages 1–3).
Fast, in-memory & SQLite verification without requiring real PostgreSQL.
Updated for asynchronous architecture using ScanJobHandler and test helpers.
"""

import io
import os
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pypdf
import pytest
from sqlalchemy import create_engine, exc, text
from sqlalchemy.orm import Session
from starlette.testclient import TestClient

import src.api.v1.ingestion as ingestion_module
from src.api.v1.ingestion import get_document_repository, get_upload_service
from src.api.v1.router import app
from src.db.engine import get_db
from src.db.enums import AuditEventType, UserRole
from src.db.models import AuditLog, Base
from src.db.models import Document as DocORM
from src.modules.audit.service import AuditService
from src.modules.auth.dependencies import get_current_user
from src.modules.document_pipeline.models import (
    Classification,
    DocumentStatus,
    UploadRequest,
)
from src.modules.document_pipeline.models import Document as DocumentDTO
from src.modules.document_pipeline.repository import InMemoryDocumentRepository
from src.modules.document_pipeline.scan_job_handler import ScanJobHandler
from src.modules.document_pipeline.upload_service import UploadService
from src.modules.document_pipeline.validation import FileValidator
from src.storage.bucket_manager import BucketManager
from src.storage.object_storage import LocalFileSystemStorage
from tests.helpers import upload_and_process_sync

FIXTURES_DIR = Path(__file__).resolve().parent.parent / "fixtures" / "pdfs"


def _ensure_fixtures():
    """Generates test fixtures if they do not exist on disk."""
    FIXTURES_DIR.mkdir(parents=True, exist_ok=True)

    # 1. Standard Digital Policy
    p1 = FIXTURES_DIR / "01_standard_digital_policy.pdf"
    if not p1.exists():
        writer = pypdf.PdfWriter()
        writer.add_blank_page(width=595, height=842)
        writer.add_metadata({"/Title": "CAQM Statutory Directive 2026"})
        with open(p1, "wb") as f:
            writer.write(f)

    # 2. Corrupted Header
    p2 = FIXTURES_DIR / "05_corrupted_header_missing.pdf"
    if not p2.exists():
        with open(p2, "wb") as f:
            f.write(b"NOT_A_PDF_STREAM_1234567890\n%%EOF")

    # 3. Truncated EOF Trailer
    p3 = FIXTURES_DIR / "06_truncated_eof_missing.pdf"
    if not p3.exists():
        with open(p3, "wb") as f:
            f.write(b"%PDF-1.4\n1 0 obj\n<<>>\nendobj\nTRUNCATED_STREAM")

    # 4. Disguised Fake Binary
    p4 = FIXTURES_DIR / "07_disguised_fake_binary.pdf"
    if not p4.exists():
        with open(p4, "wb") as f:
            f.write(b"MZ\x90\x00\x03\x00\x00\x00\x04\x00\x00\x00\xff\xff\x00\x00\xb8\x00\x00\x00")

    # 5. Malicious Script Exploit
    p5 = FIXTURES_DIR / "08_malicious_script_exploit.pdf"
    if not p5.exists():
        writer = pypdf.PdfWriter()
        writer.add_blank_page(width=595, height=842)
        writer.add_js("app.alert('Malicious payload execution');")
        with open(p5, "wb") as f:
            writer.write(f)

    # 6. Encrypted/Locked
    p6 = FIXTURES_DIR / "10_password_protected.pdf"
    if not p6.exists():
        writer = pypdf.PdfWriter()
        writer.add_blank_page(width=595, height=842)
        writer.encrypt("topsecret_password_2026")
        with open(p6, "wb") as f:
            writer.write(f)


_ensure_fixtures()

# In-memory repository and test client for unit tests
_test_repo = InMemoryDocumentRepository()
_test_storage = LocalFileSystemStorage(base_dir="./storage_data/test_unit")
_test_buckets = BucketManager(storage=_test_storage)
_test_upload_service = UploadService(bucket_manager=_test_buckets, repository=_test_repo)

class _FakeUser:
    """Minimal stand-in for src.db.models.User — avoids hitting Postgres for unit tests."""

    def __init__(self, role: str = UserRole.ADMIN.value, user_id: uuid.UUID | None = None):
        self.user_id = user_id or uuid.uuid4()
        self.role = role
        self.is_active = True


_test_user = _FakeUser()

app.dependency_overrides[get_document_repository] = lambda: _test_repo
app.dependency_overrides[get_upload_service] = lambda: _test_upload_service
app.dependency_overrides[get_current_user] = lambda: _test_user

client = TestClient(app)


# ==============================================================================
# 1. CORE PIPELINE UNIT TESTS (Category A: End-to-end promotion & versioning)
# ==============================================================================

def test_valid_policy_pdf_promoted_to_raw(tmp_path):
    """Test standard PDF goes from quarantine to raw bucket with status AWAITING_CLASSIFICATION."""
    storage = LocalFileSystemStorage(base_dir=str(tmp_path))
    buckets = BucketManager(storage=storage)
    repo = InMemoryDocumentRepository()
    service = UploadService(bucket_manager=buckets, repository=repo)
    handler = ScanJobHandler(bucket_manager=buckets, repository=repo)

    with open(FIXTURES_DIR / "01_standard_digital_policy.pdf", "rb") as f:
        data = f.read()

    resp = upload_and_process_sync(
        upload_service=service,
        scan_handler=handler,
        filename="caqm_directive.pdf",
        data=data,
        request_meta=UploadRequest(classification=Classification.PUBLIC),
    )

    assert resp.status == DocumentStatus.AWAITING_CLASSIFICATION
    assert resp.checksum is not None
    assert resp.rejection_reason is None

    # Verify storage layout
    raw_key = f"{resp.checksum}.pdf"
    assert storage.object_exists(buckets.raw, raw_key)
    assert not storage.object_exists(buckets.quarantine, resp.quarantine_key)

    # Verify repository record
    doc = repo.get_by_id(resp.document_id)
    assert doc is not None
    assert doc.status == DocumentStatus.AWAITING_CLASSIFICATION
    assert doc.classification == Classification.PUBLIC
    assert doc.raw_path == f"{buckets.raw}/{raw_key}"


def test_sha256_deduplication_match(tmp_path):
    """Test duplicate PDF upload matches SHA-256 and marks status DUPLICATE."""
    storage = LocalFileSystemStorage(base_dir=str(tmp_path))
    buckets = BucketManager(storage=storage)
    repo = InMemoryDocumentRepository()
    service = UploadService(bucket_manager=buckets, repository=repo)
    handler = ScanJobHandler(bucket_manager=buckets, repository=repo)

    with open(FIXTURES_DIR / "01_standard_digital_policy.pdf", "rb") as f:
        data = f.read()

    # First upload & process
    res1 = upload_and_process_sync(service, handler, filename="doc1.pdf", data=data)
    assert res1.status == DocumentStatus.AWAITING_CLASSIFICATION

    # Second upload with same bytes processed through pipeline
    res2 = upload_and_process_sync(service, handler, filename="doc2.pdf", data=data)
    assert res2.status == DocumentStatus.DUPLICATE
    assert res2.checksum == res1.checksum


def test_document_versioning_and_superseding(tmp_path):
    """Test document version increment and transition to SUPERSEDED."""
    storage = LocalFileSystemStorage(base_dir=str(tmp_path))
    buckets = BucketManager(storage=storage)
    repo = InMemoryDocumentRepository()
    service = UploadService(bucket_manager=buckets, repository=repo)
    handler = ScanJobHandler(bucket_manager=buckets, repository=repo)

    # Generate valid PDF Version 1
    w1 = pypdf.PdfWriter()
    w1.add_blank_page(width=595, height=842)
    w1.add_metadata({"/Title": "Policy V1"})
    b1 = io.BytesIO()
    w1.write(b1)
    data_v1 = b1.getvalue()

    # Generate valid PDF Version 2
    w2 = pypdf.PdfWriter()
    w2.add_blank_page(width=595, height=842)
    w2.add_metadata({"/Title": "Policy V2"})
    b2 = io.BytesIO()
    w2.write(b2)
    data_v2 = b2.getvalue()

    # Upload Version 1
    res1 = upload_and_process_sync(service, handler, filename="delhi_policy.pdf", data=data_v1)
    doc_v1 = repo.get_by_id(res1.document_id)
    assert doc_v1.version == 1

    # Upload Version 2 superseding Version 1
    res2 = upload_and_process_sync(
        upload_service=service,
        scan_handler=handler,
        filename="delhi_policy.pdf",
        data=data_v2,
        request_meta=UploadRequest(
            supersedes_doc_id=res1.document_id,
            keep_previous_version=True,
        ),
    )
    doc_v2 = repo.get_by_id(res2.document_id)
    assert doc_v2.version == 2
    assert doc_v2.supersedes_id == doc_v1.id

    # Verify Version 1 is marked SUPERSEDED
    doc_v1_updated = repo.get_by_id(res1.document_id)
    assert doc_v1_updated.status == DocumentStatus.SUPERSEDED


# ==============================================================================
# 2. SECURITY & VALIDATION (Category A: Scan-time rejection paths)
# ==============================================================================

def test_reject_empty_zero_byte_file():
    """Test rejection of empty 0-byte file."""
    validator = FileValidator()
    res = validator.validate(b"")
    assert not res.is_valid
    assert "EMPTY_FILE" in res.rejection_reason


def test_reject_oversized_file():
    """Test 100MB ceiling rejection."""
    validator = FileValidator()
    huge_data = b"%PDF-1.4\n" + (b"0" * (101 * 1024 * 1024)) + b"\n%%EOF"
    res = validator.validate(huge_data)
    assert not res.is_valid
    assert "FILE_TOO_LARGE" in res.rejection_reason


def test_reject_non_pdf_files():
    """Test rejection of files that aren't real PDFs — wrong MIME, wrong magic bytes, disguised binary."""
    validator = FileValidator()

    # Case 1: Declared MIME type mismatch
    res_mime = validator.validate(b"%PDF-1.4\n%%EOF", declared_mime_type="image/png")
    assert not res_mime.is_valid
    assert "INVALID_MIME_TYPE" in res_mime.rejection_reason

    # Case 2: PNG magic bytes with .pdf extension
    png_header = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR"
    res_png = validator.validate(png_header)
    assert not res_png.is_valid
    assert "CORRUPTED_PDF_STRUCTURE" in res_png.rejection_reason

    # Case 3: EXE/MZ binary disguised as .pdf
    with open(FIXTURES_DIR / "07_disguised_fake_binary.pdf", "rb") as f:
        exe_data = f.read()
    res_exe = validator.validate(exe_data)
    assert not res_exe.is_valid
    assert "CORRUPTED_PDF_STRUCTURE" in res_exe.rejection_reason


def test_reject_corrupted_header_pdf(tmp_path):
    """Test rejection when missing %PDF- header."""
    buckets = BucketManager(storage=LocalFileSystemStorage(str(tmp_path)))
    repo = InMemoryDocumentRepository()
    service = UploadService(bucket_manager=buckets, repository=repo)
    handler = ScanJobHandler(bucket_manager=buckets, repository=repo)

    with open(FIXTURES_DIR / "05_corrupted_header_missing.pdf", "rb") as f:
        data = f.read()

    res = upload_and_process_sync(service, handler, "bad_header.pdf", data)
    assert res.status == DocumentStatus.REJECTED
    assert "CORRUPTED_PDF_STRUCTURE" in res.rejection_reason


def test_reject_truncated_eof_pdf(tmp_path):
    """Test rejection when missing %%EOF trailer."""
    buckets = BucketManager(storage=LocalFileSystemStorage(str(tmp_path)))
    repo = InMemoryDocumentRepository()
    service = UploadService(bucket_manager=buckets, repository=repo)
    handler = ScanJobHandler(bucket_manager=buckets, repository=repo)

    with open(FIXTURES_DIR / "06_truncated_eof_missing.pdf", "rb") as f:
        data = f.read()

    res = upload_and_process_sync(service, handler, "bad_eof.pdf", data)
    assert res.status == DocumentStatus.REJECTED
    assert "CORRUPTED_PDF_STRUCTURE" in res.rejection_reason


def test_reject_structural_threats(tmp_path):
    """Test threat scanner rejects PDFs containing /Launch, /JavaScript, powershell.exe markers."""
    buckets = BucketManager(storage=LocalFileSystemStorage(str(tmp_path)))
    repo = InMemoryDocumentRepository()
    service = UploadService(bucket_manager=buckets, repository=repo)
    handler = ScanJobHandler(bucket_manager=buckets, repository=repo)

    with open(FIXTURES_DIR / "08_malicious_script_exploit.pdf", "rb") as f:
        data = f.read()

    res = upload_and_process_sync(service, handler, "malicious.pdf", data)
    assert res.status == DocumentStatus.REJECTED
    assert "MALICIOUS_THREAT_DETECTED" in res.rejection_reason


def test_reject_encrypted_pdf(tmp_path):
    """Test password protected PDF rejection."""
    buckets = BucketManager(storage=LocalFileSystemStorage(str(tmp_path)))
    repo = InMemoryDocumentRepository()
    service = UploadService(bucket_manager=buckets, repository=repo)
    handler = ScanJobHandler(bucket_manager=buckets, repository=repo)

    with open(FIXTURES_DIR / "10_password_protected.pdf", "rb") as f:
        data = f.read()

    res = upload_and_process_sync(service, handler, "locked.pdf", data)
    assert res.status == DocumentStatus.REJECTED
    assert "ENCRYPTED_PDF" in res.rejection_reason


# ==============================================================================
# 3. BEHAVIOURAL SIDE-EFFECT TESTS (Category A & B)
# ==============================================================================

def test_quarantine_deleted_after_promotion(tmp_path):
    """After a valid PDF is promoted to raw, quarantine must contain zero objects for that key."""
    storage = LocalFileSystemStorage(base_dir=str(tmp_path))
    buckets = BucketManager(storage=storage)
    repo = InMemoryDocumentRepository()
    service = UploadService(bucket_manager=buckets, repository=repo)
    handler = ScanJobHandler(bucket_manager=buckets, repository=repo)

    with open(FIXTURES_DIR / "01_standard_digital_policy.pdf", "rb") as f:
        data = f.read()

    resp = upload_and_process_sync(service, handler, filename="promote_test.pdf", data=data)
    assert resp.status == DocumentStatus.AWAITING_CLASSIFICATION

    quarantine_key = resp.quarantine_key
    assert not storage.object_exists(buckets.quarantine, quarantine_key), \
        "Quarantine object was NOT purged after promotion — data leak risk."


def test_quarantine_deleted_after_rejection(tmp_path):
    """After a corrupt PDF is rejected, quarantine must be purged (no toxic file left behind)."""
    storage = LocalFileSystemStorage(base_dir=str(tmp_path))
    buckets = BucketManager(storage=storage)
    repo = InMemoryDocumentRepository()
    service = UploadService(bucket_manager=buckets, repository=repo)
    handler = ScanJobHandler(bucket_manager=buckets, repository=repo)

    with open(FIXTURES_DIR / "05_corrupted_header_missing.pdf", "rb") as f:
        data = f.read()

    resp = upload_and_process_sync(service, handler, filename="reject_cleanup.pdf", data=data)
    assert resp.status == DocumentStatus.REJECTED

    quarantine_key = resp.quarantine_key
    assert not storage.object_exists(buckets.quarantine, quarantine_key), \
        "Quarantine object was NOT purged after rejection — toxic file lingering."


def test_rejected_pdf_never_reaches_raw(tmp_path):
    """A rejected file must never exist in the raw bucket, period."""
    storage = LocalFileSystemStorage(base_dir=str(tmp_path))
    buckets = BucketManager(storage=storage)
    repo = InMemoryDocumentRepository()
    service = UploadService(bucket_manager=buckets, repository=repo)
    handler = ScanJobHandler(bucket_manager=buckets, repository=repo)

    with open(FIXTURES_DIR / "05_corrupted_header_missing.pdf", "rb") as f:
        data = f.read()
    resp = upload_and_process_sync(service, handler, filename="should_not_land_in_raw.pdf", data=data)
    assert resp.status == DocumentStatus.REJECTED

    raw_dir = os.path.join(str(tmp_path), buckets.raw)
    if os.path.exists(raw_dir):
        raw_files = os.listdir(raw_dir)
        assert len(raw_files) == 0, \
            f"Rejected file leaked into raw bucket! Found: {raw_files}"


def test_dedup_short_circuits_storage(tmp_path):
    """Duplicate upload must NOT create a second copy in raw — only one raw object should exist."""
    storage = LocalFileSystemStorage(base_dir=str(tmp_path))
    buckets = BucketManager(storage=storage)
    repo = InMemoryDocumentRepository()
    service = UploadService(bucket_manager=buckets, repository=repo)
    handler = ScanJobHandler(bucket_manager=buckets, repository=repo)

    with open(FIXTURES_DIR / "01_standard_digital_policy.pdf", "rb") as f:
        data = f.read()

    res1 = upload_and_process_sync(service, handler, filename="original.pdf", data=data)
    assert res1.status == DocumentStatus.AWAITING_CLASSIFICATION

    res2 = upload_and_process_sync(service, handler, filename="copy.pdf", data=data)
    assert res2.status == DocumentStatus.DUPLICATE

    raw_dir = os.path.join(str(tmp_path), buckets.raw)
    raw_files = os.listdir(raw_dir)
    assert len(raw_files) == 1, \
        f"Dedup failed to short-circuit: expected 1 raw object, found {len(raw_files)} — {raw_files}"


def test_audit_log_immutability():
    """Test that UPDATE on audit_log rows is rejected by SQLite trigger."""
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)

    with engine.connect() as conn:
        conn.execute(text("""
            CREATE TRIGGER trg_audit_log_immutable
            BEFORE UPDATE ON audit_log
            BEGIN
                SELECT RAISE(ABORT, 'AUDIT_LOG_IMMUTABLE: Updates to audit_log are forbidden.');
            END;
        """))
        conn.commit()

    doc_id = uuid.uuid4()
    corr_id = uuid.uuid4()

    with Session(engine) as session:
        entry = AuditService.log_event(
            db=session,
            document_id=doc_id,
            event_type=AuditEventType.DOCUMENT_QUARANTINED,
            details={"file_name": "immutable_test.pdf", "size": 2048},
            user_id="admin",
            correlation_id=corr_id,
        )
        event_id = entry.event_id

    with Session(engine) as session, pytest.raises(exc.IntegrityError, match="AUDIT_LOG_IMMUTABLE"):
        session.execute(
            text("UPDATE audit_log SET event_type = 'TAMPERED' WHERE event_id = :eid"),
            {"eid": event_id},
        )
        session.commit()


# ==============================================================================
# 4. FASTAPI INTEGRATION ENDPOINTS (Category B: Async API contract)
# ==============================================================================

def test_api_health():
    """Test GET /health returns 200 OK."""
    res = client.get("/health")
    assert res.status_code == 200
    assert res.json()["status"] == "healthy"


def test_api_upload_returns_202():
    """Test POST /upload returns 202 Accepted immediately with QUARANTINED status and status_url."""
    pdf_path = FIXTURES_DIR / "01_standard_digital_policy.pdf"
    with open(pdf_path, "rb") as f:
        res = client.post(
            "/api/v1/documents/upload",
            files=[("files", ("policy.pdf", f, "application/pdf"))],
            data={"classification": "PUBLIC"},
        )
    assert res.status_code == 202
    body = res.json()
    assert isinstance(body, list) and len(body) == 1
    assert body[0]["status"] == "QUARANTINED"
    assert "document_id" in body[0]
    assert "status_url" in body[0]

    doc_id = body[0]["document_id"]
    status_res = client.get(f"/api/v1/documents/{doc_id}/status")
    assert status_res.status_code == 200
    assert status_res.json()["status"] == "QUARANTINED"


def test_api_upload_multiple_files_share_batch_id():
    """Test uploading several files in one request returns one result per file with a shared upload_batch_id."""
    pdf_path = FIXTURES_DIR / "01_standard_digital_policy.pdf"
    with open(pdf_path, "rb") as f1, open(pdf_path, "rb") as f2:
        data1 = f1.read()
        data2 = f2.read()

    res = client.post(
        "/api/v1/documents/upload",
        files=[
            ("files", ("multi_a.pdf", data1, "application/pdf")),
            ("files", ("multi_b.pdf", data2, "application/pdf")),
        ],
    )
    assert res.status_code == 202
    body = res.json()
    assert len(body) == 2
    assert body[0]["upload_batch_id"] is not None
    assert body[0]["upload_batch_id"] == body[1]["upload_batch_id"]


def test_api_upload_rejection_422():
    """Test POST /upload with empty 0-byte payload is rejected on fast-path with HTTP 422."""
    res = client.post(
        "/api/v1/documents/upload",
        files=[("files", ("empty.pdf", b"", "application/pdf"))],
    )
    assert res.status_code == 422
    assert "EMPTY_FILE" in res.json()["detail"]


def test_api_upload_unauthenticated_rejected_401():
    """Test POST /upload without a bearer token is rejected — auth is mandatory, not optional."""
    app.dependency_overrides.pop(get_current_user, None)
    try:
        res = client.post(
            "/api/v1/documents/upload",
            files=[("files", ("policy.pdf", b"%PDF-1.4\n%%EOF", "application/pdf"))],
        )
        assert res.status_code == 401
    finally:
        app.dependency_overrides[get_current_user] = lambda: _test_user


def test_api_list_documents():
    """Test GET /api/v1/documents lists repository documents (paginated envelope)."""
    res = client.get("/api/v1/documents")
    assert res.status_code == 200
    body = res.json()
    assert "documents" in body and isinstance(body["documents"], list)
    assert "total" in body


# ==============================================================================
# 5. DB CONSTRAINT TESTS
# ==============================================================================

def test_audit_service_logging():
    """Test AuditService correctly creates immutable AuditLog entries."""
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)

    doc_id = uuid.uuid4()
    corr_id = uuid.uuid4()

    with Session(engine) as session:
        entry = AuditService.log_event(
            db=session,
            document_id=doc_id,
            event_type=AuditEventType.DOCUMENT_QUARANTINED,
            details={"file_name": "directive.pdf", "size": 1024},
            user_id="user_admin",
            correlation_id=corr_id,
        )
        assert entry.event_id is not None
        assert entry.document_id == doc_id
        assert entry.event_type == AuditEventType.DOCUMENT_QUARANTINED.value
        assert entry.correlation_id == corr_id
        assert entry.details["file_name"] == "directive.pdf"


def test_check_constraint_rejects_invalid_enum_values():
    """Test that CheckConstraints reject invalid status strings."""
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)

    with Session(engine) as session:
        invalid_doc = DocORM(
            document_id=uuid.uuid4(),
            filename="invalid.pdf",
            file_size=100,
            status="GARBAGE_STATUS_INVALID",
        )
        session.add(invalid_doc)
        with pytest.raises(exc.IntegrityError):
            session.commit()


# ==============================================================================
# 6. CONCURRENCY & RACE CONDITIONS
# ==============================================================================

def test_concurrent_identical_uploads_only_one_promoted(tmp_path):
    """Test that N simultaneous uploads of the exact same PDF race safely when processed."""
    storage = LocalFileSystemStorage(base_dir=str(tmp_path))
    buckets = BucketManager(storage=storage)
    repo = InMemoryDocumentRepository()
    service = UploadService(bucket_manager=buckets, repository=repo)
    handler = ScanJobHandler(bucket_manager=buckets, repository=repo)

    with open(FIXTURES_DIR / "01_standard_digital_policy.pdf", "rb") as f:
        pdf_bytes = f.read()

    num_threads = 5
    barrier = threading.Barrier(num_threads)
    results = []

    def upload_worker(idx: int):
        barrier.wait()
        resp = upload_and_process_sync(
            upload_service=service,
            scan_handler=handler,
            filename=f"concurrent_doc_{idx}.pdf",
            data=pdf_bytes,
            request_meta=UploadRequest(classification=Classification.PUBLIC),
        )
        return resp

    with ThreadPoolExecutor(max_workers=num_threads) as pool:
        futures = [pool.submit(upload_worker, i) for i in range(num_threads)]
        results = [f.result() for f in futures]

    statuses = [r.status for r in results]
    assert statuses.count(DocumentStatus.AWAITING_CLASSIFICATION) == 1
    assert statuses.count(DocumentStatus.DUPLICATE) == num_threads - 1

    raw_dir = os.path.join(str(tmp_path), buckets.raw)
    raw_files = os.listdir(raw_dir)
    assert len(raw_files) == 1

    quarantine_dir = os.path.join(str(tmp_path), buckets.quarantine)
    quarantine_files = os.listdir(quarantine_dir) if os.path.exists(quarantine_dir) else []
    assert len(quarantine_files) == 0

    winner_doc_id = next(
        r.document_id for r in results if r.status == DocumentStatus.AWAITING_CLASSIFICATION
    )
    for r in results:
        if r.status == DocumentStatus.DUPLICATE:
            assert r.document_id == winner_doc_id
            assert r.was_duplicate is True


# ==============================================================================
# 7. AUDIT WIRING INTEGRATION (Category C: Audit event ordering)
# ==============================================================================

def test_upload_writes_audit_events_on_promotion(tmp_path):
    """Upload a valid PDF and verify audit_log has QUARANTINED, VALIDATION_PASSED, PROMOTED events in order."""
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)

    with Session(engine) as db:
        storage = LocalFileSystemStorage(base_dir=str(tmp_path))
        buckets = BucketManager(storage=storage)
        repo = InMemoryDocumentRepository()
        service = UploadService(bucket_manager=buckets, repository=repo, db_session=db)
        handler = ScanJobHandler(bucket_manager=buckets, repository=repo, db_session=db)

        with open(FIXTURES_DIR / "01_standard_digital_policy.pdf", "rb") as f:
            data = f.read()

        resp = upload_and_process_sync(service, handler, filename="audit_test.pdf", data=data)
        assert resp.status == DocumentStatus.AWAITING_CLASSIFICATION

        events = db.query(AuditLog).filter(
            AuditLog.document_id == resp.document_id
        ).order_by(AuditLog.event_id).all()

        event_types = [e.event_type for e in events]
        assert event_types == [
            AuditEventType.DOCUMENT_QUARANTINED.value,
            AuditEventType.VALIDATION_PASSED.value,
            AuditEventType.DOCUMENT_PROMOTED.value,
        ]

        quarantine_event = events[0]
        assert quarantine_event.details["filename"] == "audit_test.pdf"
        assert quarantine_event.details["size_bytes"] == len(data)

        promoted_event = events[2]
        assert "raw_path" in promoted_event.details
        assert "sha256" in promoted_event.details

        corr_ids = [e.correlation_id for e in events]
        assert all(c is not None for c in corr_ids)
        assert len({str(c) for c in corr_ids}) == 1


def test_upload_writes_audit_events_on_rejection(tmp_path):
    """Upload a corrupt PDF and verify audit_log has QUARANTINED and REJECTED events."""
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)

    with Session(engine) as db:
        storage = LocalFileSystemStorage(base_dir=str(tmp_path))
        buckets = BucketManager(storage=storage)
        repo = InMemoryDocumentRepository()
        service = UploadService(bucket_manager=buckets, repository=repo, db_session=db)
        handler = ScanJobHandler(bucket_manager=buckets, repository=repo, db_session=db)

        with open(FIXTURES_DIR / "05_corrupted_header_missing.pdf", "rb") as f:
            data = f.read()

        resp = upload_and_process_sync(service, handler, filename="corrupt_audit.pdf", data=data)
        assert resp.status == DocumentStatus.REJECTED

        events = db.query(AuditLog).filter(
            AuditLog.document_id == resp.document_id
        ).order_by(AuditLog.event_id).all()

        event_types = [e.event_type for e in events]
        assert event_types == [
            AuditEventType.DOCUMENT_QUARANTINED.value,
            AuditEventType.DOCUMENT_REJECTED.value,
        ]

        rejected_event = events[1]
        assert "rejection_reason" in rejected_event.details
        assert "CORRUPTED_PDF_STRUCTURE" in rejected_event.details["rejection_reason"]

        corr_ids = [e.correlation_id for e in events]
        assert all(c is not None for c in corr_ids)
        assert len({str(c) for c in corr_ids}) == 1


def test_accept_normal_pdf_with_reasonable_compression(tmp_path):
    """Regression guard: test that normal PDFs with standard stream compression are accepted."""
    buckets = BucketManager(storage=LocalFileSystemStorage(str(tmp_path)))
    repo = InMemoryDocumentRepository()
    service = UploadService(bucket_manager=buckets, repository=repo)
    handler = ScanJobHandler(bucket_manager=buckets, repository=repo)

    with open(FIXTURES_DIR / "01_standard_digital_policy.pdf", "rb") as f:
        pdf_data = f.read()

    res = upload_and_process_sync(service, handler, "normal_policy.pdf", pdf_data)
    assert res.status == DocumentStatus.AWAITING_CLASSIFICATION
    assert res.rejection_reason is None



# ==============================================================================
# 7. PERMANENT DELETE / PURGE TESTS (DELETE /documents/{id})
# ==============================================================================

@pytest.fixture
def purge_stack(tmp_path, monkeypatch):
    """Wires the API onto an isolated storage + repo + SQLite-audit stack.

    DELETE erases real objects and writes audit rows, so it can't run against the shared
    module-level fixtures. The endpoint reaches for the `_buckets` singleton directly (it is
    not an injectable dependency), hence the monkeypatch rather than an override.
    """
    storage = LocalFileSystemStorage(base_dir=str(tmp_path / "objects"))
    buckets = BucketManager(storage=storage)
    repo = InMemoryDocumentRepository()
    service = UploadService(bucket_manager=buckets, repository=repo)
    handler = ScanJobHandler(bucket_manager=buckets, repository=repo)

    engine = create_engine(f"sqlite:///{tmp_path / 'audit.db'}")
    Base.metadata.create_all(bind=engine)

    def _get_db():
        with Session(engine) as session:
            yield session

    monkeypatch.setattr(ingestion_module, "_buckets", buckets)
    app.dependency_overrides[get_document_repository] = lambda: repo
    app.dependency_overrides[get_upload_service] = lambda: service
    app.dependency_overrides[get_db] = _get_db
    try:
        yield SimpleNamespace(
            storage=storage, buckets=buckets, repo=repo,
            service=service, handler=handler, engine=engine,
        )
    finally:
        app.dependency_overrides[get_document_repository] = lambda: _test_repo
        app.dependency_overrides[get_upload_service] = lambda: _test_upload_service
        app.dependency_overrides[get_current_user] = lambda: _test_user
        app.dependency_overrides.pop(get_db, None)


def _promote_fixture(stack, filename="delete_me.pdf", owner_id=None):
    """Uploads + synchronously processes a valid fixture PDF, returning the terminal response."""
    with open(FIXTURES_DIR / "01_standard_digital_policy.pdf", "rb") as f:
        data = f.read()
    meta = UploadRequest(owner_id=owner_id or _test_user.user_id)
    resp = upload_and_process_sync(stack.service, stack.handler, filename, data, request_meta=meta)
    assert resp.status == DocumentStatus.AWAITING_CLASSIFICATION
    return resp


def test_delete_erases_object_from_storage(purge_stack):
    """DELETE must actually erase the promoted object — a delete that leaves bytes in the
    bucket makes the UI's "cannot be recovered" warning a lie."""
    resp = _promote_fixture(purge_stack)
    raw_key = f"{resp.checksum}.pdf"
    assert purge_stack.storage.object_exists(purge_stack.buckets.raw, raw_key), \
        "Precondition failed: object was never promoted to raw."

    res = client.delete(f"/api/v1/documents/{resp.document_id}")
    assert res.status_code == 200
    assert res.json()["permanent"] is True

    assert not purge_stack.storage.object_exists(purge_stack.buckets.raw, raw_key), \
        "Object still present in raw bucket after permanent delete."


def test_delete_tombstones_row_without_removing_it(purge_stack):
    """The row must survive as a tombstone (supersedes chains and audit rows reference this
    document_id) with purged_at/deleted_at stamped and sha256/raw_path cleared."""
    resp = _promote_fixture(purge_stack)
    client.delete(f"/api/v1/documents/{resp.document_id}")

    doc = purge_stack.repo.get_by_id(resp.document_id)
    assert doc is not None, "Row was hard-deleted — audit trail and version chains would dangle."
    assert doc.purged_at is not None
    assert doc.deleted_at is not None
    assert doc.checksum is None
    assert doc.raw_path is None


def test_delete_frees_sha256_for_reupload(purge_stack):
    """Nulling sha256 on purge is load-bearing: the dedup index is partial on
    `sha256 IS NOT NULL`, so an erased file must be re-uploadable rather than flagged
    DUPLICATE against a document whose bytes no longer exist."""
    first = _promote_fixture(purge_stack, filename="v1.pdf")
    client.delete(f"/api/v1/documents/{first.document_id}")

    second = _promote_fixture(purge_stack, filename="v1_again.pdf")
    assert second.was_duplicate is False, \
        "Re-upload after permanent delete was flagged DUPLICATE — purge did not free the hash."
    assert second.checksum == first.checksum


def test_delete_writes_permanent_audit_event(purge_stack):
    """The audit row is the only surviving record of what was destroyed, so it must capture
    the erased hash and filename."""
    resp = _promote_fixture(purge_stack)
    client.delete(f"/api/v1/documents/{resp.document_id}")

    with Session(purge_stack.engine) as session:
        events = (
            session.query(AuditLog)
            .filter(AuditLog.document_id == resp.document_id)
            .filter(AuditLog.event_type == AuditEventType.DOCUMENT_DELETED.value)
            .all()
        )
    assert len(events) == 1
    details = events[0].details
    assert details["permanent"] is True
    assert details["erased_sha256"] == resp.checksum
    assert details["erased_filename"] == "delete_me.pdf"


def test_delete_forbidden_for_non_owner_non_admin(purge_stack):
    """A plain USER who did not upload the document may not delete it — and nothing may be
    destroyed on the way to that 403."""
    resp = _promote_fixture(purge_stack, owner_id=uuid.uuid4())
    stranger = _FakeUser(role=UserRole.USER.value)
    app.dependency_overrides[get_current_user] = lambda: stranger

    res = client.delete(f"/api/v1/documents/{resp.document_id}")
    assert res.status_code == 403
    assert "FORBIDDEN" in res.json()["detail"]

    assert purge_stack.repo.get_by_id(resp.document_id).purged_at is None, \
        "Row was purged despite a 403."
    assert purge_stack.storage.object_exists(purge_stack.buckets.raw, f"{resp.checksum}.pdf"), \
        "Object was erased despite a 403."


def test_delete_allowed_for_owner_who_is_not_admin(purge_stack):
    """The uploader can delete their own document without being an ADMIN."""
    owner = _FakeUser(role=UserRole.USER.value)
    resp = _promote_fixture(purge_stack, owner_id=owner.user_id)
    app.dependency_overrides[get_current_user] = lambda: owner

    res = client.delete(f"/api/v1/documents/{resp.document_id}")
    assert res.status_code == 200
    assert purge_stack.repo.get_by_id(resp.document_id).purged_at is not None


def test_delete_allowed_for_admin_who_is_not_owner(purge_stack):
    """ADMINs can delete anyone's document — the second half of the owner-or-admin rule."""
    resp = _promote_fixture(purge_stack, owner_id=uuid.uuid4())
    res = client.delete(f"/api/v1/documents/{resp.document_id}")
    assert res.status_code == 200


def test_delete_twice_returns_404(purge_stack):
    """An already-purged document is indistinguishable from a missing one."""
    resp = _promote_fixture(purge_stack)
    assert client.delete(f"/api/v1/documents/{resp.document_id}").status_code == 200
    assert client.delete(f"/api/v1/documents/{resp.document_id}").status_code == 404


def test_download_after_delete_returns_410(purge_stack):
    """410 rather than 404 — the document demonstrably existed and was deliberately erased."""
    resp = _promote_fixture(purge_stack)
    client.delete(f"/api/v1/documents/{resp.document_id}")

    res = client.get(f"/api/v1/documents/{resp.document_id}/download")
    assert res.status_code == 410
    assert "GONE" in res.json()["detail"]


def test_purged_document_hidden_from_list(purge_stack):
    """Purged documents must disappear from list/search immediately."""
    resp = _promote_fixture(purge_stack)
    before = client.get("/api/v1/documents").json()["documents"]
    assert any(d["id"] == str(resp.document_id) for d in before)

    client.delete(f"/api/v1/documents/{resp.document_id}")

    after = client.get("/api/v1/documents").json()["documents"]
    assert not any(d["id"] == str(resp.document_id) for d in after)


def test_repo_purge_is_idempotent():
    """Repository-level: a second purge is a no-op returning False, so a retried delete
    can't double-stamp or crash."""
    repo = InMemoryDocumentRepository()
    doc = repo.create(
        DocumentDTO(
            filename="tombstone.pdf",
            size=470,
            checksum="a" * 64,
            status=DocumentStatus.AWAITING_CLASSIFICATION,
            raw_path=f"apag-raw/{'a' * 64}.pdf",
        )
    )
    assert repo.purge(doc.id) is True
    assert repo.purge(doc.id) is False


def test_repo_purge_returns_false_for_unknown_id():
    """Guards the endpoint's 404 path — purge must not invent rows."""
    assert InMemoryDocumentRepository().purge(uuid.uuid4()) is False
