---
name: cloud-atlas
description: "Synchronisiert und kartografiert Projekt- und Topic-bezogene Cloud-Verzeichnisse in das lokale Workspace-Memory (Konvertierung + Filemaps inkl. kontrollierter .doc-Unterstützung)"
---

# cloud-atlas — Synchronisation und Kartografie von Cloud-Speichern

Dieser Skill verwaltet die Synchronisation, Konvertierung und Erstellung von Dateiverzeichnissen (Filemaps) für projekt- und topicbezogene Cloud-Speicher (Junctions) innerhalb des Agent-Workspaces.

---

## 1. Übersicht

Der Skill kapselt folgende Aufgaben:
1. **Dokumenten-Konvertierung & Hänge-Schutz**:
   - Scannt einen Cloud-Speicher nach Standard-Dateitypen (`.pdf`, `.docx`, `.xlsx`, `.pptx`) und konvertiert sie direkt mittels `markitdown` in lesbare Markdown-Kopien (Mirrors) im lokalen Workspace-Memory.
   - **Kontrollierte `.doc`-Unterstützung (Word 97–2003)**: Binäre Legacy-`.doc`-Dateien werden in einem kontrollierten 2-Stufen-Verfahren über LibreOffice (`soffice`) oder Word COM in ein separates `.docx`-Derivat unter `_derivatives/` konvertiert und daraus der Markdown-Spiegel erzeugt.
   - Konvertierungen laufen prozessual isoliert auf mehreren CPU-Kernen (Standard: 2 Kerne, `--jobs 2`) mit individuellem Datei-Timeout (`--file-timeout 60`).
2. **Filemap- & Manifest-Generierung**: Erstellt eine strukturierte JSON-Datenbank (`filemap.json`) und eine lesbare Markdown-Tabelle (`filemap.md`) mit Metadaten (SHA-256, Größe, Version, Änderungsdatum, Beschreibung, Mirror-Link, Derivat-Link und Konvertierungsstatus).
3. **Fallback & Katalogisierung (`conversion_required`)**: Fehlt ein Konverter für `.doc`-Dateien oder scheitert die Prüfung beschädigter Dokumente, bricht der Scan nicht ab; die Datei wird vollständig katalogisiert und als `conversion_required` markiert.
4. **Orphaned Cleanups**: Bereinigt automatisch verwaiste Markdown-Spiegelungen und `.docx`-Derivate (wenn das Original in der Cloud gelöscht wurde) sowie leere Zwischenverzeichnisse.

---

## 2. Konfiguration & Datenmodell (Schema)

Die Synchronisationsparameter werden kanonisch im globalen Katalog des Workspaces gepflegt:
* Für Projekte in: `memory/references/projects/projects.json` (ID-basiert)
* Für Topics in: `memory/references/topics/topics.json` (ID-basiert)

### Workspace-Anbindung der Cloud-Speicher

Cloud-Speicher sollen im Agent-Workspace als Link oder Junction unter `data/cloud/` eingebunden werden. Verwende dafür einen stabilen, sprechenden Namen nach dem Muster `data/cloud/<speicher-name>/`; der Name soll Herkunft oder Zweck des Speichers klar erkennen lassen.

Die konkrete technische Einrichtung des Links bzw. der Junction erfolgt im Workspace-Bootstrap oder durch die zuständige lokale Administration. `cloud_sync.scan_dir` verweist anschließend ausschließlich mit einem relativen Workspace-Pfad auf den eingebundenen Speicher. Lokale Markdown-Spiegelungen und Zwischenderivate gehören nie in die verlinkte Cloud-Ablage, sondern in den konfigurierten lokalen `output_dir`.

> [!IMPORTANT]
> **.gitignore-Pflicht**: Das Verzeichnis `data/cloud/` (wo die Cloud-Junctions bzw. Symlinks liegen) muss zwingend sofort in die `.gitignore` des Ziel-Workspaces aufgenommen werden (`data/cloud/`), um zu verhindern, dass externe Cloud-Dateien oder Junction-Inhalte versehentlich in Git gestaged oder versioniert werden.
> 
> **Schutz der Cloud-Originale**: Originale im Cloud-Verzeichnis (`data/cloud/...`) dürfen unter keinen Umständen überschrieben, modifiziert oder gelöscht werden. Konvertierungsderivate werden strikt lokal unter `memory/cloud/.../_derivatives/` abgelegt.

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

### Manifest- und Metadaten-Verknüpfung in `filemap.json`

Jede Datei wird mit kryptografischem SHA-256 Hash und detailliertem Status erfasst. Bei `.doc`-Dateien werden Original, Derivat und Konvertierungsmethode eindeutig verknüpft:

```json
{
  "files": {
    "data/cloud/MESHE/Vertrag_v2.doc": {
      "version": "2",
      "mtime": "2026-08-28 10:15:00",
      "size": "45.2 KB",
      "sha256": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
      "description": "Hauptvertrag",
      "markdown_mirror": "memory/cloud/projects/meshe/Vertrag_v2.md",
      "conversion_status": "converted",
      "derivative": {
        "path": "memory/cloud/projects/meshe/_derivatives/Vertrag_v2.docx",
        "sha256": "ca978112ca1bbdcafac231b39a23dc4da786081cd1e14eed6e27e6a405870dbd",
        "format": "docx",
        "conversion_method": "libreoffice-headless",
        "converted_at": "2026-08-28 11:00:00",
        "potential_quality_loss": "Konvertierung von binärem .doc (Word 97-2003) über libreoffice-headless nach .docx. Formatierungen, Makros oder eingebettete OLE-Objekte können vom Original abweichen."
      }
    },
    "data/cloud/MESHE/Archiv_Unkonvertiert.doc": {
      "version": "N/A",
      "mtime": "2026-08-20 09:00:00",
      "size": "120.0 KB",
      "sha256": "f2ca1bb6c7e907d06dafe4687e579fce76b37e4e93b7605022da52e6ccc26fd2",
      "description": "-",
      "conversion_status": "conversion_required",
      "conversion_error": "No suitable converter found (LibreOffice or Microsoft Word required for .doc conversion)"
    }
  }
}
```

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

#### 2. Topic/Thema synchronisieren (alle konfigurierten Speicher)
```bash
python .agents/skills/cloud-atlas/scripts/sync_project_cloud.py --topic-id <topic_id>
```

#### 3. Einzelnen Speicher gezielt synchronisieren
```bash
python .agents/skills/cloud-atlas/scripts/sync_project_cloud.py --project-id week --storage-id bokudrive
```

#### 4. Konvertierungen erzwingen
```bash
python .agents/skills/cloud-atlas/scripts/sync_project_cloud.py --project-id meshe --force
```

#### 5. Timeout & Parallelisierung (Multi-Core & Hänge-Schutz)
```bash
python .agents/skills/cloud-atlas/scripts/sync_project_cloud.py --project-id meshe --file-timeout 120 --jobs 4
```
* **Hänge-Schutz (`--file-timeout`):** Jede Konvertierung wird prozessual isoliert. Bei Zeitüberschreitung wird der jeweilige Worker-Prozess hart beendet (`terminate()`/`kill()`), eine Warnung ausgegeben und die betroffene Datei mit `conversion_required` markiert.
* **Multi-Core Parallelisierung (`--jobs N` / `-j N`):** Verarbeitet bis zu `N` Dokumente zeitgleich auf `N` CPU-Kernen (Standard: 2).

#### 6. Konverter-Voraussetzungen für `.doc`
Für die Konvertierung alter Word 97–2003 `.doc`-Dateien:
* **LibreOffice (empfohlen, cross-platform):** `soffice.com` / `soffice` (wird automatisch in PATH und Standard-Installationspfaden erkannt).
  * *Windows:* `winget install TheDocumentFoundation.LibreOffice`
  * *Linux / Ubuntu:* `sudo apt install libreoffice-writer`
* **Microsoft Word (Windows-Fallback):** MS Word COM-Automation über PowerShell.

---

## 4. Operative Arbeitsregeln & Automatismen

### A. Frontmatter-Standard für gespiegelte Dokumente
Jede gespiegelte `.md`-Datei enthält vollständige Metadaten und Hashes zur lückenlosen Nachvollziehbarkeit (Dual Evidence):
```yaml
---
original_file: "data/cloud/MESHE/Vertrag_v2.doc"
original_sha256: "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
version: "2"
derivative_file: "memory/cloud/projects/meshe/_derivatives/Vertrag_v2.docx"
derivative_sha256: "ca978112ca1bbdcafac231b39a23dc4da786081cd1e14eed6e27e6a405870dbd"
conversion_method: "libreoffice-headless"
conversion_date: "2026-08-28 11:00:00"
file_date: "2026-08-28 10:15:00"
last_verified_date: "2026-08-28 11:00:00"
potential_quality_loss: "Konvertierung von binärem .doc (Word 97-2003) über libreoffice-headless nach .docx. Formatierungen, Makros oder eingebettete OLE-Objekte können vom Original abweichen."
---
```

### B. Gültigkeitsprüfung & 24h-Regel
* Ist eine gespiegelte Markdown-Datei älter als 24 Stunden (gemessen an `last_verified_date`), prüft `convert_cloud_docs.py` anhand von `mtime` und `original_sha256`, ob im Cloud-Speicher eine neuere Version vorliegt.
* Bei unveränderten Dateien wird lediglich das `last_verified_date` aktualisiert, ohne unnötige Neukonvertierung.

### C. Umgang mit Status `conversion_required`
* Dateien, die mangels Konverter oder wegen Dateibeschädigung nicht automatisch gespiegelt werden konnten, verbleiben mit `conversion_status: "conversion_required"` in der Filemap.
* Sobald ein Konverter installiert ist oder die Datei repariert wurde, führt der nächste Lauf von `sync_project_cloud.py` (oder `--force`) die Konvertierung automatisch nach.

---

## 5. Pflicht bei Integration in Agent-Workspaces

### Vorlage für die Ziel-`AGENTS.md`
```markdown
### Hybrid-Modell für Cloud-Filemaps (`cloud-atlas`)

Um Dateiverteilungen in den Cloud-Speichern sauber zu dokumentieren, nutzt dieser Workspace das `cloud-atlas`-Modell:
* **Source of Truth & View**: Eine `filemap.json` speichert Dateimetadaten, SHA-256, Konvertierungs-Status und manuelle Beschreibungen (`"description"`), woraus eine Markdown-Tabelle (`filemap.md`) generiert wird.
* **Lokale Spiegelung & Derivate**: Konvertierte Markdown-Kopien liegen lokal unter `memory/cloud/` (niemals direkt im Cloud-Speicher). `.doc`-Derivate werden isoliert unter `memory/cloud/.../_derivatives/` abgelegt.
* **.gitignore-Schutz**: Das Verzeichnis `data/cloud/` für Cloud-Junctions wird zwingend in `.gitignore` eingetragen (`data/cloud/`).
* **Automatisierung (24h-Regel, Cleanups & Fallbacks)**: Die Prüfung auf Dateiversionen, Neukonvertierungen bei Quelländerungen, die task-gebundene Zeitstempel-Kontrolle (`last_synced_at` >6h/12h bei Cloud-Zugriff) und die Bereinigung verwaister Dateien erfolgen über den Skill **`cloud-atlas`**.
* **Ausführliche Regeln**: Alle prozeduralen Abläufe und CLI-Aufrufe sind dokumentiert in [.agents/skills/office-intelligence/skills/cloud-atlas/SKILL.md](file:///d:/users/dagobert/agents/skills/office-intelligence/skills/cloud-atlas/SKILL.md).
```
