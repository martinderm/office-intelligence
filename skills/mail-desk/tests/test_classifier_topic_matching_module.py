"""FR-13 / MD-M1-T03 structural Red tests for the topic-matching vertical slice.

These tests define the complete topic boundary for MD-M1-T03 before any production
exists (evidence mode ``tdd``, risk tier ``high``):

1. ``core.matching.topic_matching`` is importable and canonically owns the sixteen
   baseline topic/subtopic/operation/event/evidence/target callables named by the
   spec; ``core.classifier`` re-exports every one by object identity.
2. One concrete owner callable -- ``select_topic_match`` -- owns the inline ordered
   root topic-catalog loop together with the unique subtopic fallback/override policy,
   and ``classify_email`` routes its root topic decision through that owner.
3. One concrete owner callable -- ``materialize_topic_details`` -- owns the selected
   topic's subtopic/operation/event decision enrichment, domain evidence and safe
   synthesis targets, and ``classify_email`` routes its detail enrichment through it.
4. Representative catalog order, confidence, fallback ambiguity, subtopic/operation/
   event selection and ambiguity, event validation failure, evidence path/shape and
   safe target behaviour remain unchanged for the characterized fixtures.
5. The active fingerprint source set gains ``topic_matching.py`` after
   ``project_matching.py`` (asserted in ``test_classifier_matching_modules.py``); a
   semantic AST change in the topic module must move the digest.

Full-body, thread-inheritance, sent-index and final-index orchestration deliberately
stays in ``classifier.py`` and is not required to move.

Owner callable contracts::

    select_topic_match(
        topics, *, subject, full_text, full_text_lower, from_str, to_str, parties
    ) -> {"id", "folder", "title", "confidence", "catalog", "preselected_subtopic"} | None

where ``catalog`` is the exact winning topic-catalog object, so the facade can feed
``materialize_topic_details`` directly without a second inline topic-catalog lookup.

    materialize_topic_details(
        topic, decision, *, subject, full_text, preselected_subtopic, workspace_root,
        year_month, date, message_id, from_str, to_str
    ) -> {"decision", "evidence", "synthesis_targets"}
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

_OWNER_MODULE = "core.matching.topic_matching"

# Topic-assigned baseline callables that must have exactly one canonical owner in
# ``core.matching.topic_matching`` and stay object-identical through the facade.
_OWNED_CALLABLES = (
    "_topic_parent_subject_signal",
    "_subject_signal_matches",
    "_select_topic_subtopic",
    "_topic_context_label",
    "_select_subtopic_operation",
    "_operation_context_label",
    "_select_subtopic_event",
    "_event_validation_reasons",
    "_event_context_label",
    "_build_topic_evidence",
    "_build_operation_evidence",
    "_build_event_evidence",
    "_safe_subtopic_reference_target",
    "_safe_operation_reference_target",
    "_safe_event_dossier_target",
    "_has_canonical_operation_reference",
)

# The two concrete owner callables for the existing inline topic orchestration.
_ROOT_MATCH_CALLABLE = "select_topic_match"
_DETAILS_CALLABLE = "materialize_topic_details"

_OPERATIONS_EMAIL = "Coordinator <general@example.test>"
_OPERATIONS_TO = "Martin <martin@example.test>"


def _topic_matching():
    """Import the owner module lazily so the Red failure is the missing boundary."""
    return importlib.import_module(_OWNER_MODULE)


# --------------------------------------------------------------------------------------
# Catalog fixtures (representative, deterministic copies of the characterized corpus)
# --------------------------------------------------------------------------------------

def _root_topic(**changes: object) -> dict[str, object]:
    value: dict[str, object] = {
        "id": "alpha",
        "title": "Alpha",
        "mailbox_folder": "Themen/Alpha",
    }
    value.update(changes)
    return value


def _subtopic_entry(**changes: object) -> dict[str, object]:
    value: dict[str, object] = {
        "id": "course-design",
        "title": "Course Design",
        "aliases": ["Course Redesign"],
        "keywords": ["micro credential"],
        "typical_subject_patterns": ["[RPL-2026]"],
        "contacts": [{"email": "course@example.test"}],
        "status": "active",
    }
    value.update(changes)
    return value


def _sub_topic(**changes: object) -> dict[str, object]:
    value: dict[str, object] = {
        "id": "operations",
        "title": "Operations",
        "mailbox_folder": "Topics/Operations",
        "aliases": ["Operations"],
        "keywords": ["operational"],
        "subtopics": [_subtopic_entry()],
    }
    value.update(changes)
    return value


def _operation(**changes: object) -> dict[str, object]:
    value: dict[str, object] = {
        "id": "coordination",
        "title": "Coordination cycle",
        "aliases": ["Coordination Call"],
        "keywords": ["agenda"],
        "typical_subject_patterns": ["[OPS-CALL]"],
        "status": "active",
    }
    value.update(changes)
    return value


def _event(**changes: object) -> dict[str, object]:
    value: dict[str, object] = {
        "id": "spring-forum-2026",
        "title": "Spring Forum 2026",
        "starts_on": "2026-05-04",
        "ends_on": "2026-05-06",
        "aliases": ["Spring Forum"],
        "keywords": ["registration"],
        "typical_subject_patterns": ["[SPRING-FORUM]"],
        "reference_md": (
            "memory/references/topics/operations/subtopics/course-design/"
            "events/spring-forum-2026/index.md"
        ),
        "status": "active",
        "phase": "planned",
    }
    value.update(changes)
    return value


def _cagliari_topics() -> list[dict[str, object]]:
    """Focused fixture copied from the BOKU Dienstreisen/Netzwerke contract."""
    return [
        {
            "id": "dienstreisen",
            "title": "Dienstreisen",
            "mailbox_folder": "Administratives/Dienstreisen",
            "aliases": [],
            "keywords": ["Dienstreise", "Reise"],
            "typical_subject_patterns": ["A1-Formular zu DR-Auftrag"],
            "subtopics": [
                {
                    "id": "2026-06-cagliari-eucen-conference",
                    "title": "Cagliari 2026-06 — IACEE Symposium & EUCEN Conference",
                    "aliases": ["Cagliari 2026-06", "IACEE Symposium 2026"],
                    "keywords": ["Cagliari", "IACEE"],
                    "typical_subject_patterns": [
                        "Cagliari",
                        "IACEE Symposium 2026",
                        "Join Us in Cagliari",
                        "Response submission for Registration Form 56th EUCEN Annual Conference",
                    ],
                    "contacts": [
                        {"email": "spol@unica.it"},
                        {"email": "alice.sgualdini@unica.it"},
                    ],
                    "status": "active",
                }
            ],
        },
        {
            "id": "netzwerke",
            "title": "Netzwerke",
            "mailbox_folder": "Themen/Netzwerke",
            "aliases": ["Networks"],
            "keywords": ["EUCEN"],
            "typical_subject_patterns": ["We are EUCEN", "EUCEN"],
            "subtopics": [
                {
                    "id": "eucen",
                    "title": "EUCEN",
                    "aliases": ["European University Continuing Education Network", "We are EUCEN"],
                    "keywords": ["EUCEN", "eucen annual conference"],
                    "contacts": [],
                    "status": "active",
                }
            ],
        },
    ]


def _email(subject: str, **extra: object) -> dict[str, object]:
    return {
        "envelope_id": "topic-1",
        "folder": "INBOX",
        "message_id": "<topic-1@example.test>",
        "subject": subject,
        "from": _OPERATIONS_EMAIL,
        "to": _OPERATIONS_TO,
        "date": "2026-06-10",
        **extra,
    }


def _classify(
    subject: str, topics: list[dict[str, object]], *, workspace: Path | None = None, **extra: object
) -> dict:
    return classifier.classify_email(
        _email(subject, **extra),
        workspace_root=workspace,
        projects=[],
        topics=topics,
        sent_lookup={},
        final_index={"items": {}},
    )


# --------------------------------------------------------------------------------------
# Owner-direct invocation helpers
# --------------------------------------------------------------------------------------

def _select_match(
    topics: list[dict[str, object]],
    subject: str,
    *,
    from_str: str = "sender@other.test",
    to_str: str = "desk@other.test",
    cc_str: str = "",
    preview: str = "",
) -> dict[str, object] | None:
    """Call the canonical root-topic owner with the classifier's derived inputs."""
    full_text = f"{subject}\n{from_str}\n{to_str}\n{cc_str}\n{preview}"
    return _topic_matching().select_topic_match(
        topics,
        subject=subject,
        full_text=full_text,
        full_text_lower=full_text.lower(),
        from_str=from_str,
        to_str=to_str,
        parties=f"{from_str} {to_str} {cc_str}".lower(),
    )


def _materialize(
    topic: dict[str, object],
    subject: str,
    *,
    workspace: Path,
    preselected: dict[str, object] | None = None,
    preview: str = "",
    year_month: str = "2026-06",
    date: str = "2026-06-10",
    message_id: str = "topic-1@example.test",
) -> dict[str, object]:
    """Call the canonical detail-materialization owner for one selected topic."""
    full_text = f"{subject}\n{_OPERATIONS_EMAIL}\n{_OPERATIONS_TO}\n\n{preview}"
    decision: dict[str, object] = {
        "kind": "topic",
        "id": topic.get("id", ""),
        "confidence": "high",
        "needs_reply": False,
    }
    return _topic_matching().materialize_topic_details(
        topic,
        decision,
        subject=subject,
        full_text=full_text,
        preselected_subtopic=preselected,
        workspace_root=workspace,
        year_month=year_month,
        date=date,
        message_id=message_id,
        from_str=_OPERATIONS_EMAIL,
        to_str=_OPERATIONS_TO,
    )


@contextmanager
def _spy_owner_callable(name: str):
    """Wrap a canonical owner callable while preserving its behaviour.

    The same spy is installed on the owner module and, when the facade holds a bound
    reference, on ``core.classifier`` as well, so routing is observed regardless of how
    the facade calls through to the owner.
    """
    owner = _topic_matching()
    spy = Mock(wraps=getattr(owner, name))
    patchers = [patch.object(owner, name, spy)]
    if hasattr(classifier, name):
        patchers.append(patch.object(classifier, name, spy))
    for patcher in patchers:
        patcher.start()
    try:
        yield spy
    finally:
        for patcher in reversed(patchers):
            patcher.stop()


def _add_file(workspace: Path, relative: str) -> None:
    target = workspace / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("# fixture\n", encoding="utf-8")


class TopicMatchingOwnerModuleTests(unittest.TestCase):
    """The owner module exists and the facade re-exports its callables by identity."""

    def test_module_owns_the_topic_callable_seams(self) -> None:
        module = _topic_matching()
        for name in _OWNED_CALLABLES:
            with self.subTest(callable=name):
                self.assertIn(name, vars(module))
                value = getattr(module, name)
                self.assertTrue(callable(value))
                self.assertEqual(module.__name__, getattr(value, "__module__", None))

    def test_facade_reexports_owner_callables_by_identity(self) -> None:
        module = _topic_matching()
        for name in _OWNED_CALLABLES:
            with self.subTest(callable=name):
                self.assertIs(getattr(classifier, name), getattr(module, name))


class TopicRootMatchOwnerContractTests(unittest.TestCase):
    """The canonical ordered root topic-catalog owner keeps its decisions."""

    def test_explicit_subject_name_is_a_high_confidence_match(self) -> None:
        topic = _root_topic()
        result = _select_match([topic], "Alpha weekly sync")

        self.assertIsNotNone(result)
        self.assertEqual("alpha", result["id"])
        self.assertEqual("Themen/Alpha", result["folder"])
        self.assertEqual("Alpha", result["title"])
        self.assertEqual("high", result["confidence"])
        self.assertIsNone(result["preselected_subtopic"])
        # The exact input catalog object crosses the boundary, so no second
        # facade lookup is needed before detail materialization.
        self.assertIs(topic, result["catalog"])

    def test_typical_subject_pattern_is_a_high_confidence_match(self) -> None:
        result = _select_match(
            [_root_topic(typical_subject_patterns=["Exchange Sync"])], "Exchange Sync call"
        )

        self.assertIsNotNone(result)
        self.assertEqual("alpha", result["id"])
        self.assertEqual("high", result["confidence"])

    def test_subject_keyword_is_a_high_confidence_match(self) -> None:
        result = _select_match([_root_topic(keywords=["focus groups"])], "Focus groups update")

        self.assertIsNotNone(result)
        self.assertEqual("alpha", result["id"])
        self.assertEqual("high", result["confidence"])

    def test_body_keyword_with_domain_is_a_medium_confidence_match(self) -> None:
        result = _select_match(
            [_root_topic(keywords=["retention"], domains=["gmail.com"])],
            "Just a note",
            from_str="x@gmail.com",
            preview="retention planning",
        )

        self.assertIsNotNone(result)
        self.assertEqual("alpha", result["id"])
        self.assertEqual("medium", result["confidence"])

    def test_no_catalog_signal_returns_none(self) -> None:
        self.assertIsNone(_select_match([_root_topic()], "Totally unrelated"))

    def test_first_catalog_entry_wins_over_a_later_stronger_name(self) -> None:
        first = _root_topic(id="first", title="First", mailbox_folder="Themen/First",
                            keywords=["update"])
        second = _root_topic(id="second", title="Second", mailbox_folder="Themen/Second",
                             aliases=["Alpha"])

        result = _select_match([first, second], "Alpha update")

        self.assertIsNotNone(result)
        self.assertEqual("first", result["id"])
        self.assertEqual("high", result["confidence"])

    def test_subtopic_only_signal_selects_parent_via_unique_fallback(self) -> None:
        topic = _sub_topic()
        result = _select_match([topic], "Course Redesign timetable")

        self.assertIsNotNone(result)
        self.assertEqual("operations", result["id"])
        self.assertEqual("Topics/Operations", result["folder"])
        self.assertEqual("high", result["confidence"])
        # The unique-subtopic fallback returns the exact selected parent catalog
        # object, not a copy reconstructed from scalar fields.
        self.assertIs(topic, result["catalog"])
        self.assertIsNotNone(result["preselected_subtopic"])
        self.assertEqual("course-design", result["preselected_subtopic"]["subtopic"])
        self.assertIn(
            "subject_alias:Course Redesign", result["preselected_subtopic"]["match_reasons"]
        )

    def test_unique_subtopic_pattern_overrides_a_weaker_generic_root_keyword(self) -> None:
        result = _select_match(
            _cagliari_topics(),
            "Response submission for Registration Form 56th EUCEN Annual Conference",
        )

        self.assertIsNotNone(result)
        self.assertEqual("dienstreisen", result["id"])
        self.assertEqual("Administratives/Dienstreisen", result["folder"])
        self.assertEqual(
            "2026-06-cagliari-eucen-conference", result["preselected_subtopic"]["subtopic"]
        )

    def test_generic_root_match_keeps_its_own_subtopic_without_override(self) -> None:
        result = _select_match(_cagliari_topics(), "EUCEN Annual Conference")

        self.assertIsNotNone(result)
        self.assertEqual("netzwerke", result["id"])
        self.assertEqual("Themen/Netzwerke", result["folder"])
        self.assertEqual("eucen", result["preselected_subtopic"]["subtopic"])

    def test_ambiguous_subtopic_fallback_fails_closed(self) -> None:
        other = {
            "id": "other",
            "title": "Other",
            "mailbox_folder": "Topics/Other",
            "subtopics": [
                {"id": "other-sub", "title": "Other sub", "aliases": ["Shared signal"],
                 "status": "active"}
            ],
        }
        shared = _sub_topic(
            subtopics=[
                {"id": "ops-sub", "title": "Ops sub", "aliases": ["Shared signal"],
                 "status": "active"}
            ]
        )

        self.assertIsNone(_select_match([shared, other], "Shared signal"))


class TopicDetailMaterializationOwnerContractTests(unittest.TestCase):
    """The canonical detail owner enriches one selected topic and keeps its outputs."""

    def test_subtopic_materialization_sets_decision_evidence_and_safe_target(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            reference = "memory/references/topics/operations/subtopics/course-design.md"
            _add_file(workspace, reference)
            topic = _sub_topic(subtopics=[_subtopic_entry(reference_md=reference)])

            result = _materialize(topic, "Operations — Course Redesign", workspace=workspace)

        decision = result["decision"]
        self.assertEqual("course-design", decision["subtopic"])
        self.assertEqual(["subject_alias:Course Redesign"], decision["subtopic_match_reasons"])
        self.assertEqual("topic_evidence", result["evidence"]["type"])
        self.assertEqual("memory/evidence/topics/operations/2026-06.md", result["evidence"]["file"])
        self.assertIn("[OPERATIONS | course-design (Course Design)]", result["evidence"]["entry"])
        self.assertIn("Message-ID: `topic-1@example.test`", result["evidence"]["entry"])
        self.assertEqual(
            [{"file": reference, "type": "subtopic_reference"}], result["synthesis_targets"]
        )

        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            reference = "memory/references/topics/operations/subtopics/course-design.md"
            _add_file(workspace, reference)
            topic = _sub_topic(subtopics=[_subtopic_entry(reference_md=reference)])
            dotted = _materialize(topic, "Operations — Course Redesign.", workspace=workspace)

        self.assertIn(
            "- 2026-06-10 — Operations — Course Redesign.\n", dotted["evidence"]["entry"]
        )
        self.assertNotIn("Course Redesign..", dotted["evidence"]["entry"])

    def test_operation_materialization_sets_scoped_evidence_and_index_target(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            reference = (
                "memory/references/topics/operations/subtopics/course-design/"
                "operations/coordination/index.md"
            )
            _add_file(workspace, reference)
            topic = _sub_topic(
                subtopics=[_subtopic_entry(operations=[_operation(reference_md=reference)])]
            )

            result = _materialize(
                topic, "Operations — Course Redesign [OPS-CALL]", workspace=workspace
            )

        decision = result["decision"]
        self.assertEqual("course-design", decision["subtopic"])
        self.assertEqual("coordination", decision["operation"])
        self.assertIn("subject_pattern:[OPS-CALL]", decision["operation_match_reasons"])
        self.assertEqual("operation_evidence", result["evidence"]["type"])
        self.assertEqual(
            "memory/evidence/topics/operations/subtopics/course-design/operations/"
            "coordination/2026-06.md",
            result["evidence"]["file"],
        )
        self.assertEqual(
            [{"file": reference, "type": "operation_reference"}], result["synthesis_targets"]
        )

    def test_noncanonical_operation_reference_stays_a_candidate_without_target(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            raw_operation = _operation(
                reference_md="memory/references/topics/operations/unsafe.md"
            )
            raw_operation.pop("status")
            topic = _sub_topic(subtopics=[_subtopic_entry(operations=[raw_operation])])

            result = _materialize(
                topic, "Operations — Course Redesign [OPS-CALL]", workspace=workspace
            )

        decision = result["decision"]
        self.assertNotIn("operation", decision)
        self.assertEqual(
            "noncanonical_reference_md",
            decision["operation_candidates"][0]["reasons"][-1],
        )
        self.assertEqual("topic_evidence", result["evidence"]["type"])
        self.assertEqual([], result["synthesis_targets"])

    def test_event_materialization_sets_event_evidence_and_dossier_target(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            event = _event()
            _add_file(workspace, str(event["reference_md"]))
            topic = _sub_topic(subtopics=[_subtopic_entry(events=[event], operations=[])])

            result = _materialize(
                topic, "Operations — Course Redesign [SPRING-FORUM]", workspace=workspace
            )

        decision = result["decision"]
        self.assertEqual("spring-forum-2026", decision["event"])
        self.assertIn("subject_pattern:[SPRING-FORUM]", decision["event_match_reasons"])
        self.assertEqual("event_evidence", result["evidence"]["type"])
        self.assertEqual(
            "memory/evidence/topics/operations/events/spring-forum-2026/2026-06.md",
            result["evidence"]["file"],
        )
        self.assertEqual(
            [{"file": event["reference_md"], "type": "event_dossier"}],
            result["synthesis_targets"],
        )

    def test_event_validation_failure_stays_a_candidate_without_target(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            invalid = _event(starts_on="04-05-2026")
            _add_file(workspace, str(invalid["reference_md"]))
            topic = _sub_topic(subtopics=[_subtopic_entry(events=[invalid], operations=[])])

            result = _materialize(
                topic, "Operations — Course Redesign [SPRING-FORUM]", workspace=workspace
            )

        decision = result["decision"]
        self.assertNotIn("event", decision)
        self.assertIn("invalid_starts_on", decision["event_candidates"][0]["reasons"])
        self.assertEqual("topic_evidence", result["evidence"]["type"])
        self.assertEqual([], result["synthesis_targets"])

    def test_cross_kind_conflict_reverts_both_scalars_to_candidates(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            event = _event(typical_subject_patterns=["[SHARED]"])
            _add_file(workspace, str(event["reference_md"]))
            operation = _operation(typical_subject_patterns=["[SHARED]"])
            topic = _sub_topic(
                subtopics=[_subtopic_entry(events=[event], operations=[operation])]
            )

            result = _materialize(
                topic, "Operations — Course Redesign [SHARED]", workspace=workspace
            )

        decision = result["decision"]
        self.assertNotIn("operation", decision)
        self.assertNotIn("event", decision)
        self.assertTrue(all(
            "cross_kind_conflict" in row["reasons"]
            for row in decision["operation_candidates"]
        ))
        self.assertTrue(all(
            "cross_kind_conflict" in row["reasons"] for row in decision["event_candidates"]
        ))
        self.assertEqual("topic_evidence", result["evidence"]["type"])
        self.assertEqual([], result["synthesis_targets"])

    def test_ambiguous_subtopic_sets_candidates_only(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            topic = _sub_topic(
                subtopics=[
                    {"id": "one", "title": "One", "aliases": ["Shared signal"],
                     "status": "active"},
                    {"id": "two", "title": "Two", "aliases": ["Shared signal"],
                     "status": "active"},
                ]
            )

            result = _materialize(topic, "Operations — Shared signal", workspace=workspace)

        decision = result["decision"]
        self.assertNotIn("subtopic", decision)
        self.assertEqual(
            ["one", "two"], [row["id"] for row in decision["subtopic_candidates"]]
        )
        self.assertIsNone(result["evidence"])
        self.assertEqual([], result["synthesis_targets"])

    def test_no_current_subtopic_leaves_the_decision_unenriched(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            result = _materialize(_sub_topic(subtopics=[]), "Operations notice",
                                  workspace=workspace)

        self.assertNotIn("subtopic", result["decision"])
        self.assertIsNone(result["evidence"])
        self.assertEqual([], result["synthesis_targets"])


class TopicMatchingRoutingTests(unittest.TestCase):
    """A representative classification routes topic orchestration through the owners."""

    def test_root_topic_decision_routes_through_owner_callable(self) -> None:
        with _spy_owner_callable(_ROOT_MATCH_CALLABLE) as spy:
            item = _classify("Alpha weekly sync", [_root_topic()])

        self.assertTrue(spy.called, "root topic matching must use the canonical owner")
        self.assertEqual("topic", item["decision"]["kind"])
        self.assertEqual("alpha", item["decision"]["id"])

    def test_subtopic_detail_enrichment_routes_through_owner_callable(self) -> None:
        with _spy_owner_callable(_DETAILS_CALLABLE) as spy:
            item = _classify("Operations — Course Redesign", [_sub_topic()])

        self.assertTrue(spy.called, "topic detail enrichment must use the canonical owner")
        self.assertEqual("topic", item["decision"]["kind"])
        self.assertEqual("operations", item["decision"]["id"])
        self.assertEqual("course-design", item["decision"]["subtopic"])


class TopicBehaviorParityTests(unittest.TestCase):
    """Root, subtopic, operation, event and target behaviour stays unchanged.

    These characterizations exercise ``classify_email`` directly and therefore stay
    green while the owner boundary is still missing; they guard the extraction from
    changing any decision, evidence or target.
    """

    def test_root_confidences_and_catalog_order_are_unchanged(self) -> None:
        name = _classify("Alpha weekly sync", [_root_topic()])
        pattern = _classify("Exchange Sync call",
                            [_root_topic(typical_subject_patterns=["Exchange Sync"])])
        keyword = _classify("Focus groups update", [_root_topic(keywords=["focus groups"])])
        context = _classify(
            "Just a note",
            [_root_topic(keywords=["retention"], domains=["gmail.com"])],
            **{"from": "x@gmail.com"},
            preview="retention planning",
        )
        none = _classify("Totally unrelated", [_root_topic()])

        for item in (name, pattern, keyword):
            self.assertEqual("topic", item["decision"]["kind"])
            self.assertEqual("high", item["decision"]["confidence"])
        self.assertEqual("medium", context["decision"]["confidence"])
        self.assertEqual("unknown", none["decision"]["kind"])
        self.assertEqual("INBOX", none["action"]["target_folder"])

        first = _root_topic(id="first", title="First", mailbox_folder="Themen/First",
                            keywords=["update"])
        second = _root_topic(id="second", title="Second", mailbox_folder="Themen/Second",
                             aliases=["Alpha"])
        ordered = _classify("Alpha update", [first, second])
        self.assertEqual("first", ordered["decision"]["id"])
        self.assertEqual("Themen/First", ordered["action"]["target_folder"])

    def test_unique_fallback_and_ambiguous_fallback_are_unchanged(self) -> None:
        fallback = _classify("Course Redesign timetable", [_sub_topic()])
        self.assertEqual("operations", fallback["decision"]["id"])
        self.assertEqual("course-design", fallback["decision"]["subtopic"])
        self.assertEqual("Topics/Operations", fallback["action"]["target_folder"])

        other = {
            "id": "other", "title": "Other", "mailbox_folder": "Topics/Other",
            "subtopics": [{"id": "other-sub", "title": "Other sub",
                           "aliases": ["Shared signal"], "status": "active"}],
        }
        shared = _sub_topic(subtopics=[{"id": "ops-sub", "title": "Ops sub",
                                        "aliases": ["Shared signal"], "status": "active"}])
        ambiguous = _classify("Shared signal", [shared, other])
        self.assertEqual("unknown", ambiguous["decision"]["kind"])
        self.assertEqual("INBOX", ambiguous["action"]["target_folder"])

    def test_subtopic_selection_and_ambiguity_are_unchanged(self) -> None:
        pattern = _classify("[RPL-2026] deadline", [_sub_topic()])
        self.assertEqual("course-design", pattern["decision"]["subtopic"])
        self.assertIn("subject_pattern:[RPL-2026]", pattern["decision"]["subtopic_match_reasons"])

        ambiguous = _classify(
            "Operations — Shared signal",
            [_sub_topic(subtopics=[
                {"id": "one", "title": "One", "aliases": ["Shared signal"], "status": "active"},
                {"id": "two", "title": "Two", "aliases": ["Shared signal"], "status": "active"},
            ])],
        )
        self.assertNotIn("subtopic", ambiguous["decision"])
        self.assertEqual(
            ["one", "two"],
            [row["id"] for row in ambiguous["decision"]["subtopic_candidates"]],
        )

    def test_operation_selection_and_ambiguity_are_unchanged(self) -> None:
        selected = _classify(
            "Operations — Course Redesign [OPS-CALL]",
            [_sub_topic(subtopics=[_subtopic_entry(operations=[_operation()])])],
        )
        self.assertEqual("coordination", selected["decision"]["operation"])
        self.assertEqual("operation_evidence", selected["evidence"]["type"])

        ambiguous = _classify(
            "Operations — Course Redesign — Shared",
            [_sub_topic(subtopics=[_subtopic_entry(operations=[
                _operation(id="one", title="One", aliases=["Shared"]),
                _operation(id="two", title="Two", aliases=["Shared"]),
            ])])],
        )
        self.assertNotIn("operation", ambiguous["decision"])
        self.assertEqual(
            ["one", "two"],
            [row["id"] for row in ambiguous["decision"]["operation_candidates"]],
        )

    def test_event_selection_and_validation_failure_are_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            valid = _event()
            _add_file(workspace, str(valid["reference_md"]))
            selected = _classify(
                "Operations — Course Redesign [SPRING-FORUM]",
                [_sub_topic(subtopics=[_subtopic_entry(events=[valid], operations=[])])],
                workspace=workspace,
            )

        self.assertEqual("spring-forum-2026", selected["decision"]["event"])
        self.assertEqual("event_evidence", selected["evidence"]["type"])
        self.assertEqual(
            "memory/evidence/topics/operations/events/spring-forum-2026/2026-06.md",
            selected["evidence"]["file"],
        )
        self.assertEqual(
            [{"file": valid["reference_md"], "type": "event_dossier"}],
            selected["synthesis_targets"],
        )

        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            invalid = _event(starts_on="04-05-2026")
            _add_file(workspace, str(invalid["reference_md"]))
            rejected = _classify(
                "Operations — Course Redesign [SPRING-FORUM]",
                [_sub_topic(subtopics=[_subtopic_entry(events=[invalid], operations=[])])],
                workspace=workspace,
            )

        self.assertNotIn("event", rejected["decision"])
        self.assertIn(
            "invalid_starts_on", rejected["decision"]["event_candidates"][0]["reasons"]
        )

    def test_safe_targets_require_the_canonical_catalog_file(self) -> None:
        reference = "memory/references/topics/operations/subtopics/course-design.md"
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            topic = _sub_topic(subtopics=[_subtopic_entry(reference_md=reference)])
            missing = _classify("Operations — Course Redesign", [topic], workspace=workspace)
            _add_file(workspace, reference)
            present = _classify("Operations — Course Redesign", [topic], workspace=workspace)

        self.assertEqual([], missing["synthesis_targets"])
        self.assertEqual(
            [{"file": reference, "type": "subtopic_reference"}], present["synthesis_targets"]
        )

    def test_cross_kind_conflict_remains_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            event = _event(typical_subject_patterns=["[SHARED]"])
            _add_file(workspace, str(event["reference_md"]))
            operation = _operation(typical_subject_patterns=["[SHARED]"])
            item = _classify(
                "Operations — Course Redesign [SHARED]",
                [_sub_topic(subtopics=[_subtopic_entry(events=[event], operations=[operation])])],
                workspace=workspace,
            )

        self.assertNotIn("operation", item["decision"])
        self.assertNotIn("event", item["decision"])
        self.assertTrue(item["decision"]["operation_candidates"])
        self.assertTrue(item["decision"]["event_candidates"])
        self.assertEqual("topic_evidence", item["evidence"]["type"])
        self.assertEqual([], item["synthesis_targets"])


if __name__ == "__main__":
    unittest.main()
