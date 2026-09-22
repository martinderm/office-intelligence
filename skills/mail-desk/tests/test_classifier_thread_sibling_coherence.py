"""FR-17 / MD-R2 behavior tests for thread and sibling coherence.

Evidence mode ``tdd``, risk tier ``high``.  The ``Target...`` classes define the
MD-R2 target semantics and are Red before the production change in
``core.classifier.classify_email`` (thread fast-path) and the canonical
``core.matching.project_matching.evaluate_do_not_route_signal`` predicate; the
``...Characterization`` classes pin behavior that must stay green before and
after.

Binding interpretation of design Klärung §9 (spec
``2026-09-22-office-intelligence-fr17-routing-determinism``): the direct
``in_reply_to`` parent-folder **inheritance** stays do-not-route-free, exactly as
pinned by ``test_classifier_do_not_route.ThreadInheritanceCharacterizationTests``.
Only the **sibling auto-route** over the ``references`` chain adopts an exact,
unique catalog code and is therefore subject to the shared do-not-route
predicate; when the current mail is suppressed for that catalog entry the mail
stays in ``INBOX`` with a catalog-data-only review hint and is never rerouted.

The 9388/9387 split is reproduced hermetically: the 9388 analog (clean sender)
inherits the ATAEL project through its reference chain, while the 9387 analog
(``no-reply`` sender) must surface a suppressed ATAEL candidate for review
instead of routing into a different project.
"""

from __future__ import annotations

from pathlib import Path
import sys
import unittest

MAIL_DESK_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(MAIL_DESK_ROOT / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import routing_fixtures  # noqa: E402
from core import classifier  # noqa: E402

_ATAEL_DNR = ["newsletter", "no-reply"]
_NEUTRAL_SUBJECT = "For Information - 581 - Grant Management"
_CLEAN_SENDER = "Coordinator <coord@other.test>"


def _classify(
    overrides: dict,
    *,
    projects: list[dict] | None = None,
    topics: list[dict] | None = None,
    final_index: dict | None = None,
) -> dict:
    """Classify one hermetic email through the facade with the fixture catalogs."""
    email = {
        "envelope_id": "fr17-r2",
        "folder": "INBOX",
        "message_id": "<fr17-r2@example.test>",
        "subject": "",
        "from": _CLEAN_SENDER,
        "to": "Martin <martin@other.test>",
        "date": "2026-09-22",
        "preview": "",
    }
    email.update(overrides)
    return classifier.classify_email(
        email,
        projects=list(projects or []),
        topics=list(topics or []),
        sent_lookup={},
        final_index=final_index if final_index is not None else {"items": {}},
    )


def _atael_parent_index() -> dict:
    return routing_fixtures.final_location_index(
        routing_fixtures.final_location_entry(
            routing_fixtures.PARENT_MESSAGE_ID, routing_fixtures.ATAEL_MAILBOX_FOLDER
        )
    )


class ThreadParentInheritanceCharacterizationTests(unittest.TestCase):
    """Characterization (green now and after): the direct parent-folder path is unchanged."""

    def test_direct_parent_folder_inherits_catalog_project(self) -> None:
        item = _classify(
            {
                "subject": _NEUTRAL_SUBJECT,
                "in_reply_to": f"<{routing_fixtures.PARENT_MESSAGE_ID}>",
            },
            projects=[routing_fixtures.project_atael()],
            final_index=_atael_parent_index(),
        )

        self.assertEqual("project", item["decision"]["kind"])
        self.assertEqual("atael", item["decision"]["id"])
        self.assertEqual("high", item["decision"]["confidence"])
        self.assertEqual(
            routing_fixtures.ATAEL_MAILBOX_FOLDER, item["action"]["target_folder"]
        )

    def test_direct_parent_inheritance_keeps_working_with_a_do_not_route_sender(self) -> None:
        item = _classify(
            {
                "subject": "Newsletter: For Information - ATAEL",
                "from": routing_fixtures.NO_REPLY_SENDER,
                "in_reply_to": f"<{routing_fixtures.PARENT_MESSAGE_ID}>",
            },
            projects=[routing_fixtures.project_atael(do_not_route_if=_ATAEL_DNR)],
            final_index=_atael_parent_index(),
        )

        self.assertEqual("project", item["decision"]["kind"])
        self.assertEqual("atael", item["decision"]["id"])
        self.assertEqual(
            routing_fixtures.ATAEL_MAILBOX_FOLDER, item["action"]["target_folder"]
        )

    def test_unowned_direct_parent_folder_falls_back_to_that_folder(self) -> None:
        final_index = routing_fixtures.final_location_index(
            routing_fixtures.final_location_entry(
                routing_fixtures.PARENT_MESSAGE_ID, routing_fixtures.UNOWNED_MAILBOX_FOLDER
            )
        )

        item = _classify(
            {
                "subject": _NEUTRAL_SUBJECT,
                "in_reply_to": f"<{routing_fixtures.PARENT_MESSAGE_ID}>",
            },
            projects=[routing_fixtures.project_atael()],
            final_index=final_index,
        )

        self.assertEqual("other", item["decision"]["kind"])
        self.assertEqual(routing_fixtures.UNOWNED_MAILBOX_FOLDER, item["decision"]["id"])
        self.assertEqual(
            routing_fixtures.UNOWNED_MAILBOX_FOLDER, item["action"]["target_folder"]
        )

    def test_inbox_parent_reference_does_not_match_a_thread(self) -> None:
        final_index = routing_fixtures.final_location_index(
            routing_fixtures.final_location_entry(routing_fixtures.PARENT_MESSAGE_ID, "INBOX")
        )

        item = _classify(
            {
                "subject": _NEUTRAL_SUBJECT,
                "in_reply_to": f"<{routing_fixtures.PARENT_MESSAGE_ID}>",
            },
            projects=[routing_fixtures.project_atael()],
            final_index=final_index,
        )

        self.assertEqual("unknown", item["decision"]["kind"])
        self.assertEqual("unclassified", item["decision"]["id"])
        self.assertEqual("INBOX", item["action"]["target_folder"])

    def test_unknown_sibling_reference_leaves_routing_unchanged(self) -> None:
        item = _classify(
            {
                "subject": _NEUTRAL_SUBJECT,
                "references": f"<{routing_fixtures.UNKNOWN_MESSAGE_ID}>",
            },
            projects=[routing_fixtures.project_atael()],
            final_index={"items": {}},
        )

        self.assertEqual("unknown", item["decision"]["kind"])
        self.assertEqual("unclassified", item["decision"]["id"])
        self.assertEqual("INBOX", item["action"]["target_folder"])

    def test_conflicting_sibling_chain_codes_create_no_unique_project_target(self) -> None:
        final_index = routing_fixtures.final_location_index(
            routing_fixtures.final_location_entry(
                "unowned.9000@example.test", routing_fixtures.UNOWNED_MAILBOX_FOLDER
            ),
            routing_fixtures.final_location_entry(
                routing_fixtures.PARENT_MESSAGE_ID, routing_fixtures.ATAEL_MAILBOX_FOLDER
            ),
            routing_fixtures.final_location_entry(
                "orion.9001@example.test", routing_fixtures.ORION_MAILBOX_FOLDER
            ),
        )

        item = _classify(
            {
                "subject": _NEUTRAL_SUBJECT,
                "references": (
                    "<unowned.9000@example.test> "
                    f"<{routing_fixtures.PARENT_MESSAGE_ID}> "
                    "<orion.9001@example.test>"
                ),
            },
            projects=[routing_fixtures.project_atael(), routing_fixtures.project_orion()],
            final_index=final_index,
        )

        self.assertEqual("other", item["decision"]["kind"])
        self.assertEqual(
            routing_fixtures.UNOWNED_MAILBOX_FOLDER, item["action"]["target_folder"]
        )
        self.assertNotIn("suppressed_candidates", item["decision"])

    def test_body_only_catalog_token_cannot_derive_a_project_target(self) -> None:
        item = _classify(
            {
                "subject": _NEUTRAL_SUBJECT,
                "preview": "ATAEL follow-up notes without a reference chain",
            },
            projects=[routing_fixtures.project_atael()],
            final_index={"items": {}},
        )

        self.assertEqual("unknown", item["decision"]["kind"])
        self.assertEqual("unclassified", item["decision"]["id"])
        self.assertEqual("INBOX", item["action"]["target_folder"])


class SiblingAutoRouteCharacterizationTests(unittest.TestCase):
    """Characterization (green now and after): a clean sibling chain keeps inheriting."""

    def test_clean_sender_sibling_chain_inherits_the_exact_project_code(self) -> None:
        item = _classify(
            {
                "subject": _NEUTRAL_SUBJECT,
                "references": f"<{routing_fixtures.PARENT_MESSAGE_ID}>",
            },
            projects=[routing_fixtures.project_atael()],
            final_index=_atael_parent_index(),
        )

        self.assertEqual("project", item["decision"]["kind"])
        self.assertEqual("atael", item["decision"]["id"])
        self.assertEqual(
            routing_fixtures.ATAEL_MAILBOX_FOLDER, item["action"]["target_folder"]
        )


class TargetSiblingDoNotRouteGateTests(unittest.TestCase):
    """Target (Red now): the sibling auto-route is do-not-route-gated, never a silent reroute."""

    def _blocked_item(self) -> dict:
        return _classify(
            {
                "subject": _NEUTRAL_SUBJECT,
                "from": routing_fixtures.NO_REPLY_SENDER,
                "references": f"<{routing_fixtures.PARENT_MESSAGE_ID}>",
            },
            projects=[routing_fixtures.project_atael(do_not_route_if=_ATAEL_DNR)],
            final_index=_atael_parent_index(),
        )

    def test_suppressed_sibling_chain_keeps_the_review_decision(self) -> None:
        item = self._blocked_item()

        self.assertEqual("unknown", item["decision"]["kind"])
        self.assertEqual("unclassified", item["decision"]["id"])
        self.assertIs(True, item["decision"].get("review_required"))
        self.assertEqual(
            "do_not_route_suppressed", item["decision"].get("review_reason")
        )

    def test_suppressed_sibling_chain_stays_in_inbox_without_a_reroute(self) -> None:
        item = self._blocked_item()

        self.assertEqual("keep_in_folder", item["action"]["type"])
        self.assertEqual("INBOX", item["action"]["target_folder"])

    def test_suppressed_sibling_chain_reports_a_catalog_data_only_candidate(self) -> None:
        item = self._blocked_item()
        rows = item["decision"].get("suppressed_candidates", [])

        self.assertEqual(
            [{"kind": "project", "id": "atael", "suppression_reason": "no-reply"}],
            rows,
        )
        self.assertEqual({"kind", "id", "suppression_reason"}, set(rows[0].keys()))

    def test_suppressed_sibling_chain_does_not_claim_a_thread_inheritance(self) -> None:
        item = self._blocked_item()

        self.assertNotIn("Thread-Vererbung", item["notes"])


if __name__ == "__main__":
    unittest.main()
