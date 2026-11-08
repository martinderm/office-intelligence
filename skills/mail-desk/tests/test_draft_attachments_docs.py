"""Draft attachments (#18) — the operative documentation and both system maps carry the contract.

Evidence mode ``acceptance`` (documentation contract).  Matrix row D1.  The expected phrases are the
confirmed limits and the bounded reason codes; they are literals, not read back from the code.
"""

from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path

MAIL_DESK_ROOT = Path(__file__).resolve().parents[1]
BUNDLE_ROOT = MAIL_DESK_ROOT.parents[1]
sys.path.insert(0, str(MAIL_DESK_ROOT / "scripts"))

from core import draft_attachments as da  # noqa: E402
from core import drafts  # noqa: E402

DOCS = {
    "SKILL": MAIL_DESK_ROOT / "SKILL.md",
    "cli-operations": MAIL_DESK_ROOT / "references" / "cli-operations.md",
    "himalaya": MAIL_DESK_ROOT / "references" / "backends" / "himalaya.md",
    "L2 README": MAIL_DESK_ROOT / "docs" / "system-map" / "README.md",
    "L2 processes": MAIL_DESK_ROOT / "docs" / "system-map" / "processes.md",
    "L2 objects": MAIL_DESK_ROOT / "docs" / "system-map" / "objects.md",
    "L2 effects": MAIL_DESK_ROOT / "docs" / "system-map" / "effects.md",
    "L1 README": BUNDLE_ROOT / "docs" / "system-map" / "README.md",
}

REASON_CODES = (
    "draft_append_outcome_unknown",
    "draft_replace_would_drop_attachments",
    "draft_verification_failed",
    "message_too_large",
    "draft_lock_missing",
)


class DocumentationContractTests(unittest.TestCase):
    def text(self, name: str) -> str:
        return DOCS[name].read_text(encoding="utf-8")

    def test_limits_are_the_confirmed_literals(self) -> None:  # D1
        self.assertEqual(5, da.MAX_ATTACHMENTS)
        self.assertEqual(3 * 1048576, da.MAX_ATTACHMENT_BYTES)
        self.assertEqual(10 * 1048576, da.MAX_MIME_MESSAGE_BYTES)
        for name in ("SKILL", "cli-operations", "L1 README"):
            text = self.text(name)
            for needle in ("5", "3 MiB", "10 MiB"):
                self.assertIn(needle, text, f"{name} lacks {needle}")

    def test_zones_and_exclusion_are_documented(self) -> None:  # D1
        for name in ("SKILL", "cli-operations"):
            text = self.text(name)
            for zone in ("memory/references", "memory/evidence", "memory/cloud", "data/"):
                self.assertIn(zone, text, f"{name} lacks {zone}")
            self.assertIn("data/mail-desk/", text)

    def test_operator_surface_and_reason_codes_are_documented(self) -> None:  # D1
        cli = self.text("cli-operations")
        for flag in ("--attach", "--drop-attachments", "--old-message-id", "--lease-id"):
            self.assertIn(flag, cli)
        for code in REASON_CODES:
            self.assertIn(code, cli, f"cli-operations lacks {code}")
        self.assertTrue(drafts.DRAFT_APPEND_OUTCOME_UNKNOWN_REASON in cli)

    def test_both_maps_describe_the_new_contract(self) -> None:  # D1
        self.assertIn("## 41. Issue #18", self.text("L2 README"))
        self.assertIn("## 12. Issue #18", self.text("L1 README"))
        self.assertIn("record_attempt", self.text("L2 objects") + self.text("L2 processes") + self.text("L2 README"))
        self.assertIn("attempts", self.text("L2 objects"))
        self.assertIn("6.1a", self.text("L2 processes"))
        self.assertIn("5.2 Entwürfe mit Dateianhängen", self.text("L2 effects"))

    def test_no_machine_specific_absolute_paths_in_the_new_documentation(self) -> None:  # portability
        pattern = re.compile(r"[A-Za-z]:[\\/](?:users|Users)|/home/|/Users/")
        for name in ("SKILL", "cli-operations", "himalaya", "L2 README", "L1 README"):
            section = self.text(name)
            marker = "Issue #18"
            index = section.find(marker)
            if index >= 0:
                self.assertIsNone(pattern.search(section[index:]), f"{name} contains a host path after the #18 section")


if __name__ == "__main__":
    unittest.main()
