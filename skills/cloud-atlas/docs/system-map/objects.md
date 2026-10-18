# Cloud-Atlas — Subsystem System Map: Nomen (Objects)

> **Typ**: ICM Form 6 (`system-map`), Dimension: Nomen  
> **Subsystem**: [`skills/cloud-atlas`](../SKILL.md)  
> **Ziel**: Vollständige Dokumentation aller Datenstrukturen, Schemas, Indizes und Frontmatter-Definitionen von Cloud-Atlas mit Quellcode-Zitaten.  
> **Gültig für**: `skills/cloud-atlas/` und `memory/cloud/` im Ziel-Workspace

---

## 1. Das Filemap-Objekt (`filemap.json`)

Die Filemap ist das maschinenlesbare Inventar des konfigurierten Scan-Scopes eines
angebundenen Cloud-Speichers. `exclude_dirs` dokumentiert absichtliche Auslassungen.

* **Dateipfad:** `memory/cloud/projects/<project-id>/filemap.json`
* **Implementierungsdatei:** [`scripts/gen_filemap.py`](../scripts/gen_filemap.py)
* **Schema-Referenz:** [`references/filemap.schema.json`](../references/filemap.schema.json)
* **Aktuelle Schema-Version:** `1` (`FILEMAP_SCHEMA_VERSION`)

### 1.1 Root-Struktur
```json
{
  "schema_version": 1,
  "$schema": "https://raw.githubusercontent.com/martinderm/office-intelligence/main/skills/cloud-atlas/references/filemap.schema.json",
  "kind": "cloud-filemap",
  "scope": "project",
  "storage_id": "default",
  "project": "example",
  "project_title": "Example",
  "scan_dir": "data/cloud/shared",
  "output_dir": "memory/cloud/projects/example",
  "exclude_dirs": ["Specialized Area"],
  "updated_at": "2026-10-16 12:00:00",
  "files": {}
}
```

`exclude_dirs` ist ein additives Schema-1-Feld (Legacy: fehlend = `[]`). Es
enthält die effektiven, normalisierten, relativ zu `scan_dir` geltenden
Verzeichnispfade. `scripts/core/scan_scope.py` validiert sowohl Projekt-/Topic-
als auch Subtopic-Konfigurationen; `gen_filemap.validate_filemap()` prüft die
kanonische Provenienz und das Fehlen ausgeschlossener Dateieinträge.

### 1.2 PDF-Formularfelder-Vertrag (`files[<quelle>].form_fields`)

Für PDF-Quellen schreibt der Konverter (Issue #20) ein additives, begrenztes
Objekt neben `conversion_status`/`derivative`:

```json
"form_fields": {
  "status": "complete",
  "count": 3,
  "tool": "pymupdf",
  "tool_version": "1.28.2",
  "warnings": [],
  "source_sha256": "a1b2c3d4…",
  "extracted_before_ocr": false
}
```

* `status` ∈ `complete` | `no_fields` | `partial` | `dependency_missing` | `error`.
  Grenzlogik: Dokument-Iterationsfehler **ohne** gelesene Felder ⇒ `error`, mit
  gelesenen Feldern ⇒ `partial`.
* `tool`/`tool_version` belegen die tatsächliche Extraktionsquelle (`pymupdf` oder
  Legacy-`fitz`); `dependency_missing` bedeutet fehlende optionale Fähigkeit
  (fehlender **oder** nicht importierbarer Modul, z. B. Nicht-`ImportError`).
* `warnings` ist begrenzt (max. `MAX_PDF_FORM_FIELD_WARNINGS`) und trägt Codes wie
  `open_failed`, `document_iteration_failed`, `page_read_failed`,
  `widget_read_failed`, `field_limit_reached`, `field_text_truncated`,
  `field_name_truncated`, `field_type_truncated`, `pymupdf_missing`. Die begrenzte
  Liste verbirgt den `partial`-Status nicht (`degraded_reasons` steuern ihn).
* `source_sha256` bindet den Hash des PDFs, aus dem die Werte gelesen wurden; bei
  `enrich_source` ist das der **Pre-OCR**-Hash und `extracted_before_ocr` ist `true`.

Begrenzungen: `MAX_PDF_FORM_FIELDS`, `MAX_PDF_FORM_FIELD_TEXT`,
`MAX_PDF_FORM_FIELD_NAME`, `MAX_PDF_FORM_FIELD_TYPE`,
`MAX_PDF_FORM_FIELD_WARNINGS` (Wert/Name/Typ werden hart gekürzt; die Zelle wird
über `literal_markdown_cell()` einzeilig und literal gerendert).

**Besitz & Retention (Issue #20, A3/A4):** `form_fields` ist ein **generated Feld**
(`core/curation.py:GENERATED_FILEMAP_FIELDS`) — ein Curation-Overlay darf es weder
über `custom` setzen noch überschreiben (Loader: `CurationOverlayError`;
Merge-Guard: generated Werte gewinnen). Für eine **unveränderte** Quelle (gleicher
`sha256`) tragen der Convert-Skip-Pfad und `gen_filemap.py` den bestehenden Vertrag
explizit weiter; bei geändertem/neuem Quell-Hash wird er **nicht** übernommen.

Das Objekt ist **nicht** Teil des kanonischen Mirror-Frontmatters (die 12
Pflichtschlüssel bleiben unverändert); ein unbekanntes `form_fields`-Frontmatter wäre
eine Vertragsverletzung. `gen_filemap.py` und die Schema-Validierung behandeln das
Feld additiv.

---

## 2. Das Kuratierungs-Overlay (`filemap-curation.json`)

Ermöglicht menschlichen Nutzern und Agenten, Konvertierungs- und Synchronisationsentscheidungen granular zu steuern, ohne den Generator-Code zu verändern.

* **Dateipfad:** `memory/cloud/projects/<project-id>/filemap-curation.json`
* **Implementierungsdatei:** [`scripts/core/curation.py`](../scripts/core/curation.py)
* **Schema-Referenz:** [`references/filemap-curation.schema.json`](../references/filemap-curation.schema.json)
* **Felder:**
  * `curation_version`: Integer (Standard: `1`)
  * `ignore_patterns`: Liste von Glob-Patterns (z. B. `["*.tmp", "~$*"]`)
  * `file_overrides`: Dictionary gemappt auf relative Pfade:
    * `skip_conversion`: Boolean (true verhindert Konvertierung)
    * `ocr_policy`: Enum (`"enrich_source"`, `"local_derivative"`, `"disabled"`)
    * `custom_mirror_path`: Expliziter Zielpfad im lokalen Mirror-Baum
    * `custom_tags`: Zusätzliche Schlagworte

---

## 3. Lokale Markdown-Mirrors (Data-Zone Frontmatter)

Konvertierte Dokumente werden als Markdown mit strikt typisiertem YAML-Frontmatter abgelegt.

* **Implementierungsdatei:** [`scripts/convert_cloud_docs.py`](../scripts/convert_cloud_docs.py#L48-L71) und [`scripts/core/metadata.py`](../scripts/core/metadata.py)
* **Speicherort:** `memory/cloud/projects/<project-id>/mirrors/...`

### 3.1 Die 12 Pflichtschlüssel (`REQUIRED_CLOUD_FRONTMATTER_KEYS`)
Jedes generierte Mirror-Dokument muss diese 12 Schlüssel zwingend enthalten:

| Schlüssel | Typ | Bedeutung |
| :--- | :--- | :--- |
| `zone` | String | Fest auf `"cloud"`. |
| `trust_level` | String | Vertrauensstufe der Quelle (z. B. `"external"`, `"curated"`). |
| `status` | String | Status des Derivats (z. B. `"active"`, `"archived"`). |
| `source_uri` | String | Relativer Pfad oder URI des Cloud-Originals. |
| `source_sha256` | String (64-Hex) | SHA-256 Hash der Originaldatei. |
| `artifact_sha256` | String (64-Hex) | SHA-256 Hash des reinen Markdown-Bodys (siehe Hashing-Regel). |
| `synced_at` | RFC 3339 String | Zeitstempel der Konvertierung / Synchronisation (timezone-aware). |
| `converter` | String | Name und Version des Konverters (z. B. `"pandoc-3.x"`). |
| `data_classification` | Enum | `"public"`, `"internal"`, `"confidential"`, `"restricted"`. |
| `retention_class` | String | Aufbewahrungsklasse. |
| `owner` | String | Zuständiger Projekt- oder Lead-Bezug. |
| `instructions_are_data` | Boolean | Fest auf `true` (Untrusted-Content-Schutz). |

### 3.2 Optionale Kanonische Frontmatter-Felder
* `source_version`: Revisions- oder Versionskennung der Quelle.
* `origin_classifications`: Liste ursprünglicher Klassifikationen.
* `export_policy`: Enum (`"allowed"`, `"approval_required"`, `"prohibited"`).
* `promotion_policy`: Enum (`"allowed"`, `"approval_required"`, `"prohibited"`).

### 3.3 Zirkelfreies Payload-Hashing
Definiert in [`scripts/convert_cloud_docs.py`](../scripts/convert_cloud_docs.py#L62-L71):
```python
def calculate_markdown_payload_sha256(markdown_body):
    """Hash the converted Markdown payload, never the full self-referential file."""
    payload = (markdown_body or "").encode("utf-8", errors="replace")
    return hashlib.sha256(payload).hexdigest()
```
* **Garantie:** Da das Frontmatter den Hash enthält, würde ein Hashing der gesamten Datei eine unendliche Rekursion bzw. Hash-Instabilität erzeugen. `artifact_sha256` bindet daher deterministisch nur den Body.

---

## 4. CLI-Ergebnisobjekt (Canonical Envelope)

Alle Skripte (`gen_filemap.py`, `convert_cloud_docs.py`, `sync_project_cloud.py`) emittieren ein einheitliches Ergebnisobjekt:

```json
{
  "action": "sync_project_cloud",
  "success": true,
  "state": "Completed",
  "message": "Cloud-Speicher synchronisiert",
  "data": {
    "converted_files": 5,
    "ocr_applied": 1,
    "filemap_path": "memory/cloud/projects/demo/filemap.json"
  },
  "error": null
}
```
