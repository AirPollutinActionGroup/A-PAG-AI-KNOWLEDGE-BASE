"""Stage 2: Validation Service and Threat Scanner.
Performs fail-fast pre-checks:
1. MIME type validation (application/pdf)
2. File size ceiling (100 MB)
3. Magic bytes (%PDF-)
4. Trailer marker (%%EOF)
5. Threat scanning (ClamAV)
6. Password protection / encryption check
7. Bounded page count limit (<= 5000 pages)
8. SHA-256 calculation
"""

import hashlib
import io
import logging
from abc import ABC, abstractmethod
from typing import ClassVar

import pypdf

from src.modules.document_pipeline.models import ScanResult, ValidationResult

logger = logging.getLogger(__name__)


class ThreatScanner(ABC):
    """Abstract interface for malware/exploit scanners."""

    @abstractmethod
    def scan(self, data: bytes) -> ScanResult:
        """Scans byte payload for threats and malicious markers."""


class ClamAVScanner(ThreatScanner):
    """Local / Mock ClamAV scanner checking for exploits and malicious script actions."""

    MALICIOUS_SIGNATURES: ClassVar[list[bytes]] = [
        b"/Launch",
        b"/OpenAction",
        b"/JavaScript",
        b"/JS",
        b"powershell.exe",
        b"cmd.exe",
        b"EICAR-STANDARD-ANTIVIRUS-TEST-FILE!",
    ]

    def scan(self, data: bytes) -> ScanResult:
        detected = []
        for sig in self.MALICIOUS_SIGNATURES:
            if sig in data:
                detected.append(sig.decode("latin-1", errors="ignore"))

        if detected:
            logger.warning("Threat scan INFECTED: %s", detected)
            return ScanResult(
                passed=False,
                threats_detected=detected,
                details={"scanner": "ClamAV", "verdict": "INFECTED", "threats": detected},
            )

        logger.debug("Threat scan CLEAN")
        return ScanResult(
            passed=True,
            threats_detected=[],
            details={"scanner": "ClamAV", "verdict": "CLEAN"},
        )


class FileValidator:
    """Core fail-fast validator for uploaded PDF documents."""

    MAX_FILE_SIZE_BYTES = 100 * 1024 * 1024  # 100 MB
    MAX_PAGE_COUNT = 5000
    PDF_MAGIC_HEADER = b"%PDF-"
    PDF_EOF_TRAILER = b"%%EOF"

    def __init__(self, scanner: ThreatScanner | None = None):
        self.scanner = scanner or ClamAVScanner()

    def validate(
        self,
        data: bytes,
        declared_mime_type: str = "application/pdf",
    ) -> ValidationResult:
        size = len(data)

        # 1. Zero-byte check
        if size == 0:
            return ValidationResult(
                is_valid=False,
                file_size_bytes=0,
                mime_type=declared_mime_type,
                rejection_reason="EMPTY_FILE: Document contains 0 bytes.",
            )

        # 2. File size ceiling
        if size > self.MAX_FILE_SIZE_BYTES:
            return ValidationResult(
                is_valid=False,
                file_size_bytes=size,
                mime_type=declared_mime_type,
                rejection_reason=(
                    f"FILE_TOO_LARGE: Exceeds 100MB limit (Actual: {size / (1024*1024):.2f} MB)."
                ),
            )

        # 3. MIME type check
        if declared_mime_type != "application/pdf":
            return ValidationResult(
                is_valid=False,
                file_size_bytes=size,
                mime_type=declared_mime_type,
                rejection_reason=f"INVALID_MIME_TYPE: Expected 'application/pdf', got '{declared_mime_type}'.",
            )

        # 4. Magic Bytes (%PDF-)
        if not data.startswith(self.PDF_MAGIC_HEADER):
            return ValidationResult(
                is_valid=False,
                file_size_bytes=size,
                mime_type=declared_mime_type,
                rejection_reason="CORRUPTED_PDF_STRUCTURE: Missing '%PDF-' header marker.",
            )

        # 5. EOF Trailer (%%EOF)
        trailer_window = data[-1024:] if size >= 1024 else data
        if self.PDF_EOF_TRAILER not in trailer_window:
            return ValidationResult(
                is_valid=False,
                file_size_bytes=size,
                mime_type=declared_mime_type,
                rejection_reason="CORRUPTED_PDF_STRUCTURE: Missing '%%EOF' end-of-file trailer marker.",
            )

        # 6. Threat Scan
        scan_res = self.scanner.scan(data)
        if not scan_res.passed:
            return ValidationResult(
                is_valid=False,
                file_size_bytes=size,
                mime_type=declared_mime_type,
                rejection_reason=f"MALICIOUS_THREAT_DETECTED: Scanner found {scan_res.threats_detected}",
                scan_result=scan_res,
            )

        # 7. Encryption / Password Check & Bounded Page Count
        # Fast check for encryption marker in PDF dictionary
        if b"/Encrypt" in data:
            return ValidationResult(
                is_valid=False,
                file_size_bytes=size,
                mime_type=declared_mime_type,
                rejection_reason="ENCRYPTED_PDF: Password-protected or encrypted PDFs are not supported.",
            )

        try:
            reader = pypdf.PdfReader(io.BytesIO(data))
            if reader.is_encrypted:
                return ValidationResult(
                    is_valid=False,
                    file_size_bytes=size,
                    mime_type=declared_mime_type,
                    rejection_reason="ENCRYPTED_PDF: Password-protected or encrypted PDFs are not supported.",
                )

            page_count = len(reader.pages)
            if page_count > self.MAX_PAGE_COUNT:
                return ValidationResult(
                    is_valid=False,
                    file_size_bytes=size,
                    mime_type=declared_mime_type,
                    rejection_reason=(
                        f"PAGE_LIMIT_EXCEEDED: PDF has {page_count} pages (Max: {self.MAX_PAGE_COUNT})."
                    ),
                )
        except Exception as e:
            err_str = str(e).lower()
            if "encrypt" in err_str or "password" in err_str:
                return ValidationResult(
                    is_valid=False,
                    file_size_bytes=size,
                    mime_type=declared_mime_type,
                    rejection_reason="ENCRYPTED_PDF: Password-protected or encrypted PDFs are not supported.",
                )
            return ValidationResult(
                is_valid=False,
                file_size_bytes=size,
                mime_type=declared_mime_type,
                rejection_reason=f"CORRUPTED_PDF_STRUCTURE: Malformed internal PDF structure ({e!s}).",
            )

        # 8. SHA-256 Checksum Calculation
        sha256 = hashlib.sha256(data).hexdigest()
        logger.info(
            "Validation PASSED: size=%d pages=%d sha256=%s",
            size, page_count, sha256,
        )

        return ValidationResult(
            is_valid=True,
            sha256=sha256,
            page_count=page_count,
            file_size_bytes=size,
            mime_type="application/pdf",
            rejection_reason=None,
            scan_result=scan_res,
        )


class ValidationService:
    """Service wrapper for PDF validation."""

    def __init__(self, validator: FileValidator | None = None):
        self.validator = validator or FileValidator()

    def validate_document(
        self,
        data: bytes,
        mime_type: str = "application/pdf",
    ) -> ValidationResult:
        """Runs the complete suite of fail-fast validation checks."""
        return self.validator.validate(data, mime_type)
