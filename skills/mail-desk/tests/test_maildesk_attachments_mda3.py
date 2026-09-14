"""TDD tests for FR-08 MD-A3: Bounded attachment extraction and local OCR derivative contract."""

from __future__ import annotations

import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock, patch
import zipfile

MAIL_DESK_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(MAIL_DESK_ROOT / "scripts"))

from core import himalaya  # noqa: E402
from core import attachment_policy  # noqa: E402
from core import attachments  # noqa: E402
from core import attachment_fetch as afetch  # noqa: E402
from core import attachment_extract as aextract  # noqa: E402
import mail_desk_himalaya_client as client  # noqa: E402


def _late_writer_target(write_path_str: str) -> str:
    """Module-level picklable helper to test process termination and late write prevention."""
    time.sleep(1.2)
    Path(write_path_str).write_text("late write succeeded", encoding="utf-8")
    return "done"


def _marker_side_effect_target(marker_path_str: str) -> str:
    """Module-level picklable helper to verify whether worker payload executes."""
    Path(marker_path_str).write_text("PAYLOAD_EXECUTED", encoding="utf-8")
    return "executed"



def _mock_ocr_pdf_2pages(source: Path, deriv: Path, max_pages: int = 3, pages: Any = None, timeout: float = 30) -> tuple[bytes, str, int]:
    import pymupdf
    doc = pymupdf.open()
    p1 = doc.new_page()
    p1.insert_text((50, 50), "OCR Page 1 Extracted")
    p2 = doc.new_page()
    p2.insert_text((50, 50), "OCR Page 2 Extracted")
    data = doc.tobytes()
    doc.close()
    deriv.parent.mkdir(parents=True, exist_ok=True)
    deriv.write_bytes(data)
    return data, "OCR Page 1 Extracted\nOCR Page 2 Extracted", 2


def _mock_ocr_mixed_3pages(source: Path, deriv: Path, max_pages: int = 3, pages: Any = None, timeout: float = 30) -> tuple[bytes, str, int]:
    import pymupdf
    doc = pymupdf.open()
    p1 = doc.new_page()
    p1.insert_text((50, 50), "Digital Page 1 Content")
    p2 = doc.new_page()
    p2.insert_text((50, 50), "OCR Result for Page 2")
    p3 = doc.new_page()
    p3.insert_text((50, 50), "Digital Page 3 Content")
    data = doc.tobytes()
    doc.close()
    deriv.parent.mkdir(parents=True, exist_ok=True)
    deriv.write_bytes(data)
    return data, "OCR Result for Page 2", 1


def _mock_ocr_mixed_budget_5pages(source: Path, deriv: Path, max_pages: int = 3, pages: Any = None, timeout: float = 30) -> tuple[bytes, str, int]:
    import pymupdf
    doc = pymupdf.open()
    p1 = doc.new_page()
    p1.insert_text((50, 50), "Native intro")
    p2 = doc.new_page()
    p2.insert_text((50, 50), "OCR Img 1")
    p3 = doc.new_page()
    p3.insert_text((50, 50), "OCR Img 2")
    p4 = doc.new_page()
    p4.insert_text((50, 50), "OCR Img 3")
    p5 = doc.new_page()
    data = doc.tobytes()
    doc.close()
    deriv.parent.mkdir(parents=True, exist_ok=True)
    deriv.write_bytes(data)
    return data, "OCR Img 1\nOCR Img 2\nOCR Img 3", 3


def _mock_ocr_mixed_p3(source: Path, deriv: Path, max_pages: int = 3, pages: Any = None, timeout: float = 30) -> tuple[bytes, str, int]:
    import pymupdf
    doc = pymupdf.open()
    p1 = doc.new_page()
    p1.insert_text((50, 50), "Text Page 1")
    p2 = doc.new_page()
    p2.insert_text((50, 50), "Text Page 2")
    p3 = doc.new_page()
    p3.insert_text((50, 50), "OCR Extracted Content for Page 3")
    p4 = doc.new_page()
    p4.insert_text((50, 50), "Text Page 4")
    data = doc.tobytes()
    doc.close()
    deriv.parent.mkdir(parents=True, exist_ok=True)
    deriv.write_bytes(data)
    return data, "OCR Extracted Content for Page 3", 1


def _mock_ocr_slow(source: Path, deriv: Path, max_pages: int = 3, pages: Any = None, timeout: float = 30) -> tuple[bytes, str, int]:
    deriv.parent.mkdir(parents=True, exist_ok=True)
    deriv.write_bytes(b"Partial incomplete bytes")
    time.sleep(1.0)
    return b"", "", 0


def _mock_ocr_crashing(source: Path, deriv: Path, max_pages: int = 3, pages: Any = None, timeout: float = 30) -> tuple[bytes, str, int]:
    deriv.parent.mkdir(parents=True, exist_ok=True)
    deriv.write_bytes(b"Partial crash data")
    raise RuntimeError("OCR process crashed!")


def _mock_ocr_failing(source: Path, deriv: Path, max_pages: int = 3, pages: Any = None, timeout: float = 30) -> tuple[bytes, str, int]:
    raise RuntimeError("OCR binary not available")


_DETERMINISTIC_SAMPLE_PDF = (
    b"%PDF-1.4\n%Deterministic mock derivative\n"
    b"1 0 obj\n<< /Type /Catalog /Pages 2 0 R >>\nendobj\n"
    b"2 0 obj\n<< /Type /Pages /Kids [3 0 R] /Count 1 >>\nendobj\n"
    b"3 0 obj\n<< /Type /Page /Parent 2 0 R /Contents 4 0 R >>\nendobj\n"
    b"4 0 obj\n<< /Length 20 >>\nstream\nBT /F1 12 Tf ET\nendstream\nendobj\n"
    b"xref\n0 5\n0000000000 65535 f\n0000000039 00000 n\n0000000088 00000 n\n0000000145 00000 n\n0000000212 00000 n\n"
    b"trailer\n<< /Size 5 /Root 1 0 R >>\nstartxref\n281\n%%EOF\n"
)

def _mock_ocr_tampering(source: Path, deriv: Path, max_pages: int = 3, pages: Any = None, timeout: float = 30) -> tuple[bytes, str, int]:
    source.write_bytes(b"MUTATED SOURCE FILE")
    deriv.parent.mkdir(parents=True, exist_ok=True)
    deriv.write_bytes(_DETERMINISTIC_SAMPLE_PDF)
    return _DETERMINISTIC_SAMPLE_PDF, "OCR Previous Run", 1


def _mock_ocr_new_content(source: Path, deriv: Path, max_pages: int = 3, pages: Any = None, timeout: float = 30) -> tuple[bytes, str, int]:
    import pymupdf
    doc = pymupdf.open()
    p = doc.new_page()
    p.insert_text((50, 50), "OCR New Content")
    data = doc.tobytes()
    doc.close()
    deriv.parent.mkdir(parents=True, exist_ok=True)
    deriv.write_bytes(data)
    return data, "OCR New Content", 1


def _mock_ocr_same_content(source: Path, deriv: Path, max_pages: int = 3, pages: Any = None, timeout: float = 30) -> tuple[bytes, str, int]:
    deriv.parent.mkdir(parents=True, exist_ok=True)
    deriv.write_bytes(_DETERMINISTIC_SAMPLE_PDF)
    return _DETERMINISTIC_SAMPLE_PDF, "OCR Same Content", 1


def _flaky_lock_verifier(*args: Any, **kwargs: Any) -> None:
    marker = Path(tempfile.gettempdir()) / "flaky_lock_marker_mda3.txt"
    if not marker.exists():
        marker.write_text("1", encoding="utf-8")
        return None
    marker.unlink(missing_ok=True)
    raise afetch.WorkspaceLockError("Lock expired during long OCR execution!")


def _tree_spawning_writer_target(write_path_str: str, pid_path_str: str) -> str:
    grandchild_code = f"""
import time, os, sys
with open(r'{pid_path_str}', 'w') as f:
    f.write(str(os.getpid()))
time.sleep(3.0)
with open(r'{write_path_str}', 'w') as f:
    f.write('late write by grandchild')
"""
    proc = subprocess.Popen([sys.executable, "-c", grandchild_code])
    t0 = time.time()
    while time.time() - t0 < 3.0:
        if Path(pid_path_str).exists() and Path(pid_path_str).read_text().strip():
            break
        time.sleep(0.02)
    time.sleep(5.0)
    return "done"


def _partially_writing_hanging_ocr(source: Path, deriv: Path, max_pages: int = 3, pages: Any = None, timeout: float = 30) -> tuple[bytes, str, int]:
    deriv.parent.mkdir(parents=True, exist_ok=True)
    deriv.write_bytes(b"Partial derivative content that must be cleaned on timeout")
    time.sleep(5.0)
    return b"", "", 0


class MailDeskAttachmentsMDA3Tests(unittest.TestCase):
    def setUp(self) -> None:
        self.maxDiff = None
        self._himalaya_blocker = patch.object(
            himalaya,
            "run_himalaya",
            side_effect=RuntimeError("Real Himalaya process execution is forbidden in hermetic unit tests!"),
        )
        self._himalaya_blocker.start()

    def tearDown(self) -> None:
        self._himalaya_blocker.stop()

    def _create_sample_pdf(self, page_texts: list[str]) -> bytes:
        import pymupdf
        doc = pymupdf.open()
        for text in page_texts:
            page = doc.new_page()
            page.insert_text((50, 50), text)
        data = doc.tobytes()
        doc.close()
        return data

    def _create_image_only_pdf(self, num_pages: int = 2) -> bytes:
        import pymupdf
        doc = pymupdf.open()
        pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 50, 50), 1)
        pix.clear_with(255)
        for _ in range(num_pages):
            page = doc.new_page()
            page.insert_image(page.rect, pixmap=pix)
        data = doc.tobytes()
        doc.close()
        return data

    def _create_mixed_pdf(self, pages: list[tuple[str, str]]) -> bytes:
        """Create a mixed PDF where pages are ('text', content) or ('image', '')."""
        import pymupdf
        doc = pymupdf.open()
        pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 50, 50), 1)
        pix.clear_with(255)
        for ptype, content in pages:
            page = doc.new_page()
            if ptype == "text":
                page.insert_text((50, 50), content)
            else:
                page.insert_image(page.rect, pixmap=pix)
        data = doc.tobytes()
        doc.close()
        return data

    # --------------------------------------------------------------------------
    # 1. Digital PDF Extraction within Limits
    # --------------------------------------------------------------------------
    def test_extract_digital_pdf_within_limit(self) -> None:
        pdf_bytes = self._create_sample_pdf(["Page 1 content", "Page 2 content"])
        pdf_sha = hashlib.sha256(pdf_bytes).hexdigest()

        with tempfile.TemporaryDirectory() as tmp_dir:
            data_dir = Path(tmp_dir) / "data" / "mail-desk"
            run_dir = data_dir / "attachments" / "run_pdf_01"
            run_dir.mkdir(parents=True)
            pdf_file = run_dir / "doc.pdf"
            pdf_file.write_bytes(pdf_bytes)

            fetch_result = {
                "status": "fetched",
                "run_id": "run_pdf_01",
                "relative_path": "data/mail-desk/attachments/run_pdf_01/doc.pdf",
                "filename": "doc.pdf",
                "fetch_sha256": pdf_sha,
                "inventory_sha256": pdf_sha,
                "effective_mime_type": "application/pdf",
                "size_bytes": len(pdf_bytes),
            }

            res = aextract.extract_attachment_content(
                fetch_result,
                expected_sha256=pdf_sha,
                data_dir=data_dir,
            )

            self.assertEqual("extracted", res["status"])
            self.assertEqual(pdf_sha, res["source_sha256"])
            self.assertIsNone(res["derivative_sha256"])
            self.assertIsNone(res["derivative_relative_path"])
            self.assertEqual("native", res["method"])
            self.assertEqual("pymupdf", res["tool"])
            self.assertNotEqual("1.0", res["tool_version"])
            self.assertEqual("high", res["quality"])
            self.assertEqual(2, res["scope"]["pages_processed"])
            self.assertEqual(2, res["scope"]["pages_total"])
            self.assertEqual(0, res["scope"]["ocr_pages"])
            self.assertIsNone(res["truncation_reason"])
            self.assertIn("Page 1 content", res["text"])
            self.assertIn("Page 2 content", res["text"])

    # --------------------------------------------------------------------------
    # 2. Digital PDF Exceeding 10 Pages Truncated
    # --------------------------------------------------------------------------
    def test_extract_digital_pdf_exceeding_10_pages_truncated(self) -> None:
        pages = [f"Page {i} content text" for i in range(1, 16)]  # 15 pages
        pdf_bytes = self._create_sample_pdf(pages)
        pdf_sha = hashlib.sha256(pdf_bytes).hexdigest()

        with tempfile.TemporaryDirectory() as tmp_dir:
            data_dir = Path(tmp_dir) / "data" / "mail-desk"
            run_dir = data_dir / "attachments" / "run_pdf_long"
            run_dir.mkdir(parents=True)
            pdf_file = run_dir / "long.pdf"
            pdf_file.write_bytes(pdf_bytes)

            fetch_result = {
                "status": "fetched",
                "run_id": "run_pdf_long",
                "relative_path": "data/mail-desk/attachments/run_pdf_long/long.pdf",
                "filename": "long.pdf",
                "fetch_sha256": pdf_sha,
                "inventory_sha256": pdf_sha,
                "effective_mime_type": "application/pdf",
                "size_bytes": len(pdf_bytes),
            }

            res = aextract.extract_attachment_content(
                fetch_result,
                expected_sha256=pdf_sha,
                data_dir=data_dir,
            )

            self.assertEqual("extracted", res["status"])
            self.assertEqual(10, res["scope"]["pages_processed"])
            self.assertEqual(15, res["scope"]["pages_total"])
            self.assertEqual("max_pages_exceeded", res["truncation_reason"])
            self.assertIn("Page 1 content", res["text"])
            self.assertIn("Page 10 content", res["text"])
            self.assertNotIn("Page 11 content", res["text"])

    # --------------------------------------------------------------------------
    # 3. 15,000 Character Limit Enforced
    # --------------------------------------------------------------------------
    def test_character_budget_15000_chars_truncated(self) -> None:
        huge_text = "A" * 20000
        text_bytes = huge_text.encode("utf-8")
        text_sha = hashlib.sha256(text_bytes).hexdigest()

        with tempfile.TemporaryDirectory() as tmp_dir:
            data_dir = Path(tmp_dir) / "data" / "mail-desk"
            run_dir = data_dir / "attachments" / "run_txt_huge"
            run_dir.mkdir(parents=True)
            txt_file = run_dir / "huge.txt"
            txt_file.write_bytes(text_bytes)

            fetch_result = {
                "status": "fetched",
                "run_id": "run_txt_huge",
                "relative_path": "data/mail-desk/attachments/run_txt_huge/huge.txt",
                "filename": "huge.txt",
                "fetch_sha256": text_sha,
                "inventory_sha256": text_sha,
                "effective_mime_type": "text/plain",
                "size_bytes": len(text_bytes),
            }

            res = aextract.extract_attachment_content(
                fetch_result,
                expected_sha256=text_sha,
                data_dir=data_dir,
            )

            self.assertEqual("extracted", res["status"])
            self.assertEqual(15000, len(res["text"]))
            self.assertEqual(15000, res["character_count"])
            self.assertEqual("max_chars_exceeded", res["truncation_reason"])

    # --------------------------------------------------------------------------
    # 4. Plain Text and CSV Extraction
    # --------------------------------------------------------------------------
    def test_extract_plain_text_and_csv(self) -> None:
        csv_content = "id,name,role\n1,Alice,Admin\n2,Bob,User\n"
        csv_bytes = csv_content.encode("utf-8")
        csv_sha = hashlib.sha256(csv_bytes).hexdigest()

        with tempfile.TemporaryDirectory() as tmp_dir:
            data_dir = Path(tmp_dir) / "data" / "mail-desk"
            run_dir = data_dir / "attachments" / "run_csv"
            run_dir.mkdir(parents=True)
            csv_file = run_dir / "users.csv"
            csv_file.write_bytes(csv_bytes)

            fetch_result = {
                "status": "fetched",
                "run_id": "run_csv",
                "relative_path": "data/mail-desk/attachments/run_csv/users.csv",
                "filename": "users.csv",
                "fetch_sha256": csv_sha,
                "inventory_sha256": csv_sha,
                "effective_mime_type": "text/csv",
                "size_bytes": len(csv_bytes),
            }

            res = aextract.extract_attachment_content(
                fetch_result,
                expected_sha256=csv_sha,
                data_dir=data_dir,
            )

            self.assertEqual("extracted", res["status"])
            self.assertEqual("native", res["method"])
            self.assertEqual("built_in", res["tool"])
            self.assertIn("Alice", res["text"])
            self.assertIn("Bob", res["text"])
            self.assertIsNone(res["truncation_reason"])

    # --------------------------------------------------------------------------
    # 5. DOCX Paragraph Limit Enforced During Processing
    # --------------------------------------------------------------------------
    def test_extract_docx_within_and_exceeding_paragraph_limit(self) -> None:
        import docx
        doc = docx.Document()
        for i in range(1, 55):  # 54 paragraphs (> 40 limit)
            doc.add_paragraph(f"Paragraph {i} content.")

        with tempfile.TemporaryDirectory() as tmp_dir:
            docx_path = Path(tmp_dir) / "test.docx"
            doc.save(docx_path)
            docx_bytes = docx_path.read_bytes()
            docx_sha = hashlib.sha256(docx_bytes).hexdigest()

            data_dir = Path(tmp_dir) / "data" / "mail-desk"
            run_dir = data_dir / "attachments" / "run_docx"
            run_dir.mkdir(parents=True)
            target = run_dir / "test.docx"
            target.write_bytes(docx_bytes)

            fetch_result = {
                "status": "fetched",
                "run_id": "run_docx",
                "relative_path": "data/mail-desk/attachments/run_docx/test.docx",
                "filename": "test.docx",
                "fetch_sha256": docx_sha,
                "inventory_sha256": docx_sha,
                "effective_mime_type": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                "size_bytes": len(docx_bytes),
            }

            res = aextract.extract_attachment_content(
                fetch_result,
                expected_sha256=docx_sha,
                data_dir=data_dir,
            )
            self.assertEqual("extracted", res["status"])
            self.assertEqual(40, res["scope"]["paragraphs"])
            self.assertEqual("max_paragraphs_exceeded", res["truncation_reason"])
            self.assertIn("Paragraph 1 content.", res["text"])
            self.assertIn("Paragraph 40 content.", res["text"])
            self.assertNotIn("Paragraph 41 content.", res["text"])

    # --------------------------------------------------------------------------
    # 6. XLSX Sheet and Grid Limit Enforced During Processing
    # --------------------------------------------------------------------------
    def test_extract_xlsx_sheet_and_grid_limit(self) -> None:
        import openpyxl
        wb = openpyxl.Workbook()
        ws1 = wb.active
        ws1.title = "Sheet1"
        for r in range(1, 60):  # 59 rows (> 50 limit)
            for c in range(1, 15):  # 14 cols (> 10 limit)
                ws1.cell(row=r, column=c, value=f"R{r}C{c}")

        ws2 = wb.create_sheet(title="Sheet2")
        ws2.cell(row=1, column=1, value="S2Val")

        ws3 = wb.create_sheet(title="Sheet3")  # 3rd sheet (> 2 limit)
        ws3.cell(row=1, column=1, value="S3Val")

        with tempfile.TemporaryDirectory() as tmp_dir:
            xlsx_path = Path(tmp_dir) / "test.xlsx"
            wb.save(xlsx_path)
            xlsx_bytes = xlsx_path.read_bytes()
            xlsx_sha = hashlib.sha256(xlsx_bytes).hexdigest()

            data_dir = Path(tmp_dir) / "data" / "mail-desk"
            run_dir = data_dir / "attachments" / "run_xlsx"
            run_dir.mkdir(parents=True)
            target = run_dir / "test.xlsx"
            target.write_bytes(xlsx_bytes)

            fetch_result = {
                "status": "fetched",
                "run_id": "run_xlsx",
                "relative_path": "data/mail-desk/attachments/run_xlsx/test.xlsx",
                "filename": "test.xlsx",
                "fetch_sha256": xlsx_sha,
                "inventory_sha256": xlsx_sha,
                "effective_mime_type": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                "size_bytes": len(xlsx_bytes),
            }

            res = aextract.extract_attachment_content(
                fetch_result,
                expected_sha256=xlsx_sha,
                data_dir=data_dir,
            )
            self.assertEqual("extracted", res["status"])
            self.assertEqual(2, res["scope"]["sheets"])
            self.assertIn("grid_limit_exceeded", str(res["truncation_reason"]))
            self.assertIn("R1C1", res["text"])
            self.assertIn("R50C10", res["text"])
            self.assertNotIn("R51C1", res["text"])
            self.assertNotIn("S3Val", res["text"])

    # --------------------------------------------------------------------------
    # 7. PPTX Slide Limit Enforced During Processing
    # --------------------------------------------------------------------------
    def test_extract_pptx_slide_limit(self) -> None:
        import pptx
        prs = pptx.Presentation()
        for i in range(1, 20):  # 19 slides (> 15 limit)
            slide_layout = prs.slide_layouts[0]
            slide = prs.slides.add_slide(slide_layout)
            slide.shapes.title.text = f"Slide {i} Title"

        with tempfile.TemporaryDirectory() as tmp_dir:
            pptx_path = Path(tmp_dir) / "test.pptx"
            prs.save(pptx_path)
            pptx_bytes = pptx_path.read_bytes()
            pptx_sha = hashlib.sha256(pptx_bytes).hexdigest()

            data_dir = Path(tmp_dir) / "data" / "mail-desk"
            run_dir = data_dir / "attachments" / "run_pptx"
            run_dir.mkdir(parents=True)
            target = run_dir / "test.pptx"
            target.write_bytes(pptx_bytes)

            fetch_result = {
                "status": "fetched",
                "run_id": "run_pptx",
                "relative_path": "data/mail-desk/attachments/run_pptx/test.pptx",
                "filename": "test.pptx",
                "fetch_sha256": pptx_sha,
                "inventory_sha256": pptx_sha,
                "effective_mime_type": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
                "size_bytes": len(pptx_bytes),
            }

            res = aextract.extract_attachment_content(
                fetch_result,
                expected_sha256=pptx_sha,
                data_dir=data_dir,
            )
            self.assertEqual("extracted", res["status"])
            self.assertEqual(15, res["scope"]["slides"])
            self.assertEqual("max_slides_exceeded", res["truncation_reason"])
            self.assertIn("Slide 1 Title", res["text"])
            self.assertIn("Slide 15 Title", res["text"])
            self.assertNotIn("Slide 16 Title", res["text"])

    # --------------------------------------------------------------------------
    # 8. Pure Image PDF OCR Derivative Preserves Original Byte-Identical
    # --------------------------------------------------------------------------
    def test_extract_image_pdf_ocr_local_derivative_preserves_original(self) -> None:
        pdf_bytes = self._create_image_only_pdf(num_pages=2)
        original_sha = hashlib.sha256(pdf_bytes).hexdigest()

        with tempfile.TemporaryDirectory() as tmp_dir:
            data_dir = Path(tmp_dir) / "data" / "mail-desk"
            run_dir = data_dir / "attachments" / "run_ocr_01"
            run_dir.mkdir(parents=True)
            pdf_file = run_dir / "scanned.pdf"
            pdf_file.write_bytes(pdf_bytes)

            fetch_result = {
                "status": "fetched",
                "run_id": "run_ocr_01",
                "relative_path": "data/mail-desk/attachments/run_ocr_01/scanned.pdf",
                "filename": "scanned.pdf",
                "fetch_sha256": original_sha,
                "inventory_sha256": original_sha,
                "effective_mime_type": "application/pdf",
                "size_bytes": len(pdf_bytes),
            }

            res = aextract.extract_attachment_content(
                fetch_result,
                expected_sha256=original_sha,
                data_dir=data_dir,
                allow_legacy=True,
                _ocr_runner=_mock_ocr_pdf_2pages,
            )

            self.assertEqual("extracted", res["status"])
            self.assertEqual("ocr_local_derivative", res["method"])
            self.assertEqual("ocrmypdf", res["tool"])
            self.assertEqual(original_sha, res["source_sha256"])
            self.assertIsNotNone(res["derivative_sha256"])
            self.assertNotEqual(original_sha, res["derivative_sha256"])
            self.assertEqual(
                "data/mail-desk/attachments/run_ocr_01/derivatives/scanned.ocr.pdf",
                res["derivative_relative_path"],
            )

            # CRITICAL: Original quarantine file was NEVER modified!
            self.assertEqual(pdf_bytes, pdf_file.read_bytes())
            self.assertEqual(original_sha, hashlib.sha256(pdf_file.read_bytes()).hexdigest())

            # Derivative file exists in derivatives folder
            deriv_file = run_dir / "derivatives" / "scanned.ocr.pdf"
            self.assertTrue(deriv_file.exists())
            self.assertIn("OCR Page 1 Extracted", res["text"])

    # --------------------------------------------------------------------------
    # 9. Mixed PDF (Digital + Image Pages) OCR Processing
    # --------------------------------------------------------------------------
    def test_extract_mixed_pdf_extracts_digital_and_ocrs_image_pages(self) -> None:
        mixed_bytes = self._create_mixed_pdf([
            ("text", "Digital Page 1 Content"),
            ("image", ""),
            ("text", "Digital Page 3 Content"),
        ])
        original_sha = hashlib.sha256(mixed_bytes).hexdigest()

        with tempfile.TemporaryDirectory() as tmp_dir:
            data_dir = Path(tmp_dir) / "data" / "mail-desk"
            run_dir = data_dir / "attachments" / "run_mixed_01"
            run_dir.mkdir(parents=True)
            pdf_file = run_dir / "mixed.pdf"
            pdf_file.write_bytes(mixed_bytes)

            fetch_result = {
                "status": "fetched",
                "run_id": "run_mixed_01",
                "relative_path": "data/mail-desk/attachments/run_mixed_01/mixed.pdf",
                "filename": "mixed.pdf",
                "fetch_sha256": original_sha,
                "inventory_sha256": original_sha,
                "effective_mime_type": "application/pdf",
                "size_bytes": len(mixed_bytes),
            }

            res = aextract.extract_attachment_content(
                fetch_result,
                expected_sha256=original_sha,
                data_dir=data_dir,
                allow_legacy=True,
                _ocr_runner=_mock_ocr_mixed_3pages,
            )

            self.assertEqual("extracted", res["status"])
            self.assertEqual("mixed_native_ocr", res["method"])
            self.assertEqual("mixed", res["quality"])
            self.assertEqual(1, res["scope"]["ocr_pages"])
            self.assertIn("Digital Page 1 Content", res["text"])
            self.assertIn("OCR Result for Page 2", res["text"])
            self.assertIn("Digital Page 3 Content", res["text"])
            self.assertIsNotNone(res["derivative_relative_path"])
            self.assertIsNotNone(res["derivative_sha256"])

            # Verify original quarantine file remains pristine
            self.assertEqual(mixed_bytes, pdf_file.read_bytes())

    # --------------------------------------------------------------------------
    # 10. Mixed PDF Exceeding 3 OCR Pages Explicitly Truncated
    # --------------------------------------------------------------------------
    def test_extract_mixed_pdf_exceeding_ocr_budget_marked_truncated(self) -> None:
        mixed_bytes = self._create_mixed_pdf([
            ("text", "Native intro"),
            ("image", ""),
            ("image", ""),
            ("image", ""),
            ("image", ""),  # 4th image page exceeds budget of 3
        ])
        original_sha = hashlib.sha256(mixed_bytes).hexdigest()

        with tempfile.TemporaryDirectory() as tmp_dir:
            data_dir = Path(tmp_dir) / "data" / "mail-desk"
            run_dir = data_dir / "attachments" / "run_mixed_budget"
            run_dir.mkdir(parents=True)
            pdf_file = run_dir / "mixed_budget.pdf"
            pdf_file.write_bytes(mixed_bytes)

            fetch_result = {
                "status": "fetched",
                "run_id": "run_mixed_budget",
                "relative_path": "data/mail-desk/attachments/run_mixed_budget/mixed_budget.pdf",
                "filename": "mixed_budget.pdf",
                "fetch_sha256": original_sha,
                "inventory_sha256": original_sha,
                "effective_mime_type": "application/pdf",
                "size_bytes": len(mixed_bytes),
            }

            res = aextract.extract_attachment_content(
                fetch_result,
                expected_sha256=original_sha,
                data_dir=data_dir,
                allow_legacy=True,
                _ocr_runner=_mock_ocr_mixed_budget_5pages,
            )

            self.assertEqual("extracted", res["status"])
            self.assertEqual("ocr_page_limit_exceeded", res["truncation_reason"])
            self.assertEqual(3, res["scope"]["ocr_pages"])
            self.assertIn("Native intro", res["text"])
            self.assertIn("skipped - OCR page limit of 3 exceeded", res["text"])

    # --------------------------------------------------------------------------
    # 11. Adversarial: Path Escapes & Traversal Rejected Fail-Closed
    # --------------------------------------------------------------------------
    def test_path_escapes_and_traversal_rejected_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            data_dir = Path(tmp_dir) / "data" / "mail-desk"
            run_dir = data_dir / "attachments" / "run_esc"
            run_dir.mkdir(parents=True)
            valid_sha = "a" * 64

            # Traversal attempt with ../
            fetch_traversal = {
                "status": "fetched",
                "run_id": "run_esc",
                "relative_path": "../../etc/passwd",
                "filename": "passwd",
                "fetch_sha256": valid_sha,
                "inventory_sha256": valid_sha,
                "effective_mime_type": "text/plain",
            }
            with self.assertRaises(ValueError):
                aextract.extract_attachment_content(fetch_traversal, expected_sha256=valid_sha, data_dir=data_dir)

            # Absolute path attempt
            fetch_abs = {
                "status": "fetched",
                "run_id": "run_esc",
                "relative_path": "C:/Windows/System32/drivers/etc/hosts",
                "filename": "hosts",
                "fetch_sha256": valid_sha,
                "inventory_sha256": valid_sha,
                "effective_mime_type": "text/plain",
            }
            with self.assertRaises(ValueError):
                aextract.extract_attachment_content(fetch_abs, expected_sha256=valid_sha, data_dir=data_dir)

            # Run directory mismatch
            fetch_run_mismatch = {
                "status": "fetched",
                "run_id": "run_esc",
                "relative_path": "data/mail-desk/attachments/other_run/doc.pdf",
                "filename": "doc.pdf",
                "fetch_sha256": valid_sha,
                "inventory_sha256": valid_sha,
                "effective_mime_type": "application/pdf",
            }
            with self.assertRaises(ValueError):
                aextract.extract_attachment_content(fetch_run_mismatch, expected_sha256=valid_sha, data_dir=data_dir)

            # Subdirectory attempt
            fetch_subdir = {
                "status": "fetched",
                "run_id": "run_esc",
                "relative_path": "data/mail-desk/attachments/run_esc/subfolder/doc.pdf",
                "filename": "doc.pdf",
                "fetch_sha256": valid_sha,
                "inventory_sha256": valid_sha,
                "effective_mime_type": "application/pdf",
            }
            with self.assertRaises(ValueError):
                aextract.extract_attachment_content(fetch_subdir, expected_sha256=valid_sha, data_dir=data_dir)

    # --------------------------------------------------------------------------
    # 12. Adversarial: Invalid or Unsafe MD-A2 run_id Rejected
    # --------------------------------------------------------------------------
    def test_unsafe_run_id_rejected_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            data_dir = Path(tmp_dir) / "data" / "mail-desk"
            valid_sha = "b" * 64

            for bad_run_id in ["../escape", "CON", "NUL", "run/slash", "", " "]:
                fetch_bad_run = {
                    "status": "fetched",
                    "run_id": bad_run_id,
                    "relative_path": f"data/mail-desk/attachments/{bad_run_id}/doc.pdf",
                    "filename": "doc.pdf",
                    "fetch_sha256": valid_sha,
                    "inventory_sha256": valid_sha,
                    "effective_mime_type": "application/pdf",
                }
                with self.assertRaises(ValueError):
                    aextract.extract_attachment_content(fetch_bad_run, expected_sha256=valid_sha, data_dir=data_dir)

    # --------------------------------------------------------------------------
    # 13. Adversarial: MD-A2 Envelope Integrity, Mandatory Hash, & Drift Guards
    # --------------------------------------------------------------------------
    def test_mda2_envelope_integrity_and_hash_drift_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            data_dir = Path(tmp_dir) / "data" / "mail-desk"
            run_dir = data_dir / "attachments" / "run_drift"
            run_dir.mkdir(parents=True)
            doc_path = run_dir / "doc.txt"
            doc_bytes = b"Disk content on quarantine"
            doc_path.write_bytes(doc_bytes)
            disk_sha = hashlib.sha256(doc_bytes).hexdigest()

            # 13a. Missing or empty expected_sha256 must fail closed
            valid_envelope = {
                "status": "fetched",
                "run_id": "run_drift",
                "relative_path": "data/mail-desk/attachments/run_drift/doc.txt",
                "filename": "doc.txt",
                "fetch_sha256": disk_sha,
                "inventory_sha256": disk_sha,
                "effective_mime_type": "text/plain",
            }
            with self.assertRaises(ValueError):
                aextract.extract_attachment_content(valid_envelope, expected_sha256="", data_dir=data_dir)

            # 13b. Unapproved / blocked fetch status
            bad_status = {
                "status": "blocked_by_policy",
                "run_id": "run_drift",
                "relative_path": "data/mail-desk/attachments/run_drift/doc.txt",
                "filename": "doc.txt",
                "fetch_sha256": disk_sha,
                "inventory_sha256": disk_sha,
                "effective_mime_type": "text/plain",
            }
            with self.assertRaises(ValueError):
                aextract.extract_attachment_content(bad_status, expected_sha256=disk_sha, data_dir=data_dir)

            # 13c. Missing inventory_sha256
            missing_inv = {
                "status": "fetched",
                "run_id": "run_drift",
                "relative_path": "data/mail-desk/attachments/run_drift/doc.txt",
                "filename": "doc.txt",
                "fetch_sha256": disk_sha,
                "effective_mime_type": "text/plain",
            }
            with self.assertRaises(ValueError):
                aextract.extract_attachment_content(missing_inv, expected_sha256=disk_sha, data_dir=data_dir)

            # 13d. Drift between fetch_sha256 and inventory_sha256
            drift_envelope = {
                "status": "fetched",
                "run_id": "run_drift",
                "relative_path": "data/mail-desk/attachments/run_drift/doc.txt",
                "filename": "doc.txt",
                "fetch_sha256": disk_sha,
                "inventory_sha256": "f" * 64,
                "effective_mime_type": "text/plain",
            }
            with self.assertRaises(attachments.HashDriftError):
                aextract.extract_attachment_content(drift_envelope, expected_sha256=disk_sha, data_dir=data_dir)

            # 13e. Drift between fetch_sha256 and expected_sha256 parameter
            with self.assertRaises(attachments.HashDriftError):
                aextract.extract_attachment_content(
                    valid_envelope,
                    expected_sha256="0" * 64,
                    data_dir=data_dir,
                )

            # 13f. Drift between envelope and disk content
            fake_disk_envelope = {
                "status": "fetched",
                "run_id": "run_drift",
                "relative_path": "data/mail-desk/attachments/run_drift/doc.txt",
                "filename": "doc.txt",
                "fetch_sha256": "1" * 64,
                "inventory_sha256": "1" * 64,
                "effective_mime_type": "text/plain",
            }
            with self.assertRaises(attachments.HashDriftError):
                aextract.extract_attachment_content(fake_disk_envelope, expected_sha256="1" * 64, data_dir=data_dir)

    # --------------------------------------------------------------------------
    # 14. Adversarial: Macro Formats (.docm, .xlsm, .pptm, .ps1) Rejected Fail-Closed
    # --------------------------------------------------------------------------
    def test_macro_formats_and_active_content_rejected_before_parsers(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            data_dir = Path(tmp_dir) / "data" / "mail-desk"
            run_dir = data_dir / "attachments" / "run_macro"
            run_dir.mkdir(parents=True)

            # Disallowed extensions: .docm, .xlsm, .pptm, .ps1
            for macro_ext in [".docm", ".xlsm", ".pptm", ".ps1"]:
                f_path = run_dir / f"payload{macro_ext}"
                f_bytes = b"macro code binary"
                f_path.write_bytes(f_bytes)
                f_sha = hashlib.sha256(f_bytes).hexdigest()

                fetch_res = {
                    "status": "fetched",
                    "run_id": "run_macro",
                    "relative_path": f"data/mail-desk/attachments/run_macro/payload{macro_ext}",
                    "filename": f"payload{macro_ext}",
                    "fetch_sha256": f_sha,
                    "inventory_sha256": f_sha,
                    "effective_mime_type": "application/octet-stream",
                }

                with self.assertRaises(afetch.DisallowedExtensionError):
                    aextract.extract_attachment_content(fetch_res, expected_sha256=f_sha, data_dir=data_dir)

            # Disguised .docx containing vbaProject.bin
            docx_fake_buf = io.BytesIO()
            with zipfile.ZipFile(docx_fake_buf, "w") as zf:
                zf.writestr("[Content_Types].xml", b"<Types></Types>")
                zf.writestr("word/document.xml", b"<xml></xml>")
                zf.writestr("word/vbaProject.bin", b"VBA MACRO CODE HERE")
            fake_docx_bytes = docx_fake_buf.getvalue()
            fake_sha = hashlib.sha256(fake_docx_bytes).hexdigest()
            fake_path = run_dir / "invoice.docx"
            fake_path.write_bytes(fake_docx_bytes)

            fetch_fake_docx = {
                "status": "fetched",
                "run_id": "run_macro",
                "relative_path": "data/mail-desk/attachments/run_macro/invoice.docx",
                "filename": "invoice.docx",
                "fetch_sha256": fake_sha,
                "inventory_sha256": fake_sha,
                "effective_mime_type": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            }
            with self.assertRaises(afetch.ActiveContentBlockedError):
                aextract.extract_attachment_content(fetch_fake_docx, expected_sha256=fake_sha, data_dir=data_dir)

    # --------------------------------------------------------------------------
    # 15. Adversarial: Real Process Worker Termination & Late Write Prevention
    # --------------------------------------------------------------------------
    def test_process_worker_terminates_and_prevents_late_write(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            late_file = Path(tmp_dir) / "late_write.txt"
            t0 = time.time()
            with self.assertRaises(TimeoutError):
                aextract.run_with_timeout(
                    _late_writer_target,
                    args=(str(late_file),),
                    timeout_seconds=0.1,
                )
            elapsed = time.time() - t0
            self.assertLess(elapsed, 1.0)

            # Wait beyond the 1.2s child sleep duration to prove worker process was terminated
            time.sleep(1.3)
            self.assertFalse(late_file.exists(), "Worker process was not terminated: late write occurred!")

    # --------------------------------------------------------------------------
    # 16. Adversarial: Real Extraction Process Timeout Enforcement
    # --------------------------------------------------------------------------
    def test_process_timeout_enforced_and_aborts_cleanly(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            data_dir = Path(tmp_dir) / "data" / "mail-desk"
            run_dir = data_dir / "attachments" / "run_timeout"
            run_dir.mkdir(parents=True)
            doc_path = run_dir / "slow.txt"
            doc_bytes = b"Slow text"
            doc_path.write_bytes(doc_bytes)
            doc_sha = hashlib.sha256(doc_bytes).hexdigest()

            fetch_res = {
                "status": "fetched",
                "run_id": "run_timeout",
                "relative_path": "data/mail-desk/attachments/run_timeout/slow.txt",
                "filename": "slow.txt",
                "fetch_sha256": doc_sha,
                "inventory_sha256": doc_sha,
                "effective_mime_type": "text/plain",
            }

            policy_short = {
                "extraction": {"process_timeout_seconds": 0.0001}
            }

            t0 = time.time()
            res = aextract.extract_attachment_content(
                fetch_res,
                expected_sha256=doc_sha,
                data_dir=data_dir,
                policy=policy_short,
            )
            elapsed = time.time() - t0

            self.assertLess(elapsed, 1.0)
            self.assertEqual("extraction_failed", res["status"])
            self.assertEqual("timeout_exceeded", res["truncation_reason"])
            self.assertIn("timed out after 0.0001s", res["error"])

    # --------------------------------------------------------------------------
    # 17. Adversarial: Real OCR Timeout Enforcement & Cleanup
    # --------------------------------------------------------------------------
    def test_ocr_timeout_enforced_and_cleans_temporary_artifacts(self) -> None:
        pdf_bytes = self._create_image_only_pdf(num_pages=2)
        pdf_sha = hashlib.sha256(pdf_bytes).hexdigest()

        with tempfile.TemporaryDirectory() as tmp_dir:
            data_dir = Path(tmp_dir) / "data" / "mail-desk"
            run_dir = data_dir / "attachments" / "run_ocr_timeout"
            run_dir.mkdir(parents=True)
            pdf_path = run_dir / "scanned.pdf"
            pdf_path.write_bytes(pdf_bytes)

            fetch_res = {
                "status": "fetched",
                "run_id": "run_ocr_timeout",
                "relative_path": "data/mail-desk/attachments/run_ocr_timeout/scanned.pdf",
                "filename": "scanned.pdf",
                "fetch_sha256": pdf_sha,
                "inventory_sha256": pdf_sha,
                "effective_mime_type": "application/pdf",
            }

            policy_ocr_short = {
                "extraction": {"ocr_timeout_seconds": 0.05}
            }

            t0 = time.time()
            res = aextract.extract_attachment_content(
                fetch_res,
                expected_sha256=pdf_sha,
                data_dir=data_dir,
                policy=policy_ocr_short,
                allow_legacy=True,
                _ocr_runner=_mock_ocr_slow,
            )
            elapsed = time.time() - t0

            self.assertLess(elapsed, 2.5)
            self.assertEqual("attachment_conversion_unavailable", res["status"])
            self.assertIsNone(res["derivative_sha256"])
            self.assertIsNone(res["derivative_relative_path"])

            # Verify no partial files exist in derivatives folder
            deriv_dir = run_dir / "derivatives"
            if deriv_dir.exists():
                items = list(deriv_dir.iterdir())
                self.assertEqual([], items)

            # Original quarantine file unmutated
            self.assertEqual(pdf_bytes, pdf_path.read_bytes())

    # --------------------------------------------------------------------------
    # 18. Adversarial: Workspace Lock Missing / Unowned for OCR Fails Closed
    # --------------------------------------------------------------------------
    def test_workspace_lock_missing_fails_closed_zero_mutation(self) -> None:
        pdf_bytes = self._create_image_only_pdf(num_pages=2)
        pdf_sha = hashlib.sha256(pdf_bytes).hexdigest()

        with tempfile.TemporaryDirectory() as tmp_dir:
            data_dir = Path(tmp_dir) / "data" / "mail-desk"
            run_dir = data_dir / "attachments" / "run_lock"
            run_dir.mkdir(parents=True)
            pdf_path = run_dir / "scanned.pdf"
            pdf_path.write_bytes(pdf_bytes)

            fetch_res = {
                "status": "fetched",
                "run_id": "run_lock",
                "relative_path": "data/mail-desk/attachments/run_lock/scanned.pdf",
                "filename": "scanned.pdf",
                "fetch_sha256": pdf_sha,
                "inventory_sha256": pdf_sha,
                "effective_mime_type": "application/pdf",
            }

            with self.assertRaises(afetch.WorkspaceLockError):
                aextract.extract_attachment_content(
                    fetch_res,
                    expected_sha256=pdf_sha,
                    data_dir=data_dir,
                    allow_legacy=False,
                )

            # Zero derivatives created
            deriv_dir = run_dir / "derivatives"
            self.assertFalse(deriv_dir.exists())

    # --------------------------------------------------------------------------
    # 19. Adversarial: Workspace Lock Loss Immediately Before Promotion Aborts
    # --------------------------------------------------------------------------
    def test_workspace_lock_loss_immediately_before_promotion_aborts(self) -> None:
        pdf_bytes = self._create_image_only_pdf(num_pages=2)
        pdf_sha = hashlib.sha256(pdf_bytes).hexdigest()

        with tempfile.TemporaryDirectory() as tmp_dir:
            data_dir = Path(tmp_dir) / "data" / "mail-desk"
            run_dir = data_dir / "attachments" / "run_lock_loss"
            run_dir.mkdir(parents=True)
            pdf_path = run_dir / "scanned.pdf"
            pdf_path.write_bytes(pdf_bytes)

            fetch_res = {
                "status": "fetched",
                "run_id": "run_lock_loss",
                "relative_path": "data/mail-desk/attachments/run_lock_loss/scanned.pdf",
                "filename": "scanned.pdf",
                "fetch_sha256": pdf_sha,
                "inventory_sha256": pdf_sha,
                "effective_mime_type": "application/pdf",
            }

            with self.assertRaises(afetch.WorkspaceLockError):
                aextract.extract_attachment_content(
                    fetch_res,
                    expected_sha256=pdf_sha,
                    data_dir=data_dir,
                    _ocr_runner=_mock_ocr_pdf_2pages,
                    _lock_verifier=_flaky_lock_verifier,
                )

            # Derivative file was NOT promoted
            deriv_file = run_dir / "derivatives" / "scanned.ocr.pdf"
            self.assertFalse(deriv_file.exists())
            # Temp file was cleaned up
            if (run_dir / "derivatives").exists():
                for item in (run_dir / "derivatives").iterdir():
                    self.assertFalse(item.name.endswith(".tmp"))

    # --------------------------------------------------------------------------
    # 20. Adversarial: Partial OCR Cleanup on Error & Immutability Check
    # --------------------------------------------------------------------------
    def test_partial_ocr_cleanup_on_error_and_source_immutability(self) -> None:
        pdf_bytes = self._create_image_only_pdf(num_pages=2)
        pdf_sha = hashlib.sha256(pdf_bytes).hexdigest()

        with tempfile.TemporaryDirectory() as tmp_dir:
            data_dir = Path(tmp_dir) / "data" / "mail-desk"
            run_dir = data_dir / "attachments" / "run_ocr_err"
            run_dir.mkdir(parents=True)
            pdf_path = run_dir / "scanned.pdf"
            pdf_path.write_bytes(pdf_bytes)

            fetch_res = {
                "status": "fetched",
                "run_id": "run_ocr_err",
                "relative_path": "data/mail-desk/attachments/run_ocr_err/scanned.pdf",
                "filename": "scanned.pdf",
                "fetch_sha256": pdf_sha,
                "inventory_sha256": pdf_sha,
                "effective_mime_type": "application/pdf",
            }

            res = aextract.extract_attachment_content(
                fetch_res,
                expected_sha256=pdf_sha,
                data_dir=data_dir,
                allow_legacy=True,
                _ocr_runner=_mock_ocr_crashing,
            )

            self.assertEqual("attachment_conversion_unavailable", res["status"])
            self.assertIn("OCR process crashed", res["error"])

            # Verify no leaked temporary files in derivatives
            deriv_dir = run_dir / "derivatives"
            if deriv_dir.exists():
                self.assertEqual([], list(deriv_dir.iterdir()))

            # Original remains completely unmutated
            self.assertEqual(pdf_bytes, pdf_path.read_bytes())

    # --------------------------------------------------------------------------
    # 21. Adversarial: Derivative Collision Race Fails Closed (No Clobber)
    # --------------------------------------------------------------------------
    def test_derivative_collision_race_fails_closed_no_clobber(self) -> None:
        pdf_bytes = self._create_image_only_pdf(num_pages=2)
        pdf_sha = hashlib.sha256(pdf_bytes).hexdigest()

        with tempfile.TemporaryDirectory() as tmp_dir:
            data_dir = Path(tmp_dir) / "data" / "mail-desk"
            run_dir = data_dir / "attachments" / "run_deriv_race"
            run_dir.mkdir(parents=True)
            pdf_path = run_dir / "scanned.pdf"
            pdf_path.write_bytes(pdf_bytes)

            deriv_dir = run_dir / "derivatives"
            deriv_dir.mkdir(parents=True)
            deriv_file = deriv_dir / "scanned.ocr.pdf"
            # Pre-existing file with different content
            colliding_bytes = b"%PDF-1.4 Existing alien file that should never be overwritten"
            deriv_file.write_bytes(colliding_bytes)

            fetch_res = {
                "status": "fetched",
                "run_id": "run_deriv_race",
                "relative_path": "data/mail-desk/attachments/run_deriv_race/scanned.pdf",
                "filename": "scanned.pdf",
                "fetch_sha256": pdf_sha,
                "inventory_sha256": pdf_sha,
                "effective_mime_type": "application/pdf",
            }

            with self.assertRaises(afetch.QuarantineCollisionError):
                aextract.extract_attachment_content(
                    fetch_res,
                    expected_sha256=pdf_sha,
                    data_dir=data_dir,
                    allow_legacy=True,
                    _ocr_runner=_mock_ocr_new_content,
                )

            # Pre-existing file MUST NOT be clobbered!
            self.assertEqual(colliding_bytes, deriv_file.read_bytes())

    # --------------------------------------------------------------------------
    # 22. Derivative Already Exists with Same Hash (Idempotent)
    # --------------------------------------------------------------------------
    def test_derivative_already_exists_same_hash_idempotent(self) -> None:
        pdf_bytes = self._create_image_only_pdf(num_pages=2)
        pdf_sha = hashlib.sha256(pdf_bytes).hexdigest()

        with tempfile.TemporaryDirectory() as tmp_dir:
            data_dir = Path(tmp_dir) / "data" / "mail-desk"
            run_dir = data_dir / "attachments" / "run_deriv_idemp"
            run_dir.mkdir(parents=True)
            pdf_path = run_dir / "scanned.pdf"
            pdf_path.write_bytes(pdf_bytes)

            deriv_dir = run_dir / "derivatives"
            deriv_dir.mkdir(parents=True)
            deriv_file = deriv_dir / "scanned.ocr.pdf"

            deriv_file.write_bytes(_DETERMINISTIC_SAMPLE_PDF)
            deriv_sha = hashlib.sha256(_DETERMINISTIC_SAMPLE_PDF).hexdigest()

            fetch_res = {
                "status": "fetched",
                "run_id": "run_deriv_idemp",
                "relative_path": "data/mail-desk/attachments/run_deriv_idemp/scanned.pdf",
                "filename": "scanned.pdf",
                "fetch_sha256": pdf_sha,
                "inventory_sha256": pdf_sha,
                "effective_mime_type": "application/pdf",
            }

            res = aextract.extract_attachment_content(
                fetch_res,
                expected_sha256=pdf_sha,
                data_dir=data_dir,
                allow_legacy=True,
                _ocr_runner=_mock_ocr_same_content,
            )

            self.assertEqual("extracted", res["status"])
            self.assertEqual(deriv_sha, res["derivative_sha256"])
            self.assertIn("OCR Same Content", res["text"])
            self.assertEqual(_DETERMINISTIC_SAMPLE_PDF, deriv_file.read_bytes())

    # --------------------------------------------------------------------------
    # 23. Adversarial: Existing Idempotent Derivative Preserved on Source Drift
    # --------------------------------------------------------------------------
    def test_existing_idempotent_derivative_preserved_on_source_drift(self) -> None:
        pdf_bytes = self._create_image_only_pdf(num_pages=2)
        pdf_sha = hashlib.sha256(pdf_bytes).hexdigest()

        with tempfile.TemporaryDirectory() as tmp_dir:
            data_dir = Path(tmp_dir) / "data" / "mail-desk"
            run_dir = data_dir / "attachments" / "run_drift_idemp"
            run_dir.mkdir(parents=True)
            pdf_path = run_dir / "scanned.pdf"
            pdf_path.write_bytes(pdf_bytes)

            deriv_dir = run_dir / "derivatives"
            deriv_dir.mkdir(parents=True)
            deriv_file = deriv_dir / "scanned.ocr.pdf"

            deriv_file.write_bytes(_DETERMINISTIC_SAMPLE_PDF)

            fetch_res = {
                "status": "fetched",
                "run_id": "run_drift_idemp",
                "relative_path": "data/mail-desk/attachments/run_drift_idemp/scanned.pdf",
                "filename": "scanned.pdf",
                "fetch_sha256": pdf_sha,
                "inventory_sha256": pdf_sha,
                "effective_mime_type": "application/pdf",
            }

            with self.assertRaises(RuntimeError) as ctx:
                aextract.extract_attachment_content(
                    fetch_res,
                    expected_sha256=pdf_sha,
                    data_dir=data_dir,
                    allow_legacy=True,
                    _ocr_runner=_mock_ocr_tampering,
                )
            self.assertIn("mutated during OCR", str(ctx.exception))

            # Existing idempotent derivative MUST NOT be deleted!
            self.assertTrue(deriv_file.exists())
            self.assertEqual(_DETERMINISTIC_SAMPLE_PDF, deriv_file.read_bytes())

    # --------------------------------------------------------------------------
    # 24. Adversarial: MIME Type Drift Against MD-A2 Envelope Fails Closed
    # --------------------------------------------------------------------------
    def test_mime_drift_against_mda2_envelope_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            data_dir = Path(tmp_dir) / "data" / "mail-desk"
            run_dir = data_dir / "attachments" / "run_mime_drift"
            run_dir.mkdir(parents=True)

            # File is text, but MD-A2 envelope claims it is application/pdf
            txt_bytes = b"Hello plain text file"
            txt_sha = hashlib.sha256(txt_bytes).hexdigest()
            txt_file = run_dir / "document.txt"
            txt_file.write_bytes(txt_bytes)

            fetch_drift = {
                "status": "fetched",
                "run_id": "run_mime_drift",
                "relative_path": "data/mail-desk/attachments/run_mime_drift/document.txt",
                "filename": "document.txt",
                "fetch_sha256": txt_sha,
                "inventory_sha256": txt_sha,
                "effective_mime_type": "application/pdf",  # Drift!
            }

            with self.assertRaises(afetch.MimeDriftError):
                aextract.extract_attachment_content(fetch_drift, expected_sha256=txt_sha, data_dir=data_dir)

    # --------------------------------------------------------------------------
    # 25. Adversarial: Attachments Root Junction / Symlink Fails Closed Before Resolve
    # --------------------------------------------------------------------------
    def test_attachments_root_junction_fails_closed_before_resolve(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            data_dir = Path(tmp_dir) / "data" / "mail-desk"
            run_dir = data_dir / "attachments" / "run_root_junc"
            run_dir.mkdir(parents=True)
            doc_file = run_dir / "doc.txt"
            doc_bytes = b"sample text"
            doc_sha = hashlib.sha256(doc_bytes).hexdigest()
            doc_file.write_bytes(doc_bytes)

            fetch_res = {
                "status": "fetched",
                "run_id": "run_root_junc",
                "relative_path": "data/mail-desk/attachments/run_root_junc/doc.txt",
                "filename": "doc.txt",
                "fetch_sha256": doc_sha,
                "inventory_sha256": doc_sha,
                "effective_mime_type": "text/plain",
            }

            # Simulate check_quarantine_path_security catching an unresolved root reparse point
            def _security_guard(target, root):
                if not root.is_absolute() or str(root).endswith("attachments"):
                    raise afetch.SymlinkEscapeError("Reparse point or junction detected at attachments root!")

            with patch.object(aextract, "check_quarantine_path_security", side_effect=_security_guard):
                with self.assertRaises(afetch.SymlinkEscapeError):
                    aextract.extract_attachment_content(fetch_res, expected_sha256=doc_sha, data_dir=data_dir)

    # --------------------------------------------------------------------------
    # 26. Mixed PDF with Image Page Not on Page 1
    # --------------------------------------------------------------------------
    def test_mixed_pdf_with_image_page_not_page_one(self) -> None:
        # Page 1: Text, Page 2: Text, Page 3: Image, Page 4: Text
        mixed_bytes = self._create_mixed_pdf([
            ("text", "Text Page 1"),
            ("text", "Text Page 2"),
            ("image", ""),
            ("text", "Text Page 4"),
        ])
        original_sha = hashlib.sha256(mixed_bytes).hexdigest()

        with tempfile.TemporaryDirectory() as tmp_dir:
            data_dir = Path(tmp_dir) / "data" / "mail-desk"
            run_dir = data_dir / "attachments" / "run_mixed_p3"
            run_dir.mkdir(parents=True)
            pdf_file = run_dir / "mixed_p3.pdf"
            pdf_file.write_bytes(mixed_bytes)

            fetch_result = {
                "status": "fetched",
                "run_id": "run_mixed_p3",
                "relative_path": "data/mail-desk/attachments/run_mixed_p3/mixed_p3.pdf",
                "filename": "mixed_p3.pdf",
                "fetch_sha256": original_sha,
                "inventory_sha256": original_sha,
                "effective_mime_type": "application/pdf",
                "size_bytes": len(mixed_bytes),
            }

            res = aextract.extract_attachment_content(
                fetch_result,
                expected_sha256=original_sha,
                data_dir=data_dir,
                allow_legacy=True,
                _ocr_runner=_mock_ocr_mixed_p3,
            )

            self.assertEqual("extracted", res["status"])
            self.assertEqual("mixed_native_ocr", res["method"])
            self.assertEqual(1, res["scope"]["ocr_pages"])
            self.assertEqual(4, res["scope"]["pages_processed"])

            # Verify all pages present in correct order
            self.assertIn("Text Page 1", res["text"])
            self.assertIn("Text Page 2", res["text"])
            self.assertIn("OCR Extracted Content for Page 3", res["text"])
            self.assertIn("Text Page 4", res["text"])

            pos1 = res["text"].find("Text Page 1")
            pos2 = res["text"].find("Text Page 2")
            pos3 = res["text"].find("OCR Extracted Content for Page 3")
            pos4 = res["text"].find("Text Page 4")
            self.assertTrue(pos1 < pos2 < pos3 < pos4, "Pages in mixed PDF are out of order!")

    # --------------------------------------------------------------------------
    # 27. Mixed PDF: OCR Tool Failure Preserves Native Digital Text
    # --------------------------------------------------------------------------
    def test_mixed_pdf_ocr_failure_preserves_native_text(self) -> None:
        mixed_bytes = self._create_mixed_pdf([
            ("text", "Native Header Text"),
            ("image", ""),
        ])
        original_sha = hashlib.sha256(mixed_bytes).hexdigest()

        with tempfile.TemporaryDirectory() as tmp_dir:
            data_dir = Path(tmp_dir) / "data" / "mail-desk"
            run_dir = data_dir / "attachments" / "run_mixed_fail"
            run_dir.mkdir(parents=True)
            pdf_file = run_dir / "mixed_fail.pdf"
            pdf_file.write_bytes(mixed_bytes)

            fetch_res = {
                "status": "fetched",
                "run_id": "run_mixed_fail",
                "relative_path": "data/mail-desk/attachments/run_mixed_fail/mixed_fail.pdf",
                "filename": "mixed_fail.pdf",
                "fetch_sha256": original_sha,
                "inventory_sha256": original_sha,
                "effective_mime_type": "application/pdf",
            }

            res = aextract.extract_attachment_content(
                fetch_res,
                expected_sha256=original_sha,
                data_dir=data_dir,
                allow_legacy=True,
                _ocr_runner=_mock_ocr_failing,
            )

            # Native digital text must still be returned!
            self.assertEqual("extracted", res["status"])
            self.assertEqual("partial", res["quality"])
            self.assertEqual("ocr_unavailable", res["truncation_reason"])
            self.assertIn("Native Header Text", res["text"])
            self.assertIn("OCR unavailable", res["text"])
            self.assertIsNone(res["derivative_relative_path"])
            self.assertIsNone(res["derivative_sha256"])

    # --------------------------------------------------------------------------
    # 28. Corrupt File Handled Gracefully
    # --------------------------------------------------------------------------
    def test_corrupt_file_handled_gracefully(self) -> None:
        corrupt_bytes = b"%PDF-1.4 header but corrupt truncated data \x00\xff\xfe"
        f_sha = hashlib.sha256(corrupt_bytes).hexdigest()

        with tempfile.TemporaryDirectory() as tmp_dir:
            data_dir = Path(tmp_dir) / "data" / "mail-desk"
            run_dir = data_dir / "attachments" / "run_corrupt"
            run_dir.mkdir(parents=True)
            f = run_dir / "corrupt.pdf"
            f.write_bytes(corrupt_bytes)

            fetch_result = {
                "status": "fetched",
                "run_id": "run_corrupt",
                "relative_path": "data/mail-desk/attachments/run_corrupt/corrupt.pdf",
                "filename": "corrupt.pdf",
                "fetch_sha256": f_sha,
                "inventory_sha256": f_sha,
                "effective_mime_type": "application/pdf",
            }

            res = aextract.extract_attachment_content(fetch_result, expected_sha256=f_sha, data_dir=data_dir)
            self.assertIn(res["status"], ["corrupt_attachment", "attachment_conversion_unavailable", "extraction_failed"])
            self.assertIsNotNone(res["error"])

    # --------------------------------------------------------------------------
    # 29. Missing Tool Returns attachment_conversion_unavailable
    # --------------------------------------------------------------------------
    def test_missing_tool_returns_attachment_conversion_unavailable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            data_dir = Path(tmp_dir) / "data" / "mail-desk"
            run_dir = data_dir / "attachments" / "run_notool"
            run_dir.mkdir(parents=True)
            f = run_dir / "document.doc"
            f.write_bytes(bytes([0xd0, 0xcf, 0x11, 0xe0, 0xa1, 0xb1, 0x1a, 0xe1]) + b"\x00" * 50)
            f_sha = hashlib.sha256(f.read_bytes()).hexdigest()

            fetch_result = {
                "status": "fetched",
                "run_id": "run_notool",
                "relative_path": "data/mail-desk/attachments/run_notool/document.doc",
                "filename": "document.doc",
                "fetch_sha256": f_sha,
                "inventory_sha256": f_sha,
                "effective_mime_type": "application/msword",
            }

            with patch.dict("sys.modules", {"markitdown": None}):
                res = aextract.extract_attachment_content(fetch_result, expected_sha256=f_sha, data_dir=data_dir)
            self.assertEqual("attachment_conversion_unavailable", res["status"])
            self.assertIn("No extraction converter available", str(res["error"]))
            self.assertEqual("", res["text"])


    # --------------------------------------------------------------------------
    # 30. Adversarial: Process Worker Tree Kill Terminates Descendants & Late Writes
    # --------------------------------------------------------------------------
    def test_process_worker_tree_kill_terminates_grandchild_process_and_prevents_late_write(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            late_file = Path(tmp_dir) / "grandchild_late_write.txt"
            pid_file = Path(tmp_dir) / "grandchild.pid"

            t0 = time.time()
            with self.assertRaises(TimeoutError):
                aextract.run_with_timeout(
                    _tree_spawning_writer_target,
                    args=(str(late_file), str(pid_file)),
                    timeout_seconds=0.8,
                    allow_thread_fallback=False,
                )
            elapsed = time.time() - t0
            self.assertLess(elapsed, 2.0)

            self.assertTrue(pid_file.exists(), "Grandchild process did not write PID file")
            grandchild_pid = int(pid_file.read_text().strip())

            # Verify grandchild is terminated immediately by Job Object
            time.sleep(0.2)
            self.assertFalse(
                aextract.is_process_alive(grandchild_pid),
                f"Grandchild process PID {grandchild_pid} was not terminated by tree kill!",
            )

            # Wait beyond grandchild 3.0s sleep to prove no late write occurs
            time.sleep(2.5)
            self.assertFalse(late_file.exists(), "Grandchild process executed late write!")

    # --------------------------------------------------------------------------
    # 31. Adversarial: Timeout Cleans Partially Written Temp Derivative (Zero Leakage)
    # --------------------------------------------------------------------------
    def test_timeout_cleans_partially_written_temp_derivative_zero_leakage(self) -> None:
        pdf_bytes = self._create_image_only_pdf(num_pages=2)
        pdf_sha = hashlib.sha256(pdf_bytes).hexdigest()

        with tempfile.TemporaryDirectory() as tmp_dir:
            data_dir = Path(tmp_dir) / "data" / "mail-desk"
            run_dir = data_dir / "attachments" / "run_leakage_timeout"
            run_dir.mkdir(parents=True)
            pdf_path = run_dir / "scanned.pdf"
            pdf_path.write_bytes(pdf_bytes)

            fetch_res = {
                "status": "fetched",
                "run_id": "run_leakage_timeout",
                "relative_path": "data/mail-desk/attachments/run_leakage_timeout/scanned.pdf",
                "filename": "scanned.pdf",
                "fetch_sha256": pdf_sha,
                "inventory_sha256": pdf_sha,
                "effective_mime_type": "application/pdf",
            }

            policy_short = {
                "extraction": {"process_timeout_seconds": 0.3}
            }

            res = aextract.extract_attachment_content(
                fetch_res,
                expected_sha256=pdf_sha,
                data_dir=data_dir,
                policy=policy_short,
                allow_legacy=True,
                _ocr_runner=_partially_writing_hanging_ocr,
            )

            self.assertEqual("extraction_failed", res["status"])
            self.assertEqual("timeout_exceeded", res["truncation_reason"])

            # Verify ZERO leakage of temp files in derivatives directory
            deriv_dir = run_dir / "derivatives"
            if deriv_dir.exists():
                tmp_files = [f for f in deriv_dir.iterdir() if f.name.endswith(".tmp")]
                self.assertEqual([], tmp_files, "Leaked temporary derivative files found!")
                self.assertFalse((deriv_dir / "scanned.ocr.pdf").exists())

            # Original quarantine source file is 100% pristine
            self.assertEqual(pdf_bytes, pdf_path.read_bytes())

    # --------------------------------------------------------------------------
    # 32. Adversarial: Job Object Confinement Failures Abort Fail-Closed Before Execution
    # --------------------------------------------------------------------------
    def test_job_confinement_failure_aborts_fail_closed_without_executing_payload(self) -> None:
        """Verify that under Windows, failure in Job Object creation, configuration, or
        process assignment fails closed with RuntimeError and terminates the worker BEFORE
        the worker ever executes its payload."""
        if sys.platform != "win32":
            self.skipTest("Windows Job Object confinement test is only applicable on Windows platform")

        with tempfile.TemporaryDirectory() as tmp_dir:
            # 1. CreateJobObjectW failure: returns 0 / NULL handle
            marker_1 = Path(tmp_dir) / "marker_create_fail.txt"
            with patch.object(aextract._kernel32, "CreateJobObjectW", return_value=0):
                with self.assertRaises(RuntimeError) as ctx:
                    aextract.run_with_timeout(
                        _marker_side_effect_target,
                        args=(str(marker_1),),
                        timeout_seconds=5.0,
                        allow_thread_fallback=False,
                    )
                self.assertIn("Windows Job Object confinement failed", str(ctx.exception))
                self.assertIn("CreateJobObjectW", str(ctx.exception))
            time.sleep(0.3)
            self.assertFalse(marker_1.exists(), "Worker executed payload despite CreateJobObjectW failure!")

            # 2. SetInformationJobObject failure: returns 0 / FALSE
            marker_2 = Path(tmp_dir) / "marker_set_info_fail.txt"
            with patch.object(aextract._kernel32, "SetInformationJobObject", return_value=0):
                with self.assertRaises(RuntimeError) as ctx:
                    aextract.run_with_timeout(
                        _marker_side_effect_target,
                        args=(str(marker_2),),
                        timeout_seconds=5.0,
                        allow_thread_fallback=False,
                    )
                self.assertIn("Windows Job Object confinement failed", str(ctx.exception))
                self.assertIn("SetInformationJobObject", str(ctx.exception))
            time.sleep(0.3)
            self.assertFalse(marker_2.exists(), "Worker executed payload despite SetInformationJobObject failure!")

            # 3. AssignProcessToJobObject failure: returns 0 / FALSE
            marker_3 = Path(tmp_dir) / "marker_assign_fail.txt"
            with patch.object(aextract._kernel32, "AssignProcessToJobObject", return_value=0):
                with self.assertRaises(RuntimeError) as ctx:
                    aextract.run_with_timeout(
                        _marker_side_effect_target,
                        args=(str(marker_3),),
                        timeout_seconds=5.0,
                        allow_thread_fallback=False,
                    )
                self.assertIn("Windows Job Object confinement failed", str(ctx.exception))
                self.assertIn("AssignProcessToJobObject", str(ctx.exception))
            time.sleep(0.3)
            self.assertFalse(marker_3.exists(), "Worker executed payload despite AssignProcessToJobObject failure!")

    # --------------------------------------------------------------------------
    # 33. Cleanup Removes Only Own Invocation Temp Path Preserving Sibling
    # --------------------------------------------------------------------------
    def test_cleanup_removes_only_own_invocation_temp_path_preserving_sibling(self) -> None:
        """Verify that _cleanup_temp_artifacts strictly and exclusively unlinks the
        parent-owned invocation-specific temp path, never globbing or removing sibling
        temp files belonging to concurrent invocations under the same file stem."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            data_dir = Path(tmp_dir) / "data" / "mail-desk"
            run_dir = data_dir / "attachments" / "run_sibling_cleanup"
            deriv_dir = run_dir / "derivatives"
            deriv_dir.mkdir(parents=True)

            file_stem = "contract"
            own_temp = deriv_dir / f".{file_stem}.ocr.inv_uuid_AAAA.tmp"
            sibling_temp_1 = deriv_dir / f".{file_stem}.ocr.inv_uuid_BBBB.tmp"
            sibling_temp_2 = deriv_dir / f".{file_stem}.ocr.inv_uuid_CCCC.tmp"

            own_temp.write_bytes(b"temp data of invocation A")
            sibling_temp_1.write_bytes(b"active temp data of invocation B")
            sibling_temp_2.write_bytes(b"active temp data of invocation C")

            # Direct unit test of _cleanup_temp_artifacts: only own_temp unlinked
            aextract._cleanup_temp_artifacts(own_temp, deriv_dir, file_stem)

            self.assertFalse(own_temp.exists(), "own_temp was not cleaned up!")
            self.assertTrue(sibling_temp_1.exists(), "sibling_temp_1 was unexpectedly deleted!")
            self.assertEqual(b"active temp data of invocation B", sibling_temp_1.read_bytes())
            self.assertTrue(sibling_temp_2.exists(), "sibling_temp_2 was unexpectedly deleted!")
            self.assertEqual(b"active temp data of invocation C", sibling_temp_2.read_bytes())

            # End-to-end integration: extract_attachment_content failure/timeout preserves sibling
            pdf_bytes = self._create_image_only_pdf(num_pages=2)
            pdf_sha = hashlib.sha256(pdf_bytes).hexdigest()
            pdf_path = run_dir / f"{file_stem}.pdf"
            pdf_path.write_bytes(pdf_bytes)

            fetch_res = {
                "status": "fetched",
                "run_id": "run_sibling_cleanup",
                "relative_path": f"data/mail-desk/attachments/run_sibling_cleanup/{file_stem}.pdf",
                "filename": f"{file_stem}.pdf",
                "fetch_sha256": pdf_sha,
                "inventory_sha256": pdf_sha,
                "effective_mime_type": "application/pdf",
            }

            # Call extract_attachment_content with crashing OCR runner
            res = aextract.extract_attachment_content(
                fetch_res,
                expected_sha256=pdf_sha,
                data_dir=data_dir,
                allow_legacy=True,
                _ocr_runner=_mock_ocr_crashing,
            )
            self.assertEqual("attachment_conversion_unavailable", res["status"])

            # Sibling temp files must STILL be completely intact and uncorrupted
            self.assertTrue(sibling_temp_1.exists(), "sibling_temp_1 was clobbered during extraction error cleanup!")
            self.assertEqual(b"active temp data of invocation B", sibling_temp_1.read_bytes())
            self.assertTrue(sibling_temp_2.exists(), "sibling_temp_2 was clobbered during extraction error cleanup!")
            self.assertEqual(b"active temp data of invocation C", sibling_temp_2.read_bytes())

    # --------------------------------------------------------------------------
    # 34. Adversarial: POSIX setpgid Failure & PGID Mismatch Abort Without Payload Execution
    # --------------------------------------------------------------------------
    def test_posix_confinement_setpgid_failure_and_pgid_mismatch_without_executing_payload(self) -> None:
        """Hermetic test verifying that on POSIX:
        1. Worker failing os.setpgid(0, 0) reports error and aborts WITHOUT executing payload.
        2. Parent detecting PGID mismatch (os.getpgid(proc.pid) != proc.pid) terminates worker
           strictly individually, NEVER kills unverified process group, and payload never runs.
        3. terminate_process_tree only issues killpg if process is confirmed its own group leader.
        """
        # Subtest 1: Worker setpgid failure aborts fail-closed before payload
        mock_child_conn = Mock()
        mock_payload_func = Mock()
        with patch("sys.platform", "linux"):
            with patch.object(os, "setpgid", side_effect=PermissionError("EPERM: setpgid forbidden"), create=True):
                aextract._process_worker_target(mock_child_conn, mock_payload_func, (), {})

        mock_payload_func.assert_not_called()
        self.assertTrue(mock_child_conn.send.called)
        sent_msg = mock_child_conn.send.call_args[0][0]
        self.assertTrue(str(sent_msg).startswith("ERROR:setpgid_failed"))
        self.assertNotIn("READY", str(sent_msg))

        # Subtest 2: Parent PGID mismatch terminates worker individually without killing group
        mock_proc = Mock()
        mock_proc.pid = 4321
        is_alive_state = [True]
        mock_proc.is_alive.side_effect = lambda: is_alive_state[0]

        def _kill_proc() -> None:
            is_alive_state[0] = False

        mock_proc.kill.side_effect = _kill_proc

        parent_conn = Mock()
        child_conn = Mock()
        parent_conn.poll.return_value = True
        parent_conn.recv.return_value = "READY"

        mock_killpg = Mock()

        with patch("sys.platform", "linux"):
            # Mock getpgid returning foreign PGID (mismatch)
            with patch.object(os, "getpgid", return_value=9999, create=True):
                with patch.object(os, "killpg", mock_killpg, create=True):
                    with patch("multiprocessing.Pipe", return_value=(parent_conn, child_conn)):
                        with patch("multiprocessing.Process", return_value=mock_proc):
                            with self.assertRaises(RuntimeError) as ctx:
                                aextract.run_with_timeout(
                                    aextract.is_process_alive,
                                    args=(1,),
                                    timeout_seconds=1.0,
                                    allow_thread_fallback=False,
                                )
                            self.assertIn("POSIX process group confinement failed", str(ctx.exception))
                            self.assertIn("Process group mismatch", str(ctx.exception))

        # Critical safety assertion: os.killpg MUST NEVER be called on an unverified group!
        mock_killpg.assert_not_called()
        # Worker process must be terminated individually
        mock_proc.kill.assert_called_once()
        # Abort signal sent to worker
        parent_conn.send.assert_called_once_with("ABORT")

        # Subtest 3: terminate_process_tree on POSIX only kills group if pgid == pid
        mock_foreign_proc = Mock()
        mock_foreign_proc.pid = 7777
        mock_foreign_proc.is_alive.return_value = False
        with patch("sys.platform", "linux"):
            with patch.object(os, "getpgid", return_value=8888, create=True):  # mismatch!
                with patch.object(os, "killpg", mock_killpg, create=True):
                    aextract.terminate_process_tree(mock_foreign_proc)
        mock_killpg.assert_not_called()

        mock_leader_proc = Mock()
        mock_leader_proc.pid = 7777
        mock_leader_proc.is_alive.return_value = False
        with patch("sys.platform", "linux"):
            with patch.object(os, "getpgid", return_value=7777, create=True):  # match!
                with patch.object(os, "killpg", mock_killpg, create=True):
                    aextract.terminate_process_tree(mock_leader_proc)
        mock_killpg.assert_called_once_with(7777, 9)

    # --------------------------------------------------------------------------
    # 35. Win64 Explicit ctypes Signatures & Pointer-Width Safety Test
    # --------------------------------------------------------------------------
    def test_win32_ctypes_signatures_64bit_pointer_width(self) -> None:
        """Verify that all Win32 Job Object and Process APIs possess complete, explicit
        ctypes.argtypes and restype declarations with pointer-wide HANDLE types."""
        from ctypes import wintypes
        import ctypes

        k32 = aextract._kernel32

        if k32 is not None:
            # 1. CreateJobObjectW
            self.assertEqual(k32.CreateJobObjectW.argtypes, [wintypes.LPVOID, wintypes.LPCWSTR])
            self.assertEqual(k32.CreateJobObjectW.restype, wintypes.HANDLE)

            # 2. SetInformationJobObject
            self.assertEqual(
                k32.SetInformationJobObject.argtypes,
                [wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD],
            )
            self.assertEqual(k32.SetInformationJobObject.restype, wintypes.BOOL)

            # 3. AssignProcessToJobObject
            self.assertEqual(k32.AssignProcessToJobObject.argtypes, [wintypes.HANDLE, wintypes.HANDLE])
            self.assertEqual(k32.AssignProcessToJobObject.restype, wintypes.BOOL)

            # 4. TerminateJobObject
            self.assertEqual(k32.TerminateJobObject.argtypes, [wintypes.HANDLE, wintypes.UINT])
            self.assertEqual(k32.TerminateJobObject.restype, wintypes.BOOL)

            # 5. OpenProcess
            self.assertEqual(k32.OpenProcess.argtypes, [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD])
            self.assertEqual(k32.OpenProcess.restype, wintypes.HANDLE)

            # 6. CloseHandle
            self.assertEqual(k32.CloseHandle.argtypes, [wintypes.HANDLE])
            self.assertEqual(k32.CloseHandle.restype, wintypes.BOOL)

            # 7. GetExitCodeProcess
            self.assertEqual(k32.GetExitCodeProcess.argtypes, [wintypes.HANDLE, wintypes.LPDWORD])
            self.assertEqual(k32.GetExitCodeProcess.restype, wintypes.BOOL)

        # 8. Pointer-width guarantee: wintypes.HANDLE is pointer-sized
        self.assertEqual(ctypes.sizeof(wintypes.HANDLE), ctypes.sizeof(ctypes.c_void_p))
        if sys.maxsize > 2**32:
            self.assertEqual(ctypes.sizeof(wintypes.HANDLE), 8)

        # 9. Live call test on Windows: handle return value is non-truncated
        if sys.platform == "win32" and k32 is not None:
            h_job = k32.CreateJobObjectW(None, None)
            self.assertIsNotNone(h_job)
            self.assertNotEqual(h_job, 0)
            closed = k32.CloseHandle(h_job)
            self.assertTrue(closed)

    # --------------------------------------------------------------------------
    # 36. Failed Win32 Signature Init Fails Closed Without Payload or Untyped API
    # --------------------------------------------------------------------------
    def test_failed_win32_signature_init_fails_closed_without_payload_or_untyped_api(self) -> None:
        """Verify that when Win32 kernel32 or signature initialization fails/is missing:
        1. WindowsJobObject and is_process_alive immediately fail-closed with RuntimeError.
        2. run_with_timeout terminates worker before execution; payload is never executed.
        3. Untyped ctypes.windll.kernel32 is NEVER accessed or used as an unconfigured fallback.
        """
        if sys.platform != "win32":
            self.skipTest("Win32 test only applicable on Windows platform")

        class UntypedWin32Sentinel:
            def __getattr__(self, name: str) -> Any:
                raise AssertionError(f"Untyped ctypes.windll.kernel32.{name} was accessed!")

        simulated_err = "Simulated kernel32 signature initialization failure"
        with tempfile.TemporaryDirectory() as tmp_dir:
            marker = Path(tmp_dir) / "marker_sig_fail.txt"
            with patch.object(aextract, "_kernel32", None):
                with patch.object(aextract, "_win32_init_error", simulated_err):
                    with patch("ctypes.windll.kernel32", UntypedWin32Sentinel()):
                        # 1. WindowsJobObject fails closed without accessing unconfigured kernel32
                        with self.assertRaises(RuntimeError) as ctx_job:
                            aextract.WindowsJobObject()
                        self.assertIn(simulated_err, str(ctx_job.exception))

                        # 2. is_process_alive fails closed without accessing unconfigured kernel32
                        with self.assertRaises(RuntimeError) as ctx_alive:
                            aextract.is_process_alive(12345)
                        self.assertIn(simulated_err, str(ctx_alive.exception))

                        # 3. run_with_timeout aborts and terminates worker before execution
                        with self.assertRaises(RuntimeError) as ctx_run:
                            aextract.run_with_timeout(
                                _marker_side_effect_target,
                                args=(str(marker),),
                                timeout_seconds=5.0,
                                allow_thread_fallback=False,
                            )
                        self.assertIn("Windows Job Object confinement failed", str(ctx_run.exception))
                        self.assertIn(simulated_err, str(ctx_run.exception))

            time.sleep(0.3)
            self.assertFalse(marker.exists(), "Worker executed payload despite failed signature initialization!")


if __name__ == "__main__":
    unittest.main()
