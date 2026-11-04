"""What two reviews of real documents found missing or misfiled, pinned so it stays fixed.

The reviews read the Ministry of Power memo of 20 Nov 2024 (a scan) and "TPPs Delhi NCR.pptx"
against the passages stored for them. Each test below is one finding, built from a file made in
the test rather than the real document, so the suite needs no corpus.
"""

import io
import uuid

import pytest
from docx import Document as DocxDocument
from docx.oxml import parse_xml
from openpyxl import Workbook
from PIL import Image
from pptx import Presentation
from pptx.util import Inches

from src.modules.document_pipeline.chunking.chunker import Chunker
from src.modules.document_pipeline.extraction.extractors import (
    DocxTextExtractor,
    PdfTextExtractor,
    PptxTextExtractor,
    XlsxTextExtractor,
)
from src.modules.document_pipeline.extraction.models import (
    ExtractedTable,
    ExtractionResult,
)
from src.modules.document_pipeline.extraction.ocr import (
    OcrEngine,
    OcrLine,
    OcrPage,
    RapidOcrEngine,
)
from src.modules.document_pipeline.formats import (
    DOCX_MIME,
    PDF_MIME,
    PPTX_MIME,
    XLSX_MIME,
)
from src.modules.document_pipeline.normalization.service import NormalizationService
from src.modules.document_pipeline.normalization.text_cleaner import TextCleaner
from tests.unit.test_ocr_extraction import _pdf


def _chunks(content, mime):
    """Extraction output through normalization and chunking, as the pipeline runs it."""
    result = ExtractionResult(
        document_id=uuid.uuid4(), mime_type=mime, units=content.units,
        headings=content.headings, tables=content.tables,
    )
    return Chunker().chunk(NormalizationService().normalize(result))


# ==============================================================================
# The scanned memo: OCR
# ==============================================================================

class _ScanReader(OcrEngine):
    """Reads back a fixed page, with a number of lines it could not read."""

    def __init__(self, text: str, unreadable: int = 0):
        self.text, self.unreadable = text, unreadable

    @property
    def name(self) -> str:
        return "fake/scan"

    def read(self, image) -> list[OcrLine]:
        return self.read_page(image).lines

    def read_page(self, image) -> OcrPage:
        lines = [OcrLine(text=t, confidence=0.99) for t in self.text.splitlines()]
        return OcrPage(lines=lines, unreadable=self.unreadable)


MEMO = (
    "OFFICE MEMORANDUM\n"
    "Subject: Extension of timelines for installation of FGD in Thermal Power Plants for\n"
    "compliance of SO2 emission norms -reg.\n"
    "Power Plants may not be able to comply with the prescribed timelines\n"
    "Contd...2/-"
)


def test_unreadable_ocr_lines_are_recorded_not_silent():
    """The memo lost "category A, B & C" with nothing to say a line had gone."""
    content = PdfTextExtractor(ocr=_ScanReader(MEMO, unreadable=1)).extract(_pdf(["image"]))
    assert content.ocr_unreadable_lines == {1: 1}


def test_the_engine_counts_what_it_drops():
    class _Result:
        txts = ("Clear line.", "o.in o n nn nn", "Also clear.")
        scores = (0.98, 0.67, 0.99)

    engine = RapidOcrEngine(min_confidence=0.8)
    engine._engine = lambda _img: _Result()

    page = engine.read_page(Image.new("RGB", (10, 10), "white"))

    assert [ln.text for ln in page.lines] == ["Clear line.", "Also clear."]
    assert page.unreadable == 1


def test_a_scans_subject_line_becomes_its_heading():
    """Every passage from a scan was cited under "no section recorded"."""
    content = PdfTextExtractor(ocr=_ScanReader(MEMO)).extract(_pdf(["image"]))
    chunks = _chunks(content, PDF_MIME)

    [claim] = [c for c in chunks if "may not be able to comply" in c.text]
    assert claim.section_heading.startswith("Subject: Extension of timelines")
    assert "Contd" not in claim.text


def test_a_subject_hyphenated_across_lines_still_becomes_the_heading():
    """The real memo's subject line ends "Flue Gas De-", which the cleaner joins to the next
    line, so a heading kept verbatim matched nothing and the memo still had no heading."""
    memo = (
        "Subject: Extension of timelines for installation of Flue Gas De-\n"
        "sulphurisation in Thermal Power Plants\n"
        "Power Plants may not be able to comply with the prescribed timelines"
    )
    content = PdfTextExtractor(ocr=_ScanReader(memo)).extract(_pdf(["image"]))
    [claim] = _chunks(content, PDF_MIME)
    assert claim.section_heading == "Subject: Extension of timelines for installation of Flue Gas"


def test_page_furniture_is_stripped():
    cleaned = TextCleaner().clean("to the extension.\nContd...2/-\n-2-\nPage 3 of 10\nas below")
    assert cleaned == "to the extension.\nas below"


def test_a_sentence_that_mentions_a_page_is_kept():
    text = "see page 3 of the notification\ncontinued compliance is required"
    assert TextCleaner().clean(text) == text


def test_a_soft_line_break_does_not_glue_words():
    """PowerPoint writes Shift+Enter as a vertical tab, which used to be deleted outright."""
    assert TextCleaner().clean("timelines\x0bfor compliance") == "timelines\nfor compliance"


# ==============================================================================
# The deck: slides
# ==============================================================================

def _deck(build) -> bytes:
    prs = Presentation()
    build(prs)
    buf = io.BytesIO()
    prs.save(buf)
    return buf.getvalue()


def _title_slide(prs, title: str):
    slide = prs.slides.add_slide(prs.slide_layouts[5])  # title only
    slide.shapes.title.text = title
    return slide


def _textbox(container, text: str, top: float = 2.0, left: float = 1.0):
    box = container.add_textbox(Inches(left), Inches(top), Inches(4), Inches(1))
    box.text_frame.text = text
    return box


def test_a_heading_never_carries_to_the_next_slide():
    """8 of 21 slides were filed under the previous slide's title."""
    def build(prs):
        _title_slide(prs, "3/11 TPPs in region are privately owned")
        blank = prs.slides.add_slide(prs.slide_layouts[6])
        _textbox(blank.shapes, "SOx emissions are measured at the stack", top=3)

    chunks = _chunks(PptxTextExtractor().extract(_deck(build)), PPTX_MIME)
    second = [c for c in chunks if c.page_number == 2]
    assert second and all(c.section_heading != "3/11 TPPs in region are privately owned"
                          for c in second)


def test_a_title_typed_into_a_text_box_is_still_the_slides_heading():
    def build(prs):
        slide = prs.slides.add_slide(prs.slide_layouts[6])  # blank: no title placeholder
        _textbox(slide.shapes, "Body text further down the slide", top=4)
        _textbox(slide.shapes, "Emission Norms as defined in 2015\nfirst bullet", top=0.5)

    chunks = _chunks(PptxTextExtractor().extract(_deck(build)), PPTX_MIME)
    assert chunks[0].section_heading == "Emission Norms as defined in 2015"
    assert chunks[0].text.count("Emission Norms as defined in 2015") == 1


def test_text_inside_grouped_shapes_is_read():
    """Slide 10's deadline history lives in nested groups and was lost entirely."""
    def build(prs):
        slide = _title_slide(prs, "FGD is recommended to control SOx emissions")
        outer = slide.shapes.add_group_shape()
        inner = outer.shapes.add_group_shape()
        _textbox(inner.shapes, "April 2021: phased deadlines, B by Dec 2023")

    text = PptxTextExtractor().extract(_deck(build)).units[0].text
    assert "Dec 2023" in text


def test_speaker_notes_are_kept():
    def build(prs):
        slide = _title_slide(prs, "77% units still do not have FGD installed")
        slide.notes_slide.notes_text_frame.text = "FGD connection needs a 30 to 45 days shutdown"

    text = PptxTextExtractor().extract(_deck(build)).units[0].text
    assert "Notes:\nFGD connection needs a 30 to 45 days shutdown" in text


def test_a_hidden_slide_is_skipped_without_renumbering():
    def build(prs):
        _title_slide(prs, "First")
        hidden = _title_slide(prs, "Draft numbers")
        hidden._element.set("show", "0")
        _title_slide(prs, "Third")

    units = PptxTextExtractor().extract(_deck(build)).units
    assert [u.index for u in units] == [1, 3]
    assert "Draft numbers" not in " ".join(u.text for u in units)


def test_a_slide_table_carries_its_slides_title():
    def build(prs):
        slide = _title_slide(prs, "6/11 State-owned TPPs")
        table = slide.shapes.add_table(3, 2, Inches(1), Inches(2), Inches(6), Inches(2)).table
        for r, row in enumerate([["State-Owned TPPs", ""], ["Plant", "Age"], ["Panipat", "1 : 27 yrs\n2 : 26 yrs"]]):
            for c, value in enumerate(row):
                table.cell(r, c).text = value

    content = PptxTextExtractor().extract(_deck(build))
    assert content.tables[0].heading == "6/11 State-owned TPPs"
    [table_chunk] = [c for c in _chunks(content, PPTX_MIME) if c.is_table]
    assert table_chunk.section_heading == "6/11 State-owned TPPs"
    lines = table_chunk.text.splitlines()
    # The merged title row is a caption above the real column header, and the multi-line
    # cell stays on its own row.
    assert lines == ["State-Owned TPPs", "Plant | Age", "Panipat | 1 : 27 yrs; 2 : 26 yrs"]


def _table_deck(rows: int, cols: int, build, widths=None):
    """One slide holding a rows x cols table, which `build(slide, table)` then fills in."""
    def make(prs):
        slide = _title_slide(prs, "Slide with a table")
        table = slide.shapes.add_table(
            rows, cols, Inches(1), Inches(2), Inches(6), Inches(rows * 0.4),
        ).table
        for c, width in enumerate(widths or []):
            table.columns[c].width = Inches(width)
        build(slide, table)
    return _deck(make)


def _fill(cell, hex_rgb: str):
    from pptx.dml.color import RGBColor

    cell.fill.solid()
    cell.fill.fore_color.rgb = RGBColor.from_string(hex_rgb)


def test_a_merged_cell_is_written_into_every_cell_it_covers():
    """"Total | (blank) | 35" lost what the row totals, and "Unlisted" spread over four columns
    sat in one of them. A title across the whole table stays one caption cell."""
    def build(slide, table):
        table.cell(0, 0).merge(table.cell(0, 2))
        table.cell(0, 0).text = "Centre-Owned TPPs"
        for c, value in enumerate(["Sector", "Units", "Installed"]):
            table.cell(1, c).text = value
        table.cell(2, 0).merge(table.cell(2, 1))
        table.cell(2, 0).text = "Total"
        table.cell(2, 2).text = "35"

    [table] = PptxTextExtractor().extract(_table_deck(3, 3, build)).tables
    assert table.rows == [
        ["Centre-Owned TPPs", "", ""],
        ["Sector", "Units", "Installed"],
        ["Total", "Total", "35"],
    ]


def test_a_two_row_header_becomes_one_row_of_full_column_names():
    """"SO2 (mg/NM3)" over "< 500 MW" and ">= 500 MW": the column holding 200 had no name."""
    def build(slide, table):
        table.cell(0, 0).text = "Parameter"
        table.cell(0, 1).merge(table.cell(0, 2))
        table.cell(0, 1).text = "SO2 (mg/NM3)"
        for c, value in enumerate(["Size", "< 500 MW", ">= 500 MW"]):
            table.cell(1, c).text = value
        for r, row in enumerate([["Before 2003", "600", "200"], ["2004-16", "600", "200"]], 2):
            for c, value in enumerate(row):
                table.cell(r, c).text = value

    [table] = PptxTextExtractor().extract(_table_deck(4, 3, build)).tables
    assert table.rows[0] == ["Parameter Size", "SO2 (mg/NM3) < 500 MW", "SO2 (mg/NM3) >= 500 MW"]
    assert len(table.rows) == 3


def test_a_header_spanning_two_rows_is_not_repeated_in_its_own_name():
    def build(slide, table):
        table.cell(0, 0).merge(table.cell(1, 0))
        table.cell(0, 0).text = "Owned by"
        table.cell(0, 1).merge(table.cell(0, 2))
        table.cell(0, 1).text = "Emission"
        table.cell(1, 1).text = "SOx"
        table.cell(1, 2).text = "NOx"
        for c, value in enumerate(["State", "1193", "283"]):
            table.cell(2, c).text = value

    [table] = PptxTextExtractor().extract(_table_deck(3, 3, build)).tables
    assert table.rows == [["Owned by", "Emission SOx", "Emission NOx"], ["State", "1193", "283"]]


def test_an_empty_coloured_cell_is_read_as_the_slides_colour_key_says():
    """Slide 7 says which plant reports which pollutant by colour alone, in cells with no text."""
    def make(prs):
        slide = _title_slide(prs, "Data from only 5/11 TPPs is listed online")
        grid = slide.shapes.add_table(3, 2, Inches(1), Inches(2), Inches(4), Inches(1.2)).table
        grid.cell(0, 0).text, grid.cell(0, 1).text = "Plant", "SOx"
        grid.cell(1, 0).text, grid.cell(2, 0).text = "Panipat", "Hisar"
        _fill(grid.cell(1, 1), "B6D7A8")
        _fill(grid.cell(2, 1), "FF9900")
        key = slide.shapes.add_table(2, 2, Inches(6), Inches(2), Inches(3), Inches(0.8)).table
        key.cell(0, 0).text, key.cell(0, 1).text = "Legend", "Data available"
        key.cell(1, 1).text = "Not reported"
        _fill(key.cell(0, 1), "B6D7A8")
        _fill(key.cell(1, 1), "FF9900")

    tables = PptxTextExtractor().extract(_deck(make)).tables
    assert tables[0].rows == [
        ["Plant", "SOx"], ["Panipat", "(Data available)"], ["Hisar", "(Not reported)"],
    ]


def test_a_coloured_cell_with_no_key_on_the_slide_is_left_empty():
    """Only a colour the slide explains means anything. A decorative fill must not become text."""
    def build(slide, table):
        table.cell(0, 0).text = "Plant"
        table.cell(1, 0).text = "Panipat"
        _fill(table.cell(1, 1), "B6D7A8")

    [table] = PptxTextExtractor().extract(_table_deck(2, 2, build)).tables
    assert table.rows[1] == ["Panipat", ""]


def test_labels_drawn_over_a_merged_column_are_attached_to_its_rows():
    """"State", "Centre" and "Private" were rotated text boxes lying on three empty merged cells."""
    def build(slide, table):
        table.cell(0, 0).text, table.cell(0, 1).text = "Owned by", "Plant"
        table.cell(1, 0).merge(table.cell(3, 0))
        table.cell(4, 0).merge(table.cell(5, 0))
        for r, plant in enumerate(["Panipat", "Hisar", "Yamunanagar", "Jhajjar", "Dadri"], 1):
            table.cell(r, 1).text = plant
        group = slide.shapes.add_group_shape()
        for top, label in [(2.6, "State"), (4.0, "Centre")]:
            box = group.shapes.add_textbox(Inches(1.1), Inches(top), Inches(0.3), Inches(0.4))
            box.text_frame.text = label

    content = PptxTextExtractor().extract(
        _table_deck(6, 2, build, widths=[1.5, 4.5])
    )
    [table] = content.tables
    assert [row[0] for row in table.rows] == [
        "Owned by", "State", "State", "State", "Centre", "Centre",
    ]
    # They are part of the table now, not loose words in the slide's text.
    assert "State" not in content.units[0].text.split()


def test_labels_are_not_guessed_when_they_do_not_match_the_merged_blocks():
    def build(slide, table):
        table.cell(1, 0).merge(table.cell(2, 0))
        box = slide.shapes.add_textbox(Inches(1.1), Inches(2.6), Inches(0.3), Inches(0.4))
        box.text_frame.text = "State"
        box = slide.shapes.add_textbox(Inches(1.1), Inches(3.0), Inches(0.3), Inches(0.4))
        box.text_frame.text = "Centre"

    content = PptxTextExtractor().extract(_table_deck(3, 2, build, widths=[1.5, 4.5]))
    assert content.tables == [] or all(row[0] == "" for row in content.tables[0].rows)
    assert "State" in content.units[0].text


def test_a_timeline_year_leads_its_own_description():
    """Slide 10 read "2015, <2015>, <2017>, 2017, 2021": a year after its description is the
    label of the next one."""
    def make(prs):
        slide = _title_slide(prs, "Deadline for FGD installation extended")
        for first, second in [("Supreme Court granted 5 more years", "2017"),
                              ("2021", "April notification, phased installation")]:
            group = slide.shapes.add_group_shape()
            for top, text in [(2.0, first), (3.0, second)]:
                box = group.shapes.add_textbox(Inches(1), Inches(top), Inches(3), Inches(0.5))
                box.text_frame.text = text

    text = PptxTextExtractor().extract(_deck(make)).units[0].text
    assert text.splitlines()[1:] == [
        "2017: Supreme Court granted 5 more years",
        "2021: April notification, phased installation",
    ]


# ==============================================================================
# Word: reading order, merges, text boxes, footnotes
# ==============================================================================

def _docx(build) -> bytes:
    document = DocxDocument()
    build(document)
    buf = io.BytesIO()
    document.save(buf)
    return buf.getvalue()


def test_a_word_table_is_cited_under_the_heading_it_sits_beneath():
    """Every table used to come after all the text and, with two headings, under none (#19)."""
    def build(d):
        d.add_heading("Background", level=1)
        d.add_paragraph("Intro text.")
        d.add_heading("Emission limits", level=1)
        table = d.add_table(rows=2, cols=2)
        for r, row in enumerate([["Pollutant", "Limit"], ["SO2", "100"]]):
            for c, v in enumerate(row):
                table.cell(r, c).text = v
        d.add_heading("Timelines", level=1)
        d.add_paragraph("Dec 2026.")

    content = DocxTextExtractor().extract(_docx(build))
    chunks = _chunks(content, DOCX_MIME)

    [table_chunk] = [c for c in chunks if c.is_table]
    assert table_chunk.section_heading == "Emission limits"
    order = [c.section_heading for c in chunks]
    assert order.index("Emission limits") < order.index("Timelines")
    assert chunks.index(table_chunk) < order.index("Timelines")


def test_a_horizontally_merged_word_cell_appears_once():
    def build(d):
        table = d.add_table(rows=2, cols=3)
        table.cell(0, 0).merge(table.cell(0, 2)).text = "State-Owned TPPs"
        for c, v in enumerate(["Plant", "MW", "Age"]):
            table.cell(1, c).text = v

    rows = DocxTextExtractor().extract(_docx(build)).tables[0].rows
    assert rows[0] == ["State-Owned TPPs"]


def test_word_footnotes_are_kept():
    """Fourteen footnotes in TPP.docx -- the NASA, TERI and Nature sources -- were dropped."""
    def build(d):
        d.add_paragraph("Body.")

    data = _docx(build)
    data = _with_footnote(data, "Source: https://www.teriin.org/report")
    text = DocxTextExtractor().extract(data).units[0].text
    assert "Notes:\nSource: https://www.teriin.org/report" in text


def test_text_in_a_word_text_box_is_read_once():
    def build(d):
        p = d.add_paragraph("Before the box.")
        box_xml = (
            '<w:r xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" '
            'xmlns:mc="http://schemas.openxmlformats.org/markup-compatibility/2006" '
            'xmlns:wps="http://schemas.microsoft.com/office/word/2010/wordprocessingShape">'
            '<mc:AlternateContent><mc:Choice Requires="wps"><w:drawing><wps:txbx>'
            '<w:txbxContent><w:p><w:r><w:t>Callout: 600 units</w:t></w:r></w:p></w:txbxContent>'
            '</wps:txbx></w:drawing></mc:Choice><mc:Fallback><w:pict>'
            '<w:txbxContent><w:p><w:r><w:t>Callout: 600 units</w:t></w:r></w:p></w:txbxContent>'
            '</w:pict></mc:Fallback></mc:AlternateContent></w:r>'
        )
        p._p.append(parse_xml(box_xml))

    text = DocxTextExtractor().extract(_docx(build)).units[0].text
    assert text.count("Callout: 600 units") == 1


def _with_footnote(docx_bytes: bytes, note: str) -> bytes:
    """Adds a footnotes part to a .docx, which python-docx cannot do itself."""
    import zipfile

    src = zipfile.ZipFile(io.BytesIO(docx_bytes))
    out = io.BytesIO()
    w = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
    footnotes = (
        f'<w:footnotes xmlns:w="{w}">'
        '<w:footnote w:type="separator" w:id="-1"><w:p><w:r><w:separator/></w:r></w:p></w:footnote>'
        f'<w:footnote w:id="1"><w:p><w:r><w:t>{note}</w:t></w:r></w:p></w:footnote>'
        '</w:footnotes>'
    )
    rel = ('<Relationship Id="rIdFn" Type="http://schemas.openxmlformats.org/officeDocument/'
           '2006/relationships/footnotes" Target="footnotes.xml"/>')
    ctype = ('<Override PartName="/word/footnotes.xml" ContentType="application/'
             'vnd.openxmlformats-officedocument.wordprocessingml.footnotes+xml"/>')
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as dst:
        for item in src.infolist():
            data = src.read(item.filename)
            if item.filename == "word/_rels/document.xml.rels":
                data = data.replace(b"</Relationships>", rel.encode() + b"</Relationships>")
            elif item.filename == "[Content_Types].xml":
                data = data.replace(b"</Types>", ctype.encode() + b"</Types>")
            dst.writestr(item, data)
        dst.writestr("word/footnotes.xml", footnotes)
    return out.getvalue()


# ==============================================================================
# Spreadsheets: once, not twice
# ==============================================================================

def _xlsx(build) -> bytes:
    wb = Workbook()
    build(wb.active)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def test_a_sheet_is_chunked_once_as_its_table():
    """Every sheet was chunked twice: 6,142 prose passages duplicating 8,420 table ones."""
    def build(ws):
        ws.title = "Complaints"
        ws.append(["Ward", "Status"])
        ws.append(["Ward - 43", "Resolved"])

    content = XlsxTextExtractor().extract(_xlsx(build))
    chunks = _chunks(content, XLSX_MIME)

    assert content.units[0].text_is_table
    assert [c.is_table for c in chunks] == [True]
    assert chunks[0].section_heading == "Complaints"


def test_spreadsheet_numbers_and_dates_read_as_written():
    import datetime

    def build(ws):
        ws.append(["Mobile", "Date", "Share"])
        # Naive on purpose: Excel has no time zones, and openpyxl refuses an aware datetime.
        ws.append([9910257052.0, datetime.datetime(2026, 5, 29), 0.125])  # noqa: DTZ001
        ws["C2"].number_format = "0.0%"

    rows = XlsxTextExtractor().extract(_xlsx(build)).tables[0].rows
    assert rows[1] == ["9910257052", "2026-05-29", "12.5%"]


def test_a_merged_label_fills_down_but_not_across():
    def build(ws):
        ws.append(["Owner", "Plant"])
        ws.append(["State", "Panipat"])
        ws.append([None, "Rajghat"])
        ws.merge_cells("A2:A3")
        ws.append(["Spanning header", None])
        ws.merge_cells("A4:B4")

    rows = XlsxTextExtractor().extract(_xlsx(build)).tables[0].rows
    assert rows[2] == ["State", "Rajghat"]
    assert rows[3] == ["Spanning header", ""]


def test_old_extraction_json_still_loads_and_chunks():
    """Stored artifacts predate the new fields; they must read back with the defaults."""
    stored = {
        "document_id": str(uuid.uuid4()), "mime_type": XLSX_MIME,
        "units": [{"index": 1, "label": "Sheet: S", "text": "a\tb"}],
        "headings": [{"text": "S", "level": 1, "unit_index": 1}],
        "tables": [{"unit_index": 1, "rows": [["a", "b"]]}],
    }
    result = ExtractionResult.model_validate(stored)
    assert result.units[0].text_is_table is False
    assert result.tables[0].heading is None
    assert result.ocr_unreadable_lines == {}


@pytest.mark.parametrize("rows,expected", [
    ([["Title", "", ""], ["A", "B", "C"], ["1", "2", "3"]], ["Title", "A | B | C", "1 | 2 | 3"]),
    ([["A", "B", "C"], ["1", "2", "3"]], ["A | B | C", "1 | 2 | 3"]),
])
def test_the_header_row_is_found_below_title_rows(rows, expected):
    rendered = Chunker()._render_table(ExtractedTable(unit_index=1, rows=rows))
    assert rendered[0].splitlines() == expected
