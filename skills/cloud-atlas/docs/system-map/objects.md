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

### 4.1 Einzeldatei-Ergänzung `data.only_file` (Issue #22)

Nur bei `convert_cloud_docs.py --only-file` trägt `data` zusätzlich (Envelope-Schlüssel
unverändert):

```json
"only_file": {
  "source": "data/cloud/DEMO/g_neu.doc",
  "action": "created",
  "entry_changed": true,
  "duplicate_source": false,
  "artifacts_changed": true,
  "recovered": null,
  "retained_version": null,
  "version": "memory/cloud/topics/demo/_versions/6bf52975593f2a7b/20261109T173512811272Z-40368-4e66145b",
  "journal": null,
  "mirror": "memory/cloud/topics/demo/_versions/6bf52975593f2a7b/20261109T173512811272Z-40368-4e66145b/g_neu.md"
}
```

`action` ∈ `created` | `updated` | `unchanged` | `none` (Lauf vor einer Entscheidung
gestoppt). `duplicate_source` ist bei mehrfachen/abweichend geschriebenen Einträgen der
Quelle `true` (Lauf scheitert). `entry_changed` zeigt eine Filemap-Änderung an.
`artifacts_changed` ist `true`, wenn eine neue Version geschrieben wurde.
`recovered` ist `committed`, wenn ein früherer Lauf nachweislich committet hat, sonst
`null`. `version` ist das neue Versionsverzeichnis (bei Konvertierung); `retained_version`
nennt das reell existierende Versionsverzeichnis, das liegen blieb, sonst `null`.
**Die Recovery entfernt nie eine Datei und verschiebt nie ein Artefakt**, und sie archiviert
das Journal nicht: Journale, Versionen, Reste und Commit-Temps bleiben liegen
(Retention ist bewusst nicht Teil dieses Modus; §5 beschreibt nur die rein lesende
logische Klassifikation. Eine bereits committete, später überholte Version ist
historisch und wird im Lauf nicht gemeldet). `journal` nennt ein offenes bzw.
liegengebliebenes Journal — der Lauf, der es gemeldet hat, hat dann fail-closed nichts
geschrieben. `mirror` ist der neue oder aktuelle Mirror-Pfad. Fehlerphasen:
`only-file`, `filemap`, `mirror-plan`, `curation`, `conversion`, `conflict`, `journal`,
`runtime`. Der Eintrag selbst folgt dem Filemap-Objekt (1.); `markdown_mirror` und
`derivative.path` zeigen in den reservierten Version-Root und sind **nicht stabil** — die
Filemap ist die Quelle der Wahrheit. `artifact_metadata` entspricht dem geschriebenen
Mirror-Frontmatter. Das Journal selbst (`<output_dir>/.only-file-journal/<key>.json`) ist
kein Filemap- oder Artefakt-Objekt; es wird geschrieben und **nie** umbenannt, archiviert
oder gelöscht. Vor jeder Journalmutation prüft der Lauf alle Einträge der Ziel-Filemap
(auch den der Quelle, inkl. Windows-Aliasen wie einem abschließenden Punkt): beansprucht
einer den Journalpfad, endet der Lauf fail-closed (`journal`).

### 4.2 Scope-Grenzen des Einzeldatei-Modus (bewusst)

- **Kein Vollscan-Anteil:** Ein regulärer Vollscan kennt `_versions/` und
  `.only-file-journal/` nicht und kann versionierte Quellen erneut konvertieren bzw.
  versionierte Mirror als Waisen entfernen.
- **Nur Ziel-Filemap als Referenzquelle:** Andere Topics/Projekte werden nicht gelesen; deren
  Verweise auf Pfade dieses Outputs sind ungeschützt.
- **Keine physische Quarantäne, keine Archivierung, kein Aufräumen:** alle Reste bleiben
  liegen. Die rein lesende logische Klassifikation aus §5 ist kein Bestandteil des Laufs.
- **Kein Move/Delete:** Versionen werden direkt geschrieben (kein Verzeichnis-Rename); der
  Worker löscht keine Artefakte (OCR schreibt direkt, Short-Path-Staging und
  LibreOffice-Profil bleiben im kurzen Laufverzeichnis `data/ca-doc/<run-id>` — auch im
  direkten Pfad). Einzige
  Ausnahme sind die atomaren `os.replace` von Filemap und Journal. Die OCR-Fähigkeitsprobe
  läuft nur im Vollscan; unser Code legt im `--only-file`-Pfad kein Temp-Verzeichnis
  außerhalb der Data Zones an, externe Programme verwalten ihre Temp-Dateien selbst.
- **Kein Sibling-Schutz:** nur derselbe Storage ist gesperrt.
- **Kein Roll-forward/Roll-back:** Recovery meldet und blockiert.
- **Platzhalter-Prüfung nur für Quelle und Ziel-Filemap:** Output-Artefakte werden nicht
  geprüft.

---

## 5. Retention-Audit-Objekt (Issue #97)

`retention_audit.py` liefert einen kanonischen Envelope mit `data.retention`. Das
Objekt ist ein **reiner Bericht** und keine Handlungsanweisung:

```json
"retention": {
  "policy": {
    "id": "cloud-atlas-retention-v1",
    "retained_inactive_versions": 2,
    "retained_inactive_age_days": 90.0,
    "quarantine_age_days": 365.0,
    "automatic_actions": "none",
    "storage_bound": "none; these thresholds are advisory labels, not a quota"
  },
  "output_dir": "memory/cloud/topics/demo",
  "filemap": "memory/cloud/topics/demo/filemap.json",
  "version_root": "memory/cloud/topics/demo/_versions",
  "identity": {
    "status": "verified",
    "classification_authoritative": true,
    "source": "catalog",
    "match_count": 1,
    "target": {
      "scope": "topic", "owner_id": "demo", "subtopic_id": null,
      "storage_id": "default",
      "output_json": "memory/cloud/topics/demo/filemap.json",
      "output_dir": "memory/cloud/topics/demo"
    }
  },
  "classification_authoritative": true,
  "quarantine_area": "memory/cloud/topics/demo/_quarantine",
  "reserved_area_present": false,
  "journals": {
    "path": "memory/cloud/topics/demo/.only-file-journal",
    "present": true,
    "entries": 1,
    "bytes": 142,
    "classification": "journal_not_managed",
    "unsafe": false
  },
  "counts": {"active": 1, "retained": 2, "quarantine_candidate": 1, "unresolved": 0},
  "entries": [
    {
      "path": "memory/cloud/topics/demo/_versions/6bf52975593f2a7b/20261109T173512811272Z-40368-4e66145b",
      "source_key": "6bf52975593f2a7b",
      "classification": "active",
      "reasons": ["filemap_reference"],
      "active": true,
      "age_days": 2.1,
      "unsafe": false,
      "placeholders": false,
      "potentially_active": false,
      "authoritative": true,
      "action": "none",
      "foreign_reference_risk": "unknown",
      "safe_to_delete": false,
      "safe_to_move": false,
      "quarantine": {"logical": false, "physical": false}
    }
  ]
}
```

* `active` ist ausschließlich die von **dieser** Ziel-Filemap referenzierte
  Version (`markdown_mirror` oder `derivative.path`). Nur auf Windows werden
  Windows-Pfadalias verwendet (case-insensitiv, abschließende Punkte/Leerzeichen
  je Komponente ignoriert); auf case-sensitiven Plattformen muss der Pfad exakt
  passen, sonst bleibt die Referenz `unresolved`. Ein referenziertes
  Link-/Junction-Run bleibt `active` und wird nie traversiert.
* `retained` = neueste **2** inaktive Versionen je Quell-Key **oder** höchstens
  **90 Tage** alt. `quarantine_candidate` = älter als **365 Tage** oder außerhalb
  des Counts und älter als 90 Tage. **Die 365-Tage-Regel hat Vorrang:** eine
  >365-Tage-Version ist auch dann Kandidat, wenn sie unter den neuesten zwei liegt
  oder ≤ 90 Tage alt wäre. `age_days` wird ungerundet aus der mtime des
  Versionsverzeichnisses (UTC) berechnet und nur für die Anzeige gerundet; die
  Klassifikation ist ein Snapshot, keine Manipulationsgarantie. `reasons` macht die
  Entscheidung nachvollziehbar.
* Cloud-Platzhalter in Quell-Key-, Run- oder Inhaltsverzeichnissen werden nie
  traversiert. Nicht aktive betroffene Versionen sind `cloud_placeholder_*`.
  Kandidaten, referenzierte aktive bleiben `active` mit `unsafe: true` und
  `placeholders: true`. Ist das Quell-Key-Verzeichnis selbst unlesbar (Link,
  Platzhalter, Datei/unerwartet) und zeigt eine aktive Referenz hinein, wird es
  `unresolved` mit `potentially_active: true`; die Referenz steht unter
  `references.unresolved` und wird in der menschlichen Zusammenfassung genannt.
* `identity` bindet die Filemap über die nur gelesenen `projects.json`/
  `topics.json` an `output_json` + `output_dir`, inklusive der Aliasfelder
  `filemap_json`/`cloud_mirror_dir` und synthetisierter Defaults. Nur genau ein
  Treffer **ohne jeden Katalog-Lese-/Parsefehler** setzt
  `classification_authoritative: true`; sonst ist die Klassifikation
  unverifiziert/mehrdeutig, jeder Eintrag trägt `authoritative: false`, und
  Kandidaten sind nicht-autoritative Prüfhinweise. Eine Filemap ohne deklariertes
  `output_dir` wird fail-closed abgewiesen, weil ihre Zugehörigkeit nicht bewiesen
  werden kann. Output, Filemap und Katalogpfade werden komponentenweise vor
  `realpath`/`scandir`/`open` geprüft. Links/Junctions/Platzhalter in
  Output-/Filemap-Vorfahren brechen den Audit ab; in Katalog-Vorfahren verhindern
  sie nur das Lesen und die Verifikation, nicht den Audit.
* `quarantine.logical` markiert nur die Prüfempfehlung. `physical` ist immer
  `false`; `<output_dir>/_quarantine/` ist ein reservierter Namensraum, den der
  Audit weder anlegt noch beschreibt.
* `journals` ist **kein** Retentionsobjekt: `classification:
  "journal_not_managed"`, keine Kandidaten, keine Archivierung.
* `action`, `safe_to_delete` und `safe_to_move` sind konstant. Fremde Filemaps,
  Konsumenten und manuelle Verweise sind nicht ermittelbar
  (`foreign_reference_risk: "unknown"`); ein manuelles Verschieben/Löschen liegt
  außerhalb des Produktvertrags. Die Bytes sind ein Snapshot, keine Obergrenze.
* Der Audit liest nur; er braucht keinen Workspace-Lock und verändert weder
  Filemap noch Version-Root. Er kennt die Vollscan-Grenze aus §4.2 und schützt
  keine Version vor einem späteren Vollscan.
