---
name: cloud-atlas
description: "Synchronisiert und kartografiert Projekt- und Topic-bezogene Cloud-Verzeichnisse in das lokale Workspace-Memory (Konvertierung + Filemaps)"
---

# cloud-atlas — Synchronisation und Kartografie von Cloud-Speichern

Dieser Skill verwaltet die Synchronisation, Konvertierung und Erstellung von Dateiverzeichnissen (Filemaps) für projekt- und topicbezogene Cloud-Speicher (Junctions) innerhalb des Agent-Workspaces.

---

## 1. Übersicht

Der Skill kapselt drei zusammenhängende Aufgaben:
1. **Dokumenten-Konvertierung**: Scannt einen Cloud-Speicher nach Dateitypen (`.pdf`, `.docx`, `.xlsx`, `.pptx`) und konvertiert sie mittels `markitdown` in lesbare Markdown-Kopien (Mirrors) im lokalen Workspace-Memory.
2. **Filemap-Generierung**: Erstellt eine strukturierte JSON-Datenbank (`filemap.json`) und eine lesbare Markdown-Tabelle (`filemap.md`) mit Metadaten (Größe, Version, Änderungsdatum, Beschreibung, Mirror-Link).
3. **Orphaned Cleanups**: Bereinigt automatisch verwaiste Markdown-Spiegelungen (wenn das Original-PDF in der Cloud gelöscht wurde) sowie leere Zwischenverzeichnisse.

---

## 2. Konfiguration & Datenmodell (Schema)

Die Synchronisationsparameter werden kanonisch im globalen Katalog des Workspaces gepflegt:
* Für Projekte in: `memory/references/projects/projects.json` (ID-basiert)
* Für Topics in: `memory/references/topics/topics.json` (ID-basiert)

### Das `cloud_sync`-Schema
Jedes Projekt oder Thema kann beliebig viele Cloud-Speicher besitzen. Der Eintrag `cloud_sync` muss **immer** ein Dictionary von Speicher-Konfigurationen sein (auch bei nur einem Speicher, z. B. mit der ID `"default"` oder `"meshe-teams"`):

```json
"cloud_sync": {
  "<storage_id>": {
    "scan_dir": "Relative path to cloud directory junction (e.g. data/cloud/MESHE)",
    "output_json": "Relative path to output filemap.json",
    "output_md": "Relative path to output filemap.md",
    "output_dir": "Relative path to local markdown mirror directory (e.g. memory/references/projects/meshe/cloud)"
  }
}
```

* **Namenskonventionen bei mehreren Speichern**: Wenn mehrere Speicher existieren (z. B. `"bokudrive"` und `"onedrive-legacy"`), sollten die Ausgabedateien (`filemap-bokudrive.json`/`.md` etc.) und Unterverzeichnisse (`cloud/bokudrive/`, `cloud/onedrive-legacy/`) getrennt benannt werden, um Dateikollisionen zu vermeiden.

---

## 3. CLI-Dokumentation (Ausführung)

Die Steuerung erfolgt über das zentrale Orchestrator-Skript `sync_project_cloud.py`. Dieses ruft nacheinander die Konvertierungs- und Mapping-Skripte auf.

### Speicherort der Skripte im Skill:
* Orchestrator: `skills/cloud-atlas/scripts/sync_project_cloud.py`
* Konvertierung: `skills/cloud-atlas/scripts/convert_cloud_docs.py`
* Generierung: `skills/cloud-atlas/scripts/gen_filemap.py`

### Aufrufe & Parameter

#### 1. Projekt synchronisieren (alle konfigurierten Speicher)
```bash
python .agents/skills/cloud-atlas/scripts/sync_project_cloud.py --project-id <project_id>
```
*Beispiel*: `python .agents/skills/cloud-atlas/scripts/sync_project_cloud.py --project-id week` (synchronisiert nacheinander `bokudrive` und `onedrive-legacy`).

#### 2. Topic/Thema synchronisieren (alle konfigurierten Speicher)
```bash
python .agents/skills/cloud-atlas/scripts/sync_project_cloud.py --topic-id <topic_id>
```
*Beispiel*: `python .agents/skills/cloud-atlas/scripts/sync_project_cloud.py --topic-id lifelong-learning` (synchronisiert `news-berichte`). Das Flag `--topic` wird bei Verwendung von `--topic-id` automatisch impliziert.

#### 3. Einzelnen Speicher gezielt synchronisieren
```bash
python .agents/skills/cloud-atlas/scripts/sync_project_cloud.py --project-id week --storage-id bokudrive
```

#### 4. Konvertierungen erzwingen
Standardmäßig werden nur neue oder geänderte Dokumente konvertiert. Um alle Dokumente erneut zu verarbeiten, füge das `--force`-Flag hinzu:
```bash
python .agents/skills/cloud-atlas/scripts/sync_project_cloud.py --project-id meshe --force
```

---

## 4. Operative Arbeitsregeln & Automatismen

Als Agent musst du folgende Abläufe bei der Arbeit mit Cloud-Speichern beachten:

### A. PDF-zu-Markdown-Konvertierung & die 24h-Regel
* **Keine Spiegelungen in der Cloud**: Konvertierte Markdown-Dateien dürfen niemals im Cloud-Speicher (Junction) abgelegt werden, sondern immer im lokalen Projektordner unter `cloud/` (bzw. dem konfigurierten `output_dir`).
* **Erforderliches Frontmatter**: Jede gespiegelte `.md`-Datei enthält Metadaten über das Original-PDF:
  ```yaml
  ---
  original_file: "data/cloud/MESHE/... (Pfad relativ zum Workspace-Root)"
  version: "Versionsnummer des Original-PDFs (z. B. v3, FINAL oder N/A)"
  conversion_date: "Datum/Uhrzeit der Konvertierung (YYYY-MM-DD HH:MM:SS)"
  file_date: "Änderungsdatum des Original-PDFs (YYYY-MM-DD HH:MM:SS)"
  last_verified_date: "Datum der letzten Gültigkeitsprüfung (YYYY-MM-DD HH:MM:SS)"
  ---
  ```
* **Gültigkeitsprüfung (24h-Regel)**:
  * Ist eine gespiegelte Markdown-Datei älter als 24 Stunden (gemessen an `last_verified_date`), muss vor der Arbeit mit ihr geprüft werden, ob im Cloud-Speicher eine neuere Version des Original-PDFs vorliegt.
  * Das Skript `convert_cloud_docs.py` erledigt dies automatisch: Bei unveränderten Quelldateien aktualisiert es lediglich das `last_verified_date` auf den aktuellen Prüfzeitpunkt, ohne das Dokument unnötig neu zu konvertieren.

### B. Manuelle Dokumentenbeschreibungen
* Die Datei `filemap.json` dient als Source of Truth für Dateimetadaten.
* **Arbeitsregel**: Wenn du eine Datei aus dem Cloud-Speicher analysiert oder verarbeitet hast, trage eine kurze, prägnante Zusammenfassung der Datei in `filemap.json` unter dem Schlüssel `"description"` beim jeweiligen Pfad ein.
* Generiere die Filemap anschließend neu (durch Aufruf von `sync_project_cloud.py`). Deine Beschreibung wird automatisch in die Tabelle der `filemap.md` übernommen.

### C. Unicode-Sicherheit
* Dateipfade und Terminalausgaben werden auf Windows-Systemen intern in UTF-8 verarbeitet, um Abstürze durch macOS-dekomponierte Sonderzeichen (NFD-Normalisierung, z. B. combining diaeresis `\u0308`) zu verhindern.
* In allen erzeugten Markdown-Dateien müssen echte deutsche Umlaute (`ä`, `ö`, `ü`, `Ä`, `Ö`, `Ü`, `ß`) anstelle von Umschreibungen verwendet werden.
