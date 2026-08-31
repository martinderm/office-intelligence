"""Core package for mail-desk intelligence operations."""

from .common import (
    get_iso_week_folder,
    normalize_message_id,
    resolve_data_dir,
    resolve_evidence_dir,
    resolve_final_index_path,
    utc_now_iso,
)
from .evidence import flush_batch_evidence, update_evidence_file
from .himalaya import (
    get_single_email_details,
    run_himalaya,
    search_mailbox,
    verify_in_target_folder,
)
from .index import (
    load_final_index,
    lookup_final_index,
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
    draft_manifest,
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

__all__ = [
    "normalize_message_id",
    "utc_now_iso",
    "get_iso_week_folder",
    "resolve_data_dir",
    "resolve_evidence_dir",
    "resolve_final_index_path",
    "run_himalaya",
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
    "draft_manifest",
    "load_catalogs",
    "load_sent_index",
    "sync_sent_items",
    "check_if_replied",
    "clean_subject",
    "auto_resolve_replies_from_sent",
    "BatchProgressTracker",
]
