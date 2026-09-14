"""TDD tests for FR-08 MD-A3: Bounded attachment extraction and local OCR derivative contract."""

from __future__ import annotations

import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

MAIL_DESK_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(MAIL_DESK_ROOT / "scripts"))

from core import himalaya  # noqa: E402
from core import attachment_policy  # noqa: E402
from core import attachments  # noqa: E402
from core import attachment_extract as aextract  # noqa: E402
import mail_desk_himalaya_client as client  # noqa: E402


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
        return doc.tobytes()

    def _create_image_only_pdf(self, num_pages: int = 2) -> bytes:
        import pymupdf
        doc = pymupdf.open()
        # Create small 10x10 pixmap
        pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 50, 50), 1)
        pix.clear_with(255)
        for _ in range(num_pages):
            page = doc.new_page()
            page.insert_image(page.rect, pixmap=pix)
        return doc.tobytes()

    def test_extract_digital_pdf_within_limit(self) -> None:
        import hashlib
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
            self.assertIn(res["method"], ["native", "markitdown"])
            self.assertEqual("high", res["quality"])
            self.assertEqual(2, res["scope"]["pages_processed"])
            self.assertEqual(2, res["scope"]["pages_total"])
            self.assertIsNone(res["truncation_reason"])
            self.assertIn("Page 1 content", res["text"])
            self.assertIn("Page 2 content", res["text"])

    def test_extract_digital_pdf_exceeding_10_pages_truncated(self) -> None:
        import hashlib
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
            }

            res = aextract.extract_attachment_content(
                fetch_result,
                data_dir=data_dir,
            )

            self.assertEqual("extracted", res["status"])
            self.assertEqual(10, res["scope"]["pages_processed"])
            self.assertEqual(15, res["scope"]["pages_total"])
            self.assertEqual("max_pages_exceeded", res["truncation_reason"])
            self.assertIn("Page 1 content", res["text"])
            self.assertIn("Page 10 content", res["text"])
            self.assertNotIn("Page 11 content", res["text"])

    def test_character_budget_15000_chars_truncated(self) -> None:
        import hashlib
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
                "effective_mime_type": "text/plain",
            }

            res = aextract.extract_attachment_content(
                fetch_result,
                data_dir=data_dir,
            )

            self.assertEqual("extracted", res["status"])
            self.assertEqual(15000, len(res["text"]))
            self.assertEqual(15000, res["character_count"])
            self.assertEqual("max_chars_exceeded", res["truncation_reason"])

    def test_extract_plain_text_and_csv(self) -> None:
        import hashlib
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
                "effective_mime_type": "text/csv",
            }

            res = aextract.extract_attachment_content(
                fetch_result,
                data_dir=data_dir,
            )

            self.assertEqual("extracted", res["status"])
            self.assertEqual("native", res["method"])
            self.assertIn("Alice", res["text"])
            self.assertIn("Bob", res["text"])
            self.assertIsNone(res["truncation_reason"])

    def test_extract_docx_within_and_exceeding_paragraph_limit(self) -> None:
        import hashlib
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
                "effective_mime_type": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            }

            res = aextract.extract_attachment_content(fetch_result, data_dir=data_dir)
            self.assertEqual("extracted", res["status"])
            self.assertEqual(40, res["scope"]["paragraphs"])
            self.assertEqual("max_paragraphs_exceeded", res["truncation_reason"])
            self.assertIn("Paragraph 1 content.", res["text"])
            self.assertIn("Paragraph 40 content.", res["text"])
            self.assertNotIn("Paragraph 41 content.", res["text"])

    def test_extract_xlsx_sheet_and_grid_limit(self) -> None:
        import hashlib
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
                "effective_mime_type": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            }

            res = aextract.extract_attachment_content(fetch_result, data_dir=data_dir)
            self.assertEqual("extracted", res["status"])
            self.assertEqual(2, res["scope"]["sheets"])
            self.assertIn("grid_limit_exceeded", str(res["truncation_reason"]))
            self.assertIn("R1C1", res["text"])
            self.assertIn("R50C10", res["text"])
            self.assertNotIn("R51C1", res["text"])
            self.assertNotIn("S3Val", res["text"])

    def test_extract_pptx_slide_limit(self) -> None:
        import hashlib
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
                "effective_mime_type": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
            }

            res = aextract.extract_attachment_content(fetch_result, data_dir=data_dir)
            self.assertEqual("extracted", res["status"])
            self.assertEqual(15, res["scope"]["slides"])
            self.assertEqual("max_slides_exceeded", res["truncation_reason"])
            self.assertIn("Slide 1 Title", res["text"])
            self.assertIn("Slide 15 Title", res["text"])
            self.assertNotIn("Slide 16 Title", res["text"])

    def test_extract_image_pdf_ocr_local_derivative_preserves_original(self) -> None:
        import hashlib
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
                "effective_mime_type": "application/pdf",
            }

            # Mock ocrmypdf or ocr runner to return mock OCR derivative bytes
            mock_ocr_pdf = self._create_sample_pdf(["OCR Page 1 Extracted", "OCR Page 2 Extracted"])
            with patch.object(aextract, "_run_ocr_derivative", return_value=(mock_ocr_pdf, "OCR Page 1 Extracted\nOCR Page 2 Extracted", 2)):
                res = aextract.extract_attachment_content(fetch_result, data_dir=data_dir)

            self.assertEqual("extracted", res["status"])
            self.assertEqual("ocr_local_derivative", res["method"])
            self.assertEqual(original_sha, res["source_sha256"])
            self.assertIsNotNone(res["derivative_sha256"])
            self.assertNotEqual(original_sha, res["derivative_sha256"])

            # CRITICAL CHECK: Original file was NEVER modified!
            self.assertEqual(pdf_bytes, pdf_file.read_bytes())
            self.assertEqual(original_sha, hashlib.sha256(pdf_file.read_bytes()).hexdigest())

            # Derivative file exists in derivatives folder
            deriv_file = run_dir / "derivatives" / "scanned.ocr.pdf"
            self.assertTrue(deriv_file.exists())
            self.assertEqual(mock_ocr_pdf, deriv_file.read_bytes())

    def test_hash_drift_before_extract_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            data_dir = Path(tmp_dir) / "data" / "mail-desk"
            run_dir = data_dir / "attachments" / "run_drift"
            run_dir.mkdir(parents=True)
            f = run_dir / "drift.txt"
            f.write_bytes(b"actual content on disk")

            fetch_result = {
                "status": "fetched",
                "run_id": "run_drift",
                "relative_path": "data/mail-desk/attachments/run_drift/drift.txt",
                "filename": "drift.txt",
                "fetch_sha256": "0" * 64,  # Expected hash does not match disk!
            }

            with self.assertRaises(attachments.HashDriftError):
                aextract.extract_attachment_content(fetch_result, data_dir=data_dir)

    def test_missing_tool_returns_attachment_conversion_unavailable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            data_dir = Path(tmp_dir) / "data" / "mail-desk"
            run_dir = data_dir / "attachments" / "run_notool"
            run_dir.mkdir(parents=True)
            f = run_dir / "file.xyz"
            f.write_bytes(b"some unknown format binary data")
            import hashlib
            f_sha = hashlib.sha256(f.read_bytes()).hexdigest()

            fetch_result = {
                "status": "fetched",
                "run_id": "run_notool",
                "relative_path": "data/mail-desk/attachments/run_notool/file.xyz",
                "filename": "file.xyz",
                "fetch_sha256": f_sha,
                "effective_mime_type": "application/x-unknown",
            }

            with patch.dict("sys.modules", {"markitdown": None}):
                res = aextract.extract_attachment_content(fetch_result, data_dir=data_dir)
            self.assertEqual("attachment_conversion_unavailable", res["status"])
            self.assertIn("No extraction converter available", str(res["error"]))
            self.assertEqual("", res["text"])

    def test_corrupt_file_handled_gracefully(self) -> None:
        import hashlib
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
                "effective_mime_type": "application/pdf",
            }

            res = aextract.extract_attachment_content(fetch_result, data_dir=data_dir)
            self.assertIn(res["status"], ["corrupt_attachment", "attachment_conversion_unavailable", "extraction_failed"])
            self.assertIsNotNone(res["error"])


if __name__ == "__main__":
    unittest.main()
