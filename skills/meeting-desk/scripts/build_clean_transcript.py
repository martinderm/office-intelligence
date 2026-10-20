#!/usr/bin/env python3
"""Deterministic Raw-to-Clean transcript builder with correction provenance.

Implements the ``meeting-desk`` §2b contract (two-artifact principle with
inline marking): the raw transcript (``*.transcript.raw.md``) stays untouched
as evidence; this tool derives the cleaned version
(``*.transcript.md``) from raw text plus a corrections file, so the cleaned
file is fully reproducible from its two sources and the same metadata options.

Corrections file schema (``meeting-corrections/1``, see
``corrections-template.json``)::

    {
      "schema": "meeting-corrections/1",
      "pairs": [{"orig", "corr", "cat", "ref", "note"}],
      "regex_pairs": [{"pattern", "corr", "cat", "ref", "note"}],
      "rules": [{"id", "pattern", "repl", "note", "ref"}],
      "unklar": [{"token", "guess", "why"}]
    }

* ``pairs``/``regex_pairs`` become inline prose pairs
  ``"orig" → "corr" [korr: cat → ref]`` (entity level only: names,
  abbreviations, IDs, domain terms).
* ``rules`` are silent normalizations applied without an inline pair
  (orthography with no factual content); each rule is listed once in the
  file header with its hit count.
* ``unklar`` tokens are kept verbatim with an ``[unklar: ...]`` marker and
  listed in the header table; nothing is guessed without a workspace anchor.

All operations match the original body; overlapping edits are rejected.
Generated markup is never matched again. The header (frontmatter, Bereinigungsliste, rule table,
open-questions table) is generated from the same data, so counts are
exact. Writes are atomic (tempfile + ``os.replace``); existing outputs are
refused unless ``--force`` is given.

CLI emits one canonical JSON envelope on stdout and exits ``0`` (built or
check clean), ``1`` (``--check`` drift: output differs), ``2``
(input/runtime/schema error).
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import sys
import tempfile
from pathlib import Path
from typing import Any

ACTION = "build_clean_transcript"
SCHEMA = "meeting-corrections/1"

_PAIR_FMT = "\u201e{orig}\u201c \u2192 \u201e{corr}\u201c [korr: {cat} \u2192 {ref}]"
_UNKLAR_FMT = "{token} [unklar: kein Workspace-Beleg]"


def _envelope(payload: dict[str, Any]) -> str:
    """Serialize the canonical CLI envelope (sorted keys, LF)."""
    success = payload.pop("ok")
    message = str(payload.pop("error", "Completed" if success else "Output differs"))[:1000]
    return json.dumps({"action": ACTION, "success": success,
                       "state": "Completed" if success else ("Drift" if payload.get("drift") else "Failed"),
                       "message": message,
                       "data": {k: v for k, v in payload.items() if k != "action"},
                       "error": None if success else {"type": "Drift" if payload.get("drift") else "Error",
                                                       "message": message}},
                      ensure_ascii=False, sort_keys=True)


def _fail(reason: str, **extra: Any) -> int:
    print(_envelope({"ok": False, "action": ACTION, "error": reason, **extra}))
    return 2


def _load_corrections(path: Path) -> tuple[dict[str, Any] | None, str | None]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None, f"corrections file not found: {path}"
    except json.JSONDecodeError as exc:
        return None, f"corrections file is not valid JSON: {exc}"
    if not isinstance(data, dict) or data.get("schema") != SCHEMA:
        return None, f"unsupported schema (need {SCHEMA!r})"
    for key in ("pairs", "regex_pairs", "rules", "unklar"):
        items = data.get(key, [])
        if not isinstance(items, list):
            return None, f"{key!r} must be a list"
        for i, item in enumerate(items):
            if not isinstance(item, dict):
                return None, f"{key}[{i}] must be an object"
            required = {"pairs": ("orig", "corr", "cat", "ref"),
                        "regex_pairs": ("pattern", "corr", "cat", "ref"),
                        "rules": ("id", "pattern", "repl", "note", "ref"),
                        "unklar": ("token", "why")}[key]
            for field in required:
                if not isinstance(item.get(field), str) or (field != "repl" and not item[field].strip()):
                    return None, f"{key}[{i}].{field} must be a non-empty string"
            if any(not isinstance(value, str) for value in item.values()):
                return None, f"{key}[{i}] fields must be strings"
    ids = [r["id"] for r in data.get("rules", [])]
    if len(ids) != len(set(ids)):
        return None, "rule IDs must be unique"
    for i, p in enumerate(data.get("pairs", [])):
        if not p.get("orig") or not p.get("corr"):
            return None, f"pairs[{i}] needs non-empty 'orig' and 'corr'"
    for i, p in enumerate(data.get("regex_pairs", [])):
        if not p.get("pattern") or not p.get("corr"):
            return None, f"regex_pairs[{i}] needs non-empty 'pattern' and 'corr'"
        try:
            re.compile(p["pattern"])
        except re.error as exc:
            return None, f"regex_pairs[{i}] invalid pattern: {exc}"
    for i, r in enumerate(data.get("rules", [])):
        if not r.get("id") or not r.get("pattern") or "repl" not in r:
            return None, f"rules[{i}] needs 'id', 'pattern' and 'repl'"
        try:
            re.compile(r["pattern"])
        except re.error as exc:
            return None, f"rules[{i}] invalid pattern: {exc}"
    for i, u in enumerate(data.get("unklar", [])):
        if not u.get("token"):
            return None, f"unklar[{i}] needs non-empty 'token'"
    return data, None


def _apply(data: dict[str, Any], body: str) -> tuple[str, dict[str, Any]]:
    pair_rows: list[list[Any]] = []
    unklar_rows: list[list[Any]] = []
    rule_hits: dict[str, int] = {}
    rule_meta: dict[str, dict[str, str]] = {}
    pair_count = 0
    unklar_count = 0

    # Match only the untouched source. Render once, after checking all spans.
    # No operation can inspect or rewrite generated prose pairs / evidence links.
    edits: list[tuple[int, int, str]] = []

    def matches(pattern: str) -> list[re.Match[str]]:
        found = list(re.finditer(pattern, body))
        if any(m.start() == m.end() for m in found):
            raise ValueError("zero-width corrections are not allowed")
        return found

    for p in data.get("pairs", []):
        found = matches(re.escape(p["orig"]))
        n = len(found)
        if n:
            edits.extend((m.start(), m.end(), _PAIR_FMT.format(
                orig=m.group(), corr=p["corr"], cat=p["cat"], ref=p["ref"])) for m in found)
            pair_count += n
            pair_rows.append([p["orig"], p["corr"], p.get("cat", ""),
                              p.get("ref", ""), n, p.get("note", "")])
    for p in data.get("regex_pairs", []):
        found = matches(p["pattern"])
        n = len(found)
        if n:
            edits.extend((m.start(), m.end(), _PAIR_FMT.format(
                orig=m.group(), corr=p["corr"], cat=p["cat"], ref=p["ref"])) for m in found)
            pair_count += n
            disp = re.sub(r"\\b|\(\?i\)", "", p["pattern"])
            pair_rows.append([disp, p["corr"], p.get("cat", ""),
                              p.get("ref", ""), n, p.get("note", "")])
    for r in data.get("rules", []):
        found = [(m, m.expand(r["repl"])) for m in matches(r["pattern"])]
        found = [(m, replacement) for m, replacement in found if m.group() != replacement]
        n = len(found)
        edits.extend((m.start(), m.end(), replacement) for m, replacement in found)
        rule_hits[r["id"]] = rule_hits.get(r["id"], 0) + n
        rule_meta[r["id"]] = {"note": r.get("note", ""), "ref": r.get("ref", "") or "-"}
    for u in data.get("unklar", []):
        found = matches(re.escape(u["token"]))
        n = len(found)
        if n:
            edits.extend((m.start(), m.end(), _UNKLAR_FMT.format(token=m.group())) for m in found)
            unklar_count += n
            unklar_rows.append([u["token"], u.get("guess", ""), n, u.get("why", "")])

    chunks, end = [], 0
    for start, stop, replacement in sorted(edits):
        if start < end:
            raise ValueError("overlapping corrections: keep exactly one operation per source span")
        chunks.extend((body[end:start], replacement))
        end = stop
    chunks.append(body[end:])
    body = "".join(chunks)

    stats = {"corrections_count": pair_count, "unklar_count": unklar_count,
             "rules": rule_hits, "pairs": len(pair_rows),
             "unklar_types": len(unklar_rows)}
    tables = {"pair_rows": pair_rows, "unklar_rows": unklar_rows,
              "rule_meta": rule_meta}
    return body, {"stats": stats, "tables": tables}


def _header(meeting_id: str, title: str, date: str, datestring: str,
            raw_name: str, result: dict[str, Any]) -> str:
    stats = result["stats"]
    tables = result["tables"]
    out = [
        "---",
        f'meeting_id: {json.dumps(meeting_id, ensure_ascii=False)}',
        f'title: {json.dumps(title, ensure_ascii=False)}',
        f'date: {int(date) if date else "null"}',
        f'dateString: {json.dumps(datestring, ensure_ascii=False)}',
        f'source_raw: {json.dumps(raw_name, ensure_ascii=False)}',
        f'corrections_count: {stats["corrections_count"]}',
        f'unklar_count: {stats["unklar_count"]}',
        "provenance: meeting-desk §2b (Zwei-Artefakt-Prinzip + Inline-Markierung)",
        "---",
        "",
        "# Bereinigungsliste (Original → Korrektur, mit Beleg)",
        "",
        "| # | Original | Korrektur | Kategorie | Beleg | n | Notiz |",
        "|---|----------|-----------|-----------|-------|---|-------|",
    ]
    for i, (o, c, k, r, n, no) in enumerate(tables["pair_rows"], 1):
        out.append(_table_row([i, o, c, k, r, n, no]))
    out += ["", "# Normalisierungsregeln (stillschweigend, regelhaft, ohne Inline-Paar)",
            "", "| Regel | Beschreibung | Beleg | n |",
            "|-------|--------------|-------|---|"]
    for rid in sorted(tables["rule_meta"], key=lambda c: (c != "R1", c)):
        meta = tables["rule_meta"][rid]
        out.append(_table_row([rid, meta['note'], meta['ref'], result['stats']['rules'].get(rid, 0)]))
    out += ["", "# Offene Stellen (Original bleibt — kein Workspace-Beleg)", "",
            "| Token (Belegstelle sinngemäß) | Vermutung | n | Warum kein Beleg |",
            "|------------------------------|-----------|---|------------------|"]
    for tok, guess, n, why in tables["unklar_rows"]:
        out.append(_table_row([tok, guess, n, why]))
    out += ["", "---", ""]
    return "\n".join(out) + "\n"


def _table_row(values: list[Any]) -> str:
    def cell(value: Any) -> str:
        return (str(value).replace("&", "&amp;").replace("<", "&lt;")
                .replace(">", "&gt;").replace("\\", "&#92;").replace("|", "&#124;")
                .replace("\r", "&#13;").replace("\n", "&#10;"))
    return "| " + " | ".join(cell(v) for v in values) + " |"


def _workspace_path(workspace: Path, path: Path) -> Path:
    target = path.resolve()
    zones = ("memory/references", "memory/evidence", "memory/cloud", "data")
    if not any(target.is_relative_to((workspace / zone).resolve())
               and (workspace / zone).resolve().is_relative_to(workspace) for zone in zones):
        raise ValueError("path must be inside a canonical workspace data zone")
    bundle = Path(__file__).resolve().parents[3]
    if target.is_relative_to(bundle):
        raise ValueError("runtime writes into the skill bundle are forbidden")
    return target


def _require_lock(workspace: Path, lease_id: str | None, conversation_id: str | None) -> None:
    # Load the canonical shared guard in place; never copy lock machinery.
    for parent in Path(__file__).resolve().parents:
        guard = parent / "workspace-lock/scripts/workspace_lock_guard.py"
        if guard.is_file():
            spec = importlib.util.spec_from_file_location("_meeting_workspace_lock_guard", guard)
            module = importlib.util.module_from_spec(spec)
            sys.modules[spec.name] = module
            spec.loader.exec_module(module)
            module.require_workspace_lock(workspace, lease_id=lease_id, conversation_id=conversation_id)
            return
    raise RuntimeError("canonical workspace-lock guard not found beside the skills bundle")


def _validate_refs(data: dict[str, Any], workspace: Path) -> None:
    for key in ("pairs", "regex_pairs", "rules"):
        for item in data.get(key, []):
            ref = item["ref"].split("#", 1)[0]
            path = (workspace / ref).resolve()
            if (not ref.startswith("memory/references/") or not path.is_relative_to(workspace / "memory/references")
                    or not path.is_file()):
                raise ValueError(f"{key}: ref needs an existing memory/references/ workspace document")


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".",
                               suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _template() -> dict[str, Any]:
    return json.loads(Path(__file__).with_name("corrections-template.json").read_text(encoding="utf-8"))


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise ValueError(message)


def _run(argv: list[str] | None = None) -> int:
    ap = _Parser(description=__doc__.splitlines()[0])
    ap.add_argument("--raw", help="raw transcript (*.transcript.raw.md)")
    ap.add_argument("--corrections", help="corrections JSON (meeting-corrections/1)")
    ap.add_argument("--out", help="cleaned transcript output (*.transcript.md)")
    ap.add_argument("--meeting-id", default="")
    ap.add_argument("--title", default="")
    ap.add_argument("--date", default="")
    ap.add_argument("--date-string", default="")
    ap.add_argument("--check", action="store_true",
                    help="verify-only: rebuild in memory and byte-compare with --out")
    ap.add_argument("--init-template", metavar="PATH",
                    help="write a template corrections file and exit")
    ap.add_argument("--force", action="store_true",
                     help="overwrite an existing --out file")
    ap.add_argument("--workspace", required=True, help="consuming workspace root")
    ap.add_argument("--lease-id", help="owned workspace-lock lease ID (writes only)")
    ap.add_argument("--conversation-id", help="owned workspace-lock conversation ID (writes only)")
    ap.add_argument("--json", action="store_true", help="canonical JSON envelope (also the default)")
    args = ap.parse_args(argv)
    workspace = Path(args.workspace).resolve()

    if args.init_template:
        if args.raw or args.corrections or args.out or args.check:
            raise ValueError("--init-template cannot be combined with build/check arguments")
        target = _workspace_path(workspace, Path(args.init_template))
        if target.suffix != ".json":
            raise ValueError("template output must be a .json file")
        if target.exists() and not args.force:
            return _fail(f"refusing to overwrite existing file: {target}")
        _require_lock(workspace, args.lease_id, args.conversation_id)
        _atomic_write(target, json.dumps(_template(), ensure_ascii=False, indent=2) + "\n")
        print(_envelope({"ok": True, "action": ACTION, "mode": "init-template",
                         "path": target.as_posix()}))
        return 0

    if not args.raw or not args.corrections or not args.out:
        return _fail("--raw, --corrections and --out are required")
    raw_path, corr_path, out_path = Path(args.raw), Path(args.corrections), Path(args.out)
    raw_path = _workspace_path(workspace, raw_path)
    corr_path = _workspace_path(workspace, corr_path)
    out_path = _workspace_path(workspace, out_path)
    for source in (raw_path, corr_path):
        if out_path == source or (out_path.exists() and source.exists() and out_path.samefile(source)):
            raise ValueError("output must not alias the raw transcript or corrections file")
    try:
        raw_text = raw_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return _fail(f"raw transcript not found: {raw_path}")
    except OSError as exc:
        return _fail(f"cannot read raw transcript: {exc}")
    data, err = _load_corrections(corr_path)
    if err is not None:
        return _fail(err)
    _validate_refs(data, workspace)

    frontmatter = re.match(r"\A\ufeff?---\n.*?\n---(?:\n|\Z)", raw_text, re.S)
    body = raw_text[frontmatter.end():] if frontmatter else raw_text
    new_body, result = _apply(data, body)
    header = _header(args.meeting_id or raw_path.stem.replace(".transcript.raw", ""),
                     args.title, args.date, args.date_string,
                     raw_path.relative_to(workspace).as_posix(), result)
    cleaned = header + new_body
    stats = result["stats"]

    if args.check:
        try:
            current = out_path.read_bytes()
        except FileNotFoundError:
            return _fail(f"check target not found: {out_path}")
        except OSError as exc:
            return _fail(f"cannot read check target: {exc}")
        if current == cleaned.encode("utf-8"):
            print(_envelope({"ok": True, "action": ACTION, "mode": "check",
                             "drift": False, **stats}))
            return 0
        print(_envelope({"ok": False, "action": ACTION, "mode": "check",
                         "drift": True, **stats}))
        return 1

    if out_path.exists() and not args.force:
        return _fail(f"refusing to overwrite existing file (use --force): {out_path}")
    _require_lock(workspace, args.lease_id, args.conversation_id)
    try:
        _atomic_write(out_path, cleaned)
    except OSError as exc:
        return _fail(f"cannot write output: {exc}")
    print(_envelope({"ok": True, "action": ACTION, "mode": "build",
                     "out": out_path.as_posix(), **stats}))
    return 0


def main(argv: list[str] | None = None) -> int:
    try:
        return _run(argv)
    except (OSError, UnicodeError, ValueError, RuntimeError, re.error, IndexError) as exc:
        return _fail(str(exc))


if __name__ == "__main__":
    sys.exit(main())
