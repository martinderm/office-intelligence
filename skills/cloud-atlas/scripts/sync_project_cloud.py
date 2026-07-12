#!/usr/bin/env python3
import sys
import argparse
import subprocess
import os

def parse_args():
    parser = argparse.ArgumentParser(description="Synchronisiert den Cloud-Speicher eines Projekts oder Themas (Konvertierung + Filemap).")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--project-id", help="Projekt-ID (z. B. meshe)")
    group.add_argument("--topic-id", help="Topic-ID (z. B. lifelong-learning)")
    parser.add_argument("--force", action="store_true", help="Alle Konvertierungen erzwingen")
    parser.add_argument("--topic", action="store_true", help="Erzwinge die Behandlung als Topic")
    parser.add_argument("--storage-id", required=False, help="Optionale Storage-ID bei mehreren Cloud-Speichern")
    return parser.parse_args()

def main():
    args = parse_args()
    scripts_dir = os.path.dirname(os.path.abspath(__file__))
    
    target_flag = "--project-id" if args.project_id else "--topic-id"
    target_id = args.project_id or args.topic_id
    
    # 1. Run convert_cloud_docs.py
    cmd_convert = [
        sys.executable,
        os.path.join(scripts_dir, "convert_cloud_docs.py"),
        target_flag, target_id
    ]
    if args.force:
        cmd_convert.append("--force")
    if args.topic:
        cmd_convert.append("--topic")
    if args.storage_id:
        cmd_convert.extend(["--storage-id", args.storage_id])
        
    print(f"=== Schritt 1: Konvertiere Cloud-Dokumente fuer ID '{target_id}' ===")
    res_convert = subprocess.run(cmd_convert)
    if res_convert.returncode != 0:
        print("Fehler beim Konvertieren der Cloud-Dokumente. Abbruch.")
        sys.exit(res_convert.returncode)
        
    # 2. Run gen_filemap.py
    cmd_map = [
        sys.executable,
        os.path.join(scripts_dir, "gen_filemap.py"),
        target_flag, target_id
    ]
    if args.topic:
        cmd_map.append("--topic")
    if args.storage_id:
        cmd_map.extend(["--storage-id", args.storage_id])
        
    print(f"\n=== Schritt 2: Generiere Filemap fuer ID '{target_id}' ===")
    res_map = subprocess.run(cmd_map)
    if res_map.returncode != 0:
        print("Fehler beim Generieren der Filemap.")
        sys.exit(res_map.returncode)
        
    print("\n=== Cloud-Synchronisation erfolgreich abgeschlossen! ===")

if __name__ == "__main__":
    main()
