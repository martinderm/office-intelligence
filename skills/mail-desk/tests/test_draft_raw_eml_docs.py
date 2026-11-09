"""Raw-EML import (#18) — the operative documentation and both system maps carry the contract.

Evidence mode ``acceptance`` (documentation contract).  Matrix row D1.  The expected phrases are the
confirmed limits, surface names and bounded reason codes; they are literals, not read back from the code,
except that every documented reason code must also be a code the implementation can actually raise.
"""

from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path

MAIL_DESK_ROOT = Path(__file__).resolve().parents[1]
BUNDLE_ROOT = MAIL_DESK_ROOT.parents[1]
sys.path.insert(0, str(MAIL_DESK_ROOT / "scripts"))

from core import draft_raw_eml as dre  # noqa: E402

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

#: The reason codes of the Raw-EML profile (literal list; the operator documentation must name each).
RAW_EML_CODES = (
    "raw_eml_path_invalid", "raw_eml_path_forbidden", "raw_eml_extension_invalid", "raw_eml_unreadable",
    "raw_eml_link_refused", "raw_eml_not_regular_file", "raw_eml_changed", "raw_eml_empty", "raw_eml_too_large",
    "raw_eml_message_too_large", "raw_eml_invalid", "raw_eml_not_utf8", "raw_eml_nul_byte", "raw_eml_bare_cr",
    "raw_eml_line_too_long", "raw_eml_header_invalid", "raw_eml_header_missing", "raw_eml_header_duplicate",
    "raw_eml_message_id_invalid", "raw_eml_mime_invalid", "raw_eml_structure_unsupported",
    "raw_eml_attachment_invalid", "raw_eml_attachment_count_exceeded", "raw_eml_attachment_too_large",
    "raw_eml_part_header_invalid", "raw_eml_address_invalid", "raw_eml_date_invalid",
    "raw_eml_part_count_exceeded",
)


class DocumentationContractTests(unittest.TestCase):
    def text(self, name: str) -> str:
        return DOCS[name].read_text(encoding="utf-8")

    def test_operator_surface_is_documented(self) -> None:  # D1
        cli = self.text("cli-operations")
        for needle in ("save-raw-draft", "--eml", "save_raw_draft", "10 MiB", "3 MiB", "998", "CRLF", "lines()",
                       "Message-ID", "Kein zweiter Schreibpfad"):
            self.assertIn(needle, cli, f"cli-operations lacks {needle}")
        skill = self.text("SKILL")
        for needle in ("save-raw-draft", "save_raw_draft", "10 MiB", "CRLF", "keinen zweiten Schreibpfad"):
            self.assertIn(needle, skill, f"SKILL lacks {needle}")

    def test_every_reason_code_is_documented_and_exists_in_the_implementation(self) -> None:  # D1
        cli = self.text("cli-operations")
        source = (MAIL_DESK_ROOT / "scripts" / "core" / "draft_raw_eml.py").read_text(encoding="utf-8")
        source += (MAIL_DESK_ROOT / "scripts" / "core" / "draft_attachments.py").read_text(encoding="utf-8")
        for code in RAW_EML_CODES:
            self.assertIn(code, cli, f"cli-operations lacks {code}")
            suffix = code[len("raw_eml_"):]
            self.assertTrue(
                code in source or f'f"{{prefix}}_{suffix}"' in source or f'"{suffix}"' in source,
                f"{code} is documented but never raised",
            )

    def test_limits_are_the_confirmed_literals(self) -> None:  # D1
        self.assertEqual(10 * 1048576, dre.MAX_RAW_EML_BYTES)
        self.assertEqual(998, dre.MAX_LINE_BYTES)
        for name in ("L1 README", "L2 README"):
            text = self.text(name)
            for needle in ("10 MiB", "3 MiB", "998", "CRLF"):
                self.assertIn(needle, text, f"{name} lacks {needle}")

    def test_himalaya_normalization_is_documented(self) -> None:  # D1
        himalaya = self.text("himalaya")
        for needle in ("Raw-EML import (Issue #18)", "lines()", "canonical CRLF", "one final CRLF", "not live-proven"):
            self.assertIn(needle, himalaya)

    def test_both_maps_describe_the_contract(self) -> None:  # D1
        self.assertIn("## 42. Issue #18", self.text("L2 README"))
        self.assertIn("## 13. Issue #18", self.text("L1 README"))
        self.assertIn("6.1b", self.text("L2 processes"))
        self.assertIn("5.3 Raw-EML-Import", self.text("L2 effects"))
        objects = self.text("L2 objects")
        for needle in ("source_sha256", "normalized_sha256", "PreparedRawEml", "save_raw_draft"):
            self.assertIn(needle, objects)
        self.assertIn("Aktueller Frame (Issue #18", self.text("L2 README"))
        self.assertNotIn("`save_raw_draft` ist nicht beschlossen", self.text("L2 README"))

    def test_review_round_one_contract_points_are_documented(self) -> None:  # D1 (findings 1-4)
        cli = self.text("cli-operations")
        for needle in ("Teil-Header", "`Content-*`", "Date", "RFC 5322", "Adresse", "Trailing", "'account'"):
            self.assertIn(needle, cli, f"cli-operations lacks {needle}")
        skill = self.text("SKILL")
        for needle in ("Content-*", "Teil-Header", "Adresslisten-Grammatik"):
            self.assertIn(needle, skill, f"SKILL lacks {needle}")
        for name in ("L2 README", "L2 processes", "L1 README"):
            text = self.text(name)
            for needle in ("Content-*", "raw_eml_part_header_invalid"):
                if name == "L1 README" and needle == "raw_eml_part_header_invalid":
                    continue
                self.assertIn(needle, text, f"{name} lacks {needle}")
        self.assertIn("raw_eml_date_invalid", self.text("L2 processes"))
        self.assertIn("raw_eml_address_invalid", self.text("L2 processes"))

    def test_round_three_contract_points_are_documented(self) -> None:  # D1 (residual findings 1-4)
        cli = self.text("cli-operations")
        for needle in ("64 MIME-Teile", "RFC 2231", "Anführungszeichen", "Unicode-Whitespace", "ASCII-Ziffern",
                       "part[n]:header:<name>", "part[n]:header_order", "streng geparster Content-Type",
                       "Teil-Header-Werte", "`name`", "`filename`", "`boundary`", "`charset`", "`inline`"):
            self.assertIn(needle, cli, f"cli-operations lacks {needle}")
        for stale in ("ein Kommentar erlaubt", "gültiger Content-Type (`raw_eml_attachment_invalid`"):
            self.assertNotIn(stale, cli, f"cli-operations still says: {stale}")
        self.assertNotRegex(cli, r"Alle Header, auch `Bcc`/`X-Unsent`, bleiben erhalten\s+und werden verglichen;")
        for name, needles in {
            "SKILL": ("64", "RFC 2231", "Teil-Header-Werte", "part[n]"),
            "L2 README": ("Teil-Header-Werte", "64 MIME-Teile", "RFC 2231", "ASCII-Ziffern"),
            "L2 processes": ("Teil-Header-Werte", "raw_eml_part_count_exceeded", "ASCII-Ziffern", "Anführungszeichen"),
            "L2 objects": ("MAX_MIME_PARTS", "parts", "Teil-Header"),
            "L1 README": ("64 MIME-Teile", "Teil-Header-Werte"),
        }.items():
            text = self.text(name)
            for needle in needles:
                self.assertIn(needle, text, f"{name} lacks {needle}")
        self.assertNotIn("nicht des Rücklesens", self.text("L2 README"))
        self.assertNotIn("nur LF/CRLF-neutral", self.text("L2 objects"))
        self.assertIn(str(dre.MAX_MIME_PARTS), self.text("L1 README"))

    def test_round_four_contract_points_are_documented(self) -> None:  # D1 (final-round findings 1-3)
        cli = self.text("cli-operations")
        for needle in ("Parameterposition", "Python 3.12", "Group:; , a@example.org", "Group:;, a@example.org",
                       "Kommentaren", "Anzeigenamen in Anführungszeichen", "fail-closed", "AttributeError"):
            self.assertIn(needle, cli, f"cli-operations lacks {needle}")
        self.assertNotRegex(cli, r"Unicode-Whitespace\s+außerhalb von Anführungszeichen")
        self.assertNotIn("und ASCII-Leerzeichen;", cli)
        self.assertNotRegex(cli, r"darf weder `Content-Disposition:\s+attachment` noch einen `name`/`filename` tragen;")
        for name, needles in {
            "SKILL": ("Parameterposition", "Python 3.12", "Kommentaren"),
            "L2 README": ("Parameterposition", "Python 3.12", "Kommentaren"),
            "L2 processes": ("Parameterposition", "Python 3.12", "Kommentaren"),
            "L1 README": ("Parameterposition", "Kommentaren", "Python 3.12"),
        }.items():
            text = self.text(name)
            for needle in needles:
                self.assertIn(needle, text, f"{name} lacks {needle}")
        self.assertNotRegex(self.text("SKILL"), r"akzeptieren nur ASCII-Ziffern und ASCII-Leerraum")
        self.assertNotRegex(self.text("L2 README"), r"außerhalb von Anführungszeichen abgewiesen")
        self.assertNotRegex(self.text("L2 processes"), r"außerhalb von Anführungszeichen werden abgewiesen")
        self.assertNotRegex(self.text("L1 README"), r"nur ASCII-Ziffern und\s+ASCII-Leerraum")

    def test_no_stale_uncommitted_wording_remains_in_the_l2_frame(self) -> None:  # D1 (finding 4)
        readme = self.text("L2 README")
        self.assertNotIn("ungestagten", readme)
        self.assertNotIn("mit ungestagten Änderungen", readme)
        self.assertIn("c6b8d2e", readme)

    def test_the_manifest_schema_in_the_docs_names_account(self) -> None:  # D1 (finding 4)
        for name in ("cli-operations", "SKILL", "L2 README"):
            self.assertIn("account", self.text(name))
        self.assertIn("`eml`/`folder`/`account`", self.text("L2 README"))
        self.assertIn("`account`", self.text("cli-operations"))

    def test_no_machine_specific_absolute_paths_in_the_new_documentation(self) -> None:  # portability
        pattern = re.compile(r"[A-Za-z]:[\\/](?:users|Users)|/home/|/Users/")
        for name in ("SKILL", "cli-operations", "himalaya", "L2 README", "L1 README", "L2 processes", "L2 objects", "L2 effects"):
            section = self.text(name)
            index = section.find("Raw-EML")
            if index >= 0:
                self.assertIsNone(pattern.search(section[index:]), f"{name} contains a host path after the Raw-EML text")


class NamespaceAndDocstringTests(unittest.TestCase):
    def test_save_raw_draft_is_exported_in_the_core_namespace(self) -> None:  # finding 5
        import core
        from core import drafts

        self.assertIs(drafts.save_raw_draft, core.save_raw_draft)
        self.assertIs(dre.RawEmlError, core.RawEmlError)
        self.assertNotIn("save_raw_draft", core.__all__, "__all__ stays the frozen compatibility baseline")

    def test_the_append_core_docstring_is_not_broader_than_its_callers(self) -> None:  # finding 5
        from core import drafts

        doc = drafts._append_and_verify.__doc__ or ""
        self.assertNotIn("only writer", doc)
        for needle in ("attachment-mode", "Raw-EML", "replace", "text-only", "diagnos"):
            self.assertIn(needle, doc, f"docstring lacks {needle}")


if __name__ == "__main__":
    unittest.main()
