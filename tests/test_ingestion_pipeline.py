"""Master Test Suite for Document Ingestion Pipeline (Stages 1–3).
Pure Ingestion Pipeline verification without extraction/normalization.
"""

import io
import uuid
from pathlib import Path

import pypdf
import pytest
from starlette.testclient import TestClient

from src.api.v1.router import app
from src.modules.document_pipeline.models import (
    Classification,
    DocumentStatus,
    UploadRequest,
)
from src.modules.document_pipeline.repository import InMemoryDocumentRepository
from src.modules.document_pipeline.upload_service import UploadService
from src.modules.document_pipeline.validation import (
    FileValidator,
)
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


def test_reject_disguised_fake_binary(tmp_path):
    """Test rejection of non-PDF binary masquerading as PDF."""
    service = UploadService(bucket_manager=BucketManager(storage=LocalFileSystemStorage(str(tmp_path))))
    with open(FIXTURES_DIR / "07_disguised_fake_binary.pdf", "rb") as f:
        data = f.read()

    res = service.upload("fake.pdf", data)
    assert res.status == DocumentStatus.REJECTED
    assert "CORRUPTED_PDF_STRUCTURE" in res.rejection_reason


def test_reject_malicious_script_exploit(tmp_path):
    """Test ClamAV scanner intercepts malicious scripts."""
    service = UploadService(bucket_manager=BucketManager(storage=LocalFileSystemStorage(str(tmp_path))))
    with open(FIXTURES_DIR / "08_malicious_script_exploit.pdf", "rb") as f:
        data = f.read()

    res = service.upload("malicious.pdf", data)
    assert res.status == DocumentStatus.REJECTED
    assert "MALICIOUS_THREAT_DETECTED" in res.rejection_reason


def test_reject_oversized_file_boundary():
    """Test 50MB ceiling rejection."""
    validator = FileValidator()
    huge_data = b"%PDF-1.4\n" + (b"0" * (51 * 1024 * 1024)) + b"\n%%EOF"
    res = validator.validate(huge_data)
    assert not res.is_valid
    assert "FILE_TOO_LARGE" in res.rejection_reason


def test_reject_empty_zero_byte_file():
    """Test rejection of empty 0-byte file."""
    validator = FileValidator()
    res = validator.validate(b"")
    assert not res.is_valid
    assert "EMPTY_FILE" in res.rejection_reason


def test_reject_invalid_mime_type():
    """Test rejection of invalid declared MIME type."""
    validator = FileValidator()
    res = validator.validate(b"%PDF-1.4\n%%EOF", declared_mime_type="image/png")
    assert not res.is_valid
    assert "INVALID_MIME_TYPE" in res.rejection_reason


def test_reject_encrypted_pdf(tmp_path):
    """Test password protected PDF rejection."""
    service = UploadService(bucket_manager=BucketManager(storage=LocalFileSystemStorage(str(tmp_path))))
    with open(FIXTURES_DIR / "10_password_protected.pdf", "rb") as f:
        data = f.read()

    res = service.upload("locked.pdf", data)
    assert res.status == DocumentStatus.REJECTED
    assert "ENCRYPTED_PDF" in res.rejection_reason


# ==============================================================================
# 2. FASTAPI INTEGRATION ENDPOINTS
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


def test_api_upload_restricted():
    """Test POST /upload with RESTRICTED classification."""
    pdf_path = FIXTURES_DIR / "01_standard_digital_policy.pdf"
    with open(pdf_path, "rb") as f:
        res = client.post(
            "/api/v1/documents/upload",
            files={"file": ("budget.pdf", f, "application/pdf")},
            data={"classification": "RESTRICTED"},
        )
    assert res.status_code == 202
    doc_id = res.json()["document_id"]
    status_res = client.get(f"/api/v1/documents/{doc_id}/status")
    assert status_res.json()["classification"] == Classification.RESTRICTED


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
# 3. PHASE A: DB MODELS & AUDIT SERVICE TESTS
# ==============================================================================

def test_audit_service_logging():
    """Test AuditService correctly creates immutable AuditLog entries."""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session

    from src.db.enums import AuditEventType
    from src.db.models import Base
    from src.modules.audit.service import AuditService

    # In-memory SQLite engine for fast unit test of audit service
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


def test_job_model_structure():
    """Test Job model defaults and queue fields."""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session

    from src.db.enums import JobStage, JobStatus
    from src.db.models import Base, Job
    from src.db.models import Document as DocORM

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)

    doc_id = uuid.uuid4()
    with Session(engine) as session:
        doc = DocORM(
            document_id=doc_id,
            filename="test.pdf",
            file_size=100,
        )
        session.add(doc)
        session.commit()

        job = Job(
            document_id=doc_id,
            stage=JobStage.SCAN.value,
            status=JobStatus.PENDING.value,
        )
        session.add(job)
        session.commit()
        session.refresh(job)

        assert job.stage == "SCAN"
        assert job.status == "PENDING"
        assert job.retry_count == 0
        assert job.max_retries == 3
        assert job.priority == 0
        assert job.scheduled_at is not None


def test_check_constraint_rejects_invalid_enum_values():
    """Test that CheckConstraints reject invalid status strings."""
    from sqlalchemy import create_engine, exc
    from sqlalchemy.orm import Session

    from src.db.models import Base
    from src.db.models import Document as DocORM

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
