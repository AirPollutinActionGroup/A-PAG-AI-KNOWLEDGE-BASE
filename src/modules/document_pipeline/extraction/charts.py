"""Charts and diagrams that an Office file states exactly.

A chart saved as a *picture* is pixels, and reading its bars is guesswork. A chart drawn *in* the
file is not: its categories, its series and every value are stored in the chart part, so they can
be read exactly, with no model and no OCR. The same holds for the arrows of a flow diagram built
from shapes and connectors (each connector records the two shapes it joins), and for the text of a
SmartArt graphic, which lives in a data model and not in the slide's shapes at all -- so it was
invisible to the text extractor, with nothing to say a diagram had been skipped.

Nothing here is inferred. What the file does not state is not guessed: a connector with a free
end, or a chart with no stored values, produces nothing.
"""

import datetime
import logging
import posixpath
import re
from collections.abc import Callable

from lxml import etree

logger = logging.getLogger(__name__)

_C = "http://schemas.openxmlformats.org/drawingml/2006/chart"
_A = "http://schemas.openxmlformats.org/drawingml/2006/main"
_DGM = "http://schemas.openxmlformats.org/drawingml/2006/diagram"
_R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_P = "http://schemas.openxmlformats.org/presentationml/2006/main"
_NS = {"c": _C, "a": _A}
_PARSER = etree.XMLParser(resolve_entities=False, no_network=True, huge_tree=False)

# Said on the first line of every chart, where the chunker keeps it as the table's caption.
CHART_NOTICE = "values read from the chart's own data"

_KINDS = {
    "barChart": "bar", "bar3DChart": "bar", "lineChart": "line", "line3DChart": "line",
    "pieChart": "pie", "pie3DChart": "pie", "ofPieChart": "pie", "doughnutChart": "doughnut",
    "areaChart": "area", "area3DChart": "area", "scatterChart": "scatter",
    "bubbleChart": "bubble", "radarChart": "radar", "stockChart": "stock",
    "surfaceChart": "surface", "surface3DChart": "surface",
}


def _local(el) -> str:
    return etree.QName(el).localname


def _text(el) -> str:
    """All the text of a title or label: its runs, joined."""
    if el is None:
        return ""
    return " ".join("".join(t.itertext()).strip() for t in el.iter(f"{{{_A}}}t")).strip()


def _is_date_format(code: str) -> bool:
    """A number format that shows a date: it has a year, or a day with a month."""
    lowered = re.sub(r'"[^"]*"|\[[^\]]*\]', "", (code or "").lower())
    return "%" not in lowered and bool(re.search(r"yy|d.*m|m.*d", lowered))


def _number(value: str, percent: bool = False, date: bool = False) -> str:
    try:
        x = float(value)
    except (TypeError, ValueError):
        return (value or "").strip()
    if date:
        try:
            return (datetime.datetime(1899, 12, 30, tzinfo=datetime.UTC) + datetime.timedelta(days=x)).date().isoformat()
        except (OverflowError, ValueError):
            pass
    if percent:
        return f"{x * 100:.4g}%"
    if x.is_integer() and abs(x) < 1e15:
        return str(int(x))
    return f"{x:.10g}"


def _cache(el, ns=_NS) -> tuple[list[str], str]:
    """The stored points of a category or value reference, in index order, and its format code.

    Points are keyed by index because a chart omits empty ones; the count says how many there
    were, so a gap stays a gap instead of shifting the values after it.
    """
    if el is None:
        return [], ""
    for tag in ("numCache", "strCache", "numLit", "strLit"):
        found = el.find(f".//c:{tag}", ns)
        if found is None:
            continue
        count = found.find("c:ptCount", ns)
        points = {}
        for pt in found.findall("c:pt", ns):
            v = pt.find("c:v", ns)
            points[int(pt.get("idx", "0"))] = (v.text or "") if v is not None else ""
        size = int(count.get("val", "0")) if count is not None else 0
        size = max(size, (max(points) + 1) if points else 0)
        code = found.find("c:formatCode", ns)
        return [points.get(i, "") for i in range(size)], (code.text or "") if code is not None else ""
    return [], ""


def _reference(el) -> str | None:
    if el is None:
        return None
    f = el.find(".//c:f", _NS)
    return f.text if f is not None and f.text else None


def chart_rows(xml: bytes, resolve: Callable[[str], list[str] | None] | None = None) -> list[list[str]] | None:
    """A chart as a table: a caption row, a header of series names, then one row per category.

    `resolve` turns a worksheet reference ("Sheet1!$B$2:$B$6") into its cell values, for a
    spreadsheet chart that stores references and no copy of the values. None when the chart
    stores no values at all.
    """
    try:
        root = etree.fromstring(xml, parser=_PARSER)
    except etree.XMLSyntaxError:
        return None
    plot = root.find("c:chart/c:plotArea", _NS)
    if plot is None:
        return None
    title = _text(root.find("c:chart/c:title", _NS))

    kinds: list[str] = []
    series: list[dict] = []
    for el in plot:
        tag = _local(el)
        if not tag.endswith("Chart"):
            continue
        kind = _KINDS.get(tag, tag[:-5].lower())
        if kind == "bar":
            direction = el.find("c:barDir", _NS)
            kind = "bar" if direction is not None and direction.get("val") == "bar" else "column"
        if kind not in kinds:
            kinds.append(kind)
        for ser in el.findall("c:ser", _NS):
            tx = ser.find("c:tx", _NS)
            name = _text(tx)
            if not name and tx is not None:
                cached, _ = _cache(tx)
                if cached:
                    name = cached[0]
                elif resolve is not None and (ref := _reference(tx)):
                    name = " ".join(v for v in (resolve(ref) or []) if v)
            entry = {"name": name.strip(), "kind": kind}
            for key, tag_name in (("cat", "cat"), ("val", "val"), ("x", "xVal"), ("y", "yVal")):
                node = ser.find(f"c:{tag_name}", _NS)
                values, code = _cache(node)
                if not values and resolve is not None:
                    ref = _reference(node)
                    values = (resolve(ref) if ref else None) or []
                entry[key] = values
                entry[key + "_pct"] = "%" in code
                entry[key + "_date"] = _is_date_format(code)
            series.append(entry)

    if not any(s["val"] or s["y"] for s in series):
        return None

    def axis_title(kind: str, index: int = 0) -> str:
        axes = plot.findall(f"c:{kind}", _NS)
        return _text(axes[index].find("c:title", _NS)) if len(axes) > index else ""

    caption = f"Chart: {title}" if title else "Chart"
    caption += f" ({', '.join(kinds)}; {CHART_NOTICE})"
    rows: list[list[str]] = []

    if any(s["x"] for s in series):
        x_title = axis_title("valAx", 0) or "X"
        y_title = axis_title("valAx", 1) or "Y"
        rows.append(["Series", x_title, y_title])
        for s in series:
            for x, y in zip(s["x"], s["y"], strict=False):
                rows.append([s["name"], _number(x, s["x_pct"], s["x_date"]), _number(y, s["y_pct"])])
    else:
        lead = next((s for s in series if s["cat"]), None)
        first = ([_number(v, False, lead["cat_date"]) for v in lead["cat"]] if lead else [])
        length = max([len(first)] + [len(s["val"]) for s in series])
        names = [s["name"] or f"Series {i}" for i, s in enumerate(series, 1)]
        rows.append([axis_title("catAx") or axis_title("dateAx") or "", *names])
        for i in range(length):
            label = first[i] if i < len(first) else str(i + 1)
            rows.append([label, *[
                _number(s["val"][i], s["val_pct"]) if i < len(s["val"]) else "" for s in series
            ]])
    width = max(len(r) for r in rows)
    return [[caption] + [""] * (width - 1), *[r + [""] * (width - len(r)) for r in rows]]


# ----------------------------------------------------------------------
# Where charts and diagrams are found
# ----------------------------------------------------------------------

def chart_rids(element) -> list[str]:
    """Relationship ids of the charts drawn inside an element (a Word paragraph or table)."""
    return [el.get(f"{{{_R}}}id") for el in element.iter(f"{{{_C}}}chart") if el.get(f"{{{_R}}}id")]


def smartart_rids(element) -> list[str]:
    """Relationship ids of the SmartArt data models drawn inside an element."""
    return [el.get(f"{{{_R}}}dm") for el in element.iter(f"{{{_DGM}}}relIds") if el.get(f"{{{_R}}}dm")]


def smartart_lines(xml: bytes) -> list[str]:
    """The text of a SmartArt graphic: its nodes in order, and who each one contains.

    SmartArt keeps its text in a data model of points and connections rather than in shapes, so
    reading a slide's or page's shapes never meets it. A node's children are listed under it, and
    the top-level nodes are given as a sequence, which is what a process or cycle graphic is.
    """
    try:
        root = etree.fromstring(xml, parser=_PARSER)
    except etree.XMLSyntaxError:
        return []
    ns = {"dgm": _DGM, "a": _A}
    text: dict[str, str] = {}
    doc_id = None
    for pt in root.findall("dgm:ptLst/dgm:pt", ns):
        kind = pt.get("type", "node")
        if kind == "doc":
            doc_id = pt.get("modelId")
        elif kind in ("node", "asst"):
            label = _text(pt.find("dgm:t", ns))
            if label:
                text[pt.get("modelId")] = label
    children: dict[str, list[tuple[int, str]]] = {}
    for cxn in root.findall("dgm:cxnLst/dgm:cxn", ns):
        if cxn.get("type", "parOf") != "parOf":
            continue
        src, dest = cxn.get("srcId"), cxn.get("destId")
        if dest in text:
            children.setdefault(src, []).append((int(cxn.get("srcOrd", "0")), dest))

    def kids(node: str) -> list[str]:
        return [text[d] for _, d in sorted(children.get(node, []))]

    lines = []
    top = kids(doc_id) if doc_id else list(text.values())
    if len(top) > 1:
        lines.append(" → ".join(top))
    elif top:
        lines.append(top[0])
    for node, label in text.items():
        below = kids(node)
        if below:
            lines.append(f"{label}: {'; '.join(below)}")
    return lines


def slide_connections(slide_element) -> list[str]:
    """The arrows of a flow diagram drawn with shapes and connectors, as "A -> B" lines.

    A connector records the two shapes it joins; the arrowheads say which way it points. With the
    arrow at its end it reads start -> end, at its start end -> start, at both ends both ways, and
    with none it is a plain line between the two. A connector with a free end joins nothing and
    says nothing: an arrow's position on a slide is not matched to a shape it only passes near.
    """
    shape_text: dict[str, str] = {}
    for sp in slide_element.iter(f"{{{_P}}}sp"):
        c_nv = sp.find(f"{{{_P}}}nvSpPr/{{{_P}}}cNvPr")
        if c_nv is None:
            continue
        paragraphs = [
            "".join(t.text or "" for t in p.iter(f"{{{_A}}}t")).strip()
            for p in sp.iter(f"{{{_A}}}p")
        ]
        label = " ".join(p for p in paragraphs if p).strip()
        if label:
            shape_text[c_nv.get("id")] = label

    def brief(label: str) -> str:
        return label if len(label) <= 160 else label[:157].rstrip() + "..."

    seen: set[str] = set()
    lines: list[str] = []
    for cxn in slide_element.iter(f"{{{_P}}}cxnSp"):
        start = cxn.find(f".//{{{_A}}}stCxn")
        end = cxn.find(f".//{{{_A}}}endCxn")
        if start is None or end is None:
            continue
        a, b = start.get("id"), end.get("id")
        if a == b or a not in shape_text or b not in shape_text:
            continue

        def arrow(tag: str, cxn=cxn) -> bool:
            node = cxn.find(f".//{{{_A}}}ln/{{{_A}}}{tag}")
            return node is not None and node.get("type", "none") != "none"

        head, tail = arrow("headEnd"), arrow("tailEnd")
        left, right = brief(shape_text[a]), brief(shape_text[b])
        if head and tail:
            line = f"{left} ↔ {right}"
        elif head:
            line = f"{right} → {left}"
        elif tail:
            line = f"{left} → {right}"
        else:
            line = f"{left} — {right}"
        if line not in seen:
            seen.add(line)
            lines.append(line)
    return lines


# ----------------------------------------------------------------------
# Spreadsheet charts
# ----------------------------------------------------------------------

def xlsx_chart_parts(zf) -> dict[str, list[bytes]]:
    """{sheet name: chart XML parts} for a workbook, by following the file's own relationships:
    a sheet points at a drawing, and a drawing at its charts."""
    def rels(part: str) -> dict[str, str]:
        folder, name = posixpath.split(part)
        path = posixpath.join(folder, "_rels", name + ".rels")
        if path not in zf.namelist():
            return {}
        root = etree.fromstring(zf.read(path), parser=_PARSER)
        out = {}
        for rel in root:
            target = rel.get("Target", "")
            out[rel.get("Id")] = (target.lstrip("/") if target.startswith("/")
                                  else posixpath.normpath(posixpath.join(folder, target)))
        return out

    workbook = "xl/workbook.xml"
    if workbook not in zf.namelist():
        return {}
    wb = etree.fromstring(zf.read(workbook), parser=_PARSER)
    main = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
    wb_rels = rels(workbook)
    found: dict[str, list[bytes]] = {}
    for sheet in wb.iter(f"{{{main}}}sheet"):
        path = wb_rels.get(sheet.get(f"{{{_R}}}id"))
        if not path or path not in zf.namelist():
            continue
        for drawing in (t for t in rels(path).values() if "/drawings/" in t or t.startswith("xl/drawings/")):
            if drawing not in zf.namelist() or drawing.endswith(".rels"):
                continue
            for chart in rels(drawing).values():
                is_chart = "/charts/" in chart or chart.startswith("xl/charts/")
                if is_chart and chart in zf.namelist():
                    found.setdefault(sheet.get("name"), []).append(zf.read(chart))
    return found


_REF = re.compile(r"^'?(?P<sheet>[^'!]+)'?!\$?(?P<c1>[A-Z]+)\$?(?P<r1>\d+)(?::\$?(?P<c2>[A-Z]+)\$?(?P<r2>\d+))?$")


def workbook_resolver(workbook) -> Callable[[str], list[str] | None]:
    """Turns "Sheet1!$B$2:$B$6" into those cells' values, for a chart that stores references."""
    from openpyxl.utils import column_index_from_string

    def resolve(ref: str) -> list[str] | None:
        m = _REF.match(ref.replace("(", "").replace(")", "").strip())
        if not m or m["sheet"] not in workbook.sheetnames:
            return None
        sheet = workbook[m["sheet"]]
        c1, r1 = column_index_from_string(m["c1"]), int(m["r1"])
        c2 = column_index_from_string(m["c2"]) if m["c2"] else c1
        r2 = int(m["r2"]) if m["r2"] else r1
        values = []
        for row in sheet.iter_rows(min_row=r1, max_row=r2, min_col=c1, max_col=c2):
            for cell in row:
                value = cell.value
                if isinstance(value, datetime.datetime):
                    value = value.date().isoformat() if value.time() == datetime.time(0) else value.isoformat(" ")
                elif isinstance(value, datetime.date):
                    value = value.isoformat()
                values.append("" if value is None else str(value))
        return values

    return resolve
