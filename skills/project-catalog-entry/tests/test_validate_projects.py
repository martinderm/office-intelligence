from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


SKILL_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = SKILL_ROOT.parents[1]
SCRIPT_PATH = SKILL_ROOT / "scripts" / "validate_projects.py"
FIXTURE_PATH = Path(__file__).parent / "fixtures" / "valid_eu_catalog.json"


def load_validator():
    spec = importlib.util.spec_from_file_location("validate_projects", SCRIPT_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


validator = load_validator()


class ValidateProjectsTests(unittest.TestCase):
    def fixture(self):
        return json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))

    def project(self):
        return self.fixture()["projects"][0]

    def test_formal_schema_is_parseable(self):
        schema_path = SKILL_ROOT / "references" / "projects.schema.json"
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        self.assertEqual(schema["$defs"]["project"]["properties"]["schema_version"]["const"], 3)

    def test_valid_eu_structure_and_object_root(self):
        self.assertEqual(validator.validate_catalog(self.fixture()), [])

    def test_list_root_and_project_without_workpackages_are_valid(self):
        project = self.project()
        project["workpackages"] = []
        project["milestones"] = []
        self.assertEqual(validator.validate_catalog([project]), [])

    def test_v2_is_invalid(self):
        project = self.project()
        project["schema_version"] = 2
        errors = validator.validate_catalog([project])
        self.assertTrue(any("schema_version" in error for error in errors))

    def test_case_insensitive_duplicate_artifact_ids_across_workpackages_are_invalid(self):
        project = self.project()
        project["workpackages"].append({
            "id": "wp2",
            "title": "Second workpackage",
            "status": "active",
            "tasks": [{"id": "t1.1", "title": "Duplicate task"}],
            "deliverables": [{"id": "d1.1", "title": "Duplicate deliverable"}],
        })
        project["milestones"].append({"id": "ms1", "title": "Duplicate milestone"})
        errors = validator.validate_catalog([project])
        self.assertEqual(sum("duplicate identifier" in error for error in errors), 3)

    def test_case_insensitive_duplicate_project_and_wp_slugs_are_invalid(self):
        catalog = self.fixture()
        duplicate_project = self.project()
        duplicate_project["id"] = "EU-EXAMPLE"
        duplicate_project["workpackages"] = []
        duplicate_project["milestones"] = []
        catalog["projects"].append(duplicate_project)
        catalog["projects"][0]["workpackages"].append({
            "id": "WP1", "title": "Duplicate WP", "status": "active", "tasks": [], "deliverables": [],
        })
        errors = validator.validate_catalog(catalog)
        self.assertTrue(any("expected lowercase slug" in error for error in errors))
        self.assertTrue(any("duplicate identifier" in error for error in errors))

    def test_unknown_related_wp_is_invalid(self):
        project = self.project()
        project["milestones"][0]["related_wps"] = ["wp999"]
        errors = validator.validate_catalog([project])
        self.assertTrue(any("unknown workpackage id: wp999" in error for error in errors))

    def test_invalid_wp_status_and_number_are_invalid(self):
        project = self.project()
        project["workpackages"][0]["status"] = "proposal"
        project["workpackages"][0]["number"] = 0
        errors = validator.validate_catalog([project])
        self.assertTrue(any(".status:" in error for error in errors))
        self.assertTrue(any(".number:" in error for error in errors))

    def test_cli_json_envelope_and_exit_codes(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            catalog_path = Path(temp_dir) / "catalog.json"
            catalog_path.write_text(json.dumps(self.fixture()), encoding="utf-8")
            valid = subprocess.run(
                [sys.executable, str(SCRIPT_PATH), "--catalog", str(catalog_path), "--json"],
                text=True, capture_output=True, check=False,
            )
            self.assertEqual(valid.returncode, 0, valid.stderr)
            valid_envelope = json.loads(valid.stdout)
            self.assertEqual(valid_envelope["state"], "Valid")
            self.assertTrue(valid_envelope["success"])

            invalid_catalog = self.fixture()
            invalid_catalog["projects"][0]["schema_version"] = 2
            catalog_path.write_text(json.dumps(invalid_catalog), encoding="utf-8")
            invalid = subprocess.run(
                [sys.executable, str(SCRIPT_PATH), "--catalog", str(catalog_path), "--json"],
                text=True, capture_output=True, check=False,
            )
            self.assertEqual(invalid.returncode, 1, invalid.stderr)
            invalid_envelope = json.loads(invalid.stdout)
            self.assertEqual(invalid_envelope["state"], "Invalid")
            self.assertFalse(invalid_envelope["success"])

            failed = subprocess.run(
                [sys.executable, str(SCRIPT_PATH), "--catalog", str(catalog_path.with_name("missing.json")), "--json"],
                text=True, capture_output=True, check=False,
            )
            self.assertEqual(failed.returncode, 2, failed.stderr)
            failed_envelope = json.loads(failed.stdout)
            self.assertEqual(failed_envelope["state"], "Failed")

            argument_failure = subprocess.run(
                [sys.executable, str(SCRIPT_PATH), "--unsupported", "--json"],
                text=True, capture_output=True, check=False,
            )
            self.assertEqual(argument_failure.returncode, 2, argument_failure.stderr)
            self.assertEqual(json.loads(argument_failure.stdout)["state"], "Failed")

    def test_included_example_catalog_is_valid(self):
        catalog_path = REPO_ROOT / "memory" / "references" / "projects" / "projects.json"
        document = json.loads(catalog_path.read_text(encoding="utf-8"))
        self.assertEqual(validator.validate_catalog(document), [])


if __name__ == "__main__":
    unittest.main()
