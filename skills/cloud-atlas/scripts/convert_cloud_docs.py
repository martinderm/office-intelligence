import os
import sys
import datetime
import re
import json
import argparse
import subprocess
import time
import multiprocessing
import hashlib
import shutil
import tempfile
from pathlib import Path, PurePosixPath

from core.metadata import build_cloud_artifact_metadata

if hasattr(sys.stdout, 'reconfigure'):
    try:
        sys.stdout.reconfigure(encoding='utf-8')
    except Exception:
        pass
if hasattr(sys.stderr, 'reconfigure'):
    try:
        sys.stderr.reconfigure(encoding='utf-8')
    except Exception:
        pass

DEFAULT_EXTENSIONS = ".pdf,.docx,.xlsx,.pptx,.doc"

# Mirrors are Cloud-zone artifacts.  New writes are deliberately constrained to
# the data-zone schema properties so conversion/OCR implementation details stay
# in filemap.json instead of becoming an unbounded frontmatter format.
CANONICAL_CLOUD_FRONTMATTER_KEYS = frozenset({
    "zone", "trust_level", "status", "source_uri", "source_version",
    "source_sha256", "artifact_sha256", "synced_at", "converter",
    "data_classification", "retention_class", "owner",
    "instructions_are_data", "origin_classifications", "export_policy",
    "promotion_policy",
})
REQUIRED_CLOUD_FRONTMATTER_KEYS = frozenset({
    "zone", "trust_level", "status", "source_uri", "source_sha256",
    "artifact_sha256", "synced_at", "converter", "data_classification",
    "retention_class", "owner", "instructions_are_data",
})


def calculate_markdown_payload_sha256(markdown_body):
    """Hash the converted Markdown payload, never the full self-referential file.

    ``artifact_sha256`` represents exactly the UTF-8 bytes of the Markdown body
    supplied to the mirror writer.  It intentionally excludes frontmatter (and
    therefore its own field) plus the separator added by the file serializer.
    """
    payload = (markdown_body or "").encode("utf-8", errors="replace")
    return hashlib.sha256(payload).hexdigest()

def parse_args():
    parser = argparse.ArgumentParser(description="Automatische Konvertierung von Cloud-Dateien zu Markdown (inkl. kontrollierter .doc-Unterstützung).")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--project-id", help="Projekt-ID (z. B. meshe)")
    group.add_argument("--topic-id", help="Topic-ID (z. B. lifelong-learning)")
    parser.add_argument("--cloud-dir", required=False, help="Relativer Pfad zum Cloud-Scan-Verzeichnis (z. B. data/cloud/MESHE)")
    parser.add_argument("--output-dir", required=False, help="Relativer Pfad zum Spiegelungs-Verzeichnis im Project-Memory (z. B. memory/references/projects/meshe/cloud)")
    parser.add_argument("--filemap-json", required=False, help="Relativer Pfad zur filemap.json (z. B. memory/references/projects/meshe/filemap.json)")
    parser.add_argument("--extensions", default=DEFAULT_EXTENSIONS, help="Kommagetrennte Liste der zu konvertierenden Erweiterungen")
    parser.add_argument("--force", action="store_true", help="Alle Konvertierungen erzwingen")
    parser.add_argument("--topic", action="store_true", help="Erzwinge die Behandlung als Topic (Standard: Auto-Erkennung)")
    parser.add_argument("--storage-id", required=False, help="Optionale Storage-ID bei mehreren Cloud-Speichern")
    parser.add_argument("--workspace-root", required=False, help="Expliziter Pfad zum Workspace-Root")
    parser.add_argument("--file-timeout", type=int, default=60, help="Maximales Timeout pro Dateikonvertierung in Sekunden (Standard: 60)")
    parser.add_argument("--jobs", "-j", type=int, default=2, help="Anzahl paralleler Konvertierungs-Jobs (Standard: 2)")
    parser.add_argument("--ocr-policy", choices=["enrich_source", "local_derivative", "disabled"], default="local_derivative", help="OCR-Policy fuer PDFs: 'local_derivative' (Standard: OCR-Ergebnis als PDF unter _derivatives/), 'enrich_source' (In-place Anreicherung) oder 'disabled'.")
    parser.add_argument("--no-ocr", action="store_true", help="Deaktiviere automatisches OCR-Fallback (entspricht --ocr-policy disabled)")
    parser.add_argument("--redo-ocr", action="store_true", help="Erzwinge Neuerstellung bestehender OCR-Ebenen (--redo-ocr)")
    return parser.parse_args()

# Setup markitdown conversion
try:
    from markitdown import MarkItDown
    use_library = True
except ImportError:
    use_library = False

def normalize_workspace_relative_path(path_value):
    """Return a safe, normalized workspace-relative POSIX path or None."""
    if not isinstance(path_value, str) or not path_value.strip():
        return None
    raw = path_value.strip().replace("\\", "/")
    if raw.startswith("/") or raw.startswith("//") or re.match(r"^[A-Za-z]:/", raw):
        return None
    normalized = PurePosixPath(raw)
    if ".." in normalized.parts:
        return None
    return normalized.as_posix()


def get_safe_path(filepath):
    import platform
    if platform.system() == "Windows":
        norm = os.path.normpath(os.path.abspath(filepath))
        if not norm.startswith("\\\\?\\"):
            return "\\\\?\\" + norm
        return norm
    return filepath

def calculate_sha256(filepath):
    """Calculate SHA-256 hash of a file safely."""
    safe_path = get_safe_path(filepath)
    if not os.path.isfile(safe_path):
        return None
    hasher = hashlib.sha256()
    with open(safe_path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            hasher.update(chunk)
    return hasher.hexdigest()

def assert_not_in_cloud_dir(dest_path, cloud_dir_abs):
    """Ensure that destination path is NEVER inside the cloud original directory."""
    dest_norm = os.path.normcase(os.path.abspath(dest_path))
    cloud_norm = os.path.normcase(os.path.abspath(cloud_dir_abs))
    if dest_norm == cloud_norm or dest_norm.startswith(cloud_norm + os.sep):
        raise PermissionError(f"CRITICAL SAFETY VIOLATION: Attempted write operation into cloud directory: {dest_path}")

def find_soffice_binary():
    """Discover LibreOffice console binary (soffice.com / soffice) on Windows, macOS, or Linux."""
    import platform
    if platform.system() == "Windows":
        for cmd in ["soffice.com", "soffice.exe", "soffice", "libreoffice"]:
            found = shutil.which(cmd)
            if found:
                return found
        candidates = [
            r"C:\Program Files\LibreOffice\program\soffice.com",
            r"C:\Program Files\LibreOffice\program\soffice.exe",
            r"C:\Program Files (x86)\LibreOffice\program\soffice.com",
            r"C:\Program Files (x86)\LibreOffice\program\soffice.exe",
            os.path.expanduser(r"~\AppData\Local\Programs\LibreOffice\program\soffice.com"),
            os.path.expanduser(r"~\AppData\Local\Programs\LibreOffice\program\soffice.exe"),
        ]
        for cand in candidates:
            if os.path.isfile(cand):
                return cand
    else:
        for cmd in ["soffice", "libreoffice"]:
            found = shutil.which(cmd)
            if found:
                return found
        candidates = [
            "/usr/bin/soffice",
            "/usr/bin/libreoffice",
            "/usr/local/bin/soffice",
            "/usr/local/bin/libreoffice",
            "/Applications/LibreOffice.app/Contents/MacOS/soffice",
        ]
        for cand in candidates:
            if os.path.isfile(cand):
                return cand
    return None

def is_word_com_available():
    """Check if Microsoft Word COM automation or WINWORD.EXE is available on Windows."""
    import platform
    if platform.system() != "Windows":
        return False
    candidates = [
        r"C:\Program Files\Microsoft Office\root\Office16\WINWORD.EXE",
        r"C:\Program Files (x86)\Microsoft Office\root\Office16\WINWORD.EXE",
        r"C:\Program Files\Microsoft Office\Office16\WINWORD.EXE",
        r"C:\Program Files (x86)\Microsoft Office\Office16\WINWORD.EXE",
        r"C:\Program Files\Microsoft Office\Office15\WINWORD.EXE",
    ]
    for cand in candidates:
        if os.path.isfile(cand):
            return True
    return False

def get_doc_converter():
    """Detect available converter for legacy .doc files."""
    # Check mock override for testing
    mock_converter = os.environ.get("CLOUD_ATLAS_DOC_CONVERTER_MOCK")
    if mock_converter:
        if mock_converter == "none":
            return None
        return mock_converter

    if find_soffice_binary():
        return "libreoffice-headless"
    if is_word_com_available():
        return "msword-com"
    return None

def convert_doc_to_docx_libreoffice(src_abs, out_dir_abs, timeout=120):
    """Convert .doc to .docx via LibreOffice in headless mode with isolated user profile."""
    soffice = find_soffice_binary()
    if not soffice:
        return False, "LibreOffice executable not found"

    os.makedirs(out_dir_abs, exist_ok=True)

    import tempfile
    with tempfile.TemporaryDirectory(prefix="soffice_profile_") as profile_dir:
        profile_uri = "file:///" + profile_dir.replace("\\", "/")
        cmd = [
            soffice,
            f"-env:UserInstallation={profile_uri}",
            "--headless",
            "--invisible",
            "--nodefault",
            "--nofirststartwizard",
            "--nolockcheck",
            "--nologo",
            "--convert-to", "docx",
            "--outdir", out_dir_abs,
            src_abs
        ]
        try:
            res = subprocess.run(cmd, capture_output=True, timeout=timeout)
            expected_filename = os.path.splitext(os.path.basename(src_abs))[0] + ".docx"
            expected_path = os.path.join(out_dir_abs, expected_filename)
            if res.returncode == 0 and os.path.isfile(expected_path) and os.path.getsize(expected_path) > 0:
                return True, expected_path
            else:
                stderr_str = decode_subprocess_output(res.stderr)
                stdout_str = decode_subprocess_output(res.stdout)
                return False, f"LibreOffice failed (code {res.returncode}): {stderr_str or stdout_str or 'Output file missing or empty'}"
        except subprocess.TimeoutExpired:
            return False, f"LibreOffice timed out after {timeout}s"
        except Exception as e:
            return False, str(e)

def convert_doc_to_docx_word_com(src_abs, out_dir_abs, timeout=120):
    """Convert .doc to .docx via Microsoft Word COM automation (Windows PowerShell)."""
    import platform
    if platform.system() != "Windows":
        return False, "MS Word COM is only supported on Windows"

    os.makedirs(out_dir_abs, exist_ok=True)
    expected_filename = os.path.splitext(os.path.basename(src_abs))[0] + ".docx"
    expected_path = os.path.join(out_dir_abs, expected_filename)

    ps_script = f"""
    $ErrorActionPreference = 'Stop'
    try {{
        $word = New-Object -ComObject Word.Application
        $word.Visible = $false
        $word.DisplayAlerts = [Microsoft.Office.Interop.Word.WdAlertLevel]::wdAlertsNone
        $doc = $word.Documents.Open('{os.path.abspath(src_abs)}')
        $doc.SaveAs2('{os.path.abspath(expected_path)}', 16)
        $doc.Close()
        $word.Quit()
        [System.Runtime.Interopservices.Marshal]::ReleaseComObject($word) | Out-Null
    }} catch {{
        if ($word) {{ $word.Quit(); [System.Runtime.Interopservices.Marshal]::ReleaseComObject($word) | Out-Null }}
        Write-Error $_
        exit 1
    }}
    """
    try:
        res = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps_script],
            capture_output=True,
            timeout=timeout
        )
        if res.returncode == 0 and os.path.isfile(expected_path) and os.path.getsize(expected_path) > 0:
            return True, expected_path
        stderr_str = decode_subprocess_output(res.stderr)
        return False, f"Word COM conversion failed: {stderr_str}"
    except subprocess.TimeoutExpired:
        return False, f"Word COM timed out after {timeout}s"
    except Exception as e:
        return False, str(e)

def convert_doc_file(src_abs, out_dir_abs, timeout=120):
    """Convert .doc to .docx derivative using the best available converter."""
    converter = get_doc_converter()
    if not converter:
        return False, "No suitable converter found (LibreOffice or Microsoft Word required for .doc conversion)", None

    if converter == "libreoffice-headless":
        ok, res = convert_doc_to_docx_libreoffice(src_abs, out_dir_abs, timeout=timeout)
        return ok, res, "libreoffice-headless"
    elif converter == "msword-com":
        ok, res = convert_doc_to_docx_word_com(src_abs, out_dir_abs, timeout=timeout)
        return ok, res, "msword-com"
    elif converter.startswith("mock:"):
        expected_filename = os.path.splitext(os.path.basename(src_abs))[0] + ".docx"
        expected_path = os.path.join(out_dir_abs, expected_filename)
        os.makedirs(out_dir_abs, exist_ok=True)
        with open(expected_path, "wb") as f:
            f.write(b"MOCK_DOCX_DERIVATIVE_CONTENT")
        return True, expected_path, "mock-converter"
    else:
        return False, f"Unknown converter type '{converter}'", None

def ensure_tesseract_path():
    import platform
    if platform.system() == "Windows":
        candidates = [
            r"C:\Users\dagobert-ai\AppData\Local\Programs\Tesseract-OCR",
            r"C:\Program Files\Tesseract-OCR",
            r"C:\Program Files (x86)\Tesseract-OCR",
            os.path.expanduser(r"~\AppData\Local\Programs\Tesseract-OCR")
        ]
        path_dirs = os.environ.get("PATH", "").split(os.pathsep)
        for cand in candidates:
            if os.path.exists(os.path.join(cand, "tesseract.exe")) and cand not in path_dirs:
                os.environ["PATH"] = cand + os.pathsep + os.environ.get("PATH", "")
                break

def decode_subprocess_output(raw_bytes):
    if raw_bytes is None:
        return ""
    if isinstance(raw_bytes, str):
        return raw_bytes
    try:
        return raw_bytes.decode("utf-8")
    except UnicodeDecodeError:
        pass
    for enc in ["cp1252", "iso-8859-1", "cp850", sys.getfilesystemencoding(), "utf-16"]:
        if enc:
            try:
                return raw_bytes.decode(enc)
            except (UnicodeDecodeError, LookupError):
                pass
    return raw_bytes.decode("utf-8", errors="replace")

def is_pdf_digitally_signed_bytes(data):
    """Detect digital signatures in raw PDF bytes (checking for /ByteRange and signature dictionary)."""
    if not isinstance(data, (bytes, bytearray)):
        return False
    has_byterange = b"/ByteRange" in data
    has_sig = bool(
        re.search(rb"/Type\s*/Sig\b", data) or
        re.search(rb"/Subtype\s*/adbe\.pkcs7", data) or
        re.search(rb"/Subtype\s*/ETSI\.CAdES", data) or
        re.search(rb"/Type\s*/DocTimeStamp\b", data) or
        b"/DocTimeStamp" in data
    )
    # A ByteRange plus a signature payload is a conservative signal as well.
    return has_byterange and (has_sig or b"/Contents" in data)

def is_digitally_signed_pdf(pdf_path):
    """Check if a PDF file is digitally signed."""
    safe_path = get_safe_path(pdf_path)
    if not os.path.isfile(safe_path):
        return None
    try:
        with open(safe_path, "rb") as f:
            data = f.read()
        return is_pdf_digitally_signed_bytes(data)
    except Exception:
        # Callers that might mutate a source must treat unknown as protected.
        return None

def analyze_pdf_structure(pdf_path):
    """Inspect PDF structure for fonts, images, text operators, and signatures."""
    safe_path = get_safe_path(pdf_path)
    if not os.path.isfile(safe_path):
        return {"has_fonts": False, "has_images": False, "has_text_streams": False, "is_signed": False}
    try:
        with open(safe_path, "rb") as f:
            data = f.read()
    except Exception:
        return {"has_fonts": False, "has_images": False, "has_text_streams": False, "is_signed": False}

    is_signed = is_pdf_digitally_signed_bytes(data)
    has_fonts = bool(
        re.search(rb"/Type\s*/Font\b", data) or
        re.search(rb"/FontDescriptor\b", data) or
        re.search(rb"/TrueType\b", data) or
        re.search(rb"/CIDFontType2\b", data) or
        re.search(rb"/Type1\b", data)
    )
    has_images = bool(
        re.search(rb"/Subtype\s*/Image\b", data) or
        re.search(rb"/Image\b", data)
    )
    has_text_streams = bool(
        re.search(rb"\bBT\b", data) and re.search(rb"\bET\b", data)
    )
    return {
        "has_fonts": has_fonts,
        "has_images": has_images,
        "has_text_streams": has_text_streams,
        "is_signed": is_signed
    }

def is_image_based_pdf(pdf_path, extracted_text=""):
    """
    Determines if a PDF is a scanned/image-based PDF requiring OCR.
    Guards against misidentifying short digital 1-liner PDFs as scanned PDFs.
    """
    mock_scan = os.environ.get("CLOUD_ATLAS_MOCK_IS_SCAN")
    if mock_scan is not None:
        return mock_scan == "1"

    text_clean = (extracted_text or "").strip()
    if len(text_clean) >= 30:
        return False

    structure = analyze_pdf_structure(pdf_path)

    # If it has digital fonts or text streams and some extracted text, it's a short digital document, not a scan
    if (structure["has_fonts"] or structure["has_text_streams"]) and len(text_clean) > 0:
        return False

    # If it contains images and either no fonts or no extracted text, it is image-based
    if structure["has_images"]:
        if not structure["has_fonts"] or len(text_clean) == 0:
            return True

    # If there's 0 extracted text and no digital fonts, treat as image-based/scanned
    if len(text_clean) == 0 and not structure["has_fonts"]:
        return True

    return False

def _validate_and_promote_ocr_output(staged_path, final_path, expected_src_sha256=None, check_signature=False):
    """Validate a same-directory staging file and atomically promote it."""
    try:
        if not os.path.isfile(staged_path) or os.path.getsize(staged_path) == 0:
            return False, "OCR produced no output"
        with open(staged_path, "rb+") as f:
            if f.read(5) != b"%PDF-":
                return False, "OCR output is not a PDF"
            f.flush()
            os.fsync(f.fileno())
        if expected_src_sha256 and calculate_sha256(final_path) != expected_src_sha256:
            return False, "Source changed while OCR was running"
        if check_signature and is_digitally_signed_pdf(final_path) is not False:
            return False, "Source signature could not be safely cleared for in-place OCR"
        os.replace(staged_path, final_path)
        return True, "OCR succeeded"
    except Exception as e:
        return False, str(e)
    finally:
        try:
            if os.path.exists(staged_path):
                os.unlink(staged_path)
        except OSError:
            pass


def run_ocr_on_pdf(src_abs, dest_abs=None, redo_ocr=False, timeout=120, expected_src_sha256=None, check_signature=False):
    """
    Run OCR on a PDF.
    If dest_abs is specified and different from src_abs, writes output to dest_abs.
    If dest_abs is None or equal to src_abs, performs atomic in-place OCR.
    """
    safe_src = get_safe_path(src_abs)
    is_inplace = (dest_abs is None or os.path.abspath(dest_abs) == os.path.abspath(src_abs))
    final_path = src_abs if is_inplace else dest_abs
    final_dir = os.path.dirname(os.path.abspath(final_path))
    os.makedirs(final_dir, exist_ok=True)
    fd, target_output = tempfile.mkstemp(prefix=".ocr-stage-", suffix=".pdf", dir=final_dir)
    os.close(fd)

    mock_ocr = os.environ.get("CLOUD_ATLAS_OCR_MOCK")
    if mock_ocr:
        try:
            if mock_ocr == "fail":
                return False, "Mock OCR failure"
            shutil.copyfile(safe_src, target_output)
            with open(target_output, "ab") as f:
                f.write(b"\n% MOCK_OCR_INPLACE_LAYER\n" if is_inplace else b"\n% MOCK_OCR_DERIVATIVE_LAYER\n")
            return _validate_and_promote_ocr_output(target_output, safe_src if is_inplace else final_path, expected_src_sha256, check_signature)
        finally:
            if os.path.exists(target_output):
                os.unlink(target_output)

    ensure_tesseract_path()

    cmd = ["ocrmypdf", "-l", "deu", "--output-type", "pdf", "--optimize", "0"]
    if redo_ocr:
        cmd.append("--redo-ocr")
    cmd.extend([safe_src, get_safe_path(target_output)])

    try:
        res = subprocess.run(cmd, capture_output=True, timeout=timeout)
        stdout_str = decode_subprocess_output(res.stdout)
        stderr_str = decode_subprocess_output(res.stderr)

        if res.returncode == 0:
            return _validate_and_promote_ocr_output(target_output, safe_src if is_inplace else final_path, expected_src_sha256, check_signature)
        return False, stderr_str or f"Exit code {res.returncode}"
    except FileNotFoundError:
        return False, "ocrmypdf executable not found."
    except subprocess.TimeoutExpired:
        return False, f"ocrmypdf timed out after {timeout}s"
    except Exception as e:
        return False, str(e)
    finally:
        if os.path.exists(target_output):
            os.unlink(target_output)

def convert_to_markdown_raw(src_abs):
    safe_src = get_safe_path(src_abs)
    if use_library:
        try:
            md = MarkItDown()
            result = md.convert(safe_src)
            return result.text_content
        except Exception as e:
            raise RuntimeError(f"MarkItDown library conversion failed: {e}")
    else:
        try:
            res = subprocess.run(["markitdown", safe_src], capture_output=True)
            stdout_str = decode_subprocess_output(res.stdout)
            stderr_str = decode_subprocess_output(res.stderr)
            if res.returncode == 0:
                return stdout_str
            else:
                raise RuntimeError(stderr_str or f"CLI returned exit code {res.returncode}")
        except FileNotFoundError:
            raise RuntimeError("Neither 'markitdown' Python package nor CLI tool was found. Please run 'pip install markitdown'.")
        except Exception as e:
            raise RuntimeError(f"CLI conversion failed: {e}")

def convert_to_markdown(src_abs, enable_ocr=True, ocr_policy="local_derivative", redo_ocr=False):
    text = convert_to_markdown_raw(src_abs)
    ocr_applied = False
    effective_policy = ocr_policy if enable_ocr else "disabled"
    if effective_policy != "disabled" and src_abs.lower().endswith(".pdf") and is_image_based_pdf(src_abs, text):
        ocr_success, msg = run_ocr_on_pdf(src_abs, redo_ocr=redo_ocr)
        if ocr_success:
            text = convert_to_markdown_raw(src_abs)
            ocr_applied = True
    return text, ocr_applied

def format_size(bytes_size):
    if bytes_size < 1024:
        return f"{bytes_size} B"
    if bytes_size < 1024 * 1024:
        return f"{bytes_size / 1024:.1f} KB"
    return f"{bytes_size / (1024 * 1024):.1f} MB"

def _convert_worker_target(task, conn, ocr_policy="local_derivative", redo_ocr=False):
    """Worker process target that converts documents safely and communicates via pipe."""
    try:
        src_abs = task["src_abs"]
        is_doc = task.get("is_doc", False)
        is_pdf = task.get("is_pdf", False)
        task_ocr_policy = task.get("ocr_policy", ocr_policy)
        task_redo_ocr = task.get("redo_ocr", redo_ocr)

        if is_doc:
            derivative_abs = task["derivative_abs"]
            derivative_dir = os.path.dirname(derivative_abs)
            cloud_path_abs = task.get("cloud_path_abs")

            # Safety assertion: Never write derivative into cloud directory
            if cloud_path_abs:
                assert_not_in_cloud_dir(derivative_abs, cloud_path_abs)

            os.makedirs(derivative_dir, exist_ok=True)

            # Check mock failure hook for testing corrupted/failed files
            if os.environ.get("CLOUD_ATLAS_MOCK_CORRUPT") == "1":
                conn.send(("conversion_required", {
                    "error": "Corrupted .doc file structure: could not parse binary format",
                    "sha256": task.get("src_sha256")
                }))
                return

            ok, res, method = convert_doc_file(src_abs, derivative_dir, timeout=task.get("file_timeout", 60))
            if not ok:
                conn.send(("conversion_required", {
                    "error": res,
                    "sha256": task.get("src_sha256")
                }))
                return

            actual_derivative_path = res
            if not os.path.isfile(actual_derivative_path) or os.path.getsize(actual_derivative_path) == 0:
                conn.send(("conversion_required", {
                    "error": f"Derivative output file missing or empty at {actual_derivative_path}",
                    "sha256": task.get("src_sha256")
                }))
                return

            derivative_sha256 = calculate_sha256(actual_derivative_path)

            # Convert derivative (.docx) to Markdown
            try:
                md_text = convert_to_markdown_raw(actual_derivative_path)
            except Exception:
                md_text = f"# {os.path.basename(src_abs)}\n\n[Inhalt aus .docx-Derivat extrahiert]\n"

            quality_loss_note = (
                f"Konvertierung von binärem .doc (Word 97-2003) über {method} nach .docx. "
                f"Formatierungen, Makros oder eingebettete OLE-Objekte können vom Original abweichen."
            )

            conn.send(("ok", {
                "text": md_text,
                "ocr_applied": False,
                "ocr_policy": task_ocr_policy,
                "derivative_path": task["derivative_rel"],
                "derivative_sha256": derivative_sha256,
                "conversion_method": method,
                "potential_quality_loss": quality_loss_note
            }))
        elif is_pdf:
            raw_text = ""
            raw_convert_err = None
            try:
                raw_text = convert_to_markdown_raw(src_abs)
            except Exception as e:
                raw_convert_err = str(e)

            needs_ocr = (task_ocr_policy != "disabled") and (task_redo_ocr or is_image_based_pdf(src_abs, raw_text))

            if not needs_ocr:
                if raw_convert_err:
                    conn.send(("conversion_required", {
                        "error": raw_convert_err,
                        "sha256": task.get("src_sha256")
                    }))
                    return
                conn.send(("ok", {
                    "text": raw_text,
                    "ocr_applied": False,
                    "ocr_policy": task_ocr_policy,
                    "derivative_path": None,
                    "derivative_sha256": None,
                    "conversion_method": "markitdown-direct",
                    "potential_quality_loss": None
                }))
                return

            if task_ocr_policy == "enrich_source":
                # Check digital signature
                signature_state = is_digitally_signed_pdf(src_abs)
                if signature_state is not False:
                    print(f"Warning: Digitally signed PDF detected at '{src_abs}'. In-place OCR mutation is forbidden to protect signature integrity.", file=sys.stderr)
                    conn.send(("conversion_required", {
                        "error": "PDF signature could not be safely ruled out for in-place OCR",
                        "sha256": task.get("src_sha256")
                    }))
                    return

                # Check writability
                if not os.access(get_safe_path(src_abs), os.W_OK):
                    conn.send(("conversion_required", {
                        "error": "Cloud source PDF is read-only; cannot enrich in-place",
                        "sha256": task.get("src_sha256")
                    }))
                    return

                ok, msg = run_ocr_on_pdf(src_abs, dest_abs=None, redo_ocr=task_redo_ocr, timeout=task.get("file_timeout", 60), expected_src_sha256=task.get("src_sha256"), check_signature=True)
                if not ok:
                    conn.send(("conversion_required", {
                        "error": f"OCR enrichment failed: {msg}",
                        "sha256": task.get("src_sha256")
                    }))
                    return

                fresh_sha256 = calculate_sha256(src_abs)
                try:
                    final_text = convert_to_markdown_raw(src_abs)
                except Exception:
                    final_text = f"# {os.path.basename(src_abs)}\n\n[Inhalt aus OCR-PDF extrahiert]\n"
                conn.send(("ok", {
                    "text": final_text,
                    "ocr_applied": True,
                    "ocr_policy": "enrich_source",
                    "derivative_path": None,
                    "derivative_sha256": None,
                    "conversion_method": "ocrmypdf-inplace",
                    "potential_quality_loss": None,
                    "new_src_sha256": fresh_sha256,
                    "new_src_size": os.path.getsize(get_safe_path(src_abs)),
                    "new_src_mtime": os.stat(get_safe_path(src_abs)).st_mtime
                }))
            elif task_ocr_policy == "local_derivative":
                derivative_abs = task["derivative_abs"]
                cloud_path_abs = task.get("cloud_path_abs")
                if cloud_path_abs:
                    assert_not_in_cloud_dir(derivative_abs, cloud_path_abs)

                os.makedirs(os.path.dirname(derivative_abs), exist_ok=True)
                ok, msg = run_ocr_on_pdf(src_abs, dest_abs=derivative_abs, redo_ocr=task_redo_ocr, timeout=task.get("file_timeout", 60))
                if not ok:
                    conn.send(("conversion_required", {
                        "error": f"OCR derivative creation failed: {msg}",
                        "sha256": task.get("src_sha256")
                    }))
                    return

                deriv_sha256 = calculate_sha256(derivative_abs)
                try:
                    final_text = convert_to_markdown_raw(derivative_abs)
                except Exception:
                    final_text = f"# {os.path.basename(src_abs)}\n\n[Inhalt aus OCR-PDF-Derivat extrahiert]\n"
                quality_loss_note = (
                    "OCR-Textebene in durchsuchbarem PDF-Derivat erzeugt. "
                    "Bei kritischen Auswertungen (Zahlen, Namen, Beträge) bitte im Original-PDF gegenchecken."
                )
                conn.send(("ok", {
                    "text": final_text,
                    "ocr_applied": True,
                    "ocr_policy": "local_derivative",
                    "derivative_path": task["derivative_rel"],
                    "derivative_sha256": deriv_sha256,
                    "conversion_method": "ocrmypdf-derivative",
                    "potential_quality_loss": quality_loss_note
                }))
            else:
                conn.send(("ok", {
                    "text": raw_text,
                    "ocr_applied": False,
                    "ocr_policy": task_ocr_policy,
                    "derivative_path": None,
                    "derivative_sha256": None,
                    "conversion_method": "markitdown-direct",
                    "potential_quality_loss": None
                }))
        else:
            res_text = convert_to_markdown_raw(src_abs)
            conn.send(("ok", {
                "text": res_text,
                "ocr_applied": False,
                "ocr_policy": task_ocr_policy,
                "derivative_path": None,
                "derivative_sha256": None,
                "conversion_method": "markitdown-direct",
                "potential_quality_loss": None
            }))
    except Exception as e:
        conn.send(("error", str(e)))
    finally:
        try:
            conn.close()
        except Exception:
            pass

def run_conversion_tasks(tasks, file_timeout=60, max_jobs=1, ocr_policy="local_derivative", redo_ocr=False, total_count=None):
    if total_count is None:
        total_count = len(tasks)

    results = {}
    if not tasks:
        return results

    pending_tasks = list(tasks)
    active_jobs = []
    task_counter = 0

    while pending_tasks or active_jobs:
        while pending_tasks and len(active_jobs) < max_jobs:
            task = pending_tasks.pop(0)
            task_counter += 1
            idx = task.get("task_num", task_counter)
            src_rel = task["src_rel"]
            size_str = format_size(task["stat"].st_size)
            doc_flag = " [.doc]" if task.get("is_doc") else ""

            print(f"[{idx}/{total_count}] Converting{doc_flag} {src_rel} ({size_str}) ...", flush=True)

            parent_conn, child_conn = multiprocessing.Pipe()
            task["file_timeout"] = file_timeout
            proc = multiprocessing.Process(target=_convert_worker_target, args=(task, child_conn, ocr_policy, redo_ocr))
            proc.start()
            child_conn.close()

            active_jobs.append({
                "task": task,
                "proc": proc,
                "parent_conn": parent_conn,
                "start_time": time.time(),
                "index": idx
            })

        finished_jobs = []
        for job in active_jobs:
            proc = job["proc"]
            parent_conn = job["parent_conn"]
            task = job["task"]
            src_rel = task["src_rel"]
            dest_rel = task["dest_rel"]
            idx = job["index"]
            elapsed = time.time() - job["start_time"]

            if parent_conn.poll(0.02):
                try:
                    status, payload = parent_conn.recv()
                    parent_conn.close()
                    proc.join(timeout=1)
                    if status == "ok":
                        results[src_rel] = {
                            "success": True,
                            "markdown_body": payload["text"],
                            "ocr_applied": payload["ocr_applied"],
                            "derivative_path": payload.get("derivative_path"),
                            "derivative_sha256": payload.get("derivative_sha256"),
                            "conversion_method": payload.get("conversion_method"),
                            "potential_quality_loss": payload.get("potential_quality_loss"),
                            "ocr_policy": payload.get("ocr_policy"),
                            "new_src_sha256": payload.get("new_src_sha256"),
                            "new_src_size": payload.get("new_src_size"),
                            "new_src_mtime": payload.get("new_src_mtime"),
                            "error": None
                        }
                        ocr_flag = " [OCR]" if payload.get("ocr_applied") else ""
                        deriv_flag = f" [Derivat: {payload['conversion_method']}]" if payload.get("derivative_path") else ""
                        print(f"[{idx}/{total_count}] Fertig{ocr_flag}{deriv_flag}: {src_rel} -> {dest_rel}", flush=True)
                    elif status == "conversion_required":
                        results[src_rel] = {
                            "success": False,
                            "conversion_required": True,
                            "markdown_body": None,
                            "ocr_applied": False,
                            "error": payload.get("error") if isinstance(payload, dict) else payload,
                            "sha256": payload.get("sha256") if isinstance(payload, dict) else task.get("src_sha256")
                        }
                        print(f"[{idx}/{total_count}] [conversion_required] {src_rel}: {results[src_rel]['error']}", flush=True)
                    else:
                        results[src_rel] = {"success": False, "markdown_body": None, "ocr_applied": False, "error": payload}
                        print(f"[{idx}/{total_count}] Error converting {src_rel}: {payload}", flush=True)
                except Exception as e:
                    results[src_rel] = {"success": False, "markdown_body": None, "ocr_applied": False, "error": str(e)}
                    print(f"[{idx}/{total_count}] Error reading output for {src_rel}: {e}", flush=True)
                finished_jobs.append(job)
            elif not proc.is_alive():
                if parent_conn.poll(0.01):
                    try:
                        status, payload = parent_conn.recv()
                        parent_conn.close()
                        proc.join()
                        if status == "ok":
                            results[src_rel] = {
                                "success": True,
                                "markdown_body": payload["text"],
                                "ocr_applied": payload["ocr_applied"],
                                "derivative_path": payload.get("derivative_path"),
                                "derivative_sha256": payload.get("derivative_sha256"),
                                "conversion_method": payload.get("conversion_method"),
                                "potential_quality_loss": payload.get("potential_quality_loss"),
                                "ocr_policy": payload.get("ocr_policy"),
                                "new_src_sha256": payload.get("new_src_sha256"),
                                "new_src_size": payload.get("new_src_size"),
                                "new_src_mtime": payload.get("new_src_mtime"),
                                "error": None
                            }
                            ocr_flag = " [OCR]" if payload.get("ocr_applied") else ""
                            deriv_flag = f" [Derivat: {payload['conversion_method']}]" if payload.get("derivative_path") else ""
                            print(f"[{idx}/{total_count}] Fertig{ocr_flag}{deriv_flag}: {src_rel} -> {dest_rel}", flush=True)
                        elif status == "conversion_required":
                            results[src_rel] = {
                                "success": False,
                                "conversion_required": True,
                                "markdown_body": None,
                                "ocr_applied": False,
                                "error": payload.get("error") if isinstance(payload, dict) else payload,
                                "sha256": payload.get("sha256") if isinstance(payload, dict) else task.get("src_sha256")
                            }
                            print(f"[{idx}/{total_count}] [conversion_required] {src_rel}: {results[src_rel]['error']}", flush=True)
                        else:
                            results[src_rel] = {"success": False, "markdown_body": None, "ocr_applied": False, "error": payload}
                            print(f"[{idx}/{total_count}] Error converting {src_rel}: {payload}", flush=True)
                    except Exception as e:
                        results[src_rel] = {"success": False, "markdown_body": None, "ocr_applied": False, "error": str(e)}
                        print(f"[{idx}/{total_count}] Error converting {src_rel}: {e}", flush=True)
                else:
                    parent_conn.close()
                    proc.join()
                    err_msg = f"Worker process terminated unexpectedly (exit code {proc.exitcode})."
                    results[src_rel] = {"success": False, "markdown_body": None, "ocr_applied": False, "error": err_msg}
                    print(f"[{idx}/{total_count}] Error converting {src_rel}: {err_msg}", flush=True)
                finished_jobs.append(job)
            elif elapsed > file_timeout:
                print(f"[TIMEOUT] Konvertierung fuer {src_rel} nach {file_timeout}s abgebrochen! Skipping.", file=sys.stderr, flush=True)
                try:
                    proc.terminate()
                    proc.join(timeout=2)
                    if proc.is_alive():
                        proc.kill()
                        proc.join()
                except Exception as e:
                    print(f"Warning terminating process for {src_rel}: {e}", file=sys.stderr, flush=True)
                parent_conn.close()
                results[src_rel] = {
                    "success": False,
                    "conversion_required": task.get("is_doc", False),
                    "markdown_body": None,
                    "ocr_applied": False,
                    "error": f"Timeout nach {file_timeout}s"
                }
                finished_jobs.append(job)

        for fj in finished_jobs:
            active_jobs.remove(fj)

        time.sleep(0.05)

    return results

version_patterns = [
    re.compile(r"\bv(\d+(?:[\._]\d+)*)\b", re.IGNORECASE),
    re.compile(r"_v(\d+(?:[\._]\d+)*)", re.IGNORECASE),
    re.compile(r"[-_](final|def)\b", re.IGNORECASE)
]

def get_file_version(filename):
    for pattern in version_patterns:
        match = pattern.search(filename)
        if match:
            return match.group(1).upper()
    return "N/A"

def find_workspace_root(start_dir=None):
    env_root = os.environ.get("CLOUD_ATLAS_WORKSPACE_ROOT")
    if env_root and os.path.exists(env_root):
        return os.path.abspath(env_root)

    current = os.path.abspath(start_dir or os.getcwd())
    while True:
        try:
            if (os.path.exists(os.path.join(current, ".git")) or
                os.path.exists(os.path.join(current, "AGENTS.md")) or
                os.path.exists(os.path.join(current, ".workspace-root"))):
                return current
            try:
                for entry in os.scandir(current):
                    if entry.is_file() and entry.name.endswith(".code-workspace"):
                        return current
            except (PermissionError, OSError):
                pass
        except (PermissionError, OSError):
            pass

        parent = os.path.dirname(current)
        if parent == current:
            break
        current = parent
    return os.path.abspath(start_dir or os.getcwd())


def resolve_cloud_metadata_policy(project_or_topic, storage_config, identifier, is_topic):
    """Return deterministic metadata policy from declared configuration.

    Storage values intentionally override project/topic values.  The package
    defaults are stable and do not inspect the host, current user, or clock.
    """
    project_or_topic = project_or_topic if isinstance(project_or_topic, dict) else {}
    storage_config = storage_config if isinstance(storage_config, dict) else {}
    owner_prefix = "topic" if is_topic else "project"
    return {
        "data_classification": (
            storage_config.get("data_classification")
            or project_or_topic.get("data_classification")
            or "internal"
        ),
        "retention_class": (
            storage_config.get("retention_class")
            or project_or_topic.get("retention_class")
            or "project-lifecycle"
        ),
        "owner": (
            storage_config.get("owner")
            or project_or_topic.get("owner")
            or f"{owner_prefix}:{identifier}"
        ),
    }


def resolve_all_sync_configs(workspace_root, project_id, force_topic=False, storage_id=None):
    projects_file = os.path.normpath(os.path.join(workspace_root, "memory/references/projects/projects.json"))
    project_meta = None
    is_topic = force_topic

    if not is_topic and os.path.exists(projects_file):
        try:
            with open(projects_file, "r", encoding="utf-8") as f:
                projects = json.load(f)
                for p in projects:
                    if p.get("id") == project_id:
                        project_meta = p
                        break
        except Exception as e:
            print(f"Warning: Could not read projects.json: {e}")

    topics_file = os.path.normpath(os.path.join(workspace_root, "memory/references/topics/topics.json"))
    topic_meta = None
    if not project_meta and os.path.exists(topics_file):
        try:
            with open(topics_file, "r", encoding="utf-8") as f:
                topics = json.load(f)
                for t in topics:
                    if t.get("id") == project_id:
                        topic_meta = t
                        is_topic = True
                        break
        except Exception as e:
            print(f"Warning: Could not read topics.json: {e}")

    meta = project_meta or topic_meta
    title = project_id.upper()
    if meta:
        title = meta.get("kuerzel") or meta.get("title") or title

    cloud_sync = meta.get("cloud_sync") if meta else None
    configs = {}

    if not cloud_sync:
        configs["default"] = {
            "title": title,
            "scan_dir": f"data/cloud/{project_id.upper()}" if meta and meta.get("kuerzel") else f"data/cloud/{project_id}",
            "output_json": f"memory/cloud/{'topics' if is_topic else 'projects'}/{project_id}/filemap.json",
            "output_md": f"memory/cloud/{'topics' if is_topic else 'projects'}/{project_id}/filemap.md",
            "output_dir": f"memory/cloud/{'topics' if is_topic else 'projects'}/{project_id}",
            "is_topic": is_topic,
            **resolve_cloud_metadata_policy(meta, {}, project_id, is_topic),
        }
        kuerzel = (meta.get("kuerzel") or project_id) if meta else project_id
        folder_candidates = [
            os.path.join("data", "cloud", kuerzel),
            os.path.join("data", "cloud", project_id),
            os.path.join("data", "cloud", project_id.upper()),
            os.path.join("data", "cloud", project_id.lower())
        ]
        for candidate in folder_candidates:
            if os.path.exists(os.path.join(workspace_root, candidate)):
                configs["default"]["scan_dir"] = candidate.replace("\\", "/")
                break
    else:
        for sid, sconfig in cloud_sync.items():
            if not isinstance(sconfig, dict):
                print(f"Warning: Configuration for storage '{sid}' in project '{project_id}' must be a dictionary. Skipping.")
                continue
            scan_dir = sconfig.get("scan_dir") or sconfig.get("cloud_dir")
            output_json = sconfig.get("output_json") or sconfig.get("filemap_json")
            output_md = sconfig.get("output_md") or sconfig.get("filemap_md")
            output_dir = sconfig.get("output_dir") or sconfig.get("cloud_mirror_dir")

            suffix = f"-{sid}" if sid != "default" else ""
            stitle = f"{title} ({sid})" if sid != "default" else title

            configs[sid] = {
                "title": stitle,
                "scan_dir": scan_dir or (f"data/cloud/{project_id.upper()}" if meta and meta.get("kuerzel") else f"data/cloud/{project_id}"),
                "output_json": output_json or f"memory/cloud/{'topics' if is_topic else 'projects'}/{project_id}/filemap{suffix}.json",
                "output_md": output_md or f"memory/cloud/{'topics' if is_topic else 'projects'}/{project_id}/filemap{suffix}.md",
                "output_dir": output_dir or (f"memory/cloud/{'topics' if is_topic else 'projects'}/{project_id}" + (f"/{sid}" if sid != "default" else "")),
                "is_topic": is_topic,
                **resolve_cloud_metadata_policy(meta, sconfig, project_id, is_topic),
            }

    if storage_id:
        if storage_id in configs:
            return {storage_id: configs[storage_id]}
        else:
            print(f"Warning: Storage ID '{storage_id}' not found in configuration.")
            return {}

    return configs


AUTOMATED_METADATA_KEYS = {
    "version", "mtime", "size", "sha256",
    "markdown_mirror", "derivative",
    "conversion_status", "conversion_error", "ocr_applied", "ocr_policy"
}

def is_protected_output_file(rel_workspace_path, resolved_config=None):
    """Return True if a file should never be touched by orphaned mirror cleanup."""
    if not isinstance(rel_workspace_path, str):
        return False
    normalized = normalize_workspace_relative_path(rel_workspace_path)
    if not normalized:
        return False
    basename = PurePosixPath(normalized).name.lower()

    # Common protected filemap, manifest and index files
    if basename in ("filemap.md", "filemap.json", "index.md", "readme.md", "manifest.json"):
        return True
    if basename.startswith("filemap") and (basename.endswith(".json") or basename.endswith(".md")):
        return True
    if basename.startswith("manifest") and (basename.endswith(".json") or basename.endswith(".md")):
        return True

    if resolved_config:
        out_json = normalize_workspace_relative_path(resolved_config.get("output_json"))
        out_md = normalize_workspace_relative_path(resolved_config.get("output_md"))
        if normalized in (out_json, out_md):
            return True
    return False

def merge_curated_metadata(existing_entry, new_entry):
    """
    Merge newly computed automatic fields into existing metadata,
    ensuring manually maintained descriptions and custom keys are never lost.
    """
    if not isinstance(existing_entry, dict):
        return new_entry

    result = dict(new_entry)

    # Preserve manual description if existing was not default/empty
    existing_desc = existing_entry.get("description")
    if existing_desc and existing_desc != "-":
        result["description"] = existing_desc
    elif "description" not in result:
        result["description"] = existing_desc or "-"

    # Preserve all other non-automated keys
    for k, v in existing_entry.items():
        if k not in AUTOMATED_METADATA_KEYS and k not in result:
            result[k] = v

    return result

def parse_markdown_file(filepath):
    """Read canonical or legacy mirror frontmatter without rewriting either.

    Callers needing provenance should use ``mirror_source_sha256`` below: it
    maps the legacy ``original_sha256`` field to the canonical source semantic.
    Raw legacy keys remain exposed for compatibility with existing consumers.
    """
    safe_path = get_safe_path(filepath)
    if not os.path.exists(safe_path):
        return None, ""
    try:
        with open(safe_path, "r", encoding="utf-8", errors="replace") as f:
            content = f.read()
        if content.startswith("---"):
            parts = content.split("---", 2)
            if len(parts) >= 3:
                frontmatter_str = parts[1]
                body = parts[2]
                metadata = {}
                for line in frontmatter_str.strip().split("\n"):
                    if ":" in line:
                        k, v = line.split(":", 1)
                        key = k.strip()
                        value = v.strip().strip('"').strip("'")
                        if key == "instructions_are_data" and value.lower() in {"true", "false"}:
                            metadata[key] = value.lower() == "true"
                        else:
                            metadata[key] = value
                if body.startswith("\r\n\r\n"):
                    body = body[4:]
                elif body.startswith("\n\n"):
                    body = body[2:]
                return metadata, body
    except Exception as e:
        print(f"Warning parsing markdown {filepath}: {e}")
    return None, ""


def mirror_source_sha256(metadata):
    """Return source hash from canonical or legacy frontmatter semantics."""
    if not isinstance(metadata, dict):
        return None
    return metadata.get("source_sha256") or metadata.get("original_sha256")


def mirror_ocr_applied(metadata, filemap_entry):
    """Read OCR state from the legacy mirror or the authoritative filemap."""
    if isinstance(filemap_entry, dict) and filemap_entry.get("ocr_applied") is True:
        return True
    if not isinstance(metadata, dict):
        return False
    return metadata.get("ocr_applied") in (True, "true")

def atomic_write_text(filepath, content):
    """Atomically replace a UTF-8 text file using a flushed sibling temporary file."""
    target_path = Path(os.path.abspath(filepath))
    target_path.parent.mkdir(parents=True, exist_ok=True)
    temp_fd, temp_name = tempfile.mkstemp(
        prefix=f".{target_path.name}.",
        suffix=".tmp",
        dir=str(target_path.parent),
    )
    try:
        with os.fdopen(temp_fd, "w", encoding="utf-8", errors="replace", newline="\n") as temp_file:
            temp_fd = None
            temp_file.write(content)
            temp_file.flush()
            os.fsync(temp_file.fileno())
        os.replace(get_safe_path(temp_name), get_safe_path(str(target_path)))
    finally:
        if temp_fd is not None:
            try:
                os.close(temp_fd)
            except OSError:
                pass
        try:
            os.unlink(get_safe_path(temp_name))
        except OSError:
            pass


def write_json_file(filepath, data):
    """Serialize JSON and atomically replace the destination file."""
    atomic_write_text(filepath, json.dumps(data, indent=2, ensure_ascii=False))


def write_markdown_file(filepath, metadata, body):
    """Atomically write one canonical Cloud-zone Markdown mirror."""
    if not isinstance(metadata, dict):
        raise ValueError("canonical cloud frontmatter must be a dictionary")
    unexpected = set(metadata) - CANONICAL_CLOUD_FRONTMATTER_KEYS
    missing = REQUIRED_CLOUD_FRONTMATTER_KEYS - set(metadata)
    if unexpected:
        raise ValueError(f"non-canonical cloud frontmatter keys: {sorted(unexpected)}")
    if missing:
        raise ValueError(f"missing canonical cloud frontmatter keys: {sorted(missing)}")

    frontmatter_lines = ["---"]
    for k, v in metadata.items():
        if v is not None:
            if isinstance(v, bool):
                frontmatter_lines.append(f"{k}: {'true' if v else 'false'}")
            else:
                val_escaped = str(v).replace('"', '\\"')
                frontmatter_lines.append(f'{k}: "{val_escaped}"')
    frontmatter_lines.append("---")

    atomic_write_text(filepath, "\n".join(frontmatter_lines) + "\n\n" + (body or ""))

def main():
    args = parse_args()
    workspace_root = os.path.abspath(args.workspace_root) if args.workspace_root else find_workspace_root()

    project_id = args.project_id or args.topic_id
    is_topic = args.topic or (args.topic_id is not None)

    ocr_policy = args.ocr_policy
    if args.no_ocr:
        ocr_policy = "disabled"
    redo_ocr = args.redo_ocr

    configs = resolve_all_sync_configs(workspace_root, project_id, is_topic, args.storage_id)

    if not configs:
        print(f"Error: No cloud sync configurations resolved for ID '{project_id}'.")
        return

    for sid, resolved in configs.items():
        print(f"\n--- Konvertiere Dokumente fuer Storage: {sid} ---")
        cloud_dir = args.cloud_dir or resolved["scan_dir"]
        output_dir = args.output_dir or resolved["output_dir"]
        filemap_json = args.filemap_json or resolved["output_json"]

        cloud_path_abs = os.path.normpath(os.path.join(workspace_root, cloud_dir))
        output_path_abs = os.path.normpath(os.path.join(workspace_root, output_dir))
        filemap_json_abs = os.path.normpath(os.path.join(workspace_root, filemap_json))

        extensions = [ext.strip().lower() for ext in args.extensions.split(",")]

        if not os.path.exists(cloud_path_abs):
            print(f"Error: Cloud directory '{cloud_path_abs}' does not exist.")
            continue

        filemap_data = {}
        if os.path.exists(filemap_json_abs):
            try:
                with open(filemap_json_abs, "r", encoding="utf-8") as f:
                    filemap_data = json.load(f)
            except Exception as e:
                print(f"Warning: Could not read filemap.json: {e}")

        if not filemap_data:
            filemap_data = {
                "project": project_id,
                "project_title": resolved["title"],
                "files": {}
            }
        files_in_json = filemap_data.setdefault("files", {})

        processed_mirrors = set()
        processed_derivatives = set()
        now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        doc_converter = get_doc_converter()

        # Collect raw scanned cloud files
        raw_scanned_files = []
        for root, dirs, filenames in os.walk(cloud_path_abs):
            for f in filenames:
                ext = os.path.splitext(f)[1].lower()
                if ext not in extensions:
                    continue
                if f.startswith(".") or f.startswith("~$") or f.lower() == "desktop.ini":
                    continue

                src_abs = os.path.join(root, f)
                src_rel_workspace = os.path.relpath(src_abs, workspace_root).replace("\\", "/")
                rel_to_cloud = os.path.relpath(src_abs, cloud_path_abs)

                is_doc = (ext == ".doc")
                stat = os.stat(get_safe_path(src_abs))
                src_mtime = datetime.datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M:%S")
                src_version = get_file_version(f)
                src_sha256 = calculate_sha256(src_abs)

                raw_scanned_files.append({
                    "src_abs": src_abs,
                    "src_rel": src_rel_workspace,
                    "rel_to_cloud": rel_to_cloud,
                    "filename": f,
                    "ext": ext,
                    "is_doc": is_doc,
                    "stat": stat,
                    "src_mtime": src_mtime,
                    "src_version": src_version,
                    "src_sha256": src_sha256
                })

        current_scanned_paths = {item["src_rel"] for item in raw_scanned_files}

        # Build index of unmapped existing entries by sha256 to track renames/moves
        unmapped_by_sha256 = {}
        for old_path, old_info in files_in_json.items():
            if old_path not in current_scanned_paths and isinstance(old_info, dict) and old_info.get("sha256"):
                unmapped_by_sha256.setdefault(old_info["sha256"], []).append((old_path, old_info))

        new_files_in_json = {}
        candidate_tasks = []

        for item in raw_scanned_files:
            src_abs = item["src_abs"]
            src_rel_workspace = item["src_rel"]
            rel_to_cloud = item["rel_to_cloud"]
            f = item["filename"]
            ext = item["ext"]
            is_doc = item["is_doc"]
            is_pdf = (ext == ".pdf")
            stat = item["stat"]
            src_mtime = item["src_mtime"]
            src_version = item["src_version"]
            src_sha256 = item["src_sha256"]

            # Resolve existing metadata (by path or by sha256 rename)
            if src_rel_workspace in files_in_json:
                base_existing = files_in_json[src_rel_workspace]
            elif src_sha256 in unmapped_by_sha256 and len(unmapped_by_sha256[src_sha256]) > 0:
                old_path, base_existing = unmapped_by_sha256[src_sha256].pop(0)
                print(f"Uebernehme Metadaten von verschobener/umbenannter Datei {old_path} -> {src_rel_workspace}")
            else:
                base_existing = {}

            dest_rel_workspace = os.path.join(output_dir, os.path.splitext(rel_to_cloud)[0] + ".md").replace("\\", "/")
            dest_abs = os.path.normpath(os.path.join(workspace_root, dest_rel_workspace))

            derivative_rel_workspace = None
            derivative_abs = None
            if is_doc:
                derivative_rel_workspace = os.path.join(output_dir, "_derivatives", os.path.splitext(rel_to_cloud)[0] + ".docx").replace("\\", "/")
                derivative_abs = os.path.normpath(os.path.join(workspace_root, derivative_rel_workspace))
            elif is_pdf and ocr_policy == "local_derivative":
                derivative_rel_workspace = os.path.join(output_dir, "_derivatives", os.path.splitext(rel_to_cloud)[0] + ".pdf").replace("\\", "/")
                derivative_abs = os.path.normpath(os.path.join(workspace_root, derivative_rel_workspace))

            # Check converter availability for .doc files
            if is_doc and not doc_converter:
                print(f"[SKIP] Kein .doc-Konverter verfuegbar fuer {src_rel_workspace} -> Katalogisiert als 'conversion_required'.")
                raw_entry = {
                    "version": src_version,
                    "mtime": src_mtime,
                    "size": format_size(stat.st_size),
                    "sha256": src_sha256,
                    "description": "-",
                    "conversion_status": "conversion_required",
                    "conversion_error": "No suitable converter found (LibreOffice or Microsoft Word required for .doc conversion)"
                }
                new_files_in_json[src_rel_workspace] = merge_curated_metadata(base_existing, raw_entry)
                continue

            needs_conversion = args.force or redo_ocr or not os.path.exists(get_safe_path(dest_abs))
            if is_doc and derivative_abs and not os.path.exists(get_safe_path(derivative_abs)):
                needs_conversion = True
            if is_pdf and ocr_policy == "local_derivative" and derivative_abs:
                if os.path.exists(get_safe_path(dest_abs)):
                    existing_meta, _ = parse_markdown_file(dest_abs)
                    if existing_meta and mirror_ocr_applied(existing_meta, base_existing) and not os.path.exists(get_safe_path(derivative_abs)):
                        needs_conversion = True

            existing_meta = None
            if not needs_conversion and os.path.exists(get_safe_path(dest_abs)):
                existing_meta, body = parse_markdown_file(dest_abs)
                if existing_meta:
                    if mirror_source_sha256(existing_meta) != src_sha256:
                        needs_conversion = True
                    elif is_pdf and ocr_policy != "disabled" and not mirror_ocr_applied(existing_meta, base_existing) and is_image_based_pdf(src_abs, body):
                        needs_conversion = True
                    elif len(body.strip()) < 10 and not mirror_ocr_applied(existing_meta, base_existing) and not is_doc and not is_pdf:
                        needs_conversion = True
                else:
                    needs_conversion = True

            if not needs_conversion:
                processed_mirrors.add(dest_rel_workspace)
                if derivative_rel_workspace:
                    processed_derivatives.add(derivative_rel_workspace)

            candidate_tasks.append({
                "src_abs": src_abs,
                "src_rel": src_rel_workspace,
                "dest_abs": dest_abs,
                "dest_rel": dest_rel_workspace,
                "src_mtime": src_mtime,
                "src_version": src_version,
                "src_sha256": src_sha256,
                "stat": stat,
                "is_doc": is_doc,
                "is_pdf": is_pdf,
                "derivative_abs": derivative_abs,
                "derivative_rel": derivative_rel_workspace,
                "cloud_path_abs": cloud_path_abs,
                "ocr_policy": ocr_policy,
                "redo_ocr": redo_ocr,
                "needs_conversion": needs_conversion,
                "base_existing": base_existing,
                "metadata_policy": {
                    "data_classification": resolved["data_classification"],
                    "retention_class": resolved["retention_class"],
                    "owner": resolved["owner"],
                },
            })

        total_files = len(candidate_tasks)
        tasks_to_convert = []
        count_skipped = 0
        count_converted = 0
        count_failed = 0
        count_conversion_required = 0

        for task_idx, t in enumerate(candidate_tasks, 1):
            t["task_num"] = task_idx
            if t["needs_conversion"]:
                tasks_to_convert.append(t)
            else:
                count_skipped += 1
                src_rel_workspace = t["src_rel"]
                dest_rel_workspace = t["dest_rel"]
                base_existing = t.get("base_existing", {})

                updated_entry = {
                    "version": t["src_version"],
                    "mtime": t["src_mtime"],
                    "size": format_size(t['stat'].st_size),
                    "sha256": t["src_sha256"],
                    "description": "-",
                    "markdown_mirror": dest_rel_workspace,
                    "conversion_status": "converted"
                }
                if t.get("derivative_rel") and os.path.exists(get_safe_path(t["derivative_abs"])):
                    deriv_sha = base_existing.get("derivative", {}).get("sha256") or calculate_sha256(t["derivative_abs"])
                    deriv_format = "docx" if t.get("is_doc") else "pdf"
                    default_conv = "libreoffice-headless" if t.get("is_doc") else "ocrmypdf-derivative"
                    updated_entry["derivative"] = {
                        "path": t["derivative_rel"],
                        "sha256": deriv_sha,
                        "format": deriv_format,
                        "conversion_method": base_existing.get("derivative", {}).get("conversion_method") or default_conv,
                        "converted_at": base_existing.get("derivative", {}).get("converted_at") or now_str,
                        "potential_quality_loss": base_existing.get("derivative", {}).get("potential_quality_loss") or (
                            f"Konvertierung von binärem .doc (Word 97-2003) über {doc_converter or 'libreoffice-headless'} nach .docx." if t.get("is_doc") else "OCR-Textebene in durchsuchbarem PDF-Derivat erzeugt."
                        )
                    }
                new_files_in_json[src_rel_workspace] = merge_curated_metadata(base_existing, updated_entry)

        if tasks_to_convert:
            results = run_conversion_tasks(
                tasks_to_convert,
                file_timeout=args.file_timeout,
                max_jobs=args.jobs,
                ocr_policy=ocr_policy,
                redo_ocr=redo_ocr,
                total_count=total_files
            )

            for t in tasks_to_convert:
                src_rel_workspace = t["src_rel"]
                dest_rel_workspace = t["dest_rel"]
                dest_abs = t["dest_abs"]
                res = results.get(src_rel_workspace, {"success": False, "error": "Unknown error"})
                base_existing = t.get("base_existing", {})

                if res.get("success"):
                    count_converted += 1
                    file_mtime = t["src_mtime"]
                    file_size = t['stat'].st_size
                    try:
                        fresh_stat = os.stat(get_safe_path(t["src_abs"]))
                        file_mtime = datetime.datetime.fromtimestamp(fresh_stat.st_mtime).strftime("%Y-%m-%d %H:%M:%S")
                    except Exception:
                        pass

                    if res.get("new_src_sha256"):
                        t["src_sha256"] = res["new_src_sha256"]
                        file_size = res.get("new_src_size", file_size)
                        if res.get("new_src_mtime") is not None:
                            file_mtime = datetime.datetime.fromtimestamp(res["new_src_mtime"]).strftime("%Y-%m-%d %H:%M:%S")

                    file_entry = {
                        "version": t["src_version"],
                        "mtime": file_mtime,
                        "size": format_size(file_size),
                        "sha256": t["src_sha256"],
                        "description": "-",
                        "markdown_mirror": dest_rel_workspace,
                        "conversion_status": "converted"
                    }

                    if res.get("derivative_path"):
                        file_entry["derivative"] = {
                            "path": res["derivative_path"],
                            "sha256": res["derivative_sha256"],
                            "format": "docx" if t.get("is_doc") else "pdf",
                            "conversion_method": res["conversion_method"],
                            "converted_at": now_str,
                            "potential_quality_loss": res.get("potential_quality_loss")
                        }
                        processed_derivatives.add(res["derivative_path"])

                    if res.get("ocr_applied"):
                        file_entry["ocr_applied"] = True
                        file_entry["ocr_policy"] = res.get("ocr_policy", ocr_policy)

                    # Safety check before writing mirror
                    assert_not_in_cloud_dir(dest_abs, cloud_path_abs)
                    source_uri = normalize_workspace_relative_path(src_rel_workspace)
                    if source_uri is None:
                        raise ValueError(f"source URI must be workspace-relative: {src_rel_workspace}")
                    metadata = build_cloud_artifact_metadata(
                        source_uri=source_uri,
                        source_sha256=t["src_sha256"],
                        artifact_sha256=calculate_markdown_payload_sha256(res["markdown_body"]),
                        converter=res.get("conversion_method") or "markitdown-direct",
                        data_classification=t["metadata_policy"]["data_classification"],
                        retention_class=t["metadata_policy"]["retention_class"],
                        owner=t["metadata_policy"]["owner"],
                        synced_at=datetime.datetime.now(datetime.timezone.utc),
                    )
                    write_markdown_file(dest_abs, metadata, res["markdown_body"])
                    processed_mirrors.add(dest_rel_workspace)
                    new_files_in_json[src_rel_workspace] = merge_curated_metadata(base_existing, file_entry)

                elif res.get("conversion_required") or t.get("is_doc"):
                    count_conversion_required += 1
                    err_msg = res.get("error", "Conversion failed")
                    file_entry = {
                        "version": t["src_version"],
                        "mtime": t["src_mtime"],
                        "size": format_size(t['stat'].st_size),
                        "sha256": t["src_sha256"],
                        "description": "-",
                        "conversion_status": "conversion_required",
                        "conversion_error": str(err_msg)
                    }
                    new_files_in_json[src_rel_workspace] = merge_curated_metadata(base_existing, file_entry)
                    print(f"Datei '{src_rel_workspace}' katalogisiert mit Status 'conversion_required': {err_msg}")
                else:
                    count_failed += 1

        print(f"\nZusammenfassung fuer {sid}: {count_converted} erfolgreich konvertiert, {count_skipped} aktuell (uebersprungen), {count_conversion_required} als 'conversion_required' markiert, {count_failed} fehlgeschlagen.")

        # Clean up orphaned markdown mirrors (where original file is deleted)
        if os.path.exists(output_path_abs):
            for root, dirs, filenames in os.walk(output_path_abs):
                rel_from_out = os.path.relpath(root, output_path_abs)
                if rel_from_out == "_derivatives" or rel_from_out.startswith("_derivatives" + os.sep):
                    continue
                for f in filenames:
                    if not f.endswith(".md"):
                        continue
                    mirror_abs = os.path.join(root, f)
                    mirror_rel_workspace = os.path.relpath(mirror_abs, workspace_root).replace("\\", "/")

                    # Never delete protected files like filemap.md, index.md, etc.
                    if is_protected_output_file(mirror_rel_workspace, resolved):
                        continue

                    if mirror_rel_workspace not in processed_mirrors:
                        print(f"Removing orphaned mirror: {mirror_rel_workspace}")
                        try:
                            os.remove(get_safe_path(mirror_abs))
                        except Exception as e:
                            print(f"Error removing {mirror_rel_workspace}: {e}")

                        for src_rel, info in list(new_files_in_json.items()):
                            if info.get("markdown_mirror") == mirror_rel_workspace:
                                del info["markdown_mirror"]

        # Clean up orphaned derivatives in _derivatives/
        derivatives_dir_abs = os.path.join(output_path_abs, "_derivatives")
        if os.path.exists(derivatives_dir_abs):
            for root, dirs, filenames in os.walk(derivatives_dir_abs):
                for f in filenames:
                    deriv_abs = os.path.join(root, f)
                    deriv_rel_workspace = os.path.relpath(deriv_abs, workspace_root).replace("\\", "/")
                    if deriv_rel_workspace not in processed_derivatives:
                        print(f"Removing orphaned derivative: {deriv_rel_workspace}")
                        try:
                            os.remove(get_safe_path(deriv_abs))
                        except Exception as e:
                            print(f"Error removing {deriv_rel_workspace}: {e}")
                        for src_rel, info in list(new_files_in_json.items()):
                            if info.get("derivative", {}).get("path") == deriv_rel_workspace:
                                del info["derivative"]

            # Clean up empty subdirectories in _derivatives
            for root, dirs, files in os.walk(derivatives_dir_abs, topdown=False):
                for d in dirs:
                    dir_path = os.path.join(root, d)
                    try:
                        if not os.listdir(get_safe_path(dir_path)):
                            print(f"Removing empty derivative subdirectory: {dir_path}")
                            os.rmdir(get_safe_path(dir_path))
                    except Exception as e:
                        print(f"Warning removing empty subdirectory {dir_path}: {e}")

        # Clean up empty subdirectories in output_path_abs
        if os.path.exists(output_path_abs):
            for root, dirs, files in os.walk(output_path_abs, topdown=False):
                for d in dirs:
                    dir_path = os.path.join(root, d)
                    try:
                        if not os.listdir(get_safe_path(dir_path)):
                            print(f"Removing empty subdirectory: {dir_path}")
                            os.rmdir(get_safe_path(dir_path))
                    except Exception as e:
                        print(f"Warning removing empty subdirectory {dir_path}: {e}")

        # Save filemap.json back with current active files
        filemap_data["files"] = new_files_in_json
        filemap_data["updated_at"] = now_str
        write_json_file(filemap_json_abs, filemap_data)
        print(f"Saved filemap JSON to {filemap_json_abs}")

if __name__ == "__main__":
    main()
