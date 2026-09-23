"""FR-21/MD-S5 workspace catalog validator contracts (tests-only Red phase).

MD-S5 adds one read-only validator for the three consuming-workspace catalogs:
``topics.json``, ``projects.json`` and the optional desk-signals
``mail-desk.json``.  At HEAD ``skills/mail-desk/scripts/catalog_validator.py``
does not exist, so this module is a genuine Red: the module-level import below
raises ``ModuleNotFoundError`` (an ``ImportError``).

The contract pinned by this suite (the implementation must satisfy it):

* ``catalog_validator.validate_workspace_catalogs(workspace_root)`` returns a
  structured report ``{"valid": bool, "errors": [...]}`` where every error is a
  mapping exposing at least ``{"catalog", "path", "reason"}`` -- the catalog
  file, the JSON path inside it, and a human-readable reason.  ``errors`` is
  deterministically sorted by ``(catalog, path)`` so two runs are byte-stable.
* A missing ``topics.json`` or ``projects.json`` is a loud failure; a missing
  ``mail-desk.json`` is valid because ``load_reply_heuristics`` falls back to the
  documented compatibility defaults.  All three present and valid = pass.
* ``topics.json`` accepts a bare list or an object with a ``topics`` list
  (mirroring ``core/classifier.load_catalogs``); every topic needs non-empty
  ``id``/``title``/``mailbox_folder``.  ``typical_subject_patterns`` entries must
  be strings: **root-level** patterns are consumed by ``select_topic_match`` as
  plain substrings (``topic_matching.py``), so any non-empty string is valid;
  **nested** patterns (``subtopics[]``/``operations[]``/``events[]``) flow through
  the lookaround ``_subject_signal_matches`` gate and must be at least three
  characters after stripping.
* ``projects.json`` accepts a bare list or an object with a ``projects`` list;
  every project needs ``id``/``title``/``mailbox_folder``/``workpackages``/
  ``milestones`` and a strict ``schema_version`` of exactly ``3`` (a bool, float
  or string ``3`` is drift); every workpackage needs ``id``/``title``/``status``/
  ``tasks``/``deliverables``.
* ``mail-desk.json`` mirrors ``load_reply_heuristics`` exactly: strict integer
  ``schema_version`` 1 (bool/float/string rejected), a ``reply_heuristics``
  object, a non-empty string list ``reply_triggers``, an optional string list
  ``no_reply_sender_tokens`` and an optional string-or-null ``owner_address``.
* The CLI ``python -B skills/mail-desk/scripts/catalog_validator.py --workspace
  <dir> --json`` exits ``0`` (valid), ``1`` (drift) or ``2`` (input/runtime) and
  prints one canonical JSON envelope on stdout.

Every fixture is a hermetic ``tempfile`` workspace; no mailbox, network or real
catalog is touched.
"""

from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


MAIL_DESK_ROOT = Path(__file__).resolve().parents[1]
CLI_PATH = MAIL_DESK_ROOT / "scripts" / "catalog_validator.py"

sys.path.insert(0, str(MAIL_DESK_ROOT / "scripts"))

from catalog_validator import validate_workspace_catalogs  # noqa: E402


#: Canonical workspace-relative catalog paths (portable, forward slashes).
TOPICS_RELATIVE_PATH = "memory/references/topics/topics.json"
PROJECTS_RELATIVE_PATH = "memory/references/projects/projects.json"
MAIL_DESK_RELATIVE_PATH = "memory/references/mail-desk/mail-desk.json"

#: Canonical mail-desk envelope keys, in builder order (core/envelope.py).
CANONICAL_KEYS = ("action", "success", "state", "message", "data", "error")
CLI_ACTION = "catalog_validator"


class _Workspace:
    """Hermetic temp workspace that never touches real catalogs or mailboxes."""

    def __init__(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()

    def __enter__(self) -> Path:
        return Path(self._tmp.name)

    def __exit__(self, *exc: object) -> None:
        self._tmp.cleanup()


def write_catalog(workspace_root: Path, relative_path: str, payload: object) -> Path:
    """Write a catalog; a ``str`` payload is written verbatim for malformed input."""
    path = workspace_root / relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(payload, str):
        path.write_text(payload, encoding="utf-8")
    else:
        path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def valid_topic(**overrides: object) -> dict:
    topic = {"id": "topic-a", "title": "Topic A", "mailbox_folder": "Themen/Topic A"}
    topic.update(overrides)
    return topic


def valid_subtopic(**overrides: object) -> dict:
    subtopic = {"id": "subtopic-a", "title": "Subtopic A", "status": "active"}
    subtopic.update(overrides)
    return subtopic


def valid_workpackage(**overrides: object) -> dict:
    workpackage = {
        "id": "wp1",
        "title": "WP1",
        "status": "active",
        "tasks": [],
        "deliverables": [],
    }
    workpackage.update(overrides)
    return workpackage


def valid_project(**overrides: object) -> dict:
    project = {
        "id": "project-a",
        "title": "Project A",
        "mailbox_folder": "Projects/Project A",
        "workpackages": [valid_workpackage()],
        "milestones": [],
        "schema_version": 3,
    }
    project.update(overrides)
    return project


def valid_mail_desk(**overrides: object) -> dict:
    payload = {
        "schema_version": 1,
        "reply_heuristics": {"reply_triggers": ["hallo klaus"]},
    }
    payload.update(overrides)
    return payload


def seed_valid_workspace(workspace_root: Path, *, with_mail_desk: bool = True) -> None:
    """Write the valid topics/projects catalogs, plus the optional mail-desk catalog."""
    write_catalog(workspace_root, TOPICS_RELATIVE_PATH, {"topics": [valid_topic()]})
    write_catalog(workspace_root, PROJECTS_RELATIVE_PATH, {"projects": [valid_project()]})
    if with_mail_desk:
        write_catalog(workspace_root, MAIL_DESK_RELATIVE_PATH, valid_mail_desk())


def _normalized_catalog(value: object) -> str:
    return str(value).replace("\\", "/")


def _catalog_matches(value: object, suffix: str) -> bool:
    return _normalized_catalog(value).endswith(suffix)


class _ValidatorTestCase(unittest.TestCase):
    """Shared read-only assertions; declares no test methods of its own."""

    def assert_valid(self, report: dict) -> None:
        self.assertTrue(
            report.get("valid"),
            f"expected a valid report; got errors: {report.get('errors')!r}",
        )
        self.assertEqual([], report.get("errors"), "a valid report must carry no errors")

    def assert_drift(
        self, report: dict, catalog_suffix: str, field: str | None = None
    ) -> list[dict]:
        self.assertFalse(
            report.get("valid"),
            f"expected drift for {catalog_suffix}; report was valid",
        )
        errors = report.get("errors") or []
        self.assertTrue(errors, f"a drifted report must list errors for {catalog_suffix}")
        matching = [e for e in errors if _catalog_matches(e.get("catalog"), catalog_suffix)]
        self.assertTrue(
            matching,
            f"expected an error naming catalog {catalog_suffix!r}; got {errors!r}",
        )
        for error in matching:
            self.assertTrue(
                {"catalog", "path", "reason"} <= set(error),
                f"every drift must expose catalog/path/reason; got {error!r}",
            )
        if field is not None:
            self.assertTrue(
                any(
                    field in f"{e.get('path', '')} {e.get('reason', '')}"
                    for e in matching
                ),
                f"expected the drift for {catalog_suffix!r} to name field {field!r}; got {matching!r}",
            )
        return matching


class TopicsCatalogValidationTests(_ValidatorTestCase):
    """``topics.json`` container, required-field and subject-pattern contracts."""

    def test_valid_list_payload_passes(self) -> None:
        with _Workspace() as ws:
            seed_valid_workspace(ws)
            write_catalog(ws, TOPICS_RELATIVE_PATH, [valid_topic()])
            report = validate_workspace_catalogs(ws)
        self.assert_valid(report)

    def test_valid_topics_object_payload_passes(self) -> None:
        with _Workspace() as ws:
            seed_valid_workspace(ws)
            write_catalog(ws, TOPICS_RELATIVE_PATH, {"topics": [valid_topic()]})
            report = validate_workspace_catalogs(ws)
        self.assert_valid(report)

    def test_payload_that_is_neither_list_nor_topics_object_is_rejected(self) -> None:
        payloads = {
            "integer payload": "42",
            "string payload": '"just-a-string"',
            "boolean payload": "true",
            "object without topics key": {"foo": 1},
            "object with non-list topics": {"topics": "not-a-list"},
        }
        for label, payload in payloads.items():
            with self.subTest(case=label), _Workspace() as ws:
                seed_valid_workspace(ws)
                write_catalog(ws, TOPICS_RELATIVE_PATH, payload)
                report = validate_workspace_catalogs(ws)
                self.assert_drift(report, "topics.json")

    def test_topic_missing_required_field_is_rejected(self) -> None:
        for field in ("id", "title", "mailbox_folder"):
            with self.subTest(field=field), _Workspace() as ws:
                topic = valid_topic()
                del topic[field]
                seed_valid_workspace(ws)
                write_catalog(ws, TOPICS_RELATIVE_PATH, {"topics": [topic]})
                report = validate_workspace_catalogs(ws)
                self.assert_drift(report, "topics.json", field)

    def test_topic_with_empty_or_whitespace_identity_is_rejected(self) -> None:
        payloads = {
            "empty id": {"id": "", "title": "Topic", "mailbox_folder": "Themen/T"},
            "whitespace id": {"id": "   ", "title": "Topic", "mailbox_folder": "Themen/T"},
            "empty title": {"id": "topic", "title": "", "mailbox_folder": "Themen/T"},
            "whitespace title": {"id": "topic", "title": "  ", "mailbox_folder": "Themen/T"},
        }
        for label, topic in payloads.items():
            with self.subTest(case=label), _Workspace() as ws:
                seed_valid_workspace(ws)
                write_catalog(ws, TOPICS_RELATIVE_PATH, {"topics": [topic]})
                report = validate_workspace_catalogs(ws)
                self.assert_drift(report, "topics.json")

    def test_subject_pattern_shorter_than_three_characters_is_rejected(self) -> None:
        # The min-3 gate belongs to the nested lookaround layer
        # (``_subject_signal_matches``): a short *subtopic* pattern is drift, while
        # a short *root* pattern is legal plain-substring content (see the
        # dedicated root-level tests below).
        for pattern in ("ab", "a", "  a  ", ""):
            with self.subTest(pattern=pattern), _Workspace() as ws:
                seed_valid_workspace(ws)
                topic = valid_topic(
                    subtopics=[valid_subtopic(typical_subject_patterns=[pattern])]
                )
                write_catalog(ws, TOPICS_RELATIVE_PATH, {"topics": [topic]})
                report = validate_workspace_catalogs(ws)
                self.assert_drift(report, "topics.json", "typical_subject_patterns")

    def test_short_root_subject_pattern_is_accepted(self) -> None:
        # Root-level patterns are consumed by ``select_topic_match`` as plain
        # substrings with only a truthiness gate, so a 2-character signal such as
        # the live boku-user ``QC`` pattern must not be rejected.
        with _Workspace() as ws:
            seed_valid_workspace(ws)
            topic = valid_topic(typical_subject_patterns=["QC", "  a  "])
            write_catalog(ws, TOPICS_RELATIVE_PATH, {"topics": [topic]})
            report = validate_workspace_catalogs(ws)
        self.assert_valid(report)

    def test_empty_root_subject_pattern_is_still_rejected(self) -> None:
        for pattern in ("", "   "):
            with self.subTest(pattern=pattern), _Workspace() as ws:
                seed_valid_workspace(ws)
                topic = valid_topic(typical_subject_patterns=[pattern])
                write_catalog(ws, TOPICS_RELATIVE_PATH, {"topics": [topic]})
                report = validate_workspace_catalogs(ws)
                self.assert_drift(report, "topics.json", "typical_subject_patterns")

    def test_short_nested_subject_pattern_is_rejected_at_every_level(self) -> None:
        # Nested patterns are gated at min 3 on all three nested levels: the
        # subtopic itself, its operations[] and its events[].
        payloads = {
            "subtopic": valid_subtopic(typical_subject_patterns=["ab"]),
            "operation": valid_subtopic(
                operations=[{"id": "op", "title": "Op", "typical_subject_patterns": ["ab"]}]
            ),
            "event": valid_subtopic(
                events=[{"id": "evt", "title": "Evt", "typical_subject_patterns": ["ab"]}]
            ),
        }
        for label, subtopic in payloads.items():
            with self.subTest(level=label), _Workspace() as ws:
                seed_valid_workspace(ws)
                topic = valid_topic(subtopics=[subtopic])
                write_catalog(ws, TOPICS_RELATIVE_PATH, {"topics": [topic]})
                report = validate_workspace_catalogs(ws)
                matching = self.assert_drift(
                    report, "topics.json", "typical_subject_patterns"
                )
                self.assertTrue(
                    any("nested" in str(error.get("reason", "")) for error in matching),
                    f"a nested pattern drift must name its level; got {matching!r}",
                )

    def test_subject_pattern_entry_that_is_not_a_string_is_rejected(self) -> None:
        for pattern in (123, True, None):
            with self.subTest(pattern=pattern), _Workspace() as ws:
                seed_valid_workspace(ws)
                topic = valid_topic(typical_subject_patterns=[pattern])
                write_catalog(ws, TOPICS_RELATIVE_PATH, {"topics": [topic]})
                report = validate_workspace_catalogs(ws)
                self.assert_drift(report, "topics.json", "typical_subject_patterns")

    def test_subject_pattern_container_must_be_a_list(self) -> None:
        with _Workspace() as ws:
            seed_valid_workspace(ws)
            topic = valid_topic(typical_subject_patterns="[EUEX]")
            write_catalog(ws, TOPICS_RELATIVE_PATH, {"topics": [topic]})
            report = validate_workspace_catalogs(ws)
        self.assert_drift(report, "topics.json", "typical_subject_patterns")

    def test_valid_subject_patterns_are_accepted(self) -> None:
        with _Workspace() as ws:
            seed_valid_workspace(ws)
            topic = valid_topic(typical_subject_patterns=["[EUEX]", "abc", "  xyz  "])
            write_catalog(ws, TOPICS_RELATIVE_PATH, {"topics": [topic]})
            report = validate_workspace_catalogs(ws)
        self.assert_valid(report)


class ProjectsCatalogValidationTests(_ValidatorTestCase):
    """``projects.json`` container, schema-version and entry contracts."""

    def test_valid_list_payload_passes(self) -> None:
        with _Workspace() as ws:
            seed_valid_workspace(ws)
            write_catalog(ws, PROJECTS_RELATIVE_PATH, [valid_project()])
            report = validate_workspace_catalogs(ws)
        self.assert_valid(report)

    def test_valid_projects_object_payload_passes(self) -> None:
        with _Workspace() as ws:
            seed_valid_workspace(ws)
            write_catalog(ws, PROJECTS_RELATIVE_PATH, {"projects": [valid_project()]})
            report = validate_workspace_catalogs(ws)
        self.assert_valid(report)

    def test_payload_that_is_neither_list_nor_projects_object_is_rejected(self) -> None:
        payloads = {
            "integer payload": "42",
            "string payload": '"just-a-string"',
            "object without projects key": {"foo": 1},
            "object with non-list projects": {"projects": "not-a-list"},
        }
        for label, payload in payloads.items():
            with self.subTest(case=label), _Workspace() as ws:
                seed_valid_workspace(ws)
                write_catalog(ws, PROJECTS_RELATIVE_PATH, payload)
                report = validate_workspace_catalogs(ws)
                self.assert_drift(report, "projects.json")

    def test_schema_version_drift_is_rejected(self) -> None:
        # Strict gate: ``True == 1`` / ``1.0 == 1`` would pass a loose ``==``, and
        # a string ``"3"`` must never masquerade as schema 3.
        payloads = {
            "missing schema_version": valid_project(schema_version=None),
            "boolean schema_version": valid_project(schema_version=True),
            "float schema_version": valid_project(schema_version=3.0),
            "string schema_version": valid_project(schema_version="3"),
            "wrong schema_version": valid_project(schema_version=2),
        }
        for label, project in payloads.items():
            with self.subTest(case=label), _Workspace() as ws:
                if label == "missing schema_version":
                    del project["schema_version"]
                seed_valid_workspace(ws)
                write_catalog(ws, PROJECTS_RELATIVE_PATH, {"projects": [project]})
                report = validate_workspace_catalogs(ws)
                self.assert_drift(report, "projects.json", "schema_version")

    def test_project_missing_required_field_is_rejected(self) -> None:
        for field in ("id", "title", "mailbox_folder", "workpackages", "milestones"):
            with self.subTest(field=field), _Workspace() as ws:
                project = valid_project()
                del project[field]
                seed_valid_workspace(ws)
                write_catalog(ws, PROJECTS_RELATIVE_PATH, {"projects": [project]})
                report = validate_workspace_catalogs(ws)
                self.assert_drift(report, "projects.json", field)

    def test_workpackage_missing_required_field_is_rejected(self) -> None:
        for field in ("id", "title", "status", "tasks", "deliverables"):
            with self.subTest(field=field), _Workspace() as ws:
                workpackage = valid_workpackage()
                del workpackage[field]
                seed_valid_workspace(ws)
                project = valid_project(workpackages=[workpackage])
                write_catalog(ws, PROJECTS_RELATIVE_PATH, {"projects": [project]})
                report = validate_workspace_catalogs(ws)
                self.assert_drift(report, "projects.json", field)


class MailDeskCatalogValidationTests(_ValidatorTestCase):
    """``mail-desk.json`` mirrors the ``load_reply_heuristics`` schema gate exactly."""

    def test_missing_mail_desk_catalog_is_valid_fallback(self) -> None:
        with _Workspace() as ws:
            seed_valid_workspace(ws, with_mail_desk=False)
            report = validate_workspace_catalogs(ws)
        self.assert_valid(report)

    def test_valid_mail_desk_catalog_passes(self) -> None:
        with _Workspace() as ws:
            seed_valid_workspace(ws)
            write_catalog(
                ws,
                MAIL_DESK_RELATIVE_PATH,
                {
                    "schema_version": 1,
                    "reply_heuristics": {
                        "reply_triggers": ["hallo klaus"],
                        "no_reply_sender_tokens": ["noreply@example.org"],
                        "owner_address": "desk-owner@example.org",
                    },
                },
            )
            report = validate_workspace_catalogs(ws)
        self.assert_valid(report)

    def test_schema_version_drift_is_rejected(self) -> None:
        payloads = {
            "boolean schema_version": valid_mail_desk(schema_version=True),
            "float schema_version": valid_mail_desk(schema_version=1.0),
            "string schema_version": valid_mail_desk(schema_version="1"),
            "wrong schema_version": valid_mail_desk(schema_version=2),
        }
        for label, payload in payloads.items():
            with self.subTest(case=label), _Workspace() as ws:
                seed_valid_workspace(ws)
                write_catalog(ws, MAIL_DESK_RELATIVE_PATH, payload)
                report = validate_workspace_catalogs(ws)
                self.assert_drift(report, "mail-desk.json", "schema_version")

    def test_missing_reply_heuristics_block_is_rejected(self) -> None:
        payloads = {
            "no reply_heuristics key": {"schema_version": 1},
            "reply_heuristics not an object": {
                "schema_version": 1,
                "reply_heuristics": [],
            },
        }
        for label, payload in payloads.items():
            with self.subTest(case=label), _Workspace() as ws:
                seed_valid_workspace(ws)
                write_catalog(ws, MAIL_DESK_RELATIVE_PATH, payload)
                report = validate_workspace_catalogs(ws)
                self.assert_drift(report, "mail-desk.json", "reply_heuristics")

    def test_reply_triggers_missing_empty_or_non_string_is_rejected(self) -> None:
        payloads = {
            "missing reply_triggers": {
                "schema_version": 1,
                "reply_heuristics": {"no_reply_sender_tokens": ["noreply@example.org"]},
            },
            "empty reply_triggers": {
                "schema_version": 1,
                "reply_heuristics": {"reply_triggers": []},
            },
            "non-string reply_triggers entry": {
                "schema_version": 1,
                "reply_heuristics": {"reply_triggers": [123]},
            },
            "blank reply_triggers entry": {
                "schema_version": 1,
                "reply_heuristics": {"reply_triggers": ["   "]},
            },
        }
        for label, payload in payloads.items():
            with self.subTest(case=label), _Workspace() as ws:
                seed_valid_workspace(ws)
                write_catalog(ws, MAIL_DESK_RELATIVE_PATH, payload)
                report = validate_workspace_catalogs(ws)
                self.assert_drift(report, "mail-desk.json", "reply_triggers")

    def test_no_reply_sender_tokens_wrong_type_is_rejected(self) -> None:
        payloads = {
            "string instead of list": valid_mail_desk(
                reply_heuristics={
                    "reply_triggers": ["hallo klaus"],
                    "no_reply_sender_tokens": "noreply@example.org",
                }
            ),
            "non-string list entry": valid_mail_desk(
                reply_heuristics={
                    "reply_triggers": ["hallo klaus"],
                    "no_reply_sender_tokens": [123],
                }
            ),
        }
        for label, payload in payloads.items():
            with self.subTest(case=label), _Workspace() as ws:
                seed_valid_workspace(ws)
                write_catalog(ws, MAIL_DESK_RELATIVE_PATH, payload)
                report = validate_workspace_catalogs(ws)
                self.assert_drift(report, "mail-desk.json", "no_reply_sender_tokens")

    def test_owner_address_wrong_type_is_rejected(self) -> None:
        payloads = {
            "integer owner_address": valid_mail_desk(
                reply_heuristics={
                    "reply_triggers": ["hallo klaus"],
                    "owner_address": 123,
                }
            ),
            "list owner_address": valid_mail_desk(
                reply_heuristics={
                    "reply_triggers": ["hallo klaus"],
                    "owner_address": ["desk@example.org"],
                }
            ),
        }
        for label, payload in payloads.items():
            with self.subTest(case=label), _Workspace() as ws:
                seed_valid_workspace(ws)
                write_catalog(ws, MAIL_DESK_RELATIVE_PATH, payload)
                report = validate_workspace_catalogs(ws)
                self.assert_drift(report, "mail-desk.json", "owner_address")

    def test_owner_address_null_is_accepted(self) -> None:
        with _Workspace() as ws:
            seed_valid_workspace(ws)
            write_catalog(
                ws,
                MAIL_DESK_RELATIVE_PATH,
                valid_mail_desk(
                    reply_heuristics={
                        "reply_triggers": ["hallo klaus"],
                        "owner_address": None,
                    }
                ),
            )
            report = validate_workspace_catalogs(ws)
        self.assert_valid(report)


class WorkspaceCatalogAggregateTests(_ValidatorTestCase):
    """Whole-workspace aggregation: required catalogs, optional fallback, all-valid."""

    def test_all_three_catalogs_present_and_valid_passes(self) -> None:
        with _Workspace() as ws:
            seed_valid_workspace(ws)
            report = validate_workspace_catalogs(ws)
        self.assert_valid(report)

    def test_live_boku_user_shape_with_short_root_pattern_is_valid(self) -> None:
        # Mirrors the real boku-user ``drittmittel-projektadmin-fis-support`` topic
        # whose root ``typical_subject_patterns`` include the 2-character ``QC``
        # signal: the whole workspace must validate without drift.
        with _Workspace() as ws:
            seed_valid_workspace(ws)
            topic = valid_topic(
                id="drittmittel-projektadmin-fis-support",
                title="Drittmittel-Projektadmin & FIS Support",
                mailbox_folder="Themen/Projektadmin-FIS",
                typical_subject_patterns=[
                    "Freigabe zur Einreichung",
                    "Quartalscontrolling",
                    "QC",
                    "SAP",
                ],
                subtopics=[
                    valid_subtopic(
                        id="controlling-argedata",
                        title="Controlling & ArgeData",
                        keywords=["qc", "argedata"],
                    )
                ],
            )
            write_catalog(ws, TOPICS_RELATIVE_PATH, [topic])
            report = validate_workspace_catalogs(ws)
        self.assert_valid(report)

    def test_missing_topics_catalog_is_a_failure(self) -> None:
        with _Workspace() as ws:
            seed_valid_workspace(ws)
            (ws / TOPICS_RELATIVE_PATH).unlink()
            report = validate_workspace_catalogs(ws)
        self.assert_drift(report, "topics.json")

    def test_missing_projects_catalog_is_a_failure(self) -> None:
        with _Workspace() as ws:
            seed_valid_workspace(ws)
            (ws / PROJECTS_RELATIVE_PATH).unlink()
            report = validate_workspace_catalogs(ws)
        self.assert_drift(report, "projects.json")


class CatalogValidatorReportTests(_ValidatorTestCase):
    """Every drift exposes catalog/path/reason and the report is deterministic."""

    def test_every_drift_exposes_catalog_json_path_and_reason(self) -> None:
        with _Workspace() as ws:
            seed_valid_workspace(ws)
            write_catalog(
                ws,
                TOPICS_RELATIVE_PATH,
                {"topics": [valid_topic(id="")]},
            )
            report = validate_workspace_catalogs(ws)
        errors = report.get("errors") or []
        self.assertTrue(errors)
        for error in errors:
            self.assertTrue({"catalog", "path", "reason"} <= set(error), error)
            self.assertTrue(str(error["catalog"]).strip(), error)
            self.assertIsInstance(error["path"], str, error)
            self.assertTrue(error["path"].strip(), error)
            self.assertTrue(str(error["reason"]).strip(), error)

    def test_report_is_sorted_and_stable_across_calls(self) -> None:
        with _Workspace() as ws:
            seed_valid_workspace(ws)
            write_catalog(
                ws,
                TOPICS_RELATIVE_PATH,
                {
                    "topics": [
                        {"id": "", "title": "Topic A", "mailbox_folder": "Themen/A"},
                        {"id": "topic-b", "title": "", "mailbox_folder": "Themen/B"},
                    ]
                },
            )
            write_catalog(
                ws,
                PROJECTS_RELATIVE_PATH,
                {"projects": [valid_project(milestones="not-a-list")]},
            )
            first = validate_workspace_catalogs(ws)
            second = validate_workspace_catalogs(ws)

        self.assertFalse(first["valid"])
        self.assertEqual(first, second, "validation must be deterministic")
        errors = first["errors"]
        key = lambda error: (
            _normalized_catalog(error["catalog"]),
            str(error["path"]),
        )
        self.assertEqual(errors, sorted(errors, key=key))
        catalogs = {_normalized_catalog(error["catalog"]) for error in errors}
        self.assertTrue(any(c.endswith("topics.json") for c in catalogs), catalogs)
        self.assertTrue(any(c.endswith("projects.json") for c in catalogs), catalogs)


class CatalogValidatorCliTests(_ValidatorTestCase):
    """Canonical CLI envelope and fail-closed exit codes."""

    def run_cli(self, workspace: Path) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                sys.executable,
                "-B",
                str(CLI_PATH),
                "--workspace",
                str(workspace),
                "--json",
            ],
            cwd=str(MAIL_DESK_ROOT),
            text=True,
            capture_output=True,
            check=False,
        )

    def assert_envelope(self, stdout: str) -> dict:
        self.assertEqual(1, len(stdout.strip().splitlines()), stdout)
        envelope = json.loads(stdout)
        self.assertEqual(list(CANONICAL_KEYS), list(envelope))
        self.assertEqual(CLI_ACTION, envelope["action"])
        return envelope

    def test_valid_workspace_exits_zero_with_canonical_envelope(self) -> None:
        with _Workspace() as ws:
            seed_valid_workspace(ws)
            result = self.run_cli(ws)
        self.assertEqual(0, result.returncode, result.stderr)
        envelope = self.assert_envelope(result.stdout)
        self.assertTrue(envelope["success"])
        self.assertEqual("Completed", envelope["state"])
        self.assertIsNone(envelope["error"])
        self.assertTrue(envelope["data"]["valid"])

    def test_drift_workspace_exits_one_with_drift_envelope(self) -> None:
        with _Workspace() as ws:
            seed_valid_workspace(ws)
            write_catalog(ws, TOPICS_RELATIVE_PATH, {"topics": [valid_topic(id="")]})
            result = self.run_cli(ws)
        self.assertEqual(1, result.returncode, result.stderr)
        envelope = self.assert_envelope(result.stdout)
        self.assertFalse(envelope["success"])
        self.assertIsInstance(envelope["error"], dict)
        self.assertTrue(str(envelope["error"].get("type", "")).strip())
        self.assertTrue(str(envelope["error"].get("message", "")).strip())
        self.assertFalse(envelope["data"]["valid"])

    def test_invalid_workspace_exits_two_with_error_envelope(self) -> None:
        with _Workspace() as ws:
            missing = ws / "does-not-exist"
            result = self.run_cli(missing)
        self.assertEqual(2, result.returncode, result.stderr)
        envelope = self.assert_envelope(result.stdout)
        self.assertFalse(envelope["success"])
        self.assertIsInstance(envelope["error"], dict)
        self.assertTrue(str(envelope["error"].get("type", "")).strip())


if __name__ == "__main__":
    unittest.main()
