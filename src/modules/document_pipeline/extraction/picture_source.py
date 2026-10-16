"""Finding the pictures in a Word or PowerPoint file, and which section each one sits in.

Only *finding*: what is in a picture is `picture_tables.py`'s business. A picture is wanted with
three things -- the bytes, the unit it is on (a slide number; Word has one unit), and for Word the
heading it sits under, because a picture cited under the wrong section is worse than one not found
(#19). Headings are read with the same rule as `DocxTextExtractor`, so the text matches the
chunker's section by its exact string.
"""

import io
import logging
import os
import re
import shutil
import subprocess
import tempfile
from typing import NamedTuple

from src.modules.document_pipeline.formats import PPTX_MIME

logger = logging.getLogger(__name__)

# Rendered by Pillow and OpenCV as they are.
RASTER_EXTENSIONS = {"png", "jpg", "jpeg", "gif", "bmp", "tif", "tiff", "webp"}
# Vector pictures (what Office writes when a table is pasted from Excel). Drawn by emf2svg and
# rsvg-convert, so they are read only where those are installed.
VECTOR_EXTENSIONS = {"emf", "wmf"}


class FoundPicture(NamedTuple):
    unit_index: int
    heading: str | None
    blob: bytes
    extension: str


def find_pictures(data: bytes, mime_type: str) -> list[FoundPicture]:
    """Every picture in the file, in reading order. Raises if the file cannot be opened."""
    if mime_type == PPTX_MIME:
        return _pptx_pictures(data)
    return _docx_pictures(data)


def _pptx_pictures(data: bytes) -> list[FoundPicture]:
    from pptx import Presentation

    from src.modules.document_pipeline.extraction.extractors import _placed_shapes

    found: list[FoundPicture] = []
    for index, slide in enumerate(Presentation(io.BytesIO(data)).slides, start=1):
        # The same numbering as the text extractor, which skips hidden slides without renumbering.
        if slide._element.get("show") == "0":
            continue
        pictures = []
        for placed in _placed_shapes(slide.shapes):
            try:
                image = placed.shape.image  # a picture, or a placeholder holding one
            except Exception:
                # Most shapes are not pictures: python-pptx raises for those, and that is the answer.
                logger.debug("slide %d: %r is not a picture", index, placed.shape.name)
                continue
            left, top = placed.frame[0], placed.frame[1]
            pictures.append((top, left, image))
        # Top to bottom, then left to right: the order a reader meets them.
        for _, _, image in sorted(pictures, key=lambda p: (p[0], p[1])):
            found.append(FoundPicture(index, None, image.blob, (image.ext or "").lower()))
    return found


def _docx_pictures(data: bytes) -> list[FoundPicture]:
    from docx import Document as DocxDocument
    from docx.table import Table as DocxTable

    from src.modules.document_pipeline.extraction.extractors import DocxTextExtractor

    document = DocxDocument(io.BytesIO(data))
    related = document.part.related_parts
    found: list[FoundPicture] = []
    heading: str | None = None
    for block in document.iter_inner_content():
        if isinstance(block, DocxTable):
            element = block._tbl
        else:
            text = block.text.strip()
            if text and DocxTextExtractor._heading_level(block) is not None:
                heading = text
            element = block._p
        for rid in element.xpath(".//a:blip/@r:embed"):
            part = related.get(rid)
            if part is None:
                continue
            extension = os.path.splitext(str(part.partname))[1].lstrip(".").lower()
            found.append(FoundPicture(1, heading, part.blob, extension))
    return found


# ----------------------------------------------------------------------
# Decoding
# ----------------------------------------------------------------------

def decode_picture(blob: bytes):
    """The picture as a BGR array, or None if it cannot be decoded."""
    import cv2
    import numpy as np
    from PIL import Image

    try:
        with Image.open(io.BytesIO(blob)) as image:
            return cv2.cvtColor(np.asarray(image.convert("RGB")), cv2.COLOR_RGB2BGR)
    except Exception:
        return None


# The width a vector picture is drawn at. Its own size is a printer's (one table was 10,000 pixels
# wide), which is slow to read and is scaled back down by the OCR anyway; at this width a 14-column
# table of figures still reads cleanly.
RENDER_WIDTH_PX = 1600


def is_emf(blob: bytes) -> bool:
    """Decided by the bytes, not the name: the "WMF" pictures in the Secretary Power deck are
    Enhanced Metafiles, which only a look at the header ("EMF" at byte 40) shows."""
    return blob[40:44] == b" EMF"


def renderer_available() -> bool:
    return bool(shutil.which("emf2svg-conv") and shutil.which("rsvg-convert"))


class RenderedVector(NamedTuple):
    """A vector picture drawn twice: the picture without its sideways text, and that text alone."""

    png: bytes
    labels: bytes | None


_ROTATED = re.compile(r"rotate\(\s*-?(?:90|270)\b")
_SVG = "{http://www.w3.org/2000/svg}"


def split_rotated_labels(svg: bytes) -> tuple[bytes, bytes | None]:
    """Splits an SVG into (the drawing without its sideways text, the sideways text alone).

    emf2svg stands rotated text lower than the cell it belongs to, so a table's month labels land
    on the first row of figures and a letter lies across a digit: "5.9" reads as "5.69", and the
    ruled line under the header is cut so the grid finder sees header and first row as one cell.
    Drawn apart, neither problem exists: the table has a clean first row, and the labels, alone on
    the page, are read sideways and given to the columns they stand over (by position, which both
    drawings share). None for the second when the picture has no sideways text.
    """
    from lxml import etree

    parser = etree.XMLParser(resolve_entities=False, no_network=True, huge_tree=False)
    root = etree.fromstring(svg, parser=parser)

    def rotated(el) -> bool:
        return el.tag == f"{_SVG}text" and bool(_ROTATED.search(el.get("transform", "")))

    if not any(rotated(el) for el in root.iter()):
        return svg, None

    labels = etree.fromstring(svg, parser=parser)
    for el in [e for e in root.iter() if rotated(e)]:
        el.getparent().remove(el)

    def prune(parent) -> None:
        """Keeps definitions (clip paths the text refers to), the sideways text, and the groups
        that hold them; everything else goes."""
        for child in list(parent):
            if child.tag == f"{_SVG}defs" or rotated(child):
                continue
            if any(rotated(d) for d in child.iter()):
                prune(child)
            else:
                parent.remove(child)

    prune(labels)
    return etree.tostring(root), etree.tostring(labels)


def rasterise_vectors(items: dict[int, tuple[bytes, str]]) -> dict[int, RenderedVector]:
    """Draws EMF pictures to PNG: emf2svg turns the metafile into SVG and rsvg-convert draws it.

    Two small packages (29MB with fonts) in place of LibreOffice (about 450MB, and its PNG export
    prints the picture onto an A4 page, leaving a table too small to read). Sideways text is drawn
    apart (`split_rotated_labels`). Returns the drawings for those that rendered; empty when the
    tools are absent. A true WMF, or anything else, is not attempted.
    """
    if not items or not renderer_available():
        return {}
    done: dict[int, RenderedVector] = {}
    for key, (blob, _extension) in items.items():
        if not is_emf(blob):
            continue
        with tempfile.TemporaryDirectory(prefix="picture-") as work:

            def path(name: str, work: str = work) -> str:
                return os.path.join(work, name)

            with open(path("p.emf"), "wb") as handle:
                handle.write(blob)
            drawn: dict[str, bytes] = {}
            try:
                subprocess.run(["emf2svg-conv", "-i", path("p.emf"), "-o", path("p.svg")],
                               check=True, capture_output=True, timeout=60)
                with open(path("p.svg"), "rb") as handle:
                    data_svg, labels_svg = split_rotated_labels(handle.read())
                for name, content in (("data", data_svg), ("labels", labels_svg)):
                    if content is None:
                        continue
                    with open(path(f"{name}.svg"), "wb") as handle:
                        handle.write(content)
                    subprocess.run(["rsvg-convert", "-w", str(RENDER_WIDTH_PX), "-b", "white",
                                    path(f"{name}.svg"), "-o", path(f"{name}.png")],
                                   check=True, capture_output=True, timeout=60)
                    with open(path(f"{name}.png"), "rb") as handle:
                        drawn[name] = handle.read()
            except (subprocess.SubprocessError, OSError, ValueError) as e:
                logger.warning("Could not draw a vector picture: %s", e)
                continue
            if drawn.get("data"):
                done[key] = RenderedVector(drawn["data"], drawn.get("labels"))
    return done
