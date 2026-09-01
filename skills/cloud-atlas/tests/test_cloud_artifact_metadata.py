import json
import re
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path


SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

from core.metadata import build_cloud_artifact_metadata


def _schema_path() -> Path:
    for parent in Path(__file__).resolve().parents:
        candidate = (
            parent
            / "Shared-Memory"
            / "agent-architecture"
            / "schemas"
            / "data-zone-artifact.schema.json"
        )
        if candidate.is_file():
            return candidate
    raise FileNotFoundError("data-zone-artifact.schema.json not found")


SCHEMA = json.loads(_schema_path().read_text(encoding="utf-8"))


def _assert_schema_subset(test_case, instance, schema):
    """Validate the schema features that constrain this canonical builder."""

    test_case.assertIsInstance(instance, dict)
    required = set(schema.get("required", ()))
    properties = schema["properties"]
    test_case.assertTrue(required <= instance.keys())
    if schema.get("additionalProperties") is False:
        test_case.assertTrue(instance.keys() <= properties.keys())

    for key, value in instance.items():
        rule = properties[key]
        if "enum" in rule:
            test_case.assertIn(value, rule["enum"], key)
        allowed_types = rule.get("type")
        if allowed_types:
            allowed_types = (
                {allowed_types} if isinstance(allowed_types, str) else set(allowed_types)
            )
            matches_type = (
                ("string" in allowed_types and isinstance(value, str))
                or ("boolean" in allowed_types and isinstance(value, bool))
                or ("null" in allowed_types and value is None)
            )
            test_case.assertTrue(matches_type, key)
        if isinstance(value, str) and "minLength" in rule:
            test_case.assertGreaterEqual(len(value), rule["minLength"], key)
        if value is not None and "pattern" in rule:
            test_case.assertIsNotNone(re.fullmatch(rule["pattern"], value), key)

    cloud_rule = next(
        rule["then"]
        for rule in schema.get("allOf", ())
        if rule.get("if", {}).get("properties", {}).get("zone", {}).get("const")
        == "cloud"
    )
    test_case.assertTrue(set(cloud_rule["required"]) <= instance.keys())
    for key, rule in cloud_rule.get("properties", {}).items():
        if "const" in rule:
            test_case.assertEqual(rule["const"], instance[key], key)

    synced_at_rule = properties["synced_at"]
    if instance.get("synced_at") is not None and synced_at_rule.get("format") == "date-time":
        parsed = datetime.fromisoformat(instance["synced_at"].replace("Z", "+00:00"))
        test_case.assertIsNotNone(parsed.utcoffset())


class CloudArtifactMetadataTests(unittest.TestCase):
    def setUp(self):
        self.synced_at = datetime(
            2026, 9, 1, 12, 30, 45, tzinfo=timezone(timedelta(hours=2))
        )
        self.values = {
            "source_uri": "gdrive://example/document/42",
            "source_sha256": "A" * 64,
            "artifact_sha256": "b" * 64,
            "converter": "markitdown-1",
            "data_classification": "internal",
            "retention_class": "project-lifetime",
            "owner": "project:example",
            "synced_at": self.synced_at,
        }

    def test_result_conforms_to_normative_schema_constraints(self):
        metadata = build_cloud_artifact_metadata(**self.values)

        _assert_schema_subset(self, metadata, SCHEMA)
        self.assertEqual("cloud", metadata["zone"])
        self.assertEqual("active", metadata["status"])
        self.assertEqual(self.synced_at.isoformat(timespec="seconds"), metadata["synced_at"])

    def test_clock_is_injectable_and_called_once(self):
        calls = []

        def clock():
            calls.append(True)
            return self.synced_at

        values = dict(self.values)
        values.pop("synced_at")
        metadata = build_cloud_artifact_metadata(**values, clock=clock)

        self.assertEqual([True], calls)
        self.assertEqual(self.synced_at.isoformat(timespec="seconds"), metadata["synced_at"])

    def test_non_empty_required_strings_fail_fast(self):
        for field in ("source_uri", "converter", "retention_class", "owner"):
            with self.subTest(field=field):
                values = dict(self.values)
                values[field] = "  "
                with self.assertRaisesRegex(ValueError, field):
                    build_cloud_artifact_metadata(**values)

    def test_hashes_must_be_exactly_64_hexadecimal_characters(self):
        for field, invalid in (
            ("source_sha256", "f" * 63),
            ("artifact_sha256", "g" * 64),
        ):
            with self.subTest(field=field):
                values = dict(self.values)
                values[field] = invalid
                with self.assertRaisesRegex(ValueError, field):
                    build_cloud_artifact_metadata(**values)

    def test_naive_timestamp_is_rejected(self):
        values = dict(self.values)
        values["synced_at"] = datetime(2026, 9, 1, 12, 30, 45)

        with self.assertRaisesRegex(ValueError, "timezone-aware"):
            build_cloud_artifact_metadata(**values)

    def test_invalid_classification_is_rejected(self):
        values = dict(self.values)
        values["data_classification"] = "secret"

        with self.assertRaisesRegex(ValueError, "data_classification"):
            build_cloud_artifact_metadata(**values)

    def test_clock_and_explicit_timestamp_are_mutually_exclusive(self):
        with self.assertRaisesRegex(ValueError, "either synced_at or clock"):
            build_cloud_artifact_metadata(**self.values, clock=lambda: self.synced_at)


if __name__ == "__main__":
    unittest.main()
