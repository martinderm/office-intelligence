"""Machine-readable compatibility contract for the ``core.quarantine`` move.

This suite locks the existing consumer seams that must survive the structural
extraction of the six quarantine/attachment modules into ``core/quarantine/``:

1. the exact legacy monkeypatch targets used by the existing Mail Desk suite
   stay effective through the re-export shim and act on the canonical owner;
2. the ``classifier_revision`` fingerprint source set and value are unchanged and
   never absorb a quarantine module;
3. the public symbols of every moved module stay importable by object identity
   through the legacy path;
4. the frozen ``core.__all__`` surface is preserved without removals.

It adds no production behaviour and touches no production module.
"""

from __future__ import annotations

import importlib
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

MAIL_DESK_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(MAIL_DESK_ROOT / "scripts"))

import core as core_package  # noqa: E402
from core import attachment_reclassification as reclass  # noqa: E402

# --------------------------------------------------------------------------------------
# Legacy monkeypatch seams already exercised by the existing suite
# --------------------------------------------------------------------------------------
# (patch target string, canonical owner module, attribute name).
_PATCH_STRING_SEAMS = (
    (
        "core.attachment_quarantine_index.load_quarantine_index",
        "core.quarantine.quarantine_index",
        "load_quarantine_index",
    ),
    (
        "core.attachment_quarantine_index.verify_quarantine_workspace_lock",
        "core.quarantine.quarantine_index",
        "verify_quarantine_workspace_lock",
    ),
)

# (legacy module, attribute, canonical owner module).
_PATCH_OBJECT_SEAMS = (
    ("core.attachment_fetch", "verify_workspace_lock", "core.quarantine.attachment_fetch"),
    ("core.attachment_fetch", "resolve_data_dir", "core.quarantine.attachment_fetch"),
    ("core.attachment_extract", "_kernel32", "core.quarantine.attachment_extract"),
    ("core.attachment_extract", "_win32_init_error", "core.quarantine.attachment_extract"),
    (
        "core.attachment_extract",
        "extract_attachment_content",
        "core.quarantine.attachment_extract",
    ),
    (
        "core.attachment_extract",
        "check_quarantine_path_security",
        "core.quarantine.attachment_extract",
    ),
    (
        "core.attachment_filing",
        "propose_attachment_filing",
        "core.quarantine.attachment_filing",
    ),
)

# --------------------------------------------------------------------------------------
# Classifier revision fingerprint sources
# --------------------------------------------------------------------------------------
_EXPECTED_RULE_SOURCES = (
    "scripts/core/classifier.py",
    "scripts/core/matching/ambiguity.py",
    "scripts/core/matching/date_parser.py",
    "scripts/core/matching/project_matching.py",
    "scripts/core/matching/topic_matching.py",
)

_QUARANTINE_MODULE_FILENAMES = frozenset(
    {
        "attachment_quarantine_index.py",
        "attachment_fetch.py",
        "attachment_extract.py",
        "attachment_filing.py",
        "attachment_policy.py",
        "attachment_handoff.py",
    }
)

# --------------------------------------------------------------------------------------
# Public symbol identity inventory (legacy module, symbol, canonical owner module)
# --------------------------------------------------------------------------------------
_PUBLIC_SYMBOLS = (
    ("attachment_quarantine_index", "compute_attachment_id", "quarantine_index"),
    ("attachment_quarantine_index", "load_quarantine_index", "quarantine_index"),
    ("attachment_quarantine_index", "validate_quarantine_index_entry", "quarantine_index"),
    ("attachment_quarantine_index", "record_quarantine_entry", "quarantine_index"),
    ("attachment_quarantine_index", "reconcile_quarantine_index", "quarantine_index"),
    ("attachment_quarantine_index", "verify_workspace_lock", "attachment_fetch"),
    ("attachment_quarantine_index", "check_quarantine_path_security", "attachment_fetch"),
    ("attachment_quarantine_index", "is_valid_run_id", "attachment_fetch"),
    ("attachment_quarantine_index", "sanitize_attachment_filename", "attachment_policy"),
    ("attachment_fetch", "compute_review_hash", "attachment_fetch"),
    ("attachment_fetch", "op_attachment_fetch", "attachment_fetch"),
    ("attachment_fetch", "verify_workspace_lock", "attachment_fetch"),
    ("attachment_fetch", "check_quarantine_path_security", "attachment_fetch"),
    ("attachment_fetch", "DEFAULT_ATTACHMENT_POLICY", "attachment_policy"),
    ("attachment_fetch", "sanitize_attachment_filename", "attachment_policy"),
    ("attachment_extract", "extract_attachment_content", "attachment_extract"),
    ("attachment_extract", "validate_mda2_fetch_result", "attachment_extract"),
    ("attachment_extract", "run_with_timeout", "attachment_extract"),
    ("attachment_extract", "check_quarantine_path_security", "attachment_fetch"),
    ("attachment_extract", "is_valid_run_id", "attachment_fetch"),
    ("attachment_extract", "_atomic_no_clobber_promote", "attachment_fetch"),
    ("attachment_extract", "WorkspaceLockError", "attachment_fetch"),
    ("attachment_filing", "propose_attachment_filing", "attachment_filing"),
    ("attachment_filing", "validate_mda2_attachment", "attachment_filing"),
    ("attachment_filing", "is_valid_run_id", "attachment_fetch"),
    ("attachment_filing", "compute_review_hash", "attachment_fetch"),
    ("attachment_filing", "validate_attachment_handoff", "attachment_handoff"),
    ("attachment_filing", "HandoffDriftError", "attachment_handoff"),
    ("attachment_filing", "sanitize_attachment_filename", "attachment_policy"),
    ("attachment_policy", "sanitize_attachment_filename", "attachment_policy"),
    ("attachment_policy", "check_attachment_policy", "attachment_policy"),
    ("attachment_policy", "DEFAULT_ATTACHMENT_POLICY", "attachment_policy"),
    ("attachment_handoff", "validate_attachment_handoff", "attachment_handoff"),
    ("attachment_handoff", "build_attachment_analysis_handoff", "attachment_handoff"),
    ("attachment_handoff", "compute_handoff_hash", "attachment_handoff"),
    ("attachment_handoff", "apply_attachment_handoff_to_item", "attachment_handoff"),
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


class LegacyMonkeypatchSeamTests(unittest.TestCase):
    """Existing monkeypatch seams keep acting on the canonical owner module."""

    def test_patch_string_seams_take_effect_on_the_owner(self) -> None:
        sentinel = object()
        for target, owner_dotted, attribute in _PATCH_STRING_SEAMS:
            with self.subTest(target=target):
                legacy_dotted, _, legacy_attribute = target.rpartition(".")
                self.assertEqual(attribute, legacy_attribute)
                legacy = importlib.import_module(legacy_dotted)
                owner = importlib.import_module(owner_dotted)
                self.assertIs(getattr(legacy, attribute), getattr(owner, attribute))
                with patch(target, sentinel):
                    self.assertIs(getattr(owner, attribute), sentinel)
                    self.assertIs(getattr(legacy, attribute), sentinel)
                self.assertIs(getattr(legacy, attribute), getattr(owner, attribute))

    def test_patch_object_seams_take_effect_on_the_owner(self) -> None:
        sentinel = object()
        for legacy_dotted, attribute, owner_dotted in _PATCH_OBJECT_SEAMS:
            with self.subTest(legacy=legacy_dotted, attribute=attribute):
                legacy = importlib.import_module(legacy_dotted)
                owner = importlib.import_module(owner_dotted)
                self.assertTrue(hasattr(legacy, attribute))
                self.assertTrue(hasattr(owner, attribute))
                self.assertIs(getattr(legacy, attribute), getattr(owner, attribute))
                with patch.object(legacy, attribute, sentinel):
                    self.assertIs(getattr(owner, attribute), sentinel)
                    self.assertIs(getattr(legacy, attribute), sentinel)
                self.assertIs(getattr(legacy, attribute), getattr(owner, attribute))
                with patch.object(owner, attribute, sentinel):
                    self.assertIs(getattr(legacy, attribute), sentinel)


class ClassifierRevisionFingerprintTests(unittest.TestCase):
    """The fingerprint binds only the classifier sources and stays unchanged."""

    def test_ordered_rule_source_set_is_unchanged(self) -> None:
        sources = tuple(reclass._CLASSIFIER_MODULE_PATHS)
        relative = tuple(
            Path(path).resolve().relative_to(MAIL_DESK_ROOT).as_posix() for path in sources
        )
        self.assertEqual(_EXPECTED_RULE_SOURCES, relative)

    def test_no_quarantine_module_is_bound_into_the_fingerprint(self) -> None:
        for path in reclass._CLASSIFIER_MODULE_PATHS:
            resolved = Path(path).resolve()
            with self.subTest(source=resolved.name):
                self.assertNotIn("quarantine", resolved.parts)
                self.assertNotIn(resolved.name, _QUARANTINE_MODULE_FILENAMES)

    def test_fingerprint_value_is_stable_across_package_import(self) -> None:
        before = reclass.classifier_rules_fingerprint(MAIL_DESK_ROOT)
        importlib.import_module("core.quarantine")
        after = reclass.classifier_rules_fingerprint(MAIL_DESK_ROOT)
        self.assertEqual(before, after)


class PublicSymbolIdentityTests(unittest.TestCase):
    """Every listed public symbol stays importable by identity through the legacy path."""

    def test_public_symbol_table_is_unique(self) -> None:
        keys = [(legacy, symbol) for legacy, symbol, _ in _PUBLIC_SYMBOLS]
        self.assertEqual(len(keys), len(set(keys)))

    def test_public_symbols_resolve_to_the_owner_objects(self) -> None:
        for legacy_name, symbol, owner_name in _PUBLIC_SYMBOLS:
            with self.subTest(legacy=legacy_name, symbol=symbol):
                legacy = importlib.import_module(f"core.{legacy_name}")
                owner = importlib.import_module(f"core.quarantine.{owner_name}")
                self.assertTrue(hasattr(legacy, symbol))
                self.assertTrue(hasattr(owner, symbol))
                self.assertIs(getattr(legacy, symbol), getattr(owner, symbol))


class CoreAllSnapshotTests(unittest.TestCase):
    """``core.__all__`` remains exactly the frozen baseline list."""

    def test_core_all_is_unchanged(self) -> None:
        self.assertEqual(_EXPECTED_CORE_ALL, list(core_package.__all__))

    def test_core_all_entries_are_unique_and_resolve(self) -> None:
        self.assertEqual(len(core_package.__all__), len(set(core_package.__all__)))
        for name in core_package.__all__:
            with self.subTest(symbol=name):
                self.assertTrue(hasattr(core_package, name))


if __name__ == "__main__":
    unittest.main()
