"""Charts and diagrams that a file states exactly: chart values, connector arrows, SmartArt text.

None of it is read from pixels. A chart's categories and values are stored in the chart part; a
connector records the two shapes it joins; SmartArt keeps its text in a data model. What a file
does not state is not guessed, and each test below that says so is a refusal to invent.
"""

import io
import uuid

from docx import Document as DocxDocument
from docx.opc.constants import RELATIONSHIP_TYPE as RT
from docx.opc.packuri import PackURI
from docx.opc.part import Part
from lxml import etree
from openpyxl import Workbook
from openpyxl.chart import BarChart, Reference
from pptx import Presentation
from pptx.chart.data import CategoryChartData
from pptx.enum.chart import XL_CHART_TYPE
from pptx.enum.shapes import MSO_CONNECTOR, MSO_SHAPE
from pptx.opc.package import Part as PptxPart
from pptx.opc.packuri import PackURI as PptxPackURI
from pptx.oxml.ns import qn
from pptx.util import Inches

from src.modules.document_pipeline.extraction.charts import (
    CHART_NOTICE,
    chart_rows,
    slide_connections,
    smartart_lines,
)
from src.modules.document_pipeline.extraction.extractors import (
    DocxTextExtractor,
    PptxTextExtractor,
    XlsxTextExtractor,
)
from src.modules.document_pipeline.extraction.models import ExtractionResult
from src.modules.document_pipeline.formats import PPTX_MIME
from src.modules.document_pipeline.normalization.service import NormalizationService

C = "http://schemas.openxmlformats.org/drawingml/2006/chart"
A = "http://schemas.openxmlformats.org/drawingml/2006/main"


def _chart(plot: str, title: str = "", axes: str = "") -> bytes:
    heading = (f'<c:title><c:tx><c:rich><a:bodyPr/><a:p><a:r><a:t>{title}</a:t></a:r></a:p></c:rich></c:tx></c:title>'
               if title else "")
    return (f'<c:chartSpace xmlns:c="{C}" xmlns:a="{A}"><c:chart>{heading}'
            f'<c:plotArea>{plot}{axes}</c:plotArea></c:chart></c:chartSpace>').encode()


def _cache(kind: str, values: list, code: str = "General") -> str:
    points = "".join(f'<c:pt idx="{i}"><c:v>{v}</c:v></c:pt>' for i, v in enumerate(values) if v is not None)
    return f'<c:{kind}><c:formatCode>{code}</c:formatCode><c:ptCount val="{len(values)}"/>{points}</c:{kind}>'


def _series(name: str | None, cats: list, vals: list, cat_code="General", val_code="General") -> str:
    tx = f'<c:tx><c:strRef><c:strCache><c:ptCount val="1"/><c:pt idx="0"><c:v>{name}</c:v></c:pt></c:strCache></c:strRef></c:tx>' if name else ""
    return (f'<c:ser>{tx}<c:cat><c:strRef>{_cache("strCache", cats)}</c:strRef></c:cat>'
            f'<c:val><c:numRef>{_cache("numCache", vals, val_code)}</c:numRef></c:val></c:ser>')


def test_a_charts_values_are_read_exactly_with_its_series_and_categories():
    plot = ('<c:barChart><c:barDir val="col"/>' + _series("India", ["2021", "2022", "2023"], [10.5, 12, 9.25])
            + _series("China", ["2021", "2022", "2023"], [40, 38.5, 41]) + "</c:barChart>")
    rows = chart_rows(_chart(plot, "SO2 emissions (Kt)"))
    assert rows[0][0] == f"Chart: SO2 emissions (Kt) (column; {CHART_NOTICE})"
    assert rows[1] == ["", "India", "China"]
    assert rows[2:] == [["2021", "10.5", "40"], ["2022", "12", "38.5"], ["2023", "9.25", "41"]]


def test_a_gap_in_a_series_stays_a_gap():
    """A chart omits empty points. Counting from the first one stored would shift every value
    after the gap onto the wrong category."""
    plot = "<c:lineChart>" + _series("Plant A", ["Jan", "Feb", "Mar", "Apr"], [5, None, 7, 8]) + "</c:lineChart>"
    rows = chart_rows(_chart(plot))
    assert rows[2:] == [["Jan", "5"], ["Feb", ""], ["Mar", "7"], ["Apr", "8"]]


def test_percent_formatted_values_read_as_the_chart_shows_them():
    plot = "<c:pieChart>" + _series("Share", ["Coal", "Hydro"], [0.4565, 0.1019], val_code="0.0%") + "</c:pieChart>"
    rows = chart_rows(_chart(plot))
    assert rows[2:] == [["Coal", "45.65%"], ["Hydro", "10.19%"]]


def test_date_categories_are_dates_not_serial_numbers():
    series = (f'<c:ser><c:cat><c:numRef>{_cache("numCache", [44105, 44106], "m/d/yyyy")}</c:numRef></c:cat>'
              f'<c:val><c:numRef>{_cache("numCache", [131, 145])}</c:numRef></c:val></c:ser>')
    rows = chart_rows(_chart(f"<c:lineChart>{series}</c:lineChart>"))
    assert [r[0] for r in rows[2:]] == ["2020-10-01", "2020-10-02"]


def test_a_series_with_no_stored_name_is_not_given_one():
    plot = "<c:barChart>" + _series(None, ["a", "b"], [1, 2]) + "</c:barChart>"
    assert chart_rows(_chart(plot))[1] == ["", "Series 1"]


def test_a_scatter_chart_gives_its_points():
    ser = (f'<c:ser><c:tx><c:strRef><c:strCache><c:ptCount val="1"/><c:pt idx="0"><c:v>Plant</c:v></c:pt></c:strCache></c:strRef></c:tx>'
           f'<c:xVal><c:numRef>{_cache("numCache", [100, 200])}</c:numRef></c:xVal>'
           f'<c:yVal><c:numRef>{_cache("numCache", [1.5, 2.5])}</c:numRef></c:yVal></c:ser>')
    rows = chart_rows(_chart(f"<c:scatterChart>{ser}</c:scatterChart>"))
    assert rows[1:] == [["Series", "X", "Y"], ["Plant", "100", "1.5"], ["Plant", "200", "2.5"]]


def test_a_chart_with_no_stored_values_says_nothing():
    plot = '<c:barChart><c:ser><c:val><c:numRef><c:f>Sheet1!$B$2:$B$4</c:f></c:numRef></c:val></c:ser></c:barChart>'
    assert chart_rows(_chart(plot)) is None
    assert chart_rows(b"<not-xml") is None


def test_references_are_resolved_from_the_workbook_when_nothing_is_cached():
    plot = ('<c:barChart><c:ser><c:tx><c:strRef><c:f>Sheet1!$B$1</c:f></c:strRef></c:tx>'
            '<c:cat><c:strRef><c:f>Sheet1!$A$2:$A$3</c:f></c:strRef></c:cat>'
            '<c:val><c:numRef><c:f>Sheet1!$B$2:$B$3</c:f></c:numRef></c:val></c:ser></c:barChart>')
    cells = {"Sheet1!$B$1": ["Units"], "Sheet1!$A$2:$A$3": ["North", "South"], "Sheet1!$B$2:$B$3": ["7", "9"]}
    rows = chart_rows(_chart(plot), resolve=cells.get)
    assert rows[1:] == [["", "Units"], ["North", "7"], ["South", "9"]]


# ==============================================================================
# Through the real extractors
# ==============================================================================

def _deck(build) -> bytes:
    prs = Presentation()
    build(prs)
    buf = io.BytesIO()
    prs.save(buf)
    return buf.getvalue()


def _titled(prs, title):
    slide = prs.slides.add_slide(prs.slide_layouts[5])
    slide.shapes.title.text = title
    return slide


def test_a_chart_drawn_in_a_slide_becomes_a_table_under_the_slides_title():
    def build(prs):
        slide = _titled(prs, "Avg SO2 emissions by category")
        data = CategoryChartData()
        data.categories = ["Category 1a", "Category 1b"]
        data.add_series("Average of max SOx", (1112, 1209))
        slide.shapes.add_chart(XL_CHART_TYPE.COLUMN_CLUSTERED, Inches(1), Inches(2), Inches(6), Inches(3), data)

    content = PptxTextExtractor().extract(_deck(build))
    [table] = content.tables
    assert table.heading == "Avg SO2 emissions by category"
    assert table.rows[1:] == [["", "Average of max SOx"], ["Category 1a", "1112"], ["Category 1b", "1209"]]

    result = ExtractionResult(document_id=uuid.uuid4(), mime_type=PPTX_MIME, units=content.units,
                              headings=content.headings, tables=content.tables)
    from src.modules.document_pipeline.chunking.chunker import Chunker
    [chunk] = [c for c in Chunker().chunk(NormalizationService().normalize(result)) if c.is_table]
    assert chunk.text.splitlines()[0].startswith("Chart (column;") and "Category 1b | 1209" in chunk.text


def _flow(prs, arrows: str):
    slide = _titled(prs, "Compliance process")
    a = slide.shapes.add_shape(MSO_SHAPE.RECTANGLE, Inches(1), Inches(2), Inches(2), Inches(1))
    b = slide.shapes.add_shape(MSO_SHAPE.RECTANGLE, Inches(5), Inches(2), Inches(2), Inches(1))
    a.text_frame.text, b.text_frame.text = "Non-compliance noticed", "Environmental compensation"
    line = slide.shapes.add_connector(MSO_CONNECTOR.STRAIGHT, Inches(3), Inches(2.5), Inches(5), Inches(2.5))
    line.begin_connect(a, 3)
    line.end_connect(b, 1)
    ln = line.line._get_or_add_ln()
    for end in arrows:
        etree.SubElement(ln, qn(f"a:{end}"), type="triangle")
    return slide


def test_a_connector_between_two_shapes_is_an_arrow_between_their_texts():
    deck = _deck(lambda prs: _flow(prs, ["tailEnd"]))
    [unit] = PptxTextExtractor().extract(deck).units
    assert "Diagram connections:\nNon-compliance noticed → Environmental compensation" in unit.text


def test_the_arrowhead_decides_the_direction():
    only_head = PptxTextExtractor().extract(_deck(lambda prs: _flow(prs, ["headEnd"]))).units[0].text
    assert "Environmental compensation → Non-compliance noticed" in only_head
    both = PptxTextExtractor().extract(_deck(lambda prs: _flow(prs, ["headEnd", "tailEnd"]))).units[0].text
    assert "Non-compliance noticed ↔ Environmental compensation" in both


def test_a_connector_with_no_arrowhead_is_a_plain_link_not_a_guessed_direction():
    text = PptxTextExtractor().extract(_deck(lambda prs: _flow(prs, []))).units[0].text
    assert "Non-compliance noticed — Environmental compensation" in text
    assert "→" not in text


def test_a_connector_with_a_free_end_says_nothing():
    def build(prs):
        slide = _titled(prs, "Timeline")
        a = slide.shapes.add_shape(MSO_SHAPE.RECTANGLE, Inches(1), Inches(2), Inches(2), Inches(1))
        a.text_frame.text = "2015 norms"
        line = slide.shapes.add_connector(MSO_CONNECTOR.STRAIGHT, Inches(3), Inches(2.5), Inches(6), Inches(2.5))
        line.begin_connect(a, 3)  # the other end is loose

    [unit] = PptxTextExtractor().extract(_deck(build)).units
    assert "Diagram connections" not in unit.text


def test_slide_connections_ignores_a_connector_to_a_shape_with_no_text():
    prs = Presentation()
    slide = _titled(prs, "x")
    a = slide.shapes.add_shape(MSO_SHAPE.RECTANGLE, Inches(1), Inches(2), Inches(1), Inches(1))
    b = slide.shapes.add_shape(MSO_SHAPE.RECTANGLE, Inches(4), Inches(2), Inches(1), Inches(1))
    a.text_frame.text = "Only one has words"
    line = slide.shapes.add_connector(MSO_CONNECTOR.STRAIGHT, 0, 0, Inches(1), Inches(1))
    line.begin_connect(a, 3)
    line.end_connect(b, 1)
    assert slide_connections(slide._element) == []


DGM = "http://schemas.openxmlformats.org/drawingml/2006/diagram"


def _smartart(nodes: list[tuple[str, str]], links: list[tuple[str, str, int]]) -> bytes:
    """A data model: nodes as (id, text), and parent->child links with the child's order."""
    pts = '<dgm:pt modelId="doc" type="doc"/>' + "".join(
        f'<dgm:pt modelId="{i}"><dgm:t><a:bodyPr/><a:p><a:r><a:t>{t}</a:t></a:r></a:p></dgm:t></dgm:pt>' for i, t in nodes
    )
    cxns = "".join(f'<dgm:cxn modelId="c{n}" srcId="{s}" destId="{d}" srcOrd="{o}"/>' for n, (s, d, o) in enumerate(links))
    return (f'<dgm:dataModel xmlns:dgm="{DGM}" xmlns:a="{A}"><dgm:ptLst>{pts}</dgm:ptLst>'
            f'<dgm:cxnLst>{cxns}</dgm:cxnLst></dgm:dataModel>').encode()


def test_smartart_text_is_read_in_order_with_who_contains_whom():
    xml = _smartart(
        [("n1", "Collect data"), ("n2", "Validate"), ("n3", "Publish"), ("n4", "Check units")],
        [("doc", "n3", 2), ("doc", "n1", 0), ("doc", "n2", 1), ("n2", "n4", 0)],
    )
    assert smartart_lines(xml) == [
        "Collect data → Validate → Publish",
        "Validate: Check units",
    ]


def test_smartart_in_a_slide_was_invisible_and_is_now_read():
    def build(prs):
        slide = _titled(prs, "Process")
        part = PptxPart(PptxPackURI("/ppt/diagrams/data1.xml"),
                        "application/vnd.openxmlformats-officedocument.drawingml.diagramData+xml",
                        slide.part.package, _smartart([("a", "Plan"), ("b", "Do")], [("doc", "a", 0), ("doc", "b", 1)]))
        rid = slide.part.relate_to(part, "http://schemas.openxmlformats.org/officeDocument/2006/relationships/diagramData")
        frame = etree.fromstring(
            '<p:graphicFrame xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main" '
            f'xmlns:a="{A}" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
            '<p:nvGraphicFramePr><p:cNvPr id="9" name="SmartArt"/><p:cNvGraphicFramePr/><p:nvPr/></p:nvGraphicFramePr>'
            '<p:xfrm><a:off x="0" y="0"/><a:ext cx="100" cy="100"/></p:xfrm>'
            f'<a:graphic><a:graphicData uri="{DGM}"><dgm:relIds xmlns:dgm="{DGM}" r:dm="{rid}" r:lo="{rid}" r:qs="{rid}" r:cs="{rid}"/>'
            '</a:graphicData></a:graphic></p:graphicFrame>')
        slide.shapes._spTree.append(frame)

    [unit] = PptxTextExtractor().extract(_deck(build)).units
    assert "Plan → Do" in unit.text


def test_a_spreadsheet_chart_that_stores_only_references_is_read_from_the_cells():
    """openpyxl, like Google Sheets, writes the cell references of a chart and no copy of the
    values; reading only the chart part would find nothing."""
    wb = Workbook()
    ws = wb.active
    ws.title = "Data"
    ws.append(["Year", "China", "India"])
    ws.append([2000, 20832, 4464])
    ws.append([2009, 33008, 7506])
    chart = BarChart()
    chart.title = "SO2 by country"
    chart.add_data(Reference(ws, min_col=2, max_col=3, min_row=1, max_row=3), titles_from_data=True)
    chart.set_categories(Reference(ws, min_col=1, min_row=2, max_row=3))
    ws.add_chart(chart, "E2")
    buf = io.BytesIO()
    wb.save(buf)

    content = XlsxTextExtractor().extract(buf.getvalue())
    charts = [t for t in content.tables if t.rows[0][0].startswith("Chart")]
    assert len(charts) == 1 and charts[0].heading == "Data"
    assert charts[0].rows[1:] == [["", "China", "India"], ["2000", "20832", "4464"], ["2009", "33008", "7506"]]
    # The sheet's own table is still there, unchanged.
    assert any(t.rows[0] == ["Year", "China", "India"] for t in content.tables)


def test_a_chart_in_a_word_file_is_filed_under_the_heading_it_sits_beneath():
    d = DocxDocument()
    d.add_heading("Background", level=1)
    d.add_heading("Emissions", level=1)
    plot = "<c:barChart>" + _series("India", ["2021", "2022"], [10, 12]) + "</c:barChart>"
    part = Part(PackURI("/word/charts/chart1.xml"), "application/vnd.openxmlformats-officedocument.drawingml.chart+xml",
                _chart(plot, "Trend"), d.part.package)
    rid = d.part.relate_to(part, RT.CHART)
    paragraph = d.add_paragraph()
    paragraph._p.append(etree.fromstring(
        '<w:r xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" '
        'xmlns:wp="http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing" '
        f'xmlns:a="{A}" xmlns:c="{C}" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        '<w:drawing><wp:inline><wp:extent cx="100" cy="100"/><wp:docPr id="1" name="c"/>'
        f'<a:graphic><a:graphicData uri="{C}"><c:chart r:id="{rid}"/></a:graphicData></a:graphic></wp:inline></w:drawing></w:r>'))
    d.add_heading("Results", level=1)
    buf = io.BytesIO()
    d.save(buf)

    [table] = [t for t in DocxTextExtractor().extract(buf.getvalue()).tables]
    assert table.heading == "Emissions"
    assert table.rows[2:] == [["2021", "10"], ["2022", "12"]]


def test_a_heading_with_only_its_own_table_is_not_also_a_passage_by_itself():
    """"Zorbia monthly load Mar 341" found the slide's title instead of the 341: a title-only
    passage outranks the table for any question that repeats the title."""
    def build(prs):
        slide = _titled(prs, "Zorbia monthly load")
        data = CategoryChartData()
        data.categories = ["Jan", "Mar"]
        data.add_series("Load GWh", (318, 341))
        slide.shapes.add_chart(XL_CHART_TYPE.COLUMN_CLUSTERED, Inches(1), Inches(2), Inches(6), Inches(3), data)

    content = PptxTextExtractor().extract(_deck(build))
    result = ExtractionResult(document_id=uuid.uuid4(), mime_type=PPTX_MIME, units=content.units,
                              headings=content.headings, tables=content.tables)
    from src.modules.document_pipeline.chunking.chunker import Chunker
    chunks = Chunker().chunk(NormalizationService().normalize(result))
    assert [c.is_table for c in chunks] == [True]
    assert chunks[0].section_heading == "Zorbia monthly load"


def test_a_title_slide_with_nothing_attached_is_still_a_passage():
    def build(prs):
        _titled(prs, "TPP details and emission profile")

    content = PptxTextExtractor().extract(_deck(build))
    result = ExtractionResult(document_id=uuid.uuid4(), mime_type=PPTX_MIME, units=content.units,
                              headings=content.headings, tables=content.tables)
    from src.modules.document_pipeline.chunking.chunker import Chunker
    [chunk] = Chunker().chunk(NormalizationService().normalize(result))
    assert chunk.text == "TPP details and emission profile"
