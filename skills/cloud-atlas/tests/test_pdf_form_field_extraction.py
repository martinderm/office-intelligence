"""Durable regression pins for filled PDF AcroForm field extraction (Issue #20).

These pins encode the requirements confirmed by the Issue #20 review findings:

1. every table cell (name, type, value) is rendered on one line and literally,
   with Markdown and HTML neutralised (no injected headings, images or raw HTML);
2. ``Off``/``/Off`` is dropped only for button-family widgets, while text values
   such as ``Off`` are preserved;
3. extraction reports an explicit, bounded status
   (``complete``/``no_fields``/``partial``/``dependency_missing``/``error``) with
   bounded warnings instead of silently returning an empty or partial list;
4. the status contract is transported through the worker and scheduler and is
   persisted deliberately in the filemap (never in the canonical frontmatter);
5. PyMuPDF stays an optional dependency: the legacy ``fitz`` import and a missing
   module are exercised explicitly, so no module-wide skip hides these paths;
6. ``enrich_source`` captures the field values *before* the destructive in-place
   OCR rewrites the source and records the pre-OCR source hash and tool/version,
   while ``local_derivative`` keeps reading the fields from the untouched original.
"""
import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))
import convert_cloud_docs as ccd

try:  # pragma: no cover - import guard, exercised in the environment
    import pymupdf as _pymupdf
except ImportError:  # pragma: no cover
    try:
        import fitz as _pymupdf
    except ImportError:
        _pymupdf = None

requires_pymupdf = unittest.skipUnless(
    _pymupdf is not None, "PyMuPDF is required for real synthetic PDF fixtures"
)


# --------------------------------------------------------------------------
# Real synthetic PDF fixtures (need PyMuPDF)
# --------------------------------------------------------------------------

def _make_form_pdf(path, fields):
    """Create a one-page PDF with the given ``(name, field_type, value)`` widgets."""
    doc = _pymupdf.open()
    page = doc.new_page()
    for idx, (name, field_type, value) in enumerate(fields):
        widget = _pymupdf.Widget()
        widget.rect = _pymupdf.Rect(100, 40 + 30 * idx, 320, 65 + 30 * idx)
        widget.field_name = name
        widget.field_type = field_type
        page.add_widget(widget)
        if value is None:
            continue
        stored = [w for w in page.widgets() if w.field_name == name][-1]
        if field_type == _pymupdf.PDF_WIDGET_TYPE_CHECKBOX:
            stored.field_value = value
            stored.update()
        else:
            doc.xref_set_key(stored.xref, "V", _pymupdf.get_pdf_str(value))
    doc.save(str(path))
    doc.close()


def _make_blank_pdf(path):
    """Create a one-page PDF without any form widgets."""
    doc = _pymupdf.open()
    doc.new_page()
    doc.save(str(path))
    doc.close()


# --------------------------------------------------------------------------
# Controllable fake PyMuPDF boundary (no real dependency needed)
# --------------------------------------------------------------------------

class _FakeWidget:
    def __init__(self, name="", field_type=7, value="", type_string="Text", fail_read=False):
        self.field_name = name
        self.field_type = field_type
        self.field_type_string = type_string
        self._value = value
        self._fail_read = fail_read

    @property
    def field_value(self):
        if self._fail_read:
            raise ValueError("corrupt widget value")
        return self._value


class _FakePage:
    def __init__(self, number=0, widgets=None, widgets_error=None):
        self.number = number
        self._widgets = widgets or []
        self._widgets_error = widgets_error

    def widgets(self):
        if self._widgets_error:
            raise RuntimeError(self._widgets_error)
        return list(self._widgets)


class _FakeDoc:
    def __init__(self, pages=None):
        self._pages = pages or []

    def __iter__(self):
        return iter(self._pages)

    def close(self):
        pass


class _FakeModule:
    """Minimal PyMuPDF-shaped module for boundary tests."""

    PDF_WIDGET_TYPE_UNKNOWN = 0
    PDF_WIDGET_TYPE_BUTTON = 1
    PDF_WIDGET_TYPE_CHECKBOX = 2
    PDF_WIDGET_TYPE_COMBOBOX = 3
    PDF_WIDGET_TYPE_LISTBOX = 4
    PDF_WIDGET_TYPE_RADIOBUTTON = 5
    PDF_WIDGET_TYPE_SIGNATURE = 6
    PDF_WIDGET_TYPE_TEXT = 7
    __version__ = "9.9.9-fake"

    def __init__(self, doc=None, open_error=None):
        self._doc = doc
        self._open_error = open_error

    def open(self, _path):
        if self._open_error:
            raise RuntimeError(self._open_error)
        return self._doc


class _FakeWidgetBadMetadata:
    """Widget whose name/type access can raise, to pin bounded degradation."""

    def __init__(self, name="b", type_string="Text", value="v", fail_on="name", field_type=7):
        self.field_type = field_type
        self._name = name
        self._type = type_string
        self._value = value
        self._fail_on = fail_on

    @property
    def field_value(self):
        return self._value

    @property
    def field_name(self):
        if self._fail_on == "name":
            raise RuntimeError("corrupt field name")
        return self._name

    @property
    def field_type_string(self):
        if self._fail_on == "type":
            raise RuntimeError("corrupt field type")
        return self._type


class _FakePageLazyWidgets:
    def __init__(self, number, widgets):
        self.number = number
        self._widgets = widgets

    def widgets(self):
        return self._widgets


class _FakeDocFailAfter:
    """Document whose lazy iteration raises instead of ending after its pages."""

    def __init__(self, pages):
        self._pages = pages
        self.closed = False

    def __iter__(self):
        for page in self._pages:
            yield page
        raise RuntimeError("document iteration failed")

    def close(self):
        self.closed = True


class _FakeDocCloseRecorder:
    def __init__(self, pages):
        self._pages = pages
        self.closed = False

    def __iter__(self):
        return iter(self._pages)

    def close(self):
        self.closed = True


def _run_worker(task, **patches):
    """Run ``_convert_worker_target`` in-process and return its pipe message."""
    parent, child = ccd.multiprocessing.Pipe()
    with contextlib.ExitStack() as stack:
        for name, value in patches.items():
            stack.enter_context(mock.patch.object(ccd, name, value))
        ccd._convert_worker_target(task, child)
    status, payload = parent.recv()
    parent.close()
    return status, payload


class _InlineProcess:
    """Run a worker target synchronously so scheduler mapping is observable."""

    def __init__(self, target=None, args=(), kwargs=None):
        self._target = target
        self._args = args
        self._kwargs = kwargs or {}
        self.exitcode = None

    def start(self):
        try:
            self._target(*self._args, **self._kwargs)
        finally:
            if self.exitcode is None:
                self.exitcode = 0

    def is_alive(self):
        return False

    def join(self, timeout=None):
        return None

    def terminate(self):
        return None

    def kill(self):
        return None


# --------------------------------------------------------------------------
# 1. Literal, single-line cells (Markdown + HTML)
# --------------------------------------------------------------------------

class LiteralCellTests(unittest.TestCase):
    def test_pipe_is_escaped(self):
        self.assertEqual("a\\|b", ccd.literal_markdown_cell("a|b"))

    def test_newlines_and_tabs_collapse_to_single_line(self):
        self.assertEqual("a b c", ccd.literal_markdown_cell("a\r\nb\tc"))
        self.assertNotIn("\n", ccd.literal_markdown_cell("x\ny"))

    def test_unicode_spacing_is_preserved_faithfully(self):
        # A non-breaking space is data, not line noise: only ASCII whitespace is
        # collapsed, so the value reaches the mirror unchanged.
        self.assertEqual("a\u00a0b", ccd.literal_markdown_cell("a\u00a0b"))

    def test_unicode_line_separators_are_collapsed(self):
        # U+2028/U+2029 are line separators in some renderers; collapsing them
        # keeps every cell on a single line (NBSP above still survives).
        self.assertEqual("a b", ccd.literal_markdown_cell("a\u2028b"))
        self.assertEqual("a b", ccd.literal_markdown_cell("a\u2029b"))

    def test_none_value_renders_an_empty_cell(self):
        self.assertEqual("", ccd.literal_markdown_cell(None))

    def test_unknown_page_renders_without_a_literal_none(self):
        markdown, _ = ccd.render_pdf_form_fields_section("Body\n", {
            "fields": [{"page": None, "field": "f", "type": "Text", "value": "v"}],
        })
        self.assertNotIn("None", markdown)

    def test_html_is_entity_escaped(self):
        self.assertEqual("&lt;img src=x&gt;", ccd.literal_markdown_cell("<img src=x>"))
        self.assertEqual("a &amp; b", ccd.literal_markdown_cell("a & b"))

    def test_markdown_link_and_image_syntax_is_neutralised(self):
        # Escaping the brackets is what makes the image/link syntax inert; the
        # parentheses are not structural on their own and stay readable.
        self.assertEqual("!\\[x\\](y)", ccd.literal_markdown_cell("![x](y)"))
        self.assertNotIn("![", ccd.literal_markdown_cell("![x](y)"))
        self.assertNotIn("[x]", ccd.literal_markdown_cell("[x](y)"))

    def test_backticks_and_emphasis_are_escaped(self):
        self.assertEqual("\\`code\\`", ccd.literal_markdown_cell("`code`"))
        self.assertEqual("\\*bold\\*", ccd.literal_markdown_cell("*bold*"))

    def test_backslash_is_escaped_first(self):
        self.assertEqual("back\\\\slash", ccd.literal_markdown_cell("back\\slash"))

    def test_malicious_name_cannot_inject_a_heading(self):
        extraction = {
            "fields": [{
                "page": 1,
                "field": "evil\n## Injected Heading",
                "type": "Text",
                "value": "![x](http://example.invalid/pixel.png)\n\n# Injected H2",
            }]
        }
        markdown, count = ccd.render_pdf_form_fields_section("Body\n", extraction)
        self.assertEqual(1, count)
        self.assertNotIn("\n## Injected Heading", markdown)
        self.assertNotIn("\n# Injected H2", markdown)
        self.assertNotIn("![", markdown)
        for line in markdown.splitlines():
            self.assertNotIn("\n", line)
        # The value row is a single table line and still ends the table.
        value_row = [line for line in markdown.splitlines() if "Injected" in line][-1]
        self.assertTrue(value_row.startswith("| "))

    def test_html_value_cannot_become_raw_html(self):
        extraction = {
            "fields": [{"page": 2, "field": "n", "type": "Text",
                        "value": "<img src=x onerror=alert(1)>"}]
        }
        markdown, _ = ccd.render_pdf_form_fields_section("Body\n", extraction)
        self.assertNotIn("<img", markdown)
        self.assertIn("&lt;img", markdown)

    def test_render_omits_section_without_fields(self):
        markdown, count = ccd.render_pdf_form_fields_section("Body\n", {"fields": []})
        self.assertEqual("Body\n", markdown)
        self.assertEqual(0, count)


# --------------------------------------------------------------------------
# 2. Type-aware Off detection
# --------------------------------------------------------------------------

class OffHandlingTests(unittest.TestCase):
    def test_text_off_value_is_preserved(self):
        module = _FakeModule(_FakeDoc([_FakePage(0, [
            _FakeWidget("t", _FakeModule.PDF_WIDGET_TYPE_TEXT, "Off", "Text"),
        ])]))
        with mock.patch.object(ccd, "_load_pymupdf", return_value=(module, "pymupdf")):
            result = ccd.extract_pdf_form_fields("ignored.pdf")
        self.assertEqual("complete", result["status"])
        self.assertEqual(["Off"], [f["value"] for f in result["fields"]])

    def test_text_slash_off_value_is_preserved(self):
        module = _FakeModule(_FakeDoc([_FakePage(0, [
            _FakeWidget("t", _FakeModule.PDF_WIDGET_TYPE_TEXT, "/Off", "Text"),
        ])]))
        with mock.patch.object(ccd, "_load_pymupdf", return_value=(module, "pymupdf")):
            result = ccd.extract_pdf_form_fields("ignored.pdf")
        self.assertEqual(["/Off"], [f["value"] for f in result["fields"]])

    def test_unchecked_button_off_is_dropped(self):
        module = _FakeModule(_FakeDoc([_FakePage(0, [
            _FakeWidget("c", _FakeModule.PDF_WIDGET_TYPE_CHECKBOX, "Off", "CheckBox"),
            _FakeWidget("t", _FakeModule.PDF_WIDGET_TYPE_TEXT, "kept", "Text"),
        ])]))
        with mock.patch.object(ccd, "_load_pymupdf", return_value=(module, "pymupdf")):
            result = ccd.extract_pdf_form_fields("ignored.pdf")
        self.assertEqual(["kept"], [f["value"] for f in result["fields"]])

    def test_checked_button_value_is_kept(self):
        module = _FakeModule(_FakeDoc([_FakePage(0, [
            _FakeWidget("c", _FakeModule.PDF_WIDGET_TYPE_CHECKBOX, "Yes", "CheckBox"),
        ])]))
        with mock.patch.object(ccd, "_load_pymupdf", return_value=(module, "pymupdf")):
            result = ccd.extract_pdf_form_fields("ignored.pdf")
        self.assertEqual(["Yes"], [f["value"] for f in result["fields"]])

    def test_custom_uppercase_off_on_state_is_kept(self):
        # A checkbox may use a custom on-state literally named ``OFF``; it is a
        # checked value, not the unchecked marker, so it must survive.
        # (PyMuPDF's supported API normalises a set custom on-state to ``Off``,
        # so this checked-state semantics is pinned with the fake boundary rather
        # than a real library fixture, which is not constructible.)
        module = _FakeModule(_FakeDoc([_FakePage(0, [
            _FakeWidget("c", _FakeModule.PDF_WIDGET_TYPE_CHECKBOX, "OFF", "CheckBox"),
        ])]))
        with mock.patch.object(ccd, "_load_pymupdf", return_value=(module, "pymupdf")):
            result = ccd.extract_pdf_form_fields("ignored.pdf")
        self.assertEqual(["OFF"], [f["value"] for f in result["fields"]])

    def test_custom_lowercase_off_on_state_is_kept(self):
        module = _FakeModule(_FakeDoc([_FakePage(0, [
            _FakeWidget("c", _FakeModule.PDF_WIDGET_TYPE_CHECKBOX, "off", "CheckBox"),
        ])]))
        with mock.patch.object(ccd, "_load_pymupdf", return_value=(module, "pymupdf")):
            result = ccd.extract_pdf_form_fields("ignored.pdf")
        self.assertEqual(["off"], [f["value"] for f in result["fields"]])

    def test_exact_slash_off_button_marker_is_still_dropped(self):
        module = _FakeModule(_FakeDoc([_FakePage(0, [
            _FakeWidget("c", _FakeModule.PDF_WIDGET_TYPE_CHECKBOX, "/Off", "CheckBox"),
        ])]))
        with mock.patch.object(ccd, "_load_pymupdf", return_value=(module, "pymupdf")):
            result = ccd.extract_pdf_form_fields("ignored.pdf")
        self.assertEqual([], result["fields"])

    @requires_pymupdf
    def test_real_text_off_and_checkbox_off_are_distinguished(self):
        with tempfile.TemporaryDirectory() as tmp:
            pdf = Path(tmp) / "form.pdf"
            _make_form_pdf(pdf, [
                ("text_off", _pymupdf.PDF_WIDGET_TYPE_TEXT, "Off"),
                ("text_slash_off", _pymupdf.PDF_WIDGET_TYPE_TEXT, "/Off"),
                ("check_unchecked", _pymupdf.PDF_WIDGET_TYPE_CHECKBOX, None),
                ("check_checked", _pymupdf.PDF_WIDGET_TYPE_CHECKBOX, "Yes"),
            ])
            result = ccd.extract_pdf_form_fields(str(pdf))
        values = {f["field"]: f["value"] for f in result["fields"]}
        self.assertEqual("Off", values["text_off"])
        self.assertEqual("/Off", values["text_slash_off"])
        self.assertNotIn("check_unchecked", values)
        self.assertEqual("Yes", values["check_checked"])


# --------------------------------------------------------------------------
# 3. Bounded, explicit statuses
# --------------------------------------------------------------------------

class ExtractionStatusTests(unittest.TestCase):
    def test_dependency_missing_status(self):
        with mock.patch.object(ccd, "_load_pymupdf", return_value=(None, None)):
            result = ccd.extract_pdf_form_fields("ignored.pdf")
        self.assertEqual("dependency_missing", result["status"])
        self.assertEqual([], result["fields"])
        self.assertIsNone(result["tool"])
        self.assertEqual("pymupdf_missing", result["warnings"][0]["code"])

    def test_open_failure_is_error_not_empty_extraction(self):
        module = _FakeModule(open_error="cannot open")
        with mock.patch.object(ccd, "_load_pymupdf", return_value=(module, "pymupdf")):
            result = ccd.extract_pdf_form_fields("ignored.pdf")
        self.assertEqual("error", result["status"])
        self.assertEqual([], result["fields"])
        self.assertEqual("open_failed", result["warnings"][0]["code"])

    def test_no_fields_status(self):
        module = _FakeModule(_FakeDoc([_FakePage(0, [])]))
        with mock.patch.object(ccd, "_load_pymupdf", return_value=(module, "pymupdf")):
            result = ccd.extract_pdf_form_fields("ignored.pdf")
        self.assertEqual("no_fields", result["status"])
        self.assertEqual([], result["warnings"])

    def test_complete_status_with_fields(self):
        module = _FakeModule(_FakeDoc([_FakePage(0, [
            _FakeWidget("a", _FakeModule.PDF_WIDGET_TYPE_TEXT, "v", "Text"),
        ])]))
        with mock.patch.object(ccd, "_load_pymupdf", return_value=(module, "pymupdf")):
            result = ccd.extract_pdf_form_fields("ignored.pdf")
        self.assertEqual("complete", result["status"])

    def test_partial_status_on_widget_read_failure(self):
        module = _FakeModule(_FakeDoc([_FakePage(0, [
            _FakeWidget("bad", _FakeModule.PDF_WIDGET_TYPE_TEXT, "x", "Text", fail_read=True),
            _FakeWidget("good", _FakeModule.PDF_WIDGET_TYPE_TEXT, "y", "Text"),
        ])]))
        with mock.patch.object(ccd, "_load_pymupdf", return_value=(module, "pymupdf")):
            result = ccd.extract_pdf_form_fields("ignored.pdf")
        self.assertEqual("partial", result["status"])
        self.assertEqual(["y"], [f["value"] for f in result["fields"]])
        self.assertEqual("widget_read_failed", result["warnings"][0]["code"])

    def test_partial_status_on_page_read_failure(self):
        module = _FakeModule(_FakeDoc([
            _FakePage(0, widgets_error="broken page"),
            _FakePage(1, [_FakeWidget("good", _FakeModule.PDF_WIDGET_TYPE_TEXT, "y", "Text")]),
        ]))
        with mock.patch.object(ccd, "_load_pymupdf", return_value=(module, "pymupdf")):
            result = ccd.extract_pdf_form_fields("ignored.pdf")
        self.assertEqual("partial", result["status"])
        self.assertEqual("page_read_failed", result["warnings"][0]["code"])

    def test_field_count_is_bounded(self):
        widgets = [
            _FakeWidget(f"f{i}", _FakeModule.PDF_WIDGET_TYPE_TEXT, f"v{i}", "Text")
            for i in range(ccd.MAX_PDF_FORM_FIELDS + 5)
        ]
        module = _FakeModule(_FakeDoc([_FakePage(0, widgets)]))
        with mock.patch.object(ccd, "_load_pymupdf", return_value=(module, "pymupdf")):
            result = ccd.extract_pdf_form_fields("ignored.pdf")
        self.assertEqual(ccd.MAX_PDF_FORM_FIELDS, len(result["fields"]))
        self.assertEqual("partial", result["status"])
        self.assertTrue(any(w["code"] == "field_limit_reached" for w in result["warnings"]))

    def test_field_text_is_bounded_with_warning(self):
        long_value = "x" * (ccd.MAX_PDF_FORM_FIELD_TEXT + 50)
        module = _FakeModule(_FakeDoc([_FakePage(0, [
            _FakeWidget("f", _FakeModule.PDF_WIDGET_TYPE_TEXT, long_value, "Text"),
        ])]))
        with mock.patch.object(ccd, "_load_pymupdf", return_value=(module, "pymupdf")):
            result = ccd.extract_pdf_form_fields("ignored.pdf")
        self.assertEqual(ccd.MAX_PDF_FORM_FIELD_TEXT, len(result["fields"][0]["value"]))
        self.assertTrue(any(w["code"] == "field_text_truncated" for w in result["warnings"]))

    def test_warning_list_is_bounded(self):
        widgets = [
            _FakeWidget(f"bad{i}", _FakeModule.PDF_WIDGET_TYPE_TEXT, "x", "Text", fail_read=True)
            for i in range(ccd.MAX_PDF_FORM_FIELD_WARNINGS + 10)
        ]
        module = _FakeModule(_FakeDoc([_FakePage(0, widgets)]))
        with mock.patch.object(ccd, "_load_pymupdf", return_value=(module, "pymupdf")):
            result = ccd.extract_pdf_form_fields("ignored.pdf")
        self.assertLessEqual(len(result["warnings"]), ccd.MAX_PDF_FORM_FIELD_WARNINGS)
        # A bounded warning list must not hide the fact that the extraction is incomplete.
        self.assertEqual("partial", result["status"])

    def test_widget_name_read_failure_degrades_not_aborts(self):
        module = _FakeModule(_FakeDoc([
            _FakePage(0, [_FakeWidget("good", _FakeModule.PDF_WIDGET_TYPE_TEXT, "val", "Text")]),
            _FakePage(1, [_FakeWidgetBadMetadata(fail_on="name")]),
        ]))
        with mock.patch.object(ccd, "_load_pymupdf", return_value=(module, "pymupdf")):
            result = ccd.extract_pdf_form_fields("ignored.pdf")
        self.assertEqual("partial", result["status"])
        self.assertEqual(["val"], [f["value"] for f in result["fields"]])
        self.assertTrue(any(w["code"] == "widget_read_failed" for w in result["warnings"]))

    def test_widget_type_read_failure_degrades_not_aborts(self):
        module = _FakeModule(_FakeDoc([_FakePage(0, [
            _FakeWidget("good", _FakeModule.PDF_WIDGET_TYPE_TEXT, "val", "Text"),
            _FakeWidgetBadMetadata(fail_on="type"),
        ])]))
        with mock.patch.object(ccd, "_load_pymupdf", return_value=(module, "pymupdf")):
            result = ccd.extract_pdf_form_fields("ignored.pdf")
        self.assertEqual("partial", result["status"])
        self.assertEqual(["val"], [f["value"] for f in result["fields"]])

    def test_lazy_widget_iterator_failure_keeps_fields_and_continues_pages(self):
        def broken_iterator():
            yield _FakeWidget("good", _FakeModule.PDF_WIDGET_TYPE_TEXT, "first", "Text")
            raise RuntimeError("lazy widget iteration failed")

        module = _FakeModule(_FakeDoc([
            _FakePageLazyWidgets(0, broken_iterator()),
            _FakePage(1, [_FakeWidget("later", _FakeModule.PDF_WIDGET_TYPE_TEXT, "second", "Text")]),
        ]))
        with mock.patch.object(ccd, "_load_pymupdf", return_value=(module, "pymupdf")):
            result = ccd.extract_pdf_form_fields("ignored.pdf")
        self.assertEqual("partial", result["status"])
        self.assertEqual(["first", "second"], [f["value"] for f in result["fields"]])

    def test_document_iteration_failure_with_fields_is_partial(self):
        doc = _FakeDocFailAfter(
            [_FakePage(0, [_FakeWidget("good", _FakeModule.PDF_WIDGET_TYPE_TEXT, "val", "Text")])],
        )
        module = _FakeModule(doc)
        with mock.patch.object(ccd, "_load_pymupdf", return_value=(module, "pymupdf")):
            result = ccd.extract_pdf_form_fields("ignored.pdf")
        self.assertEqual("partial", result["status"])
        self.assertEqual(["val"], [f["value"] for f in result["fields"]])
        self.assertTrue(any(w["code"] == "document_iteration_failed" for w in result["warnings"]))

    def test_document_iteration_failure_without_fields_is_error(self):
        module = _FakeModule(_FakeDocFailAfter([]))
        with mock.patch.object(ccd, "_load_pymupdf", return_value=(module, "pymupdf")):
            result = ccd.extract_pdf_form_fields("ignored.pdf")
        self.assertEqual("error", result["status"])
        self.assertEqual([], result["fields"])

    def test_document_is_closed_when_widget_metadata_fails(self):
        doc = _FakeDocCloseRecorder([_FakePage(0, [_FakeWidgetBadMetadata(fail_on="name")])])
        module = _FakeModule(doc)
        with mock.patch.object(ccd, "_load_pymupdf", return_value=(module, "pymupdf")):
            ccd.extract_pdf_form_fields("ignored.pdf")
        self.assertTrue(doc.closed)

    def test_field_name_and_type_are_bounded(self):
        long_name = "n" * (ccd.MAX_PDF_FORM_FIELD_NAME + 20)
        long_type = "t" * (ccd.MAX_PDF_FORM_FIELD_TYPE + 20)
        module = _FakeModule(_FakeDoc([_FakePage(0, [
            _FakeWidget(long_name, _FakeModule.PDF_WIDGET_TYPE_TEXT, "v", long_type),
        ])]))
        with mock.patch.object(ccd, "_load_pymupdf", return_value=(module, "pymupdf")):
            result = ccd.extract_pdf_form_fields("ignored.pdf")
        field = result["fields"][0]
        self.assertEqual(ccd.MAX_PDF_FORM_FIELD_NAME, len(field["field"]))
        self.assertEqual(ccd.MAX_PDF_FORM_FIELD_TYPE, len(field["type"]))
        self.assertEqual("partial", result["status"])
        codes = {w["code"] for w in result["warnings"]}
        self.assertIn("field_name_truncated", codes)
        self.assertIn("field_type_truncated", codes)


# --------------------------------------------------------------------------
# 4. Optional dependency: legacy fitz fallback / missing capability
# --------------------------------------------------------------------------

class OptionalDependencyTests(unittest.TestCase):
    def test_legacy_fitz_is_used_and_recorded(self):
        module = _FakeModule(_FakeDoc([_FakePage(0, [
            _FakeWidget("a", _FakeModule.PDF_WIDGET_TYPE_TEXT, "v", "Text"),
        ])]))
        with mock.patch.object(ccd, "_load_pymupdf", return_value=(module, "fitz")):
            result = ccd.extract_pdf_form_fields("ignored.pdf")
        self.assertEqual("fitz", result["tool"])
        self.assertEqual("9.9.9-fake", result["tool_version"])
        self.assertEqual("complete", result["status"])

    @requires_pymupdf
    def test_real_module_load_prefers_pymupdf_or_fitz(self):
        module, name = ccd._load_pymupdf()
        self.assertIsNotNone(module)
        self.assertIn(name, ("pymupdf", "fitz"))

    def test_load_pymupdf_without_any_module_is_absent(self):
        # Exercised through the real import machinery, so it runs on every host
        # (no real PyMuPDF and no ``_load_pymupdf`` mock needed).
        with mock.patch.dict(sys.modules, {"pymupdf": None, "fitz": None}):
            module, name = ccd._load_pymupdf()
        self.assertIsNone(module)
        self.assertIsNone(name)

    def test_load_pymupdf_falls_back_to_legacy_fitz_when_modern_absent(self):
        fake_fitz = _FakeModule(_FakeDoc([]))
        with mock.patch.dict(sys.modules, {"pymupdf": None, "fitz": fake_fitz}):
            module, name = ccd._load_pymupdf()
        self.assertIs(fake_fitz, module)
        self.assertEqual("fitz", name)

    def test_load_pymupdf_treats_non_importerror_import_failure_as_absent(self):
        import builtins

        real_import = builtins.__import__

        def guarded_import(name, *args, **kwargs):
            if name == "pymupdf":
                raise OSError("broken optional binary")
            if name == "fitz":
                raise ImportError("no legacy alias")
            return real_import(name, *args, **kwargs)

        with mock.patch("builtins.__import__", side_effect=guarded_import):
            module, name = ccd._load_pymupdf()
        self.assertIsNone(module)
        self.assertIsNone(name)


# --------------------------------------------------------------------------
# 5. Backward-compatible helper API
# --------------------------------------------------------------------------

class CompatibilityTests(unittest.TestCase):
    def test_extract_values_returns_a_list(self):
        module = _FakeModule(_FakeDoc([_FakePage(0, [
            _FakeWidget("a", _FakeModule.PDF_WIDGET_TYPE_TEXT, "v", "Text"),
        ])]))
        with mock.patch.object(ccd, "_load_pymupdf", return_value=(module, "pymupdf")):
            fields = ccd.extract_pdf_form_field_values("ignored.pdf")
        self.assertIsInstance(fields, list)
        self.assertEqual("v", fields[0]["value"])

    @requires_pymupdf
    def test_append_section_graceful_without_fields(self):
        with tempfile.TemporaryDirectory() as tmp:
            markdown, count = ccd.append_pdf_form_fields_section("Body\n", str(Path(tmp) / "missing.pdf"))
        self.assertEqual("Body\n", markdown)
        self.assertEqual(0, count)

    @requires_pymupdf
    def test_append_section_with_real_fields(self):
        with tempfile.TemporaryDirectory() as tmp:
            pdf = Path(tmp) / "form.pdf"
            _make_form_pdf(pdf, [
                ("fee", _pymupdf.PDF_WIDGET_TYPE_TEXT, "700\u20ac"),
                ("note", _pymupdf.PDF_WIDGET_TYPE_TEXT, "Text with | pipe\nand newline"),
                ("empty", _pymupdf.PDF_WIDGET_TYPE_TEXT, ""),
            ])
            markdown, count = ccd.append_pdf_form_fields_section("Body\n", str(pdf))
        self.assertGreaterEqual(count, 2)
        self.assertIn("## Formularfelder (ausgefüllt)", markdown)
        self.assertIn("700", markdown)
        self.assertIn("Text with \\| pipe and newline", markdown)
        self.assertTrue(markdown.startswith("Body\n"))

    @requires_pymupdf
    def test_real_extraction_collapses_newlines_and_keeps_values(self):
        with tempfile.TemporaryDirectory() as tmp:
            pdf = Path(tmp) / "form.pdf"
            _make_form_pdf(pdf, [
                ("fee", _pymupdf.PDF_WIDGET_TYPE_TEXT, "700\u20ac"),
                ("note", _pymupdf.PDF_WIDGET_TYPE_TEXT, "Text with | pipe\nand newline"),
                ("empty", _pymupdf.PDF_WIDGET_TYPE_TEXT, ""),
            ])
            result = ccd.extract_pdf_form_fields(str(pdf))
        names = {f["field"] for f in result["fields"]}
        self.assertIn("fee", names)
        self.assertIn("note", names)
        self.assertNotIn("empty", names)
        note = next(f for f in result["fields"] if f["field"] == "note")
        self.assertEqual("Text with | pipe and newline", note["value"])
        self.assertEqual("complete", result["status"])


# --------------------------------------------------------------------------
# 6. Worker + scheduler transport
# --------------------------------------------------------------------------

class WorkerTransportTests(unittest.TestCase):
    def _pdf_task(self, **overrides):
        task = {
            "src_abs": "C:/nonexistent/form.pdf",
            "src_rel": "data/cloud/X/form.pdf",
            "src_sha256": "a" * 64,
            "is_doc": False,
            "is_pdf": True,
            "ocr_policy": "disabled",
            "redo_ocr": False,
            "derivative_abs": None,
            "derivative_rel": None,
            "cloud_path_abs": None,
            "file_timeout": 30,
        }
        task.update(overrides)
        return task

    def test_worker_payload_carries_form_fields_contract(self):
        module = _FakeModule(_FakeDoc([_FakePage(0, [
            _FakeWidget("a", _FakeModule.PDF_WIDGET_TYPE_TEXT, "v", "Text"),
        ])]))
        status, payload = _run_worker(
            self._pdf_task(),
            convert_to_markdown_raw=lambda _p: "Body\n",
            _load_pymupdf=lambda: (module, "pymupdf"),
        )
        self.assertEqual("ok", status)
        self.assertEqual(1, payload["form_fields_filled"])
        self.assertEqual("complete", payload["form_fields"]["status"])
        self.assertEqual(1, payload["form_fields"]["count"])
        self.assertEqual("pymupdf", payload["form_fields"]["tool"])
        self.assertIn("Formularfelder", payload["text"])

    def test_worker_dependency_missing_leaves_mirror_unchanged(self):
        status, payload = _run_worker(
            self._pdf_task(),
            convert_to_markdown_raw=lambda _p: "Body\n",
            _load_pymupdf=lambda: (None, None),
        )
        self.assertEqual("ok", status)
        self.assertEqual(0, payload["form_fields_filled"])
        self.assertEqual("dependency_missing", payload["form_fields"]["status"])
        self.assertNotIn("Formularfelder", payload["text"])

    def test_scheduler_propagates_form_fields(self):
        module = _FakeModule(_FakeDoc([_FakePage(0, [
            _FakeWidget("a", _FakeModule.PDF_WIDGET_TYPE_TEXT, "v", "Text"),
        ])]))
        task = {
            "src_abs": "C:/nonexistent/form.pdf",
            "src_rel": "data/cloud/X/form.pdf",
            "dest_abs": "C:/nonexistent/form.md",
            "dest_rel": "memory/cloud/x/form.md",
            "src_sha256": "a" * 64,
            "src_mtime": "2026-01-01 00:00:00",
            "src_version": "N/A",
            "stat": os.stat_result((0, 0, 0, 0, 0, 0, 100, 0, 0, 0)),
            "is_doc": False,
            "is_pdf": True,
            "ocr_policy": "disabled",
            "redo_ocr": False,
            "file_timeout": 30,
        }
        with mock.patch.object(ccd, "convert_to_markdown_raw", return_value="Body\n"), \
             mock.patch.object(ccd, "_load_pymupdf", return_value=(module, "pymupdf")), \
             mock.patch.object(ccd.multiprocessing, "Process", _InlineProcess):
            results = ccd.run_conversion_tasks([task], file_timeout=30, max_jobs=1, ocr_policy="disabled")
        self.assertEqual("complete", results["data/cloud/X/form.pdf"]["form_fields"]["status"])
        self.assertEqual(1, results["data/cloud/X/form.pdf"]["form_fields_filled"])


# --------------------------------------------------------------------------
# 7. Filemap persistence + canonical frontmatter untouched
# --------------------------------------------------------------------------

class FilemapPersistenceTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        (self.root / "AGENTS.md").write_text("# Test workspace\n", encoding="utf-8")
        projects = self.root / "memory" / "references" / "projects"
        projects.mkdir(parents=True)
        cloud = self.root / "data" / "cloud" / "EXAMPLE"
        cloud.mkdir(parents=True)
        (cloud / "form.pdf").write_bytes(b"%PDF-1.4\n%%EOF")
        (projects / "projects.json").write_text(json.dumps([{
            "id": "example",
            "title": "Example",
            "cloud_sync": {
                "default": {
                    "scan_dir": "data/cloud/EXAMPLE",
                    "output_dir": "memory/cloud/projects/example",
                    "output_json": "memory/cloud/projects/example/filemap.json",
                }
            },
        }]), encoding="utf-8")

    def tearDown(self):
        self._tmp.cleanup()

    def _converted(self, tasks, **_kwargs):
        return {
            task["src_rel"]: {
                "success": True,
                "markdown_body": "Body\n",
                "ocr_applied": False,
                "derivative_path": None,
                "derivative_sha256": None,
                "conversion_method": "markitdown-direct",
                "potential_quality_loss": None,
                "ocr_policy": "disabled",
                "new_src_sha256": None,
                "new_src_size": None,
                "new_src_mtime": None,
                "form_fields_filled": 1,
                "form_fields": {
                    "status": "complete",
                    "count": 1,
                    "tool": "pymupdf",
                    "tool_version": "1.28.2",
                    "warnings": [],
                    "source_sha256": task["src_sha256"],
                    "extracted_before_ocr": False,
                },
            }
            for task in tasks
        }

    def test_filemap_persists_form_fields_contract(self):
        with mock.patch.object(ccd, "run_conversion_tasks", side_effect=self._converted), \
             contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            exit_code = ccd.main([
                "--project-id", "example", "--workspace-root", str(self.root),
                "--no-ocr", "--json",
            ])
        self.assertEqual(0, exit_code)
        filemap = json.loads((self.root / "memory" / "cloud" / "projects" / "example" / "filemap.json").read_text(encoding="utf-8"))
        entry = filemap["files"]["data/cloud/EXAMPLE/form.pdf"]
        self.assertEqual("complete", entry["form_fields"]["status"])
        self.assertEqual(1, entry["form_fields"]["count"])
        self.assertEqual("pymupdf", entry["form_fields"]["tool"])

    def test_canonical_frontmatter_is_not_expanded(self):
        with mock.patch.object(ccd, "run_conversion_tasks", side_effect=self._converted), \
             contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            ccd.main([
                "--project-id", "example", "--workspace-root", str(self.root),
                "--no-ocr", "--json",
            ])
        mirror = self.root / "memory" / "cloud" / "projects" / "example" / "form.md"
        self.assertTrue(mirror.is_file(), "mirror should exist")
        frontmatter = mirror.read_text(encoding="utf-8").split("---", 2)[1]
        self.assertNotIn("form_fields", frontmatter)


# --------------------------------------------------------------------------
# 8. OCR boundary: capture before destructive OCR / use original for derivative
# --------------------------------------------------------------------------

@requires_pymupdf
class OcrBoundaryTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_enrich_source_captures_fields_before_destructive_ocr(self):
        src = self.root / "enrich.pdf"
        _make_form_pdf(src, [("preocr", _pymupdf.PDF_WIDGET_TYPE_TEXT, "PRE-OCR-VALUE")])
        pre_hash = ccd.calculate_sha256(str(src))
        task = {
            "src_abs": str(src),
            "src_rel": "data/cloud/X/enrich.pdf",
            "src_sha256": pre_hash,
            "is_doc": False,
            "is_pdf": True,
            "ocr_policy": "enrich_source",
            "redo_ocr": True,
            "derivative_abs": None,
            "derivative_rel": None,
            "cloud_path_abs": None,
            "file_timeout": 30,
        }

        def destructive_ocr(*_args, **_kwargs):
            # Simulate an OCR run that rewrites the source and drops widget values.
            _make_blank_pdf(src)
            return True, "ok"

        status, payload = _run_worker(
            task,
            convert_to_markdown_raw=lambda _p: "Body\n",
            is_digitally_signed_pdf=lambda _p: False,
            run_ocr_on_pdf=destructive_ocr,
        )
        self.assertEqual("ok", status)
        self.assertIn("PRE-OCR-VALUE", payload["text"])
        contract = payload["form_fields"]
        self.assertTrue(contract["extracted_before_ocr"])
        self.assertEqual(pre_hash, contract["source_sha256"])
        # The destructive OCR really removed the widgets from the source.
        self.assertEqual("no_fields", ccd.extract_pdf_form_fields(str(src))["status"])

    def test_local_derivative_reads_fields_from_original(self):
        src = self.root / "original.pdf"
        _make_form_pdf(src, [("orig", _pymupdf.PDF_WIDGET_TYPE_TEXT, "FROM-ORIGINAL")])
        derivative = self.root / "_derivatives" / "original.pdf"
        task = {
            "src_abs": str(src),
            "src_rel": "data/cloud/X/original.pdf",
            "src_sha256": ccd.calculate_sha256(str(src)),
            "is_doc": False,
            "is_pdf": True,
            "ocr_policy": "local_derivative",
            "redo_ocr": False,
            "derivative_abs": str(derivative),
            "derivative_rel": "memory/cloud/x/_derivatives/original.pdf",
            "cloud_path_abs": None,
            "file_timeout": 30,
        }

        def derivative_ocr(*_args, **_kwargs):
            _make_blank_pdf(derivative)  # derivative has no widgets
            return True, "ok"

        status, payload = _run_worker(
            task,
            convert_to_markdown_raw=lambda _p: "Body\n",
            is_image_based_pdf=lambda *_a, **_k: True,
            run_ocr_on_pdf=derivative_ocr,
        )
        self.assertEqual("ok", status)
        self.assertIn("FROM-ORIGINAL", payload["text"])
        self.assertFalse(payload["form_fields"]["extracted_before_ocr"])


if __name__ == "__main__":
    unittest.main()
