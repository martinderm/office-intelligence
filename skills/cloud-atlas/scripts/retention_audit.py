#!/usr/bin/env python3
"""Read-only retention audit for cloud-atlas version roots (Issue #97).

This command classifies existing ``<output_dir>/_versions/`` directories so an
operator can see which versions are active, still inside the retention policy,
or logical quarantine candidates.  It does not lock, create, move, rename,
archive or delete anything; the target filemap is the only reference source.
"""

import argparse
import datetime
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from core.retention import (  # noqa: E402
    DEFAULT_POLICY, RetentionError, classify_versions,
)

ACTION = "retention_audit"
COMPLETED = "Completed"
FAILED = "Failed"


class CliArgumentError(ValueError):
    """An argument error that can be represented by the JSON contract."""


class ArgumentParser(argparse.ArgumentParser):
    def error(self, message):
        raise CliArgumentError(message)


def parse_args(argv=None):
    parser = ArgumentParser(
        description="Klassifiziert versionierte cloud-atlas-Ziele (nur lesend, keine Loeschung/Verschiebung)."
    )
    parser.add_argument("--workspace-root", help="Expliziter Pfad zum Workspace-Root")
    parser.add_argument("--output-dir", required=True, help="Workspace-relativer Output-Ordner mit _versions/")
    parser.add_argument("--filemap", required=True, help="Workspace-relative Ziel-Filemap (aktive Referenz)")
    parser.add_argument("--now", help="Optionaler RFC-3339-Zeitpunkt fuer reproduzierbare Audits")
    parser.add_argument("--json", action="store_true", help="Gibt einen kanonischen Structured-CLI-Envelope aus")
    return parser.parse_args(argv)


def envelope(success, state, message, data, error=None):
    return {
        "action": ACTION,
        "success": success,
        "state": state,
        "message": message,
        "data": data,
        "error": error,
    }


def emit_json(result):
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))


def _now(value):
    if value is None:
        return None
    try:
        parsed = datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise CliArgumentError(f"--now is not an RFC-3339 timestamp: {value!r}") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=datetime.timezone.utc)
    return parsed


def _summary(report):
    counts = report["counts"]
    identity = report["identity"]
    authoritative = identity["classification_authoritative"]
    lines = [
        f"Retention-Policy {report['policy']['id']} (nur lesend)",
        f"Output: {report['output_dir']}",
        f"Version-Root: {report['version_root']}"
        + ("" if report["version_root_present"] else " (nicht vorhanden)"),
        (
            "Identität: "
            + (
                "verifiziert über Katalog"
                if authoritative else
                f"UNVERIFIZIERT ({identity['status']}); Klassifikation nicht autoritativ"
            )
        ),
        (
            "Versionen: "
            f"{sum(counts.values())} gesamt, {counts['active']} aktiv, "
            f"{counts['retained']} retained, {counts['quarantine_candidate']} "
            f"Prüfhinweise, {counts['unresolved']} unaufgelöst"
        ),
        f"Bekannte Versionsbytes: {report['bytes']['known_version_bytes']}",
        (
            "Kein Eintrag ist zum Loeschen oder Verschieben freigegeben; "
            "fremde Referenzen sind unbekannt."
        ),
    ]
    unresolved = report["references"]["unresolved"]
    if unresolved:
        lines.append("Unaufgelöste aktive Referenzen (geschützt, nicht als Kandidat markiert):")
        lines.extend(f"  {reference}" for reference in unresolved)
    for entry in report["entries"]:
        if entry["classification"] == "unresolved":
            lines.append(
                f"  Unaufgelöst/potenziell aktiv: {entry['path']} "
                f"({', '.join(entry['reasons'])})"
            )
        elif entry["classification"] == "quarantine_candidate":
            label = "Kandidat" if authoritative else "Prüfhinweis (nicht autoritativ)"
            lines.append(f"  {label}: {entry['path']} ({', '.join(entry['reasons'])})")
    return "\n".join(lines)


def main(argv=None):
    args = None
    raw_arguments = list(argv) if argv is not None else sys.argv[1:]
    json_requested = "--json" in raw_arguments
    try:
        args = parse_args(raw_arguments)
        workspace_root = args.workspace_root or str(Path.cwd())
        report = classify_versions(
            workspace_root, args.output_dir, args.filemap,
            now=_now(args.now), policy=DEFAULT_POLICY,
        )
        result = envelope(
            True, COMPLETED,
            "Retention-Klassifikation abgeschlossen (nur lesend, keine Aktion)",
            {"retention": report},
        )
        if args.json:
            emit_json(result)
        else:
            print(_summary(report))
        return 0
    except (CliArgumentError, RetentionError) as exc:
        phase = "arguments" if isinstance(exc, CliArgumentError) else "retention"
        result = envelope(
            False, FAILED, str(exc), {},
            {"phase": phase, "type": type(exc).__name__, "message": str(exc)},
        )
        if json_requested or (args is not None and args.json):
            emit_json(result)
        else:
            print(f"Retention-Audit abgebrochen ({phase}): {exc}", file=sys.stderr)
        return 2 if phase == "arguments" else 1


if __name__ == "__main__":
    sys.exit(main())
