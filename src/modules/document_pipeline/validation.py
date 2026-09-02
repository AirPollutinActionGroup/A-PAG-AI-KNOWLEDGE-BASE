"""Stage 2: Validation Service and Threat Scanner.
Performs fail-fast pre-checks:
1. MIME type validation (application/pdf)
2. File size ceiling (100 MB)
3. Magic bytes (%PDF-)
4. Trailer marker (%%EOF)
5. Threat scanning (Heuristic / ClamAV interface)
6. Password protection / encryption check
7. Bounded page count limit (<= 5000 pages)
8. SHA-256 calculation
9. Decompression bomb detection (Stream expansion ratio & absolute size cap)
"""

import hashlib
import io
import logging
import zlib
from abc import ABC, abstractmethod
from typing import Any, ClassVar

import pypdf

from src.core.config import settings
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

    def __init__(
        self,
        scanner: ThreatScanner | None = None,
        max_decompression_ratio: int | None = None,
        max_single_stream_bytes: int | None = None,
    ):
        self.scanner = scanner or ClamAVScanner()
        self.max_decompression_ratio = (
            max_decompression_ratio or settings.MAX_DECOMPRESSION_RATIO
        )
        self.max_single_stream_bytes = (
            max_single_stream_bytes or settings.MAX_SINGLE_STREAM_DECOMPRESSED_BYTES
        )

    def _check_stream_for_bomb(
        self, raw_data: bytes, filter_type: Any, chunk_size: int = 64 * 1024
    ) -> tuple[bool, int, int]:
        """Inspects a stream payload incrementally without full in-memory buffer expansion.
        Returns (is_bomb, compressed_bytes, decompressed_bytes).
        """
        compressed_len = len(raw_data)
        if compressed_len == 0:
            return False, 0, 0

        max_allowed = min(
            compressed_len * self.max_decompression_ratio,
            self.max_single_stream_bytes,
        )

        filters = []
        if isinstance(filter_type, list):
            filters = [str(f) for f in filter_type]
        elif filter_type is not None:
            filters = [str(filter_type)]

        # If uncompressed, verify against absolute ceiling directly
        if not any("Flate" in f for f in filters):
            if compressed_len > self.max_single_stream_bytes:
                return True, compressed_len, compressed_len
            return False, compressed_len, compressed_len

        # Bounded streaming decompression
        for wbits in (zlib.MAX_WBITS, -zlib.MAX_WBITS):
            try:
                decompressor = zlib.decompressobj(wbits)
                total_decompressed = 0
                for i in range(0, compressed_len, chunk_size):
                    chunk = raw_data[i : i + chunk_size]
                    remaining_allowed = max_allowed - total_decompressed
                    unpacked = decompressor.decompress(chunk, remaining_allowed + 1)
                    total_decompressed += len(unpacked)
                    if total_decompressed > max_allowed:
                        return True, compressed_len, total_decompressed
                return False, compressed_len, total_decompressed
            except Exception:
                continue

        return False, compressed_len, compressed_len

    def _check_decompression_bombs(self, reader: pypdf.PdfReader) -> tuple[bool, dict[str, Any]]:
        """Iterates over all stream objects in the PDF and enforces compression ratio limits."""
        size = reader.trailer.get("/Size", 0)
        checked_objects = set()

        # 1. Check all indirect objects in trailer
        if isinstance(size, int) and size > 0:
            for i in range(1, size):
                try:
                    obj = reader.get_object(i)
                    if obj is None:
                        continue
                    checked_objects.add(id(obj))
                    if isinstance(obj, (pypdf.generic.EncodedStreamObject, pypdf.generic.StreamObject)) or hasattr(obj, "_data"):
                        raw_bytes = getattr(obj, "_data", None)
                        if isinstance(raw_bytes, bytes):
                            is_bomb, c_len, d_len = self._check_stream_for_bomb(
                                raw_bytes, obj.get("/Filter")
                            )
                            if is_bomb:
                                ratio = d_len / max(c_len, 1)
                                return True, {
                                    "compressed_bytes": c_len,
                                    "decompressed_bytes": d_len,
                                    "ratio": ratio,
                                }
                except Exception:
                    continue

        # 2. Check page contents and resources recursively
        for page in reader.pages:
            try:
                contents = page.get_contents()
                if contents is not None:
                    content_list = contents if isinstance(contents, list) else [contents]
                    for c in content_list:
                        if id(c) in checked_objects:
                            continue
                        checked_objects.add(id(c))
                        raw_bytes = getattr(c, "_data", None)
                        if isinstance(raw_bytes, bytes):
                            is_bomb, c_len, d_len = self._check_stream_for_bomb(
                                raw_bytes, c.get("/Filter")
                            )
                            if is_bomb:
                                ratio = d_len / max(c_len, 1)
                                return True, {
                                    "compressed_bytes": c_len,
                                    "decompressed_bytes": d_len,
                                    "ratio": ratio,
                                }
            except Exception:
                continue

        return False, {}

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

            # 9. Decompression Bomb Detection (Checked before proceeding)
            is_bomb, bomb_details = self._check_decompression_bombs(reader)
            if is_bomb:
                return ValidationResult(
                    is_valid=False,
                    file_size_bytes=size,
                    mime_type=declared_mime_type,
                    rejection_reason=(
                        f"DECOMPRESSION_BOMB_SUSPECTED: Stream expansion ratio exceeded limit "
                        f"(Compressed: {bomb_details.get('compressed_bytes', 0)}B, "
                        f"Decompressed: {bomb_details.get('decompressed_bytes', 0)}B, "
                        f"Ratio: {bomb_details.get('ratio', 0.0):.1f}x > Max {self.max_decompression_ratio}x)."
                    ),
                    scan_result=scan_res,
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
