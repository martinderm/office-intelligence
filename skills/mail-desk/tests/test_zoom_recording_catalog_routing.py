"""FR-18/MD-S3 Zoom-recording catalog-routing contracts (tests-only Red phase).

The base classifier must not decide *where* a Zoom-recording notification belongs.
At HEAD ``core.classifier`` carries a hardcoded branch (``classifier.py:557-568``)
that matches a recording subject and routes it to the literal
``Themen/BOKU-Organisation`` / ``id="boku-organisation"`` whenever no project or
topic matched earlier.

MD-S3 removes that branch: the routing target belongs to the consuming
workspace's topic catalog (``memory/references/topics/topics.json``), whose topic
entry declares the recording signals via ``typical_subject_patterns`` /
``keywords``.  The bundle keeps only the workspace-independent generic
``zoom-join-ping`` -> ``Trash`` heuristic.  A workspace without a matching catalog
entry must fail closed into ``unknown`` / ``unclassified`` in ``INBOX`` instead of
silently mis-routing (FR-18 MD-S3 acceptance).

Test map (one MD-S3 acceptance criterion each):

* ``test_consumer_catalog_routes_zoom_recording`` -- a consuming workspace whose
  topic entry covers the recording signals routes exactly as before (characterization:
  this already resolves through catalog matching at HEAD, proving the catalog seam
  carries the routing).
* ``test_zoom_recording_without_catalog_entry_is_unknown`` -- the removal case: no
  catalog entry yields ``unknown``/``unclassified`` in ``INBOX`` (RED at HEAD).
* ``test_no_zoom_topic_literals_in_classifier_source`` -- source-level pin: the
  ``boku-organisation`` / ``BOKU-Organisation`` literals leave ``classifier.py``
  (RED at HEAD).
* ``test_zoom_join_ping_trash_regression`` -- the generic join-ping heuristic stays
  workspace-independent and unchanged (regression floor, green at HEAD and after).

All fixtures are hermetic temp workspaces; ``classify_email`` is called directly,
no mailbox/network/catalog outside the temporary directory is touched.
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


TOPICS_RELATIVE_PATH = Path("memory") / "references" / "topics" / "topics.json"
CLASSIFIER_SOURCE_PATH = MAIL_DESK_ROOT / "scripts" / "core" / "classifier.py"

#: The topic literal that must no longer be baked into the bundle classifier.
REMOVED_TOPIC_ID = "boku-organisation"
REMOVED_TOPIC_LITERAL = "BOKU-Organisation"

#: Consumer topic entry, shaped like a real ``topics.json`` root entry (schema 1).
#: ``typical_subject_patterns`` are matched as substrings / word-bounded patterns,
#: so ``"Meeting-Objekte"`` covers the characterized
#: ``"Meeting-Objekte für … sind bereit"`` recording subject and
#: ``"Cloud-Aufzeichnung"`` the availability notice.
BOKU_ORGANISATION_TOPIC = {
    "id": REMOVED_TOPIC_ID,
    "title": "BOKU-Organisation",
    "mailbox_folder": "Themen/BOKU-Organisation",
    "keywords": ["Cloud-Aufzeichnung", "Meeting-Objekte"],
    "typical_subject_patterns": ["Meeting-Objekte", "Cloud-Aufzeichnung"],
    "routing_priority": 70,
    "schema_version": 1,
}


class _CatalogWorkspace:
    """Hermetic temp workspace that never touches real catalogs or mailboxes."""

    def __init__(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()

    def __enter__(self) -> Path:
        return Path(self._tmp.name)

    def __exit__(self, *exc: object) -> None:
        self._tmp.cleanup()


def write_topics(workspace_root: Path, topics: list[dict]) -> Path:
    """Write a workspace ``topics.json`` from the given root-topic entries."""
    path = workspace_root / TOPICS_RELATIVE_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(topics), encoding="utf-8")
    return path


def zoom_recording_email() -> dict:
    """A Zoom cloud-recording availability notification (the MD-S3 subject)."""
    return {
        "envelope_id": "9419",
        "folder": "INBOX",
        "message_id": "<zoom-recording@example.test>",
        "subject": "Meeting-Objekte für Organisationsmeeting sind bereit",
        "from": "Zoom <no-reply@zoom.us>",
        "to": "desk@example.org",
        "preview": (
            "Cloud-Aufzeichnung ist jetzt verfügbar: the zoom recording for the "
            "meeting is now available."
        ),
    }


def zoom_join_ping_email() -> dict:
    """A generic Zoom join notification (the workspace-independent heuristic)."""
    return {
        "envelope_id": "9087",
        "folder": "INBOX",
        "message_id": "<zoom-join-ping@example.test>",
        "subject": "Martin ist dem Meeting beigetreten",
        "from": "Zoom <no-reply@zoom.us>",
        "to": "desk@example.org",
        "preview": "Martin ist dem Meeting beigetreten.",
    }


def classify_in_workspace(workspace_root: Path, email: dict) -> dict:
    """Classify via the catalog loader of ``workspace_root`` (no explicit catalogs)."""
    return classifier.classify_email(email, workspace_root=workspace_root)


class ZoomRecordingCatalogRoutingTests(unittest.TestCase):
    """MD-S3: recording routing comes from the consumer topic catalog."""

    def test_consumer_catalog_routes_zoom_recording(self) -> None:
        with _CatalogWorkspace() as ws:
            write_topics(ws, [BOKU_ORGANISATION_TOPIC])
            item = classify_in_workspace(ws, zoom_recording_email())

        decision = item["decision"]
        self.assertEqual("topic", decision["kind"])
        self.assertEqual(REMOVED_TOPIC_ID, decision["id"])
        self.assertFalse(decision["needs_reply"])
        self.assertEqual(
            {"type": "copy_as_move", "target_folder": "Themen/BOKU-Organisation"},
            item["action"],
        )

    def test_zoom_recording_without_catalog_entry_is_unknown(self) -> None:
        with _CatalogWorkspace() as ws:
            item = classify_in_workspace(ws, zoom_recording_email())

        decision = item["decision"]
        # No catalog entry -> fail closed, never the hardcoded BOKU-Organisation topic.
        self.assertEqual("unknown", decision["kind"])
        self.assertEqual("unclassified", decision["id"])
        self.assertFalse(decision["needs_reply"])
        self.assertEqual("keep_in_folder", item["action"]["type"])
        self.assertEqual("INBOX", item["action"]["target_folder"])
        self.assertNotEqual(REMOVED_TOPIC_ID, decision.get("id"))

    def test_no_zoom_topic_literals_in_classifier_source(self) -> None:
        source = CLASSIFIER_SOURCE_PATH.read_text(encoding="utf-8")
        self.assertNotIn(REMOVED_TOPIC_ID, source)
        self.assertNotIn(REMOVED_TOPIC_LITERAL, source)


class ZoomJoinPingRegressionTests(unittest.TestCase):
    """MD-S3 regression floor: the generic join-ping heuristic is unchanged."""

    def _assert_trash_join_ping(self, workspace_root: Path) -> None:
        item = classify_in_workspace(workspace_root, zoom_join_ping_email())
        self.assertEqual("notification", item["decision"]["kind"])
        self.assertEqual("zoom-join-ping", item["decision"]["id"])
        self.assertFalse(item["decision"]["needs_reply"])
        self.assertEqual("Trash", item["action"]["target_folder"])

    def test_zoom_join_ping_trash_regression(self) -> None:
        # Workspace without any catalog: the heuristic must not depend on a catalog.
        with _CatalogWorkspace() as ws:
            self._assert_trash_join_ping(ws)

    def test_zoom_join_ping_is_not_hijacked_by_the_topic_catalog(self) -> None:
        # And with the recording topic catalog present the join ping still wins as Trash.
        with _CatalogWorkspace() as ws:
            write_topics(ws, [BOKU_ORGANISATION_TOPIC])
            self._assert_trash_join_ping(ws)


if __name__ == "__main__":
    unittest.main()
