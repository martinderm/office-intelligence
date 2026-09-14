"""Deterministic bounded attachment extraction and local OCR derivative contract (MD-A3).

Extracts text from verified quarantine attachments under `data/mail-desk/attachments/<run-id>/`.
Enforces strict character, page, slide, and grid limits.
For image-only or scanned PDFs, generates an isolated local OCR derivative under
`derivatives/<filename>.ocr.pdf` while leaving the original quarantine file completely unmutated.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
import re
import shutil
import subprocess
from typing import Any

from core.common import resolve_data_dir
from core.attachment_policy import DEFAULT_ATTACHMENT_POLICY
from core.attachments import HashDriftError


# Extraction limits
MAX_CHARS_PER_ATTACHMENT = 15000
MAX_PDF_PAGES = 10
MAX_OCR_PAGES = 3
MAX_DOCX_PARAGRAPHS = 40
MAX_PPTX_SLIDES = 15
MAX_XLSX_SHEETS = 2
MAX_XLSX_ROWS = 50
MAX_XLSX_COLS = 10
PROCESS_TIMEOUT_SECONDS = 20
OCR_TIMEOUT_SECONDS = 30


def _run_ocr_derivative(
    source_pdf_path: Path,
    derivative_pdf_path: Path,
    max_pages: int = MAX_OCR_PAGES,
) -> tuple[bytes, str, int]:
    """Perform bounded local OCR on an image PDF, writing exclusively to the derivative path."""
    import pymupdf

    derivative_pdf_path.parent.mkdir(parents=True, exist_ok=True)

    # Check if ocrmypdf is available
    try:
        import ocrmypdf
        # Use ocrmypdf to produce derivative
        ocrmypdf.ocr(
            source_pdf_path,
            derivative_pdf_path,
            pages=f"1-{max_pages}",
            force_ocr=True,
            output_type="pdf",
            optimize=0,
            fast_web_view=0,
        )
        deriv_bytes = derivative_pdf_path.read_bytes()
        doc = pymupdf.open(derivative_pdf_path)
        texts = [p.get_text() for p in doc]
        doc.close()
        return deriv_bytes, "\n".join(texts), len(texts)
    except Exception:
        pass

    # Fallback: if ocrmypdf fails or cannot run in environment, check if pymupdf has OCR or raise unavailable
    raise RuntimeError("OCR tool execution failed or unavailable.")


def extract_attachment_content(
    fetch_result: dict[str, Any],
    expected_sha256: str | None = None,
    data_dir: Path | None = None,
    policy: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Extract bounded textual content from a verified quarantine attachment."""
    if not isinstance(fetch_result, dict):
        raise ValueError("fetch_result must be a valid dictionary.")

    rel_path = fetch_result.get("relative_path")
    if not rel_path or not str(rel_path).strip():
        raise ValueError("fetch_result missing 'relative_path'.")

    base_data_dir = data_dir or resolve_data_dir()
    # Resolve target file
    # Note: rel_path may be "data/mail-desk/attachments/<run-id>/<file>" or "attachments/<run-id>/<file>"
    rel_clean = str(rel_path).replace("\\", "/")
    if rel_clean.startswith("data/mail-desk/"):
        rel_clean = rel_clean[len("data/mail-desk/"):]

    target_file = (base_data_dir / rel_clean).resolve()
    if not target_file.exists() or not target_file.is_file():
        raise FileNotFoundError(f"Attachment file not found in quarantine: {target_file}")

    file_bytes = target_file.read_bytes()
    actual_sha = hashlib.sha256(file_bytes).hexdigest().lower()

    # Preflight Hash Drift Verification
    exp_sha = expected_sha256 or fetch_result.get("fetch_sha256") or fetch_result.get("inventory_sha256")
    if exp_sha:
        if actual_sha != str(exp_sha).strip().lower():
            raise HashDriftError(
                f"Quarantine file hash drift: file has '{actual_sha}', expected '{exp_sha}'"
            )

    filename = fetch_result.get("filename") or target_file.name
    ext = target_file.suffix.lower()
    eff_mime = str(fetch_result.get("effective_mime_type", "")).lower()

    status = "extracted"
    method = "native"
    tool = "built_in"
    tool_version = "1.0"
    quality = "high"
    truncation_reason: str | None = None
    extracted_text = ""
    derivative_sha256: str | None = None
    scope: dict[str, Any] = {
        "pages_processed": None,
        "pages_total": None,
        "ocr_pages": 0,
        "paragraphs": None,
        "slides": None,
        "sheets": None,
    }
    error: str | None = None

    try:
        # 1. Plain Text / CSV / Markdown
        if ext in [".txt", ".csv", ".tsv", ".md", ".log", ".json"] or eff_mime.startswith("text/"):
            for enc in ["utf-8", "cp1252", "latin1"]:
                try:
                    extracted_text = file_bytes.decode(enc)
                    break
                except UnicodeDecodeError:
                    continue
            method = "native"
            tool = "built_in"

        # 2. PDF Documents
        elif ext == ".pdf" or eff_mime == "application/pdf":
            import pymupdf
            try:
                doc = pymupdf.open(target_file)
            except Exception as e:
                return {
                    "status": "corrupt_attachment",
                    "source_sha256": actual_sha,
                    "derivative_sha256": None,
                    "method": "native",
                    "tool": "pymupdf",
                    "tool_version": getattr(pymupdf, "__version__", "1.0"),
                    "quality": "low",
                    "scope": scope,
                    "truncation_reason": None,
                    "character_count": 0,
                    "text": "",
                    "error": f"PDF parse error: {e}",
                }

            total_pages = len(doc)
            scope["pages_total"] = total_pages
            pages_to_process = min(total_pages, MAX_PDF_PAGES)
            scope["pages_processed"] = pages_to_process
            if total_pages > MAX_PDF_PAGES:
                truncation_reason = "max_pages_exceeded"

            page_texts = []
            has_text = False
            for p_idx in range(pages_to_process):
                p = doc[p_idx]
                p_txt = p.get_text()
                if p_txt and p_txt.strip():
                    has_text = True
                page_texts.append(p_txt)
            doc.close()

            if has_text:
                extracted_text = "\n".join(page_texts)
                method = "native"
                tool = "pymupdf"
                tool_version = getattr(pymupdf, "__version__", "1.0")
                quality = "high"
            else:
                # Scanned or Image-only PDF -> Local OCR Derivative Workflow
                run_dir = target_file.parent
                deriv_dir = run_dir / "derivatives"
                deriv_path = deriv_dir / f"{target_file.stem}.ocr.pdf"

                try:
                    deriv_bytes, ocr_text, ocr_count = _run_ocr_derivative(
                        target_file, deriv_path, max_pages=MAX_OCR_PAGES
                    )
                    deriv_path.parent.mkdir(parents=True, exist_ok=True)
                    if not deriv_path.exists() or deriv_path.read_bytes() != deriv_bytes:
                        deriv_path.write_bytes(deriv_bytes)
                    derivative_sha256 = hashlib.sha256(deriv_bytes).hexdigest().lower()
                    extracted_text = ocr_text
                    method = "ocr_local_derivative"
                    tool = "ocrmypdf"
                    tool_version = "1.0"
                    quality = "medium"
                    scope["ocr_pages"] = ocr_count
                except Exception as e:
                    return {
                        "status": "attachment_conversion_unavailable",
                        "source_sha256": actual_sha,
                        "derivative_sha256": None,
                        "method": "ocr_local_derivative",
                        "tool": "ocrmypdf",
                        "tool_version": "1.0",
                        "quality": "low",
                        "scope": scope,
                        "truncation_reason": None,
                        "character_count": 0,
                        "text": "",
                        "error": f"OCR tool execution unavailable: {e}",
                    }

        # 3. Microsoft Word (DOCX / DOCM)
        elif ext in [".docx", ".docm"]:
            import docx
            try:
                doc = docx.Document(target_file)
            except Exception as e:
                return {
                    "status": "corrupt_attachment",
                    "source_sha256": actual_sha,
                    "derivative_sha256": None,
                    "method": "native",
                    "tool": "python-docx",
                    "tool_version": "1.0",
                    "quality": "low",
                    "scope": scope,
                    "truncation_reason": None,
                    "character_count": 0,
                    "text": "",
                    "error": f"DOCX parse error: {e}",
                }

            total_paras = len(doc.paragraphs)
            paras_to_process = min(total_paras, MAX_DOCX_PARAGRAPHS)
            scope["paragraphs"] = paras_to_process
            if total_paras > MAX_DOCX_PARAGRAPHS:
                truncation_reason = "max_paragraphs_exceeded"

            para_texts = [p.text for p in doc.paragraphs[:paras_to_process]]
            extracted_text = "\n".join(para_texts)
            method = "native"
            tool = "python-docx"

        # 4. Microsoft Excel (XLSX / XLSM)
        elif ext in [".xlsx", ".xlsm"]:
            import openpyxl
            try:
                wb = openpyxl.load_workbook(target_file, read_only=True, data_only=True)
            except Exception as e:
                return {
                    "status": "corrupt_attachment",
                    "source_sha256": actual_sha,
                    "derivative_sha256": None,
                    "method": "native",
                    "tool": "openpyxl",
                    "tool_version": "1.0",
                    "quality": "low",
                    "scope": scope,
                    "truncation_reason": None,
                    "character_count": 0,
                    "text": "",
                    "error": f"XLSX parse error: {e}",
                }

            sheets_processed = 0
            lines = []
            grid_exceeded = False
            for sheetname in wb.sheetnames[:MAX_XLSX_SHEETS]:
                sheets_processed += 1
                lines.append(f"--- Sheet: {sheetname} ---")
                ws = wb[sheetname]
                row_idx = 0
                for row in ws.iter_rows(values_only=True):
                    row_idx += 1
                    if row_idx > MAX_XLSX_ROWS:
                        grid_exceeded = True
                        break
                    row_vals = [str(c) if c is not None else "" for c in row[:MAX_XLSX_COLS]]
                    if len(row) > MAX_XLSX_COLS:
                        grid_exceeded = True
                    lines.append("\t".join(row_vals))

            wb.close()
            scope["sheets"] = sheets_processed
            if grid_exceeded or len(wb.sheetnames) > MAX_XLSX_SHEETS:
                truncation_reason = "grid_limit_exceeded"
            extracted_text = "\n".join(lines)
            method = "native"
            tool = "openpyxl"

        # 5. Microsoft PowerPoint (PPTX / PPTM)
        elif ext in [".pptx", ".pptm"]:
            import pptx
            try:
                prs = pptx.Presentation(target_file)
            except Exception as e:
                return {
                    "status": "corrupt_attachment",
                    "source_sha256": actual_sha,
                    "derivative_sha256": None,
                    "method": "native",
                    "tool": "python-pptx",
                    "tool_version": "1.0",
                    "quality": "low",
                    "scope": scope,
                    "truncation_reason": None,
                    "character_count": 0,
                    "text": "",
                    "error": f"PPTX parse error: {e}",
                }

            total_slides = len(prs.slides)
            slides_to_process = min(total_slides, MAX_PPTX_SLIDES)
            scope["slides"] = slides_to_process
            if total_slides > MAX_PPTX_SLIDES:
                truncation_reason = "max_slides_exceeded"

            slide_texts = []
            for s_idx in range(slides_to_process):
                slide = prs.slides[s_idx]
                s_lines = []
                for shape in slide.shapes:
                    if shape.has_text_frame:
                        for paragraph in shape.text_frame.paragraphs:
                            s_lines.append(paragraph.text)
                slide_texts.append(f"--- Slide {s_idx + 1} ---\n" + "\n".join(s_lines))

            extracted_text = "\n\n".join(slide_texts)
            method = "native"
            tool = "python-pptx"

        # 6. Fallback: MarkItDown for other formats
        else:
            try:
                from markitdown import MarkItDown
                md = MarkItDown()
                result = md.convert(str(target_file))
                extracted_text = result.text_content or ""
                method = "markitdown"
                tool = "markitdown"
            except Exception:
                return {
                    "status": "attachment_conversion_unavailable",
                    "source_sha256": actual_sha,
                    "derivative_sha256": None,
                    "method": "native",
                    "tool": "none",
                    "tool_version": "1.0",
                    "quality": "low",
                    "scope": scope,
                    "truncation_reason": None,
                    "character_count": 0,
                    "text": "",
                    "error": f"No extraction converter available for format '{ext}' ({eff_mime})",
                }

    except Exception as exc:
        return {
            "status": "extraction_failed",
            "source_sha256": actual_sha,
            "derivative_sha256": None,
            "method": method,
            "tool": tool,
            "tool_version": tool_version,
            "quality": "low",
            "scope": scope,
            "truncation_reason": None,
            "character_count": 0,
            "text": "",
            "error": str(exc)[:1000],
        }

    # Apply 15,000 character budget cap
    if len(extracted_text) > MAX_CHARS_PER_ATTACHMENT:
        extracted_text = extracted_text[:MAX_CHARS_PER_ATTACHMENT]
        truncation_reason = "max_chars_exceeded"

    return {
        "status": status,
        "source_sha256": actual_sha,
        "derivative_sha256": derivative_sha256,
        "method": method,
        "tool": tool,
        "tool_version": tool_version,
        "quality": quality,
        "scope": scope,
        "truncation_reason": truncation_reason,
        "character_count": len(extracted_text),
        "text": extracted_text,
        "error": None,
    }
