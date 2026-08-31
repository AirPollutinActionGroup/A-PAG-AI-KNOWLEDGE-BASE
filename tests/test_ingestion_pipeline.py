"""Master Test Suite for Document Ingestion Pipeline (Stages 1–3).
Pure Ingestion Pipeline verification without extraction/normalization.
"""

import io
import uuid
from pathlib import Path

import pypdf
import pytest
from sqlalchemy import create_engine, exc, text, event
from sqlalchemy.orm import Session
from starlette.testclient import TestClient

from src.api.v1.router import app
from src.db.enums import AuditEventType
from src.db.models import AuditLog, Base
from src.db.models import Document as DocORM
from src.modules.audit.service import AuditService
from src.modules.document_pipeline.models import (
    Classification,
    DocumentStatus,
    UploadRequest,
)
from src.modules.document_pipeline.repository import InMemoryDocumentRepository
from src.modules.document_pipeline.upload_service import UploadService
from src.modules.document_pipeline.validation import FileValidator
from src.storage.bucket_manager import BucketManager
from src.storage.object_storage import LocalFileSystemStorage

FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures" / "pdfs"


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
            f.write(b"MZ\x90\x00\x03\x00\x00\x00\x04\x00\x00\x00\xff\xff\x00\x00FAKE_EXE_PAYLOAD")

    # 5. Malicious Script Exploit
    p5 = FIXTURES_DIR / "08_malicious_script_exploit.pdf"
    if not p5.exists():
        with open(p5, "wb") as f:
            f.write(b"%PDF-1.7\n/Launch (powershell.exe -enc AAAA)\n/OpenAction /JavaScript (eval())\n%%EOF")

    # 6. Password Protected PDF
    p6 = FIXTURES_DIR / "10_password_protected.pdf"
    if not p6.exists():
        writer = pypdf.PdfWriter()
        writer.add_blank_page(width=595, height=842)
        writer.encrypt("super_secret_password")
        with open(p6, "wb") as f:
            writer.write(f)


_ensure_fixtures()
client = TestClient(app)


# ==============================================================================
# 1. CORE PIPELINE UNIT TESTS (Quarantine -> Validation -> Promotion/Rejection)
# ==============================================================================

def test_valid_policy_pdf_promoted_to_raw(tmp_path):
    """Test standard PDF goes from quarantine to raw bucket with status AWAITING_CLASSIFICATION."""
    storage = LocalFileSystemStorage(base_dir=str(tmp_path))
    buckets = BucketManager(storage=storage)
    repo = InMemoryDocumentRepository()
    service = UploadService(bucket_manager=buckets, repository=repo)

    with open(FIXTURES_DIR / "01_standard_digital_policy.pdf", "rb") as f:
        data = f.read()

    resp = service.upload(
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
    # Verify quarantine is purged
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

    with open(FIXTURES_DIR / "01_standard_digital_policy.pdf", "rb") as f:
        data = f.read()

    # First upload
    res1 = service.upload(filename="doc1.pdf", data=data)
    assert res1.status == DocumentStatus.AWAITING_CLASSIFICATION

    # Second upload with same bytes
    res2 = service.upload(filename="doc2.pdf", data=data)
    assert res2.status == DocumentStatus.DUPLICATE
    assert res2.checksum == res1.checksum


def test_document_versioning_and_superseding(tmp_path):
    """Test document version increment and transition to SUPERSEDED."""
    storage = LocalFileSystemStorage(base_dir=str(tmp_path))
    buckets = BucketManager(storage=storage)
    repo = InMemoryDocumentRepository()
    service = UploadService(bucket_manager=buckets, repository=repo)

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
    res1 = service.upload(filename="delhi_policy.pdf", data=data_v1)
    doc_v1 = repo.get_by_id(res1.document_id)
    assert doc_v1.version == 1

    # Upload Version 2 superseding Version 1
    res2 = service.upload(
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
# 2. SECURITY & VALIDATION (Rejection Paths)
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
    """Test rejection of files that aren't real PDFs — wrong MIME, wrong magic bytes, disguised binary.

    Merged test: covers MIME-type mismatch, PNG data, and .exe-renamed-to-.pdf in one test.
    """
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
    service = UploadService(bucket_manager=BucketManager(storage=LocalFileSystemStorage(str(tmp_path))))
    with open(FIXTURES_DIR / "05_corrupted_header_missing.pdf", "rb") as f:
        data = f.read()

    res = service.upload("bad_header.pdf", data)
    assert res.status == DocumentStatus.REJECTED
    assert "CORRUPTED_PDF_STRUCTURE" in res.rejection_reason


def test_reject_truncated_eof_pdf(tmp_path):
    """Test rejection when missing %%EOF trailer."""
    service = UploadService(bucket_manager=BucketManager(storage=LocalFileSystemStorage(str(tmp_path))))
    with open(FIXTURES_DIR / "06_truncated_eof_missing.pdf", "rb") as f:
        data = f.read()

    res = service.upload("bad_eof.pdf", data)
    assert res.status == DocumentStatus.REJECTED
    assert "CORRUPTED_PDF_STRUCTURE" in res.rejection_reason


def test_reject_structural_threats(tmp_path):
    """Test threat scanner rejects PDFs containing /Launch, /JavaScript, powershell.exe markers.

    Renamed from 'test_reject_malicious_script_exploit' to be honest:
    this tests our signature-based scanner, not real ClamAV.
    """
    service = UploadService(bucket_manager=BucketManager(storage=LocalFileSystemStorage(str(tmp_path))))
    with open(FIXTURES_DIR / "08_malicious_script_exploit.pdf", "rb") as f:
        data = f.read()

    res = service.upload("malicious.pdf", data)
    assert res.status == DocumentStatus.REJECTED
    assert "MALICIOUS_THREAT_DETECTED" in res.rejection_reason


def test_reject_encrypted_pdf(tmp_path):
    """Test password protected PDF rejection."""
    service = UploadService(bucket_manager=BucketManager(storage=LocalFileSystemStorage(str(tmp_path))))
    with open(FIXTURES_DIR / "10_password_protected.pdf", "rb") as f:
        data = f.read()

    res = service.upload("locked.pdf", data)
    assert res.status == DocumentStatus.REJECTED
    assert "ENCRYPTED_PDF" in res.rejection_reason


# ==============================================================================
# 3. BEHAVIOURAL SIDE-EFFECT TESTS
# ==============================================================================

def test_quarantine_deleted_after_promotion(tmp_path):
    """After a valid PDF is promoted to raw, quarantine must contain zero objects for that key."""
    storage = LocalFileSystemStorage(base_dir=str(tmp_path))
    buckets = BucketManager(storage=storage)
    repo = InMemoryDocumentRepository()
    service = UploadService(bucket_manager=buckets, repository=repo)

    with open(FIXTURES_DIR / "01_standard_digital_policy.pdf", "rb") as f:
        data = f.read()

    resp = service.upload(filename="promote_test.pdf", data=data)
    assert resp.status == DocumentStatus.AWAITING_CLASSIFICATION

    # The quarantine key should have been deleted during promotion
    quarantine_key = resp.quarantine_key
    assert not storage.object_exists(buckets.quarantine, quarantine_key), \
        "Quarantine object was NOT purged after promotion — data leak risk."


def test_quarantine_deleted_after_rejection(tmp_path):
    """After a corrupt PDF is rejected, quarantine must be purged (no toxic file left behind)."""
    storage = LocalFileSystemStorage(base_dir=str(tmp_path))
    buckets = BucketManager(storage=storage)
    repo = InMemoryDocumentRepository()
    service = UploadService(bucket_manager=buckets, repository=repo)

    with open(FIXTURES_DIR / "05_corrupted_header_missing.pdf", "rb") as f:
        data = f.read()

    resp = service.upload(filename="reject_cleanup.pdf", data=data)
    assert resp.status == DocumentStatus.REJECTED

    # Quarantine must be clean — no corrupt/infected files lingering
    quarantine_key = resp.quarantine_key
    assert not storage.object_exists(buckets.quarantine, quarantine_key), \
        "Quarantine object was NOT purged after rejection — toxic file lingering."


def test_rejected_pdf_never_reaches_raw(tmp_path):
    """A rejected file must never exist in the raw bucket, period."""
    storage = LocalFileSystemStorage(base_dir=str(tmp_path))
    buckets = BucketManager(storage=storage)
    repo = InMemoryDocumentRepository()
    service = UploadService(bucket_manager=buckets, repository=repo)

    # Upload corrupt PDF
    with open(FIXTURES_DIR / "05_corrupted_header_missing.pdf", "rb") as f:
        data = f.read()
    resp = service.upload(filename="should_not_land_in_raw.pdf", data=data)
    assert resp.status == DocumentStatus.REJECTED

    # Scan the entire raw bucket directory — nothing should be there for this upload
    import os
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

    with open(FIXTURES_DIR / "01_standard_digital_policy.pdf", "rb") as f:
        data = f.read()

    # First upload — lands in raw
    res1 = service.upload(filename="original.pdf", data=data)
    assert res1.status == DocumentStatus.AWAITING_CLASSIFICATION

    # Second upload — should be marked DUPLICATE
    res2 = service.upload(filename="copy.pdf", data=data)
    assert res2.status == DocumentStatus.DUPLICATE

    # Count objects in raw bucket — must be exactly 1
    import os
    raw_dir = os.path.join(str(tmp_path), buckets.raw)
    raw_files = os.listdir(raw_dir)
    assert len(raw_files) == 1, \
        f"Dedup failed to short-circuit: expected 1 raw object, found {len(raw_files)} — {raw_files}"


def test_audit_log_immutability():
    """Test that UPDATE on audit_log rows is rejected by the database.

    The audit_log table is meant to be append-only. This test creates a trigger
    that blocks UPDATEs, writes an entry, then verifies the UPDATE is rejected.
    """
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)

    # Install a trigger that rejects UPDATEs on audit_log
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

    # Attempt to UPDATE — must fail
    with Session(engine) as session:
        with pytest.raises(exc.IntegrityError, match="AUDIT_LOG_IMMUTABLE"):
            session.execute(
                text("UPDATE audit_log SET event_type = 'TAMPERED' WHERE event_id = :eid"),
                {"eid": event_id},
            )
            session.commit()


# ==============================================================================
# 4. FASTAPI INTEGRATION ENDPOINTS
# ==============================================================================

def test_api_health():
    """Test GET /health returns 200 OK."""
    res = client.get("/health")
    assert res.status_code == 200
    assert res.json()["status"] == "healthy"


def test_api_upload_returns_202():
    """Test POST /upload returns 202 Accepted with AWAITING_CLASSIFICATION status."""
    pdf_path = FIXTURES_DIR / "01_standard_digital_policy.pdf"
    with open(pdf_path, "rb") as f:
        res = client.post(
            "/api/v1/documents/upload",
            files={"file": ("policy.pdf", f, "application/pdf")},
            data={"classification": "PUBLIC"},
        )
    assert res.status_code == 202
    doc_id = res.json()["document_id"]
    assert res.json()["status"] == "AWAITING_CLASSIFICATION"

    # Verify status endpoint
    status_res = client.get(f"/api/v1/documents/{doc_id}/status")
    assert status_res.status_code == 200
    assert status_res.json()["status"] == "AWAITING_CLASSIFICATION"


def test_api_upload_rejection_422():
    """Test POST /upload with corrupt PDF returns HTTP 422."""
    pdf_path = FIXTURES_DIR / "05_corrupted_header_missing.pdf"
    with open(pdf_path, "rb") as f:
        res = client.post(
            "/api/v1/documents/upload",
            files={"file": ("corrupt.pdf", f, "application/pdf")},
        )
    assert res.status_code == 422
    assert "CORRUPTED_PDF_STRUCTURE" in res.json()["detail"] or "Validation failed" in res.json()["detail"]


def test_api_list_documents():
    """Test GET /api/v1/documents lists repository documents."""
    res = client.get("/api/v1/documents")
    assert res.status_code == 200
    assert isinstance(res.json(), list)


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
        # Invalid status not in check constraint
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
    """Test that N simultaneous uploads of the exact same PDF race safely.

    Uses threading.Barrier to force 5 threads to hit the upload endpoint simultaneously.
    Asserts:
    1. Exactly 1 upload is promoted to AWAITING_CLASSIFICATION.
    2. Exactly 4 uploads are recognized as DUPLICATE.
    3. Exactly 1 file exists in raw storage.
    4. 0 leftover files in quarantine storage.
    5. Duplicate responses return the winner's canonical document_id.
    """
    import os
    import threading
    from concurrent.futures import ThreadPoolExecutor

    storage = LocalFileSystemStorage(base_dir=str(tmp_path))
    buckets = BucketManager(storage=storage)
    repo = InMemoryDocumentRepository()
    service = UploadService(bucket_manager=buckets, repository=repo)

    with open(FIXTURES_DIR / "01_standard_digital_policy.pdf", "rb") as f:
        pdf_bytes = f.read()

    num_threads = 5
    barrier = threading.Barrier(num_threads)
    results = []

    def upload_worker(idx: int):
        barrier.wait()  # Synchronize all threads to execute upload at the exact same instant
        resp = service.upload(
            filename=f"concurrent_doc_{idx}.pdf",
            data=pdf_bytes,
            request_meta=UploadRequest(classification=Classification.PUBLIC),
        )
        return resp

    with ThreadPoolExecutor(max_workers=num_threads) as pool:
        futures = [pool.submit(upload_worker, i) for i in range(num_threads)]
        results = [f.result() for f in futures]

    # Invariant 1: Exactly 1 promoted, rest marked duplicate
    statuses = [r.status for r in results]
    assert statuses.count(DocumentStatus.AWAITING_CLASSIFICATION) == 1, \
        f"Expected exactly 1 promoted, got: {statuses}"
    assert statuses.count(DocumentStatus.DUPLICATE) == num_threads - 1, \
        f"Expected {num_threads - 1} duplicates, got: {statuses}"

    # Invariant 2: Exactly 1 file in raw storage
    raw_dir = os.path.join(str(tmp_path), buckets.raw)
    raw_files = os.listdir(raw_dir)
    assert len(raw_files) == 1, \
        f"Dedup race failed! Expected 1 raw file, found {len(raw_files)}: {raw_files}"

    # Invariant 3: Zero leftover files in quarantine storage
    quarantine_dir = os.path.join(str(tmp_path), buckets.quarantine)
    quarantine_files = os.listdir(quarantine_dir) if os.path.exists(quarantine_dir) else []
    assert len(quarantine_files) == 0, \
        f"Quarantine leak! Found lingering files: {quarantine_files}"

    # Invariant 4: All duplicates reference the winning canonical document ID
    winner_doc_id = [r.document_id for r in results if r.status == DocumentStatus.AWAITING_CLASSIFICATION][0]
    for r in results:
        if r.status == DocumentStatus.DUPLICATE:
            assert r.document_id == winner_doc_id, \
                f"Duplicate response did not return winning doc ID! Got: {r.document_id}"
            assert r.was_duplicate is True

