"""FR-13 / MD-M1-T02 structural Red tests for the project-matching vertical slice.

These tests define the complete project boundary for MD-M1-T02 before any production
exists (evidence mode ``tdd``, risk tier ``high``):

1. ``core.matching.project_matching`` is importable and canonically owns the baseline
   artifact primitives, artifact candidate/selection, catalog artifact title, project
   context label, project evidence builder and the shared evidence-read escalation
   helper that the topic evidence builders reuse.
2. Every moved baseline callable stays object-identical through the ``core.classifier``
   compatibility facade.
3. One concrete owner callable -- ``select_project_match`` -- owns the inline root
   project-catalog matching loop, and ``classify_email`` routes its root project decision
   through that owner.
4. Representative root match strengths/order, artifact resolution/ambiguity, evidence
   shape/path and target behaviour remain unchanged for the characterized fixtures.
5. The active fingerprint source set gains ``project_matching.py`` (asserted in
   ``test_classifier_matching_modules.py``); a semantic AST change in the project module
   must move the digest.

T02 owns only ``project_matching.py`` plus the facade re-exports; the topic module
arrives with MD-M1-T03 and is deliberately not required here.  The root-match owner
contract is asserted on observable results (which catalog project wins, its confidence
and its catalog payload), never on the internal loop layout.

Owner callable contract::

    select_project_match(
        projects, *, subject, full_text, full_text_lower, from_str, to_str, parties
    ) -> {"id", "folder", "name", "confidence", "catalog"} | None
"""

from __future__ import annotations

from contextlib import contextmanager
import importlib
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

MAIL_DESK_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(MAIL_DESK_ROOT / "scripts"))

from core import classifier  # noqa: E402

_OWNER_MODULE = "core.matching.project_matching"

# Project-assigned baseline helpers that must have exactly one canonical owner in
# ``core.matching.project_matching`` and stay object-identical through the facade.
_OWNED_CALLABLES = (
    "_artifact_text_matches",
    "_artifact_code_matches",
    "_artifact_candidate",
    "_select_project_artifacts",
    "_catalog_artifact_title",
    "_project_context_label",
    "_evidence_read_escalation",
    "_build_project_evidence",
)

# The concrete owner callable for the existing inline root project-catalog loop.
_ROOT_MATCH_CALLABLE = "select_project_match"


def _project_matching():
    """Import the owner module lazily so the Red failure is the missing boundary."""
    return importlib.import_module(_OWNER_MODULE)


def _project(identifier: str, kuerzel: str, **extra: object) -> dict[str, object]:
    return {
        "id": identifier,
        "kuerzel": kuerzel,
        "title": kuerzel,
        "mailbox_folder": f"Projects/{kuerzel}",
        **extra,
    }


def _meshe() -> dict[str, object]:
    return {
        "id": "meshe",
        "kuerzel": "MESHE",
        "title": "MESHE",
        "mailbox_folder": "Projects/MESHE",
        "schema_version": 3,
        "workpackages": [
            {
                "id": "wp1",
                "title": "Quality and Evaluation",
                "status": "active",
                "tasks": [{"id": "T1.7", "title": "Quality Plan"}],
                "deliverables": [{"id": "D1.2", "title": "Quality Handbook"}],
            }
        ],
        "milestones": [{"id": "MS5", "title": "Evaluation completed"}],
    }


def _match(
    projects: list[dict[str, object]],
    subject: str,
    *,
    from_str: str = "sender@other.test",
    to_str: str = "desk@other.test",
    cc_str: str = "",
    preview: str = "",
) -> dict[str, object] | None:
    """Call the canonical root-match owner with the classifier's derived inputs."""
    full_text = f"{subject}\n{from_str}\n{to_str}\n{cc_str}\n{preview}"
    return _project_matching().select_project_match(
        projects,
        subject=subject,
        full_text=full_text,
        full_text_lower=full_text.lower(),
        from_str=from_str,
        to_str=to_str,
        parties=f"{from_str} {to_str} {cc_str}".lower(),
    )


@contextmanager
def _spy_root_match_callable():
    """Wrap the canonical root-match owner while preserving its behaviour.

    The same spy is installed on the owner module and, when the facade holds a bound
    reference, on ``core.classifier`` as well, so routing is observed regardless of how
    the facade calls through to the owner.
    """
    owner = _project_matching()
    spy = Mock(wraps=getattr(owner, _ROOT_MATCH_CALLABLE))
    patchers = [patch.object(owner, _ROOT_MATCH_CALLABLE, spy)]
    if hasattr(classifier, _ROOT_MATCH_CALLABLE):
        patchers.append(patch.object(classifier, _ROOT_MATCH_CALLABLE, spy))
    for patcher in patchers:
        patcher.start()
    try:
        yield spy
    finally:
        for patcher in reversed(patchers):
            patcher.stop()


def _classify(subject: str, projects: list[dict[str, object]], **extra: object) -> dict:
    email = {
        "envelope_id": "42",
        "folder": "INBOX",
        "message_id": "<t02@example.test>",
        "subject": subject,
        "from": "Coordinator <coord@other.test>",
        "to": "Martin <martin@other.test>",
        "date": "2026-05-18",
        "preview": "",
        **extra,
    }
    return classifier.classify_email(
        email,
        projects=projects,
        topics=[],
        sent_lookup={},
        final_index={"items": {}},
    )


class ProjectMatchingOwnerModuleTests(unittest.TestCase):
    """The owner module exists and the facade re-exports its callables by identity."""

    def test_module_owns_the_project_callable_seams(self) -> None:
        module = _project_matching()
        for name in _OWNED_CALLABLES:
            with self.subTest(callable=name):
                self.assertIn(name, vars(module))
                value = getattr(module, name)
                self.assertTrue(callable(value))
                self.assertEqual(module.__name__, getattr(value, "__module__", None))

    def test_facade_reexports_owner_callables_by_identity(self) -> None:
        module = _project_matching()
        for name in _OWNED_CALLABLES:
            with self.subTest(callable=name):
                self.assertIs(getattr(classifier, name), getattr(module, name))


class ProjectRootMatchOwnerContractTests(unittest.TestCase):
    """The canonical root project-catalog matching owner keeps its decisions."""

    def test_explicit_subject_name_is_a_high_confidence_match(self) -> None:
        project = _project("meshe", "MESHE", aliases=["MCX"])
        result = _match([project], "MESHE weekly sync")

        self.assertIsNotNone(result)
        self.assertEqual("meshe", result["id"])
        self.assertEqual("Projects/MESHE", result["folder"])
        self.assertEqual("MESHE", result["name"])
        self.assertEqual("high", result["confidence"])
        self.assertEqual(project, result["catalog"])

    def test_typical_subject_pattern_is_a_high_confidence_match(self) -> None:
        project = _project("meshe", "MESHE", typical_subject_patterns=["Exchange Sync"])

        result = _match([project], "Exchange Sync call")

        self.assertIsNotNone(result)
        self.assertEqual("meshe", result["id"])
        self.assertEqual("high", result["confidence"])

    def test_known_contact_is_a_high_confidence_match(self) -> None:
        project = _project("meshe", "MESHE", contacts=[{"email": "coord@example.test"}])

        result = _match([project], "Just a note", from_str="Coord <coord@example.test>")

        self.assertIsNotNone(result)
        self.assertEqual("meshe", result["id"])
        self.assertEqual("high", result["confidence"])

    def test_keyword_with_generic_domain_is_a_medium_confidence_match(self) -> None:
        project = _project("gen", "GEN", keywords=["focus groups"], domains=["gmail.com"])

        result = _match(
            [project], "Just a note", from_str="x@gmail.com", preview="focus groups"
        )

        self.assertIsNotNone(result)
        self.assertEqual("gen", result["id"])
        self.assertEqual("medium", result["confidence"])

    def test_no_catalog_signal_returns_none(self) -> None:
        project = _project("clean", "CLEAN")

        self.assertIsNone(_match([project], "Totally unrelated"))

    def test_exact_subject_code_beats_earlier_catalog_contact_entry(self) -> None:
        first = _project("alpha", "ALPHA", contacts=[{"email": "coord@example.test"}])
        second = _project("orion", "ORION")

        result = _match(
            [first, second], "ORION update", from_str="coord@example.test"
        )

        self.assertIsNotNone(result)
        self.assertEqual("orion", result["id"])
        self.assertEqual("high", result["confidence"])

    def test_do_not_route_catalog_entry_is_skipped(self) -> None:
        project = _project("meshe", "MESHE", do_not_route_if=["newsletter"])

        self.assertIsNone(_match([project], "MESHE newsletter"))


class ProjectMatchingParityTests(unittest.TestCase):
    """Moved artifact/evidence primitives keep their observable behaviour."""

    def test_artifact_text_and_code_gates_stay_token_bounded(self) -> None:
        owner = _project_matching()
        self.assertTrue(owner._artifact_text_matches("The Quality Plan is ready", "Quality Plan"))
        self.assertFalse(owner._artifact_text_matches("preauditing", "audit"))
        self.assertTrue(owner._artifact_code_matches("WP2 done", "wp2"))
        self.assertFalse(owner._artifact_code_matches("XWP2Y", "wp2"))

    def test_artifact_candidate_builds_inspectable_reasons(self) -> None:
        owner = _project_matching()
        candidate = owner._artifact_candidate(
            "task", {"id": "T1.7", "title": "Quality Plan"}, "T1.7 Quality Plan"
        )
        self.assertEqual(
            {"id": "T1.7", "title": "Quality Plan", "reasons": ["exact_code:T1.7", "title:Quality Plan"]},
            candidate,
        )
        self.assertIsNone(owner._artifact_candidate("task", {"id": "", "title": "X"}, "x"))

    def test_select_project_artifacts_resolves_unique_codes(self) -> None:
        resolution = _project_matching()._select_project_artifacts(
            _meshe(), "MESHE WP1 T1.7 D1.2"
        )
        self.assertEqual("wp1", resolution["workpackage"])
        self.assertEqual("T1.7", resolution["task"])
        self.assertEqual("D1.2", resolution["deliverable"])
        self.assertEqual(["exact_code:wp1"], resolution["match_reasons"]["workpackage"])

    def test_catalog_title_and_context_label_are_catalog_backed(self) -> None:
        owner = _project_matching()
        project = _meshe()
        self.assertEqual("Quality Plan", owner._catalog_artifact_title(project, "task", "T1.7"))
        self.assertEqual(
            "Evaluation completed", owner._catalog_artifact_title(project, "milestone", "MS5")
        )
        self.assertEqual("", owner._catalog_artifact_title(project, "task", "ZZ"))
        self.assertEqual(
            "MESHE | WP1 (Quality and Evaluation)",
            owner._project_context_label(project, {"workpackage": "wp1"}),
        )

    def test_evidence_read_escalation_is_bounded_and_neutral(self) -> None:
        owner = _project_matching()
        self.assertEqual(
            {"level": "full_body", "status": "completed"},
            owner._evidence_read_escalation(
                {"read_escalation": {"level": "full_body", "status": "completed", "junk": 1}}
            ),
        )
        self.assertIsNone(owner._evidence_read_escalation({}))

    def test_project_evidence_file_and_entry_shape_are_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            spec = _project_matching()._build_project_evidence(
                _meshe(),
                {"workpackage": "wp1", "task": "T1.7", "deliverable": "D1.2"},
                workspace_root=Path(temporary),
                year_month="2026-05",
                date="2026-05-18",
                subject="MESHE WP1",
                message_id="t02@example.test",
                from_str="a@other.test",
                to_str="b@other.test",
            )

        self.assertEqual("memory/evidence/projects/meshe/2026-05.md", spec["file"])
        self.assertIn(
            "[MESHE | WP1 (Quality and Evaluation) / T1.7 (Quality Plan) / D1.2 (Quality Handbook)]",
            spec["entry"],
        )
        self.assertIn("Message-ID: `t02@example.test`", spec["entry"])
        self.assertIn("- 2026-05-18 — MESHE WP1.\n", spec["entry"])

        with tempfile.TemporaryDirectory() as temporary:
            dotted = _project_matching()._build_project_evidence(
                _meshe(),
                {"workpackage": "wp1", "task": "T1.7", "deliverable": "D1.2"},
                workspace_root=Path(temporary),
                year_month="2026-05",
                date="2026-05-18",
                subject="MESHE WP1.",
                message_id="t02@example.test",
                from_str="a@other.test",
                to_str="b@other.test",
            )

        self.assertIn("- 2026-05-18 — MESHE WP1.\n", dotted["entry"])
        self.assertNotIn("MESHE WP1..", dotted["entry"])

    def test_classification_target_and_artifact_ambiguity_are_unchanged(self) -> None:
        item = _classify("MESHE WP1 T1.7 D1.2", [_meshe()])
        self.assertEqual("project", item["decision"]["kind"])
        self.assertEqual("meshe", item["decision"]["id"])
        self.assertEqual("Projects/MESHE", item["action"]["target_folder"])

        ambiguous = _meshe()
        ambiguous["workpackages"] = [
            {"id": "wp1", "title": "A", "status": "active",
             "tasks": [{"id": "T1.8", "title": "Shared Workshop"}], "deliverables": []},
            {"id": "wp2", "title": "B", "status": "active",
             "tasks": [{"id": "T2.3", "title": "Shared Workshop"}], "deliverables": []},
        ]
        decision = _classify("MESHE Shared Workshop", [ambiguous])["decision"]
        self.assertNotIn("task", decision)
        self.assertEqual(
            ["T1.8", "T2.3"],
            [row["id"] for row in decision["artifact_candidates"]["task"]],
        )


class ProjectRootMatchRoutingTests(unittest.TestCase):
    """A representative classification routes its root project decision through the owner."""

    def test_root_project_decision_routes_through_owner_callable(self) -> None:
        project = _project("meshe", "MESHE")
        with _spy_root_match_callable() as spy:
            item = _classify("MESHE weekly sync", [project])

        self.assertTrue(spy.called, "root project matching must use the canonical owner")
        self.assertEqual("project", item["decision"]["kind"])
        self.assertEqual("meshe", item["decision"]["id"])


if __name__ == "__main__":
    unittest.main()
