"""Tests for the universal tool layer (documents, system, UI, and the
capability-dispatched communication/calendar/media tools). All external
interaction is mocked or avoided."""

import sys

import pytest

from tools.document_tool import (
    extract_text,
    read_docx,
    read_pdf,
    read_pptx,
    read_xlsx,
    summarize_document,
)
from tools.system_tool import (
    get_system_info,
    get_clipboard,
    set_clipboard,
    set_volume,
    launch_process,
    terminate_process,
)

pytestmark = pytest.mark.skipif(
    sys.platform != "win32", reason="Jarvis universal tools target Windows"
)


# --------------------------------------------------------------------- #
# Document tools
# --------------------------------------------------------------------- #


def test_extract_text_plain_file(tmp_path):
    f = tmp_path / "notes.txt"
    f.write_text("hello world\nsecond line", encoding="utf-8")
    result = extract_text(str(f))
    assert result["success"] is True
    assert "hello world" in result["text"]


def test_extract_text_missing_file(tmp_path):
    result = extract_text(str(tmp_path / "nope.pdf"))
    assert result["success"] is False
    assert "does not exist" in result["error"]


def test_read_docx_with_python_docx(tmp_path):
    docx = pytest.importorskip("docx")
    f = tmp_path / "t.docx"
    doc = docx.Document()
    doc.add_paragraph("Assignment one")
    doc.add_paragraph("Complete questions 1-10")
    doc.save(str(f))
    result = read_docx(str(f))
    assert result["success"] is True
    assert "Assignment one" in result["text"]
    assert "Complete questions 1-10" in result["text"]


def test_read_xlsx_with_stdlib(tmp_path):
    # Build a minimal xlsx by hand: shared strings + one sheet.
    import xml.etree.ElementTree as ET
    import zipfile

    f = tmp_path / "t.xlsx"

    def w(name, data):
        f.write_bytes(b"")
        with zipfile.ZipFile(f, "w") as zf:
            for path, content in data.items():
                zf.writestr(path, content)
        return f

    ns_s = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
    shared = ET.Element(f"{ns_s}sst", attrib={"count": "2", "uniqueCount": "2"})
    for val in ("Hello", "Cell"):
        si = ET.SubElement(shared, f"{ns_s}si")
        ET.SubElement(si, f"{ns_s}t").text = val
    shared_xml = ET.tostring(shared, encoding="unicode")

    sheet = ET.Element(f"{ns_s}worksheet")
    rows = ET.SubElement(sheet, f"{ns_s}sheetData")
    row1 = ET.SubElement(rows, f"{ns_s}row")
    for ref, t, v in (("A1", "s", "0"), ("B1", "s", "1")):
        c = ET.SubElement(row1, f"{ns_s}c", attrib={"r": ref, "t": t})
        ET.SubElement(c, f"{ns_s}v").text = v
    sheet_xml = ET.tostring(sheet, encoding="unicode")

    w(f, {
        "[Content_Types].xml": (
            '<?xml version="1.0"?><Types xmlns='
            '"http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="xml" ContentType="application/xml"/>'
            '<Override PartName="/xl/workbook.xml" ContentType='
            '"application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
            '<Override PartName="/xl/worksheets/sheet1.xml" ContentType='
            '"application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
            '<Override PartName="/xl/sharedStrings.xml" ContentType='
            '"application/vnd.openxmlformats-officedocument.spreadsheetml.sharedStrings+xml"/>'
            "</Types>"
        ),
        "xl/workbook.xml": (
            '<?xml version="1.0"?><workbook xmlns='
            '"http://schemas.openxmlformats.org/spreadsheetml/2006/main"/>'
        ),
        "xl/worksheets/sheet1.xml": sheet_xml,
        "xl/sharedStrings.xml": shared_xml,
    })
    result = read_xlsx(str(f))
    assert result["success"] is True
    assert "Hello" in result["text"]
    assert "Cell" in result["text"]


def test_read_pdf_stdlib_fallback(tmp_path):
    # Minimal hand-built PDF with a FlateDecode stream containing "Hello".
    import zlib

    content = b"BT /F1 12 Tf 72 720 Td (Hello PDF) Tj ET"
    stream = b"stream\r\n" + zlib.compress(content) + b"\r\nendstream"
    pdf = (
        b"%PDF-1.4\n"
        b"1 0 obj << /Type /Catalog /Pages 2 0 R >> endobj\n"
        b"2 0 obj << /Type /Pages /Kids [3 0 R] /Count 1 >> endobj\n"
        b"3 0 obj << /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Contents 4 0 R >> endobj\n"
        b"4 0 obj << /Length " + str(len(stream)).encode() + b" >>\n" + stream + b"\nendobj\n"
        b"trailer << /Root 1 0 R >>\n%%EOF"
    )
    f = tmp_path / "t.pdf"
    f.write_bytes(pdf)
    result = read_pdf(str(f))
    assert result["success"] is True
    assert "Hello PDF" in result["text"]


def test_read_pptx_stdlib(tmp_path):
    import xml.etree.ElementTree as ET
    import zipfile

    f = tmp_path / "t.pptx"
    ns = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
    slide = ET.Element(f"{ns}sld")
    sp = ET.SubElement(slide, f"{ns}sp")
    tx = ET.SubElement(sp, f"{ns}txBody")
    ET.SubElement(tx, f"{ns}p").append(ET.Element(f"{ns}t"))
    p = ET.SubElement(tx, f"{ns}p")
    ET.SubElement(p, f"{ns}t").text = "Slide title here"
    slide_xml = ET.tostring(slide, encoding="unicode")

    with zipfile.ZipFile(f, "w") as zf:
        zf.writestr("[Content_Types].xml", '<?xml version="1.0"?><Types/>')
        zf.writestr("ppt/presentation.xml", '<?xml version="1.0"?><p:presentation/>')
        zf.writestr("ppt/slides/slide1.xml", slide_xml)
    result = read_pptx(str(f))
    assert result["success"] is True
    assert "Slide title here" in result["text"]


def test_summarize_document_offline_fallback(tmp_path):
    f = tmp_path / "notes.txt"
    f.write_text(
        "This is the first important sentence about the project. "
        "This is the second sentence covering the deadline on Friday. "
        "This is the third sentence describing the next steps. "
        "This is the fourth sentence with a conclusion.",
        encoding="utf-8",
    )
    result = summarize_document(str(f))
    assert result["success"] is True
    assert result["engine"] == "extractive"  # pytest is offline
    assert result["summary"]


# --------------------------------------------------------------------- #
# System tools
# --------------------------------------------------------------------- #


def test_get_system_info():
    info = get_system_info()
    assert info["success"] is True
    assert info["os"].lower() == "windows"
    assert info["python"]


def test_clipboard_roundtrip():
    result = set_clipboard("jarvis_clipboard_test_123")
    assert result["success"] is True
    read = get_clipboard()
    assert read["success"] is True
    assert "jarvis_clipboard_test_123" in read["text"]


def test_set_volume_direction():
    result = set_volume("up")
    assert result["success"] is True
    assert result["action"] == "up"


def test_set_volume_unknown_direction():
    result = set_volume("sideways")
    assert result["success"] is False
    assert "Unknown direction" in result["error"]


def test_launch_and_terminate_process():
    result = launch_process("cmd /c exit 0")
    assert result["success"] is True
    assert result["pid"] > 0


def test_terminate_process_invalid():
    result = terminate_process(pid="not_a_number")
    assert result["success"] is False


def test_terminate_process_by_pid():
    result = launch_process("ping -n 2 127.0.0.1")
    assert result["success"] is True
    pid = result["pid"]
    killed = terminate_process(pid=pid)
    assert killed["success"] is True