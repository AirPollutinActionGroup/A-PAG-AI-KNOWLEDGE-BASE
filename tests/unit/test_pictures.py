"""Text and tables read out of pictures in Word and PowerPoint files.

A fake reader stands in for the OCR and the grid finder, returning what the real one measured on
the real deck (screenshots of tables like those of the Secretary Power presentation). What is
pinned here is everything around it -- where the text lands, what it is cited under, and that a
picture that cannot be read never costs a document its text. The reader's own logic is in
`test_picture_tables.py`.
"""

import io
import sys
import uuid

import pytest
from docx import Document as DocxDocument
from pptx import Presentation
from pptx.util import Inches

from src.core.config import settings
from src.modules.document_pipeline.chunking.chunker import Chunker
from src.modules.document_pipeline.extraction.extractors import (
    DocxTextExtractor,
    PptxTextExtractor,
)
from src.modules.document_pipeline.extraction.models import (
    ExtractedContent,
    ExtractedUnit,
    Heading,
)
from src.modules.document_pipeline.extraction.pictures import (
    PictureReader,
    PictureText,
    get_picture_reader,
    merge_pictures,
    reset_picture_reader,
)
from src.modules.document_pipeline.extraction.service import ExtractionService
from src.modules.document_pipeline.formats import (
    DOCX_MIME,
    PDF_MIME,
    PPTX_MIME,
    XLSX_MIME,
)
from src.modules.document_pipeline.normalization.service import NormalizationService


class _Reader(PictureReader):
    """Returns what it was given, and records how often it was asked."""

    def __init__(self, pictures=None, fail=False):
        self.pictures, self.fail, self.calls = pictures or [], fail, []

    @property
    def name(self) -> str:
        return "fake/reader"

    def read(self, data: bytes, mime_type: str) -> list[PictureText]:
        self.calls.append(mime_type)
        if self.fail:
            raise RuntimeError("model failed to load")
        return self.pictures


NTPC_ROWS = [
    ["NTPC Plants", "April-19", "May-19", "Data Type"],
    ["NTPC/IGSTPP", "9.4", "9.0", "Max."],
    ["NTPC/Unchahar TPS", "22.6", "6.6", "Max."],
]


def _deck(slides) -> bytes:
    prs = Presentation()
    for title in slides:
        slide = prs.slides.add_slide(prs.slide_layouts[5])
        slide.shapes.title.text = title
        box = slide.shapes.add_textbox(Inches(1), Inches(2), Inches(4), Inches(1))
        box.text_frame.text = f"Body of {title}"
    buf = io.BytesIO()
    prs.save(buf)
    return buf.getvalue()


def _chunks(content, mime):
    from src.modules.document_pipeline.extraction.models import ExtractionResult

    result = ExtractionResult(
        document_id=uuid.uuid4(), mime_type=mime, units=content.units, headings=content.headings,
        tables=content.tables, figures=content.figures,
    )
    return Chunker().chunk(NormalizationService().normalize(result))


# ==============================================================================
# A screenshot of a table becomes a table, under its slide
# ==============================================================================

def test_a_screenshot_of_a_table_becomes_a_table_cited_under_its_slide():
    """Slides 13-16 of the Secretary Power deck are images of tables; their numbers were gone."""
    deck = _deck(["Ambient SO2 levels", "Central Sector Plants"])
    reader = _Reader([PictureText(unit_index=2, tables=[NTPC_ROWS])])

    result = ExtractionService(picture_reader=reader).extract(uuid.uuid4(), deck, PPTX_MIME)

    assert [(t.unit_index, t.heading) for t in result.tables] == [(2, "Central Sector Plants")]
    [table_chunk] = [c for c in _chunks(result, PPTX_MIME) if c.is_table]
    assert table_chunk.page_number == 2
    assert table_chunk.section_heading == "Central Sector Plants"
    assert "NTPC/Unchahar TPS | 22.6 | 6.6 | Max." in table_chunk.text
    assert table_chunk.text.splitlines()[0] == "NTPC Plants | April-19 | May-19 | Data Type"


def test_text_read_from_a_chart_is_a_figure_under_its_slide():
    deck = _deck(["Avg SO2 Emissions from TPPs India-wide"])
    reader = _Reader([PictureText(unit_index=1, text="Category 1a: 1112\nCategory 1b: 1209")])

    content = ExtractionService(picture_reader=reader).extract(uuid.uuid4(), deck, PPTX_MIME)
    figure = [c for c in _chunks(content, PPTX_MIME) if c.text.startswith("[Figure]")]

    assert len(figure) == 1
    assert figure[0].section_heading == "Avg SO2 Emissions from TPPs India-wide"
    assert "Category 1b: 1209" in figure[0].text
    assert figure[0].is_table is False


def test_a_figure_follows_its_slides_prose():
    deck = _deck(["Slide A"])
    reader = _Reader([PictureText(unit_index=1, text="axis label")])
    chunks = _chunks(ExtractionService(picture_reader=reader).extract(uuid.uuid4(), deck, PPTX_MIME),
                     PPTX_MIME)
    assert [c.text.startswith("[Figure]") for c in chunks] == [False, True]


def test_a_picture_with_a_caption_keeps_it():
    deck = _deck(["Location of 11 TPPs"])
    reader = _Reader([PictureText(unit_index=1, text="Delhi\nPanipat", caption="Map of plants")])
    chunks = _chunks(ExtractionService(picture_reader=reader).extract(uuid.uuid4(), deck, PPTX_MIME),
                     PPTX_MIME)
    assert any(c.text.startswith("[Figure] (text read from a picture by OCR) Map of plants\nDelhi")
               for c in chunks)


# ==============================================================================
# Word: a picture is cited under the heading it was under, not the first one
# ==============================================================================

def _word() -> bytes:
    d = DocxDocument()
    d.add_heading("Background", level=1)
    d.add_paragraph("Intro.")
    d.add_heading("FGD process", level=1)
    d.add_paragraph("Limestone slurry.")
    buf = io.BytesIO()
    d.save(buf)
    return buf.getvalue()


def test_a_word_pictures_text_is_cited_under_the_heading_it_sat_beneath():
    """A unit with several headings is a Word file's single unit; "the unit's heading" would be
    the first heading in the document, and every picture would be cited under it."""
    reader = _Reader([PictureText(unit_index=1, text="Absorber\nGypsum", heading="FGD process")])

    content = ExtractionService(picture_reader=reader).extract(uuid.uuid4(), _word(), DOCX_MIME)
    [figure] = content.figures

    assert figure.heading == "FGD process"
    chunks = _chunks(content, DOCX_MIME)
    [placed] = [c for c in chunks if c.text.startswith("[Figure]")]
    assert placed.section_heading == "FGD process"
    # ...and it reads in order: after its section's text, not at the end of the document.
    assert chunks.index(placed) == len(chunks) - 1


def test_a_picture_under_the_first_heading_is_not_filed_under_the_last():
    reader = _Reader([PictureText(unit_index=1, text="Chart labels", heading="Background")])
    content = ExtractionService(picture_reader=reader).extract(uuid.uuid4(), _word(), DOCX_MIME)
    chunks = _chunks(content, DOCX_MIME)
    [placed] = [c for c in chunks if c.text.startswith("[Figure]")]
    assert placed.section_heading == "Background"
    assert chunks.index(placed) < [c.section_heading for c in chunks].index("FGD process")


def test_a_picture_in_a_word_file_with_no_known_heading_is_cited_under_none():
    """A wrong citation is worse than an absent one."""
    reader = _Reader([PictureText(unit_index=1, text="Chart labels", heading=None)])
    content = ExtractionService(picture_reader=reader).extract(uuid.uuid4(), _word(), DOCX_MIME)
    assert content.figures[0].heading is None


# ==============================================================================
# What must never happen
# ==============================================================================

def test_a_reader_that_fails_costs_the_document_nothing():
    reader = _Reader(fail=True)
    result = ExtractionService(picture_reader=reader).extract(uuid.uuid4(), _deck(["Slide A"]), PPTX_MIME)
    assert "Body of Slide A" in result.full_text
    assert result.figures == [] and reader.calls == [PPTX_MIME]


def test_every_supported_format_is_sent_to_the_reader():
    """A PDF reaches it too: a table pasted into a typed report as a picture is pixels, and the
    page's text layer says nothing of it. Which of its pictures are read is the source's decision
    (`picture_source._pdf_pictures`), not this one's."""
    from openpyxl import Workbook

    from tests.unit.test_ocr_extraction import _pdf

    reader = _Reader()
    service = ExtractionService(picture_reader=reader)
    d = DocxDocument()
    d.add_paragraph("x")
    buf = io.BytesIO()
    d.save(buf)
    service.extract(uuid.uuid4(), buf.getvalue(), DOCX_MIME)

    wb = Workbook()
    wb.active["A1"] = "x"
    sheet = io.BytesIO()
    wb.save(sheet)
    service.extract(uuid.uuid4(), sheet.getvalue(), XLSX_MIME)
    service.extract(uuid.uuid4(), _pdf(["text"]), PDF_MIME)

    assert reader.calls == [DOCX_MIME, XLSX_MIME, PDF_MIME]


def test_a_picture_that_said_nothing_adds_nothing():
    deck = _deck(["Slide A"])
    reader = _Reader([PictureText(unit_index=1, text="   ")])
    result = ExtractionService(picture_reader=reader).extract(uuid.uuid4(), deck, PPTX_MIME)
    assert result.figures == []


def test_figure_text_is_cleaned_like_body_text():
    deck = _deck(["Slide A"])
    reader = _Reader([PictureText(unit_index=1, text="Max.\x0bAvg\x00 value")])
    extracted = ExtractionService(picture_reader=reader).extract(uuid.uuid4(), deck, PPTX_MIME)
    normalized = NormalizationService().normalize(extracted)
    assert normalized.figures[0].text == "Max.\nAvg value"


def test_old_extraction_json_without_figures_still_loads():
    from src.modules.document_pipeline.extraction.models import ExtractionResult

    stored = {
        "document_id": str(uuid.uuid4()), "mime_type": PPTX_MIME,
        "units": [{"index": 1, "label": "Slide 1", "text": "x"}], "headings": [], "tables": [],
    }
    assert ExtractionResult.model_validate(stored).figures == []


def test_merge_falls_back_to_the_first_unit_for_a_picture_on_an_unknown_page():
    content = ExtractedContent(
        units=[ExtractedUnit(index=1, label="Document", text="x")],
        headings=[Heading(text="Only heading", level=1, unit_index=1)],
    )
    merge_pictures(content, [PictureText(unit_index=7, text="labels")])
    assert content.figures[0].unit_index == 1


# ==============================================================================
# Switching it off, and its absence
# ==============================================================================

def test_the_reader_is_off_when_the_setting_is_off(monkeypatch):
    reset_picture_reader()
    monkeypatch.setattr(settings, "PICTURES_ENABLED", False)
    assert get_picture_reader() is None


def test_the_reader_needs_ocr_as_well(monkeypatch):
    reset_picture_reader()
    monkeypatch.setattr(settings, "PICTURES_ENABLED", True)
    monkeypatch.setattr(settings, "OCR_ENABLED", False)
    assert get_picture_reader() is None


def test_a_missing_install_degrades_to_no_pictures_and_says_so_once(monkeypatch):
    """Counted on the module's logger directly, not read back from log output: Alembic's logging
    setup in the integration tests disables existing loggers, which silences `caplog` for any
    test that runs after them."""
    from src.modules.document_pipeline.extraction import pictures

    reset_picture_reader()
    monkeypatch.setattr(settings, "PICTURES_ENABLED", True)
    monkeypatch.setattr(settings, "OCR_ENABLED", True)
    monkeypatch.setitem(sys.modules, "img2table", None)  # makes `import img2table` raise ImportError
    warnings = []
    monkeypatch.setattr(pictures.logger, "warning", lambda msg, *a, **k: warnings.append(msg))

    assert get_picture_reader() is None
    assert get_picture_reader() is None

    assert len(warnings) == 1 and "not installed" in warnings[0]
    reset_picture_reader()


def test_extraction_without_a_reader_is_exactly_what_it_was():
    deck = _deck(["Slide A"])
    plain = PptxTextExtractor().extract(deck)
    result = ExtractionService().extract(uuid.uuid4(), deck, PPTX_MIME)
    assert [u.text for u in result.units] == [u.text for u in plain.units]
    assert result.figures == [] and result.tables == plain.tables


def test_word_extraction_without_a_reader_is_unchanged():
    plain = DocxTextExtractor().extract(_word())
    result = ExtractionService().extract(uuid.uuid4(), _word(), DOCX_MIME)
    assert result.units[0].text == plain.units[0].text


@pytest.fixture(autouse=True)
def _fresh_reader():
    reset_picture_reader()
    yield
    reset_picture_reader()
