import os
import sys
import datetime
import re
import json
import argparse
import subprocess

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
    parser = argparse.ArgumentParser(description="Automatische Konvertierung von Cloud-Dateien zu Markdown.")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--project-id", help="Projekt-ID (z. B. meshe)")
    group.add_argument("--topic-id", help="Topic-ID (z. B. lifelong-learning)")
    parser.add_argument("--cloud-dir", required=False, help="Relativer Pfad zum Cloud-Scan-Verzeichnis (z. B. data/cloud/MESHE)")
    parser.add_argument("--output-dir", required=False, help="Relativer Pfad zum Spiegelungs-Verzeichnis im Project-Memory (z. B. memory/references/projects/meshe/cloud)")
    parser.add_argument("--filemap-json", required=False, help="Relativer Pfad zur filemap.json (z. B. memory/references/projects/meshe/filemap.json)")
    parser.add_argument("--extensions", default=".pdf,.docx,.xlsx,.pptx", help="Kommagetrennte Liste der zu konvertierenden Erweiterungen")
    parser.add_argument("--force", action="store_true", help="Alle Konvertierungen erzwingen")
    parser.add_argument("--topic", action="store_true", help="Erzwinge die Behandlung als Topic (Standard: Auto-Erkennung)")
    parser.add_argument("--storage-id", required=False, help="Optionale Storage-ID bei mehreren Cloud-Speichern")
    return parser.parse_args()

# Setup markitdown conversion
try:
    from markitdown import MarkItDown
    use_library = True
except ImportError:
    use_library = False

def get_safe_path(filepath):
    import platform
    if platform.system() == "Windows":
        return "\\\\?\\" + os.path.normpath(os.path.abspath(filepath))
    return filepath

def convert_to_markdown(src_abs):
    safe_src = get_safe_path(src_abs)
    if use_library:
        try:
            md = MarkItDown()
            result = md.convert(safe_src)
            return result.text_content
        except Exception as e:
            raise RuntimeError(f"MarkItDown library conversion failed: {e}")
    else:
        # Fallback to CLI command
        try:
            res = subprocess.run(["markitdown", safe_src], capture_output=True, text=True, encoding="utf-8")
            if res.returncode == 0:
                return res.stdout
            else:
                raise RuntimeError(res.stderr or f"CLI returned exit code {res.returncode}")
        except FileNotFoundError:
            raise RuntimeError("Neither 'markitdown' Python package nor CLI tool was found. Please run 'pip install markitdown'.")
        except Exception as e:
            raise RuntimeError(f"CLI conversion failed: {e}")

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
            "output_json": f"memory/references/{'topics' if is_topic else 'projects'}/{project_id}/filemap.json",
            "output_md": f"memory/references/{'topics' if is_topic else 'projects'}/{project_id}/filemap.md",
            "output_dir": f"memory/references/{'topics' if is_topic else 'projects'}/{project_id}/cloud",
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
                "output_json": output_json or f"memory/references/{'topics' if is_topic else 'projects'}/{project_id}/filemap{suffix}.json",
                "output_md": output_md or f"memory/references/{'topics' if is_topic else 'projects'}/{project_id}/filemap{suffix}.md",
                "output_dir": output_dir or (f"memory/references/{'topics' if is_topic else 'projects'}/{project_id}/cloud" + (f"/{sid}" if sid != "default" else "")),
                "is_topic": is_topic
            }
                
    if storage_id:
        if storage_id in configs:
            return {storage_id: configs[storage_id]}
        else:
            print(f"Warning: Storage ID '{storage_id}' not found in configuration.")
            return {}
            
    return configs

def parse_markdown_file(filepath):
    safe_path = get_safe_path(filepath)
    if not os.path.exists(safe_path):
        return None, ""
    try:
        with open(safe_path, "r", encoding="utf-8") as f:
            content = f.read()
        if content.startswith("---"):
            parts = content.split("---", 2)
            if len(parts) >= 3:
                frontmatter_str = parts[1]
                body = parts[2]
                metadata = {}
                for line in frontmatter_str.strip().split("\n"):
                    if ":" in line:
                        k, v = line.split(":", 1)
                        metadata[k.strip()] = v.strip().strip('"').strip("'")
                return metadata, body
    except Exception as e:
        print(f"Warning parsing markdown {filepath}: {e}")
    return None, ""

def write_markdown_file(filepath, metadata, body):
    safe_path = get_safe_path(filepath)
    frontmatter_lines = ["---"]
    for k, v in metadata.items():
        frontmatter_lines.append(f'{k}: "{v}"')
    frontmatter_lines.append("---")
    
    os.makedirs(os.path.dirname(safe_path), exist_ok=True)
    with open(safe_path, "w", encoding="utf-8") as f:
        f.write("\n".join(frontmatter_lines) + "\n\n" + body)

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
        print(f"\n--- Konvertiere Dokumente fuer Storage: {sid} ---")
        cloud_dir = args.cloud_dir or resolved["scan_dir"]
        output_dir = args.output_dir or resolved["output_dir"]
        filemap_json = args.filemap_json or resolved["output_json"]
        
        cloud_path_abs = os.path.normpath(os.path.join(workspace_root, cloud_dir))
        output_path_abs = os.path.normpath(os.path.join(workspace_root, output_dir))
        filemap_json_abs = os.path.normpath(os.path.join(workspace_root, filemap_json))
        
        extensions = [ext.strip().lower() for ext in args.extensions.split(",")]
        
        if not os.path.exists(cloud_path_abs):
            print(f"Error: Cloud directory '{cloud_path_abs}' does not exist.")
            continue
            
        # Load filemap.json
        filemap_data = {}
        if os.path.exists(filemap_json_abs):
            try:
                with open(filemap_json_abs, "r", encoding="utf-8") as f:
                    filemap_data = json.load(f)
            except Exception as e:
                print(f"Warning: Could not read filemap.json: {e}")
                
        # Initialize filemap_data if empty to fix write bug
        if not filemap_data:
            filemap_data = {
                "project": project_id,
                "project_title": resolved["title"],
                "files": {}
            }
        files_in_json = filemap_data.setdefault("files", {})
        
        processed_mirrors = set()
        now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        
        # Scan cloud directory
        for root, dirs, filenames in os.walk(cloud_path_abs):
            for f in filenames:
                ext = os.path.splitext(f)[1].lower()
                if ext not in extensions:
                    continue
                if f.startswith(".") or f.startswith("~$") or f.lower() == "desktop.ini":
                    continue
                    
                src_abs = os.path.join(root, f)
                src_rel_workspace = os.path.relpath(src_abs, workspace_root).replace("\\", "/")
                
                # Determine output markdown path
                rel_to_cloud = os.path.relpath(src_abs, cloud_path_abs)
                dest_rel_workspace = os.path.join(output_dir, os.path.splitext(rel_to_cloud)[0] + ".md").replace("\\", "/")
                dest_abs = os.path.normpath(os.path.join(workspace_root, dest_rel_workspace))
                
                processed_mirrors.add(dest_rel_workspace)
                
                stat = os.stat(get_safe_path(src_abs))
                src_mtime = datetime.datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M:%S")
                src_version = get_file_version(f)
                
                # Check if conversion is needed
                needs_conversion = args.force or not os.path.exists(get_safe_path(dest_abs))
                existing_meta = None
                
                if os.path.exists(get_safe_path(dest_abs)):
                    existing_meta, body = parse_markdown_file(dest_abs)
                    if existing_meta:
                        if existing_meta.get("file_date") != src_mtime:
                            needs_conversion = True
                        else:
                            last_verified_str = existing_meta.get("last_verified_date")
                            if last_verified_str:
                                try:
                                    last_verified = datetime.datetime.strptime(last_verified_str, "%Y-%m-%d %H:%M:%S")
                                    age_hours = (datetime.datetime.now() - last_verified).total_seconds() / 3600.0
                                    if age_hours >= 24.0:
                                        print(f"Verifying {src_rel_workspace} (unchanged since last check)...")
                                        existing_meta["last_verified_date"] = now_str
                                        write_markdown_file(dest_abs, existing_meta, body)
                                except Exception as e:
                                    print(f"Warning parsing last_verified_date: {e}")
                                    needs_conversion = True
                    else:
                        needs_conversion = True
                        
                if needs_conversion:
                    print(f"Converting {src_rel_workspace} -> {dest_rel_workspace}...")
                    try:
                        markdown_body = convert_to_markdown(src_abs)
                        metadata = {
                            "original_file": src_rel_workspace,
                            "version": src_version,
                            "conversion_date": now_str,
                            "file_date": src_mtime,
                            "last_verified_date": now_str
                        }
                        write_markdown_file(dest_abs, metadata, markdown_body)
                    except Exception as e:
                        print(f"Error converting {src_rel_workspace}: {e}")
                        continue
                        
                if src_rel_workspace in files_in_json:
                    files_in_json[src_rel_workspace]["markdown_mirror"] = dest_rel_workspace
                else:
                    files_in_json[src_rel_workspace] = {
                        "version": src_version,
                        "mtime": src_mtime,
                        "size": f"{stat.st_size / 1024:.1f} KB",
                        "description": "-",
                        "markdown_mirror": dest_rel_workspace
                    }
                    
        # Clean up orphaned markdown mirrors (where original PDF is deleted)
        if os.path.exists(output_path_abs):
            for root, dirs, filenames in os.walk(output_path_abs):
                for f in filenames:
                    if not f.endswith(".md"):
                        continue
                    mirror_abs = os.path.join(root, f)
                    mirror_rel_workspace = os.path.relpath(mirror_abs, workspace_root).replace("\\", "/")
                    
                    if mirror_rel_workspace not in processed_mirrors:
                        print(f"Removing orphaned mirror: {mirror_rel_workspace}")
                        try:
                            os.remove(get_safe_path(mirror_abs))
                        except Exception as e:
                            print(f"Error removing {mirror_rel_workspace}: {e}")
                            
                        for src_rel, info in list(files_in_json.items()):
                            if info.get("markdown_mirror") == mirror_rel_workspace:
                                del info["markdown_mirror"]
                                
            # Clean up empty subdirectories
            for root, dirs, files in os.walk(output_path_abs, topdown=False):
                for d in dirs:
                    dir_path = os.path.join(root, d)
                    try:
                        if not os.listdir(get_safe_path(dir_path)):
                            print(f"Removing empty subdirectory: {dir_path}")
                            os.rmdir(get_safe_path(dir_path))
                    except Exception as e:
                        print(f"Warning removing empty subdirectory {dir_path}: {e}")
                                
        # Save filemap.json back
        filemap_data["files"] = files_in_json
        filemap_data["updated_at"] = now_str
        with open(filemap_json_abs, "w", encoding="utf-8") as f:
            json.dump(filemap_data, f, indent=2, ensure_ascii=False)

if __name__ == "__main__":
    main()
