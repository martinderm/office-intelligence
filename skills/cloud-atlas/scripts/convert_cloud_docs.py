import os
import sys
import datetime
import re
import json
import argparse
import subprocess
import time
import multiprocessing

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
    parser.add_argument("--file-timeout", type=int, default=60, help="Maximales Timeout pro Dateikonvertierung in Sekunden (Standard: 60)")
    parser.add_argument("--jobs", "-j", type=int, default=2, help="Anzahl paralleler Konvertierungs-Jobs (Standard: 2)")
    parser.add_argument("--no-ocr", action="store_true", help="Deaktiviere automatisches OCR-Fallback fuer rein bildbasierte PDFs")
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

def ensure_tesseract_path():
    import platform
    if platform.system() == "Windows":
        candidates = [
            r"C:\Users\dagobert-ai\AppData\Local\Programs\Tesseract-OCR",
            r"C:\Program Files\Tesseract-OCR",
            r"C:\Program Files (x86)\Tesseract-OCR",
            os.path.expanduser(r"~\AppData\Local\Programs\Tesseract-OCR")
        ]
        path_dirs = os.environ.get("PATH", "").split(os.pathsep)
        for cand in candidates:
            if os.path.exists(os.path.join(cand, "tesseract.exe")) and cand not in path_dirs:
                os.environ["PATH"] = cand + os.pathsep + os.environ.get("PATH", "")
                break

def run_ocr_on_pdf(src_abs, timeout=120):
    safe_src = get_safe_path(src_abs)
    ensure_tesseract_path()
    try:
        res = subprocess.run(
            ["ocrmypdf", "-l", "deu", "--skip-text", safe_src, safe_src],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=timeout
        )
        if res.returncode == 0:
            return True, "OCR succeeded"
        else:
            return False, res.stderr or f"Exit code {res.returncode}"
    except FileNotFoundError:
        return False, "ocrmypdf executable not found."
    except subprocess.TimeoutExpired:
        return False, f"ocrmypdf timed out after {timeout}s"
    except Exception as e:
        return False, str(e)

def convert_to_markdown_raw(src_abs):
    safe_src = get_safe_path(src_abs)
    if use_library:
        try:
            md = MarkItDown()
            result = md.convert(safe_src)
            return result.text_content
        except Exception as e:
            raise RuntimeError(f"MarkItDown library conversion failed: {e}")
    else:
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

def convert_to_markdown(src_abs, enable_ocr=True):
    text = convert_to_markdown_raw(src_abs)
    if enable_ocr and src_abs.lower().endswith(".pdf") and len(text.strip()) < 30:
        ocr_success, msg = run_ocr_on_pdf(src_abs)
        if ocr_success:
            text = convert_to_markdown_raw(src_abs)
    return text


def format_size(bytes_size):
    if bytes_size < 1024 * 1024:
        return f"{bytes_size / 1024:.1f} KB"
    return f"{bytes_size / (1024 * 1024):.1f} MB"

def _convert_worker_target(src_abs, conn, enable_ocr=True):
    try:
        res = convert_to_markdown(src_abs, enable_ocr=enable_ocr)
        conn.send(("ok", res))
    except Exception as e:
        conn.send(("error", str(e)))
    finally:
        try:
            conn.close()
        except Exception:
            pass

def run_conversion_tasks(tasks, file_timeout=60, max_jobs=1, enable_ocr=True, total_count=None):
    if total_count is None:
        total_count = len(tasks)
        
    results = {}
    if not tasks:
        return results

    pending_tasks = list(tasks)
    active_jobs = []
    task_counter = 0

    while pending_tasks or active_jobs:
        while pending_tasks and len(active_jobs) < max_jobs:
            task = pending_tasks.pop(0)
            task_counter += 1
            idx = task.get("task_num", task_counter)
            src_rel = task["src_rel"]
            size_str = format_size(task["stat"].st_size)
            
            print(f"[{idx}/{total_count}] Converting {src_rel} ({size_str}) ...", flush=True)
            
            parent_conn, child_conn = multiprocessing.Pipe()
            proc = multiprocessing.Process(target=_convert_worker_target, args=(task["src_abs"], child_conn, enable_ocr))
            proc.start()
            child_conn.close()
            
            active_jobs.append({
                "task": task,
                "proc": proc,
                "parent_conn": parent_conn,
                "start_time": time.time(),
                "index": idx
            })

        finished_jobs = []
        for job in active_jobs:
            proc = job["proc"]
            parent_conn = job["parent_conn"]
            task = job["task"]
            src_rel = task["src_rel"]
            dest_rel = task["dest_rel"]
            idx = job["index"]
            elapsed = time.time() - job["start_time"]

            if parent_conn.poll(0.02):
                try:
                    status, payload = parent_conn.recv()
                    parent_conn.close()
                    proc.join(timeout=1)
                    if status == "ok":
                        results[src_rel] = {"success": True, "markdown_body": payload, "error": None}
                        print(f"[{idx}/{total_count}] Fertig: {src_rel} -> {dest_rel}", flush=True)
                    else:
                        results[src_rel] = {"success": False, "markdown_body": None, "error": payload}
                        print(f"[{idx}/{total_count}] Error converting {src_rel}: {payload}", flush=True)
                except Exception as e:
                    results[src_rel] = {"success": False, "markdown_body": None, "error": str(e)}
                    print(f"[{idx}/{total_count}] Error reading output for {src_rel}: {e}", flush=True)
                finished_jobs.append(job)
            elif not proc.is_alive():
                if parent_conn.poll(0.01):
                    try:
                        status, payload = parent_conn.recv()
                        parent_conn.close()
                        proc.join()
                        if status == "ok":
                            results[src_rel] = {"success": True, "markdown_body": payload, "error": None}
                            print(f"[{idx}/{total_count}] Fertig: {src_rel} -> {dest_rel}", flush=True)
                        else:
                            results[src_rel] = {"success": False, "markdown_body": None, "error": payload}
                            print(f"[{idx}/{total_count}] Error converting {src_rel}: {payload}", flush=True)
                    except Exception as e:
                        results[src_rel] = {"success": False, "markdown_body": None, "error": str(e)}
                        print(f"[{idx}/{total_count}] Error converting {src_rel}: {e}", flush=True)
                else:
                    parent_conn.close()
                    proc.join()
                    err_msg = f"Worker process terminated unexpectedly (exit code {proc.exitcode})."
                    results[src_rel] = {"success": False, "markdown_body": None, "error": err_msg}
                    print(f"[{idx}/{total_count}] Error converting {src_rel}: {err_msg}", flush=True)
                finished_jobs.append(job)
            elif elapsed > file_timeout:
                print(f"[TIMEOUT] Konvertierung fuer {src_rel} nach {file_timeout}s abgebrochen! Skipping.", file=sys.stderr, flush=True)
                try:
                    proc.terminate()
                    proc.join(timeout=2)
                    if proc.is_alive():
                        proc.kill()
                        proc.join()
                except Exception as e:
                    print(f"Warning terminating process for {src_rel}: {e}", file=sys.stderr, flush=True)
                parent_conn.close()
                results[src_rel] = {"success": False, "markdown_body": None, "error": f"Timeout nach {file_timeout}s"}
                finished_jobs.append(job)

        for fj in finished_jobs:
            active_jobs.remove(fj)

        time.sleep(0.05)

    return results

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
            
        filemap_data = {}
        if os.path.exists(filemap_json_abs):
            try:
                with open(filemap_json_abs, "r", encoding="utf-8") as f:
                    filemap_data = json.load(f)
            except Exception as e:
                print(f"Warning: Could not read filemap.json: {e}")
                
        if not filemap_data:
            filemap_data = {
                "project": project_id,
                "project_title": resolved["title"],
                "files": {}
            }
        files_in_json = filemap_data.setdefault("files", {})
        
        processed_mirrors = set()
        now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        
        candidate_tasks = []
        for root, dirs, filenames in os.walk(cloud_path_abs):
            for f in filenames:
                ext = os.path.splitext(f)[1].lower()
                if ext not in extensions:
                    continue
                if f.startswith(".") or f.startswith("~$") or f.lower() == "desktop.ini":
                    continue
                    
                src_abs = os.path.join(root, f)
                src_rel_workspace = os.path.relpath(src_abs, workspace_root).replace("\\", "/")
                
                rel_to_cloud = os.path.relpath(src_abs, cloud_path_abs)
                dest_rel_workspace = os.path.join(output_dir, os.path.splitext(rel_to_cloud)[0] + ".md").replace("\\", "/")
                dest_abs = os.path.normpath(os.path.join(workspace_root, dest_rel_workspace))
                
                processed_mirrors.add(dest_rel_workspace)
                
                stat = os.stat(get_safe_path(src_abs))
                src_mtime = datetime.datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M:%S")
                src_version = get_file_version(f)
                
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
                        
                candidate_tasks.append({
                    "src_abs": src_abs,
                    "src_rel": src_rel_workspace,
                    "dest_abs": dest_abs,
                    "dest_rel": dest_rel_workspace,
                    "src_mtime": src_mtime,
                    "src_version": src_version,
                    "stat": stat,
                    "needs_conversion": needs_conversion
                })

        total_files = len(candidate_tasks)
        tasks_to_convert = []
        count_skipped = 0
        count_converted = 0
        count_failed = 0

        for task_idx, t in enumerate(candidate_tasks, 1):
            t["task_num"] = task_idx
            if t["needs_conversion"]:
                tasks_to_convert.append(t)
            else:
                count_skipped += 1
                src_rel_workspace = t["src_rel"]
                dest_rel_workspace = t["dest_rel"]
                if src_rel_workspace in files_in_json:
                    files_in_json[src_rel_workspace]["markdown_mirror"] = dest_rel_workspace
                else:
                    files_in_json[src_rel_workspace] = {
                        "version": t["src_version"],
                        "mtime": t["src_mtime"],
                        "size": f"{t['stat'].st_size / 1024:.1f} KB",
                        "description": "-",
                        "markdown_mirror": dest_rel_workspace
                    }

        if tasks_to_convert:
            results = run_conversion_tasks(
                tasks_to_convert,
                file_timeout=args.file_timeout,
                max_jobs=args.jobs,
                enable_ocr=not args.no_ocr,
                total_count=total_files
            )

            for t in tasks_to_convert:
                src_rel_workspace = t["src_rel"]
                dest_rel_workspace = t["dest_rel"]
                dest_abs = t["dest_abs"]
                res = results.get(src_rel_workspace, {"success": False, "error": "Unknown error"})

                if res["success"]:
                    count_converted += 1
                    metadata = {
                        "original_file": src_rel_workspace,
                        "version": t["src_version"],
                        "conversion_date": now_str,
                        "file_date": t["src_mtime"],
                        "last_verified_date": now_str
                    }
                    os.makedirs(os.path.dirname(get_safe_path(dest_abs)), exist_ok=True)
                    write_markdown_file(dest_abs, metadata, res["markdown_body"])

                    if src_rel_workspace in files_in_json:
                        files_in_json[src_rel_workspace]["markdown_mirror"] = dest_rel_workspace
                    else:
                        files_in_json[src_rel_workspace] = {
                            "version": t["src_version"],
                            "mtime": t["src_mtime"],
                            "size": f"{t['stat'].st_size / 1024:.1f} KB",
                            "description": "-",
                            "markdown_mirror": dest_rel_workspace
                        }
                else:
                    count_failed += 1

        print(f"\nZusammenfassung fuer {sid}: {count_converted} erfolgreich konvertiert, {count_skipped} aktuell (uebersprungen), {count_failed} fehlgeschlagen/Timeout.")

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
