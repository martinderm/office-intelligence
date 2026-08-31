#!/usr/bin/env python3
"""Deterministic manifest inspector and summary tool for mail-desk.

Provides structured and human-readable summaries of drafted batch manifests
(default: data/mail-desk/batch-manifest.json).

Usage:
    python3 scripts/mail_desk_inspect_manifest.py
    python3 scripts/mail_desk_inspect_manifest.py --input data/mail-desk/batch-manifest.json
    python3 scripts/mail_desk_inspect_manifest.py --json
    python3 scripts/mail_desk_inspect_manifest.py --needs-reply
"""

from __future__ import annotations

import argparse
from collections import Counter
import json
from pathlib import Path
import sys
from typing import Any


from core.classifier import classify_email, load_catalogs
from core.common import normalize_message_id, resolve_data_dir, resolve_final_index_path
from core.index import load_final_index


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Inspect, filter, audit and re-classify mail-desk batch manifests."
    )
    parser.add_argument(
        "--input",
        "-i",
        type=str,
        default="data/mail-desk/batch-manifest.json",
        help="Path to the batch manifest JSON file (default: data/mail-desk/batch-manifest.json)",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Output in structured JSON envelope format.",
    )
    parser.add_argument(
        "--needs-reply",
        action="store_true",
        help="Filter items that require reply (needs_reply == True).",
    )
    parser.add_argument(
        "--unindexed",
        action="store_true",
        help="Filter items that are not yet recorded in final-location-index.json.",
    )
    parser.add_argument(
        "--reclassify",
        action="store_true",
        help="Re-run classifier using updated catalogs on all items and overwrite the manifest.",
    )
    parser.add_argument(
        "--filter-kind",
        type=str,
        default=None,
        choices=["project", "topic", "unknown", "inbox-review", "ignore"],
        help="Filter items by decision kind.",
    )
    return parser.parse_args()


def load_manifest(manifest_path: Path) -> dict[str, Any]:
    if not manifest_path.exists():
        raise FileNotFoundError(f"Manifest file not found: {manifest_path}")
    with manifest_path.open("r", encoding="utf-8") as f:
        return json.load(f)


def inspect_manifest(
    manifest_data: dict[str, Any],
    needs_reply_only: bool = False,
    unindexed_only: bool = False,
    filter_kind: str | None = None,
    data_dir: Path | None = None,
) -> dict[str, Any]:
    raw_items = manifest_data.get("items", [])
    filtered_items = []

    known_mids = set()
    if unindexed_only:
        dd = data_dir or resolve_data_dir()
        idx_p = resolve_final_index_path(data_dir=dd)
        idx_data = load_final_index(idx_p)
        known_mids = set(idx_data.get("items", {}).keys())

    for item in raw_items:
        dec = item.get("decision", {})
        norm_mid = normalize_message_id(item.get("message_id") or item.get("raw_message_id", ""))

        if needs_reply_only and not dec.get("needs_reply", False):
            continue
        if unindexed_only and norm_mid in known_mids:
            continue
        if filter_kind and dec.get("kind") != filter_kind:
            continue
        filtered_items.append(item)

    # Statistics
    target_counter: Counter[str] = Counter()
    kind_counter: Counter[str] = Counter()
    needs_reply_count = 0
    reply_candidate_count = 0

    for item in raw_items:
        action = item.get("action", {})
        target = action.get("target_folder") or "NONE"
        target_counter[target] += 1

        dec = item.get("decision", {})
        kind = dec.get("kind") or "unclassified"
        kind_counter[kind] += 1

        if dec.get("needs_reply", False):
            needs_reply_count += 1
        if dec.get("reply_candidate"):
            reply_candidate_count += 1

    return {
        "manifest_path": str(manifest_data.get("manifest_path", "")),
        "schema_version": manifest_data.get("schema_version", 1),
        "total_items_in_manifest": len(raw_items),
        "filtered_items_count": len(filtered_items),
        "stats": {
            "by_target_folder": dict(target_counter),
            "by_kind": dict(kind_counter),
            "needs_reply_total": needs_reply_count,
            "reply_candidates_found": reply_candidate_count,
        },
        "items": filtered_items,
    }


def render_human_readable(result: dict[str, Any]) -> str:
    lines = []
    total = result["total_items_in_manifest"]
    filtered_count = result["filtered_items_count"]
    stats = result["stats"]

    lines.append("=" * 80)
    lines.append(f"BATCH MANIFEST INSPECTION ({filtered_count}/{total} Items)")
    lines.append("=" * 80)

    for i, item in enumerate(result["items"], 1):
        env_id = item.get("envelope_id", "?")
        date = item.get("date", "")
        sender = item.get("from", "")
        subj = item.get("subject", "")
        action = item.get("action", {})
        act_type = action.get("type", "none")
        target = action.get("target_folder", "NONE")
        dec = item.get("decision", {})
        kind = dec.get("kind", "unknown")
        dec_id = dec.get("id", "none")
        needs_rep = dec.get("needs_reply", False)
        reply_cand = dec.get("reply_candidate")

        reply_str = "REPLY NEEDED" if needs_rep else "No Reply"
        if reply_cand:
            reply_str += " (Candidate found)"

        lines.append(
            f"[{i:02d}] Env {env_id:>4} | {date[:16]:<16} | {sender[:28]:<28} -> {act_type} -> '{target}'"
        )
        lines.append(f"     Class: {kind}/{dec_id} | Status: {reply_str}")
        lines.append(f"     Subj:  {subj[:75]}")
        if item.get("notes"):
            lines.append(f"     Notes: {item['notes']}")
        lines.append("-" * 80)

    lines.append("\nTARGET FOLDER BREAKDOWN:")
    for folder, count in sorted(stats["by_target_folder"].items(), key=lambda x: -x[1]):
        lines.append(f"  - {folder:<45}: {count:>2} Mails")

    lines.append("\nSUMMARY:")
    lines.append(f"  - Total Mails      : {total}")
    lines.append(f"  - Reply Needed     : {stats['needs_reply_total']}")
    lines.append(f"  - Reply Candidates : {stats['reply_candidates_found']}")
    lines.append("=" * 80)

    return "\n".join(lines)


def main() -> int:
    args = parse_args()
    manifest_path = Path(args.input)

    try:
        input_data = load_manifest(manifest_path)
        is_control_file = "action" in input_data and "manifest_path" in input_data

        target_manifest_path = manifest_path
        if is_control_file:
            target_manifest_path = Path(input_data["manifest_path"]).expanduser().resolve()
            data = load_manifest(target_manifest_path)
            do_reclassify = input_data.get("reclassify", args.reclassify)
            needs_reply_only = input_data.get("needs_reply", args.needs_reply)
            unindexed_only = input_data.get("unindexed", args.unindexed)
            filter_kind = input_data.get("filter_kind", args.filter_kind)
            output_json = input_data.get("json", args.json)
        else:
            data = input_data
            do_reclassify = args.reclassify
            needs_reply_only = args.needs_reply
            unindexed_only = args.unindexed
            filter_kind = args.filter_kind
            output_json = args.json

        if do_reclassify:
            ws_root = target_manifest_path.resolve().parent.parent.parent
            projects, topics = load_catalogs(ws_root)
            reclassified_items = []
            for item in data.get("items", []):
                new_item = classify_email(
                    item,
                    workspace_root=ws_root,
                    projects=projects,
                    topics=topics,
                )
                reclassified_items.append(new_item)
            data["items"] = reclassified_items
            with target_manifest_path.open("w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
                f.write("\n")

        result = inspect_manifest(
            data,
            needs_reply_only=needs_reply_only,
            unindexed_only=unindexed_only,
            filter_kind=filter_kind,
        )

        if is_control_file and input_data.get("delete_input_on_success", True) and manifest_path.exists():
            try:
                manifest_path.unlink()
            except Exception:
                pass

        if output_json:
            envelope = {
                "status": "success",
                "data": result,
                "error": None,
            }
            print(json.dumps(envelope, ensure_ascii=False, indent=2))
        else:
            print(render_human_readable(result))
        return 0

    except Exception as e:
        if args.json:
            envelope = {
                "status": "error",
                "data": None,
                "error": str(e),
            }
            print(json.dumps(envelope, ensure_ascii=False, indent=2))
        else:
            print(f"Error inspecting manifest: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
