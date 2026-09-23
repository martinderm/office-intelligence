"""FR-22/MD-ID1 desk-signals reply-trigger catalog contracts (tests-only Red phase).

The reply-requirement heuristics of the base classifier are workspace-bound: the
trigger list and the no-reply sender tokens live in the consuming workspace's
desk-signals catalog ``memory/references/mail-desk/mail-desk.json`` (FR-18/MD-S1,
schema 1).  FR-22/MD-ID1 removes the identity-bearing ``martin`` fallback and
introduces catalog **schema 2**:

* A missing catalog file yields a **neutral** fallback: an EMPTY ``reply_triggers``
  tuple ("ohne Katalog keine Anrede-Trigger") -- no personal name is ever an
  expected fallback.  ``needs_reply`` stays trigger-false; review and
  ``FULL_BODY_ACTION_REQUEST`` semantics are untouched.
* Schema 2 accepts an ``owner_address`` without an explicit ``reply_triggers`` and
  **derives** the generic greeting-trigger class from the owner's local part
  (lowercased, first dot-segment; whole local part when it has no dot).  Explicit
  ``reply_triggers`` are used verbatim and never mixed with derived ones.
* Schema 1 files stay valid legacy (BOKU shape: ``reply_triggers`` required,
  ``owner_address`` optional).  Schema drift fails loud with ``ValueError`` for a
  bool/float/str ``schema_version`` and for wrong field types.
* Schema 2 adds the optional desk-signals fields ``sent_sender_domain_whitelist``,
  ``sent_subject_stopwords``, ``internal_domains`` and ``spam_sender_allowlist``
  (list of non-empty strings each); absent fields fall back to the documented
  defaults that mirror today's hardcoded constants.

MD-ID1 contract defined by this suite (the implementation must satisfy it):

* ``core.matching.reply_heuristics.load_reply_heuristics(workspace_root)`` returns a
  small immutable config object exposing ``reply_triggers``,
  ``no_reply_sender_tokens``, ``owner_address`` and the four schema-2 fields.
* ``core.matching.reply_heuristics.matches_reply_trigger(text, triggers)`` stays the
  canonical word-boundary predicate; a bare substring inside a longer word
  (``"Smartin bitte"`` for trigger ``"martin bitte"``) must not match.

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


#: Greeting-trigger derivation class (FR-22/MD-ID1).  ``{X}`` is replaced by the
#: owner local-part's first dot-segment.  This is the documented template for the
#: owner-derived triggers -- deliberately NOT a runtime fallback trigger list, so
#: no personal name remains an expected fallback.
GREETING_TRIGGER_TEMPLATES: tuple[str, ...] = (
    "{X} bitte",
    "bitte {X}",
    "frage an {X}",
    "hallo {X}",
    "lieber {X}",
    "{X} kannst du",
    "{X} ?",
    "{X}, bitte",
    "@{X}",
)

#: Explicit expected tuple for owner local-part ``klaus.weber@example.org``.
KLAUS_GREETING_TRIGGERS: tuple[str, ...] = (
    "klaus bitte",
    "bitte klaus",
    "frage an klaus",
    "hallo klaus",
    "lieber klaus",
    "klaus kannst du",
    "klaus ?",
    "klaus, bitte",
    "@klaus",
)

DEFAULT_NO_REPLY_SENDER_TOKENS = (
    "no-reply",
    "do_not_reply",
    "quarantine",
    "mailer-daemon",
)

#: Schema-2 optional desk-signals defaults, identical to today's hardcoded
#: constants (``sent_indexer.py:362/377`` and the internal-domain literals).
DEFAULT_SENT_SENDER_DOMAIN_WHITELIST = (
    "boku.ac.at",
    "gmail.com",
    "yahoo.com",
    "hotmail.com",
)
DEFAULT_SENT_SUBJECT_STOPWORDS = (
    "antwort",
    "betreff",
    "anfrage",
    "update",
    "fwd",
    "wtrlt",
    "2025",
    "2026",
    "boku",
    "mail",
)
DEFAULT_INTERNAL_DOMAINS = ("boku.ac.at",)
DEFAULT_SPAM_SENDER_ALLOWLIST: tuple[str, ...] = ()

CATALOG_RELATIVE_PATH = Path("memory") / "references" / "mail-desk" / "mail-desk.json"


def derive_greeting_triggers(owner_local_part: str) -> tuple[str, ...]:
    """Test-side mirror of the loader rule: first dot-segment, lowercased."""
    name = owner_local_part.strip().lower().split(".", 1)[0]
    return tuple(template.replace("{X}", name) for template in GREETING_TRIGGER_TEMPLATES)


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


class _ReplyCatalogTestCase(unittest.TestCase):
    """Shared hermetic helpers for the catalog suites.

    ``_load`` converts an unexpected loader rejection into an assertion failure
    instead of an opaque error, keeping the Red phase an intent-revealing failed
    assertion (the original ``ValueError`` message is preserved).  ``_new_field``
    reads a schema-2 optional field and reports its absence as an assertion
    failure rather than an ``AttributeError``.
    """

    def _load(self, workspace_root: Path):
        try:
            return load_reply_heuristics(workspace_root)
        except ValueError as exc:
            self.fail(f"valid catalog must load; loader rejected it: {exc}")

    def _new_field(self, config: object, name: str) -> object:
        return getattr(config, name, None)


class ReplyTriggerCatalogLoaderTests(_ReplyCatalogTestCase):
    """Loader contracts for the canonical reply-heuristics owner."""

    def test_owner_exports_the_canonical_loader_and_predicate(self) -> None:
        self.assertTrue(callable(reply_heuristics.load_reply_heuristics))
        self.assertIs(load_reply_heuristics, reply_heuristics.load_reply_heuristics)
        self.assertTrue(callable(matches_reply_trigger))

    # --- Schema 1 (legacy, BOKU shape) ------------------------------------

    def test_schema1_catalog_returns_configured_triggers_tokens_and_owner(self) -> None:
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
            config = self._load(ws)

        self.assertEqual(tuple(config.reply_triggers), ("hallo klaus", "bitte klaus"))
        self.assertEqual(tuple(config.no_reply_sender_tokens), ("noreply@example",))
        self.assertIsNone(config.owner_address)

    def test_schema1_optional_fields_use_defaults_and_owner_address_is_returned(self) -> None:
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
            config = self._load(ws)

        self.assertEqual(tuple(config.reply_triggers), ("hallo klaus",))
        # ``no_reply_sender_tokens`` is optional and falls back to today's constants.
        self.assertEqual(
            tuple(config.no_reply_sender_tokens), DEFAULT_NO_REPLY_SENDER_TOKENS
        )
        self.assertEqual(config.owner_address, "desk-owner@example.org")

    def test_schema1_missing_reply_triggers_fails_loud(self) -> None:
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

    def test_schema1_schema_drift_fails_loud(self) -> None:
        payloads = {
            "missing schema_version": {
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

    # --- Neutral fallback (FR-22/MD-ID1) ----------------------------------

    def test_missing_catalog_file_returns_neutral_empty_fallback(self) -> None:
        # "ohne Katalog keine Anrede-Trigger": no personal name is a fallback.
        with _CatalogWorkspace() as ws:
            config = self._load(ws)

        self.assertEqual(tuple(config.reply_triggers), ())
        self.assertEqual(
            tuple(config.no_reply_sender_tokens), DEFAULT_NO_REPLY_SENDER_TOKENS
        )
        self.assertIsNone(config.owner_address)
        self.assertEqual(
            self._new_field(config, "sent_sender_domain_whitelist"),
            DEFAULT_SENT_SENDER_DOMAIN_WHITELIST,
        )
        self.assertEqual(
            set(self._new_field(config, "sent_subject_stopwords") or ()),
            set(DEFAULT_SENT_SUBJECT_STOPWORDS),
        )
        self.assertEqual(
            self._new_field(config, "internal_domains"), DEFAULT_INTERNAL_DOMAINS
        )
        self.assertEqual(
            self._new_field(config, "spam_sender_allowlist"), DEFAULT_SPAM_SENDER_ALLOWLIST
        )

    def test_invalid_json_fails_loud(self) -> None:
        with _CatalogWorkspace() as ws:
            write_catalog(ws, "{ this is not valid json")
            with self.assertRaises(ValueError) as ctx:
                load_reply_heuristics(ws)

        self.assertTrue(str(ctx.exception).strip())

    # --- Schema 2 (owner-derived triggers) --------------------------------

    def test_schema2_derives_greeting_triggers_from_owner_local_part(self) -> None:
        with _CatalogWorkspace() as ws:
            write_catalog(
                ws,
                {
                    "schema_version": 2,
                    "reply_heuristics": {
                        # No ``reply_triggers`` key: the owner drives derivation.
                        "owner_address": "klaus.weber@example.org",
                    },
                },
            )
            config = self._load(ws)

        self.assertEqual(tuple(config.reply_triggers), KLAUS_GREETING_TRIGGERS)
        self.assertEqual(config.owner_address, "klaus.weber@example.org")
        self.assertEqual(
            tuple(config.no_reply_sender_tokens), DEFAULT_NO_REPLY_SENDER_TOKENS
        )

    def test_schema2_derives_from_whole_local_part_without_dot(self) -> None:
        with _CatalogWorkspace() as ws:
            write_catalog(
                ws,
                {
                    "schema_version": 2,
                    "reply_heuristics": {"owner_address": "Info@Example.ORG"},
                },
            )
            config = self._load(ws)

        self.assertEqual(tuple(config.reply_triggers), derive_greeting_triggers("info"))
        self.assertEqual(
            tuple(config.reply_triggers),
            (
                "info bitte",
                "bitte info",
                "frage an info",
                "hallo info",
                "lieber info",
                "info kannst du",
                "info ?",
                "info, bitte",
                "@info",
            ),
        )

    def test_schema2_explicit_reply_triggers_are_used_verbatim(self) -> None:
        with _CatalogWorkspace() as ws:
            write_catalog(
                ws,
                {
                    "schema_version": 2,
                    "reply_heuristics": {
                        "reply_triggers": ["hallo team", "bitte team"],
                        "owner_address": "klaus.weber@example.org",
                    },
                },
            )
            config = self._load(ws)

        self.assertEqual(tuple(config.reply_triggers), ("hallo team", "bitte team"))
        # Explicit triggers are never mixed with the owner-derived class.
        for derived in KLAUS_GREETING_TRIGGERS:
            self.assertNotIn(derived, config.reply_triggers)

    def test_schema2_requires_reply_triggers_or_owner_address(self) -> None:
        payloads = {
            "empty reply_heuristics": {"schema_version": 2, "reply_heuristics": {}},
            "null owner without triggers": {
                "schema_version": 2,
                "reply_heuristics": {"owner_address": None},
            },
        }
        for label, payload in payloads.items():
            with self.subTest(case=label), _CatalogWorkspace() as ws:
                write_catalog(ws, payload)
                with self.assertRaises(ValueError):
                    load_reply_heuristics(ws)

    def test_schema2_schema_version_drift_fails_loud(self) -> None:
        for bad_version in (True, 2.0, "2"):
            payload = {
                "schema_version": bad_version,
                "reply_heuristics": {"owner_address": "klaus.weber@example.org"},
            }
            with self.subTest(schema_version=repr(bad_version)), _CatalogWorkspace() as ws:
                write_catalog(ws, payload)
                with self.assertRaises(ValueError):
                    load_reply_heuristics(ws)

    def test_schema2_reply_triggers_type_drift_fails_loud(self) -> None:
        payloads = {
            "string instead of list": {
                "schema_version": 2,
                "reply_heuristics": {"reply_triggers": "hallo klaus"},
            },
            "non-string item": {
                "schema_version": 2,
                "reply_heuristics": {"reply_triggers": ["hallo klaus", 5]},
            },
        }
        for label, payload in payloads.items():
            with self.subTest(case=label), _CatalogWorkspace() as ws:
                write_catalog(ws, payload)
                with self.assertRaises(ValueError):
                    load_reply_heuristics(ws)

    def test_schema2_owner_address_type_drift_fails_loud(self) -> None:
        with _CatalogWorkspace() as ws:
            write_catalog(
                ws,
                {
                    "schema_version": 2,
                    "reply_heuristics": {
                        "reply_triggers": ["hallo klaus"],
                        "owner_address": 123,
                    },
                },
            )
            with self.assertRaises(ValueError):
                load_reply_heuristics(ws)

    # --- Schema-2 optional desk-signals fields ----------------------------

    def test_new_optional_fields_default_when_absent(self) -> None:
        with _CatalogWorkspace() as ws:
            write_catalog(
                ws,
                {
                    "schema_version": 1,
                    "reply_heuristics": {"reply_triggers": ["hallo klaus"]},
                },
            )
            config = self._load(ws)

        self.assertEqual(
            self._new_field(config, "sent_sender_domain_whitelist"),
            DEFAULT_SENT_SENDER_DOMAIN_WHITELIST,
        )
        # Stopwords originate from a set; membership is the contract, order is not.
        self.assertEqual(
            set(self._new_field(config, "sent_subject_stopwords") or ()),
            set(DEFAULT_SENT_SUBJECT_STOPWORDS),
        )
        self.assertEqual(
            self._new_field(config, "internal_domains"), DEFAULT_INTERNAL_DOMAINS
        )
        self.assertEqual(
            self._new_field(config, "spam_sender_allowlist"), DEFAULT_SPAM_SENDER_ALLOWLIST
        )

    def test_new_optional_fields_override_when_present(self) -> None:
        with _CatalogWorkspace() as ws:
            write_catalog(
                ws,
                {
                    "schema_version": 2,
                    "reply_heuristics": {
                        "reply_triggers": ["hallo klaus"],
                        "sent_sender_domain_whitelist": ["example.org", "example.net"],
                        "sent_subject_stopwords": ["re", "fwd"],
                        "internal_domains": ["example.org"],
                        "spam_sender_allowlist": ["trusted@example.org"],
                    },
                },
            )
            config = self._load(ws)

        self.assertEqual(
            tuple(self._new_field(config, "sent_sender_domain_whitelist")),
            ("example.org", "example.net"),
        )
        self.assertEqual(
            tuple(self._new_field(config, "sent_subject_stopwords")), ("re", "fwd")
        )
        self.assertEqual(
            tuple(self._new_field(config, "internal_domains")), ("example.org",)
        )
        self.assertEqual(
            tuple(self._new_field(config, "spam_sender_allowlist")),
            ("trusted@example.org",),
        )

    def test_new_optional_fields_type_drift_fails_loud(self) -> None:
        drifts = {
            "domain whitelist not a list": {"sent_sender_domain_whitelist": "example.org"},
            "stopword item not a string": {"sent_subject_stopwords": ["re", 5]},
            "empty internal domain": {"internal_domains": [""]},
            "allowlist not a list": {"spam_sender_allowlist": {"trusted": True}},
        }
        for label, drift in drifts.items():
            block = {"reply_triggers": ["hallo klaus"], **drift}
            with self.subTest(case=label), _CatalogWorkspace() as ws:
                write_catalog(ws, {"schema_version": 2, "reply_heuristics": block})
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


class ClassifyEmailReplyCatalogIntegrationTests(_ReplyCatalogTestCase):
    """The facade consumes the workspace catalog instead of the hardcoded list."""

    ALTERNATIVE_MAIL = "Hallo Klaus, bitte sende mir das Protokoll."
    # The former hardcoded owner: without a catalog it must NOT trigger a reply.
    FORMER_DEFAULT_OWNER_MAIL = "Hallo Martin, bitte sende mir das Protokoll."

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
        try:
            return classifier.classify_email(
                email,
                workspace_root=workspace_root,
                projects=[],
                topics=[],
            )
        except ValueError as exc:
            self.fail(f"valid catalog must classify; loader rejected it: {exc}")

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

    def test_neutral_fallback_does_not_fire_for_alternative_owner(self) -> None:
        with _CatalogWorkspace() as ws:
            item = self._classify(ws, self._email(preview=self.ALTERNATIVE_MAIL))

        self.assertFalse(item["decision"]["needs_reply"])

    def test_missing_catalog_fires_no_trigger_for_any_owner(self) -> None:
        with _CatalogWorkspace() as ws:
            item = self._classify(ws, self._email(preview=self.FORMER_DEFAULT_OWNER_MAIL))

        self.assertFalse(item["decision"]["needs_reply"])

    def test_schema2_owner_derived_trigger_sets_needs_reply(self) -> None:
        with _CatalogWorkspace() as ws:
            write_catalog(
                ws,
                {
                    "schema_version": 2,
                    "reply_heuristics": {"owner_address": "klaus.weber@example.org"},
                },
            )
            item = self._classify(ws, self._email(preview=self.ALTERNATIVE_MAIL))

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
