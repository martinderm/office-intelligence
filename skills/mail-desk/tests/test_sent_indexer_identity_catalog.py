"""FR-22/MD-ID2 sent-index identity catalog contracts (tests-only Red phase).

The sent-items reply matcher in ``core/sent_indexer.py`` currently owns two
identity-bearing constants: the sender-domain set used by the ``domain_to_sender``
heuristic (HEAD ``sent_indexer.py:377``) and the subject stopword set that feeds
keyword extraction (HEAD ``sent_indexer.py:362``, including ``"boku"``).  FR-22/
MD-ID2 moves both into the workspace desk-signals catalog, consumed through the
``ReplyHeuristicsConfig`` fields ``sent_sender_domain_whitelist`` and
``sent_subject_stopwords`` that MD-ID1 already introduced (schema 2, optional,
documented defaults mirroring today's constants).

MD-ID2 contract pinned by this suite (the implementation must satisfy it):

* ``check_if_replied(email, sent_lookup, identity_config=...)`` accepts an optional
  ``ReplyHeuristicsConfig``.  ``None`` resolves to the documented default identity
  (the MD-ID1 dataclass defaults, no catalog file I/O); with it the outcome stays
  byte-identical to HEAD.
* ``check_if_replied`` consumes ``identity_config.sent_sender_domain_whitelist`` in
  the ``domain_to_sender`` branch and ``identity_config.sent_subject_stopwords`` in
  keyword extraction; overriding either changes the observable outcome.
* ``auto_resolve_replies_from_sent(data_dir=..., workspace_root=...)`` threads the
  same identity config (the existing ``core/modes/execute.py`` call sites already
  pass ``workspace_root``).
* ``core.sent_indexer`` re-uses the canonical MD-ID1 defaults
  (``DEFAULT_SENT_SENDER_DOMAIN_WHITELIST`` / ``DEFAULT_SENT_SUBJECT_STOPWORDS``)
  instead of duplicating them, and no ``"boku"`` literal remains in the module.

All fixtures are hermetic temp directories; no mailbox, network or real catalog is
touched.  At HEAD the module-level seam import fails with ``ImportError`` -- the
``sent_indexer`` identity defaults do not exist yet.  That import failure is the
genuine Red.
"""

from __future__ import annotations

import inspect
import json
from pathlib import Path
import sys
import tempfile
import unittest


MAIL_DESK_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(MAIL_DESK_ROOT / "scripts"))

from core import sent_indexer  # noqa: E402
from core.matching import reply_heuristics  # noqa: E402
from core.matching.reply_heuristics import (  # noqa: E402
    DEFAULT_NO_REPLY_SENDER_TOKENS,
    ReplyHeuristicsConfig,
)

# MD-ID2 seam: ``sent_indexer`` must re-use the canonical MD-ID1 defaults instead of
# duplicating them.  At HEAD neither name exists on ``core.sent_indexer``, so this
# import fails loud with ``ImportError`` -- the genuine Red of this suite.
from core.sent_indexer import (  # noqa: E402
    DEFAULT_SENT_SENDER_DOMAIN_WHITELIST as SENT_INDEXER_DOMAIN_WHITELIST,
    DEFAULT_SENT_SUBJECT_STOPWORDS as SENT_INDEXER_SUBJECT_STOPWORDS,
)


CATALOG_RELATIVE_PATH = Path("memory") / "references" / "mail-desk" / "mail-desk.json"
SENT_INDEX_NAME = "sent-index.jsonl"
REPLIES_NEEDED_NAME = "replies-needed.jsonl"


class _Workspace:
    """Hermetic temp workspace that never touches a real catalog or mailbox."""

    def __init__(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()

    def __enter__(self) -> Path:
        return Path(self._tmp.name)

    def __exit__(self, *exc: object) -> None:
        self._tmp.cleanup()


def _write_jsonl(path: Path, entries: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for entry in entries:
            handle.write(json.dumps(entry, ensure_ascii=False) + "\n")


def _write_catalog(workspace_root: Path, payload: object) -> Path:
    path = workspace_root / CATALOG_RELATIVE_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _seed_replies_data(workspace_root: Path, *, email: dict, sent: dict) -> Path:
    """Write one sent entry and one open reply case, returning the data dir."""
    data_dir = workspace_root / "data" / "mail-desk"
    data_dir.mkdir(parents=True, exist_ok=True)
    _write_jsonl(data_dir / SENT_INDEX_NAME, [sent])
    _write_jsonl(data_dir / REPLIES_NEEDED_NAME, [email])
    return data_dir


class SentIndexIdentityCatalogTests(unittest.TestCase):
    """MD-ID2 acceptance: the sent-index identity constants come from the config."""

    def _default_config(self) -> ReplyHeuristicsConfig:
        """The documented default identity, constructed without any file I/O."""
        return ReplyHeuristicsConfig(
            reply_triggers=(),
            no_reply_sender_tokens=DEFAULT_NO_REPLY_SENDER_TOKENS,
        )

    def _lookup(self, workspace_root: Path, sent_entries: list[dict]) -> dict:
        data_dir = workspace_root / "data" / "mail-desk"
        data_dir.mkdir(parents=True, exist_ok=True)
        _write_jsonl(data_dir / SENT_INDEX_NAME, sent_entries)
        return sent_indexer.load_sent_index(data_dir)

    def _incoming(
        self,
        *,
        from_: str,
        subject: str = "Projektplanung",
        date: str = "2026-09-20T10:00:00Z",
    ) -> dict:
        return {
            "message_id": "<incoming-md-id2@example.test>",
            "subject": subject,
            "from": from_,
            "to": "desk@example.org",
            "date": date,
        }

    def _sent(
        self,
        *,
        to: list[str],
        subject: str = "Projektplanung Termin",
        at: str = "2026-09-21T10:00:00Z",
    ) -> dict:
        return {
            "message_id": "<sent-md-id2@example.test>",
            "to": to,
            "subject": subject,
            "at": at,
            "sent_envelope_id": "5001",
            "from": "Desk Owner <owner@example.org>",
            "references": [],
            "in_reply_to": "",
        }

    # --- Default identity (byte-identical to HEAD) ------------------------

    def test_default_identity_matches_head_constants(self) -> None:
        with _Workspace() as ws:
            lookup = self._lookup(ws, [self._sent(to=["other@example.org"])])
            email = self._incoming(from_="Person <person@example.org>")

            implicit = sent_indexer.check_if_replied(email, lookup)
            explicit = sent_indexer.check_if_replied(
                email, lookup, identity_config=self._default_config()
            )

        self.assertEqual(
            implicit,
            explicit,
            "identity_config=None must resolve to the documented MD-ID1 default "
            "identity without reading a catalog file",
        )
        self.assertIsNotNone(
            implicit,
            "the default identity must keep the HEAD domain_to_sender candidate "
            "for a sender outside the documented whitelist",
        )
        self.assertFalse(implicit["replied"])
        self.assertTrue(implicit["has_candidate"])
        self.assertEqual("medium", implicit["confidence"])
        self.assertEqual(["projektplanung"], implicit["candidate"]["matched_keywords"])
        self.assertEqual(
            "other@example.org", implicit["candidate"]["matched_recipient"]
        )

    def test_default_whitelist_keeps_boku_domain_out_of_candidate_search(self) -> None:
        with _Workspace() as ws:
            lookup = self._lookup(ws, [self._sent(to=["other@boku.ac.at"])])
            email = self._incoming(from_="Person <person@boku.ac.at>")
            result = sent_indexer.check_if_replied(email, lookup)

        self.assertIsNone(
            result,
            "boku.ac.at is in the documented default whitelist, so the "
            "domain_to_sender heuristic must not produce a candidate (HEAD behavior)",
        )

    # --- Catalog overrides (observable difference) ------------------------

    def test_catalog_domain_whitelist_override_changes_domain_to_sender_outcome(
        self,
    ) -> None:
        override = ReplyHeuristicsConfig(
            reply_triggers=(),
            no_reply_sender_tokens=DEFAULT_NO_REPLY_SENDER_TOKENS,
            sent_sender_domain_whitelist=("example.org",),
        )
        with _Workspace() as ws:
            lookup = self._lookup(ws, [self._sent(to=["other@boku.ac.at"])])
            email = self._incoming(from_="Person <person@boku.ac.at>")
            default_result = sent_indexer.check_if_replied(email, lookup)
            override_result = sent_indexer.check_if_replied(
                email, lookup, identity_config=override
            )

        self.assertIsNone(default_result)
        self.assertIsNotNone(
            override_result,
            "removing boku.ac.at from the whitelist must let the domain_to_sender "
            "heuristic match the boku recipient",
        )
        self.assertFalse(override_result["replied"])
        self.assertTrue(override_result["has_candidate"])
        self.assertEqual(
            ["projektplanung"], override_result["candidate"]["matched_keywords"]
        )

    def test_catalog_stopword_override_changes_keyword_extraction(self) -> None:
        override = ReplyHeuristicsConfig(
            reply_triggers=(),
            no_reply_sender_tokens=DEFAULT_NO_REPLY_SENDER_TOKENS,
            sent_subject_stopwords=("projektplanung",),
        )
        with _Workspace() as ws:
            lookup = self._lookup(ws, [self._sent(to=["other@example.org"])])
            email = self._incoming(from_="Person <person@example.org>")
            default_result = sent_indexer.check_if_replied(email, lookup)
            override_result = sent_indexer.check_if_replied(
                email, lookup, identity_config=override
            )

        self.assertIsNotNone(default_result)
        self.assertEqual(
            ["projektplanung"], default_result["candidate"]["matched_keywords"]
        )
        self.assertIsNone(
            override_result,
            "a configured stopword must drop the subject keyword so no candidate "
            "remains",
        )

    # --- Threading seam ---------------------------------------------------

    def test_public_seams_accept_the_identity_config(self) -> None:
        check_params = inspect.signature(sent_indexer.check_if_replied).parameters
        self.assertIn(
            "identity_config",
            check_params,
            "check_if_replied must accept the catalog identity config",
        )
        self.assertIsNone(
            check_params["identity_config"].default,
            "identity_config must default to None (documented default identity)",
        )

        auto_params = inspect.signature(
            sent_indexer.auto_resolve_replies_from_sent
        ).parameters
        self.assertIn(
            "workspace_root",
            auto_params,
            "auto_resolve_replies_from_sent must accept the workspace_root that "
            "core/modes/execute.py already passes",
        )
        self.assertIsNone(
            auto_params["workspace_root"].default,
            "workspace_root must default to None (documented default identity)",
        )

    def test_auto_resolve_threads_the_workspace_identity_config(self) -> None:
        catalog = {
            "schema_version": 2,
            "reply_heuristics": {
                "owner_address": "desk@example.org",
                "sent_sender_domain_whitelist": ["example.org"],
            },
        }
        email = self._incoming(from_="Person <person@boku.ac.at>")
        sent = self._sent(to=["other@boku.ac.at"])

        with _Workspace() as override_ws:
            _write_catalog(override_ws, catalog)
            override_dir = _seed_replies_data(override_ws, email=email, sent=sent)
            overridden = sent_indexer.auto_resolve_replies_from_sent(
                data_dir=override_dir, workspace_root=override_ws
            )

        with _Workspace() as default_ws:
            default_dir = _seed_replies_data(default_ws, email=email, sent=sent)
            default = sent_indexer.auto_resolve_replies_from_sent(
                data_dir=default_dir, workspace_root=default_ws
            )

        self.assertEqual(0, overridden["resolved_count"])
        self.assertEqual(0, default["resolved_count"])
        self.assertIsNotNone(
            overridden["remaining_open"][0]["reply_candidate"],
            "the catalog override must thread into the auto-resolve candidate search",
        )
        self.assertIsNone(
            default["remaining_open"][0]["reply_candidate"],
            "without the override the default whitelist keeps boku out of the search",
        )

    # --- Canonical-default sourcing (Green-phase pins) --------------------

    def test_sent_indexer_reuses_canonical_identity_defaults(self) -> None:
        self.assertIs(
            SENT_INDEXER_DOMAIN_WHITELIST,
            reply_heuristics.DEFAULT_SENT_SENDER_DOMAIN_WHITELIST,
            "sent_indexer must import the canonical sender-domain default instead of "
            "duplicating it",
        )
        self.assertIs(
            SENT_INDEXER_SUBJECT_STOPWORDS,
            reply_heuristics.DEFAULT_SENT_SUBJECT_STOPWORDS,
            "sent_indexer must import the canonical stopword default instead of "
            "duplicating it",
        )

    def test_no_boku_literal_remains_in_sent_indexer_source(self) -> None:
        source = (MAIL_DESK_ROOT / "scripts" / "core" / "sent_indexer.py").read_text(
            encoding="utf-8"
        )
        self.assertNotIn(
            "boku",
            source.lower(),
            "the identity-bearing boku literals must be removed from sent_indexer.py",
        )


if __name__ == "__main__":
    unittest.main()
