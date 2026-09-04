#!/usr/bin/env python3
"""Unified CLI and JSON-manifest client for final-location-index.json."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Any

_script_dir = Path(__file__).resolve().parent
if str(_script_dir) not in sys.path:
    sys.path.insert(0, str(_script_dir))

from core import (
    build_error,
    build_success,
    emit_json,
    load_final_index,
    lookup_final_index,
    normalize_message_id,
    query_final_index,
    resolve_data_dir,
    resolve_final_index_path,
    save_final_index_atomic,
    upsert_final_index_entry,
    upsert_final_index_many,
    utc_now_iso,
)


ACTION = "final_location_index"


def _emit_success(operation: str, message: str, data: dict[str, Any]) -> None:
    emit_json(build_success(ACTION, message, {"operation": operation, **data}))


def _emit_error(
    operation: str,
    message: str,
    exc: Exception | None = None,
    *,
    state: str = "Failed",
    data: dict[str, Any] | None = None,
) -> None:
    emit_json(
        build_error(
            ACTION,
            message,
            {"operation": operation, **(data or {})},
            state=state,
            error_type=type(exc).__name__ if exc else "OperationError",
            error_details={"reason": str(exc)[:1000]} if exc else None,
        )
    )


def get_index_stats(index_path: Path) -> dict[str, Any]:
    """Calculate summary statistics from the final location index."""
    data = load_final_index(index_path)
    items = data.get("items", {})
    folders: dict[str, int] = {}
    backends: dict[str, int] = {}

    for item in items.values():
        folder = item.get("final_folder") or item.get("final_label") or "Unknown"
        folders[folder] = folders.get(folder, 0) + 1
        backend = item.get("backend", "himalaya")
        backends[backend] = backends.get(backend, 0) + 1

    sorted_folders = dict(sorted(folders.items(), key=lambda kv: kv[1], reverse=True))

    file_size_bytes = index_path.stat().st_size if index_path.exists() else 0

    return {
        "index_file": str(index_path),
        "file_size_bytes": file_size_bytes,
        "file_size_kb": round(file_size_bytes / 1024, 1),
        "total_indexed_items": len(items),
        "schema_version": data.get("schema_version", 1),
        "updated_at": data.get("updated_at"),
        "backends": backends,
        "folders_count": len(folders),
        "top_folders": sorted_folders,
    }


def execute_index_manifest(manifest: dict[str, Any], data_dir: Path | None = None) -> dict[str, Any]:
    """Execute a batch of index operations from a JSON manifest."""
    dd = data_dir or resolve_data_dir()
    index_path = resolve_final_index_path(manifest.get("index"), data_dir=dd)

    operations = manifest.get("operations", [])
    if not operations:
        # Fallback to single action at root
        action = manifest.get("action", manifest.get("mode", "stats"))
        operations = [dict(manifest, action=action)]

    results: list[dict[str, Any]] = []
    all_ok = True

    for op in operations:
        action = str(op.get("action", op.get("mode", "stats"))).lower()
        res: dict[str, Any] = {}

        try:
            if action in ("stats", "summary", "count"):
                res = {
                    "ok": True,
                    "action": "stats",
                    "result": get_index_stats(index_path),
                }
            elif action in ("lookup", "get", "find"):
                mid = op.get("message_id") or op.get("mid")
                if not mid:
                    raise ValueError("'message_id' is required for lookup")
                norm_mid = normalize_message_id(str(mid))
                item = lookup_final_index(index_path, norm_mid)
                res = {
                    "ok": True,
                    "action": "lookup",
                    "found": item is not None,
                    "message_id": norm_mid,
                    "item": item,
                }
            elif action in ("query", "filter", "search"):
                folder = op.get("folder")
                query_str = op.get("query") or op.get("q")
                limit = int(op.get("limit", 100))
                q_res = query_final_index(index_path, folder=folder, query=query_str)
                items = q_res.get("items", {})
                total_matches = len(items) if isinstance(items, dict) else len(items)
                if isinstance(items, dict):
                    sliced_items = dict(list(items.items())[:limit])
                else:
                    sliced_items = items[:limit]
                res = {
                    "ok": True,
                    "action": "query",
                    "folder_filter": folder,
                    "query_filter": query_str,
                    "total_matches": total_matches,
                    "returned_count": min(total_matches, limit),
                    "items": sliced_items,
                }
            elif action in ("upsert", "patch"):
                mode = op.get("mode", "upsert-final")
                payload = op.get("payload", op)
                up_res = upsert_final_index_entry(index_path, payload, mode=mode)
                res = {
                    "ok": True,
                    "action": "upsert",
                    "result": up_res,
                }
            elif action in ("batch_import", "batch-import", "import"):
                items = op.get("items", [])
                mode = op.get("mode", "upsert-final")
                imp_res = upsert_final_index_many(index_path, items, mode=mode)
                res = {
                    "ok": True,
                    "action": "batch_import",
                    "result": imp_res,
                }
            else:
                res = {
                    "ok": False,
                    "action": action,
                    "error": f"Unsupported index action: {action}",
                }
                all_ok = False
        except Exception as exc:
            res = {
                "ok": False,
                "action": action,
                "error": str(exc),
            }
            all_ok = False

        results.append(res)

    out = {
        "ok": all_ok,
        "total_operations": len(operations),
        "results": results if len(results) > 1 else results[0],
    }
    return out


def _manifest_output_data(result: dict[str, Any]) -> dict[str, Any]:
    """Translate legacy internal operation results into canonical payload data."""
    raw_results = result.get("results")
    raw_list = raw_results if isinstance(raw_results, list) else [raw_results]
    translated = []
    for raw in raw_list:
        if not isinstance(raw, dict):
            translated.append({"operation": "unknown", "success": False, "data": {}, "error": {"type": "InvalidResult", "message": str(raw)}})
            continue
        error = raw.get("error")
        translated.append(
            {
                "operation": raw.get("action", "unknown"),
                "success": bool(raw.get("ok")),
                "data": {key: value for key, value in raw.items() if key not in {"ok", "action", "error"}},
                "error": None if error is None else {"type": "OperationError", "message": str(error)[:1000]},
            }
        )
    return {
        "total_operations": result.get("total_operations", len(translated)),
        "results": translated if isinstance(raw_results, list) else translated[0],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Unified CLI and JSON manifest client for final-location-index.json")
    parser.add_argument("--input", "-i", help="Path to JSON operation manifest")
    parser.add_argument("--index", help="Path to final-location-index.json")
    parser.add_argument("--json", action="store_true", default=True, help="Output results as JSON (default)")
    parser.add_argument("--keep-input", action="store_true", help="Do not delete input file even if delete_input_on_success is true")

    subparsers = parser.add_subparsers(dest="subcommand", help="Direct subcommands")

    # Stats
    subparsers.add_parser("stats", help="Get index statistics and summary")

    # Lookup
    p_lookup = subparsers.add_parser("lookup", help="Look up a single message by Message-ID")
    p_lookup.add_argument("--mid", "--message-id", required=True, dest="message_id", help="Message-ID")

    # Query
    p_query = subparsers.add_parser("query", help="Query / filter indexed messages")
    p_query.add_argument("--folder", "-f", help="Filter by folder/label")
    p_query.add_argument("--query", "-q", help="Search keyword")
    p_query.add_argument("--limit", "-l", type=int, default=50, help="Max results to return")

    try:
        args = parser.parse_args()
    except SystemExit as exc:
        if exc.code not in (None, 0):
            _emit_error("argument_parse", "Invalid command-line arguments.", ValueError("argparse rejected the arguments"))
            return int(exc.code)
        raise

    data_dir = resolve_data_dir()
    index_path = resolve_final_index_path(args.index, data_dir=data_dir)

    # 1. Manifest file mode
    if args.input:
        in_p = Path(args.input).expanduser().resolve()
        if not in_p.exists():
            _emit_error("manifest", "Input manifest was not found.", FileNotFoundError(str(in_p)))
            return 1
        try:
            with in_p.open("r", encoding="utf-8") as f:
                manifest = json.load(f)
        except Exception as e:
            _emit_error("manifest", "Input manifest is not valid JSON.", e)
            return 1

        out = execute_index_manifest(manifest, data_dir=data_dir)
        output_data = _manifest_output_data(out)

        delete_input = manifest.get("delete_input_on_success", True) and not args.keep_input
        if in_p.exists() and delete_input and out.get("ok"):
            try:
                in_p.unlink()
                out["input_file_deleted"] = True
                output_data["input_file_deleted"] = True
            except Exception as e:
                out["input_file_deleted"] = False
                out["input_file_delete_error"] = str(e)
                output_data["input_file_deleted"] = False
                output_data["input_file_delete_error"] = str(e)[:1000]

        if out.get("ok"):
            _emit_success("manifest", "Final-location index operations completed.", output_data)
            return 0
        _emit_error("manifest", "One or more final-location index operations failed.", data=output_data)
        return 1

    # 2. Subcommand mode
    if args.subcommand == "stats" or not args.subcommand:
        out = get_index_stats(index_path)
        _emit_success("stats", "Final-location index statistics completed.", {"result": out})
        return 0

    if args.subcommand == "lookup":
        norm_mid = normalize_message_id(args.message_id)
        item = lookup_final_index(index_path, norm_mid)
        if item is None:
            _emit_error(
                "lookup",
                "Message ID is not indexed.",
                state="NotFound",
                data={"message_id": norm_mid, "item": None},
            )
            return 2
        _emit_success("lookup", "Final-location index lookup completed.", {"message_id": norm_mid, "item": item})
        return 0

    if args.subcommand == "query":
        q_res = query_final_index(index_path, folder=args.folder, query=args.query)
        items = q_res.get("items", {})
        total_matches = len(items) if isinstance(items, dict) else len(items)
        if isinstance(items, dict):
            sliced_items = dict(list(items.items())[: args.limit])
        else:
            sliced_items = items[: args.limit]
        out = {
            "folder_filter": args.folder,
            "query_filter": args.query,
            "total_matches": total_matches,
            "returned_count": min(total_matches, args.limit),
            "items": sliced_items,
        }
        _emit_success("query", "Final-location index query completed.", {"result": out})
        return 0

    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:  # noqa: BLE001
        _emit_error("unknown", "Final-location index operation failed.", exc)
        raise SystemExit(1)
