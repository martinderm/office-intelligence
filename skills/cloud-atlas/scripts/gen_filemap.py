import os
import sys
import datetime
import re
import json
import argparse

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
    version = "N/A"
    
    for pattern in version_patterns:
        match = pattern.search(filename)
        if match:
            version = match.group(1).upper()
            break
            
    return size_str, mtime, version

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

        files_data = {}
        
        # Scan directory
        for root, dirs, filenames in os.walk(scan_path_abs):
            for f in filenames:
                if f.startswith(".") or f.startswith("~$") or f.lower() == "desktop.ini":
                    continue
                full_path = os.path.join(root, f)
                
                # Path relative to workspace root
                rel_to_workspace = os.path.relpath(full_path, workspace_root)
                rel_to_workspace_forward = rel_to_workspace.replace("\\", "/")
                
                size_str, mtime, version = get_file_info(full_path)
                
                # Get existing file info if present to preserve descriptions and custom keys
                existing_info = existing_files_data.get(rel_to_workspace_forward, {})
                desc = existing_info.get("description", "-")
                
                files_data[rel_to_workspace_forward] = {
                    "version": version,
                    "mtime": mtime,
                    "size": size_str,
                    "description": desc
                }
                
                # Preserve any other custom keys (like markdown_mirror)
                for key, val in existing_info.items():
                    if key not in files_data[rel_to_workspace_forward]:
                        files_data[rel_to_workspace_forward][key] = val

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
            desc = info['description']
            
            # If there's a markdown mirror, format a relative link to it
            if info.get("markdown_mirror"):
                mirror_rel_to_workspace = info["markdown_mirror"]
                mirror_abs = os.path.normpath(os.path.join(workspace_root, mirror_rel_to_workspace))
                output_md_dir = os.path.dirname(output_md_abs)
                # Relative path from the output markdown directory to the mirror file
                mirror_rel_link = os.path.relpath(mirror_abs, output_md_dir).replace("\\", "/")
                basename = os.path.basename(mirror_rel_to_workspace)
                desc += f" (Spiegelung: [{basename}]({mirror_rel_link}))"
                
            row = f"| {path} | {info['version']} | {info['mtime']} | {info['size']} | {desc} |"
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
