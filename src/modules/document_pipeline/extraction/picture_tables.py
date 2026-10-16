"""Reading a table out of a picture of one.

A deck's chart or table is often pasted in as a screenshot. The numbers are pixels, and OCR alone
returns lines with no grid, so "1209" loses its row and its column. Two small tools recover them
without a model that needs PyTorch:

* **img2table** finds the ruled grid with OpenCV -- the lines, the cells, which cells are one
  block merged over several. No inference, 0.04 seconds.
* **rapidocr**, already here for scanned pages, reads the words; each lands in the cell whose
  area holds it.

Chosen over Docling by scoring both against eight tables read by eye (242 cells): 98% of cells and
100% of figures against 78% and 75%, in 1.7 seconds a table against 3-14, in about 100MB rather
than PyTorch and 730MB of models. Docling's failures were structural -- it moved the "Category"
column to the end of the row and merged header cells -- which a figure in the wrong column makes
worse than a missing one.

What it adds on top of those two, each from a failure measured on the real pictures:

* a cell the OCR left empty that has ink in it is read again on its own (a yellow-highlighted
  "239.0" came back blank), as is one a single detected box straddled;
* month headers drawn sideways ("April-19" rotated) read as "APriI-9" and "y-19". Months run in
  sequence, so the damaged ones are rebuilt from the ones that read, and left alone unless the
  sequence is plain;
* a picture is a table only if it has a grid of at least six cells, mostly filled -- four map
  panels in a frame were otherwise a 2 x 2 table of their year labels;
* the title above the table and the notes below it come back too, apart from the grid.

It does not read tables with no ruling lines (a plain-text table with only a top and bottom
rule): img2table's borderless mode split wrapped lines into rows and merged columns, which is worse
than the plain text the caller falls back to.
"""

from __future__ import annotations

import logging
import re
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from typing import Any

from src.modules.document_pipeline.extraction.grid import Span, flatten_header

logger = logging.getLogger(__name__)

# Fewer than this many cells is a frame round something, not a table.
_MIN_CELLS = 6
# Share of cells that must hold text. A grid of photographs or map panels holds almost none.
_MIN_FILLED = 0.5
_MAX_TABLES = 3
# Small pictures are read at twice the size (one cell more of 242 was right). Larger ones are not:
# the OCR reads a picture in time proportional to its pixels, and above this it is already large
# enough that it was being scaled back down.
_UPSCALE_BELOW_PX = 900
# A pixel darker than this is ink, when asking whether an empty cell was really empty.
_INK_GRAY = 110
_INK_SHARE = 0.01
# A word the engine scored below this is read a second time from its own crop.
_DOUBTFUL = 0.9
# A tall narrow cell is a header written sideways.
_SIDEWAYS_RATIO = 1.6


@dataclass
class _Word:
    text: str
    score: float
    x1: float
    y1: float
    x2: float
    y2: float

    @property
    def cx(self) -> float:
        return (self.x1 + self.x2) / 2

    @property
    def cy(self) -> float:
        return (self.y1 + self.y2) / 2

    @property
    def height(self) -> float:
        return self.y2 - self.y1


@dataclass
class _Grid:
    """A ruled table as the cell box at every grid position. A merged block is one box that
    appears at each position it covers."""

    bbox: tuple[int, int, int, int]
    cells: list[list[tuple[int, int, int, int]]]


@dataclass
class ImageTable:
    rows: list[list[str]]
    bbox: tuple[int, int, int, int]
    title: str = ""


@dataclass
class ImageReading:
    tables: list[ImageTable] = field(default_factory=list)
    # Text outside any table, in reading order: a table's notes, or all of a picture that has none.
    lines: list[str] = field(default_factory=list)


class TableImageReader:
    """Reads the tables, and the text around them, in one picture.

    `engine` and `grid_finder` are injectable so the logic around them is testable without a
    model; in use both are built on first need, so a worker that never meets a picture loads
    neither.
    """

    def __init__(
        self,
        threads: int = 0,
        min_confidence: float = 0.8,
        cell_min_confidence: float = 0.4,
        engine: Callable[[Any], Any] | None = None,
        grid_finder: Callable[[Any], list[_Grid]] | None = None,
    ):
        self._threads = threads
        self._min_confidence = min_confidence
        # Inside a table a figure is the point and its row and column are known, so a lower floor
        # than for loose text, where a guessed word is worse than a gap.
        self._cell_min_confidence = cell_min_confidence
        self._engine_obj = engine
        self._grid_finder = grid_finder or _find_grids
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # The engine
    # ------------------------------------------------------------------

    def _engine(self):
        if self._engine_obj is not None:
            return self._engine_obj
        with self._lock:
            if self._engine_obj is not None:
                return self._engine_obj
            import os

            from rapidocr import LangRec, RapidOCR

            threads = self._threads or min(8, os.cpu_count() or 1)
            # Thread count is not a tuning detail: left at the default OCR measured 6x slower
            # (ocr.py). English recogniser: the same model file the scanned-page OCR already
            # ships, so nothing is downloaded.
            self._engine_obj = RapidOCR(params={
                "EngineConfig.onnxruntime.intra_op_num_threads": threads,
                "EngineConfig.onnxruntime.inter_op_num_threads": 1,
                "Rec.lang_type": LangRec.EN,
            })
            return self._engine_obj

    def _words(self, image) -> list[_Word]:
        import numpy as np

        # The flags are passed every time: rapidocr keeps whatever a call set, so a recognition-only
        # call would otherwise leave the next page read without its detector.
        result = self._engine()(image, use_det=True, use_cls=True, use_rec=True)
        if result is None or getattr(result, "txts", None) is None:
            return []
        words = []
        for text, score, box in zip(result.txts, result.scores, result.boxes, strict=True):
            points = np.asarray(box, dtype=float)
            text = (text or "").strip()
            if text:
                words.append(_Word(text, float(score), points[:, 0].min(), points[:, 1].min(),
                                   points[:, 0].max(), points[:, 1].max()))
        return words

    def _line(self, crop) -> tuple[str, float]:
        """A crop holding one line of text, read without the detector: a month label, a number.
        Running the detector over a thin crop upscales it to a huge one (6 crops took 79s)."""
        result = self._engine()(crop, use_det=False, use_cls=False, use_rec=True)
        texts = getattr(result, "txts", None)
        if not texts:
            return "", 0.0
        scores = [float(s) for s in result.scores]
        return " ".join(t.strip() for t in texts).strip(), sum(scores) / len(scores)

    # ------------------------------------------------------------------
    # One picture
    # ------------------------------------------------------------------

    def read(self, image, labels_image=None) -> ImageReading:
        """`image` is a BGR array as OpenCV loads it. `labels_image` is the same picture holding
        only its sideways text (see `picture_source.split_rotated_labels`), same size."""
        import cv2

        height, width = image.shape[:2]
        if min(height, width) < 40:
            return ImageReading()
        if max(height, width) < _UPSCALE_BELOW_PX:
            image = cv2.resize(image, None, fx=2.0, fy=2.0, interpolation=cv2.INTER_CUBIC)
            if labels_image is not None:
                labels_image = cv2.resize(labels_image, image.shape[1::-1],
                                          interpolation=cv2.INTER_CUBIC)

        words = self._words(image)
        labels = self._sideways_labels(labels_image) if labels_image is not None else []
        tables: list[ImageTable] = []
        for grid in self._grid_finder(image)[:_MAX_TABLES]:
            table = self._build_table(image, grid, words, labels)
            if table is not None:
                tables.append(table)

        outside = [w for w in words if not any(_inside(w, t.bbox) for t in tables)]
        lines = _group_lines([w for w in outside if w.score >= self._min_confidence])
        for table in tables:
            above = _title_above(lines, table.bbox)
            if above is not None:
                table.title = above[0]
                lines.remove(above)
        texts = [text for text, *_ in lines if len(text.strip()) > 2]
        if not tables and len(texts) < 2:
            texts = []
        return ImageReading(tables=tables, lines=texts)

    # ------------------------------------------------------------------
    # One table
    # ------------------------------------------------------------------

    def _sideways_labels(self, labels_image) -> list[tuple[str, float]]:
        """The sideways text of a picture as (text, x position in the picture), left to right.

        The picture is turned a quarter clockwise so text that reads bottom to top reads left to
        right, and a word's position down the turned picture is its position across the original.
        """
        import cv2

        turned = cv2.rotate(labels_image, cv2.ROTATE_90_CLOCKWISE)
        found = [(w.text, w.cy) for w in self._words(turned) if w.score >= self._cell_min_confidence]
        return sorted(found, key=lambda item: item[1])

    def _build_table(self, image, grid: _Grid, words: list[_Word],
                     labels: list[tuple[str, float]] | None = None) -> ImageTable | None:
        n_rows = len(grid.cells)
        n_cols = max((len(r) for r in grid.cells), default=0)
        if n_rows < 2 or n_cols < 2 or n_rows * n_cols < _MIN_CELLS:
            return None
        boxes = [row + [row[-1]] * (n_cols - len(row)) for row in grid.cells]

        # Positions sharing one box are one merged block.
        blocks: dict[tuple, list[tuple[int, int]]] = {}
        for r, row in enumerate(boxes):
            for c, box in enumerate(row):
                blocks.setdefault(box, []).append((r, c))
        spans: list[list[Span | None]] = [[None] * n_cols for _ in range(n_rows)]
        for places in blocks.values():
            r0, c0 = min(p[0] for p in places), min(p[1] for p in places)
            height = max(p[0] for p in places) - r0 + 1
            width = max(p[1] for p in places) - c0 + 1
            if len(places) > 1:
                for r, c in places:
                    spans[r][c] = (r0, c0, height, width)

        inside = [w for w in words if _inside(w, grid.bbox) and w.score >= self._cell_min_confidence]
        self._second_opinion(image, inside)
        line_height = sorted(w.height for w in inside)[len(inside) // 2] if inside else 20.0
        text_of: dict[tuple, list[_Word]] = {box: [] for box in blocks}
        reread: set[tuple] = set()
        for word in inside:
            hit = [b for b in blocks if _covers(b, word)]
            if len(hit) == 1:
                text_of[hit[0]].append(word)
            elif len(hit) > 1:
                reread.update(hit)  # one detected box across several cells: read each on its own
            else:
                centre = [b for b in blocks if b[0] <= word.cx <= b[2] and b[1] <= word.cy <= b[3]]
                if centre:
                    text_of[centre[0]].append(word)

        cell_text: dict[tuple, str] = {}
        for box, places in blocks.items():
            width, height = box[2] - box[0], box[3] - box[1]
            mine = text_of[box]
            # A header cell with no text of its own takes the sideways labels standing over it.
            if labels and not mine and min(p[0] for p in places) == 0:
                over = [text for text, x in labels if box[0] <= x <= box[2]]
                if over:
                    cell_text[box] = " ".join(over)
                    continue
            # Sideways: a tall narrow cell in the header rows whose text, where any was found,
            # is itself tall. A tall narrow cell with an ordinary digit in it is not.
            tall = [w for w in mine if (w.y2 - w.y1) > 1.3 * (w.x2 - w.x1) and len(w.text) >= 2]
            sideways = (
                height > _SIDEWAYS_RATIO * width and width > 8
                and min(p[0] for p in places) <= 1 and (not mine or bool(tall))
            )
            if box in reread or sideways or (not mine and _has_ink(image, box)):
                text = self._read_crop(image, box, sideways, line_height)
                if text:
                    cell_text[box] = text
                    continue
            cell_text[box] = " ".join(line[0] for line in _group_lines(mine))

        rows: list[list[str]] = []
        for r in range(n_rows):
            row = []
            for c in range(n_cols):
                span = spans[r][c]
                text = cell_text[boxes[r][c]]
                # A title across the whole table stays one cell for the chunker to see as a caption.
                if span and n_cols > 1 and span[3] == n_cols and (r, c) != (span[0], span[1]):
                    text = ""
                row.append(text)
            rows.append(row)

        # How full the grid is is judged as drawn, before empty rows and columns are dropped: a
        # grid of photographs holding two words would otherwise collapse to one full cell.
        cells = [c for row in rows for c in row]
        if not cells or sum(1 for c in cells if c) / len(cells) < _MIN_FILLED:
            return None

        rows = _drop_empty(flatten_header(rows, spans))
        if len(rows) < 2 or len(rows[0]) < 2:
            return None
        rows = split_fused_header(rows)
        for i in range(min(2, len(rows))):
            rows[i] = repair_month_headers(rows[i])
        return ImageTable(rows=rows, bbox=grid.bbox)

    def _second_opinion(self, image, words: list[_Word]) -> None:
        """Rereads each word the engine was not sure of from its own crop, and keeps whichever
        reading scored higher. The same pixels read "164" or "1964" depending on how the picture
        was resized (0.82 against 1.00), so a doubtful figure is not taken from one pass."""
        import cv2

        for word in words:
            if word.score >= _DOUBTFUL:
                continue
            x1, y1, x2, y2 = (int(v) for v in (word.x1 - 4, word.y1 - 4, word.x2 + 4, word.y2 + 4))
            crop = image[max(y1, 0):y2, max(x1, 0):x2]
            if crop.size == 0:
                continue
            crop = cv2.copyMakeBorder(crop, 8, 8, 8, 8, cv2.BORDER_CONSTANT, value=(255, 255, 255))
            text, score = self._line(crop)
            if text and score > word.score:
                word.text, word.score = text, score

    def _read_crop(self, image, box: tuple, sideways: bool, line_height: float) -> str:
        """One cell read on its own. A sideways one is tried both ways round; a cell of more than
        a line or two goes through the full reader, padded so the detector does not blow it up."""
        import cv2

        x1, y1, x2, y2 = (int(v) for v in box)
        crop = image[y1 + 2:y2 - 2, x1 + 2:x2 - 2]
        if crop.size == 0:
            return ""

        def pad(picture, minimum: int = 0):
            h, w = picture.shape[:2]
            extra_h, extra_w = max(minimum - h, 0) // 2 + 8, max(minimum - w, 0) // 2 + 8
            return cv2.copyMakeBorder(picture, extra_h, extra_h, extra_w, extra_w,
                                      cv2.BORDER_CONSTANT, value=(255, 255, 255))

        if sideways:
            reads = [self._line(pad(cv2.rotate(crop, code)))
                     for code in (cv2.ROTATE_90_CLOCKWISE, cv2.ROTATE_90_COUNTERCLOCKWISE)]
            text, score = max(reads, key=lambda r: r[1])
        elif crop.shape[0] <= 2.2 * line_height:
            text, score = self._line(pad(crop))
        else:
            found = [w for w in self._words(pad(crop, 160)) if w.score >= self._cell_min_confidence]
            if not found:
                return ""
            text = " ".join(line[0] for line in _group_lines(found))
            score = sum(w.score for w in found) / len(found)
        return text if score >= self._cell_min_confidence else ""


# ----------------------------------------------------------------------
# Geometry and text helpers
# ----------------------------------------------------------------------

def _inside(word: _Word, bbox: tuple) -> bool:
    return bbox[0] - 2 <= word.cx <= bbox[2] + 2 and bbox[1] - 2 <= word.cy <= bbox[3] + 2


def _covers(box: tuple, word: _Word) -> bool:
    """The word belongs to this cell: the cell holds most of the word's width and height. A word
    that is half in one cell and half in the next holds in neither."""
    width, height = word.x2 - word.x1, word.y2 - word.y1
    if width <= 0 or height <= 0:
        return False
    across = min(box[2], word.x2) - max(box[0], word.x1)
    down = min(box[3], word.y2) - max(box[1], word.y1)
    return across >= 0.8 * width and down >= 0.5 * height


def _has_ink(image, box: tuple) -> bool:
    import cv2

    x1, y1, x2, y2 = (int(v) for v in box)
    crop = image[y1 + 3:y2 - 3, x1 + 3:x2 - 3]
    if crop.size == 0 or crop.shape[0] * crop.shape[1] < 150:
        return False
    gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY) if crop.ndim == 3 else crop
    return float((gray < _INK_GRAY).mean()) > _INK_SHARE


def _group_lines(words: list[_Word]) -> list[tuple[str, float, float, float]]:
    """Words into lines: (text, centre y, left, right), top to bottom, words left to right."""
    if not words:
        return []
    typical = sorted(w.height for w in words)[len(words) // 2] or 1.0
    lines: list[list[_Word]] = []
    for word in sorted(words, key=lambda w: (w.cy, w.x1)):
        if lines and abs(word.cy - sum(w.cy for w in lines[-1]) / len(lines[-1])) <= 0.6 * typical:
            lines[-1].append(word)
        else:
            lines.append([word])
    out = []
    for line in lines:
        line.sort(key=lambda w: w.x1)
        out.append((" ".join(w.text for w in line), sum(w.cy for w in line) / len(line),
                    line[0].x1, line[-1].x2))
    return out


def _title_above(lines, bbox: tuple):
    """The line just above the table and over it: its title ("3. SO2 Emission Report ...")."""
    reach = 0.12 * max(bbox[3] - bbox[1], 1) + 60
    above = [
        line for line in lines
        if line[1] < bbox[1] and bbox[1] - line[1] <= reach and line[3] >= bbox[0] and line[2] <= bbox[2]
    ]
    return max(above, key=lambda line: line[1], default=None)


def _drop_empty(rows: list[list[str]]) -> list[list[str]]:
    rows = [r for r in rows if any(r)]
    if not rows:
        return rows
    keep = [c for c in range(len(rows[0])) if any(r[c] for r in rows)]
    return [[r[c] for c in keep] for r in rows]


def _find_grids(image) -> list[_Grid]:
    """Ruled tables in a picture, by img2table (OpenCV; no OCR attached, so nothing is read yet)."""
    import cv2
    from img2table.document import Image

    ok, buffer = cv2.imencode(".png", image)
    if not ok:
        return []
    found = Image(src=buffer.tobytes(), detect_rotation=False).extract_tables(
        ocr=None, borderless_tables=False,
    )
    grids = []
    for table in found:
        cells = [
            [(c.bbox.x1, c.bbox.y1, c.bbox.x2, c.bbox.y2) for c in row]
            for row in table.content.values()
        ]
        grids.append(_Grid(
            bbox=(table.bbox.x1, table.bbox.y1, table.bbox.x2, table.bbox.y2), cells=cells,
        ))
    return grids


# ----------------------------------------------------------------------
# Month headers
# ----------------------------------------------------------------------

_MONTHS = ["january", "february", "march", "april", "may", "june", "july", "august",
           "september", "october", "november", "december"]
# Anything short that ends in a year: "April-19", but also what sideways OCR makes of it ("l--19").
_MONTH_CELL = re.compile(r"^\s*(.{0,12}?)[-–'’\s]*(\d{4}|\d{1,2})\s*$")
# What survives of a damaged month name is short; a longer word is a header that is not a month.
_MAX_DAMAGED_LETTERS = 6


def _month_index(letters: str) -> int | None:
    text = letters.lower().replace("0", "o")
    if len(text) < 2:
        return None
    best, best_ratio = None, 0.0
    for i, name in enumerate(_MONTHS):
        for candidate in (name, name[:3]):
            ratio = SequenceMatcher(None, text, candidate).ratio()
            if ratio > best_ratio:
                best, best_ratio = i, ratio
    return best if best_ratio >= 0.75 else None


def _month_label(letters: str, month: int, year: int) -> str:
    """The month as the damaged cell was probably written: short if what survived of it looks
    like the short name ("AU" -> Aug), in full otherwise ("y" -> July)."""
    full, short = _MONTHS[month], _MONTHS[month][:3]
    text = letters.lower().replace("0", "o")
    use_short = bool(text) and (
        SequenceMatcher(None, text, short).ratio() >= SequenceMatcher(None, text, full).ratio()
    )
    return f"{(short if use_short else full).capitalize()}-{year % 100:02d}"


# "pril-19 5.69": a damaged month label followed by the figure beneath it.
_FUSED_CELL = re.compile(r"^(?P<label>\S{1,10}?)\s+(?P<figure>\d+(?:[.,]\d+)?)$")


def split_fused_header(rows: list[list[str]]) -> list[list[str]]:
    """Undoes a header that has run into the first row of figures, keeping only what is safe.

    Sideways month labels drawn by a metafile converter can stand lower than their cell, over the
    row beneath. The ruled line between the two is then cut at every label, the grid finder sees
    one tall cell, and its text is the label and the figure together: "pril-19 5.69", where the
    figure was 5.9 and a letter lay across it. The labels are recoverable (the months run in
    sequence); the figures are not, and a figure that is wrong by a digit is worse than one
    missing. So the first row is used for its labels and its figures are not emitted.
    """
    if len(rows) < 3 or len(rows[0]) < 5:
        return rows
    first = rows[0]
    fused = {
        c: m for c in range(1, len(first) - 1)
        if (m := _FUSED_CELL.match(first[c] or "")) and re.search(r"[A-Za-z]", m["label"])
    }
    if len(fused) < 4 or len(fused) < 0.6 * (len(first) - 2):
        return rows
    header = [(fused[c]["label"] if c in fused else first[c]) for c in range(len(first))]
    return [header, *rows[1:]]


def repair_month_headers(row: list[str]) -> list[str]:
    """Rebuilds damaged month labels in a header row from the ones that read.

    Sideways text is what OCR reads worst, and a row of months is the commonest sideways header:
    "April-19", "June-19" and "July-19" came back as "APriI-9", "n-9" and "y-19". A run of months is
    consecutive, so once most of it has read, the rest is known. Done only when the sequence is
    plain -- most of the run agrees on where it starts -- and never across a cell that is not
    month-shaped, so a header that merely contains a few month names is left as it is.
    """
    parsed: dict[int, tuple[str, int | None, int | None]] = {}
    shaped: list[int] = []
    for c, cell in enumerate(row):
        match = _MONTH_CELL.match(cell or "")
        if not match:
            continue
        letters, digits = re.sub(r"[^A-Za-z0-9]", "", match.group(1)), match.group(2)
        if len(letters) > _MAX_DAMAGED_LETTERS:
            continue
        month = _month_index(letters)
        year = 2000 + int(digits) if len(digits) == 2 else None
        parsed[c] = (letters, month, year)
        shaped.append(c)

    known = [c for c, (_, month, _) in parsed.items() if month is not None]
    if len(known) < 4:
        return row
    first, last = min(known), max(known)
    # Every cell in the run must look like a month, or be empty: a "Total" in the middle means
    # this is not a plain run of months.
    for c in range(first, last + 1):
        if c not in parsed and (row[c] or "").strip():
            return row
    # The ends of the run can be damaged too (the first label is often the worst): take in the
    # month-shaped cells beside it.
    while first - 1 in parsed:
        first -= 1
    while last + 1 in parsed:
        last += 1

    votes: dict[int, int] = {}
    for c in known:
        _, month, year = parsed[c]
        if year is not None:
            offset = year * 12 + month - c
            votes[offset] = votes.get(offset, 0) + 1
    if not votes:
        return row
    offset, support = max(votes.items(), key=lambda kv: kv[1])
    # Enough cells read, and the ones that read agree on where the run starts. Half the labels
    # of a sideways row are routinely damaged, so "most of the run" would never be met; "the
    # readable ones agree" is the test that still refuses a quarterly or irregular header.
    if support < 4 or support < 0.75 * sum(votes.values()):
        return row

    out = list(row)
    for c in range(first, last + 1):
        absolute = offset + c
        year, month = divmod(absolute, 12)
        letters = parsed[c][0] if c in parsed else ""
        current = (parsed[c][0].lower(), parsed[c][2]) if c in parsed else None
        if current and current[0] in (_MONTHS[month], _MONTHS[month][:3]) and current[1] == year:
            continue
        out[c] = _month_label(letters, month, year)
    return out
