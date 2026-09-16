# Cloud-Atlas — Subsystem System Map: Nomen (Objects)

> **Typ**: ICM Form 6 (`system-map`), Dimension: Nomen  
> **Subsystem**: [`skills/cloud-atlas`](../SKILL.md)  
> **Ziel**: Vollständige Dokumentation aller Datenstrukturen, Schemas, Indizes und Frontmatter-Definitionen von Cloud-Atlas mit Quellcode-Zitaten.  
> **Gültig für**: `skills/cloud-atlas/` und `memory/cloud/` im Ziel-Workspace

---

## 1. Das Filemap-Objekt (`filemap.json`)

Die Filemap ist das vollständige, maschinenlesbare Inventar eines angebundenen Cloud-Speichers.

* **Dateipfad:** `memory/cloud/projects/<project-id>/filemap.json`
* **Implementierungsdatei:** [`scripts/gen_filemap.py`](../scripts/gen_filemap.py)
* **Schema-Referenz:** [`references/filemap.schema.json`](../references/filemap.schema.json)
* **Aktuelle Schema-Version:** `1` (`FILEMAP_SCHEMA_VERSION`)

### 1.1 Root-Struktur
```json
{
  "schema_version": 1,
  "generated_at": "2026-09-16T12:00:00Z",
  "storage_id": "default",
  "source_root": "rel/path/to/cloud",
  "files_count": 42,
  "total_bytes": 10485760,
  "files": [
    {
      "relative_path": "docs/bericht.docx",
      "size_bytes": 24576,
      "mtime": "2026-09-15T10:30:00Z",
      "sha256": "4a5b6c...",
      "mime_type": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
      "category": "document",
      "mirror_path": "mirrors/docs/bericht.md",
      "conversion_status": "converted"
    }
  ]
}
```

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
