"""Reading a table out of a picture: the finder, the reader, and the repair of damaged headers.

The OCR engine and the grid finder are replaced by fakes that return what the real ones measured,
so what is pinned here is the reader's own decisions -- which cell a word belongs to, what is read
again, what is not a table -- without loading a model. One test at the end runs the real OCR over a
table drawn in the test, so the pieces are also known to fit.
"""

import io
import uuid

import numpy as np
import pytest
from docx import Document as DocxDocument
from PIL import Image
from pptx import Presentation
from pptx.util import Inches

from src.core.config import settings
from src.modules.document_pipeline.extraction import picture_source, pictures
from src.modules.document_pipeline.extraction.picture_source import find_pictures
from src.modules.document_pipeline.extraction.picture_tables import (
    ImageReading,
    ImageTable,
    TableImageReader,
    _Grid,
    repair_month_headers,
    split_fused_header,
)
from src.modules.document_pipeline.extraction.pictures import (
    TABLE_NOTICE,
    OfficePictureReader,
)
from src.modules.document_pipeline.extraction.service import ExtractionService
from src.modules.document_pipeline.formats import DOCX_MIME, PPTX_MIME

# ==============================================================================
# A fake engine and grid finder
# ==============================================================================


class _Out:
    def __init__(self, txts=(), scores=(), boxes=()):
        self.txts, self.scores, self.boxes = tuple(txts), tuple(scores), tuple(boxes)


class _Engine:
    """Reads back fixed words. A call without the detector (one cell, on its own) takes its answer
    from `line_reads` in order, and is recorded."""

    def __init__(self, words, line_reads=(), label_words=()):
        self.words, self.line_reads, self.detector_calls = words, list(line_reads), []
        # What the sideways-labels picture reads as once turned a quarter: 1000 rows tall.
        self.label_words = list(label_words)

    def __call__(self, image, use_det=True, use_cls=True, use_rec=True):
        self.detector_calls.append(use_det)
        if not use_det:
            if not self.line_reads:
                return _Out()
            text, score = self.line_reads.pop(0)
            return _Out([text], [score]) if text else _Out()
        if image.shape[0] < 300:  # a crop of one cell: the page's words are not in it
            return _Out()
        words = self.label_words if image.shape[0] == 1000 else self.words
        boxes = [np.array([[x1, y1], [x2, y1], [x2, y2], [x1, y2]]) for *_, x1, y1, x2, y2 in words]
        return _Out([w[0] for w in words], [w[1] for w in words], boxes)


def _grid(row_edges, col_edges, merged=()):
    """A ruled grid. `merged` is (row0, col0, row1, col1) blocks, each one box at every position."""
    cells = [
        [(col_edges[c], row_edges[r], col_edges[c + 1], row_edges[r + 1])
         for c in range(len(col_edges) - 1)]
        for r in range(len(row_edges) - 1)
    ]
    for r0, c0, r1, c1 in merged:
        box = (col_edges[c0], row_edges[r0], col_edges[c1 + 1], row_edges[r1 + 1])
        for r in range(r0, r1 + 1):
            for c in range(c0, c1 + 1):
                cells[r][c] = box
    return _Grid(bbox=(col_edges[0], row_edges[0], col_edges[-1], row_edges[-1]), cells=cells)


def _word(grid, r, c, text, score=0.99):
    x1, y1, x2, y2 = grid.cells[r][c]
    cx, cy = (x1 + x2) / 2, (y1 + y2) / 2
    return (text, score, cx - 25, cy - 10, cx + 25, cy + 10)


ROWS, COLS = [100, 200, 300, 400], [100, 300, 500, 700]


def _image(ink_box=None):
    image = np.full((600, 1000, 3), 255, dtype=np.uint8)  # wide enough that it is not upscaled
    if ink_box:
        x1, y1, x2, y2 = ink_box
        image[y1 + 8:y2 - 8, x1 + 20:x2 - 20] = 0
    return image


def _reader(engine, grid, **kw):
    return TableImageReader(engine=engine, grid_finder=lambda _img: [grid], **kw)


# ==============================================================================
# Which cell a word belongs to
# ==============================================================================

def test_words_land_in_the_cell_that_holds_them():
    grid = _grid(ROWS, COLS)
    words = [_word(grid, r, c, t) for r, row in enumerate(
        [["Category", "Total", "Norm"], ["Category 1", "206", "600"], ["Category 2", "323", "450"]]
    ) for c, t in enumerate(row)]
    [table] = _reader(_Engine(words), grid).read(_image()).tables
    assert table.rows == [["Category", "Total", "Norm"], ["Category 1", "206", "600"],
                          ["Category 2", "323", "450"]]


def test_a_merged_block_is_written_into_every_position_it_covers():
    grid = _grid(ROWS, COLS, merged=[(1, 2, 2, 2)])
    words = [_word(grid, 0, 0, "Region"), _word(grid, 0, 1, "Levels"), _word(grid, 0, 2, "FGD"),
             _word(grid, 1, 0, "1"), _word(grid, 1, 1, ">40"), _word(grid, 1, 2, "Not required"),
             _word(grid, 2, 0, "2"), _word(grid, 2, 1, ">30")]
    [table] = _reader(_Engine(words), grid).read(_image()).tables
    assert [row[2] for row in table.rows] == ["FGD", "Not required", "Not required"]


def test_a_two_row_header_becomes_one_row_of_full_names():
    rows = [100, 160, 220, 300, 380]
    grid = _grid(rows, [100, 250, 400, 550], merged=[(0, 1, 0, 2)])
    words = [_word(grid, 0, 0, "Parameter"), _word(grid, 0, 1, "SO2"),
             _word(grid, 1, 0, "Size"), _word(grid, 1, 1, "<500 MW"), _word(grid, 1, 2, ">=500 MW"),
             _word(grid, 2, 0, "Before 2003"), _word(grid, 2, 1, "600"), _word(grid, 2, 2, "200"),
             _word(grid, 3, 0, "2004-16"), _word(grid, 3, 1, "600"), _word(grid, 3, 2, "200")]
    [table] = _reader(_Engine(words), grid).read(_image()).tables
    assert table.rows[0] == ["Parameter Size", "SO2 <500 MW", "SO2 >=500 MW"]


# ==============================================================================
# What is, and is not, a table
# ==============================================================================

def test_the_title_above_and_the_notes_below_come_back_apart_from_the_grid():
    grid = _grid(ROWS, COLS)
    words = [_word(grid, r, c, f"{r}{c}") for r in range(3) for c in range(3)]
    words += [("3. SO2 Emission Report", 0.99, 120, 62, 420, 86),
              ("Note: Category 1 commissioned before 2003", 0.99, 120, 430, 600, 454)]
    reading = _reader(_Engine(words), grid).read(_image())
    assert reading.tables[0].title == "3. SO2 Emission Report"
    assert reading.lines == ["Note: Category 1 commissioned before 2003"]


def test_a_frame_round_a_few_cells_is_not_a_table():
    """Four map panels in a frame were a 2 x 2 table of their year labels."""
    grid = _grid([100, 250, 400], [100, 400, 700])
    words = [_word(grid, 0, 0, "2005"), _word(grid, 0, 1, "2009"),
             _word(grid, 1, 0, "2016"), _word(grid, 1, 1, "2019")]
    reading = _reader(_Engine(words), grid).read(_image())
    assert reading.tables == []


def test_a_grid_that_is_mostly_empty_is_not_a_table():
    grid = _grid(ROWS, COLS)
    reading = _reader(_Engine([_word(grid, 0, 0, "only")]), grid).read(_image())
    assert reading.tables == []


def test_text_in_a_picture_with_no_table_comes_back_as_lines():
    words = [("OMI SO2 VCD (DU)", 0.99, 100, 500, 400, 520), ("0.00 0.15 0.30", 0.97, 100, 530, 400, 550)]
    reading = TableImageReader(engine=_Engine(words), grid_finder=lambda _i: []).read(_image())
    assert reading.lines == ["OMI SO2 VCD (DU)", "0.00 0.15 0.30"]


def test_a_guessed_word_outside_a_table_is_dropped():
    """A wrong word in a passage that will be cited is worse than a gap (OCR_MIN_CONFIDENCE)."""
    words = [("Clear line of text", 0.99, 100, 500, 400, 520), ("Pxo l i n", 0.55, 100, 530, 400, 550),
             ("Another clear one", 0.98, 100, 560, 400, 580)]
    reading = TableImageReader(engine=_Engine(words), grid_finder=lambda _i: [],
                               min_confidence=0.8).read(_image())
    assert reading.lines == ["Clear line of text", "Another clear one"]


# ==============================================================================
# Cells read a second time
# ==============================================================================

def test_an_empty_cell_with_ink_in_it_is_read_on_its_own():
    """A yellow-highlighted "239.0" came back blank from the whole-picture pass."""
    grid = _grid([100, 140, 180, 220], COLS)  # rows one line tall, as a table of figures is
    words = [_word(grid, r, c, f"{r}{c}") for r in range(3) for c in range(3) if (r, c) != (1, 1)]
    engine = _Engine(words, line_reads=[("239.0", 0.95)])
    [table] = _reader(engine, grid).read(_image(ink_box=grid.cells[1][1])).tables
    assert table.rows[1][1] == "239.0"
    assert False in engine.detector_calls, "the cell was read without the detector"


def test_an_empty_cell_with_nothing_in_it_stays_empty():
    grid = _grid(ROWS, COLS)
    words = [_word(grid, r, c, f"{r}{c}") for r in range(3) for c in range(3) if (r, c) != (1, 1)]
    engine = _Engine(words, line_reads=[("noise", 0.99)])
    [table] = _reader(engine, grid).read(_image()).tables
    assert table.rows[1][1] == ""


def test_a_doubtful_word_takes_the_second_reading_when_it_scores_higher():
    """The same pixels read "164" or "1964" depending on how the picture was resized."""
    grid = _grid(ROWS, COLS)
    words = [_word(grid, r, c, f"{r}{c}") for r in range(3) for c in range(3) if (r, c) != (1, 1)]
    words.append(_word(grid, 1, 1, "1964", score=0.82))
    [table] = _reader(_Engine(words, line_reads=[("164", 0.99)]), grid).read(_image()).tables
    assert table.rows[1][1] == "164"


def test_a_doubtful_word_keeps_its_reading_when_the_second_scores_lower():
    grid = _grid(ROWS, COLS)
    words = [_word(grid, r, c, f"{r}{c}") for r in range(3) for c in range(3) if (r, c) != (1, 1)]
    words.append(_word(grid, 1, 1, "164", score=0.82))
    [table] = _reader(_Engine(words, line_reads=[("1964", 0.5)]), grid).read(_image()).tables
    assert table.rows[1][1] == "164"


def test_a_sideways_header_is_tried_both_ways_round_and_the_better_read_kept():
    grid = _grid([100, 220, 300, 380], [100, 300, 360, 420, 480])
    words = [_word(grid, 0, 0, "Plant")] + [
        # Detected boxes of sideways text are tall.
        ("Apri", 0.95, 320, 130, 345, 200),
        ("May", 0.95, 375, 130, 400, 200), ("June", 0.95, 435, 130, 460, 200),
    ]
    words += [_word(grid, r, c, f"{r}{c}") for r in (1, 2) for c in range(4)]
    engine = _Engine(words, line_reads=[("xxx", 0.3), ("April-19", 0.9), ("May-19", 0.95), ("???", 0.2),
                                        ("June-19", 0.9), ("zzz", 0.1)])
    [table] = _reader(engine, grid).read(_image()).tables
    assert table.rows[0][1:] == ["April-19", "May-19", "June-19"]


def test_a_digit_in_a_tall_narrow_cell_is_not_rotated():
    """A serial-number column is tall and narrow and holds ordinary horizontal digits."""
    grid = _grid([100, 220, 340, 460], [100, 160, 400, 640])
    words = [_word(grid, r, c, f"{r}{c}") for r in range(3) for c in range(3)]
    words[3] = ("2", 0.99, 112, 250, 148, 290)  # row 1, col 0: wide for its height
    engine = _Engine(words, line_reads=[("La", 0.99), ("La", 0.99)])
    [table] = _reader(engine, grid).read(_image()).tables
    assert table.rows[1][0] == "2"


# ==============================================================================
# Month headers
# ==============================================================================

def test_damaged_month_labels_are_rebuilt_from_the_ones_that_read():
    row = ["NTPC Plants", "APpi-19", "May-19", "une-19", "l--19", "AU-19", "SeP-9", "0d-19", "Nov-19",
           "Dec-19", "Jan-20", "Feb-20", "Mar-20", "Data Type"]
    fixed = repair_month_headers(row)
    assert fixed[1:13] == ["April-19", "May-19", "June-19", "Jul-19", "Aug-19", "Sep-19", "Oct-19",
                           "Nov-19", "Dec-19", "Jan-20", "Feb-20", "Mar-20"]
    assert fixed[0] == "NTPC Plants" and fixed[13] == "Data Type"


def test_the_year_rolls_over_with_the_months():
    row = ["Plant", "Oct-19", "Nov-19", "Dec-19", "Jan-20", "x-9", "Mar-20"]
    assert repair_month_headers(row)[5] == "Febru-20" or repair_month_headers(row)[5].startswith("Feb")
    assert repair_month_headers(row)[5].endswith("-20")


def test_a_quarterly_header_is_left_alone():
    row = ["Plant", "Jan-19", "Mar-19", "Jun-19", "Sep-19", "Dec-19"]
    assert repair_month_headers(row) == row


def test_a_header_that_is_not_months_is_left_alone():
    row = ["Plant", "Total", "Notes", "Year 2019", "Rank"]
    assert repair_month_headers(row) == row


def test_a_word_in_the_middle_of_a_run_stops_the_repair():
    row = ["Plant", "Jan-19", "Feb-19", "Total", "Apr-19", "May-19", "Jun-19"]
    assert repair_month_headers(row) == row


def test_a_header_with_too_few_readable_months_is_left_alone():
    row = ["Plant", "Jan-19", "xx-9", "yy-19", "Apr-19"]
    assert repair_month_headers(row) == row


def test_a_header_run_into_the_first_row_is_kept_for_its_labels_and_loses_its_figures():
    """Sideways labels drawn too low land on the row below: "pril-19 5.69" is a label and a figure
    that a letter lay across. The labels are recoverable; the figure is not, and a figure wrong by
    a digit is worse than a missing one."""
    rows = [
        ["NLC/DVC Plants NLCIL/TPS-II", "pril-19 5.69", "ay-19 5.9", "ine-19 5.8", "ly-19 T.2",
         "Aug-19 4.0", "ep-19 3.7", "ct-19 3.7", "lov-19 3.3", "Data Tvne Max. Avg"],
        ["NLCIL/TPS-1", "7.4", "8.5", "6.4", "6.1", "8.1", "7.3", "9.9", "5.4", "Max. Avg"],
        ["NLCIL/TPS-2", "9.6", "9.2", "9.3", "6.3", "7.8", "7.7", "11.0", "5.5", "Max. Avg"],
    ]
    fixed = split_fused_header(rows)
    assert len(fixed) == 3
    assert fixed[0][1:4] == ["pril-19", "ay-19", "ine-19"]
    assert "5.69" not in " ".join(fixed[0]) and fixed[1:] == rows[1:]
    assert repair_month_headers(fixed[0])[1:9] == [
        "April-19", "May-19", "June-19", "July-19", "Aug-19", "Sep-19", "Oct-19", "Nov-19"]


def test_a_header_cell_with_no_text_takes_the_sideways_labels_standing_over_it():
    """The metafile converter draws month labels over the first row, so they are drawn apart and
    read turned: a word's position down the turned picture is its position across the original."""
    grid = _grid(ROWS, COLS)
    words = [_word(grid, 0, 0, "Plant")] + [
        _word(grid, r, c, f"{r}{c}") for r in (1, 2) for c in range(3)
    ]
    # In the quarter-turned picture a label y is where it stands across the original: 400 is
    # over the second column (300-500) and 600 over the third (500-700).
    labels = [("April-19", 0.97, 100, 390, 160, 410), ("May-19", 0.97, 100, 590, 160, 610)]
    engine = _Engine(words, label_words=labels)
    [table] = _reader(engine, grid).read(_image(), labels_image=_image()).tables
    assert table.rows[0] == ["Plant", "April-19", "May-19"]
    assert table.rows[1] == ["10", "11", "12"]


def test_an_ordinary_first_row_of_figures_is_not_split():
    rows = [["Plant", "April-19", "May-19", "Jun-19", "Jul-19", "Type"],
            ["A", "5.9", "5.9", "5.8", "7.2", "Max."], ["B", "7.4", "8.5", "6.4", "6.1", "Max."]]
    assert split_fused_header(rows) == rows


# ==============================================================================
# Finding the pictures
# ==============================================================================

def _png(color, size=(120, 90)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, format="PNG")
    return buf.getvalue()


def _deck(*slides, hidden=()) -> bytes:
    prs = Presentation()
    for i, pictures_on_slide in enumerate(slides, start=1):
        slide = prs.slides.add_slide(prs.slide_layouts[5])
        slide.shapes.title.text = f"Slide {i} title"
        for top, blob in enumerate(pictures_on_slide, start=1):
            slide.shapes.add_picture(io.BytesIO(blob), Inches(1), Inches(top), Inches(2), Inches(1))
        if i in hidden:
            slide._element.set("show", "0")
    buf = io.BytesIO()
    prs.save(buf)
    return buf.getvalue()


def test_pictures_are_found_on_the_slide_they_are_on_and_hidden_slides_are_not_renumbered():
    deck = _deck([_png("red")], [_png("green")], [_png("blue")], hidden=(2,))
    found = find_pictures(deck, PPTX_MIME)
    assert [p.unit_index for p in found] == [1, 3]
    assert all(p.extension == "png" for p in found)


def test_a_word_picture_is_found_under_the_heading_it_sits_beneath():
    d = DocxDocument()
    d.add_heading("Background", level=1)
    d.add_paragraph("Intro.")
    d.add_heading("FGD process", level=1)
    d.add_paragraph().add_run().add_picture(io.BytesIO(_png("red")), width=Inches(1))
    table = d.add_table(rows=1, cols=1)
    table.cell(0, 0).paragraphs[0].add_run().add_picture(io.BytesIO(_png("blue")), width=Inches(1))
    d.add_heading("Results", level=1)
    d.add_paragraph().add_run().add_picture(io.BytesIO(_png("green")), width=Inches(1))
    buf = io.BytesIO()
    d.save(buf)

    found = find_pictures(buf.getvalue(), DOCX_MIME)
    assert [(p.unit_index, p.heading) for p in found] == [
        (1, "FGD process"), (1, "FGD process"), (1, "Results"),
    ]


# ==============================================================================
# What the reader sends on
# ==============================================================================

class _Tables:
    """A table reader that records what it was shown and returns a fixed reading."""

    def __init__(self, reading=None):
        self.reading = reading or ImageReading(
            tables=[ImageTable(rows=[["Category", "Norm"], ["1", "600"]], bbox=(0, 0, 1, 1),
                               title="3. SO2 Emission Report")],
            lines=["Note: 1 is before 2003"],
        )
        self.shown = []

    def read(self, image, labels_image=None):
        self.shown.append(image.shape[:2])
        self.labels_shown = getattr(self, "labels_shown", []) + [labels_image is not None]
        return self.reading


def _big(color):
    return _png(color, size=(300, 200))


def test_a_table_comes_back_with_its_title_and_a_notice_that_it_was_read_by_ocr():
    tables = _Tables()
    [picture] = OfficePictureReader(tables).read(_deck([_big("red")]), PPTX_MIME)
    [rows] = picture.tables
    assert rows[0][0] == f"3. SO2 Emission Report ({TABLE_NOTICE})"
    assert rows[0][1:] == [""] and rows[1:] == [["Category", "Norm"], ["1", "600"]]
    assert picture.text == "Note: 1 is before 2003"


def test_the_notice_survives_chunking_as_the_tables_caption():
    from tests.unit.test_pictures import _chunks

    result = ExtractionService(picture_reader=OfficePictureReader(_Tables())).extract(
        uuid.uuid4(), _deck([_big("red")]), PPTX_MIME)
    [chunk] = [c for c in _chunks(result, PPTX_MIME) if c.is_table]
    lines = chunk.text.splitlines()
    assert lines[0] == f"3. SO2 Emission Report ({TABLE_NOTICE})"
    assert lines[1:] == ["Category | Norm", "1 | 600"]


def test_a_picture_on_many_slides_is_a_logo_and_is_not_read():
    logo, once = _big("red"), _big("blue")
    tables = _Tables()
    OfficePictureReader(tables).read(_deck([logo], [logo], [logo, once]), PPTX_MIME)
    assert len(tables.shown) == 1


def test_a_picture_too_small_to_be_a_figure_is_not_read():
    tables = _Tables()
    OfficePictureReader(tables).read(_deck([_png("red", size=(40, 40))]), PPTX_MIME)
    assert tables.shown == []


def test_a_picture_that_says_nothing_adds_nothing():
    tables = _Tables(ImageReading())
    assert OfficePictureReader(tables).read(_deck([_big("red")]), PPTX_MIME) == []


def test_the_per_document_limit_is_applied(monkeypatch):
    monkeypatch.setattr(settings, "PICTURE_MAX_PER_DOCUMENT", 2)
    tables = _Tables()
    OfficePictureReader(tables).read(
        _deck([_big("red")], [_big("green")], [_big("blue")], [_big("yellow")]), PPTX_MIME)
    assert len(tables.shown) == 2


def test_a_picture_that_fails_to_read_costs_the_rest_nothing():
    class _Failing(_Tables):
        def read(self, image, labels_image=None):
            if not self.shown:
                self.shown.append(1)
                raise RuntimeError("model failed")
            return super().read(image, labels_image)

    result = OfficePictureReader(_Failing()).read(_deck([_big("red")], [_big("green")]), PPTX_MIME)
    assert [p.unit_index for p in result] == [2]


def test_vector_pictures_are_reported_not_silently_dropped_when_the_renderer_is_missing(monkeypatch):
    monkeypatch.setattr(picture_source, "find_pictures",
                        lambda data, mime: [picture_source.FoundPicture(1, None, b"emf-bytes", "emf")])
    monkeypatch.setattr(picture_source, "renderer_available", lambda: False)
    warnings = []
    monkeypatch.setattr(pictures.logger, "warning", lambda msg, *a, **k: warnings.append(msg % a))
    tables = _Tables()

    assert OfficePictureReader(tables).read(b"", PPTX_MIME) == []
    assert tables.shown == []
    assert len(warnings) == 1 and "emf2svg" in warnings[0] and "1 vector" in warnings[0]


def test_vector_pictures_are_read_once_rendered(monkeypatch):
    monkeypatch.setattr(picture_source, "find_pictures",
                        lambda data, mime: [picture_source.FoundPicture(2, None, b"emf-bytes", "emf")])
    rendered = picture_source.RenderedVector(_big("red"), _big("blue"))
    monkeypatch.setattr(picture_source, "rasterise_vectors", lambda items: {0: rendered})
    tables = _Tables()
    [picture] = OfficePictureReader(tables).read(b"", PPTX_MIME)
    assert picture.unit_index == 2 and len(tables.shown) == 1
    assert tables.labels_shown == [True], "the sideways labels reach the reader with the picture"


# ==============================================================================
# The real thing, end to end
# ==============================================================================

def _drawn_table() -> np.ndarray:
    """A ruled table drawn with OpenCV, big enough to read."""
    import cv2

    image = np.full((420, 900, 3), 255, dtype=np.uint8)
    cells = [["Category", "Total Units", "Norm"], ["Category 1", "206", "600"],
             ["Category 2", "323", "450"], ["Category 3", "69", "100"]]
    xs, ys = [20, 340, 620, 880], [20, 110, 190, 270, 350]
    for x in xs:
        cv2.line(image, (x, ys[0]), (x, ys[-1]), (0, 0, 0), 3)
    for y in ys:
        cv2.line(image, (xs[0], y), (xs[-1], y), (0, 0, 0), 3)
    for r, row in enumerate(cells):
        for c, text in enumerate(row):
            cv2.putText(image, text, (xs[c] + 25, ys[r] + 55), cv2.FONT_HERSHEY_SIMPLEX, 1.3,
                        (0, 0, 0), 3, cv2.LINE_AA)
    return image


@pytest.fixture(scope="module")
def _real_reader():
    return TableImageReader(threads=2, min_confidence=0.8, cell_min_confidence=0.4)


def test_a_drawn_table_is_read_with_every_figure_in_its_row_and_column(_real_reader):
    reading = _real_reader.read(_drawn_table())
    [table] = reading.tables
    assert len(table.rows) == 4 and len(table.rows[0]) == 3
    assert [row[1:] for row in table.rows[1:]] == [["206", "600"], ["323", "450"], ["69", "100"]]
    assert table.rows[0][0].lower().startswith("categ")


def test_a_deck_with_a_table_picture_is_searchable_by_its_figures(_real_reader):
    import cv2

    _, png = cv2.imencode(".png", _drawn_table())
    deck = _deck([png.tobytes()])
    from tests.unit.test_pictures import _chunks

    result = ExtractionService(picture_reader=OfficePictureReader(_real_reader)).extract(
        uuid.uuid4(), deck, PPTX_MIME)
    [chunk] = [c for c in _chunks(result, PPTX_MIME) if c.is_table]
    assert chunk.page_number == 1 and chunk.section_heading == "Slide 1 title"
    assert TABLE_NOTICE in chunk.text and "323 | 450" in chunk.text


_SVG_WITH_LABELS = (
    '<svg xmlns="http://www.w3.org/2000/svg" width="500" height="200"><g>'
    '<defs><clipPath id="c1"><path d="M 0,0 L 500,0 L 500,200 L 0,200 Z"/></clipPath></defs>'
    '<rect x="1" y="1" width="498" height="198" fill="none"/>'
    '<text clip-path="url(#c1)" x="10" y="20">Plant</text>'
    '<text clip-path="url(#c1)" transform="rotate(-90, 120.0, 150.0) translate(0, 9)" x="120" y="150">April-19</text>'
    '<text clip-path="url(#c1)" x="10" y="60">5.9</text>'
    '</g></svg>'
)


def test_sideways_text_is_drawn_apart_from_the_rest_of_the_picture():
    data, labels = picture_source.split_rotated_labels(_SVG_WITH_LABELS.encode())
    assert b"April-19" not in data and b"Plant" in data and b"5.9" in data
    assert labels is not None
    assert b"April-19" in labels and b"Plant" not in labels and b"5.9" not in labels
    assert b"<rect" not in labels, "only the text is wanted, drawn alone"
    assert b"clipPath" in labels, "the clip path the text refers to is kept"


def test_a_picture_with_no_sideways_text_is_left_as_it_is():
    plain = (b'<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10">'
             b'<text x="1" y="5">flat</text></svg>')
    assert picture_source.split_rotated_labels(plain) == (plain, None)


def test_a_metafile_is_recognised_by_its_header_whatever_it_is_called():
    """The "WMF" pictures of the Secretary Power deck are Enhanced Metafiles: ".wmf" in the file,
    "EMF" at byte 40 of the data."""
    emf = b"\x01\x00\x00\x00\x6c\x00\x00\x00" + b"\x00" * 32 + b" EMF" + b"\x00" * 20
    assert picture_source.is_emf(emf)
    assert not picture_source.is_emf(b"\xd7\xcd\xc6\x9a" + b"\x00" * 60)
    assert not picture_source.is_emf(b"")


def test_nothing_is_attempted_without_the_renderer_or_for_a_file_it_cannot_draw(monkeypatch):
    monkeypatch.setattr(picture_source, "renderer_available", lambda: False)
    assert picture_source.rasterise_vectors({0: (b"x" * 60, "emf")}) == {}

    monkeypatch.setattr(picture_source, "renderer_available", lambda: True)
    ran = []
    monkeypatch.setattr(picture_source.subprocess, "run", lambda *a, **k: ran.append(a))
    assert picture_source.rasterise_vectors({0: (b"not-an-emf" * 8, "wmf")}) == {}
    assert ran == [], "a file that is not an EMF must not reach the converter"
