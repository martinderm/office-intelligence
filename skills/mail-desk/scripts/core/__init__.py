"""Core package for mail-desk intelligence operations."""

from .common import (
    atomic_rewrite_jsonl,
    atomic_write_json,
    atomic_write_text,
    get_iso_week_folder,
    normalize_message_id,
    resolve_data_dir,
    resolve_evidence_dir,
    resolve_final_index_path,
    utc_now_iso,
)
from .evidence import flush_batch_evidence, update_evidence_file
from .himalaya import (
    HimalayaInvocationError,
    build_himalaya_command,
    get_single_email_details,
    resolve_himalaya_invocation,
    run_himalaya,
    search_mailbox,
    verify_in_target_folder,
)
from .index import (
    backfill_index_signatures,
    build_signature_index,
    extract_email_address,
    load_final_index,
    lookup_final_index,
    normalize_signature_text,
    query_final_index,
    save_final_index_atomic,
    upsert_final_index_entry,
    upsert_final_index_many,
)
from .action_log import (
    append_action_log_entry,
    append_replies_needed_entry,
    resolve_case,
)

from .classifier import (
    classify_email,
    classify_email_two_pass,
    draft_manifest,
    full_body_triggers,
    load_catalogs,
)
from .sent_indexer import (
    auto_resolve_replies_from_sent,
    check_if_replied,
    clean_subject,
    load_sent_index,
    sync_sent_items,
)
from .progress import BatchProgressTracker
from .envelope import build_error, build_success, emit_json

__all__ = [
    "atomic_rewrite_jsonl",
    "atomic_write_json",
    "atomic_write_text",
    "normalize_message_id",
    "utc_now_iso",
    "get_iso_week_folder",
    "resolve_data_dir",
    "resolve_evidence_dir",
    "resolve_final_index_path",
    "run_himalaya",
    "HimalayaInvocationError",
    "build_himalaya_command",
    "resolve_himalaya_invocation",
    "get_single_email_details",
    "verify_in_target_folder",
    "search_mailbox",
    "load_final_index",
    "save_final_index_atomic",
    "upsert_final_index_entry",
    "upsert_final_index_many",
    "query_final_index",
    "lookup_final_index",
    "append_action_log_entry",
    "append_replies_needed_entry",
    "resolve_case",
    "update_evidence_file",
    "classify_email",
    "classify_email_two_pass",
    "draft_manifest",
    "full_body_triggers",
    "load_catalogs",
    "load_sent_index",
    "sync_sent_items",
    "check_if_replied",
    "clean_subject",
    "auto_resolve_replies_from_sent",
    "BatchProgressTracker",
    "build_success",
    "build_error",
    "emit_json",
]
