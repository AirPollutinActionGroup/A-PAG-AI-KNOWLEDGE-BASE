"""Reading a page that has no text in it.

A scanned PDF is a photograph of a document. pdfplumber reads it correctly and finds nothing,
because there is nothing there to find — the words are pixels. Three of A-PAG's first 39
documents are like this, including a 162-page set of CPCB directions under Section 5 of the
Environment (Protection) Act, and before this they stopped at `NORMALIZATION_FAILED` with
`EMPTY_TEXT` and were never searchable.

**Why an OCR engine rather than a document parser.** Docling, unstructured and LlamaParse all
recover *layout* — headings, reading order, table grids — and call an OCR engine underneath for
the pixels. They earn their weight on scanned tables, which is the case a bare OCR engine handles
worst: text lines with no grid. Measured on this corpus, the scans contain no tables. Sampling
pages 1, 41, 91 and 141 of the 162-page set for detected lines sharing a horizontal band — prose
sits alone on a row, table cells sit beside neighbours — found 0 such rows on every page. They are
prose notifications. Docling's headline advantage is 97.9% table-cell accuracy, which buys nothing
here, against ~500MB of models plus PyTorch in an image that already carries a 640MB embedding
model. LlamaParse and unstructured's hosted tier are cloud services, which would send every
document out of the deployment at ingestion — the opposite of what the boundary between this
system and an external model exists to enforce.

That reasoning is corpus-specific and may not survive the rest of the drive. If scanned budget
sheets or emission tables turn up, revisit it: Docling accepts RapidOCR as its own OCR backend,
so adding layout recovery later is a layer on top of this, not a replacement for it.

**Why the model is loaded lazily.** Unlike the embedding model, which every EMBED job needs, OCR
is needed only by the small minority of documents that are scans. Loading on first use means an
extraction worker that never meets a scan never pays for it.
"""

import logging
import threading
from abc import ABC, abstractmethod

from pydantic import BaseModel

from src.core.config import settings

logger = logging.getLogger(__name__)

# Recorded on an ExtractedUnit so a reader can tell a page that was read from its text layer from
# one that was inferred from pixels. OCR is very good and not perfect: measured at 0.99 mean
# confidence on this corpus, but it drops word boundaries ("FGDinexisting plantswere"), which
# costs the lexical arm a term it can never match. A passage that came from OCR is worth knowing
# about before quoting it in a government submission.
METHOD_NATIVE = "NATIVE"
METHOD_OCR = "OCR"
METHOD_MIXED = "MIXED"


class OcrLine(BaseModel):
    """One line of text the engine found, with how sure it is."""

    text: str
    confidence: float


class OcrEngine(ABC):
    """Turns an image of a page into lines of text."""

    @property
    @abstractmethod
    def name(self) -> str:
        """Identifies the engine and model in logs and stored artifacts."""

    @abstractmethod
    def read(self, image) -> list[OcrLine]:
        """Reads a PIL image. Returns an empty list for a page with nothing legible on it."""


class OcrUnavailable(RuntimeError):
    """The engine could not be loaded. Deliberately distinct from "read the page and found
    nothing": one is a broken deployment, the other is a blank page."""


class RapidOcrEngine(OcrEngine):
    """RapidOCR (PP-OCR models) on onnxruntime.

    Chosen partly because onnxruntime is already in this image for embeddings, so OCR adds
    models and OpenCV rather than a second inference runtime — and no PyTorch, consistent with
    every other model decision here.

    Thread count is set explicitly. It is the difference between a usable path and an unusable
    one: left at its default this measured 20.5s per page, and at 8 intra-op threads, 4.0s. On
    the 162-page document that is 54 minutes against 11.
    """

    def __init__(self, threads: int | None = None, min_confidence: float | None = None):
        self._threads = threads if threads is not None else settings.OCR_THREADS
        self._min_confidence = (
            min_confidence if min_confidence is not None else settings.OCR_MIN_CONFIDENCE
        )
        self._engine = None
        # Extraction workers are single-threaded today, but a lazily built model behind a
        # process-wide accessor is exactly the shape that breaks when that changes.
        self._lock = threading.Lock()

    @property
    def name(self) -> str:
        return "rapidocr/PP-OCRv6"

    def _load(self):
        if self._engine is not None:
            return self._engine
        with self._lock:
            if self._engine is not None:
                return self._engine
            try:
                from rapidocr import RapidOCR
            except ImportError as e:  # pragma: no cover - depends on the deployed image
                raise OcrUnavailable(
                    "rapidocr is not installed; set OCR_ENABLED=false or install it."
                ) from e

            threads = self._threads or min(8, _cpu_count())
            params = {
                "EngineConfig.onnxruntime.intra_op_num_threads": threads,
                "EngineConfig.onnxruntime.inter_op_num_threads": 1,
            }
            try:
                self._engine = RapidOCR(params=params)
            except Exception as e:
                raise OcrUnavailable(f"Could not start RapidOCR: {e}") from e
            logger.info("OCR engine ready: %s (%d threads)", self.name, threads)
            return self._engine

    def read(self, image) -> list[OcrLine]:
        import numpy as np

        engine = self._load()
        try:
            result = engine(np.asarray(image.convert("RGB")))
        except Exception as e:
            raise OcrUnavailable(f"OCR failed on a page: {e}") from e

        texts = list(result.txts) if result.txts is not None else []
        scores = list(result.scores) if result.scores is not None else []

        lines = []
        for i, text in enumerate(texts):
            confidence = float(scores[i]) if i < len(scores) else 0.0
            cleaned = (text or "").strip()
            # A low-confidence line is usually a stamp, a signature or a scan artifact. Keeping
            # it puts invented words into a passage that will later be cited, which is worse
            # than a gap: nothing downstream can tell the difference between a word the engine
            # read and a word it guessed.
            if cleaned and confidence >= self._min_confidence:
                lines.append(OcrLine(text=cleaned, confidence=confidence))
        return lines


def _cpu_count() -> int:
    import os

    return os.cpu_count() or 1


_engine: OcrEngine | None = None
_engine_lock = threading.Lock()
_engine_failed = False


def get_ocr_engine() -> OcrEngine | None:
    """The process's OCR engine, or None when OCR is switched off or cannot be loaded.

    Returns None rather than raising, because OCR is a fallback: a deployment without it should
    extract every typed document exactly as before and record the scans as unreadable, which is
    what happened before this existed. A hard failure here would turn a missing optional
    dependency into a dead pipeline.
    """
    global _engine, _engine_failed

    if not settings.OCR_ENABLED or _engine_failed:
        return None
    if _engine is not None:
        return _engine

    with _engine_lock:
        if _engine is not None:
            return _engine
        engine = RapidOcrEngine()
        try:
            engine._load()
        except OcrUnavailable as e:
            # Logged once, at ERROR: a scan silently falling back to "no text" is precisely the
            # invisible gap the quality gate exists to prevent.
            logger.error("OCR unavailable, scanned pages will not be read: %s", e)
            _engine_failed = True
            return None
        _engine = engine
        return _engine


def reset_ocr_engine() -> None:
    """Drops the cached engine. For tests, which substitute their own."""
    global _engine, _engine_failed
    with _engine_lock:
        _engine = None
        _engine_failed = False
