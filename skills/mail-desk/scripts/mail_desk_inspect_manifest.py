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


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Inspect and summarize mail-desk batch manifests."
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
    filter_kind: str | None = None,
) -> dict[str, Any]:
    raw_items = manifest_data.get("items", [])
    filtered_items = []

    for item in raw_items:
        dec = item.get("decision", {})
        if needs_reply_only and not dec.get("needs_reply", False):
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
        data = load_manifest(manifest_path)
        data["manifest_path"] = str(manifest_path)
        result = inspect_manifest(
            data,
            needs_reply_only=args.needs_reply,
            filter_kind=args.filter_kind,
        )

        if args.json:
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
