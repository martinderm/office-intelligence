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
1. Scannt den Quellordner nach unterstützten Formaten (`.pdf`, `.docx`, `.xlsx`, `.pptx`, `.doc`).
2. Liest bestehende Mirror-Dateien unter `memory/cloud/<id>/mirrors/`.
3. Vergleicht den aktuellen SHA-256 der Quelldatei mit dem im Frontmatter gespeicherten `source_sha256`.
4. Ist der Hash identisch und nicht `--force` gesetzt, wird die Datei übersprungen (Idempotenz).

### Phase 2: Formatspezifische Transformation
* **DOCX / DOC:** Konvertierung via Pandoc nach GitHub Flavored Markdown (GFM). Bei alten Binär-DOCs Fallback über LibreOffice.
* **PPTX:** Extraktion von Folientexten und Notizen in hierarchische Markdown-Strukturen.
* **XLSX:** Tabellen-Extraktion mit Markdown-Tabellen-Formatierung.

### Phase 3: Die PDF- & OCR-Verzweigungslogik
Bei PDFs prüft der Konverter, ob digitaler Text vorhanden ist. Fehlt dieser (reine Scans / Bild-PDFs), greift die OCR-Policy:

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

### Phase 4: Nachbereitung & Atomarer Write
1. **Bildlink-Neutralisierung:** `neutralize_missing_local_image_links()` ersetzt Verweise auf lokale Bilder, die nicht extrahiert wurden, durch neutrale Text-Hinweise.
2. **Frontmatter-Generierung:** `build_cloud_artifact_metadata()` erzeugt die 12 Pflichtschlüssel.
3. **Atomarer Write:** Schreiben via `tempfile` + `os.replace`.

---

## 3. Der Filemap-Generierungs-Lifecycle

Implementiert in [`scripts/gen_filemap.py`](../scripts/gen_filemap.py):

1. **Overlay-Laden:** Liest `filemap-curation.json` (falls vorhanden).
2. **Dateisystem-Traversierung:**
   - Scannt alle Verzeichnisse unterhalb des Speicherpfads.
   - Filtert Dateien gegen interne Ignore-Regeln (`.git`, `~$*`, temporäre Dateien) und Kuratierungs-Muster.
3. **Knoten-Erstellung:**
   - Berechnet Dateigröße, Modifikationsdatum und SHA-256 Hash.
   - Weist MIME-Typ und Dateikategorie (`document`, `archive`, `image`, `data`) zu.
   - Ermittelt den zugehörigen relativen `mirror_path`.
4. **Validierung & Speicherung:**
   - Validiert die Gesamtstruktur im Speicher gegen `filemap.schema.json`.
   - Speichert atomar unter `memory/cloud/<id>/filemap.json`.

---

## 4. Der Orchestrierte Sync-Runner

Implementiert in [`scripts/sync_project_cloud.py`](../scripts/sync_project_cloud.py):

1. Kapselt beide Einzelschritte in einen sequentiellen, deterministischen Lauf.
2. Führt zuerst die Konvertierung aus (damit generierte Mirrors existieren).
3. Führt anschließend die Filemap-Generierung aus (damit die Filemap den frischesten Konvertierungsstatus der Mirrors widerspiegelt).
4. Emittiert das standardisierte Envelope-Ergebnis.
