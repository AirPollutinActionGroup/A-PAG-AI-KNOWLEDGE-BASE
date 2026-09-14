"""Stage 2: Validation Service and Threat Scanner.

Performs fail-fast pre-checks, in order:
1. Zero-byte check
2. File size ceiling (100 MB)
3. Format lookup against the registry in `formats.py`
4. That format's container check (is this plausibly a PDF / OOXML zip at all)
5. Threat scanning (Heuristic / ClamAV interface)
6. Deep structural inspection (PDF parse / OOXML inner parts)
7. SHA-256 calculation

Per-format knowledge lives in `formats.py`, not here — this module owns the shared ladder and the
threat scanner, and adding a format means adding a `FormatSpec`, not editing this file.

Decompression-bomb detection (stream expansion ratio) was tried and removed — see
KNOWN_DEBTS.md. It flagged legitimate highly-compressible content (solid-fill images, blank
pages, font glyph tables) as false positives regardless of how the ratio threshold or an
absolute-size floor was tuned. The 100MB file-size ceiling (check #2) remains the actual
bound on how much data any single upload can cause the worker to process.
"""

import hashlib
import logging
from abc import ABC, abstractmethod
from typing import ClassVar

from src.modules.document_pipeline.formats import (
    MAX_PDF_PAGES,
    PDF_MIME,
    describe_unsupported,
    spec_for,
)
from src.modules.document_pipeline.models import ScanResult, ValidationResult

logger = logging.getLogger(__name__)


class ThreatScanner(ABC):
    """Abstract interface for malware/exploit scanners."""

    @abstractmethod
    def scan(self, data: bytes) -> ScanResult:
        """Scans byte payload for threats and malicious markers."""


class ClamAVScanner(ThreatScanner):
    """Local / Mock ClamAV scanner checking for exploits and malicious script actions."""

    # /OpenAction and bare /JS are deliberately excluded: /OpenAction is a common, benign PDF
    # directive (e.g. "open at page 1, fit width") and /JS collides with unrelated binary stream
    # bytes. /JavaScript (the actual embedded-script marker) is kept.
    MALICIOUS_SIGNATURES: ClassVar[list[bytes]] = [
        b"/Launch",
        b"/JavaScript",
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
    """Core fail-fast validator for uploaded documents."""

    MAX_FILE_SIZE_BYTES = 100 * 1024 * 1024  # 100 MB
    MAX_PAGE_COUNT = MAX_PDF_PAGES

    def __init__(self, scanner: ThreatScanner | None = None):
        self.scanner = scanner or ClamAVScanner()

    def validate(
        self,
        data: bytes,
        declared_mime_type: str = PDF_MIME,
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

        # 3. Format lookup — the declared type decides which structural check runs, so an
        #    unsupported one cannot reach a validator that would misread it.
        spec = spec_for(declared_mime_type)
        if spec is None:
            return ValidationResult(
                is_valid=False,
                file_size_bytes=size,
                mime_type=declared_mime_type,
                rejection_reason=f"INVALID_MIME_TYPE: {describe_unsupported(data, declared_mime_type)}",
            )

        # 4. Container check — cheap confirmation that these bytes are the declared container, so
        #    a disguised binary is reported as corrupt rather than handed to a parser.
        container = spec.container_check(data, spec)
        if not container.ok:
            return ValidationResult(
                is_valid=False,
                file_size_bytes=size,
                mime_type=declared_mime_type,
                rejection_reason=container.rejection_reason,
            )

        # 5. Threat Scan — deliberately before the deep parse below, so a file carrying a known
        #    signature is reported as malicious rather than as whatever a parser chokes on first.
        scan_res = self.scanner.scan(data)
        if not scan_res.passed:
            return ValidationResult(
                is_valid=False,
                file_size_bytes=size,
                mime_type=declared_mime_type,
                rejection_reason=f"MALICIOUS_THREAT_DETECTED: Scanner found {scan_res.threats_detected}",
                scan_result=scan_res,
            )

        # 6. Deep structural inspection, and the source of the unit count.
        structural = spec.structural_check(data, spec)
        if not structural.ok:
            return ValidationResult(
                is_valid=False,
                file_size_bytes=size,
                mime_type=declared_mime_type,
                rejection_reason=structural.rejection_reason,
                scan_result=scan_res,
            )

        # 7. SHA-256 Checksum Calculation
        sha256 = hashlib.sha256(data).hexdigest()
        logger.info(
            "Validation PASSED: format=%s size=%d units=%s sha256=%s",
            spec.label, size, structural.unit_count, sha256,
        )

        return ValidationResult(
            is_valid=True,
            sha256=sha256,
            page_count=structural.unit_count,
            file_size_bytes=size,
            mime_type=spec.mime_type,
            rejection_reason=None,
            scan_result=scan_res,
        )


class ValidationService:
    """Service wrapper for document validation."""

    def __init__(self, validator: FileValidator | None = None):
        self.validator = validator or FileValidator()

    def validate_document(
        self,
        data: bytes,
        mime_type: str = PDF_MIME,
    ) -> ValidationResult:
        """Runs the complete suite of fail-fast validation checks."""
        return self.validator.validate(data, mime_type)
