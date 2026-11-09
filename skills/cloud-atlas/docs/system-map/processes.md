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
   (Exit 2). Der Pfad ist workspace-relativ wie die übrigen Pfad-Flags.
2. **Scope und Realpath:** lexikalisch unter `scan_dir` und außerhalb `exclude_dirs`;
   `realpath` der Quelle muss exakt `realpath(cloud)/Rest` sein (kein Symlink/Junction/
   Casing-Abweichung); Output-Zone und Filemap dürfen nicht über Links entweichen oder
   die Cloud überlappen.
3. **Filemap lesen und validieren:** `core/filemap_merge.parse_filemap` (strikt, keine
   doppelten Schlüssel, keine Konstanten wie `NaN`) lokalisiert die Roh-Spans aller
   Mitglieder; `validate_filemap_document` prüft Container und alle Einträge im Speicher.
   Fehlende Filemap oder Schemafehler stoppen. `locate_source` weist mehrere/abweichend
   geschriebene Einträge derselben Quelle ab (`duplicate_source`).
4. **Zielplanung:** `plan_markdown_mirrors` nur über die Quelle und ihre kollidierende
   Gruppe; Ziele, die fremde Einträge beanspruchen, geschützte Namen, nicht eigene
   vorhandene Dateien und Link-Ausbrüche stoppen als `mirror-plan`.
5. **Konvertieren:** gleiche Entscheidung (`determine_needs_conversion`) und gleicher
   Worker/Timeout wie der Voll-Scan, aber eine Aufgabe unter `ArtifactTransaction`
   (Backup von Mirror, Derivat, bei `enrich_source` Quelle; Verzeichnis-Snapshots).
6. **Merge:** Eintrag bauen (`build_converted_file_entry` bzw. `build_unchanged_file_entry`,
   dann `merge_curated_metadata`), validieren, `splice_entry` + `verify_splice`, schreiben
   per Temp-Datei + `os.replace`. Identischer Eintrag → kein Schreiben der Filemap.
7. **Fehler:** `ArtifactTransaction.rollback()` stellt alle Artefakte wieder her; Exit ≠ 0.
   Nur Erfolg schreibt Artefakte und Eintrag; `conversion_required` wird gemeldet, nicht
   persistiert.

---

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
