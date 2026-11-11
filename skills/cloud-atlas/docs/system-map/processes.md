# Cloud-Atlas — Subsystem System Map: Verben (Processes)

> **Typ**: ICM Form 6 (`system-map`), Dimension: Verben  
> **Subsystem**: [`skills/cloud-atlas`](../SKILL.md)  
> **Ziel**: Detaillierte Beschreibung aller Filemap-, Konvertierungs-, OCR- und Synchronisations-Pipelines.  
> **Gültig für**: `skills/cloud-atlas/` relativ zum Repository-Root

---

## 1. Übersicht der Lifecycles in Cloud-Atlas

```
[Cloud Storage Originale]
           │
           ▼
[sync_project_cloud.py] ────────────────────────────────┐
           │                                            │
           ├─► [Phase 1: convert_cloud_docs.py]          │
           │       ├─► Pandoc / LibreOffice Bridge      │
           │       ├─► OCR-Verzweigung (Tesseract)       │
           │       ├─► PDF-Formularfelder (AcroForm)     │
           │       └─► Schreibt Markdown-Mirrors        │
           │                                            │
           └─► [Phase 2: gen_filemap.py]                │
                   ├─► Rekursiver Scan & Hashing        │
                   └─► Schreibt filemap.json            │
                                                        ▼
                            [Ziel-Workspace memory/cloud/<id>/]
```

---

## 2. Der Dokument-Konvertierungs-Lifecycle

Implementiert in [`scripts/convert_cloud_docs.py`](../scripts/convert_cloud_docs.py):

### Phase 1: Identifikation & Inkrementalitätsprüfung
1. Validiert `exclude_dirs` pro Storage; `core/scan_scope.walk_scoped()` beschneidet ausgeschlossene Teilbäume vor der Traversierung und scannt danach nach unterstützten Formaten (`.pdf`, `.docx`, `.xlsx`, `.pptx`, `.doc`).
2. Liest bestehende Mirror-Dateien unter `memory/cloud/<id>/mirrors/`.
3. Vergleicht den aktuellen SHA-256 der Quelldatei mit dem im Frontmatter gespeicherten `source_sha256`.
4. Ist der Hash identisch und nicht `--force` gesetzt, wird die Datei übersprungen (Idempotenz).
5. Bei aktiven Ausschlüssen reserviert `retained_artifact_paths()` vorhandene
   Artefakte ohne In-Scope-Ownership. Nicht auflösbare Mirror-/Derivat-Kollisionen stoppen vor dem
   ersten Write; auflösbare Kreuzgruppen-Kollisionen werden vorher auf `original.ext.md`
   disambiguiert. Auch der Generator darf reservierte Pfade nicht neu zuordnen.

### Phase 2: Formatspezifische Transformation
* **DOCX / DOC:** Konvertierung via Pandoc nach GitHub Flavored Markdown (GFM). Bei alten Binär-DOCs Fallback über LibreOffice.
* **PPTX:** Extraktion von Folientexten und Notizen in hierarchische Markdown-Strukturen.
* **XLSX:** Tabellen-Extraktion mit Markdown-Tabellen-Formatierung.

### Phase 3: Die PDF- & OCR-Verzweigungslogik
Bei PDFs prüft der Konverter, ob digitaler Text vorhanden ist. Fehlt dieser (reine Scans / Bild-PDFs), greift die OCR-Policy:

**Tagged-PDF-Erkennung (Issue #50):** `analyze_pdf_structure()` liefert immer
`is_tagged`. Maßgeblich ist PyMuPDF (`pdf_catalog()` → `StructTreeRoot`, ersatzweise
`MarkInfo`/`Marked`), weil OCRmyPDF den Strukturbaum in komprimierten Objektstreams
ablegt; die Rohbyte-Prüfung (`/StructTreeRoot` bzw. `/MarkInfo` + `/Marked true`) ist
nur der Fallback ohne nutzbares PyMuPDF. Ein getaggtes PDF mit eigener Textebene gilt
als digital und erreicht die OCR-Verzweigung nicht — auch dann nicht, wenn die
übergebene Textextraktion leer ist. Getaggte PDFs ohne jede Textebene bleiben
OCR-Kandidaten.

```
[PDF ohne Textlayer] ──► Prüfe --ocr-policy:
                              │
                              ├─► "disabled"         ──► Abbruch ohne Text
                              │
                              ├─► "local_derivative" ──► Tesseract extrahiert Text
                              │   (Standard)             nur in lokale .md-Datei;
                              │                          Original-PDF bleibt unangetastet!
                              │
                              └─► "enrich_source"    ──► Prüfe Signatur & Schreibrecht:
                                  (Ausnahme)             In-Place Injektion der OCR-Ebene
                                                         in das Cloud-PDF mit Provenienz-Log.
```

**OCRmyPDF-Aufruf (Issue #50):** Standard ist
`ocrmypdf -l deu --output-type pdf --optimize 0 --skip-text`; vorhandene Textebenen
und Tags bleiben unverändert, nur bild-only Seiten werden OCRt. Nur der explizite
Redo-Zweig (`--redo-ocr`) nutzt `--redo-ocr --tagged-pdf-mode ignore` ohne
`--skip-text` (die Optionen schließen sich in OCRmyPDF 17.x aus) und verwirft die
Struktur bewusst.

**Formularfelder-Reihenfolge (Issue #20):** Unabhängig von der OCR-Policy werden
gefüllte AcroForm-Werte über `extract_pdf_form_fields()` rein lesend erfasst. Bei
`enrich_source` geschieht das **vor** der destruktiven In-Place-OCR (der Pre-OCR-Hash
wird als `source_sha256` gebunden, `extracted_before_ocr: true`); bei
`local_derivative` stammen die Werte aus dem unveränderten Original, nie aus dem
Derivat. Der begrenzte Status (`complete`/`no_fields`/`partial`/`dependency_missing`/
`error`) und die Warnungen werden durch Worker und Scheduler transportiert und in den
Filemap-Eintrag `files[<quelle>].form_fields` geschrieben; das kanonische Mirror-
Frontmatter wächst dabei nicht. `form_fields` ist ein **reserviertes generated Feld**
(nicht per Curation-Overlay fälschbar) und wird nur bei **unverändertem** Quell-Hash
(Convert-Skip-Pfad und Filemap-Generator) weitergetragen.

### Phase 4: Nachbereitung & Atomarer Write
1. **Bildlink-Neutralisierung:** `neutralize_missing_local_image_links()` ersetzt Verweise auf lokale Bilder, die nicht extrahiert wurden, durch neutrale Text-Hinweise.
2. **Frontmatter-Generierung:** `build_cloud_artifact_metadata()` erzeugt die 12 Pflichtschlüssel.
3. **Atomarer Write:** Schreiben via `tempfile` + `os.replace`.
4. **Retention & Orphan-Cleanup:** Mirror und Derivate gelöschter Quellen werden entfernt. Bleibt eine Quelle inventarisiert, deren Konvertierung fehlschlug (`conversion_required` oder `failed`), werden ihr Mirror und ihr Derivat nicht gelöscht. Ein retained Mirror wird nur dann als aktuell geführt, wenn sein Frontmatter-`source_sha256` dem aktuellen Quellhash entspricht; sonst schreibt der Konverter `mirror_stale: true` und Phase 2 adoptiert ihn weder als `artifact_metadata` noch als `converted`, sondern behält `conversion_required` und den `conversion_error` bei.

### Phase 5: Einzeldatei-Modus (`--only-file`, Issue #22)

`run_conversion()` verzweigt bei gesetztem `--only-file` **vor** der Storage-Schleife in
`run_only_file_conversion()`; der Voll-Scan-Code (Walk, Rename-Matching, Orphan-Cleanup,
Filemap-Neuaufbau) wird nicht betreten. Ablauf:

1. **Argumente:** Genau ein Storage (sonst `--storage-id`), `--extensions` ist unvereinbar
   (Exit 2), ebenso `--ocr-policy enrich_source` (der Einzelmodus mutiert die Quelle nie).
2. **Scope und Realpath:** lexikalisch unter `scan_dir`, außerhalb `exclude_dirs`; `realpath`
   der Quelle, der Output-Zone und der Filemap gegen die erwarteten Pfade; keine Links.
3. **Filemap lesen/validieren:** strikte Parse-/Schema-Prüfung vor jeder Mutation;
   `locate_source` weist Duplikate/Casing-Varianten ab (`duplicate_source`).
4. **Lock und Recovery (nur melden):** Der Lauf hält **OS-Locks pro Storage**:
   Output-Verzeichnis exklusiv, Elternordner shared, Filemap-Pfad exklusiv, sortiert
   akquiriert. Unter dem Lock wird die Filemap erneut gelesen. Ein offenes Journal derselben
   Quelle wird **nur gelesen und gemeldet**: kein Roll-forward, kein Roll-back, kein
   Verschieben, Umbenennen, Archivieren oder Löschen. Ist der frühere Lauf nachweislich
   committet (`published`/`committed` oder Live-Eintrag == aufgezeichneter neuer Digest),
   fährt dieser Lauf fort; jeder andere Zustand (planned, staged ohne passenden Live-Eintrag,
   unlesbares Journal, beschädigte aktive Version) ist unklar und beendet ihn fail-closed
   (`phase: journal`) vor jeder Mutation. Journal, Versionen und Stages bleiben liegen; die
   manuelle Abhilfe ist die Inspektion und das Entfernen des Journals.
5. **Version planen:** Für eine Konvertierung wird ein neues
   `<output_dir>/_versions/<quell-key>/<zeit-pid-id>/` gewählt; Mirror-/Derivatnamen kommen
   aus der bestehenden Pfadplanung. Für einen unveränderten Lauf bleiben die Eintragspfade.
6. **Konvertieren:** Worker/Timeout wie der Voll-Scan. Das Versionsverzeichnis wird
   angelegt und Mirror und Derivat werden **direkt hineingeschrieben** (kein
   Staging-Verzeichnis, kein Verzeichnis-Rename; der Mirror ohne Temp/Replace), gefsynct
   und gehasht; das Journal bleibt dabei `planned`. Der Worker löscht keine Artefakte:
   OCR schreibt direkt in das endgültige Derivat (kein `os.replace(x, x)`, Pfadgleichheit
   nach Entfernen des Windows-Extended-Prefix), und das DOC-Short-Path-Staging samt
   LibreOffice-Profil liegt — im Short-Path- wie im direkten Pfad — im kurzen Laufverzeichnis
   `data/ca-doc/<run-id>` (kanonische Data Zone) und bleibt liegen; die Profil-URI bleibt
   dort unter 120 Zeichen (sonst `conversion_required`).
7. **Commit:** Nur der Eintrag ändert sich; die Filemap wird unter OS-Lock in die aktuelle
   Datei gespleißt, Base-Hash vor Temp-Write und unmittelbar vor dem Replace geprüft. Als
   letzter Schritt vor dem atomaren `os.replace` wird `staged` gespeichert; danach
   folgen `published`/`committed`. Das Journal bleibt liegen (keine Archivierung). Das ist
   der einzige
   Commit-Punkt; die einzigen zulässigen `os.replace`-Aufrufe im Produktpfad sind Filemap
   und Journal. **Vor jeder Journalmutation** prüft der Lauf alle Einträge der
   Ziel-Filemap (auch den der Quelle, inkl. Windows-Aliasen): beansprucht einer den
   Journalpfad, endet er fail-closed (`journal`).
8. **Fehler/Retention:** Jeder Fehler vor dem Replace lässt die alte Version aktiv. **Es wird
   nichts gelöscht, verschoben, umbenannt, archiviert oder aufgeräumt**; die neue Version
   bleibt liegen und wird als `retained_version` gemeldet. Ein unlesbares JSON der
   Ziel-Filemap beendet den Lauf mit `filemap`, ein unklares Journal mit `journal`; in beiden
   Fällen ohne Mutation. Quelle und Ziel-Filemap werden nie hydratisiert (Attribute
   `OFFLINE`, `RECALL_ON_OPEN`, `RECALL_ON_DATA_ACCESS`; `stat`-Fehler auf existierendem Pfad
   fail-closed; erneute Prüfung unmittelbar vor jedem Öffnen, auch in der CAS-Digest-Prüfung
   und in jedem Quellzugriff des PDF-Workers). Ein existierendes Ziel, das ein **anderer
   Eintrag derselben Filemap** beansprucht oder das diesem Eintrag nicht gehört, wird nicht
   überschrieben (`mirror-plan`). Fremde Topics/Projekte werden nicht ermittelt.
   Der Mirror-Pfad ist nicht stabil; die Filemap ist die Quelle der Wahrheit. Der
   **Vollscan** bleibt unverändert und kennt den Version-Root nicht: er kann versionierte
   Quellen erneut konvertieren und versionierte Mirror als Waisen entfernen; Journale liest er
   nicht (bewusste Grenze der Scope-Reduktion). Locks liegen persistent unter
   `data/.locks/cloud-atlas/` (nicht gelöscht, `.gitignore` empfohlen). Keine
   Mehrdatei-Atomarität gegen Stromausfall, aber nie ein gemischter Versionszustand.

### Phase 6: Retention-Audit (Issue #97, rein lesend)

`retention_audit.py` ist **kein** Teil von `run_conversion()` und wird von
`sync_project_cloud.py` nicht aufgerufen. Es liest genau eine Ziel-Filemap und
deren `<output_dir>/_versions/` und schreibt nichts:

1. **Eingabe:** `--output-dir` und `--filemap` sind workspace-relativ und müssen in
   den kanonischen Data Zones liegen. Output, Filemap, Version-Root und jeder
   Katalogpfad werden **komponentenweise vor `realpath`/`scandir`/`open`** geprüft.
   Links, Junctions und Cloud-Platzhalter in Output-/Filemap-Vorfahren brechen den
   Audit fail-closed ab; in Katalog-Vorfahren verhindern sie nur das Lesen und die
   Verifikation (Audit läuft nicht-autoritativ weiter), auch bei externen
   Katalogzielen. Ein in der Filemap deklariertes `output_dir` muss per
   Alias-Vergleich zur auditierten Output-Zone passen. Eine Legacy-Filemap **ohne**
   `output_dir` wird fail-closed abgewiesen. Die Identität wird über die nur
   gelesenen `projects.json`/`topics.json` an `output_json` + `output_dir` gebunden
   (inkl. `filemap_json`/`cloud_mirror_dir` und synthetisierter Defaults). Jeder
   Katalog-Lese-/Parsefehler verhindert `verified`; ohne genau einen fehlerfreien
   Treffer ist die Klassifikation nicht autoritativ.
2. **Aktive Referenz:** Nur `markdown_mirror` und `derivative.path` derselben
   Filemap markieren ein Versionsverzeichnis als `active`. Windows-Pfadalias
   (case-insensitiv, abschließende Punkte/Leerzeichen je Komponente ignoriert) gilt
   **nur auf Windows**; auf case-sensitiven Plattformen muss der Pfad exakt passen,
   sonst bleibt die Referenz `unresolved`. Andere Filemaps, Konsumenten und manuelle
   Verweise werden nicht gelesen.
3. **Klassifikation:** Nicht aktive Versionen sind `retained`, wenn sie zu den
   neuesten 2 inaktiven Versionen ihres Quell-Keys gehören oder höchstens 90 Tage
   alt sind; sonst `quarantine_candidate`. **Älter als 365 Tage hat Vorrang** und ist
   auch dann Kandidat, wenn die Version unter den neuesten zwei liegt oder ≤ 90 Tage
   alt wäre. Das Alter wird ungerundet aus der mtime berechnet. Unerwartete Layouts
   und Links sind Kandidaten; ein referenziertes Link-/Junction-Run bleibt `active`
   und wird nie traversiert. Cloud-Platzhalter in Quell-Key-, Run- oder
   Inhaltsverzeichnissen werden nicht traversiert (Guard vor `realpath`/`scandir`).
   Ein unlesbarer Quell-Key mit aktiver Referenz wird `unresolved`/
   `potentially_active: true`, nicht Kandidat; `references.unresolved` und die
   menschliche Zusammenfassung nennen die Referenz. Ein Link/Platzhalter auf dem
   Version-Root bricht fail-closed ab.
4. **Bericht:** `data.retention` enthält Policy, Zähler, bekannte Bytes, Journal-
   Zusammenfassung und Einträge. Jeder Eintrag hat `action: "none"`,
   `foreign_reference_risk: "unknown"`, `safe_to_delete: false` und
   `safe_to_move: false`. `quarantine_candidate` ist nur eine logische
   Prüfempfehlung; der reservierte Bereich `<output_dir>/_quarantine/` wird weder
   angelegt noch beschrieben.
5. **Grenzen:** Kein Lock (nur lesend, Snapshot), keine automatische Bereinigung,
   keine physische Quarantäne, keine Kenntnis fremder Referenzen. Die Schwellen
   sind Empfehlungen, keine Speicherobergrenze. Ein späterer Vollscan kann den
   Version-Root weiterhin nicht schützen (Phase 5, Schritt 8).

## 3. Der Filemap-Generierungs-Lifecycle

Implementiert in [`scripts/gen_filemap.py`](../scripts/gen_filemap.py):

1. **Overlay-Laden:** Liest `filemap-curation.json` (falls vorhanden).
2. **Dateisystem-Traversierung:**
    - Nutzt denselben `core/scan_scope.walk_scoped()` wie der Konverter; ausgeschlossene Verzeichnisse werden nicht betreten, ähnlich benannte Siblings bleiben enthalten.
   - Filtert Dateien gegen interne Ignore-Regeln (`.git`, `~$*`, temporäre Dateien) und Kuratierungs-Muster.
3. **Knoten-Erstellung:**
   - Berechnet Dateigröße, Modifikationsdatum und SHA-256 Hash.
   - Weist MIME-Typ und Dateikategorie (`document`, `archive`, `image`, `data`) zu.
   - Ermittelt den zugehörigen relativen `mirror_path`.
4. **Validierung & Speicherung:**
   - Validiert die Gesamtstruktur im Speicher gegen `filemap.schema.json`.
    - Speichert atomar unter `memory/cloud/<id>/filemap.json`.
    - Schreibt effektive `exclude_dirs` in JSON, lesbare Filemap und Storage-Ergebnis;
      ausgeschlossene Quellen nehmen nicht am Hash-Rename-Matching teil.

---

## 4. Der Orchestrierte Sync-Runner

Implementiert in [`scripts/sync_project_cloud.py`](../scripts/sync_project_cloud.py):

1. Kapselt beide Einzelschritte in einen sequentiellen, deterministischen Lauf.
2. Führt zuerst die Konvertierung aus (damit generierte Mirrors existieren).
3. Führt anschließend die Filemap-Generierung aus (damit die Filemap den frischesten Konvertierungsstatus der Mirrors widerspiegelt).
4. Emittiert das standardisierte Envelope-Ergebnis.
