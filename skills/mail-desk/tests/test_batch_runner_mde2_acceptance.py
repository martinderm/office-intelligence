"""FR-15 / MD-E2-T04 package acceptance: one hermetic clarifying-attachment call path.

This is a focused, package-level acceptance test (evidence mode ``acceptance``): it proves
the already-implemented MD-E2-T01..T03 behavior end-to-end through the single approved
public seam ``run_draft_mode(config, account, data_dir, dependencies)`` and adds no
production change.

The whole path runs with the **real defaults**: the real ``draft_manifest`` two-pass
classifier, the real ``classify_email``/classifier rules, the real MD-E1
``attachment_evaluate`` orchestrator, the real canonical catalog loading and the real
DraftManifest installation. Fakes exist only at the true external boundaries -- message
listing (``get_unprocessed_emails``), the Full Read (``get_single_email_details``), the raw
RFC-822 acquisition (``fetch_raw_message_eml``) -- plus the unavoidable hermetic
tracked-quarantine Git preflight and the progress tracker. ``mde2_fixtures`` is deliberately
not used here: the MD-E1 handoff is produced by the real backend, not a canonical fixture.

Proven in one call path:

1. Body/Full Read stay ambiguous (preview and full read both ``unknown``).
2. A real ``text/plain`` attachment supplies a project catalog signal; the validated
   ``ready`` handoff is consumed **once** as a distinct, encapsulated
   ``<untrusted_attachment_content>`` input, producing a clear project decision/action.
3. The final ``DraftManifest`` is really installed and persisted, carrying a bounded
   ``attachment_evaluation`` with ``used_for_classification: true`` and a 64-hex
   ``classifier_revision`` content-bound to the active classifier rules plus the actual
   attachment SHA-256.
4. The persisted manifest leaks no raw MIME, attachment text, ``prompt_content``,
   attachment-derived absolute quarantine path, capability, approval receipt or machine
   authorization.

The mandatory negative matrix, mixed-batch isolation, no-double-fetch and bounded,
visible truncation are covered by the already-green focused suites this ticket's
verification reruns (``test_batch_runner_mde2_draft.py``,
``test_batch_runner_mde2_hardening.py`` including
``Mde2StillAmbiguousTests.test_truncation_coverage_is_visible_and_preserved``,
``test_batch_runner_mde2_inspect.py`` and ``test_maildesk_attachment_evaluation_mde1.py``);
this file proves the single real-path happy chain without duplicating those.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

MAIL_DESK_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(MAIL_DESK_ROOT / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import mail_desk_batch_runner as runner  # noqa: E402
from core import attachments  # noqa: E402
from core import classifier  # noqa: E402
from core import himalaya  # noqa: E402
from core import attachment_fetch as afetch  # noqa: E402
from core import attachment_reclassification as reclass  # noqa: E402
from core.modes import draft as draft_mode  # noqa: E402

_LEASE = "lease-mde2-t04"
_CONV = "conv-mde2-t04"
_ABS_PATH_RE = r"[A-Za-z]:\\\\"

_ENVELOPE_ID = "9100"
_MESSAGE_ID = "mde2-t04-acceptance@example.test"
_ATTACHMENT_FILENAME = "clue.txt"
#: The real text/plain attachment payload: a project catalog signal (name + keyword)
#: that only becomes visible through the MD-E1 untrusted handoff.
_ATTACHMENT_TEXT = "Project QMD handover checklist: all items ready for review."
_ATTACHMENT_SHA = hashlib.sha256(_ATTACHMENT_TEXT.encode("utf-8")).hexdigest()
_RAW_MIME_BODY = "MDE2 T04 acceptance body; no attachment context here."

_PROJECT = {
    "id": "qmd",
    "title": "Quality Management Data",
    "kuerzel": "QMD",
    "mailbox_folder": "Projekte/QMD",
    "aliases": ["Quality Management Data"],
    "keywords": ["handover"],
    "domains": [],
    "contacts": [],
    "typical_subject_patterns": [],
    "do_not_route_if": [],
    "workpackages": [],
    "milestones": [],
    "schema_version": 3,
}


class _Tracker:
    def __init__(self, **_kwargs: object) -> None:
        pass

    def step(self, *_args: object, **_kwargs: object) -> None:
        pass

    def advance_item(self, *_args: object, **_kwargs: object) -> None:
        pass

    def complete(self, *_args: object, **_kwargs: object) -> None:
        pass


def _preview_email() -> dict[str, object]:
    return {
        "envelope_id": _ENVELOPE_ID,
        "folder": "INBOX",
        "message_id": _MESSAGE_ID,
        "subject": "Kurzfrage",
        "from": "sender@example.test",
        "to": "desk@example.test",
        "cc": "",
        "date": "Fri, 18 Sep 2026 09:00:00 +0200",
        "preview": "Kurze Rueckfrage ohne weitere Details.",
    }


class Mde2PackageAcceptanceTests(unittest.TestCase):
    """One hermetic call path: ambiguous mail + real clarifying attachment -> clear draft."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.workspace = Path(self._tmp.name)
        self._write_catalogs()

        guard = afetch._load_workspace_lock_guard()
        guard.acquire_workspace_lock(
            str(self.workspace), harness="mde2-t04-acceptance", lease_id=_LEASE, conversation_id=_CONV
        )
        self.addCleanup(
            lambda: guard._invoke(
                guard._command("release", Path(self.workspace), "--lease-id", _LEASE),
                None,
            )
        )
        preflight = patch.object(
            afetch, "verify_no_tracked_quarantine", return_value=None, create=True
        )
        preflight.start()
        self.addCleanup(preflight.stop)

        self.data_dir = self.workspace / "data" / "mail-desk"
        self.data_dir.mkdir(parents=True)

    def _write_catalogs(self) -> None:
        projects = self.workspace / "memory" / "references" / "projects" / "projects.json"
        projects.parent.mkdir(parents=True)
        projects.write_text(
            json.dumps({"catalog_name": "T04 acceptance", "projects": [_PROJECT]}),
            encoding="utf-8",
        )
        topics = self.workspace / "memory" / "references" / "topics" / "topics.json"
        topics.parent.mkdir(parents=True)
        topics.write_text(json.dumps({"topics": []}), encoding="utf-8")

    def _raw_eml(self) -> bytes:
        return attachments.build_test_eml(
            subject="Kurzfrage",
            message_id=f"<{_MESSAGE_ID}>",
            body_text=_RAW_MIME_BODY,
            attachments=[
                {
                    "filename": _ATTACHMENT_FILENAME,
                    "mime_type": "text/plain",
                    "data": _ATTACHMENT_TEXT.encode("utf-8"),
                }
            ],
        )

    def test_clarifying_attachment_reaches_persisted_project_manifest(self) -> None:
        self.assertFalse((self.data_dir / "final-location-index.json").exists())

        email = _preview_email()
        full_email = {**email, "preview": "Vollstaendiger Text, weiterhin unklar."}
        raw_eml = self._raw_eml()
        output_path = self.data_dir / "batch-manifest.json"

        full_reader = Mock(return_value=full_email)
        raw_reader = Mock(return_value=raw_eml)
        dependencies = {
            "BatchProgressTracker": _Tracker,
            "atomic_write_json": runner.atomic_write_json,
            "get_unprocessed_emails": Mock(return_value=([email], 0)),
            "load_sent_index": Mock(return_value={}),
            "get_single_email_details": full_reader,
            "fetch_raw_message_eml": raw_reader,
        }
        config = {
            "count": 1,
            "output_file": str(output_path),
            "lease_id": _LEASE,
            "conversation_id": _CONV,
        }

        # The real classifier rules run; the spy only observes the single reclassification
        # boundary (it delegates to the real ``classify_email`` and returns its result).
        with patch.object(
            draft_mode, "classify_email", wraps=classifier.classify_email
        ) as reclassify_spy, patch.object(
            himalaya,
            "run_himalaya",
            side_effect=RuntimeError("live mailbox/network is forbidden in this hermetic test"),
        ) as himalaya_spy:
            result = draft_mode.run_draft_mode(
                config, account="primary", data_dir=self.data_dir, dependencies=dependencies
            )

        # 1. Real two-pass classification: the preview triggers exactly one Full Read, both
        #    passes stay ambiguous, and only the attachment's project signal resolves it.
        self.assertTrue(output_path.exists())
        full_reader.assert_called_once()
        raw_reader.assert_called_once()
        item = result["draft"]["items"][0]
        base_kwargs = {
            "projects": [_PROJECT],
            "topics": [],
            "sent_lookup": {},
            "final_index": {"items": {}},
        }
        self.assertEqual(
            "unknown",
            classifier.classify_email(_preview_email(), **base_kwargs)["decision"]["kind"],
        )
        self.assertEqual(
            "unknown",
            classifier.classify_email({**email, "preview": full_email["preview"]}, **base_kwargs)[
                "decision"
            ]["kind"],
        )
        self.assertEqual("project", item["decision"]["kind"])
        self.assertEqual("qmd", item["decision"]["id"])
        self.assertEqual(
            {"type": "copy_as_move", "target_folder": "Projekte/QMD"}, item["action"]
        )

        # 2. Exactly one reclassification, fed the effective Full Read source and the
        #    encapsulated untrusted attachment boundary (not the raw preview email).
        reclassify_spy.assert_called_once()
        self.assertEqual("Vollstaendiger Text, weiterhin unklar.", reclassify_spy.call_args.args[0]["preview"])
        untrusted = reclassify_spy.call_args.kwargs["untrusted_external_text"]
        self.assertIn("<untrusted_attachment_content", untrusted)
        self.assertIn("QMD handover", untrusted)

        # 3. Final DraftManifest installed and persisted with a bounded evaluation.
        persisted_text = output_path.read_text(encoding="utf-8")
        self.assertEqual(result["draft"], json.loads(persisted_text))
        installed = item["attachment_evaluation"]
        self.assertEqual(
            {
                "status",
                "reason",
                "authorization",
                "files",
                "used_for_classification",
                "classifier_revision",
            },
            set(installed),
        )
        self.assertEqual("completed", installed["status"])
        self.assertEqual("classification_clear", installed["reason"])
        self.assertEqual("auto_evaluated", installed["authorization"])
        self.assertIs(True, installed["used_for_classification"])
        self.assertRegex(installed["classifier_revision"], r"^[0-9a-f]{64}$")
        self.assertEqual(1, len(installed["files"]))
        safe_file = installed["files"][0]
        self.assertEqual(_ATTACHMENT_FILENAME, safe_file["filename"])
        self.assertEqual(_ATTACHMENT_SHA, safe_file["sha256"])
        self.assertEqual("text/plain", safe_file["mime_type"])
        self.assertEqual("full", safe_file["coverage"])

        # 4. The revision binds the real canonical rules plus the actual consumed hash.
        fingerprint = reclass.classifier_rules_fingerprint(self.workspace)
        self.assertEqual(
            reclass.compute_classifier_revision(fingerprint, [_ATTACHMENT_SHA]),
            installed["classifier_revision"],
        )
        self.assertNotEqual(
            reclass.compute_classifier_revision(fingerprint, ["0" * 64]),
            installed["classifier_revision"],
        )

        # 5. Exactly one real quarantine artifact; the attachment is not re-fetched.
        identity = {
            "account": "primary",
            "folder": "INBOX",
            "envelope_id": _ENVELOPE_ID,
            "message_id": _MESSAGE_ID,
        }
        run_id = reclass.derive_evaluation_run_id(identity)
        quarantine = list((self.data_dir / "attachments").rglob(_ATTACHMENT_FILENAME))
        self.assertEqual(1, len(quarantine))
        self.assertEqual(_ATTACHMENT_TEXT.encode("utf-8"), quarantine[0].read_bytes())
        self.assertEqual(self.data_dir / "attachments" / run_id / _ATTACHMENT_FILENAME, quarantine[0])

        # 6. No leak into the long-lived manifest.
        for token in (
            _ATTACHMENT_TEXT,
            _RAW_MIME_BODY,
            "prompt_content",
            "<untrusted_attachment_content",
            "capability",
            "approval_receipt",
            "receipt_class",
            str(self.workspace),
        ):
            self.assertNotIn(token, persisted_text)
        self.assertNotRegex(persisted_text, _ABS_PATH_RE)

        # 7. No mailbox or downstream mutation and no live process.
        himalaya_spy.assert_not_called()
        self.assertFalse((self.workspace / "memory" / "evidence").exists())
        self.assertFalse((self.workspace / "Projekte").exists())
        self.assertFalse((self.data_dir / "final-location-index.json").exists())
        self.assertEqual([], list(self.data_dir.glob("*.jsonl")))


if __name__ == "__main__":
    unittest.main()
