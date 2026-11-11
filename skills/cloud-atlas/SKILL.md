---
name: cloud-atlas
description: "Synchronisiert und kartografiert Projekt- und Topic-bezogene Cloud-Verzeichnisse in das lokale Workspace-Memory (Konvertierung + Filemaps inkl. kontrollierter .doc-Unterstützung)"
---

# cloud-atlas — Synchronisation und Kartografie von Cloud-Speichern

Dieser Skill verwaltet die Synchronisation, Konvertierung und Erstellung von Dateiverzeichnissen (Filemaps) für projekt- und topicbezogene Cloud-Speicher (Junctions) innerhalb des Agent-Workspaces.

Das maschinenlesbare Container-Schema für `filemap.json` und die zugehörige
Schemaentscheidung stehen in [references/filemap.schema.json](references/filemap.schema.json)
und [references/filemap-schema.md](references/filemap-schema.md).
Für versionierte manuelle Metadaten pro Storage ist zusätzlich
[references/filemap-curation.schema.json](references/filemap-curation.schema.json)
mit der [Curation-Overlay-Referenz](references/filemap-curation.md) zu lesen.

---

## 1. Übersicht

Der Skill kapselt folgende Aufgaben:
1. **Dokumenten-Konvertierung & Hänge-Schutz**:
   - Scannt einen Cloud-Speicher nach Standard-Dateitypen (`.pdf`, `.docx`, `.xlsx`, `.pptx`) und kann sie mittels `markitdown` in lesbare Markdown-Kopien (Mirrors) im lokalen Workspace-Memory konvertieren.
   - **Nicht materialisierte Bildverweise**: Gibt MarkItDown für ein eingebettetes Bild nur einen lokalen relativen Verweis aus, dessen Asset beim Mirror fehlt, wird genau dieser Verweis als nicht klickbarer Provenienzmarker mit Alttext und ursprünglichem Ziel gespeichert. Bestehende Bilddateien sowie normale Markdown-Links, URI-Links und Anker bleiben unverändert.
   - **Kontrollierte `.doc`-Unterstützung (Word 97–2003)**: Binäre Legacy-`.doc`-Dateien werden in einem kontrollierten 2-Stufen-Verfahren über LibreOffice (`soffice`) oder Word COM in ein separates `.docx`-Derivat unter `_derivatives/` konvertiert und daraus der Markdown-Spiegel erzeugt. Bei überlangen Windows-Pfaden (oder einer eindeutigen LibreOffice-Pfadlängenmeldung) nutzt der Konverter automatisch eine isolierte Kurzpfad-Kopie und promotet nur das geprüfte Derivat atomar; die Cloud-Quelle bleibt unverändert.
   - **Kontrollierte PDF-OCR & Policy-Steuerung (`--ocr-policy`)**:
     - *`local_derivative` (Standard & sichere Voreinstellung)*: Bei bildbasierten PDFs wird das OCR-Ergebnis als durchsuchbares PDF unter `_derivatives/` abgelegt; das Original im Cloud-Speicher bleibt 100% unberührt.
     - *`enrich_source`*: Kontrollierte In-place-Anreicherung des Cloud-Originals nur bei beschreibbarem Speicher und nicht signierten PDFs.
     - *`disabled` / `--no-ocr`*: Deaktiviert jegliche OCR-Verarbeitung.
     - *Präzise Scan-Erkennung & Signaturschutz*: Kurze 1-Zeiler-Digital-PDFs mit echten Schriften werden geschützt und nicht fehlerhaft als Scan behandelt. Bereits OCR-te, **getaggte** PDFs mit Textebene (auch mit sehr kurzem Text) gelten als digital: Die Tagged-Erkennung läuft PyMuPDF-first über `/StructTreeRoot` und `/MarkInfo` am Katalog, mit einem Rohbyte-Fallback nur wenn PyMuPDF fehlt oder scheitert. Digital signierte PDFs werden vor In-place-Mutationen strikt geschützt.
   - Konvertierungen laufen prozessual isoliert auf mehreren CPU-Kernen (Standard: 2 Kerne, `--jobs 2`) mit individuellem Datei-Timeout (`--file-timeout 60`).
2. **Filemap- & Manifest-Generierung**: Erstellt eine strukturierte JSON-Datenbank (`filemap.json`) und eine lesbare Markdown-Tabelle (`filemap.md`) mit Metadaten (SHA-256, Größe, Version, Änderungsdatum, Beschreibung, Mirror-Link, Derivat-Link und Konvertierungsstatus).
3. **Fallback & Katalogisierung (`conversion_required`)**: Fehlt ein Konverter für `.doc`-Dateien, scheitert die OCR-Verarbeitung oder ist ein PDF digital signiert bei `enrich_source`, bricht der Scan nicht ab; die Datei wird vollständig katalogisiert und als `conversion_required` markiert.
4. **Orphaned Cleanups**: Bereinigt automatisch verwaiste Markdown-Spiegelungen und Derivate unter `_derivatives/` (wenn das Original in der Cloud gelöscht wurde) sowie leere Zwischenverzeichnisse. Schlägt die Konvertierung einer weiterhin inventarisierten Quelle fehl (`conversion_required` oder `failed`), bleiben ihr Mirror und ihr Derivat erhalten; ein nicht mehr zum aktuellen Quellhash passender Mirror wird im Filemap-Eintrag mit `mirror_stale: true` markiert statt als aktuell ausgegeben.

### Mirror-Zielkollisionen

Vor jedem Write planen Konverter und Filemap-Generator dieselben Mirror-Ziele und
prüfen sie fail-closed auf Output-Zone und case-insensitive Eindeutigkeit. Quellen,
deren bisheriger Pfad `relative/stem.md` innerhalb desselben Storage-Scans kollidieren
wäre, erhalten ausnahmslos `relative/original.ext.md`; nicht kollidierende Quellen
behalten ihren bisherigen Pfad. Ein vorhandener expliziter Custom-Mirror bleibt nur
innerhalb der Output-Zone und ohne Zielkollision erhalten. Derivate und OCR-Ziele
bleiben in diesem Paket unverändert: für sie liegt keine analoge reale Kollision vor.

### Optionale Konvertierungs- und OCR-Voraussetzungen

Der Office-Intelligence-Router und alle Nicht-Cloud-Desks benötigen **keine**
Konvertierungspakete. Dokumentkonvertierung ist eine optionale Cloud-Atlas-Funktion;
sie wird bewusst nur bei Bedarf installiert:

```bash
python -m pip install -r .agents/skills/cloud-atlas/requirements-conversion.txt
```

`requirements-conversion.txt` enthält `markitdown` (Python-Bibliothek; alternativ
kann ein kompatibles `markitdown`-CLI im `PATH` bereitstehen), `ocrmypdf` (stellt
das `ocrmypdf`-CLI bereit) und `pymupdf` (rein lesende AcroForm-Extraktion, siehe
unten). Für OCR müssen zusätzlich hostseitig **Ghostscript** sowie
**Tesseract** mit den benötigten Sprachdaten verfügbar sein; der aktuelle Aufruf nutzt
`-l deu` und benötigt deshalb insbesondere das deutsche Tesseract-Sprachpaket. Diese
Systemvoraussetzungen werden weder vom Bundle noch von der Python-Requirements-Datei
installiert. Für Legacy-`.doc` ist außerdem LibreOffice (`soffice`, bevorzugt) oder
unter Windows Microsoft Word mit PowerShell-COM erforderlich.

Vor einem tatsächlich benötigten OCR-Batch prüft Cloud Atlas einmalig die reale
Runtime: OCRmyPDF, Tesseract, die Sprache `deu` und einen disposablen hOCR-Probeaufruf
gegen den konfigurierten tessdata-Baum. Digitale oder mit `--no-ocr` ausgeschlossene
PDFs lösen diesen Preflight nicht aus. Fehlt eine dieser optionalen Fähigkeiten,
bleibt der Scan deterministisch: Das
Original wird katalogisiert mit `conversion_status: "conversion_required"`, andere
Dateien und weitere Storages laufen weiter, und es wird kein freier
Installationsbefehl ausgegeben. Im `--json`-Envelope ist dies ein nicht-erfolgreicher
Zustand `state: "ConversionRequired"` mit `error.type: "ConversionRequired"` und
maschinell lesbaren `error.requirements` (jeweils `storage_id`, `source`,
`capability`, `message`; bei OCR insbesondere `tesseract_deu` oder `tesseract_hocr`). Der Aufrufer entscheidet anschließend ausdrücklich, ob er
die optionale Funktion bereitstellt oder die Datei unverändert katalogisiert lässt.

### PDF-Formularfelder (AcroForm) — optionaler Capability-Vertrag

MarkItDown liest gefüllte AcroForm-Widget-Werte nicht mit. Cloud Atlas ergänzt sie
deshalb rein lesend über **PyMuPDF** (`import pymupdf`, mit Fallback auf den
Legacy-Alias `import fitz`) als Abschnitt „Formularfelder (ausgefüllt)“
(Tabelle Seite/Feld/Typ/Wert) am Mirror. PyMuPDF ist **optional**: fehlt es oder
lässt es sich nicht importieren, bleibt der Mirror unverändert und der Lauf gilt
weiterhin als erfolgreich (kein `conversion_required`). Jede Extraktion liefert einen
begrenzten Status:

* `complete` — alle Widgets ohne Lesefehler gelesen, mindestens ein gefülltes Feld;
* `no_fields` — Dokument geöffnet, aber kein gefülltes Feld;
* `partial` — einzelne Seiten/Widgets unlesbar, Feld-/Warnungs-/Text-/Name-/Typlimits erreicht;
* `dependency_missing` — `pymupdf`/`fitz` fehlt oder ist nicht importierbar (auch Nicht-`ImportError`);
* `error` — PDF nicht öffnbar oder Dokument-Iteration ohne gelesene Felder abgebrochen
  (wird **nicht** als leere Extraktion dargestellt); mit bereits gelesenen Feldern ⇒ `partial`.

Alle Widget-Metadaten (`field_value`/`field_name`/`field_type_string`), die lazy
Widget-Iteration (`page.widgets()`) und die lazy Dokument-Iteration sind einzeln
guardiert: ein defektes Widget wird zu einer begrenzten Warnung, gute Felder bleiben
erhalten, das Dokument-Handle wird immer geschlossen.

Der Status wird zusammen mit `count`, `tool`/`tool_version`, begrenzten `warnings`,
dem Quell-`source_sha256` und `extracted_before_ocr` als `form_fields`-Objekt durch
Worker und Scheduler transportiert und **im Filemap-Eintrag** (`files[<quelle>]
.form_fields`) persistiert. Bewusste Grenzen:

* **Kein Frontmatter-Wachstum.** Das kanonische Frontmatter behält exakt seine
  Pflichtschlüssel. Für den zusammengesetzten Mirror (MarkItDown-Body + AcroForm-Tabelle)
  bleibt `converter` die **Basis-Konvertierungsmethode** (z. B. `markitdown-direct`);
  die PyMuPDF-Herkunft lebt ausschließlich im Filemap-`form_fields`-Objekt.
* **Reserviert & Retention.** `form_fields` ist ein generated Feld: ein Curation-Overlay
  darf es weder über `custom` setzen noch überschreiben. Bei **unverändertem** Quell-Hash
  wird der bestehende Vertrag im Skip-Pfad und in `gen_filemap.py` explizit
  weitergetragen; bei geändertem/neuem Quell-Hash wird er verworfen.
* **Literal & einzeilig.** Feldname, Typ und Wert werden einzeilig und literal
  dargestellt (Zeilenumbrüche/Steuerzeichen sowie U+2028/U+2029 zu Leerzeichen;
  Nicht-umbrechende Leerzeichen bleiben erhalten; `None` ⇒ leere Zelle; HTML
  entity-escaped; Markdown-Metazeichen escaped), damit weder Zeilenumbrüche noch
  Markdown-Bilder/HTML den Mirror umstrukturieren. `Off`/`/Off` wird **exakt** und nur
  für Button-Familien (Checkbox/Radio/Button) als ungeprüft verworfen; bei
  Text-/Choice-Feldern und benutzerdefinierten Checked-Werten wie `OFF`/`off` bleibt
  der Wert erhalten.
* **Reihenfolge vor destruktiver OCR.** Bei `enrich_source` werden die Felder **vor**
  der In-Place-OCR aus dem Original gelesen und mit dem Pre-OCR-Hash geführt
  (`extracted_before_ocr: true`), weil die OCR-Neuschreibung Widget-Werte verlieren
  kann. Bei `local_derivative` werden die Felder weiterhin aus dem unveränderten
  Original gelesen, nie aus dem Derivat.
* **Begrenzungen.** Feldanzahl, Wert-, Namens- und Typlänge sowie Warnungsanzahl sind
  begrenzt (`MAX_PDF_FORM_FIELDS`, `MAX_PDF_FORM_FIELD_TEXT`,
  `MAX_PDF_FORM_FIELD_NAME`, `MAX_PDF_FORM_FIELD_TYPE`, `MAX_PDF_FORM_FIELD_WARNINGS`);
  überschreitende Fälle werden als `partial` mit `*_truncated`-Warnung geführt, auch
  wenn das Warnungsbudget erschöpft ist.
* **Kein automatischer Rebuild.** Bereits gespiegelte unveränderte Quellen werden
  nicht automatisch nachgerüstet. Ein gezielter `--force`-Lauf (bzw. ein `redo_ocr`-Lauf)
  erzeugt die Mirrors neu; es gibt keinen impliziten Live-Rebuild.

### Mutations- und Lock-Vorbedingung

`sync_project_cloud.py`, `convert_cloud_docs.py` und `gen_filemap.py` mutieren je nach
Lauf lokale Mirrors, Derivate, Filemaps, Katalogzeitstempel und bei explizitem
`enrich_source` ein Cloud-PDF. Vor jedem solchen Lauf ist der **konsumierende
Ziel-Workspace** mittels `workspace-lock` zu sperren. Die Lease muss dem ausführenden
Harness gehören. Ein aktiver fremder Lock (Tier 1) stoppt den Lauf. Ein eindeutig stale
Lock darf nur über das reguläre Tier-2-Takeover von `workspace-lock` übernommen werden;
ein Force-Unlock/-Override (Tier 3) erfordert explizite Human Approval und ist nie
autonom zulässig. Bei fehlender oder nicht verifizierbarer Ownership wird nicht mutiert.

Vor dem Aufruf prüft der ausführende Harness die Ownership mit
`workspace-lock/scripts/workspace_lock_guard.py` und `require_workspace_lock()` unter
Angabe seiner Lease- oder Conversation-ID. Der Guard bleibt als gemeinsame
Sicherheitskomponente im `workspace-lock`-Skill; Cloud Atlas dupliziert ihn nicht. Der
Operator hält Erwerb, Lease-Status und Freigabe im Handoff fest. Ohne verifizierbare
eigene Lease wird nicht mutiert; einen lockfreien Legacy-Modus gibt es nicht.

### Empfangsgrenze für Mail-Dossier-Preflights

Ein `mail-desk`-Dossier kann einen rein deklarativen `cloud_atlas_preflight` für
eine exakte Projekt-ID übergeben. `pending_review` benennt höchstens bereits im
Projektkatalog deklarierte Storage-IDs; `not_configured` bedeutet, dass keine
Synchronisation anzufordern ist. Der Empfang übernimmt weder Pfade noch eine
Sync-Autorisierung: Cloud-Atlas prüft `cloud_sync`, Lock und Human Gate selbst.
Ohne diese Prüfung wird kein Sync gestartet.

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
> **Schutz der Cloud-Originale**: Originale im Cloud-Verzeichnis (`data/cloud/...`) bleiben grundsätzlich unverändert. Die einzige Ausnahme ist die explizite Policy `enrich_source`: Sie darf ein beschreibbares, sicher als unsigniert geprüftes PDF atomar ersetzen, wenn eine Rückholung/Versionierung möglich ist; Vorher-/Nachher-Hash und Policy werden in der Provenienz festgehalten. Alle anderen Derivate bleiben strikt lokal unter `memory/cloud/.../_derivatives/`.

### Das `cloud_sync`-Schema
Jedes Projekt oder Thema kann beliebig viele Cloud-Speicher besitzen. Der Eintrag `cloud_sync` muss **immer** ein Dictionary von Speicher-Konfigurationen sein (auch bei nur einem Speicher, z. B. mit der ID `"default"` oder `"meshe-teams"`):

```json
"cloud_sync": {
  "<storage_id>": {
    "scan_dir": "Relative path to cloud directory junction (e.g. data/cloud/MESHE)",
    "output_json": "Relative path to output filemap.json (e.g. memory/cloud/projects/meshe/filemap.json)",
    "output_md": "Relative path to output filemap.md (e.g. memory/cloud/projects/meshe/filemap.md)",
    "output_dir": "Relative path to local markdown mirror directory (e.g. memory/cloud/projects/meshe/default oder memory/cloud/topics/<slug>/<storage_id>)",
    "curation_json": "Optional relative path to the tracked filemap curation overlay",
    "exclude_dirs": ["Specialized Area", "Archive/Private"],
    "last_synced_at": "Automated timestamp of last successful sync (e.g. 2026-08-05 21:09:12)"
  }
}
```

### Verzeichnisausschlüsse pro Storage (`exclude_dirs`, Issue #2)

Das optionale Array gilt identisch für Projekte, Topics und explizite
Subtopic-Storages. Jeder Eintrag ist ein **literaler Verzeichnispfad relativ zu
`scan_dir`**. Beispiel: Ein breiter Storage mit `scan_dir: "data/cloud/shared"`
und `exclude_dirs: ["Specialized Area"]` lässt diesen Teilbaum aus. Ein zweiter
Storage darf `data/cloud/shared/Specialized Area` mit **eigenem, getrenntem
`output_dir` und eigenen Filemaps** registrieren. Keine automatische Neuzuordnung.

- Fehlendes Feld oder `[]`: bisheriger vollständiger Scan.
- `/` und `\` sind Separatoren; `.`-Segmente, doppelte Separatoren und abschließende
  Separatoren werden normalisiert. Leerzeichen in Namen bleiben erhalten.
- Komponentenvergleich ist auf allen Plattformen case-insensitive: `Area`
  schließt `area/Nested` aus, aber nicht `Area Extra`. Ein File namens `Area`
  bleibt erfasst. Glob-Zeichen werden nicht interpretiert.
- Leere Einträge, ein Root-Ausschluss (`.`), absolute Pfade, Laufwerkspfade,
  NUL und jedes `..`-Segment sind ungültig. Fehler stoppen vor dem ersten Write.
- Doppelte und durch Eltern bereits abgedeckte Ausschlüsse werden reduziert;
  beide Scanner beschneiden die Traversierung **vor** Hashing oder Konvertierung.
- `filemap.json.exclude_dirs`, die lesbare Filemap und die Storage-Ergebnisse
  enthalten die effektiven Ausschlüsse. Legacy-Filemaps ohne Feld bedeuten `[]`.

**Änderungen & Cleanup:** Neu ausgeschlossene Quellen verschwinden aus dem
Inventar, aber bestehende Mirrors und Derivate werden **nicht gelöscht** und
nicht über Hash-Matches für andere Quellen wiederverwendet. Solange Ausschlüsse
aktiv sind, bereinigt der Konverter nur zuvor inventarisierte Artefakte von
weiterhin im Scope liegenden Quellen; unbekannte Artefakte und leere Verzeichnisse
bleiben konservativ erhalten, auch bei wiederholten Läufen. Solche zurückbehaltenen
Dateien sind kein aktives Inventar. Ihre Entfernung braucht eine separate,
geprüfte Bereinigung. Ohne Ausschlüsse bleibt das bisherige Cleanup erhalten.
Zurückbehaltene und nicht zuordenbare Artefaktpfade sind auch für die Zielplanung
reserviert: Kollidiert ein neuer Mirror oder ein Derivat damit, stoppt der Lauf
vor dem Write bzw. der Inventarübernahme mit `mirror-plan`, statt Alt-Artefakte
zu überschreiben. Das gilt auch nach Entfernung ihres alten Filemap-Eintrags.
Die Ausschlussfunktion verändert oder löscht keine Cloud-Originale; die bestehende,
explizite OCR-Policy `enrich_source` bleibt auf **eingeschlossene** Quellen beschränkt.

### Manifest- und Metadaten-Verknüpfung in `filemap.json`

Jede Datei wird mit kryptografischem SHA-256 Hash und detailliertem Status erfasst. Bei `.doc`-Dateien sowie bildbasierten PDFs mit OCR-Derivaten werden Original, Derivat und Konvertierungsmethode eindeutig verknüpft:

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
    "data/cloud/MESHE/Scan_Rechnung.pdf": {
      "version": "N/A",
      "mtime": "2026-08-29 14:20:00",
      "size": "320.5 KB",
      "sha256": "a1b2c3d4e5f67890123456789abcdef0123456789abcdef0123456789abcdef0",
      "description": "Eingescannte Rechnung",
      "markdown_mirror": "memory/cloud/projects/meshe/Scan_Rechnung.md",
      "conversion_status": "converted",
      "ocr_applied": true,
      "ocr_policy": "local_derivative",
      "derivative": {
        "path": "memory/cloud/projects/meshe/_derivatives/Scan_Rechnung.pdf",
        "sha256": "b2c3d4e5f6a17890123456789abcdef0123456789abcdef0123456789abcdef1",
        "format": "pdf",
        "conversion_method": "ocrmypdf-derivative",
        "converted_at": "2026-08-29 14:22:00",
        "potential_quality_loss": "OCR-Textebene in durchsuchbarem PDF-Derivat erzeugt. Bei kritischen Auswertungen (Zahlen, Namen, Beträge) bitte im Original-PDF gegenchecken."
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

Im `--json`-Modus bleibt die Standardausgabe des Orchestrators kanonisch: Sie
enthält ausschließlich den abschließenden JSON-Envelope. Die JSON-Envelopes der
Child-Prozesse werden intern für Fehlerauswertung erfasst; deren laufender
Fortschritt und Diagnostik werden dagegen unverzögert über `stderr` an den
aufrufenden Prozess durchgereicht. Daher konsumieren Automationen `stdout` als
JSON und beobachten `stderr` für Live-Fortschritt.

### Speicherort der Skripte im Skill:
* Orchestrator: `skills/cloud-atlas/scripts/sync_project_cloud.py`
* Konvertierung: `skills/cloud-atlas/scripts/convert_cloud_docs.py`
* Generierung: `skills/cloud-atlas/scripts/gen_filemap.py`
* Retention-Audit (Issue #97, rein lesend): `skills/cloud-atlas/scripts/retention_audit.py`

### Aufrufe & Parameter

#### 1. Projekt synchronisieren (alle konfigurierten Speicher)
```bash
python .agents/skills/cloud-atlas/scripts/sync_project_cloud.py --project-id <project_id>
```

#### 2. Topic/Thema synchronisieren (alle konfigurierten Speicher)
```bash
python .agents/skills/cloud-atlas/scripts/sync_project_cloud.py --topic-id <topic_id>
```

#### 2a. Katalogisiertes Subtopic synchronisieren
```bash
python .agents/skills/cloud-atlas/scripts/sync_project_cloud.py --topic-id <topic_id> --subtopic-id <subtopic_id>
```
`--subtopic-id` ist nur zusammen mit `--topic-id` zulässig. Der Lauf verwendet
ausschließlich den exakt einmal vorhandenen aktiven (oder Legacy-statuslosen)
`topics[].subtopics[]`-Eintrag und dessen `cloud_sync`; er fällt niemals auf
Parent-Topic-Speicher oder abgeleitete Standardpfade zurück. Inaktive, unbekannte
oder doppelte Subtopics sowie fehlendes `cloud_sync` brechen fail-closed ab.
`--storage-id` begrenzt dabei nur die deklarierte Subtopic-Storage-ID. Konverter,
Filemap und Orchestrator geben das Ziel als `kind: subtopic`, `topic_id` und
`subtopic_id` im JSON-Envelope aus; `last_synced_at` wird ausschließlich im
verwendeten verschachtelten Storage aktualisiert. Bei einem Subtopic-Lauf sind
direkte Pfad-Overrides des Konverters (`--cloud-dir`, `--output-dir`,
`--filemap-json`) und der Filemap (`--scan-dir`, `--output-json`,
`--output-md`) unzulässig: Die Pfade stammen ausschließlich aus dem
verschachtelten `cloud_sync`-Eintrag. `--project-title` ist weiterhin nur ein
Anzeige-Label.

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

#### 6. OCR-Steuerung & Policy (`convert_cloud_docs.py`)
```bash
python .agents/skills/cloud-atlas/scripts/convert_cloud_docs.py --project-id meshe --ocr-policy {local_derivative,enrich_source,disabled}
```
* **`--ocr-policy local_derivative` (Standard):** Durchsuchbares PDF-Derivat wird isoliert unter `_derivatives/` abgelegt; Originaldatei in der Cloud bleibt unverändert.
* **`--ocr-policy enrich_source`:** In-place-Anreicherung des Originals im Cloud-Verzeichnis. Nur zulässig bei beschreibbarem Speicher, sicher als unsigniert geprüften PDFs und verfügbarer Rückholung/Versionierung; die Promotion erfolgt über eine temporäre Geschwisterdatei und `os.replace()`.
* **`--ocr-policy disabled` bzw. `--no-ocr`:** Deaktiviert OCR vollständig (Standard-Textparsing ohne OCRmyPDF-Aufruf).
* **`--redo-ocr`:** Erzwingt die Neuerstellung bestehender OCR-Textebenen (`ocrmypdf --redo-ocr --tagged-pdf-mode ignore`). `--tagged-pdf-mode ignore` wird **nur** in diesem expliziten Redo-Zweig verwendet und verwirft die Struktur bewusst; `--skip-text` wird dort nicht zusätzlich gesetzt, weil sich beide Optionen in OCRmyPDF ausschließen.
* **Sichere Parameter:** OCRmyPDF läuft standardmäßig mit `--output-type pdf --optimize 0 --skip-text`. Seiten mit vorhandener Textebene werden also nicht neu OCRt und ihre Tags bleiben erhalten; nur bild-only Seiten werden ergänzt.

#### 7. Konverter-Voraussetzungen für `.doc`
Für die Konvertierung alter Word 97–2003 `.doc`-Dateien:
* **LibreOffice (empfohlen, cross-platform):** `soffice.com` / `soffice` (wird automatisch in PATH und Standard-Installationspfaden erkannt).
* **Microsoft Word (Windows-Fallback):** MS Word COM-Automation über PowerShell.

Diese hostseitigen Konverter gehören nicht zu den optionalen Python-Paketen. Fehlt
beides, liefert der Lauf `ConversionRequired`/`conversion_required`; er fordert keine
ungeprüfte Systeminstallation an.

Bei überlangen Windows-Quell- oder Derivatpfaden wird der externe Konverter automatisch
in einem isolierten Kurzpfad-Staging ausgeführt. Das Ergebnis wird erst nach Prüfung
und atomarer Promotion am vorgesehenen lokalen Derivatpfad sichtbar; temporäre Dateien
werden bereinigt.

#### 8. Einzeldatei-Modus (`convert_cloud_docs.py --only-file`, Issue #22)
```bash
python .agents/skills/cloud-atlas/scripts/convert_cloud_docs.py --topic-id <topic_id> --only-file data/cloud/<ORDNER>/<Pfad>/datei.pdf --json
```

**Bewusste Scope-Reduktion.** Dieser Modus ist absichtlich klein: genau eine Quelle,
genau ein Filemap-Eintrag, ein Commit-Punkt. Er hat **keine** Vollscan-Integration, **keine**
Fremdreferenz-Ermittlung, **keine** physische Quarantäne, **keine** Journal-Archivierung und **kein**
Verschieben oder Löschen im Produktpfad. Die daraus folgenden Grenzen stehen unten
ausdrücklich; sie sind Vertragsbestandteil, nicht offene Fehler.

* **Umfang:** Der Modus konvertiert **genau eine** Quelle und erzeugt oder aktualisiert
  **genau einen** Eintrag einer **bestehenden** `filemap.json`. Er läuft in einem eigenen
  Pfad ohne Verzeichnis-Scan, ohne `retained_artifact_paths`-Walk, ohne Orphan-Cleanup und
  ohne `gen_filemap.py`. `sync_project_cloud.py` kennt den Modus nicht.
* **Pfadsemantik:** `--only-file` ist wie `--cloud-dir`, `--output-dir` und
  `--filemap-json` **relativ zum Workspace-Root** und identisch mit dem Schlüssel in
  `files`. Er muss unter dem Cloud-Verzeichnis liegen (`--cloud-dir` oder `scan_dir` des
  Storages; bei `--subtopic-id` ausschließlich der Katalogpfad) und darf nicht in einem
  `exclude_dirs`-Verzeichnis liegen. Absolute Pfade, Laufwerke, `..`, `:`-Segmente,
  Verzeichnisse, versteckte/`~$`-Namen und nicht unterstützte Endungen werden abgewiesen.
  Backslashes werden zu `/` normalisiert. Mehrere Storages erfordern `--storage-id`.
* **Byte-Identität:** Die Filemap wird als Rohtext gespleißt. Alle anderen `files`-Einträge,
  deren Reihenfolge, Formatierung (Einrückung, CRLF, Escapes, Zahlenschreibweise) und alle
  Container-Felder inkl. `updated_at` bleiben Zeichen für Zeichen identisch. Ein neuer
  Eintrag wird an der sortierten Position eingefügt (bei unsortierter Map am Ende) und im
  Stil der Datei formatiert; ein bestehender wird an Ort und Stelle ersetzt. Vor dem
  Schreiben beweist `verify_splice`, dass nur dieser Eintrag abweicht.
* **Versionierte Ziele (direkt geschrieben):** Eine Konvertierung legt ein **neues,
  eindeutiges Versionsverzeichnis** unter dem reservierten Root
  `<output_dir>/_versions/<quell-key>/<zeit-pid-id>/` an und schreibt Mirror und Derivat
  **direkt hinein** — kein Ziel-Staging, kein Verzeichnis-Rename; Dateien und
  Verzeichnis werden gefsynct (Verzeichnis-fsync nur POSIX). Auch der Mirror wird direkt
  geschrieben (kein Temp, kein Replace). Der Filemap-Eintrag (`markdown_mirror`,
  `derivative.path`) zeigt danach auf diese Version. **Der Mirror-Pfad ist nicht stabil**;
  die Filemap ist die Quelle der Wahrheit. Ein vorhandenes Artefakt wird nie in-place
  überschrieben, umbenannt oder zurückgerollt. Der reservierte Root hat **keine
  Aufräumlogik**: alte Versionen, Reste und Journale bleiben liegen (Retention ist bewusst
  nicht Teil dieses Modus; die rein lesende Klassifikation steht in Abschnitt 9).
  Das separate DOC-Short-Path-Staging ist kein Ziel-Staging:
  Im `--only-file`-Modus liegt es unter `data/ca-doc/<run-id>` und wird initial ab
  `>= 240` Zeichen (Windows) oder nach einem erkannten Pfadlängenfehler betreten; der
  Vollscan nutzt ein temporäres Verzeichnis (siehe unten).
* **Commit-Punkt:** Der atomare Filemap-Replace ist der **einzige** Commit-Punkt. Jeder
  Fehler davor — Konvertierungsfehler, Timeout (`--file-timeout`), Validierungs-, Schreib-
  oder Konfliktfehler, Exception — lässt die alte Version aktiv; die neue Version bleibt
  unreferenziert liegen und wird als `retained_version` gemeldet. **Einzige zulässige
  Mutationen neben den direkten Schreibvorgängen sind** der atomare `os.replace` der
  Ziel-Filemap und des Journals (Temp-Datei im selben Verzeichnis, notwendige Ausnahme für
  den Commit bzw. den Journal-Record). Es gibt **kein** `os.rename` von Verzeichnissen und
  **kein** `unlink`/`rmtree` im Produktpfad — auch nicht im Worker: OCR schreibt direkt in
  das endgültige Derivat; im `--only-file`-Modus bleibt ein Short-Path-Staging im kurzen
  Data-Zone-Laufverzeichnis `data/ca-doc/<run-id>` liegen (Quellkopie `source.doc` nur
  dort), Derivate werden im Short-Path-Zweig per Kopie (nicht per Replace) übernommen; der
  direkte Pfad übergibt die Cloud-Quelle ohne Kopie.
* **Minimales Journal:** Ein atomar geschriebenes Journal
  `<output_dir>/.only-file-journal/<key>.json` hält `planned → staged → published →
  committed`, Quellpfad, Versionsverzeichnis, Zielpfade mit SHA-256 und die
  Eintrags-Digests. `planned` wird vor dem Anlegen des Versionsverzeichnisses geschrieben,
  `staged` **unmittelbar vor** dem Filemap-Replace, danach `published`/`committed`. Das
  Journal wird **nie** umbenannt, archiviert oder gelöscht; es bleibt nach jedem Lauf
  liegen. **Vor jeder Journalmutation** prüft der Lauf **alle** Einträge derselben
  Ziel-Filemap — auch den Eintrag der gewählten Quelle: beansprucht einer davon den
  Journalpfad (`markdown_mirror` oder `derivative.path`), endet der Lauf fail-closed
  (`phase: journal`), ohne das Journal zu schreiben oder die Filemap zu ändern (kein
  Scan, keine andere Filemap). Der Vergleich nutzt dieselbe Windows-Aliasing-Regel wie
  das Dateisystem: case-insensitiv und abschließende Punkte/Leerzeichen je
  Pfadkomponente ignoriert (`…/.only-file-journal./…` ist derselbe Pfad).
* **Recovery meldet nur:** Der nächste Lauf derselben Quelle liest **genau diesen Pfad**
  (kein Scan, keine Suche nach anderen Journalen, keine Fremd-Filemap). Er trifft **keine**
  Roll-forward-/Roll-back-Entscheidung und schreibt, verschiebt oder löscht nichts:
  - `published`/`committed` oder ein Live-Eintrag, der dem aufgezeichneten neuen Digest
    entspricht ⇒ der frühere Lauf hat committet; der neue Lauf fährt fort (und ersetzt dabei
    nur seinen eigenen Journalinhalt).
  - Jeder andere Zustand (`planned`, `staged` ohne passenden Live-Eintrag, unlesbares
    Journal, beschädigte aktive Version) ist **unklar** ⇒ der Lauf endet **fail-closed**
    (`phase: journal`) vor jeder Mutation; Journal, Versionen und Stages bleiben exakt
    liegen. Die manuelle Abhilfe ist die Inspektion und das Entfernen des Journals durch den
    Operator.
* **Fremdreferenzschutz (nur Ziel-Filemap):** Geschützt wird ausschließlich, was **dieselbe**
  `filemap.json` schon beansprucht: Ziele anderer Einträge (`claimed`), eigene Artefakte
  (`owned`) und die Storage-eigenen Dateien (Filemap, `filemap.md`, Kuratierung). Andere
  Topics/Projekte werden **nicht** gelesen und nicht berücksichtigt.
* **Keine Cloud-Hydratation (nur Quelle und Ziel-Filemap):** Beide werden vor jedem
  relevanten Öffnen lokal geprüft (`FILE_ATTRIBUTE_OFFLINE`, `RECALL_ON_OPEN`,
  `RECALL_ON_DATA_ACCESS`; ein `stat`-Fehler auf einem existierenden Pfad gilt fail-closed als
  Platzhalter). Eine nicht-lokale Quelle bzw. Platzhalter-Ziel-Filemap endet fail-closed,
  ohne zu hydratisieren.
* **Locking:** Der Lauf hält **OS-Locks pro Storage**: Output-Verzeichnis **exklusiv**,
  Elternordner **shared**, Filemap-Pfad **exklusiv**, sortiert akquiriert. Die Filemap wird
  darunter gelesen, gespleißt und per Base-Hash vor dem Temp-Write und unmittelbar vor dem
  Replace geprüft. Die Locks werden vom OS beim Prozesstod freigegeben und liegen persistent
  unter `<workspace>/data/.locks/cloud-atlas/` (nicht gelöscht; `.gitignore` empfohlen).
  **Kooperierende Läufe sind geschützt; Writer ohne Harness-Lease sind ausdrücklich außerhalb
  des Vertrags.** Das ist kein vollständiges CAS gegen beliebige Fremdschreiber.
* **Idempotent:** Ein zweiter Lauf ohne `--force` schreibt weder Eintrag noch Artefakte noch
  Filemap (keine geänderte mtime) — auch bei gültigen kurzen Office-Dokumenten (die
  Voll-Scan-Heuristik „Body < 10 Zeichen“ gilt hier nicht), bei einem benutzerdefinierten
  Derivatpfad des Eintrags und ohne installierten `.doc`-Konverter (er ist nur nötig, wenn
  tatsächlich konvertiert wird). `--force` und `--redo-ocr` wirken nur auf diese Datei.
* **Abweisungen vor jeder Mutation:** fehlende oder nicht lesbare Filemap (sie wird nie
  angelegt), ungültiges JSON, doppelte JSON-Schlüssel, `NaN`, BOM, Verstoß gegen den
  Container-/Eintrags-Vertrag (Runtime-Subset von `gen_filemap.validate_filemap`, ohne
  Datenträgerzugriff: `storage_id`-Muster, normalisierte `exclude_dirs`; `scope`,
  `storage_id`, `project`, `scan_dir`, `output_dir` müssen zum aufgelösten Ziel passen;
  ein Container ohne `$schema` ist nur die exakte Legacy-Form des Konverters mit
  `project`, `project_title`, `files`, `exclude_dirs`, `updated_at`), mehrere oder per
  Schreibweise (casefold/NFC/Separator) abweichende Einträge derselben Quelle
  (`duplicate_source`), Symlink-/Junction-/`realpath`-Ausbruch der Quelle, der Output-Zone
  oder der Filemap, Link-Komponenten zwischen `output_dir` und Mirror-/Derivat-Ziel (Alias
  auf fremde Dateien), abweichende Pfad-Groß-/Kleinschreibung, `:`-, Laufwerks- oder
  `..`-Segmente in `--cloud-dir`/`--output-dir`/`--filemap-json`, Output-Zone oder Filemap
  außerhalb der kanonischen Data Zones (`memory/references/`, `memory/evidence/`,
  `memory/cloud/`, `data/`), Mirror-/Derivat-Ziele,
  die ein fremder Eintrag **derselben** Filemap beansprucht, bereits vorhandene Ziele ohne
  Besitz durch diesen Eintrag, die eigene Filemap/`filemap.md`/Kuratierungsdatei als Ziel
  (andere Namen wie `index.md` oder `readme.md` sind erlaubt) und nicht auflösbare
  Mirror-Kollisionen.
* **Inkompatible Flags:** `--extensions` (Scan-Filter) und `--ocr-policy enrich_source`
  (Quellmutation) werden mit Exit 2 und `phase: arguments` abgewiesen. `--jobs` hat bei
  einer Datei keine Wirkung.
* **Envelope:** Der kanonische Envelope bleibt unverändert; `data.only_file` ergänzt
  `source`, `action` (`created`/`updated`/`unchanged`/`none`), `entry_changed`,
  `duplicate_source`, `artifacts_changed`, `recovered` (`committed`, wenn ein früherer Lauf
  nachweislich committet hat, sonst `null`), `retained_version` (reell existierender
  Versionsverzeichnis, der liegen blieb, sonst `null`), `version` (neues
  Versionsverzeichnis), `journal` (offener bzw. liegengebliebener Journalpfad) und `mirror`.
  Bei Argumentfehlern (Exit 2) gibt es noch keinen Lauf und damit kein `only_file`.
  `data.storages[0]` trägt `counts` (`scanned: 1`) und `file_count`.
* **Eintragsfelder:** Wie beim Voll-Scan inkl. `form_fields`, `derivative`, OCR-Feldern und
  kuratierten Feldern/Overlay; zusätzlich `artifact_metadata` aus dem geschriebenen Mirror
  (so wie der Generator es nachträglich ergänzt). Unveränderte Quellen übernehmen ihre
  generierten Felder aus dem bestehenden Eintrag.
* **Nicht Teil des Modus (Sicherheitsgrenzen, bewusst):**
  - **Kein Vollscan:** `--only-file` scannt und bereinigt nichts. Ein späterer regulärer
    Vollscan kennt den Version-Root **nicht**: er kann versionierte Quellen erneut
    konvertieren und versionierte Mirror-Dateien als Waisen entfernen. Wer das vermeiden
    will, betreibt den Modus in einem Storage, der nicht zusätzlich vollgescannt wird.
  - **Keine Fremdreferenz-Discovery:** Verweise anderer Storages/Filemaps auf Pfade dieses
    Outputs werden nicht ermittelt und nicht geschützt.
  - **Keine physische Quarantäne, keine Journal-Archivierung, kein Aufräumen:** Journale,
    Versionen, Reste und Commit-Temps bleiben liegen und sammeln sich an. Nichts wird
    verschoben oder gelöscht. Die rein lesende logische Retention-Klassifikation aus
    Abschnitt 9 ändert daran nichts und ist kein Bestandteil dieses Laufs.
  - **Kein Sibling-Schutz:** Ein paralleler Schreiber eines anderen Storages wird nicht
    koordiniert; nur derselbe Storage (Output/Filemap) ist gesperrt.
  - **Kein Roll-forward/Roll-back:** Recovery meldet und blockiert, sie repariert nicht.
  - **Grenze der Platzhalter-Prüfung:** Nur Quelle und Ziel-Filemap werden geprüft; bereits
    erzeugte Mirror-/Derivat-/Versionsdateien im Output werden nicht geprüft. Liegt der
    Output in einem cloud-synchronisierten Ordner, ist das eine dokumentierte Grenze.
  - **Kein Move/Delete im Worker:** Im `--only-file`-Modus löscht der Konvertierungs-Worker
    keine Artefakte: Dort liegt das LibreOffice-Profil in beiden DOC-Zweigen und das
    DOC-Short-Path-Staging (Quellkopie `source.doc`, gestagtes Derivat) nur im
    Short-Path-Zweig — jeweils im kurzen, eindeutigen Laufverzeichnis
    `<workspace>/data/ca-doc/<run-id>` (kanonische Data Zone) — und bleibt liegen; ein
    fehlgeschlagenes OCR-/Derivat-Artefakt bleibt an seinem Pfad.
    Unser Code legt im `--only-file`-Pfad kein temporäres Verzeichnis außerhalb der Data
    Zones an; die OCR-Fähigkeitsprobe läuft **nur im Vollscan**. Externe Programme
    (LibreOffice, OCRmyPDF, Tesseract) verwalten ihre eigenen internen Temp-Dateien selbst.
    Die Profil-URI wird als reiner `file://`-URI gebildet (ohne Windows-Extended-Prefix,
    Leerzeichen percent-kodiert, UNC als `file://server/share/…`), damit der Konverter nicht
    auf ein unauflösbares Profil wartet. Das kurze Laufverzeichnis hält die URI bei
    höchstens 120 Zeichen (`<= 120`); ist der Workspace-Pfad zu tief, endet die Konvertierung
    fail-closed als `conversion_required` (statt eines Konverter-Absturzes), und es wird
    nichts gelöscht. Die Windows-Vorabprüfung `>= 240` Zeichen (geplanter Quell- **oder**
    Derivatpfad) wählt den Short-Path-Zweig initial; auf POSIX ist sie immer `False`.
    Zusätzlich löst ein eindeutiger Pfadlängenfehler von LibreOffice im direkten Aufruf
    einmalig denselben Short-Staging-Retry aus — auch unterhalb von 240 und auf POSIX. In
    diesem Zweig kopiert der Konverter die Quelle als `source.doc` und konvertiert die
    Kopie; der direkte Pfad übergibt die Cloud-Quelle ohne Kopie an LibreOffice. Auf POSIX
    werden Separatoren nicht normalisiert; ein Backslash im Dateinamen bleibt Namenszeichen
    (percent-kodiert als `%5C`). OCR schreibt das Derivat direkt (kein `os.replace(x, x)`);
    die Pfadgleichheit wird nach Entfernen des Windows-Extended-Prefix geprüft. Der
    Vollscan ist davon getrennt: Er nutzt für Short-Staging und LibreOffice-Profil
    temporäre Verzeichnisse und räumt sie auf; der Word-COM-Pfad hat kein
    LibreOffice-Profil.
  - **Trust-Boundary:** Same-User-Manipulation (z. B. Hardlink-Restore) liegt außerhalb des
    kooperativen Vertrags; es gibt keine Identitätsgarantie.

<a id="retention-policy"></a>
#### 9. Retention- und Quarantäne-Policy für versionierte Ziele (Issue #97)

Diese Policy gilt für die von `--only-file` erzeugten Versionsverzeichnisse unter
`<output_dir>/_versions/`. Sie ist **rein lesend und nachgelagert**: `--only-file`
selbst behält den #22-Vertrag unverändert (kein Cleanup, keine physische Quarantäne,
kein Move/Delete). Der Audit ist die nicht-destruktive Bereinigungsstufe: er markiert
nur und führt nichts aus. Er ändert keine Datei, kein Verzeichnis, kein Journal und
keine Filemap und ist kein Bestandteil eines Konvertierungs- oder Sync-Laufs. Das
#22-Envelope-Feld `retained_version` bleibt unverändert und ist **keine** Audit-Klasse.

**Klassifikation (Policy `cloud-atlas-retention-v1`):**

| Klasse | Bedingung | Bedeutung |
| :--- | :--- | :--- |
| `active` | Versionsverzeichnis wird von `markdown_mirror` oder `derivative.path` **derselben Ziel-Filemap** referenziert | aktive, von dieser Filemap sichtbare Version; bleibt unangetastet und wird nie Kandidat |
| `retained` | nicht aktiv und (unter den neuesten **2** inaktiven Versionen je Quell-Key **oder** höchstens **90 Tage** alt) | innerhalb der dokumentierten Aufbewahrungsempfehlung |
| `quarantine_candidate` | nicht aktiv und (älter als **365 Tage** oder sowohl außerhalb der neuesten 2 inaktiven Versionen als auch älter als 90 Tage) | **logische** Quarantäne-Markierung für die Prüfung durch einen Operator; keine physische Quarantäne |

**Vorrang der 365-Tage-Regel:** Die absolute Altersgrenze hat Vorrang vor der
Count-/90-Tage-Aufbewahrung. Eine inaktive Version, die älter als 365 Tage ist, ist
`quarantine_candidate`, **auch wenn sie zu den neuesten zwei inaktiven Versionen gehört
oder jünger als 90 Tage wäre**; die neuesten zwei/90 Tage können eine >365-Tage-Version
nicht retten. Umgekehrt bleibt eine aktive Version immer `active`.

Das Alter wird **ungerundet** aus der **mtime des Versionsverzeichnisses** (UTC)
berechnet; nur das Anzeigefeld `age_days` ist auf drei Nachkommastellen gerundet. Die
Klassifikation ist ein Lesesnapshot und keine Garantie gegen nachträgliche
mtime-Manipulation. Filemap-Referenzen werden **nur auf Windows** mit
Windows-Pfadalias verglichen (case-insensitiv, abschließende Punkte/Leerzeichen je
Komponente ignoriert). Auf case-sensitiven Plattformen werden unterschiedliche
Schreibweisen **nicht** gleichgesetzt; eine nicht exakt auflösbare Referenz bleibt
unaufgelöst und wird nicht als `active` behauptet. Das Quell-Key-Format selbst wird
unabhängig von Groß-/Kleinschreibung validiert (ein großgeschriebener Key bleibt
zulässig), die Pfadidentität folgt aber den Plattformregeln. Zusätzlich werden unerwartete
Einträge unter `_versions/` (fremder Quell-Key, Dateien statt
Versionsverzeichnissen, nicht auflösbare Layouts) als `quarantine_candidate` mit
`unexpected_layout` gemeldet. Versionsverzeichnisse mit Symlink/Junction oder mit
Links im Inhalt werden nicht verfolgt und ebenfalls als Kandidat
(`unsafe_version_directory`) geführt. Ein **referenziertes** Link-/Junction-Run bleibt
jedoch `active` und wird nie traversiert; nur nicht referenzierte Links sind Kandidaten.
Cloud-Platzhalter in Quell-Key-, Run- oder Inhaltsverzeichnissen werden ebenfalls nie
traversiert: betroffene nicht-aktive Versionen werden `cloud_placeholder_*`-Kandidaten,
eine referenzierte aktive Version bleibt `active` (mit `unsafe: true`). Wird ein
Quell-Key-Verzeichnis selbst unlesbar (Link, Platzhalter, Datei/unerwartetes Layout),
aber eine aktive Referenz zeigt in diesen Key, wird der Key als `unresolved` mit
`potentially_active: true` geführt und **nicht** als Kandidat markiert; die aktiven
Referenzen erscheinen unter `references.unresolved` und in der menschlichen
Zusammenfassung. Ein Link oder Platzhalter auf dem Version-Root selbst bricht den
Audit fail-closed ab.

**Filemap-Zugehörigkeit:** Die Ziel-Filemap muss ein `output_dir` deklarieren, das zum
auditierten Output passt, und wird über den Workspace-Katalog (`projects.json` /
`topics.json`, inkl. Subtopics) an `output_json` + `output_dir` gebunden. Dabei gelten
die etablierten Aliasfelder `filemap_json`/`cloud_mirror_dir` und die synthetisierten
Default-Pfade für Projekte/Topics. Nur bei genau einem Katalogtreffer **und ohne
jeden Katalog-Lese-/Parsefehler** ist `identity.status: "verified"` und die
Klassifikation autoritativ. Ein Fehler in irgendeinem Katalog, eine nicht
kataloggebundene oder eine mehrdeutige Filemap ergibt
`identity.status: "unverified"`/`"ambiguous"`, setzt
`classification_authoritative: false` und führt Kandidaten nur als nicht-autoritative
Prüfhinweise; sie wird nicht als autoritative Zielidentität ausgegeben. Eine
Legacy-Filemap ohne `output_dir` wird **fail-closed** abgewiesen, weil ihre
Zugehörigkeit nicht bewiesen werden kann. Output, Filemap und jeder Katalogpfad
werden **komponentenweise vor `realpath`/`scandir`/`open`** geprüft. Ein Link,
Junction oder Cloud-Platzhalter in einem Output-/Filemap-Vorfahren **bricht den
Audit fail-closed ab**. Beim Katalogpfad verhindert derselbe Guard nur das Lesen:
der Audit läuft weiter, die Identität wird `unverified` und die Klassifikation
nicht autoritativ; ein externes Katalogziel wird dabei nie traversiert.

**Quarantäne-Bereich:** `<output_dir>/_quarantine/` ist als reservierter Namensraum
dokumentiert, wird von diesem Audit aber **weder angelegt noch beschrieben oder
gelesen**. Es gibt keine automatische Verschiebung dorthin. Ein manuelles Verschieben
oder Löschen ist **nicht Teil des Produktvertrags**: Es kann Filemap-Verweise,
Recovery-Zustände und fremde Konsumenten brechen; Rückholung/Rollback und
Fremdreferenz-Schutz werden nicht zugesichert.

**Journale:** `<output_dir>/.only-file-journal/` ist kein Retentionsobjekt. Journale
und Commit-Temps werden nie klassifiziert, archiviert, umbenannt oder gelöscht; der
Audit meldet sie nur als `journal_not_managed`. Ein unklares Journal blockiert den
nächsten `--only-file`-Lauf weiterhin fail-closed (Abschnitt 8).

**Fremdreferenzen und Grenzen:** Der Audit liest ausschließlich die angegebene
Ziel-Filemap, den Version-Root, die Journal-Zusammenfassung und die
Workspace-Kataloge `memory/references/projects/projects.json` sowie
`memory/references/topics/topics.json` (nur zur Identitätsbindung). Andere Filemaps,
Konsumenten und manuelle Verweise werden nicht ermittelt. Deshalb ist **kein**
Eintrag `safe_to_delete` oder `safe_to_move`; jeder Eintrag trägt
`foreign_reference_risk: "unknown"` und `action: "none"`. Die Schwellen sind
Empfehlungen, **keine harte Speicherobergrenze**: Der Version-Root kann weiter
wachsen; die Bytes werden nur berichtet.

**Vollscan-Grenze (unverändert):** Ein regulärer Vollscan kennt den Version-Root
nicht, reserviert ihn nicht und kann versionierte Quellen erneut konvertieren sowie
versionierte Mirror-Dateien als Waisen entfernen (Abschnitt 8, `effects.md` §5). Der
Retention-Audit repariert das nicht und schützt keine Version vor einem Vollscan.
Ein Storage, dessen Versionen erhalten bleiben sollen, darf nicht zusätzlich
vollgescannt werden.

**Aufruf (nur lesend, ohne Lock):**

```bash
python .agents/skills/cloud-atlas/scripts/retention_audit.py \
  --workspace-root <workspace> \
  --output-dir memory/cloud/topics/<topic> \
  --filemap memory/cloud/topics/<topic>/filemap.json \
  --json
```

`--now <RFC-3339>` erlaubt einen reproduzierbaren Stichtag. Der Audit ist ein
Snapshots lesender Vorgang; parallele Schreiber können das Ergebnis veralten lassen.
Er liefert einen kanonischen Envelope mit `data.retention` (Policy, Zähler, Bytes,
Journale, Einträge). Exit 0 bedeutet „Bericht erstellt“ (auch mit Kandidaten), Exit 2
einen Argumentfehler und Exit 1 ein nicht sicher lesbares Ziel (`phase: retention`).
Ein `quarantine_candidate` ist eine Prüfempfehlung, keine Löschfreigabe.

---

## 4. Operative Arbeitsregeln & Automatismen

### A. Frontmatter-Standard für gespiegelte Dokumente
Jede gespiegelte `.md`-Datei enthält vollständige Metadaten und Hashes zur lückenlosen Nachvollziehbarkeit (Dual Evidence):

**Beispiel 1: `.doc`-Derivat-Spiegelung**
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

**Beispiel 2: Bild-PDF mit OCR-Derivat (`local_derivative`)**
```yaml
---
original_file: "data/cloud/MESHE/Scan_Rechnung.pdf"
original_sha256: "a1b2c3d4e5f67890123456789abcdef0123456789abcdef0123456789abcdef0"
version: "N/A"
derivative_file: "memory/cloud/projects/meshe/_derivatives/Scan_Rechnung.pdf"
derivative_sha256: "b2c3d4e5f6a17890123456789abcdef0123456789abcdef0123456789abcdef1"
conversion_method: "ocrmypdf-derivative"
conversion_date: "2026-08-29 14:22:00"
file_date: "2026-08-29 14:20:00"
last_verified_date: "2026-08-29 14:22:00"
ocr_applied: "true"
ocr_policy: "local_derivative"
ocr_notice: "Hinweis: Text wurde mittels OCR aus einem Bild-PDF erfasst. Bei kritischen Auswertungen (Zahlen, Namen, Beträge) bitte im Original-PDF gegenchecken."
---
```

### B. Schutz digitaler und digital signierter PDFs
1. **Präzise Scan-Erkennung**: `convert_cloud_docs.py` analysiert die PDF-Struktur auf Vorhandensein digitaler Schriften (`/Type /Font`, `/FontDescriptor`, `/TrueType`) und Textoperatoren (`BT ... ET`). Kurze digitale Dokumente (z. B. 1-Zeiler wie Bescheide oder Buchungsbestätigungen mit < 30 Zeichen) werden als echte Digital-PDFs erkannt und niemals fälschlich OCR-mutiert. Zusätzlich erkennt `analyze_pdf_structure()` getaggte PDFs (`is_tagged`): PyMuPDF prüft `StructTreeRoot` und `MarkInfo/Marked` am Dokumentkatalog, Rohbytes (`/StructTreeRoot` bzw. `/MarkInfo` + `/Marked true`) sind nur der Fallback. Ein getaggtes PDF mit eigener Textebene bleibt damit digital, auch wenn die übergebene Extraktion leer oder kürzer als 30 Zeichen ist.
2. **Schutz digital signierter PDFs**: Vor jeder Mutation wird das PDF auf Signatur-Dictionaries und ByteRanges (`/Type /Sig`, `/DocTimeStamp`, `/ByteRange`) geprüft. Digital signierte PDFs dürfen **niemals in-place mutiert** werden, da dies die Signaturgültigkeit zerstören würde. Bei `enrich_source` wird eine Warnung ausgegeben und die Datei als `conversion_required` katalogisiert.
3. **Atomare In-place-Operationen**: Bei `enrich_source` wird das OCR-Ergebnis als temporäre Geschwisterdatei erzeugt, als PDF validiert und geflusht. Erst dann ersetzt `os.replace()` das Original auf demselben Dateisystem. Scheitert ein Schritt, wird die temporäre Datei entfernt und das Original bleibt unberührt.

### C. Gültigkeitsprüfung & 24h-Regel
* Ist eine gespiegelte Markdown-Datei älter als 24 Stunden (gemessen an `last_verified_date`), prüft `convert_cloud_docs.py` anhand von `mtime` und `original_sha256`, ob im Cloud-Speicher eine neuere Version vorliegt.
* Bei unveränderten Dateien wird lediglich das `last_verified_date` aktualisiert, ohne unnötige Neukonvertierung.

### D. Umgang mit Status `conversion_required`
* Dateien, die mangels Konverter, wegen Dateibeschädigung oder Signaturschutz nicht automatisch gespiegelt/angereichert werden konnten, verbleiben mit `conversion_status: "conversion_required"` in der Filemap.
* Sobald ein Konverter installiert ist oder die Policy angepasst wird, führt der nächste Lauf von `sync_project_cloud.py` (oder `--force`) die Konvertierung automatisch nach.

---

## 5. Pflicht bei Integration in Agent-Workspaces

### Vorlage für die Ziel-`AGENTS.md`
```markdown
### Hybrid-Modell für Cloud-Filemaps (`cloud-atlas`)

Um Dateiverteilungen in den Cloud-Speichern sauber zu dokumentieren, nutzt dieser Workspace das `cloud-atlas`-Modell:
* **Tracked Curation & abgeleitete View**: Ist `memory/cloud/` gitignoriert, ist die im Storage deklarierte, getrackte `curation_json` das SSOT für manuelle Beschreibungen und Zusatzfelder. `filemap.json` bleibt lokaler, abgeleiteter Zustand mit aktuellen Scanner-/Konvertierungsdaten. Ohne Overlay bleibt das bestehende kompatible Verhalten aktiv.
* **Lokale Spiegelung & Derivate**: Konvertierte Markdown-Kopien liegen lokal unter `memory/cloud/` (niemals direkt im Cloud-Speicher). `.doc`-Derivate werden isoliert unter `memory/cloud/.../_derivatives/` abgelegt.
* **.gitignore-Schutz**: Das Verzeichnis `data/cloud/` für Cloud-Junctions wird zwingend in `.gitignore` eingetragen (`data/cloud/`).
* **Automatisierung (24h-Regel, Cleanups & Fallbacks)**: Die Prüfung auf Dateiversionen, Neukonvertierungen bei Quelländerungen, die task-gebundene Zeitstempel-Kontrolle (`last_synced_at` >6h/12h bei Cloud-Zugriff) und die Bereinigung verwaister Dateien erfolgen über den Skill **`cloud-atlas`**.
* **Ausführliche Regeln**: Alle prozeduralen Abläufe und CLI-Aufrufe sind dokumentiert in [.agents/skills/office-intelligence/skills/cloud-atlas/SKILL.md](.agents/skills/office-intelligence/skills/cloud-atlas/SKILL.md).
```
