"""Read-only inspector for the mail-desk reference catalogs of a consumer workspace.

Read-side companion to ``catalog_validator.py`` (MD-S5): prints selected fields of
``topics.json``, ``projects.json`` or ``mail-desk.json`` as a canonical envelope.
Run from the consumer workspace root.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Iterable
from pathlib import Path

from core.envelope import build_error, build_success, emit_json

CATALOG_PATHS = {
    "topics": "memory/references/topics/topics.json",
    "projects": "memory/references/projects/projects.json",
    "mail-desk": "memory/references/mail-desk/mail-desk.json",
}

DEFAULT_FIELDS = ("id", "title", "mailbox_folder", "domains", "typical_subject_patterns")


class CatalogInspectError(Exception):
    """Raised when a catalog payload cannot be interpreted as an item list."""


def _extract_items(catalog: str, data: object) -> list[dict]:
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in ("topics", "projects", "items"):
            if isinstance(data.get(key), list):
                return data[key]
    raise CatalogInspectError(f"cannot find item list in {catalog} catalog")


def _select_entry(entry: dict, fields: Iterable[str]) -> dict:
    """Return ``{id}`` plus only the selected fields present in ``entry``.

    Mirrors the list-summary projection so single-entry (``--id``) output stays
    bounded: verbose fields the caller did not ask for (e.g. ``workpackages``)
    are never emitted.
    """
    selected: dict = {"id": entry.get("id")}
    selected.update({field: entry.get(field) for field in fields if field != "id" and field in entry})
    return selected


def _load_catalog(catalog: str, catalog_path: Path) -> object:
    """Read and parse a catalog file, surfacing failures as ``CatalogInspectError``.

    Covers both unreadable files and malformed JSON so ``main`` can emit the
    canonical ``CatalogError`` envelope instead of a traceback.
    """
    try:
        return json.loads(catalog_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CatalogInspectError(f"cannot read {catalog} catalog: {exc}") from exc


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("catalog", choices=sorted(CATALOG_PATHS))
    parser.add_argument("--workspace", default=".", help="workspace root (default: current directory)")
    parser.add_argument("--id", help="select a single entry by id (mail-desk prints the whole file)")
    parser.add_argument("--fields", help="comma-separated fields (default: id,title,mailbox_folder,domains,typical_subject_patterns)")
    args = parser.parse_args()

    workspace = Path(args.workspace).resolve()
    catalog_path = workspace / CATALOG_PATHS[args.catalog]
    if not catalog_path.exists():
        emit_json(build_error("catalog_inspect", "Catalog file not found.", {"catalog": args.catalog, "path": str(catalog_path)}, error_type="NotFound"))
        return 1

    try:
        data = _load_catalog(args.catalog, catalog_path)
        if args.catalog != "mail-desk":
            items = _extract_items(args.catalog, data)
    except CatalogInspectError as exc:
        emit_json(build_error("catalog_inspect", str(exc), {"catalog": args.catalog}, error_type="CatalogError"))
        return 2

    if args.catalog == "mail-desk":
        emit_json(build_success("catalog_inspect", "Desk-signals catalog.", {"catalog": args.catalog, "content": data}))
        return 0

    fields = [f.strip() for f in args.fields.split(",")] if args.fields else list(DEFAULT_FIELDS)

    if args.id:
        selected = [item for item in items if item.get("id") == args.id]
        if not selected:
            emit_json(build_error(
                "catalog_inspect", "Entry id not found.",
                {"catalog": args.catalog, "id": args.id},
                error_type="NotFound",
            ))
            return 1
        emit_json(build_success("catalog_inspect", "Entry loaded.", {"catalog": args.catalog, "entry": _select_entry(selected[0], fields)}))
        return 0

    entries = [_select_entry(item, fields) for item in items]
    emit_json(build_success("catalog_inspect", "Catalog summary.", {"catalog": args.catalog, "entries": entries}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
