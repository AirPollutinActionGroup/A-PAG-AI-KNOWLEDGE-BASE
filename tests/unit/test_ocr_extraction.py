"""OCR fallback for PDF pages with no text layer.

No OCR engine runs here. A fake stands in, because what is under test is everything around the
engine: *whether* a page is sent to it, what happens per page rather than per document, and what
is recorded afterwards. Engine accuracy was measured against the real corpus (0.99 mean
confidence on A-PAG's scanned CPCB directions) and is not something a unit test can assert.

The decisions worth pinning:

- Per page, not per document. A government PDF is routinely a typed covering letter with a
  scanned annexure behind it. A per-document switch reads one half and loses the other.
- A typed page must never reach the engine. OCR costs ~3.5s a page; spending that on a corpus
  that is 92% typed would make extraction ten times slower for nothing.
- A missing or broken engine degrades to exactly the old behaviour. OCR is a fallback, and a
  deployment without it must still extract every typed document.
- Pages past the budget are *recorded*, never silently dropped. A truncated document that says
  nothing about it looks identical to a short one.
"""

import io

import pdfplumber
import pytest
from PIL import Image, ImageDraw

from src.core import config
from src.modules.document_pipeline.extraction.extractors import PdfTextExtractor
from src.modules.document_pipeline.extraction.models import (
    METHOD_MIXED,
    METHOD_NATIVE,
    METHOD_OCR,
)
from src.modules.document_pipeline.extraction.ocr import (
    OcrEngine,
    OcrLine,
    OcrUnavailable,
)
from src.modules.document_pipeline.extraction.service import ExtractionService
from src.modules.document_pipeline.formats import PDF_MIME


class FakeOcr(OcrEngine):
    """Reads back whatever it was told to, and records how many pages it was asked about."""

    def __init__(self, reply: str = "Text recovered from the scan.", fail: bool = False,
                 confidence: float = 0.98):
        self.reply = reply
        self.fail = fail
        self.confidence = confidence
        self.pages_read = 0

    @property
    def name(self) -> str:
        return "fake/ocr"

    def read(self, image) -> list[OcrLine]:
        self.pages_read += 1
        if self.fail:
            raise OcrUnavailable("engine is down")
        if not self.reply:
            return []
        return [OcrLine(text=line, confidence=self.confidence)
                for line in self.reply.split("\n")]


# ==============================================================================
# Building PDFs whose pages differ, which is the whole point
# ==============================================================================

def _typed_pdf(lines: list[str], width: int = 300, height: int = 400) -> bytes:
    """A one-page PDF with a real text layer, written by hand.

    Built rather than borrowed because none of the repo's PDF fixtures actually carry extractable
    text -- 01_standard_digital_policy.pdf reports zero characters -- and reportlab is not a
    dependency here. Fifteen lines of PDF is more predictable than either.
    """
    objs = [
        b"<</Type/Catalog/Pages 2 0 R>>",
        b"<</Type/Pages/Kids[3 0 R]/Count 1>>",
        b"<</Type/Page/Parent 2 0 R/MediaBox[0 0 %d %d]/Contents 4 0 R"
        b"/Resources<</Font<</F1 5 0 R>>>>>>" % (width, height),
    ]
    body = [b"BT", b"/F1 12 Tf", b"14 TL", b"20 %d Td" % (height - 40)]
    for line in lines:
        escaped = line.replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)")
        body.append(b"(" + escaped.encode("latin-1", "replace") + b") Tj T*")
    body.append(b"ET")
    stream = b"\n".join(body)
    objs.append(b"<</Length %d>>\nstream\n%s\nendstream" % (len(stream), stream))
    objs.append(b"<</Type/Font/Subtype/Type1/BaseFont/Helvetica>>")

    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for i, obj in enumerate(objs, start=1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % i + obj + b"\nendobj\n"
    xref_at = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1)
    for off in offsets:
        out += b"%010d 00000 n \n" % off
    out += b"trailer\n<</Size %d/Root 1 0 R>>\nstartxref\n%d\n%%%%EOF\n" % (
        len(objs) + 1, xref_at
    )
    return bytes(out)


def _pdf(pages: list[str]) -> bytes:
    """Builds a PDF from a list of "text" / "image" / "blank" page kinds.

    A text page is the handmade one above; an image page is a picture with no text layer, which
    is what a scanner produces and what the fallback exists for.
    """
    import pypdfium2 as pdfium

    doc = pdfium.PdfDocument.new()
    for kind in pages:
        if kind == "text":
            typed = pdfium.PdfDocument(_typed_pdf([
                "Typed covering letter with a real text layer.",
                "Ministry of Power, dated 20th November 2024.",
            ]))
            doc.import_pages(typed, [0], index=len(doc))
            continue

        page = doc.new_page(300, 400)
        if kind == "image":
            img = Image.new("RGB", (280, 380), "white")
            draw = ImageDraw.Draw(img)
            draw.rectangle([20, 20, 260, 360], outline="black")
            draw.text((40, 180), "scanned", fill="black")
            obj = pdfium.PdfImage.new(doc)
            obj.set_bitmap(pdfium.PdfBitmap.from_pil(img))
            obj.set_matrix(pdfium.PdfMatrix().scale(280, 380))
            page.insert_obj(obj)
        page.gen_content()

    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


@pytest.fixture
def scanned_pdf() -> bytes:
    """One page: an image, no text layer. What a scan is."""
    return _pdf(["image"])


@pytest.fixture
def hybrid_pdf() -> bytes:
    """Page 1 typed, page 2 scanned — the common government-document shape."""
    return _pdf(["text", "image"])


def _readable(data: bytes) -> bool:
    """Guards the fixtures themselves: if pypdfium2 cannot build what these tests assume, the
    assertions below would pass for the wrong reason."""
    with pdfplumber.open(io.BytesIO(data)) as pdf:
        return any(p.images for p in pdf.pages)


# ==============================================================================
# A page with no text layer is read from its pixels
# ==============================================================================

def test_a_scanned_page_is_read_by_ocr(scanned_pdf):
    """The failure this exists for: three of A-PAG's first 39 documents extracted to zero
    characters and stopped at NORMALIZATION_FAILED, unsearchable and unexplained."""
    assert _readable(scanned_pdf), "fixture is not a scan-shaped page"
    engine = FakeOcr(reply="Directions under Section 5 of the Environment (Protection) Act.")

    content = PdfTextExtractor(ocr=engine).extract(scanned_pdf)

    assert engine.pages_read == 1
    assert "Section 5" in content.units[0].text
    assert content.units[0].method == METHOD_OCR
    assert content.ocr_pages == [1]


def test_a_typed_page_never_reaches_the_engine(hybrid_pdf):
    """OCR costs ~3.5s a page. Spending it on a corpus that is overwhelmingly typed would make
    extraction an order of magnitude slower and gain nothing."""
    engine = FakeOcr()

    content = PdfTextExtractor(ocr=engine).extract(hybrid_pdf)

    assert content.units[0].method == METHOD_NATIVE
    assert 1 not in content.ocr_pages


def test_one_document_can_be_read_both_ways(hybrid_pdf):
    """A typed covering letter in front of a scanned annexure. A per-document switch reads one
    half and loses the other, whichever way it is set."""
    engine = FakeOcr(reply="Annexure text.")

    content = PdfTextExtractor(ocr=engine).extract(hybrid_pdf)

    methods = [u.method for u in content.units]
    assert methods == [METHOD_NATIVE, METHOD_OCR]
    assert "covering letter" in content.units[0].text
    assert "Annexure text." in content.units[1].text


# ==============================================================================
# Degrading without the engine
# ==============================================================================

def test_with_ocr_switched_off_extraction_still_succeeds(scanned_pdf, monkeypatch):
    """OCR is a fallback. A deployment that switches it off must extract every typed document
    exactly as before, and record the scan as empty — which is what happened before OCR
    existed, and is a state the quality gate already understands.

    The switch is tested rather than a null engine, because `ocr=None` means "resolve the
    process engine", and a setting that an injected engine could override would not be a
    switch at all."""
    monkeypatch.setattr(config.settings, "OCR_ENABLED", False)

    content = PdfTextExtractor(ocr=FakeOcr(reply="should never run")).extract(scanned_pdf)

    assert content.units[0].text.strip() == ""
    assert content.units[0].method == METHOD_NATIVE
    assert content.ocr_pages == []


def test_an_engine_failure_does_not_fail_the_document(scanned_pdf):
    """A broken engine must not turn a readable corpus into EXTRACTION_FAILED."""
    engine = FakeOcr(fail=True)

    content = PdfTextExtractor(ocr=engine).extract(scanned_pdf)

    assert content.units[0].text.strip() == ""
    assert content.ocr_pages == []


def test_a_page_the_engine_finds_nothing_on_is_not_marked_ocr(scanned_pdf):
    """A blank scanned page. It was photographed and read; there was nothing on it. Recording
    that as OCR-derived would claim provenance for text that does not exist."""
    engine = FakeOcr(reply="")

    content = PdfTextExtractor(ocr=engine).extract(scanned_pdf)

    assert engine.pages_read == 1
    assert content.units[0].method == METHOD_NATIVE
    assert content.ocr_pages == []


# ==============================================================================
# The budget, and saying so
# ==============================================================================

def test_pages_past_the_budget_are_recorded_not_dropped(monkeypatch):
    """A document truncated without a trace looks exactly like a document that was short, and
    the gap is unfindable afterwards."""
    monkeypatch.setattr(config.settings, "OCR_MAX_PAGES", 1)
    data = _pdf(["image", "image", "image"])
    engine = FakeOcr(reply="Read.")

    content = PdfTextExtractor(ocr=engine).extract(data)

    assert content.ocr_pages == [1]
    assert content.ocr_skipped_pages == [2, 3]
    assert engine.pages_read == 1, "the budget must stop the work, not just the recording"


def test_a_page_with_no_text_and_no_image_is_not_photographed():
    """A genuinely blank page. Rendering and reading it costs seconds to confirm it is blank."""
    data = _pdf(["blank"])
    engine = FakeOcr()

    PdfTextExtractor(ocr=engine).extract(data)

    assert engine.pages_read == 0


# ==============================================================================
# What the stored artifact says
# ==============================================================================

def test_the_result_records_how_the_document_was_read(scanned_pdf):
    """`extraction_method` is derived from the pages rather than set alongside them, so it
    cannot disagree with the units it describes."""
    engine = FakeOcr(reply="Scanned body text.")
    svc = ExtractionService(extractors={PDF_MIME: PdfTextExtractor(ocr=engine)})

    import uuid
    result = svc.extract(uuid.uuid4(), scanned_pdf, PDF_MIME)

    assert result.extraction_method == METHOD_OCR
    assert result.ocr_page_count == 1
    assert result.char_count > 0


def test_a_mixed_document_is_recorded_as_mixed(hybrid_pdf):
    engine = FakeOcr(reply="Annexure.")
    svc = ExtractionService(extractors={PDF_MIME: PdfTextExtractor(ocr=engine)})

    import uuid
    result = svc.extract(uuid.uuid4(), hybrid_pdf, PDF_MIME)

    assert result.extraction_method == METHOD_MIXED


def test_a_typed_document_is_still_recorded_as_native():
    """The control. Nothing about OCR may change how a typed document is described."""
    data = _pdf(["text"])
    svc = ExtractionService(extractors={PDF_MIME: PdfTextExtractor(ocr=FakeOcr())})

    import uuid
    result = svc.extract(uuid.uuid4(), data, PDF_MIME)

    assert result.extraction_method == METHOD_NATIVE
    assert result.ocr_pages == []


# ==============================================================================
# Confidence filtering
# ==============================================================================

def test_low_confidence_lines_are_dropped():
    """A low-confidence line is usually a stamp, a signature or a scan artifact. Keeping it puts
    an invented word into a passage that will later be cited, and nothing downstream can tell a
    guessed word from a read one."""
    from src.modules.document_pipeline.extraction.ocr import RapidOcrEngine

    class _Result:
        txts = ("Clear line.", "sm dge nt", "Also clear.")
        scores = (0.97, 0.21, 0.95)

    engine = RapidOcrEngine(min_confidence=0.5)
    engine._engine = lambda _img: _Result()

    lines = engine.read(Image.new("RGB", (10, 10), "white"))

    assert [ln.text for ln in lines] == ["Clear line.", "Also clear."]
