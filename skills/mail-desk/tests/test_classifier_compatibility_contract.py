"""FR-13 / MD-M1-T04 machine-readable classifier compatibility contract.

Baseline-green characterization (evidence mode ``characterization``, risk tier
``high``): this module does not manufacture a Red.  It locks the pre-existing
``core.classifier`` compatibility surface named by the approved MD-M1 spec
(``spec.md`` lines 83-109) together with the ``core`` package re-exports and
``__all__`` snapshot, so any future contraction that drops or rebinds a baseline
symbol fails loudly.

Scope is deliberately structural.  Behavioural parity for the characterized
corpus is already owned by the focused per-ticket suites (``test_classifier_*``);
this suite only makes the existing facade, direct-import, lazy-import,
dependency-injection and monkeypatch seams machine-readable.  It adds no
production behaviour and touches no production module.
"""

from __future__ import annotations

import importlib
import inspect
import re
import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

MAIL_DESK_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(MAIL_DESK_ROOT / "scripts"))

import core as core_package  # noqa: E402
from core import classifier  # noqa: E402

_FACADE = "core.classifier"

# --------------------------------------------------------------------------------------
# Spec: classifier facade functions (spec.md lines 83-97)
# --------------------------------------------------------------------------------------
# (facade name, canonical owner module).  Catalog I/O and the top-level
# orchestrators stay owned by ``core.classifier``; every moved matcher is owned by a
# ``core.matching`` vertical and reaches the facade by object identity only.
_SPEC_FUNCTIONS = (
    ("parse_date_to_year_month", "core.matching.date_parser"),
    ("load_catalogs", _FACADE),
    ("full_body_triggers", _FACADE),
    ("classify_email_two_pass", _FACADE),
    ("classify_email", _FACADE),
    ("draft_manifest", _FACADE),
    ("_artifact_text_matches", "core.matching.project_matching"),
    ("_artifact_code_matches", "core.matching.project_matching"),
    ("_artifact_candidate", "core.matching.project_matching"),
    ("_select_project_artifacts", "core.matching.project_matching"),
    ("_catalog_artifact_title", "core.matching.project_matching"),
    ("_project_context_label", "core.matching.project_matching"),
    ("_evidence_read_escalation", "core.matching.project_matching"),
    ("_build_project_evidence", "core.matching.project_matching"),
    ("_topic_parent_subject_signal", "core.matching.topic_matching"),
    ("_subject_signal_matches", "core.matching.topic_matching"),
    ("_select_topic_subtopic", "core.matching.topic_matching"),
    ("_topic_context_label", "core.matching.topic_matching"),
    ("_select_subtopic_operation", "core.matching.topic_matching"),
    ("_operation_context_label", "core.matching.topic_matching"),
    ("_select_subtopic_event", "core.matching.topic_matching"),
    ("_event_validation_reasons", "core.matching.topic_matching"),
    ("_event_context_label", "core.matching.topic_matching"),
    ("_build_topic_evidence", "core.matching.topic_matching"),
    ("_build_operation_evidence", "core.matching.topic_matching"),
    ("_build_event_evidence", "core.matching.topic_matching"),
    ("_safe_subtopic_reference_target", "core.matching.topic_matching"),
    ("_safe_operation_reference_target", "core.matching.topic_matching"),
    ("_safe_event_dossier_target", "core.matching.topic_matching"),
    ("_has_canonical_operation_reference", "core.matching.topic_matching"),
    ("_full_read_failure", _FACADE),
)

# --------------------------------------------------------------------------------------
# Spec: baseline imported attributes (spec.md lines 102-109)
# --------------------------------------------------------------------------------------
# (facade name, owner module, owner attribute).  ``None`` as the owner attribute means
# the imported module object itself is canonical (``json``, ``re``).
_SPEC_IMPORTED_ATTRIBUTES = (
    ("json", "json", None),
    ("re", "re", None),
    ("date", "datetime", "date"),
    ("datetime", "datetime", "datetime"),
    ("Path", "pathlib", "Path"),
    ("PurePosixPath", "pathlib", "PurePosixPath"),
    ("Any", "typing", "Any"),
    ("Callable", "typing", "Callable"),
    ("Mapping", "typing", "Mapping"),
    ("HandoffDriftError", "core.attachment_handoff", "HandoffDriftError"),
    (
        "apply_attachment_handoff_to_item",
        "core.attachment_handoff",
        "apply_attachment_handoff_to_item",
    ),
    (
        "build_attachment_analysis_handoff",
        "core.attachment_handoff",
        "build_attachment_analysis_handoff",
    ),
    ("validate_attachment_handoff", "core.attachment_handoff", "validate_attachment_handoff"),
    (
        "AttachmentInventoryValidationError",
        "core.attachments",
        "AttachmentInventoryValidationError",
    ),
    (
        "canonicalize_and_bind_attachments",
        "core.attachments",
        "canonicalize_and_bind_attachments",
    ),
    ("normalize_message_id", "core.common", "normalize_message_id"),
    ("resolve_data_dir", "core.common", "resolve_data_dir"),
    ("resolve_evidence_dir", "core.common", "resolve_evidence_dir"),
    ("resolve_final_index_path", "core.common", "resolve_final_index_path"),
    ("load_final_index", "core.index", "load_final_index"),
    ("check_if_replied", "core.sent_indexer", "check_if_replied"),
    ("load_sent_index", "core.sent_indexer", "load_sent_index"),
    ("sync_sent_items", "core.sent_indexer", "sync_sent_items"),
)

# --------------------------------------------------------------------------------------
# Spec: baseline constants (spec.md lines 99-100)
# --------------------------------------------------------------------------------------
_BASELINE_FULL_BODY_ARTIFACT_SIGNALS = (
    "qm plan",
    "draft",
    "handbook",
    "deliverable",
    "agreement",
    "red flags",
    "audit",
    "focus group",
    "fokusgruppe",
)

_BASELINE_FULL_BODY_ACTION_PATTERN = (
    r"\b(?:please|kindly|could you|can you|action required|please respond|"
    r"bitte|kannst du|können sie)\b"
)

# --------------------------------------------------------------------------------------
# Spec: core package re-exports and __all__ (spec.md lines 111-112)
# --------------------------------------------------------------------------------------
# The five classifier symbols re-exported by ``scripts/core/__init__.py``.
_CORE_CLASSIFIER_REEXPORTS = (
    "classify_email",
    "classify_email_two_pass",
    "draft_manifest",
    "full_body_triggers",
    "load_catalogs",
)

# Frozen baseline ``core.__all__`` snapshot, in declaration order.
_EXPECTED_CORE_ALL = """
atomic_rewrite_jsonl
atomic_write_json
atomic_write_text
normalize_message_id
utc_now_iso
get_iso_week_folder
resolve_data_dir
resolve_evidence_dir
resolve_final_index_path
run_himalaya
HimalayaInvocationError
build_himalaya_command
resolve_himalaya_invocation
get_single_email_details
verify_in_target_folder
search_mailbox
load_final_index
save_final_index_atomic
upsert_final_index_entry
upsert_final_index_many
query_final_index
lookup_final_index
append_action_log_entry
append_replies_needed_entry
resolve_case
update_evidence_file
classify_email
classify_email_two_pass
draft_manifest
full_body_triggers
load_catalogs
load_sent_index
sync_sent_items
check_if_replied
clean_subject
auto_resolve_replies_from_sent
BatchProgressTracker
build_success
build_error
emit_json
INDEX_FILENAME
SCHEMA_VERSION
LIFECYCLE_STATE_QUARANTINED
ANALYSIS_STATUS_COMPLETED
ANALYSIS_COMPLETENESS_FULL
ANALYSIS_COMPLETENESS_TRUNCATED
ANALYSIS_COMPLETENESS_PARTIAL
ANALYSIS_COMPLETENESS_UNAVAILABLE
ANALYSIS_COMPLETENESS_UNKNOWN
ALLOWED_ANALYSIS_COMPLETENESS
TRUNCATION_STAGE_NONE
TRUNCATION_STAGE_EXTRACTION
TRUNCATION_STAGE_HANDOFF_PER_ATTACHMENT
TRUNCATION_STAGE_HANDOFF_CUMULATIVE_MAIL
ALLOWED_TRUNCATION_STAGES
QuarantineIndexError
WorkspaceLockRequiredError
AttachmentIndexDriftError
AttachmentIndexSchemaError
ForbiddenContentError
PhysicalVerificationError
compute_attachment_id
resolve_quarantine_index_path
load_quarantine_index
save_quarantine_index_atomic
validate_quarantine_index_entry
record_quarantine_entry
reconcile_quarantine_index
lookup_quarantine_entry
get_quarantine_index_stats
canonical_index_entry_sha256
remove_quarantine_entry
update_quarantine_entry_disposition_ref
CONTEXT_APPLY
CONTEXT_DIRECT_FETCH
CONTEXT_DISPOSITION
CONTEXT_EVALUATION
CONTEXT_EXPORT
CONTEXT_FILING
CONTEXT_PROMOTION
HUMAN_APPROVAL_CONTEXTS
MACHINE_APPROVER_ID
RECEIPT_CLASS_HUMAN
RECEIPT_CLASS_MACHINE
RECEIPT_TYPE_ATTACHMENT_AUTO_EVALUATION
AttachmentAuthorizationError
MachineAuthorizationProvenanceError
MachineAuthorizationRequiredError
MachineBindingError
ReceiptClassRejectedError
UnknownAuthorizationContextError
create_machine_authorization
guard_context_authorization
ALLOWED_ATTACHMENT_EVALUATION_AUTHORIZATIONS
ALLOWED_ATTACHMENT_EVALUATION_COVERAGES
ALLOWED_ATTACHMENT_EVALUATION_REASONS
ALLOWED_ATTACHMENT_EVALUATION_STATUSES
AUTHORIZATION_AUTO_EVALUATED
AUTHORIZATION_NOT_APPLICABLE
COVERAGE_FULL
COVERAGE_TRUNCATED
REASON_CLASSIFICATION_CLEAR
REASON_EVALUATION_PENDING
REASON_EXTRACTION_FAILED
REASON_FETCH_FAILED
REASON_HANDOFF_INVALID
REASON_HANDOFF_READY
REASON_LOCK_UNAVAILABLE
REASON_NO_ALLOWED_ATTACHMENTS
REASON_NO_ATTACHMENTS
REASON_POLICY_BLOCKED
REASON_QUOTA_EXCEEDED
REASON_STILL_AMBIGUOUS
STATUS_COMPLETED
STATUS_FAILED
STATUS_NOT_NEEDED
STATUS_SKIPPED
AttachmentEvaluationError
attachment_evaluate
DISPOSITION_LOG_FILENAME
DISCARD_JOURNAL_FILENAME
DECISION_RETAIN
DECISION_DISCARD
DECISION_PROMOTE
ALLOWED_DECISIONS
STATUS_ELIGIBLE
STATUS_PROTECTED
STATUS_INVALID
ALLOWED_REPORT_STATUSES
JOURNAL_STATE_PREPARED
JOURNAL_STATE_FILE_DELETED
JOURNAL_STATE_INVENTORY_UPDATED
JOURNAL_STATE_INDEX_UPDATED
JOURNAL_STATE_COMPLETED
JOURNAL_STATE_FAILED
ALLOWED_JOURNAL_STATES
DispositionError
DispositionSchemaError
DispositionDriftError
DispositionLockRequiredError
DispositionApplyError
ReceiptError
ReceiptMissingError
ReceiptMalformedError
ReceiptDriftError
RecoveryEvidenceMissingError
RecoveryJournalCorruptedError
InventoryUpdateError
compute_decision_id
resolve_disposition_log_path
resolve_discard_journal_path
validate_disposition_entry
load_disposition_log
load_discard_journal
record_disposition_entry
check_active_run_evidence
report_dispositions
apply_discard
canonical_receipt_sha256
build_disposition_request
canonical_disposition_request_sha256
build_disposition_receipt
verify_approval_receipt
build_apply_request
canonical_apply_request_sha256
build_apply_receipt
verify_apply_receipt
record_journal_state
record_journal_failure
DISCARD_JOURNAL_ENTRY_ALLOWED_KEYS
DISCARD_JOURNAL_HISTORY_ALLOWED_KEYS
STATE_ORDER
update_quarantine_inventory_atomic
""".split()

# --------------------------------------------------------------------------------------
# Existing import / DI / monkeypatch seams used by current consumers
# --------------------------------------------------------------------------------------
# ``switchboard`` direct-import consumer: ``mail_desk_inspect_manifest.py``.
_DIRECT_IMPORT_NAMES = ("classify_email", "load_catalogs")

# Owner modules imported on demand by the facade and by consumers (lazy-import seam).
_OWNER_MODULES = (
    "core.matching",
    "core.matching.ambiguity",
    "core.matching.date_parser",
    "core.matching.project_matching",
    "core.matching.topic_matching",
)

# Dependency-injection keyword keys consumed by the public facade callables.
_DI_KEYS = {
    "classify_email": (
        "workspace_root",
        "projects",
        "topics",
        "sent_lookup",
        "final_index",
        "account",
        "untrusted_external_text",
    ),
    "classify_email_two_pass": (
        "workspace_root",
        "projects",
        "topics",
        "sent_lookup",
        "final_index",
        "full_reader",
        "account",
        "source_sink",
    ),
    "draft_manifest": (
        "workspace_root",
        "delete_input_on_success",
        "sent_lookup",
        "sync_sent",
        "final_index",
        "full_reader",
        "account",
        "source_sink",
    ),
    "load_catalogs": ("workspace_root",),
    "full_body_triggers": ("email", "preview_item"),
}

# Representative facade seams already monkeypatched by the existing suite.
_MONKEYPATCH_TARGETS = (
    "sync_sent_items",
    "load_catalogs",
    "load_sent_index",
    "load_final_index",
    "resolve_final_index_path",
    "parse_date_to_year_month",
    "build_attachment_analysis_handoff",
    "apply_attachment_handoff_to_item",
    "validate_attachment_handoff",
)

# --------------------------------------------------------------------------------------
# MD-M1-T04 extracted domain-routing helpers reached from the facade
# --------------------------------------------------------------------------------------
# (facade module attribute, canonical owner module, owner callable).  These second-wave
# seams are not part of the frozen baseline inventory; they are asserted only for identity
# and routing so a later contraction cannot silently re-inline the blocked domain work.
_ROUTING_HELPERS = (
    ("project_matching", "core.matching.project_matching", "match_thread_project_inheritance"),
    ("project_matching", "core.matching.project_matching", "resolve_full_read_project_evidence"),
    ("topic_matching", "core.matching.topic_matching", "match_thread_topic_inheritance"),
    ("topic_matching", "core.matching.topic_matching", "resolve_full_read_topic_evidence"),
)


class ClassifierFacadeFunctionContractTests(unittest.TestCase):
    """Every baseline facade function stays present, callable and canonically owned."""

    def test_spec_function_table_is_unique_and_enumerates_all_31_symbols(self) -> None:
        names = [name for name, _ in _SPEC_FUNCTIONS]
        self.assertEqual(31, len(names))
        self.assertEqual(len(names), len(set(names)))

    def test_every_spec_function_is_identity_bound_to_its_canonical_owner(self) -> None:
        for name, owner_name in _SPEC_FUNCTIONS:
            with self.subTest(symbol=name):
                self.assertTrue(hasattr(classifier, name))
                value = getattr(classifier, name)
                self.assertTrue(callable(value))
                owner = importlib.import_module(owner_name)
                self.assertIs(value, getattr(owner, name))
                # Facade-resident callables must never be silently relocated; moved
                # callables must report their matching owner as the defining module.
                self.assertEqual(owner_name, value.__module__)


class ClassifierFacadeImportedAttributeContractTests(unittest.TestCase):
    """Every baseline imported attribute stays bound to its canonical definition."""

    def test_spec_imported_attribute_table_is_unique_and_covers_all_23_symbols(self) -> None:
        names = [name for name, _, _ in _SPEC_IMPORTED_ATTRIBUTES]
        self.assertEqual(23, len(names))
        self.assertEqual(len(names), len(set(names)))

    def test_every_spec_imported_attribute_is_identity_bound(self) -> None:
        for name, module_name, attribute in _SPEC_IMPORTED_ATTRIBUTES:
            with self.subTest(symbol=name):
                self.assertTrue(hasattr(classifier, name))
                owner = importlib.import_module(module_name)
                canonical = owner if attribute is None else getattr(owner, attribute)
                self.assertIs(getattr(classifier, name), canonical)


class ClassifierFacadeConstantContractTests(unittest.TestCase):
    """The two baseline facade constants keep their value and type."""

    def test_full_body_artifact_signals_unchanged(self) -> None:
        signals = classifier.FULL_BODY_ARTIFACT_SIGNALS
        self.assertIsInstance(signals, tuple)
        self.assertEqual(_BASELINE_FULL_BODY_ARTIFACT_SIGNALS, signals)
        self.assertTrue(all(isinstance(signal, str) for signal in signals))

    def test_full_body_action_request_unchanged(self) -> None:
        action_request = classifier.FULL_BODY_ACTION_REQUEST
        self.assertIsInstance(action_request, re.Pattern)
        self.assertEqual(_BASELINE_FULL_BODY_ACTION_PATTERN, action_request.pattern)
        self.assertTrue(bool(action_request.flags & re.IGNORECASE))


class CorePackageReExportContractTests(unittest.TestCase):
    """The existing core package re-exports and __all__ snapshot stay unchanged."""

    def test_five_classifier_reexports_are_identity_bound_and_exported(self) -> None:
        for name in _CORE_CLASSIFIER_REEXPORTS:
            with self.subTest(symbol=name):
                self.assertIs(getattr(core_package, name), getattr(classifier, name))
                self.assertIn(name, core_package.__all__)

    def test_core_all_snapshot_is_unchanged(self) -> None:
        self.assertEqual(_EXPECTED_CORE_ALL, list(core_package.__all__))

    def test_core_all_entries_are_unique_and_resolve(self) -> None:
        self.assertEqual(len(core_package.__all__), len(set(core_package.__all__)))
        for name in core_package.__all__:
            with self.subTest(symbol=name):
                self.assertTrue(hasattr(core_package, name))


class ClassifierImportSeamContractTests(unittest.TestCase):
    """The direct- and lazy-import seams used by existing consumers stay operational."""

    def test_direct_facade_import_surface_resolves(self) -> None:
        # ``from core.classifier import classify_email, load_catalogs`` is exactly this
        # module import followed by attribute resolution.
        facade = importlib.import_module("core.classifier")
        self.assertIs(facade, classifier)
        for name in _DIRECT_IMPORT_NAMES:
            with self.subTest(symbol=name):
                self.assertIs(getattr(facade, name), getattr(classifier, name))

    def test_owner_modules_are_lazily_importable_by_dotted_name(self) -> None:
        for dotted in _OWNER_MODULES:
            with self.subTest(module=dotted):
                module = importlib.import_module(dotted)
                self.assertEqual(dotted, module.__name__)
                self.assertIs(module, sys.modules[dotted])


class ClassifierDependencyInjectionSeamTests(unittest.TestCase):
    """The documented DI keyword keys remain accepted by the public callables."""

    def test_public_callables_accept_the_documented_di_keys(self) -> None:
        keyword_kinds = (
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
            inspect.Parameter.KEYWORD_ONLY,
        )
        for function, keys in _DI_KEYS.items():
            with self.subTest(function=function):
                parameters = inspect.signature(getattr(classifier, function)).parameters
                for key in keys:
                    self.assertIn(key, parameters)
                    self.assertIn(parameters[key].kind, keyword_kinds)


class ClassifierMonkeypatchSeamTests(unittest.TestCase):
    """The existing facade monkeypatch targets remain patchable and restore cleanly."""

    def test_monkeypatch_targets_are_patchable_and_restored(self) -> None:
        sentinel = object()
        for name in _MONKEYPATCH_TARGETS:
            with self.subTest(symbol=name):
                self.assertTrue(hasattr(classifier, name))
                original = getattr(classifier, name)
                with patch.object(classifier, name, sentinel):
                    self.assertIs(getattr(classifier, name), sentinel)
                self.assertIs(getattr(classifier, name), original)


class ClassifierDomainRoutingHelperContractTests(unittest.TestCase):
    """The T04 extracted domain-routing helpers stay bound to their canonical owners."""

    def test_routing_helpers_are_identity_bound_to_their_owners(self) -> None:
        for module_attr, owner_name, helper_name in _ROUTING_HELPERS:
            with self.subTest(helper=helper_name):
                facade_owner = getattr(classifier, module_attr)
                owner = importlib.import_module(owner_name)
                self.assertIs(facade_owner, owner)
                helper = getattr(owner, helper_name)
                self.assertTrue(callable(helper))
                self.assertEqual(owner_name, getattr(helper, "__module__", None))

    def test_thread_parent_lookup_routes_through_the_project_owner(self) -> None:
        project = {
            "id": "meshe",
            "kuerzel": "MESHE",
            "title": "MESHE",
            "mailbox_folder": "Projects/MESHE",
        }
        owner = importlib.import_module("core.matching.project_matching")
        spy = Mock(wraps=owner.match_thread_project_inheritance)
        email = {
            "envelope_id": "1",
            "folder": "INBOX",
            "message_id": "<child@example.test>",
            "subject": "MESHE update",
            "from": "sender@example.test",
            "to": "desk@example.test",
            "date": "2026-05-18",
            "in_reply_to": "<parent@example.test>",
        }
        with patch.object(owner, "match_thread_project_inheritance", spy):
            item = classifier.classify_email(
                email,
                projects=[project],
                topics=[],
                sent_lookup={},
                final_index={
                    "items": {"parent@example.test": {"final_folder": "Projects/MESHE"}}
                },
            )
        self.assertTrue(spy.called, "thread inheritance must route through the project owner")
        self.assertEqual("project", item["decision"]["kind"])
        self.assertEqual("meshe", item["decision"]["id"])
        self.assertEqual("Projects/MESHE", item["action"]["target_folder"])


if __name__ == "__main__":
    unittest.main()
