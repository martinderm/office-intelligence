"""Tests for filled PDF AcroForm field extraction (Issue: form values missing from mirrors)."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

import pytest

pymupdf = pytest.importorskip("pymupdf", reason="PyMuPDF required for form-field tests")
import convert_cloud_docs as ccd


def _make_filled_form_pdf(path):
    doc = pymupdf.open()
    page = doc.new_page()
    widgets = [
        ("fee", "700\u20ac"),
        ("note", "Text with | pipe\nand newline"),
        ("empty", ""),
    ]
    for idx, (name, value) in enumerate(widgets):
        w = pymupdf.Widget()
        w.rect = pymupdf.Rect(100, 100 + 30 * idx, 300, 130 + 30 * idx)
        w.field_name = name
        w.field_type = pymupdf.PDF_WIDGET_TYPE_TEXT
        page.add_widget(w)
        if value:
            xref = [sw.xref for sw in page.widgets()][-1]
            doc.xref_set_key(xref, "V", pymupdf.get_pdf_str(value))
    doc.save(path)
    doc.close()


def test_extract_filled_form_fields(tmp_path):
    pdf = tmp_path / "form.pdf"
    _make_filled_form_pdf(str(pdf))
    fields = ccd.extract_pdf_form_field_values(str(pdf))
    names = {f["field"] for f in fields}
    assert "fee" in names and "note" in names
    assert "empty" not in names
    fee = next(f for f in fields if f["field"] == "fee")
    assert fee["value"] == "700\u20ac"
    note = next(f for f in fields if f["field"] == "note")
    # Newlines are collapsed for readability; the pipe must survive extraction
    # (escaping to the Markdown table happens in append_pdf_form_fields_section).
    assert note["value"] == "Text with | pipe and newline"


def test_append_section_omitted_without_fields(tmp_path):
    md, count = ccd.append_pdf_form_fields_section("Body\n", str(tmp_path / "missing.pdf"))
    assert md == "Body\n"
    assert count == 0


def test_append_section_with_fields(tmp_path):
    pdf = tmp_path / "form.pdf"
    _make_filled_form_pdf(str(pdf))
    md, count = ccd.append_pdf_form_fields_section("Body\n", str(pdf))
    assert count >= 2
    assert "## Formularfelder (ausgefüllt)" in md
    assert "700" in md
    assert md.startswith("Body\n")
