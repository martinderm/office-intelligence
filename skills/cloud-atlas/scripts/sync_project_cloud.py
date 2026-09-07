#!/usr/bin/env python3
"""Run the cloud converter and filemap generator as one ordered sync."""

import argparse
import json
import subprocess
import sys
from pathlib import Path


ACTION = "sync_project_cloud"
COMPLETED = "Completed"
FAILED = "Failed"


class CliArgumentError(ValueError):
    """An argument error that can be represented by the JSON contract."""


class ArgumentParser(argparse.ArgumentParser):
    def error(self, message):
        raise CliArgumentError(message)


def parse_args():
    parser = ArgumentParser(
        description="Synchronisiert den Cloud-Speicher eines Projekts oder Themas (Konvertierung + Filemap)."
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--project-id", help="Projekt-ID (z. B. meshe)")
    group.add_argument("--topic-id", help="Topic-ID (z. B. lifelong-learning)")
    parser.add_argument("--force", action="store_true", help="Alle Konvertierungen erzwingen")
    parser.add_argument("--topic", action="store_true", help="Erzwinge die Behandlung als Topic")
    parser.add_argument("--storage-id", help="Optionale Storage-ID bei mehreren Cloud-Speichern")
    parser.add_argument("--workspace-root", help="Expliziter Pfad zum Workspace-Root")
    parser.add_argument("--file-timeout", type=int, default=60, help="Maximales Timeout pro Dateikonvertierung in Sekunden (Standard: 60)")
    parser.add_argument("--jobs", "-j", type=int, default=2, help="Anzahl paralleler Konvertierungs-Jobs (Standard: 2)")
    parser.add_argument("--no-ocr", action="store_true", help="Deaktiviere automatisches OCR-Fallback fuer rein bildbasierte PDFs")
    parser.add_argument("--ocr-policy", choices=["enrich_source", "local_derivative", "disabled"], default="local_derivative", help="OCR-Policy fuer PDFs")
    parser.add_argument("--redo-ocr", action="store_true", help="Erzwinge die Neuerstellung bestehender OCR-Ebenen")
    parser.add_argument("--json", action="store_true", help="Gibt einen kanonischen Structured-CLI-Envelope aus")
    return parser.parse_args()


def envelope(success, state, message, data, error=None):
    """Return the canonical, single-source-of-truth CLI result."""
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


def command_data(target_kind, target_id, completed_steps):
    return {
        "target": {"kind": target_kind, "id": target_id},
        "completed_steps": completed_steps,
    }


def run_step(step, command, json_mode):
    """Run one child command and avoid any child output on JSON stdout."""
    try:
        if json_mode:
            result = subprocess.run(command, capture_output=True, text=True)
        else:
            result = subprocess.run(command)
    except OSError as exc:
        return None, {
            "step": step,
            "exit_code": None,
            "type": type(exc).__name__,
            "message": str(exc),
        }

    if result.returncode != 0:
        error = {
            "step": step,
            "exit_code": result.returncode,
            "type": "ChildProcessError",
            "message": f"Der Schritt '{step}' wurde mit Exitcode {result.returncode} beendet.",
        }
        if json_mode:
            try:
                child = json.loads(result.stdout or "")
            except (TypeError, ValueError):
                child = None
            if isinstance(child, dict) and child.get("state") == "ConversionRequired":
                error["type"] = "ConversionRequired"
                error["message"] = child.get("message") or error["message"]
                child_error = child.get("error")
                error["details"] = child_error
                error["requirements"] = (
                    child_error.get("requirements", [])
                    if isinstance(child_error, dict)
                    else []
                )
        return None, error
    return result, None


def main():
    try:
        args = parse_args()
    except CliArgumentError as exc:
        if "--json" in sys.argv[1:]:
            emit_json(
                envelope(
                    False,
                    FAILED,
                    "Cloud-Synchronisation konnte nicht gestartet werden.",
                    {"target": None, "completed_steps": []},
                    {
                        "step": "arguments",
                        "exit_code": 2,
                        "type": type(exc).__name__,
                        "message": str(exc),
                    },
                )
            )
        else:
            print(f"Fehlerhafte Argumente: {exc}", file=sys.stderr)
        return 2
    scripts_dir = Path(__file__).resolve().parent
    target_flag = "--project-id" if args.project_id else "--topic-id"
    target_kind = "project" if args.project_id else "topic"
    target_id = args.project_id or args.topic_id
    completed_steps = []
    conversion_required_error = None

    convert_command = [
        sys.executable,
        str(scripts_dir / "convert_cloud_docs.py"),
        target_flag,
        target_id,
        "--file-timeout",
        str(args.file_timeout),
        "--jobs",
        str(args.jobs),
    ]
    if args.workspace_root:
        convert_command.extend(["--workspace-root", args.workspace_root])
    if args.force:
        convert_command.append("--force")
    if args.topic:
        convert_command.append("--topic")
    if args.storage_id:
        convert_command.extend(["--storage-id", args.storage_id])
    if args.no_ocr:
        convert_command.append("--no-ocr")
    else:
        convert_command.extend(["--ocr-policy", args.ocr_policy])
    if args.redo_ocr:
        convert_command.append("--redo-ocr")
    if args.json:
        convert_command.append("--json")

    if not args.json:
        print(f"=== Schritt 1: Konvertiere Cloud-Dokumente fuer ID '{target_id}' ===")
    _, error = run_step("convert", convert_command, args.json)
    if error:
        if error["type"] == "ConversionRequired":
            # The converter has written the catalog state.  Generate the filemap
            # before exposing the deferred, non-successful overall outcome.
            conversion_required_error = error
        else:
            if args.json:
                emit_json(envelope(False, FAILED, "Cloud-Synchronisation fehlgeschlagen.", command_data(target_kind, target_id, completed_steps), error))
            else:
                print("Fehler beim Konvertieren der Cloud-Dokumente. Abbruch.")
            return error["exit_code"] if error["exit_code"] is not None else 1
    completed_steps.append("convert")

    filemap_command = [sys.executable, str(scripts_dir / "gen_filemap.py"), target_flag, target_id]
    if args.workspace_root:
        filemap_command.extend(["--workspace-root", args.workspace_root])
    if args.topic:
        filemap_command.append("--topic")
    if args.storage_id:
        filemap_command.extend(["--storage-id", args.storage_id])
    if args.json:
        filemap_command.append("--json")

    if not args.json:
        print(f"\n=== Schritt 2: Generiere Filemap fuer ID '{target_id}' ===")
    _, error = run_step("filemap", filemap_command, args.json)
    if error:
        if args.json:
            emit_json(envelope(False, FAILED, "Cloud-Synchronisation fehlgeschlagen.", command_data(target_kind, target_id, completed_steps), error))
        else:
            print("Fehler beim Generieren der Filemap.")
        return error["exit_code"] if error["exit_code"] is not None else 1
    completed_steps.append("filemap")

    if conversion_required_error:
        if args.json:
            emit_json(envelope(
                False,
                "ConversionRequired",
                conversion_required_error["message"],
                command_data(target_kind, target_id, completed_steps),
                conversion_required_error,
            ))
        else:
            print("Cloud-Synchronisation abgeschlossen; mindestens eine Datei benötigt eine optionale Konvertierungsfähigkeit.")
        return conversion_required_error["exit_code"] if conversion_required_error["exit_code"] is not None else 1

    if args.json:
        emit_json(envelope(True, COMPLETED, "Cloud-Synchronisation erfolgreich abgeschlossen.", command_data(target_kind, target_id, completed_steps)))
    else:
        print("\n=== Cloud-Synchronisation erfolgreich abgeschlossen! ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
