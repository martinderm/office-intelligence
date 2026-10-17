import unittest
from pathlib import Path


class OptionalDependencyIsolationTests(unittest.TestCase):
    def test_conversion_dependencies_are_scoped_to_cloud_atlas(self):
        bundle_root = Path(__file__).resolve().parents[3]
        optional_requirements = bundle_root / "skills" / "cloud-atlas" / "requirements-conversion.txt"

        self.assertFalse((bundle_root / "requirements.txt").exists())
        self.assertEqual(
            ["markitdown>=0.1.0", "ocrmypdf>=17.0.0", "pymupdf>=1.24.0"],
            [line for line in optional_requirements.read_text(encoding="utf-8").splitlines() if line and not line.startswith("#")],
        )

    def test_skill_documents_typed_degraded_state(self):
        skill = (Path(__file__).resolve().parents[1] / "SKILL.md").read_text(encoding="utf-8")

        self.assertIn("requirements-conversion.txt", skill)
        self.assertIn('state: "ConversionRequired"', skill)
        self.assertIn('conversion_status: "conversion_required"', skill)

    def test_pymupdf_is_a_documented_optional_capability(self):
        skill = (Path(__file__).resolve().parents[1] / "SKILL.md").read_text(encoding="utf-8")
        self.assertIn("pymupdf", skill)
        self.assertIn("fitz", skill)

    def test_form_field_tests_do_not_globally_skip_on_pymupdf(self):
        feature_test = (Path(__file__).resolve().parent / "test_pdf_form_field_extraction.py").read_text(encoding="utf-8")
        self.assertNotIn("importorskip", feature_test)
        self.assertIn("_load_pymupdf", feature_test)


if __name__ == "__main__":
    unittest.main()
