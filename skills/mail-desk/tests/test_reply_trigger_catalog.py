"""FR-18/MD-S1 desk-signals reply-trigger catalog contracts (tests-only Red phase).

The reply-requirement heuristics of the base classifier must be workspace-bound:
the trigger list and the no-reply sender tokens move from module constants to the
consuming workspace's desk-signals catalog
``memory/references/mail-desk/mail-desk.json`` (schema 1).

MD-S1 contract defined by this suite (the implementation must satisfy it):

* ``core.matching.reply_heuristics.load_reply_heuristics(workspace_root)`` returns a
  small immutable config object exposing ``reply_triggers``,
  ``no_reply_sender_tokens`` and ``owner_address``.
* A missing catalog file yields the documented compatibility defaults equal to the
  previous hardcoded constants; an invalid file or schema drift fails loud with a
  ``ValueError`` -- never a silent fallback.
* ``core.matching.reply_heuristics.matches_reply_trigger(text, triggers)`` is the
  canonical word-boundary predicate; a bare substring inside a longer word
  (``"Smartin bitte"`` for trigger ``"martin bitte"``) must not match.
* ``core.classifier.classify_email`` consumes the loaded catalog instead of the
  hardcoded ``martin`` list, including for unknown/``unclassified`` items.

All fixtures are hermetic temp directories; no mailbox, network or real catalog is
touched.  The MD-R8 closing/quote semantics and ``FULL_BODY_ACTION_REQUEST`` are
out of scope and stay owned by their existing modules/tests.
"""

from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
import unittest


MAIL_DESK_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(MAIL_DESK_ROOT / "scripts"))

from core import classifier  # noqa: E402
from core.matching import reply_heuristics  # noqa: E402
from core.matching.reply_heuristics import (  # noqa: E402
    load_reply_heuristics,
    matches_reply_trigger,
)


#: The exact pre-MD-S1 hardcoded compatibility defaults (classifier.py).
DEFAULT_REPLY_TRIGGERS = (
    "martin bitte",
    "bitte martin",
    "frage an martin",
    "hallo martin",
    "lieber martin",
    "martin kannst du",
    "martin ?",
    "martin, bitte",
    "@martin",
)
DEFAULT_NO_REPLY_SENDER_TOKENS = (
    "no-reply",
    "do_not_reply",
    "quarantine",
    "mailer-daemon",
)
CATALOG_RELATIVE_PATH = Path("memory") / "references" / "mail-desk" / "mail-desk.json"


class _CatalogWorkspace:
    """Hermetic temp workspace that never touches real catalogs or mailboxes."""

    def __init__(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()

    def __enter__(self) -> Path:
        return Path(self._tmp.name)

    def __exit__(self, *exc: object) -> None:
        self._tmp.cleanup()


def write_catalog(workspace_root: Path, payload: object) -> Path:
    """Write the desk-signals catalog; a ``str`` payload is written verbatim."""
    path = workspace_root / CATALOG_RELATIVE_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(payload, str):
        path.write_text(payload, encoding="utf-8")
    else:
        path.write_text(json.dumps(payload), encoding="utf-8")
    return path


class ReplyTriggerCatalogLoaderTests(unittest.TestCase):
    """Loader contracts for the canonical reply-heuristics owner."""

    def test_owner_exports_the_canonical_loader_and_predicate(self) -> None:
        self.assertTrue(callable(reply_heuristics.load_reply_heuristics))
        self.assertIs(load_reply_heuristics, reply_heuristics.load_reply_heuristics)
        self.assertTrue(callable(matches_reply_trigger))

    def test_catalog_file_returns_configured_triggers_tokens_and_owner(self) -> None:
        with _CatalogWorkspace() as ws:
            write_catalog(
                ws,
                {
                    "schema_version": 1,
                    "reply_heuristics": {
                        "reply_triggers": ["hallo klaus", "bitte klaus"],
                        "no_reply_sender_tokens": ["noreply@example"],
                        "owner_address": None,
                    },
                },
            )
            config = load_reply_heuristics(ws)

        self.assertEqual(tuple(config.reply_triggers), ("hallo klaus", "bitte klaus"))
        self.assertEqual(tuple(config.no_reply_sender_tokens), ("noreply@example",))
        self.assertIsNone(config.owner_address)

    def test_optional_fields_use_defaults_and_owner_address_is_returned(self) -> None:
        with _CatalogWorkspace() as ws:
            write_catalog(
                ws,
                {
                    "schema_version": 1,
                    "reply_heuristics": {
                        "reply_triggers": ["hallo klaus"],
                        "owner_address": "desk-owner@example.org",
                    },
                },
            )
            config = load_reply_heuristics(ws)

        self.assertEqual(tuple(config.reply_triggers), ("hallo klaus",))
        # ``no_reply_sender_tokens`` is optional and falls back to today's constants.
        self.assertEqual(
            tuple(config.no_reply_sender_tokens), DEFAULT_NO_REPLY_SENDER_TOKENS
        )
        self.assertEqual(config.owner_address, "desk-owner@example.org")

    def test_missing_catalog_file_returns_documented_defaults(self) -> None:
        with _CatalogWorkspace() as ws:
            config = load_reply_heuristics(ws)

        self.assertEqual(tuple(config.reply_triggers), DEFAULT_REPLY_TRIGGERS)
        self.assertEqual(
            tuple(config.no_reply_sender_tokens), DEFAULT_NO_REPLY_SENDER_TOKENS
        )
        self.assertIsNone(config.owner_address)

    def test_invalid_json_fails_loud(self) -> None:
        with _CatalogWorkspace() as ws:
            write_catalog(ws, "{ this is not valid json")
            with self.assertRaises(ValueError) as ctx:
                load_reply_heuristics(ws)

        self.assertTrue(str(ctx.exception).strip())

    def test_missing_reply_triggers_fails_loud(self) -> None:
        payloads = {
            "no reply_heuristics block": {"schema_version": 1},
            "empty reply_heuristics": {
                "schema_version": 1,
                "reply_heuristics": {},
            },
            "no reply_triggers key": {
                "schema_version": 1,
                "reply_heuristics": {"no_reply_sender_tokens": ["noreply@example"]},
            },
        }
        for label, payload in payloads.items():
            with self.subTest(case=label), _CatalogWorkspace() as ws:
                write_catalog(ws, payload)
                with self.assertRaises(ValueError):
                    load_reply_heuristics(ws)

    def test_schema_drift_fails_loud(self) -> None:
        payloads = {
            "missing schema_version": {
                "reply_heuristics": {"reply_triggers": ["hallo klaus"]},
            },
            "unsupported schema_version": {
                "schema_version": 2,
                "reply_heuristics": {"reply_triggers": ["hallo klaus"]},
            },
            # Loose ``==`` would accept these as schema_version 1
            # (``True == 1`` / ``1.0 == 1``); the strict gate must reject each.
            "boolean schema_version": {
                "schema_version": True,
                "reply_heuristics": {"reply_triggers": ["hallo klaus"]},
            },
            "string schema_version": {
                "schema_version": "1",
                "reply_heuristics": {"reply_triggers": ["hallo klaus"]},
            },
            "float schema_version": {
                "schema_version": 1.0,
                "reply_heuristics": {"reply_triggers": ["hallo klaus"]},
            },
        }
        for label, payload in payloads.items():
            with self.subTest(case=label), _CatalogWorkspace() as ws:
                write_catalog(ws, payload)
                with self.assertRaises(ValueError):
                    load_reply_heuristics(ws)


class ReplyTriggerMatchingTests(unittest.TestCase):
    """Word-boundary predicate contracts (no bare-substring false positives)."""

    def test_matching_trigger_is_detected_with_word_boundaries(self) -> None:
        self.assertTrue(
            matches_reply_trigger("hallo martin, bitte senden", ["hallo martin"])
        )
        self.assertTrue(
            matches_reply_trigger("@martin kannst du mir helfen?", ["@martin"])
        )

    def test_bare_substrings_inside_longer_words_do_not_match(self) -> None:
        # The MD-S1 discriminator: a naive substring search would match both.
        self.assertFalse(matches_reply_trigger("Smartin bitte", ["martin bitte"]))
        self.assertFalse(matches_reply_trigger("bitte marting", ["bitte martin"]))
        self.assertFalse(matches_reply_trigger("xmartin, bitte", ["martin, bitte"]))

    def test_unrelated_text_does_not_match(self) -> None:
        self.assertFalse(matches_reply_trigger("kein trigger hier", ["hallo martin"]))


class ClassifyEmailReplyCatalogIntegrationTests(unittest.TestCase):
    """The facade consumes the workspace catalog instead of the hardcoded list."""

    ALTERNATIVE_MAIL = "Hallo Klaus, bitte sende mir das Protokoll."
    DEFAULT_OWNER_MAIL = "Hallo Martin, bitte sende mir das Protokoll."

    def _email(
        self,
        *,
        preview: str = "",
        from_: str = "Klaus Beispiel <klaus@example.org>",
        subject: str = "Desk-Anfrage",
        to: str = "desk@example.org",
    ) -> dict:
        return {
            "envelope_id": "9912",
            "folder": "INBOX",
            "message_id": "md-s1@example.test",
            "subject": subject,
            "from": from_,
            "to": to,
            "preview": preview,
        }

    def _classify(self, workspace_root: Path, email: dict) -> dict:
        return classifier.classify_email(
            email,
            workspace_root=workspace_root,
            projects=[],
            topics=[],
        )

    def test_configured_alternative_trigger_sets_needs_reply(self) -> None:
        with _CatalogWorkspace() as ws:
            write_catalog(
                ws,
                {
                    "schema_version": 1,
                    "reply_heuristics": {"reply_triggers": ["hallo klaus"]},
                },
            )
            item = self._classify(ws, self._email(preview=self.ALTERNATIVE_MAIL))

        self.assertTrue(item["decision"]["needs_reply"])

    def test_defaults_do_not_fire_for_alternative_owner(self) -> None:
        with _CatalogWorkspace() as ws:
            item = self._classify(ws, self._email(preview=self.ALTERNATIVE_MAIL))

        self.assertFalse(item["decision"]["needs_reply"])

    def test_default_triggers_still_fire_without_catalog_file(self) -> None:
        with _CatalogWorkspace() as ws:
            item = self._classify(ws, self._email(preview=self.DEFAULT_OWNER_MAIL))

        self.assertTrue(item["decision"]["needs_reply"])


class NoReplySenderTokenTests(unittest.TestCase):
    """The no-reply sender check consumes the configured/default tokens."""

    def _email(self, *, from_: str, preview: str) -> dict:
        return {
            "envelope_id": "9912",
            "folder": "INBOX",
            "message_id": "md-s1-token@example.test",
            "subject": "Desk-Anfrage",
            "from": from_,
            "to": "desk@example.org",
            "preview": preview,
        }

    def _classify(self, workspace_root: Path, email: dict) -> dict:
        return classifier.classify_email(
            email,
            workspace_root=workspace_root,
            projects=[],
            topics=[],
        )

    def test_default_tokens_suppress_an_otherwise_triggering_mail(self) -> None:
        with _CatalogWorkspace() as ws:
            item = self._classify(
                ws,
                self._email(
                    from_="Quarantine@example",
                    preview="Hallo Martin, bitte sende mir das Protokoll.",
                ),
            )

        self.assertFalse(item["decision"]["needs_reply"])

    def test_configured_tokens_replace_the_defaults(self) -> None:
        with _CatalogWorkspace() as ws:
            write_catalog(
                ws,
                {
                    "schema_version": 1,
                    "reply_heuristics": {
                        "reply_triggers": ["hallo klaus"],
                        "no_reply_sender_tokens": ["special-drop@example.example"],
                    },
                },
            )
            suppressed = self._classify(
                ws,
                self._email(
                    from_="special-drop@example.example",
                    preview="Hallo Klaus, bitte sende mir das Protokoll.",
                ),
            )
            # "quarantine" is no longer a configured token, so it does not suppress.
            not_suppressed = self._classify(
                ws,
                self._email(
                    from_="Quarantine@example",
                    preview="Hallo Klaus, bitte sende mir das Protokoll.",
                ),
            )

        self.assertFalse(suppressed["decision"]["needs_reply"])
        self.assertTrue(not_suppressed["decision"]["needs_reply"])


if __name__ == "__main__":
    unittest.main()
