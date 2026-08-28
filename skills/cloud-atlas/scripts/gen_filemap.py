import os
import sys
import datetime
import re
import json
import argparse
import hashlib
from pathlib import Path, PurePosixPath
from urllib.parse import quote


MIRROR_SOURCE_EXTENSIONS = {".pdf", ".docx", ".xlsx", ".pptx", ".doc"}


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


def canonical_mirror_path(source_path, scan_dir, output_dir):
    """Derive the converter's canonical mirror path for a supported source."""
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
    if relative_source.suffix.lower() not in MIRROR_SOURCE_EXTENSIONS:
        return None
    return (PurePosixPath(output_rel) / relative_source.with_suffix(".md")).as_posix()


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


def select_markdown_mirror(workspace_root, source_path, scan_dir, output_dir, existing_info):
    """Select an existing mirror without allowing stale or cross-zone paths."""
    canonical = canonical_mirror_path(source_path, scan_dir, output_dir)
    if canonical and workspace_file_exists(workspace_root, canonical):
        return canonical

    existing = normalize_workspace_relative_path(existing_info.get("markdown_mirror"))
    if existing and path_is_within(existing, output_dir) and workspace_file_exists(workspace_root, existing):
        return existing
    return None


AUTOMATED_METADATA_KEYS = {
    "version", "mtime", "size", "sha256",
    "markdown_mirror", "derivative",
    "conversion_status", "conversion_error"
}


def merge_curated_metadata(existing_entry, new_entry):
    """
    Merge newly computed automatic fields into existing metadata,
    ensuring manually maintained descriptions and custom keys are never lost.
    """
    if not isinstance(existing_entry, dict):
        return new_entry
    
    result = dict(new_entry)
    
    # Preserve manual description if existing was not default/empty
    existing_desc = existing_entry.get("description")
    if existing_desc and existing_desc != "-":
        result["description"] = existing_desc
    elif "description" not in result:
        result["description"] = existing_desc or "-"
        
    # Preserve all other non-automated keys
    for k, v in existing_entry.items():
        if k not in AUTOMATED_METADATA_KEYS and k not in result:
            result[k] = v
            
    return result


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
    parser = argparse.ArgumentParser(description="Generischer Filemap-Generator fuer Cloud-Speicher Junctions.")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--project-id", help="Projekt-ID (z. B. meshe)")
    group.add_argument("--topic-id", help="Topic-ID (z. B. lifelong-learning)")
    parser.add_argument("--project-title", required=False, help="Titel des Projekts oder Themas")
    parser.add_argument("--scan-dir", required=False, help="Relativer Pfad zum Scan-Verzeichnis vom Workspace-Root aus (z. B. data/cloud/MESHE)")
    parser.add_argument("--output-json", required=False, help="Relativer Pfad zur Ziel-JSON-Datei (z. B. memory/references/projects/meshe/filemap.json)")
    parser.add_argument("--output-md", required=False, help="Relativer Pfad zur Ziel-Markdown-Datei (z. B. memory/references/projects/meshe/filemap.md)")
    parser.add_argument("--topic", action="store_true", help="Erzwinge die Behandlung als Topic (Standard: Auto-Erkennung)")
    parser.add_argument("--storage-id", required=False, help="Optionale Storage-ID bei mehreren Cloud-Speichern")
    return parser.parse_args()

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

def find_workspace_root():
    current = os.path.abspath(os.getcwd())
    while True:
        if (os.path.exists(os.path.join(current, ".git")) or
            os.path.exists(os.path.join(current, "AGENTS.md")) or
            any(f.endswith(".code-workspace") for f in os.listdir(current) if os.path.isfile(os.path.join(current, f)))):
            return current
        parent = os.path.dirname(current)
        if parent == current:
            break
        current = parent
    return os.path.abspath(os.getcwd())

def resolve_all_sync_configs(workspace_root, project_id, force_topic=False, storage_id=None):
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

def update_config_last_synced_at(workspace_root, target_id, is_topic, storage_id):
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
                with open(json_file, "w", encoding="utf-8") as f:
                    json.dump(items, f, indent=2, ensure_ascii=False)
                print(f"Updated 'last_synced_at' timestamp for '{storage_id}' in {json_file}")
                break
        except Exception as e:
            print(f"Warning: Could not update last_synced_at in {json_file}: {e}")

def main():
    args = parse_args()
    workspace_root = find_workspace_root()
    
    project_id = args.project_id or args.topic_id
    is_topic = args.topic or (args.topic_id is not None)
    
    configs = resolve_all_sync_configs(workspace_root, project_id, is_topic, args.storage_id)
    
    if not configs:
        print(f"Error: No cloud sync configurations resolved for ID '{project_id}'.")
        return

    for sid, resolved in configs.items():
        print(f"\n--- Generiere Filemap fuer Storage: {sid} ---")
        project_title = args.project_title or resolved["title"]
        scan_dir = args.scan_dir or resolved["scan_dir"]
        output_json = args.output_json or resolved["output_json"]
        output_md = args.output_md or resolved["output_md"]
        output_dir = resolved["output_dir"]
        
        # Convert relative paths to absolute paths
        scan_path_abs = os.path.normpath(os.path.join(workspace_root, scan_dir))
        output_json_abs = os.path.normpath(os.path.join(workspace_root, output_json))
        output_md_abs = os.path.normpath(os.path.join(workspace_root, output_md))
        
        if not os.path.exists(scan_path_abs):
            print(f"Error: Scan directory '{scan_path_abs}' does not exist.")
            continue

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

        current_scanned_paths = {item["rel_to_workspace"] for item in raw_scanned_files}
        
        # Build index of unmapped existing entries by sha256 to track renames/moves
        unmapped_by_sha256 = {}
        for old_path, old_info in existing_files_data.items():
            if old_path not in current_scanned_paths and isinstance(old_info, dict) and old_info.get("sha256"):
                unmapped_by_sha256.setdefault(old_info["sha256"], []).append((old_path, old_info))

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
            )
            if markdown_mirror:
                file_entry["markdown_mirror"] = markdown_mirror

            derivative = select_derivative(
                workspace_root,
                rel_to_workspace_forward,
                scan_dir,
                output_dir,
                existing_info,
            )
            if derivative:
                file_entry["derivative"] = derivative

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

            files_data[rel_to_workspace_forward] = merge_curated_metadata(existing_info, file_entry)

        # Save to JSON
        json_output_data = {
            "project": project_id,
            "project_title": project_title,
            "updated_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "files": files_data
        }
        
        os.makedirs(os.path.dirname(output_json_abs), exist_ok=True)
        with open(output_json_abs, "w", encoding="utf-8") as f:
            json.dump(json_output_data, f, indent=2, ensure_ascii=False)
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
        
        os.makedirs(os.path.dirname(output_md_abs), exist_ok=True)
        with open(output_md_abs, "w", encoding="utf-8") as f:
            f.write(md_content)
        print(f"Updated {output_md_abs}")
        
        # Update last_synced_at in projects.json / topics.json
        update_config_last_synced_at(workspace_root, project_id, is_topic, sid)

if __name__ == "__main__":
    main()
