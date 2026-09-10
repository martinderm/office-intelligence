import ast
import os
import sys
import datetime
import re
import json
import argparse
import hashlib
import tempfile
import contextlib
import io
from pathlib import Path, PurePosixPath
from urllib.parse import quote

try:
    from core.metadata import (
        CANONICAL_CLOUD_METADATA_KEYS,
        validate_cloud_artifact_metadata,
    )
    from core.curation import load_curation_overlay, merge_curated_metadata
    from core.mirror_paths import (
        explicit_custom_mirror_path, legacy_canonical_mirror_path,
        plan_markdown_mirrors,
    )
except ModuleNotFoundError:
    # Keep direct imports from a test loader portable while preserving the
    # standalone script's bundled ``scripts/core`` import path.
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from core.metadata import (
        CANONICAL_CLOUD_METADATA_KEYS,
        validate_cloud_artifact_metadata,
    )
    from core.curation import load_curation_overlay, merge_curated_metadata
    from core.mirror_paths import (
        explicit_custom_mirror_path, legacy_canonical_mirror_path,
        plan_markdown_mirrors,
    )


VALID_OCR_POLICIES = {"enrich_source", "local_derivative", "disabled"}
FILEMAP_SCHEMA_VERSION = 1
FILEMAP_SCHEMA_URI = "https://raw.githubusercontent.com/martinderm/office-intelligence/main/skills/cloud-atlas/references/filemap.schema.json"
CANONICAL_ARTIFACT_METADATA_KEYS = CANONICAL_CLOUD_METADATA_KEYS
ACTION = "gen_filemap"
COMPLETED = "Completed"
FAILED = "Failed"
MAX_ENVELOPE_MESSAGE_LENGTH = 1000


class CliArgumentError(ValueError):
    """An argument error that can be represented by the JSON contract."""


class GenerationError(Exception):
    """A generation failure with a stable processing phase and storage context."""

    def __init__(self, phase, storage_id, cause, storage_results=None, partial_storage=None):
        super().__init__(str(cause))
        self.phase = phase
        self.storage_id = storage_id
        self.cause = cause
        self.storage_results = storage_results or []
        self.partial_storage = partial_storage


class ArgumentParser(argparse.ArgumentParser):
    def error(self, message):
        raise CliArgumentError(message)


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


def bounded_message(message):
    """Keep envelope diagnostics useful without embedding unbounded output."""
    text = str(message)
    if len(text) <= MAX_ENVELOPE_MESSAGE_LENGTH:
        return text
    return text[:MAX_ENVELOPE_MESSAGE_LENGTH - 3] + "..."


def atomic_write_text(filepath, content):
    """Atomically replace a UTF-8 text file using a flushed sibling temporary file."""
    target_path = Path(os.path.abspath(filepath))
    target_path.parent.mkdir(parents=True, exist_ok=True)
    temp_fd, temp_name = tempfile.mkstemp(
        prefix=f".{target_path.name}.",
        suffix=".tmp",
        dir=str(target_path.parent),
    )
    try:
        with os.fdopen(temp_fd, "w", encoding="utf-8", newline="\n") as temp_file:
            temp_fd = None
            temp_file.write(content)
            temp_file.flush()
            os.fsync(temp_file.fileno())
        os.replace(temp_name, target_path)
    finally:
        if temp_fd is not None:
            try:
                os.close(temp_fd)
            except OSError:
                pass
        try:
            os.unlink(temp_name)
        except OSError:
            pass


def write_json_file(filepath, data):
    """Serialize JSON and atomically replace the destination file."""
    atomic_write_text(filepath, json.dumps(data, indent=2, ensure_ascii=False))


def normalize_workspace_relative_path(path_value):
    """Return a safe, normalized workspace-relative POSIX path or None."""
    if not isinstance(path_value, str) or not path_value.strip():
        return None
    raw = path_value.strip().replace("\\", "/")
    if raw.startswith("/") or raw.startswith("//") or re.match(r"^[A-Za-z]:/", raw):
        return None
    normalized = PurePosixPath(raw)
    if ".." in normalized.parts:
        return None
    return normalized.as_posix()


def _read_mirror_artifact_metadata(workspace_root, mirror_path):
    """Read canonical scalar frontmatter; return None for an unchanged legacy mirror."""
    mirror_abs = Path(workspace_root) / Path(*PurePosixPath(mirror_path).parts)
    if not mirror_abs.is_file():
        return None
    try:
        content = mirror_abs.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        raise ValueError(f"Could not read mirror metadata {mirror_path}: {exc}") from exc
    if not content.startswith("---"):
        return None
    parts = content.split("---", 2)
    if len(parts) < 3:
        return None

    metadata = {}
    for line in parts[1].strip().splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if value.lower() in {"true", "false"}:
            metadata[key] = value.lower() == "true"
        elif value.lower() == "null":
            metadata[key] = None
        elif value.startswith("[") and value.endswith("]"):
            try:
                metadata[key] = json.loads(value)
            except json.JSONDecodeError:
                try:
                    metadata[key] = ast.literal_eval(value)
                except (ValueError, SyntaxError):
                    metadata[key] = value
        else:
            metadata[key] = value

    if not (set(metadata) & CANONICAL_ARTIFACT_METADATA_KEYS):
        return None
    return metadata


def _read_mirror_payload(workspace_root, mirror_path):
    """Return the exact Markdown payload represented by artifact_sha256."""
    mirror_abs = Path(workspace_root) / Path(*PurePosixPath(mirror_path).parts)
    try:
        content = mirror_abs.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        raise ValueError(f"Could not read mirror payload {mirror_path}: {exc}") from exc
    parts = content.split("---", 2)
    if len(parts) < 3:
        return content
    body = parts[2]
    if body.startswith("\r\n\r\n"):
        return body[4:]
    if body.startswith("\n\n"):
        return body[2:]
    return body


def validate_filemap(filemap, workspace_root=None):
    """Fail closed unless a generated filemap satisfies its container contract."""
    workspace_root = workspace_root or os.getcwd()
    if not isinstance(filemap, dict):
        raise ValueError("filemap must be an object")

    required = {
        "$schema", "schema_version", "kind", "scope", "storage_id", "project",
        "project_title", "scan_dir", "output_dir", "updated_at", "files",
    }
    missing = sorted(required - set(filemap))
    if missing:
        raise ValueError(f"filemap is missing required keys: {missing}")
    unknown = sorted(set(filemap) - required)
    if unknown:
        raise ValueError(f"filemap has unknown container keys: {unknown}")
    if filemap["$schema"] != FILEMAP_SCHEMA_URI:
        raise ValueError("filemap.$schema does not identify the bundled Filemap schema")
    if filemap["schema_version"] != FILEMAP_SCHEMA_VERSION:
        raise ValueError(f"filemap.schema_version must be {FILEMAP_SCHEMA_VERSION}")
    if filemap["kind"] != "cloud-filemap":
        raise ValueError("filemap.kind must be 'cloud-filemap'")
    if not isinstance(filemap["scope"], str) or filemap["scope"] not in {"project", "topic"}:
        raise ValueError("filemap.scope is invalid")
    if not isinstance(filemap["storage_id"], str) or re.fullmatch(
        r"[A-Za-z0-9][A-Za-z0-9._-]*", filemap["storage_id"]
    ) is None:
        raise ValueError("filemap.storage_id is invalid")
    for field in ("project", "project_title"):
        if not isinstance(filemap[field], str) or not filemap[field].strip():
            raise ValueError(f"filemap.{field} must be a non-empty string")
    for field in ("updated_at",):
        if not isinstance(filemap[field], str) or re.fullmatch(
            r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}", filemap[field]
        ) is None:
            raise ValueError(f"filemap.{field} has an invalid timestamp")

    scan_dir = normalize_workspace_relative_path(filemap["scan_dir"])
    output_dir = normalize_workspace_relative_path(filemap["output_dir"])
    if not scan_dir or not output_dir:
        raise ValueError("filemap scan_dir and output_dir must be workspace-relative paths")
    files = filemap["files"]
    if not isinstance(files, dict):
        raise ValueError("filemap.files must be an object")

    required_entry = {"version", "mtime", "size", "sha256", "description"}
    valid_statuses = {"converted", "conversion_required"}
    valid_ocr_policies = {"enrich_source", "local_derivative", "disabled"}
    seen_mirrors = {}
    for source_path, entry in files.items():
        normalized_source = normalize_workspace_relative_path(source_path)
        if not normalized_source or not path_is_within(normalized_source, scan_dir):
            raise ValueError(f"filemap.files contains a source outside scan_dir: {source_path!r}")
        if not isinstance(entry, dict):
            raise ValueError(f"filemap.files[{source_path!r}] must be an object")
        missing_entry = sorted(required_entry - set(entry))
        if missing_entry:
            raise ValueError(f"filemap.files[{source_path!r}] is missing keys: {missing_entry}")
        if not isinstance(entry["version"], str) or not isinstance(entry["description"], str):
            raise ValueError(f"filemap.files[{source_path!r}] has invalid text fields")
        for field in ("mtime",):
            if not isinstance(entry[field], str) or re.fullmatch(
                r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}", entry[field]
            ) is None:
                raise ValueError(f"filemap.files[{source_path!r}].{field} has an invalid timestamp")
        if not isinstance(entry["size"], str) or re.fullmatch(
            r"\d+(?:\.\d+)? (?:B|KB|MB)", entry["size"]
        ) is None:
            raise ValueError(f"filemap.files[{source_path!r}].size has an invalid format")
        if not isinstance(entry["sha256"], str) or re.fullmatch(
            r"[a-fA-F0-9]{64}", entry["sha256"]
        ) is None:
            raise ValueError(f"filemap.files[{source_path!r}].sha256 has an invalid format")

        mirror = None
        if "markdown_mirror" in entry:
            mirror = entry["markdown_mirror"]
            mirror = normalize_workspace_relative_path(mirror)
            if not mirror or not path_is_within(mirror, output_dir):
                raise ValueError(f"filemap.files[{source_path!r}].markdown_mirror is outside output_dir")
            previous_source = seen_mirrors.get(mirror.casefold())
            if previous_source is not None:
                raise ValueError(
                    "filemap.files has duplicate markdown_mirror targets under "
                    f"case-insensitive comparison: {previous_source!r} and {source_path!r}"
                )
            seen_mirrors[mirror.casefold()] = source_path
        status = entry.get("conversion_status")
        if "conversion_status" in entry and (
            not isinstance(status, str) or status not in valid_statuses
        ):
            raise ValueError(f"filemap.files[{source_path!r}].conversion_status is invalid")
        if status == "converted" and not mirror:
            raise ValueError(f"filemap.files[{source_path!r}] is converted without a markdown_mirror")
        if "ocr_applied" in entry and not isinstance(entry["ocr_applied"], bool):
            raise ValueError(f"filemap.files[{source_path!r}].ocr_applied must be boolean")
        if "ocr_policy" in entry and (
            not isinstance(entry["ocr_policy"], str)
            or entry["ocr_policy"] not in valid_ocr_policies
        ):
            raise ValueError(f"filemap.files[{source_path!r}].ocr_policy is invalid")
        if "conversion_error" in entry and not isinstance(entry["conversion_error"], str):
            raise ValueError(f"filemap.files[{source_path!r}].conversion_error must be a string")

        derivative = entry.get("derivative")
        if "derivative" in entry:
            if not isinstance(derivative, dict):
                raise ValueError(f"filemap.files[{source_path!r}].derivative must be an object")
            for field in ("path", "sha256", "format"):
                if field not in derivative:
                    raise ValueError(f"filemap.files[{source_path!r}].derivative is missing {field}")
            deriv_path = normalize_workspace_relative_path(derivative["path"])
            if not deriv_path or not path_is_within(deriv_path, output_dir):
                raise ValueError(f"filemap.files[{source_path!r}].derivative.path is outside output_dir")
            if not isinstance(derivative["sha256"], str) or re.fullmatch(
                r"[a-fA-F0-9]{64}", derivative["sha256"]
            ) is None:
                raise ValueError(f"filemap.files[{source_path!r}].derivative.sha256 has an invalid format")
            if not isinstance(derivative["format"], str) or not derivative["format"].strip():
                raise ValueError(f"filemap.files[{source_path!r}].derivative.format is invalid")

        artifact_metadata = entry.get("artifact_metadata")
        if "artifact_metadata" in entry:
            if not mirror:
                raise ValueError(f"filemap.files[{source_path!r}] has artifact_metadata without a mirror")
            validate_cloud_artifact_metadata(
                artifact_metadata,
                expected_source_uri=normalized_source,
            )
            source_abs = Path(workspace_root) / Path(*PurePosixPath(normalized_source).parts)
            actual_source_sha256 = calculate_sha256(source_abs)
            if actual_source_sha256 is None:
                raise ValueError(
                    f"filemap.files[{source_path!r}].artifact_metadata source file is unavailable"
                )
            if artifact_metadata["source_sha256"].lower() != actual_source_sha256:
                raise ValueError(
                    f"filemap.files[{source_path!r}].artifact_metadata.source_sha256 "
                    "does not match the current source file"
                )
            actual_artifact_sha256 = calculate_markdown_payload_sha256(
                _read_mirror_payload(workspace_root, mirror)
            )
            if artifact_metadata["artifact_sha256"].lower() != actual_artifact_sha256:
                raise ValueError(
                    f"filemap.files[{source_path!r}].artifact_metadata.artifact_sha256 "
                    "does not match the mirror payload"
                )
    return True


def calculate_sha256(filepath):
    """Calculate SHA-256 hash of a file safely."""
    path_obj = Path(filepath)
    if not path_obj.is_file():
        return None
    hasher = hashlib.sha256()
    try:
        with open(path_obj, "rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                hasher.update(chunk)
        return hasher.hexdigest()
    except Exception:
        return None


def calculate_markdown_payload_sha256(markdown_body):
    """Hash the UTF-8 payload used by canonical Cloud mirror metadata."""
    return hashlib.sha256((markdown_body or "").encode("utf-8", errors="replace")).hexdigest()


def canonical_mirror_path(source_path, scan_dir, output_dir):
    """Derive the converter's canonical mirror path for a supported source."""
    return legacy_canonical_mirror_path(source_path, scan_dir, output_dir)


def canonical_derivative_path(source_path, scan_dir, output_dir):
    """Derive the converter's canonical derivative path for a .doc source."""
    source_rel = normalize_workspace_relative_path(source_path)
    scan_rel = normalize_workspace_relative_path(scan_dir)
    output_rel = normalize_workspace_relative_path(output_dir)
    if not source_rel or not scan_rel or not output_rel:
        return None

    source = PurePosixPath(source_rel)
    scan = PurePosixPath(scan_rel)
    try:
        relative_source = source.relative_to(scan)
    except ValueError:
        return None
    if relative_source.suffix.lower() != ".doc":
        return None
    return (PurePosixPath(output_rel) / "_derivatives" / relative_source.with_suffix(".docx")).as_posix()


def path_is_within(path_value, parent_value):
    """Return True when both safe relative paths place path inside parent."""
    path_rel = normalize_workspace_relative_path(path_value)
    parent_rel = normalize_workspace_relative_path(parent_value)
    if not path_rel or not parent_rel:
        return False
    try:
        PurePosixPath(path_rel).relative_to(PurePosixPath(parent_rel))
        return True
    except ValueError:
        return False


def workspace_file_exists(workspace_root, relative_path):
    normalized = normalize_workspace_relative_path(relative_path)
    if not normalized:
        return False
    return (Path(workspace_root) / Path(*PurePosixPath(normalized).parts)).is_file()


def encode_markdown_link_target(relative_path):
    """Percent-encode a relative Markdown link target without encoding slashes."""
    return quote(relative_path.replace("\\", "/"), safe="/-._~")


def select_markdown_mirror(
    workspace_root, source_path, scan_dir, output_dir, existing_info,
    planned_mirror=None,
):
    """Select an existing mirror without allowing stale or cross-zone paths."""
    if planned_mirror is not None:
        return planned_mirror if workspace_file_exists(workspace_root, planned_mirror) else None
    canonical = canonical_mirror_path(source_path, scan_dir, output_dir)
    if canonical and workspace_file_exists(workspace_root, canonical):
        return canonical

    existing = normalize_workspace_relative_path(existing_info.get("markdown_mirror"))
    if existing and path_is_within(existing, output_dir) and workspace_file_exists(workspace_root, existing):
        return existing
    return None


def select_derivative(workspace_root, source_path, scan_dir, output_dir, existing_info):
    """Select an existing derivative for .doc files or derive from canonical location."""
    canonical = canonical_derivative_path(source_path, scan_dir, output_dir)
    if canonical and workspace_file_exists(workspace_root, canonical):
        existing_deriv = existing_info.get("derivative")
        deriv_dict = dict(existing_deriv) if isinstance(existing_deriv, dict) else {}
        deriv_dict["path"] = canonical
        if "sha256" not in deriv_dict:
            full_deriv = Path(workspace_root) / Path(*PurePosixPath(canonical).parts)
            deriv_dict["sha256"] = calculate_sha256(full_deriv)
        if "format" not in deriv_dict:
            deriv_dict["format"] = "docx"
        return deriv_dict

    existing = existing_info.get("derivative")
    if isinstance(existing, dict) and existing.get("path"):
        normalized = normalize_workspace_relative_path(existing["path"])
        if normalized and path_is_within(normalized, output_dir) and workspace_file_exists(workspace_root, normalized):
            return existing
    return None


def preserve_existing_ocr_metadata(source_path, existing_info, generated_entry):
    """Carry validated OCR provenance into a regenerated technical entry.

    OCR state is converter-owned metadata, not curation.  It is retained only
    for a current PDF mirror and, for local OCR derivatives, a current local
    PDF derivative.  Invalid or contextless legacy values are deliberately not
    copied into the new filemap.
    """
    if (
        not isinstance(existing_info, dict)
        or not isinstance(generated_entry, dict)
        or not str(source_path).lower().endswith(".pdf")
        or "markdown_mirror" not in generated_entry
    ):
        return
    applied = existing_info.get("ocr_applied")
    policy = existing_info.get("ocr_policy")
    if type(applied) is not bool or policy not in VALID_OCR_POLICIES:
        return
    if applied:
        if policy == "disabled":
            return
        if policy == "local_derivative":
            derivative = generated_entry.get("derivative")
            if (
                not isinstance(derivative, dict)
                or derivative.get("format") != "pdf"
                or not str(derivative.get("path", "")).lower().endswith(".pdf")
            ):
                return
    generated_entry["ocr_applied"] = applied
    generated_entry["ocr_policy"] = policy


if hasattr(sys.stdout, 'reconfigure'):
    try:
        sys.stdout.reconfigure(encoding='utf-8')
    except Exception:
        pass
if hasattr(sys.stderr, 'reconfigure'):
    try:
        sys.stderr.reconfigure(encoding='utf-8')
    except Exception:
        pass

def parse_args():
    parser = ArgumentParser(description="Generischer Filemap-Generator fuer Cloud-Speicher Junctions.")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--project-id", help="Projekt-ID (z. B. meshe)")
    group.add_argument("--topic-id", help="Topic-ID (z. B. lifelong-learning)")
    parser.add_argument("--subtopic-id", help="Explizite Subtopic-ID unter --topic-id")
    parser.add_argument("--project-title", required=False, help="Titel des Projekts oder Themas")
    parser.add_argument("--scan-dir", required=False, help="Relativer Pfad zum Scan-Verzeichnis vom Workspace-Root aus (z. B. data/cloud/MESHE)")
    parser.add_argument("--output-json", required=False, help="Relativer Pfad zur Ziel-JSON-Datei (z. B. memory/references/projects/meshe/filemap.json)")
    parser.add_argument("--output-md", required=False, help="Relativer Pfad zur Ziel-Markdown-Datei (z. B. memory/references/projects/meshe/filemap.md)")
    parser.add_argument("--topic", action="store_true", help="Erzwinge die Behandlung als Topic (Standard: Auto-Erkennung)")
    parser.add_argument("--storage-id", required=False, help="Optionale Storage-ID bei mehreren Cloud-Speichern")
    parser.add_argument("--workspace-root", required=False, help="Expliziter Pfad zum Workspace-Root")
    parser.add_argument("--json", action="store_true", help="Gibt einen kanonischen Structured-CLI-Envelope aus")
    args = parser.parse_args()
    if args.subtopic_id and not args.topic_id:
        parser.error("--subtopic-id requires --topic-id")
    if args.subtopic_id:
        direct_paths = [
            flag for flag, value in (
                ("--scan-dir", args.scan_dir),
                ("--output-json", args.output_json),
                ("--output-md", args.output_md),
            )
            if value is not None
        ]
        if direct_paths:
            parser.error(
                "--subtopic-id uses only catalogued subtopic cloud_sync paths; "
                f"direct overrides are not allowed: {', '.join(direct_paths)}"
            )
    return args

# Regex patterns for version extraction
version_patterns = [
    re.compile(r"\bv(\d+(?:[\._]\d+)*)\b", re.IGNORECASE),
    re.compile(r"_v(\d+(?:[\._]\d+)*)", re.IGNORECASE),
    re.compile(r"[-_](final|def)\b", re.IGNORECASE)
]

def get_file_version(filename):
    for pattern in version_patterns:
        match = pattern.search(filename)
        if match:
            return match.group(1).upper()
    return "N/A"

def find_workspace_root(start_dir=None):
    env_root = os.environ.get("CLOUD_ATLAS_WORKSPACE_ROOT")
    if env_root and os.path.exists(env_root):
        return os.path.abspath(env_root)

    current = os.path.abspath(start_dir or os.getcwd())
    while True:
        try:
            if (os.path.exists(os.path.join(current, ".git")) or
                os.path.exists(os.path.join(current, "AGENTS.md")) or
                os.path.exists(os.path.join(current, ".workspace-root"))):
                return current
            try:
                for entry in os.scandir(current):
                    if entry.is_file() and entry.name.endswith(".code-workspace"):
                        return current
            except (PermissionError, OSError):
                pass
        except (PermissionError, OSError):
            pass

        parent = os.path.dirname(current)
        if parent == current:
            break
        current = parent
    return os.path.abspath(start_dir or os.getcwd())

def resolve_all_sync_configs(workspace_root, project_id, force_topic=False, storage_id=None, subtopic_id=None):
    projects_file = os.path.normpath(os.path.join(workspace_root, "memory/references/projects/projects.json"))
    project_meta = None
    is_topic = force_topic
    
    if not is_topic and os.path.exists(projects_file):
        try:
            with open(projects_file, "r", encoding="utf-8") as f:
                projects = json.load(f)
                for p in projects:
                    if p.get("id") == project_id:
                        project_meta = p
                        break
        except Exception as e:
            print(f"Warning: Could not read projects.json: {e}")

    topics_file = os.path.normpath(os.path.join(workspace_root, "memory/references/topics/topics.json"))
    topic_meta = None
    if not project_meta and os.path.exists(topics_file):
        try:
            with open(topics_file, "r", encoding="utf-8") as f:
                topics = json.load(f)
                for t in topics:
                    if t.get("id") == project_id:
                        topic_meta = t
                        is_topic = True
                        break
        except Exception as e:
            print(f"Warning: Could not read topics.json: {e}")

    if subtopic_id:
        if not topic_meta:
            raise ValueError(f"Topic '{project_id}' was not found for subtopic '{subtopic_id}'.")
        matches = [
            sub for sub in topic_meta.get("subtopics", [])
            if isinstance(sub, dict) and str(sub.get("id", "")) == subtopic_id
        ] if isinstance(topic_meta.get("subtopics"), list) else []
        if len(matches) != 1:
            raise ValueError(f"Subtopic '{subtopic_id}' must resolve exactly once under topic '{project_id}'.")
        subtopic = matches[0]
        if str(subtopic.get("status", "")).casefold() not in {"", "active"}:
            raise ValueError(f"Subtopic '{subtopic_id}' under topic '{project_id}' is not active.")
        cloud_sync = subtopic.get("cloud_sync")
        if not isinstance(cloud_sync, dict) or not cloud_sync:
            raise ValueError(f"Subtopic '{subtopic_id}' under topic '{project_id}' has no cloud_sync configuration.")
        configs = {}
        for sid, sconfig in cloud_sync.items():
            if not isinstance(sconfig, dict):
                raise ValueError(f"Subtopic storage '{sid}' must be a dictionary.")
            values = {
                "scan_dir": sconfig.get("scan_dir") or sconfig.get("cloud_dir"),
                "output_json": sconfig.get("output_json") or sconfig.get("filemap_json"),
                "output_md": sconfig.get("output_md") or sconfig.get("filemap_md"),
                "output_dir": sconfig.get("output_dir") or sconfig.get("cloud_mirror_dir"),
            }
            if not all(isinstance(value, str) and value.strip() for value in values.values()):
                raise ValueError(f"Subtopic storage '{sid}' must declare scan_dir, output_json, output_md and output_dir.")
            configs[sid] = {"title": str(subtopic.get("title") or subtopic_id), **values, "curation_json": sconfig.get("curation_json"), "is_topic": True, "subtopic_id": subtopic_id}
        if storage_id:
            if storage_id not in configs:
                raise ValueError(f"Storage ID '{storage_id}' was not found for subtopic '{subtopic_id}'.")
            return {storage_id: configs[storage_id]}
        return configs

    meta = project_meta or topic_meta
    
    title = project_id.upper()
    if meta:
        title = meta.get("kuerzel") or meta.get("title") or title
        
    cloud_sync = meta.get("cloud_sync") if meta else None
    configs = {}
    
    if not cloud_sync:
        configs["default"] = {
            "title": title,
            "scan_dir": f"data/cloud/{project_id.upper()}" if meta and meta.get("kuerzel") else f"data/cloud/{project_id}",
            "output_json": f"memory/cloud/{'topics' if is_topic else 'projects'}/{project_id}/filemap.json",
            "output_md": f"memory/cloud/{'topics' if is_topic else 'projects'}/{project_id}/filemap.md",
            "output_dir": f"memory/cloud/{'topics' if is_topic else 'projects'}/{project_id}",
            "curation_json": None,
            "is_topic": is_topic
        }
        kuerzel = (meta.get("kuerzel") or project_id) if meta else project_id
        folder_candidates = [
            os.path.join("data", "cloud", kuerzel),
            os.path.join("data", "cloud", project_id),
            os.path.join("data", "cloud", project_id.upper()),
            os.path.join("data", "cloud", project_id.lower())
        ]
        for candidate in folder_candidates:
            if os.path.exists(os.path.join(workspace_root, candidate)):
                configs["default"]["scan_dir"] = candidate.replace("\\", "/")
                break
    else:
        for sid, sconfig in cloud_sync.items():
            if not isinstance(sconfig, dict):
                print(f"Warning: Configuration for storage '{sid}' in project '{project_id}' must be a dictionary. Skipping.")
                continue
            scan_dir = sconfig.get("scan_dir") or sconfig.get("cloud_dir")
            output_json = sconfig.get("output_json") or sconfig.get("filemap_json")
            output_md = sconfig.get("output_md") or sconfig.get("filemap_md")
            output_dir = sconfig.get("output_dir") or sconfig.get("cloud_mirror_dir")
            
            suffix = f"-{sid}" if sid != "default" else ""
            stitle = f"{title} ({sid})" if sid != "default" else title
            
            configs[sid] = {
                "title": stitle,
                "scan_dir": scan_dir or (f"data/cloud/{project_id.upper()}" if meta and meta.get("kuerzel") else f"data/cloud/{project_id}"),
                "output_json": output_json or f"memory/cloud/{'topics' if is_topic else 'projects'}/{project_id}/filemap{suffix}.json",
                "output_md": output_md or f"memory/cloud/{'topics' if is_topic else 'projects'}/{project_id}/filemap{suffix}.md",
                "output_dir": output_dir or (f"memory/cloud/{'topics' if is_topic else 'projects'}/{project_id}" + (f"/{sid}" if sid != "default" else "")),
                "curation_json": sconfig.get("curation_json"),
                "is_topic": is_topic
            }
                
    if storage_id:
        if storage_id in configs:
            return {storage_id: configs[storage_id]}
        else:
            print(f"Warning: Storage ID '{storage_id}' not found in configuration.")
            return {}
            
    return configs

def get_file_info(filepath):
    import platform
    path_to_stat = filepath
    if platform.system() == "Windows":
        path_to_stat = "\\\\?\\" + os.path.abspath(filepath)
    stat = os.stat(path_to_stat)
    mtime = datetime.datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M:%S")
    size_bytes = stat.st_size
    if size_bytes < 1024:
        size_str = f"{size_bytes} B"
    elif size_bytes < 1024 * 1024:
        size_str = f"{size_bytes / 1024:.1f} KB"
    else:
        size_str = f"{size_bytes / (1024 * 1024):.1f} MB"
        
    filename = os.path.basename(filepath)
    version = get_file_version(filename)
    sha256 = calculate_sha256(filepath)
    return size_str, mtime, version, sha256

def update_config_last_synced_at(workspace_root, target_id, is_topic, storage_id, subtopic_id=None):
    now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    projects_file = os.path.normpath(os.path.join(workspace_root, "memory/references/projects/projects.json"))
    topics_file = os.path.normpath(os.path.join(workspace_root, "memory/references/topics/topics.json"))
    
    target_files = [topics_file, projects_file] if is_topic else [projects_file, topics_file]
    
    for json_file in target_files:
        if not os.path.exists(json_file):
            continue
        try:
            with open(json_file, "r", encoding="utf-8") as f:
                items = json.load(f)
            
            if not isinstance(items, list):
                continue
                
            updated = False
            for item in items:
                if isinstance(item, dict) and item.get("id") == target_id:
                    if subtopic_id:
                        matches = [
                            sub for sub in item.get("subtopics", [])
                            if isinstance(sub, dict) and str(sub.get("id", "")) == subtopic_id
                        ] if isinstance(item.get("subtopics"), list) else []
                        if len(matches) != 1:
                            return bounded_message(f"Could not update last_synced_at: subtopic '{subtopic_id}' must resolve exactly once.")
                        cloud_sync = matches[0].get("cloud_sync")
                        if not isinstance(cloud_sync, dict) or not isinstance(cloud_sync.get(storage_id), dict):
                            return bounded_message(f"Could not update last_synced_at: storage '{storage_id}' is not declared for subtopic '{subtopic_id}'.")
                        cloud_sync[storage_id]["last_synced_at"] = now_str
                        updated = True
                        break
                    cloud_sync = item.get("cloud_sync")
                    if isinstance(cloud_sync, dict):
                        if storage_id in cloud_sync and isinstance(cloud_sync[storage_id], dict):
                            cloud_sync[storage_id]["last_synced_at"] = now_str
                            updated = True
                        elif storage_id == "default" and len(cloud_sync) == 1:
                            first_key = list(cloud_sync.keys())[0]
                            if isinstance(cloud_sync[first_key], dict):
                                cloud_sync[first_key]["last_synced_at"] = now_str
                                updated = True
            
            if updated:
                write_json_file(json_file, items)
                print(f"Updated 'last_synced_at' timestamp for '{storage_id}' in {json_file}")
                break
        except Exception as e:
            warning = bounded_message(f"Could not update last_synced_at in {json_file}: {e}")
            print(f"Warning: {warning}")
            return warning
    return None

def run_generation(args):
    workspace_root = os.path.abspath(args.workspace_root) if args.workspace_root else find_workspace_root()
    
    project_id = args.project_id or args.topic_id
    is_topic = args.topic or (args.topic_id is not None)
    
    configs = resolve_all_sync_configs(workspace_root, project_id, is_topic, args.storage_id, args.subtopic_id)
    
    if not configs:
        raise RuntimeError(f"No cloud sync configurations resolved for ID '{project_id}'.")

    overlay_scope = "subtopic" if args.subtopic_id else ("topic" if is_topic else "project")
    curation_overlays = {}
    for sid, resolved in configs.items():
        try:
            curation_overlays[sid] = load_curation_overlay(
                workspace_root,
                resolved.get("curation_json"),
                scope=overlay_scope,
                target_id=project_id,
                subtopic_id=args.subtopic_id,
                storage_id=sid,
                scan_dir=resolved["scan_dir"],
            )
        except ValueError as exc:
            raise GenerationError("curation", sid, exc, storage_results=[]) from exc

    storage_results = []
    warnings = []
    for sid, resolved in configs.items():
        print(f"\n--- Generiere Filemap fuer Storage: {sid} ---")
        project_title = args.project_title or resolved["title"]
        if args.subtopic_id:
            # Do not permit programmatic callers to replace the explicit
            # subtopic's catalogued storage paths after argument validation.
            scan_dir = resolved["scan_dir"]
            output_json = resolved["output_json"]
            output_md = resolved["output_md"]
        else:
            scan_dir = args.scan_dir or resolved["scan_dir"]
            output_json = args.output_json or resolved["output_json"]
            output_md = args.output_md or resolved["output_md"]
        output_dir = resolved["output_dir"]
        
        # Convert relative paths to absolute paths
        scan_path_abs = os.path.normpath(os.path.join(workspace_root, scan_dir))
        output_json_abs = os.path.normpath(os.path.join(workspace_root, output_json))
        output_md_abs = os.path.normpath(os.path.join(workspace_root, output_md))
        
        if not os.path.exists(scan_path_abs):
            raise GenerationError("scan", sid, FileNotFoundError(f"Scan directory '{scan_path_abs}' does not exist."), storage_results)

        curation_overlay = curation_overlays[sid]

        # Load existing files data from JSON if it exists to preserve descriptions and custom keys
        existing_files_data = {}
        if os.path.exists(output_json_abs):
            try:
                with open(output_json_abs, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    if "files" in data:
                        existing_files_data = data["files"]
            except Exception as e:
                print(f"Warning: Could not read existing JSON: {e}")

        # First pass: scan directory
        raw_scanned_files = []
        for root, dirs, filenames in os.walk(scan_path_abs):
            for f in filenames:
                if f.startswith(".") or f.startswith("~$") or f.lower() == "desktop.ini":
                    continue
                full_path = os.path.join(root, f)
                rel_to_workspace = os.path.relpath(full_path, workspace_root)
                rel_to_workspace_forward = rel_to_workspace.replace("\\", "/")
                size_str, mtime, version, sha256 = get_file_info(full_path)
                raw_scanned_files.append({
                    "full_path": full_path,
                    "rel_to_workspace": rel_to_workspace_forward,
                    "size_str": size_str,
                    "mtime": mtime,
                    "version": version,
                    "sha256": sha256
                })

        raw_scanned_files.sort(
            key=lambda item: (item["rel_to_workspace"].casefold(), item["rel_to_workspace"])
        )

        current_scanned_paths = {item["rel_to_workspace"] for item in raw_scanned_files}
        
        # Build index of unmapped existing entries by sha256 to track renames/moves
        unmapped_by_sha256 = {}
        for old_path, old_info in existing_files_data.items():
            if old_path not in current_scanned_paths and isinstance(old_info, dict) and old_info.get("sha256"):
                unmapped_by_sha256.setdefault(old_info["sha256"], []).append((old_path, old_info))

        existing_mirrors = {
            item["rel_to_workspace"]: existing_files_data.get(item["rel_to_workspace"], {}).get("markdown_mirror")
            for item in raw_scanned_files
            if isinstance(existing_files_data.get(item["rel_to_workspace"]), dict)
        }
        for item in raw_scanned_files:
            if item["rel_to_workspace"] in existing_mirrors:
                continue
            prior = unmapped_by_sha256.get(item["sha256"], [])
            if len(prior) == 1 and isinstance(prior[0][1], dict):
                old_path, old_info = prior[0]
                custom = explicit_custom_mirror_path(
                    old_path, scan_dir, output_dir, old_info.get("markdown_mirror"),
                )
                if custom:
                    existing_mirrors[item["rel_to_workspace"]] = custom
        try:
            planned_mirrors = plan_markdown_mirrors(
                [item["rel_to_workspace"] for item in raw_scanned_files],
                scan_dir,
                output_dir,
                existing_mirrors,
            )
        except ValueError as exc:
            raise GenerationError("mirror-plan", sid, exc, storage_results) from exc

        files_data = {}
        for item in raw_scanned_files:
            rel_to_workspace_forward = item["rel_to_workspace"]
            full_path = item["full_path"]
            size_str = item["size_str"]
            mtime = item["mtime"]
            version = item["version"]
            sha256 = item["sha256"]
            
            # Resolve existing metadata (by path or by sha256 rename)
            if rel_to_workspace_forward in existing_files_data:
                existing_info = existing_files_data[rel_to_workspace_forward]
            elif sha256 in unmapped_by_sha256 and len(unmapped_by_sha256[sha256]) > 0:
                old_path, existing_info = unmapped_by_sha256[sha256].pop(0)
                print(f"Filemap: Uebernehme Metadaten von {old_path} -> {rel_to_workspace_forward} (SHA-256 Match)")
            else:
                existing_info = {}
                
            file_entry = {
                "version": version,
                "mtime": mtime,
                "size": size_str,
                "sha256": sha256,
                "description": "-"
            }
            
            markdown_mirror = select_markdown_mirror(
                workspace_root,
                rel_to_workspace_forward,
                scan_dir,
                output_dir,
                existing_info,
                planned_mirrors.get(rel_to_workspace_forward),
            )
            if markdown_mirror:
                file_entry["markdown_mirror"] = markdown_mirror
                artifact_metadata = _read_mirror_artifact_metadata(
                    workspace_root, markdown_mirror
                )
                if artifact_metadata is not None:
                    file_entry["artifact_metadata"] = artifact_metadata

            derivative = select_derivative(
                workspace_root,
                rel_to_workspace_forward,
                scan_dir,
                output_dir,
                existing_info,
            )
            if derivative:
                file_entry["derivative"] = derivative

            preserve_existing_ocr_metadata(
                rel_to_workspace_forward,
                existing_info,
                file_entry,
            )

            # Status handling
            if rel_to_workspace_forward.lower().endswith(".doc"):
                if markdown_mirror and derivative:
                    file_entry["conversion_status"] = "converted"
                else:
                    file_entry["conversion_status"] = existing_info.get("conversion_status", "conversion_required")
                    file_entry["conversion_error"] = existing_info.get(
                        "conversion_error",
                        "No converter available or conversion not yet run"
                    )
            elif markdown_mirror:
                file_entry["conversion_status"] = "converted"
            elif existing_info.get("conversion_status"):
                file_entry["conversion_status"] = existing_info["conversion_status"]
                if existing_info.get("conversion_error"):
                    file_entry["conversion_error"] = existing_info["conversion_error"]

            try:
                overlay_entry = curation_overlay.match(rel_to_workspace_forward, sha256) if curation_overlay else None
            except ValueError as exc:
                raise GenerationError("curation", sid, exc, storage_results) from exc
            files_data[rel_to_workspace_forward] = merge_curated_metadata(
                existing_info,
                file_entry,
                overlay_entry,
                overlay_active=curation_overlay is not None,
            )

        # Keep file ordering stable even when the filesystem returns directory
        # entries in a different order between runs.
        files_data = {path: files_data[path] for path in sorted(files_data)}

        # Save to JSON only after validating the container and any delegated
        # canonical artifact metadata.  This prevents a malformed map from
        # replacing the previous valid one.
        json_output_data = {
            "$schema": FILEMAP_SCHEMA_URI,
            "schema_version": FILEMAP_SCHEMA_VERSION,
            "kind": "cloud-filemap",
            "scope": "topic" if is_topic else "project",
            "storage_id": sid,
            "project": project_id,
            "project_title": project_title,
            "scan_dir": scan_dir,
            "output_dir": output_dir,
            "updated_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "files": files_data
        }
        try:
            validate_filemap(json_output_data, workspace_root)
        except ValueError as exc:
            raise GenerationError("validation", sid, exc, storage_results) from exc
        try:
            write_json_file(output_json_abs, json_output_data)
        except OSError as exc:
            raise GenerationError("write", sid, exc, storage_results) from exc
        print(f"Updated {output_json_abs}")

        # Generate Markdown File
        sorted_files = sorted(files_data.keys(), key=lambda x: x.lower())
        
        filemap_id = f"{project_id}-{sid}-filemap" if sid != "default" else f"{project_id}-filemap"
        
        md_content = f"""---
type: Reference
id: {filemap_id}
title: "{project_title} Cloud Storage Filemap"
project: {project_id}
updated_at: {datetime.datetime.now().strftime("%Y-%m-%d")}
schema_version: 1
---

# {project_title} Cloud Storage Filemap

Dieses Dokument stellt die automatisierte Filemap des kanonischen **{project_title} Cloud Storage** dar. 
Der Cloud-Speicher dient als primäre und offizielle Dateiablage des Projekts/Themas. Hier dürfen ausschließlich finale bzw. zur Veröffentlichung in der offiziellen Projektdokumentation freigegebene Dokumente abgelegt werden.

Pfad relativ zum Workspace-Root: `{scan_dir}/`

> [!NOTE]
> Die Dateiliste und Versionen werden automatisch aus den im Verzeichnis vorhandenen Dateien generiert. Versionen werden anhand gängiger Namenskonventionen (z. B. `_v3`, `-FINAL`, `-def`) extrahiert. Alle Pfade sind relativ zum Workspace-Root angegeben.
> 
> **Arbeitsregel**: Immer wenn eine Datei aus dem Cloud-Speicher genauer analysiert, verarbeitet oder ergänzt wird, soll eine kurze Beschreibung dieser Datei in der Datei `{output_json}` hinterlegt und diese Filemap anschließend neu generiert werden.

## Dateiverzeichnis ({len(sorted_files)} Dateien)

| Relativer Pfad | Version | Letzte Änderung | Größe | Beschreibung |
| :--- | :---: | :---: | :---: | :--- |
"""

        table_rows = []
        for path in sorted_files:
            info = files_data[path]
            desc = info.get('description', '-')
            
            links = []
            if info.get("markdown_mirror"):
                mirror_rel_to_workspace = info["markdown_mirror"]
                mirror_abs = os.path.normpath(os.path.join(workspace_root, mirror_rel_to_workspace))
                output_md_dir = os.path.dirname(output_md_abs)
                mirror_rel_link = os.path.relpath(mirror_abs, output_md_dir).replace("\\", "/")
                mirror_link_target = encode_markdown_link_target(mirror_rel_link)
                basename = os.path.basename(mirror_rel_to_workspace)
                links.append(f"Spiegelung: [{basename}]({mirror_link_target})")

            if info.get("derivative") and isinstance(info["derivative"], dict) and info["derivative"].get("path"):
                deriv_rel_to_workspace = info["derivative"]["path"]
                deriv_abs = os.path.normpath(os.path.join(workspace_root, deriv_rel_to_workspace))
                if os.path.isfile(deriv_abs):
                    output_md_dir = os.path.dirname(output_md_abs)
                    deriv_rel_link = os.path.relpath(deriv_abs, output_md_dir).replace("\\", "/")
                    deriv_link_target = encode_markdown_link_target(deriv_rel_link)
                    deriv_basename = os.path.basename(deriv_rel_to_workspace)
                    links.append(f"Derivat: [{deriv_basename}]({deriv_link_target})")

            desc_parts = []
            if info.get("conversion_status") == "conversion_required":
                err = info.get("conversion_error", "Konvertierung erforderlich")
                desc_parts.append(f"⚠️ **Konvertierung erforderlich** ({err})")
                
            if desc and desc != "-":
                desc_parts.append(desc)
                
            if links:
                desc_parts.append(f"({', '.join(links)})")
                
            final_desc = " — ".join(desc_parts) if desc_parts else "-"
            row = f"| {path} | {info['version']} | {info['mtime']} | {info['size']} | {final_desc} |"
            table_rows.append(row)

        md_content += "\n".join(table_rows) + "\n"
        
        current_result = {
            "storage_id": sid,
            "output_json": output_json.replace("\\", "/"),
            "output_md": output_md.replace("\\", "/"),
            "file_count": len(files_data),
        }
        try:
            atomic_write_text(output_md_abs, md_content)
        except OSError as exc:
            partial_result = dict(current_result)
            partial_result["completed_outputs"] = ["json"]
            raise GenerationError("write", sid, exc, storage_results, partial_result) from exc
        print(f"Updated {output_md_abs}")
        
        # Update last_synced_at in projects.json / topics.json
        storage_results.append(current_result)
        warning = update_config_last_synced_at(workspace_root, project_id, is_topic, sid, args.subtopic_id)
        if warning:
            warnings.append({"storage_id": sid, "message": bounded_message(warning)})
    return {
        "target": (
            {"kind": "subtopic", "topic_id": project_id, "subtopic_id": args.subtopic_id}
            if args.subtopic_id else {"kind": "topic" if is_topic else "project", "id": project_id}
        ),
        "storages": storage_results,
        "warnings": warnings,
    }


def _error_details(phase, exc, storage_id=None):
    error = {"phase": phase, "type": type(exc).__name__, "message": bounded_message(exc)}
    if storage_id is not None:
        error["storage_id"] = storage_id
    return error


def main():
    try:
        args = parse_args()
    except CliArgumentError as exc:
        if "--json" in sys.argv[1:]:
            emit_json(envelope(False, FAILED, "Filemap-Generierung konnte nicht gestartet werden.", {"target": None, "storages": [], "warnings": []}, _error_details("arguments", exc)))
        else:
            print(f"Fehlerhafte Argumente: {exc}", file=sys.stderr)
        return 2

    if not args.json:
        try:
            run_generation(args)
        except Exception as exc:
            print(f"Error: {exc}", file=sys.stderr)
            return 1
        return 0

    captured_output = io.StringIO()
    try:
        with contextlib.redirect_stdout(captured_output):
            data = run_generation(args)
    except Exception as exc:
        phase = "generation"
        storage_id = args.storage_id
        completed_storages = []
        partial_storage = None
        if isinstance(exc, GenerationError):
            phase = exc.phase
            storage_id = exc.storage_id
            completed_storages = exc.storage_results
            partial_storage = exc.partial_storage
            exc = exc.cause
        elif isinstance(exc, FileNotFoundError):
            phase = "scan"
        elif isinstance(exc, RuntimeError) and str(exc).startswith("No cloud sync configurations"):
            phase = "config"
        elif isinstance(exc, ValueError):
            phase = "validation"
        elif isinstance(exc, OSError):
            phase = "write"
        target = (
            {"kind": "subtopic", "topic_id": args.topic_id, "subtopic_id": args.subtopic_id}
            if args.subtopic_id
            else {"kind": "topic" if (args.topic or args.topic_id) else "project", "id": args.project_id or args.topic_id}
        )
        emit_json(envelope(False, FAILED, "Filemap-Generierung fehlgeschlagen.", {"target": target, "storages": completed_storages, "partial_storage": partial_storage, "warnings": []}, _error_details(phase, exc, storage_id)))
        return 1

    emit_json(envelope(True, COMPLETED, "Filemap-Generierung erfolgreich abgeschlossen.", data))
    return 0

if __name__ == "__main__":
    sys.exit(main())
