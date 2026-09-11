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
import convert_cloud_docs as MODULE


class ConvertCloudDocsContractTests(unittest.TestCase):
    keys = {"action", "success", "state", "message", "data", "error"}

    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        (self.root / "AGENTS.md").write_text("# Test workspace\n", encoding="utf-8")
        (self.root / ".workspace-root").touch()
        self.projects_dir = self.root / "memory" / "references" / "projects"
        self.projects_dir.mkdir(parents=True)
        self.old_mock_converter = os.environ.get("CLOUD_ATLAS_DOC_CONVERTER_MOCK")
        os.environ["CLOUD_ATLAS_DOC_CONVERTER_MOCK"] = "mock:test"

    def tearDown(self):
        if self.old_mock_converter is None:
            os.environ.pop("CLOUD_ATLAS_DOC_CONVERTER_MOCK", None)
        else:
            os.environ["CLOUD_ATLAS_DOC_CONVERTER_MOCK"] = self.old_mock_converter
        self.temporary_directory.cleanup()

    def configure(self, storage_ids=("default",), missing_storage_ids=(), document_extension=".doc"):
        cloud_sync = {}
        for storage_id in storage_ids:
            name = storage_id.upper()
            if storage_id not in missing_storage_ids:
                cloud_directory = self.root / "data" / "cloud" / name
                cloud_directory.mkdir(parents=True)
                (cloud_directory / f"{storage_id}{document_extension}").write_bytes(b"\xd0\xcf\x11\xe0" + storage_id.encode("ascii"))
            suffix = "" if storage_id == "default" else f"-{storage_id}"
            cloud_sync[storage_id] = {
                "scan_dir": f"data/cloud/{name}",
                "output_dir": f"memory/cloud/projects/example/{storage_id}",
                "output_json": f"memory/cloud/projects/example/filemap{suffix}.json",
            }
        (self.projects_dir / "projects.json").write_text(
            json.dumps([{"id": "example", "title": "Example", "cloud_sync": cloud_sync}]),
            encoding="utf-8",
        )

    def run_main(self, *arguments):
        stdout = io.StringIO()
        stderr = io.StringIO()
        with mock.patch.object(MODULE.sys, "argv", ["convert_cloud_docs.py", *arguments]), contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            exit_code = MODULE.main()
        return exit_code, stdout.getvalue(), stderr.getvalue()

    def json_result(self, stdout):
        result = json.loads(stdout)
        self.assertEqual(self.keys, set(result))
        self.assertEqual("convert_cloud_docs", result["action"])
        return result

    def test_json_stdout_is_one_envelope_and_progress_moves_to_stderr(self):
        self.configure()

        exit_code, stdout, stderr = self.run_main(
            "--project-id", "example", "--workspace-root", str(self.root), "--json"
        )

        result = self.json_result(stdout)
        self.assertEqual(0, exit_code)
        self.assertTrue(result["success"])
        self.assertEqual("Completed", result["state"])
        self.assertEqual("project", result["data"]["target"]["kind"])
        self.assertEqual(1, result["data"]["storages"][0]["counts"]["converted"])
        self.assertIn("Konvertiere Dokumente", stderr)
        self.assertIn("Zusammenfassung", stderr)
        self.assertIn("Saved filemap JSON", stderr)
        self.assertNotIn("Konvertiere Dokumente", stdout)

    def test_human_mode_keeps_progress_on_stdout(self):
        self.configure()

        exit_code, stdout, stderr = self.run_main(
            "--project-id", "example", "--workspace-root", str(self.root)
        )

        self.assertEqual(0, exit_code)
        self.assertIn("Konvertiere Dokumente", stdout)
        self.assertIn("Zusammenfassung", stdout)
        self.assertFalse(stdout.lstrip().startswith("{"))
        self.assertEqual("", stderr)

    def test_json_config_error_is_canonical_and_nonzero(self):
        self.configure()
        exit_code, stdout, stderr = self.run_main(
            "--project-id", "example", "--storage-id", "missing", "--workspace-root", str(self.root), "--json"
        )

        result = self.json_result(stdout)
        self.assertEqual(1, exit_code)
        self.assertFalse(result["success"])
        self.assertEqual("Failed", result["state"])
        self.assertEqual("config", result["error"]["phase"])
        self.assertEqual([], result["data"]["storages"])
        self.assertIn("Storage ID 'missing'", stderr)

    def test_multistorage_write_failure_keeps_completed_and_partial_results(self):
        self.configure(("first", "second"))
        real_write_json = MODULE.write_json_file

        def fail_second_filemap(filepath, data):
            if Path(filepath).name == "filemap-second.json":
                raise OSError("simulated second filemap write failure")
            return real_write_json(filepath, data)

        with mock.patch.object(MODULE, "write_json_file", side_effect=fail_second_filemap):
            exit_code, stdout, stderr = self.run_main(
                "--project-id", "example", "--workspace-root", str(self.root), "--json"
            )

        result = self.json_result(stdout)
        self.assertEqual(1, exit_code)
        self.assertEqual("runtime", result["error"]["phase"])
        self.assertEqual("second", result["error"]["storage_id"])
        self.assertEqual("first", result["data"]["storages"][0]["storage_id"])
        self.assertEqual("second", result["data"]["partial_storage"]["storage_id"])
        self.assertEqual(1, result["data"]["partial_storage"]["counts"]["converted"])
        self.assertIn("Konvertiere Dokumente", stderr)

    def test_missing_storage_keeps_later_storage_result_and_fails_overall_json_run(self):
        self.configure(("missing", "valid"), missing_storage_ids=("missing",))

        exit_code, stdout, stderr = self.run_main(
            "--project-id", "example", "--workspace-root", str(self.root), "--json"
        )

        result = self.json_result(stdout)
        self.assertEqual(1, exit_code)
        self.assertFalse(result["success"])
        self.assertEqual("Failed", result["state"])
        self.assertEqual("storage", result["error"]["phase"])
        self.assertEqual("missing", result["data"]["storages"][0]["storage_id"])
        self.assertFalse(result["data"]["storages"][0]["success"])
        self.assertEqual("valid", result["data"]["storages"][1]["storage_id"])
        self.assertTrue(result["data"]["storages"][1]["success"])
        self.assertEqual(1, result["data"]["storages"][1]["counts"]["converted"])
        self.assertIn("Cloud directory", stderr)
        self.assertIn("Konvertiere Dokumente fuer Storage: valid", stderr)

    def test_human_missing_storage_continues_to_later_storage(self):
        self.configure(("missing", "valid"), missing_storage_ids=("missing",))

        exit_code, stdout, stderr = self.run_main(
            "--project-id", "example", "--workspace-root", str(self.root)
        )

        self.assertEqual(1, exit_code)
        self.assertIn("Cloud directory", stdout)
        self.assertIn("Konvertiere Dokumente fuer Storage: valid", stdout)
        self.assertIn("Saved filemap JSON", stdout)
        self.assertEqual("", stderr)

    def test_actual_file_conversion_failure_fails_storage_and_envelope_after_filemap_write(self):
        self.configure(document_extension=".pdf")

        def failed_conversion(tasks, **_kwargs):
            return {
                task["src_rel"]: {"success": False, "error": "simulated converter failure"}
                for task in tasks
            }

        with mock.patch.object(MODULE, "run_conversion_tasks", side_effect=failed_conversion):
            exit_code, stdout, stderr = self.run_main(
                "--project-id", "example", "--workspace-root", str(self.root), "--no-ocr", "--json"
            )

        result = self.json_result(stdout)
        storage = result["data"]["storages"][0]
        self.assertEqual(1, exit_code)
        self.assertFalse(result["success"])
        self.assertEqual("Failed", result["state"])
        self.assertEqual("storage", result["error"]["phase"])
        self.assertFalse(storage["success"])
        self.assertEqual(1, storage["counts"]["failed"])
        self.assertEqual("conversion", storage["error"]["phase"])
        self.assertTrue((self.root / "memory" / "cloud" / "projects" / "example" / "filemap.json").is_file())
        self.assertIn("Zusammenfassung", stderr)
        self.assertNotIn("Zusammenfassung", stdout)

    def test_optional_doc_converter_is_conversion_required_not_completed(self):
        self.configure(document_extension=".doc")
        os.environ["CLOUD_ATLAS_DOC_CONVERTER_MOCK"] = "none"

        exit_code, stdout, _ = self.run_main(
            "--project-id", "example", "--workspace-root", str(self.root), "--json"
        )

        result = self.json_result(stdout)
        storage = result["data"]["storages"][0]
        self.assertEqual(1, exit_code)
        self.assertFalse(result["success"])
        self.assertEqual("ConversionRequired", result["state"])
        self.assertEqual("ConversionRequired", result["error"]["type"])
        self.assertIn("requires attention", result["message"])
        self.assertNotIn("optional capabilities", result["message"])
        self.assertEqual(1, storage["counts"]["conversion_required"])
        self.assertFalse(storage["success"])
        requirement = result["error"]["requirements"][0]
        self.assertEqual("doc_converter", requirement["capability"])
        self.assertEqual("default", requirement["storage_id"])

    def test_conversion_required_storage_does_not_stop_later_storage(self):
        self.configure(("needs_converter", "valid"), document_extension=".doc")
        os.environ["CLOUD_ATLAS_DOC_CONVERTER_MOCK"] = "none"
        (self.root / "data" / "cloud" / "VALID" / "valid.doc").unlink()
        (self.root / "data" / "cloud" / "VALID" / "valid.docx").write_bytes(b"valid")

        def converted(tasks, **_kwargs):
            return {
                task["src_rel"]: {
                    "success": True,
                    "markdown_body": "Converted valid storage document.",
                    "ocr_applied": False,
                    "derivative_path": None,
                    "derivative_sha256": None,
                    "conversion_method": "markitdown-direct",
                    "potential_quality_loss": None,
                    "ocr_policy": "disabled",
                    "new_src_sha256": None,
                    "new_src_size": None,
                    "new_src_mtime": None,
                }
                for task in tasks
            }

        with mock.patch.object(MODULE, "run_conversion_tasks", side_effect=converted):
            exit_code, stdout, _ = self.run_main(
                "--project-id", "example", "--workspace-root", str(self.root), "--json"
            )

        result = self.json_result(stdout)
        self.assertEqual(1, exit_code)
        self.assertEqual("ConversionRequired", result["state"])
        self.assertEqual(2, len(result["data"]["storages"]))
        self.assertEqual(1, result["data"]["storages"][0]["counts"]["conversion_required"])
        self.assertTrue(result["data"]["storages"][1]["success"])
        self.assertEqual(1, result["data"]["storages"][1]["counts"]["converted"])

    def test_missing_markitdown_is_a_typed_conversion_requirement(self):
        source = self.root / "source.docx"
        source.write_bytes(b"placeholder")
        task = {
            "src_abs": str(source),
            "src_sha256": "a" * 64,
            "is_doc": False,
            "is_pdf": False,
        }
        parent, child = MODULE.multiprocessing.Pipe()
        with mock.patch.object(
            MODULE,
            "convert_to_markdown_raw",
            side_effect=MODULE.ConversionRequiredError("markitdown", "MarkItDown is unavailable."),
        ):
            MODULE._convert_worker_target(task, child)
        status, payload = parent.recv()
        parent.close()

        self.assertEqual("conversion_required", status)
        self.assertEqual("markitdown", payload["capability"])
        self.assertEqual("MarkItDown is unavailable.", payload["error"])
        self.assertNotIn("pip install", payload["error"])

    def test_missing_ocr_cli_prerequisites_are_deterministic(self):
        source = self.root / "scan.pdf"
        source.write_bytes(b"%PDF-1.4\nplaceholder\n%%EOF")
        output = self.root / "derivative.pdf"

        with mock.patch.object(MODULE.shutil, "which", return_value=None):
            success, message = MODULE.run_ocr_on_pdf(str(source), str(output))
        self.assertFalse(success)
        self.assertEqual("OCRmyPDF CLI is unavailable (capability: ocrmypdf).", message)

        with mock.patch.object(MODULE.shutil, "which", side_effect=["/tools/ocrmypdf", None]):
            success, message = MODULE.run_ocr_on_pdf(str(source), str(output))
        self.assertFalse(success)
        self.assertEqual("Tesseract CLI is unavailable (capability: tesseract).", message)

    def test_ocr_runtime_preflight_requires_deu_and_hocr(self):
        missing_deu = mock.Mock(returncode=0, stdout=b'List of available languages in "C:/tessdata/" (1):\neng\n', stderr=b"")
        with mock.patch.object(MODULE.shutil, "which", side_effect=["/tools/ocrmypdf", "/tools/tesseract"]), \
             mock.patch.object(MODULE.subprocess, "run", return_value=missing_deu):
            with self.assertRaisesRegex(MODULE.ConversionRequiredError, "language 'deu' is unavailable") as raised:
                MODULE.ocr_runtime_preflight()
        self.assertEqual("tesseract_deu", raised.exception.capability)

        languages = mock.Mock(returncode=0, stdout=b'List of available languages in "C:/tessdata/" (2):\ndeu\neng\n', stderr=b"")
        missing_hocr = mock.Mock(returncode=1, stdout=b"", stderr=b"read_params_file: Can't open hocr")
        with mock.patch.object(MODULE.shutil, "which", side_effect=["/tools/ocrmypdf", "/tools/tesseract"]), \
             mock.patch.object(MODULE.subprocess, "run", side_effect=[languages, missing_hocr]):
            with self.assertRaisesRegex(MODULE.ConversionRequiredError, "hOCR configuration is unavailable") as raised:
                MODULE.ocr_runtime_preflight()
        self.assertEqual("tesseract_hocr", raised.exception.capability)

        def successful_hocr(command, **_kwargs):
            if command[1] == "--list-langs":
                return languages
            Path(command[2] + ".hocr").write_text("<html><body>hOCR probe</body></html>", encoding="utf-8")
            return mock.Mock(returncode=0, stdout=b"", stderr=b"")

        with mock.patch.object(MODULE.shutil, "which", side_effect=["/tools/ocrmypdf", "/tools/tesseract"]), \
             mock.patch.object(MODULE.subprocess, "run", side_effect=successful_hocr):
            self.assertIsNone(MODULE.ocr_runtime_preflight())

    def test_ocr_runtime_preflight_is_conversion_required_before_workers(self):
        self.configure(document_extension=".pdf")
        requirement = MODULE.ConversionRequiredError(
            "tesseract_hocr", "Tesseract hOCR configuration is unavailable (capability: tesseract_hocr)."
        )
        with mock.patch.object(MODULE, "convert_to_markdown_raw", return_value=""), \
             mock.patch.object(MODULE, "ocr_runtime_preflight", side_effect=requirement) as preflight, \
             mock.patch.object(MODULE, "run_conversion_tasks") as workers:
            exit_code, stdout, _ = self.run_main(
                "--project-id", "example", "--workspace-root", str(self.root), "--json"
            )

        result = self.json_result(stdout)
        self.assertEqual(1, exit_code)
        self.assertEqual("ConversionRequired", result["state"])
        self.assertEqual("ConversionRequired", result["error"]["type"])
        self.assertEqual("tesseract_hocr", result["error"]["requirements"][0]["capability"])
        self.assertIn("hOCR configuration", result["error"]["requirements"][0]["message"])
        preflight.assert_called_once()
        workers.assert_not_called()

    def test_ocr_preflight_does_not_block_digital_pdf(self):
        self.configure(document_extension=".pdf")

        def converted(tasks, **_kwargs):
            return {
                task["src_rel"]: {
                    "success": True,
                    "markdown_body": "Digital source.",
                    "ocr_applied": False,
                    "derivative_path": None,
                    "derivative_sha256": None,
                    "conversion_method": "markitdown-direct",
                    "potential_quality_loss": None,
                    "ocr_policy": "local_derivative",
                    "new_src_sha256": None,
                    "new_src_size": None,
                    "new_src_mtime": None,
                }
                for task in tasks
            }

        with mock.patch.object(MODULE, "is_image_based_pdf", return_value=False), \
             mock.patch.object(MODULE, "ocr_runtime_preflight") as preflight, \
             mock.patch.object(MODULE, "run_conversion_tasks", side_effect=converted):
            exit_code, stdout, _ = self.run_main(
                "--project-id", "example", "--workspace-root", str(self.root), "--json"
            )

        result = self.json_result(stdout)
        self.assertEqual(0, exit_code)
        self.assertTrue(result["success"])
        preflight.assert_not_called()

    def test_digital_pdf_with_logo_and_text_bypasses_failed_ocr_preflight(self):
        """A logo must not turn an otherwise readable digital PDF into an OCR candidate."""
        self.configure(document_extension=".pdf")
        source = self.root / "data" / "cloud" / "DEFAULT" / "default.pdf"
        source.write_bytes(
            b"%PDF-1.4\n/Type /Font\n/FontDescriptor\n"
            b"/Subtype /Image\nBT /F1 12 Tf (Digital notice with logo) Tj ET\n%%EOF"
        )

        def converted(tasks, **_kwargs):
            return {
                task["src_rel"]: {
                    "success": True,
                    "markdown_body": task["preflight_raw_text"],
                    "ocr_applied": False,
                    "derivative_path": None,
                    "derivative_sha256": None,
                    "conversion_method": "markitdown-direct",
                    "potential_quality_loss": None,
                    "ocr_policy": "local_derivative",
                    "new_src_sha256": None,
                    "new_src_size": None,
                    "new_src_mtime": None,
                }
                for task in tasks
            }

        extracted = "This is enough extracted digital text to avoid OCR for the notice."
        requirement = MODULE.ConversionRequiredError("tesseract_hocr", "hOCR deliberately unavailable")
        with mock.patch.object(MODULE, "convert_to_markdown_raw", return_value=extracted) as extract, \
             mock.patch.object(MODULE, "ocr_runtime_preflight", side_effect=requirement) as preflight, \
             mock.patch.object(MODULE, "run_conversion_tasks", side_effect=converted):
            exit_code, stdout, _ = self.run_main(
                "--project-id", "example", "--workspace-root", str(self.root), "--json"
            )

        result = self.json_result(stdout)
        self.assertEqual(0, exit_code)
        self.assertTrue(result["success"])
        self.assertEqual(1, result["data"]["storages"][0]["counts"]["converted"])
        extract.assert_called_once_with(str(source))
        preflight.assert_not_called()

    def test_ghostscript_prerequisite_is_typed_deterministically(self):
        self.assertEqual(
            "ghostscript",
            MODULE.conversion_capability("Ghostscript is unavailable for OCR processing."),
        )

    def test_json_argument_error_is_canonical(self):
        exit_code, stdout, _ = self.run_main("--json")

        result = self.json_result(stdout)
        self.assertEqual(2, exit_code)
        self.assertEqual("arguments", result["error"]["phase"])


if __name__ == "__main__":
    unittest.main()
