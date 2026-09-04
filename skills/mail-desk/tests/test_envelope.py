"""Focused contract tests for mail-desk canonical CLI envelopes."""

from __future__ import annotations

import io
import json
import sys
import unittest
from pathlib import Path


MAIL_DESK_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(MAIL_DESK_ROOT / "scripts"))

from core.envelope import (  # noqa: E402
    ENVELOPE_KEYS,
    MAX_TEXT_LENGTH,
    build_error,
    build_success,
    emit_json,
)


class MailDeskEnvelopeTests(unittest.TestCase):
    def assert_canonical_keys(self, envelope):
        self.assertEqual(ENVELOPE_KEYS, tuple(envelope))
        self.assertNotIn("ok", envelope)
        self.assertNotIn("status", envelope)

    def test_success_has_exact_keys_and_defaults(self):
        envelope = build_success("inspect_manifest", "Manifest inspected.", {"count": 1})

        self.assert_canonical_keys(envelope)
        self.assertTrue(envelope["success"])
        self.assertEqual("Completed", envelope["state"])
        self.assertEqual({"count": 1}, envelope["data"])
        self.assertIsNone(envelope["error"])

    def test_error_has_exact_keys_defaults_and_partial_data(self):
        envelope = build_error(
            "batch_runner",
            "One item failed.",
            {"completed": ["first"]},
            error_type="StorageFailure",
            error_details={"phase": "write"},
        )

        self.assert_canonical_keys(envelope)
        self.assertFalse(envelope["success"])
        self.assertEqual("Failed", envelope["state"])
        self.assertEqual({"completed": ["first"]}, envelope["data"])
        self.assertEqual(
            {"type": "StorageFailure", "message": "One item failed.", "details": {"phase": "write"}},
            envelope["error"],
        )

    def test_error_and_top_level_messages_are_bounded(self):
        envelope = build_error("mailbox_preflight", "x" * (MAX_TEXT_LENGTH + 25))

        self.assertEqual(MAX_TEXT_LENGTH, len(envelope["message"]))
        self.assertEqual(envelope["message"], envelope["error"]["message"])
        self.assertTrue(envelope["message"].endswith("..."))

    def test_builders_do_not_mutate_or_retain_input_mappings(self):
        data = {"completed": ["first"]}
        details = {"context": {"phase": "write"}}

        envelope = build_error("batch_runner", data=data, error_details=details)
        data["completed"].append("second")
        details["context"]["phase"] = "changed"

        self.assertEqual(["first"], envelope["data"]["completed"])
        self.assertEqual("write", envelope["error"]["details"]["context"]["phase"])

    def test_emit_json_writes_one_parseable_object_to_chosen_stream(self):
        stream = io.StringIO()

        result = emit_json(build_success("inspect_manifest", data={"count": 1}), stream)

        self.assertIsNone(result)
        output = stream.getvalue()
        self.assertEqual(1, len(output.splitlines()))
        parsed = json.loads(output)
        self.assert_canonical_keys(parsed)
        self.assertEqual(1, parsed["data"]["count"])

    def test_emit_json_does_not_write_before_serialization_failure(self):
        stream = io.StringIO()
        envelope = build_success("inspect_manifest", data={"not_json": object()})

        with self.assertRaises(TypeError):
            emit_json(envelope, stream)

        self.assertEqual("", stream.getvalue())

    def test_emit_json_rejects_unbounded_manual_envelope_before_writing(self):
        stream = io.StringIO()
        envelope = build_success("inspect_manifest")
        envelope["message"] = "x" * (MAX_TEXT_LENGTH + 1)

        with self.assertRaises(ValueError):
            emit_json(envelope, stream)

        self.assertEqual("", stream.getvalue())

    def test_invalid_actions_are_rejected_deterministically(self):
        for action in ("", "  ", " action", "action ", None, 42):
            with self.subTest(action=action):
                with self.assertRaises(ValueError) as raised:
                    build_success(action)
                self.assertEqual("action must be a non-empty, trimmed string.", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
