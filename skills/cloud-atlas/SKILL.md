---
name: cloud-atlas
description: "Synchronisiert und kartografiert Projekt- und Topic-bezogene Cloud-Verzeichnisse in das lokale Workspace-Memory (Konvertierung + Filemaps)"
---

# cloud-atlas — Synchronisation und Kartografie von Cloud-Speichern

Dieser Skill verwaltet die Synchronisation, Konvertierung und Erstellung von Dateiverzeichnissen (Filemaps) für projekt- und topicbezogene Cloud-Speicher (Junctions) innerhalb des Agent-Workspaces.

---

## 1. Übersicht

Der Skill kapselt drei zusammenhängende Aufgaben:
1. **Dokumenten-Konvertierung & Hänge-Schutz**: Scannt einen Cloud-Speicher nach Dateitypen (`.pdf`, `.docx`, `.xlsx`, `.pptx`) und konvertiert sie mittels `markitdown` in lesbare Markdown-Kopien (Mirrors) im lokalen Workspace-Memory. Konvertierungen laufen prozessual isoliert auf mehreren CPU-Kernen (Standard: 2 Kerne, `--jobs 2`) mit individuellem Datei-Timeout (`--file-timeout 60`).
2. **Filemap-Generierung**: Erstellt eine strukturierte JSON-Datenbank (`filemap.json`) und eine lesbare Markdown-Tabelle (`filemap.md`) mit Metadaten (Größe, Version, Änderungsdatum, Beschreibung, Mirror-Link).
3. **Orphaned Cleanups**: Bereinigt automatisch verwaiste Markdown-Spiegelungen (wenn das Original-PDF in der Cloud gelöscht wurde) sowie leere Zwischenverzeichnisse.

---

## 2. Konfiguration & Datenmodell (Schema)

Die Synchronisationsparameter werden kanonisch im globalen Katalog des Workspaces gepflegt:
* Für Projekte in: `memory/references/projects/projects.json` (ID-basiert)
* Für Topics in: `memory/references/topics/topics.json` (ID-basiert)

### Workspace-Anbindung der Cloud-Speicher

Cloud-Speicher sollen im Agent-Workspace als Link oder Junction unter `data/cloud/` eingebunden werden. Verwende dafür einen stabilen, sprechenden Namen nach dem Muster `data/cloud/<speicher-name>/`; der Name soll Herkunft oder Zweck des Speichers klar erkennen lassen.

Die konkrete technische Einrichtung des Links bzw. der Junction erfolgt im Workspace-Bootstrap oder durch die zuständige lokale Administration. `cloud_sync.scan_dir` verweist anschließend ausschließlich mit einem relativen Workspace-Pfad auf den eingebundenen Speicher. Lokale Markdown-Spiegelungen gehören nie in die verlinkte Cloud-Ablage, sondern in den konfigurierten lokalen `output_dir`.

> [!IMPORTANT]
> **.gitignore-Pflicht**: Das Verzeichnis `data/cloud/` (wo die Cloud-Junctions bzw. Symlinks liegen) muss zwingend sofort in die `.gitignore` des Ziel-Workspaces aufgenommen werden (`data/cloud/`), um zu verhindern, dass externe Cloud-Dateien oder Junction-Inhalte versehentlich in Git gestaged oder versioniert werden.

### Das `cloud_sync`-Schema
Jedes Projekt oder Thema kann beliebig viele Cloud-Speicher besitzen. Der Eintrag `cloud_sync` muss **immer** ein Dictionary von Speicher-Konfigurationen sein (auch bei nur einem Speicher, z. B. mit der ID `"default"` oder `"meshe-teams"`):

```json
"cloud_sync": {
  "<storage_id>": {
    "scan_dir": "Relative path to cloud directory junction (e.g. data/cloud/MESHE)",
    "output_json": "Relative path to output filemap.json (e.g. memory/cloud/projects/meshe/filemap.json)",
    "output_md": "Relative path to output filemap.md (e.g. memory/cloud/projects/meshe/filemap.md)",
    "output_dir": "Relative path to local markdown mirror directory (e.g. memory/cloud/projects/meshe/default oder memory/cloud/topics/<slug>/<storage_id>)",
    "last_synced_at": "Automated timestamp of last successful sync (e.g. 2026-08-05 21:09:12)"
  }
}
```

### Subtopic-Auflösung und Speicherzuordnung

Bei Arbeit an einem Subtopic den `cloud_sync` des übergeordneten Topics als potenzielle Kontextquelle berücksichtigen. Er kann relevante Dokumente enthalten, auch wenn sie nicht auf Ebene des Subtopics abgelegt oder benannt sind. Bei plausibler Relevanz den Topic-Filemap-Kontext oder passende übergeordnete Cloud-Spiegelungen prüfen.

Ein Cloud-Speicher kann bei Bedarf ausschließlich einem Subtopic zugeordnet werden. Dazu ist innerhalb des betreffenden `subtopics[]`-Eintrags ein eigenes `cloud_sync`-Dictionary im selben Schema zulässig. Die wirksamen Speicher eines Subtopics sind die Vereinigung aus:

1. den Speichern des übergeordneten Topics und
2. seinen explizit definierten Subtopic-Speichern.

Gleiche `storage_id`-Werte sind nur zulässig, wenn das Subtopic den Topic-Eintrag bewusst erweitert oder überschreibt; dies ist im Subtopic-Index kurz zu dokumentieren. Für klar abgegrenzte Inhalte bevorzugt einen eigenen `storage_id` und getrennte Filemap- sowie Mirror-Ausgabepfade.

* **Namenskonventionen bei mehreren Speichern**: Wenn mehrere Speicher existieren (z. B. `"bokudrive"` und `"onedrive-legacy"`), sollten die Ausgabedateien (`filemap-bokudrive.json`/`.md` etc.) und Unterverzeichnisse (`cloud/bokudrive/`, `cloud/onedrive-legacy/`) getrennt benannt werden, um Dateikollisionen zu vermeiden.

### Mirror-Pfad-Policy

`output_dir` ist die autoritative Trust-Grenze für lokale Markdown-Spiegelungen. Bei jeder Filemap-Regeneration wird für unterstützte Quelldateien zuerst der kanonische, tatsächlich vorhandene Mirror unter `output_dir` gewählt. Ein abweichender manueller Mirror-Pfad bleibt nur erhalten, wenn die Datei existiert und ebenfalls innerhalb desselben `output_dir` liegt. Fehlende, unsichere oder zonenüberschreitende Altpfade werden nicht in die neue Filemap übernommen. Generierte Markdown-Linkziele werden URL-kodiert, damit Leerzeichen, Unicode, Klammern und Markdown-Sonderzeichen sicher aufgelöst werden. Manuelle Beschreibungen und andere benutzerdefinierte Metadaten bleiben davon unberührt.

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

#### 5. Timeout & Parallelisierung (Multi-Core & Hänge-Schutz)
Standardmäßig verarbeitet der Konvertierungsprozess Dokumente mit 2 parallelen Jobs (`--jobs 2`) und einem maximalen Timeout von 60 Sekunden pro Einzeldatei (`--file-timeout 60`).
Diese Parameter können frei angepasst werden:
```bash
python .agents/skills/cloud-atlas/scripts/sync_project_cloud.py --project-id meshe --file-timeout 120 --jobs 4
```
* **Hänge-Schutz (`--file-timeout`):** Jede Konvertierung wird prozessual isoliert. Bei Zeitüberschreitung (z. B. durch 90-seitige PDF-Gutachten oder komplexe Tabellen) wird der jeweilige Worker-Prozess hart beendet (`terminate()`/`kill()`), eine Warnung ausgegeben und die betroffene Datei sauber übersprungen.
* **Multi-Core Parallelisierung (`--jobs N` / `-j N`):** Verarbeitet bis zu `N` Dokumente zeitgleich auf `N` CPU-Kernen (Standard: 2).

#### 6. Automatisches OCR-Fallback für gescannte PDFs
Reine Bild-Scans (z. B. iOS Kamera-Scans/Dateien-App) besitzen initial keine Textschicht. `convert_cloud_docs.py` erkennt dies automatisch:
* **Automatisches OCR:** Sobald aus einer PDF-Datei weniger als 30 Zeichen extrahiert werden können, führt das Skript im Hintergrund automatisch `ocrmypdf -l deu --skip-text` auf der Quelldatei aus.
* **Original-PDF Anreicherung:** Die OCR-Textschicht wird unsichtbar direkt im Original-PDF hinterlegt (Sandwich-PDF). Das visuelle Erscheinungsbild bleibt zu 100 % unverändert, aber das Original-PDF ist ab sofort im gesamten System durchsuchbar und wird vollständig in Markdown gespiegelt.
* **OCR deaktivieren (`--no-ocr`):** Über das Flag `--no-ocr` kann die automatische OCR-Texterkennung bei Bedarf deaktiviert werden.

### Systemvoraussetzungen / Requirements
* **Python-Pakete:** `markitdown>=0.1.0`, `ocrmypdf>=17.0.0` (siehe `requirements.txt`).
* **OCR-Engine:** Tesseract OCR v5+ mit deutschem Sprachpaket (`tessdata/deu.traineddata`).
  * *Windows:* `winget install UB-Mannheim.TesseractOCR` (wird automatisch in den Standardpfaden erkannt).
  * *Linux / WSL:* `sudo apt install ocrmypdf tesseract-ocr-deu`.

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

### B. Task-gebundene Zeitstempel-Prüfung (`last_synced_at`)
* **Aufgaben-gekoppelte Aktualitätsprüfung**: Sobald eine Aufgabe den Zugriff auf Dokumente aus einem Cloud-Speicher erfordert, ist vor der Dokumentenanalyse der Zeitstempel `last_synced_at` im `cloud_sync`-Objekt (`projects.json` / `topics.json`) zu prüfen.
* **Gültigkeitsfenster & Re-Sync Trigger**:
  * Für dynamische Arbeits- und Posteingangsverzeichnisse: Ist `last_synced_at` älter als 6 Stunden, führe vor dem Zugriff auf die Dokumente eine Synchronisation durch (`sync_project_cloud.py`).
  * Für statische Referenz- und Archivspeicher: Ist `last_synced_at` älter als 12 bis 24 Stunden, führe vorab eine Synchronisation durch.
* **Fokussierte Ausführung**: Die Prüfung erfolgt ausschließlich task-gebunden beim konkreten Arbeiten mit Cloud-Dokumenten, nicht bei allgemeinen Workspace-Interaktionen ohne Cloud-Bezug.

### C. Manuelle Dokumentenbeschreibungen
* Die Datei `filemap.json` dient als Source of Truth für Dateimetadaten.
* **Arbeitsregel**: Wenn du eine Datei aus dem Cloud-Speicher analysiert oder verarbeitet hast, trage eine kurze, prägnante Zusammenfassung der Datei in `filemap.json` unter dem Schlüssel `"description"` beim jeweiligen Pfad ein.
* Generiere die Filemap anschließend neu (durch Aufruf von `sync_project_cloud.py`). Deine Beschreibung wird automatisch in die Tabelle der `filemap.md` übernommen.

### D. Unicode-Sicherheit
* Dateipfade und Terminalausgaben werden auf Windows-Systemen intern in UTF-8 verarbeitet, um Abstürze durch macOS-dekomponierte Sonderzeichen (NFD-Normalisierung, z. B. combining diaeresis `\u0308`) zu verhindern.
* In allen erzeugten Markdown-Dateien müssen echte deutsche Umlaute (`ä`, `ö`, `ü`, `Ä`, `Ö`, `Ü`, `ß`) anstelle von Umschreibungen verwendet werden.

---

## 5. Pflicht bei Integration in Agent-Workspaces

Bei der Erstinstallation des Skills in einem Agent-Workspace müssen die folgenden Regeln zwingend in die `AGENTS.md` des Ziel-Workspaces übernommen werden (z. B. unter den Arbeitsregeln für den Projektmodus):

### Vorlage für die Ziel-`AGENTS.md`
```markdown
### Hybrid-Modell für Cloud-Filemaps (`cloud-atlas`)

Um Dateiverteilungen in den Cloud-Speichern sauber zu dokumentieren, nutzt dieser Workspace das `cloud-atlas`-Modell:
* **Source of Truth & View**: Eine `filemap.json` speichert Dateimetadaten und manuelle Beschreibungen (`"description"`), woraus eine Markdown-Tabelle (`filemap.md`) generiert wird.
* **Lokale Spiegelung**: Konvertierte Markdown-Kopien (via `markitdown`) liegen lokal unter `memory/cloud/` (niemals direkt im Cloud-Speicher).
* **.gitignore-Schutz**: Das Verzeichnis `data/cloud/` für Cloud-Junctions wird zwingend in `.gitignore` eingetragen (`data/cloud/`).
* **Automatisierung (24h-Regel & Cleanups)**: Die Prüfung auf Dateiversionen, Neukonvertierungen bei Quelländerungen, die task-gebundene Zeitstempel-Kontrolle (`last_synced_at` >6h/12h bei Cloud-Zugriff) und die Bereinigung verwaister Dateien erfolgen über den Skill **`cloud-atlas`**.
* **Ausführliche Regeln**: Alle prozeduralen Abläufe und CLI-Aufrufe sind dokumentiert in [.agents/skills/office-intelligence/skills/cloud-atlas/SKILL.md](file:///d:/users/dagobert/agents/mayr-ps/.agents/skills/office-intelligence/skills/cloud-atlas/SKILL.md).
```
