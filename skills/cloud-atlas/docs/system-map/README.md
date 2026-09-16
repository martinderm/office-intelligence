# Cloud-Atlas — Subsystem System Map

> **Typ**: ICM Form 6 (`system-map`), Sub-Skill-Ebene (L2)  
> **Subsystem**: [`skills/cloud-atlas`](../SKILL.md)  
> **Ziel**: Kompakte, zitierbare Architekturkarte der Filemap-, Konvertierungs- und Synchronisations-Engine (65 Dateien, 41 Tests) zur Vermeidung von Context-Bloat bei Refactorings und Konvertierungs-Erweiterungen.  
> **Gültig für**: `skills/cloud-atlas/` relativ zum Repository-Root

---

## 1. Systemübersicht

`cloud-atlas` ist das zentrale Subsystem für die Kartierung, Spiegelung und Konvertierung externer Cloud-Speicher (Nextcloud, Google Drive, OneDrive, Netzlaufwerke) in agentenlesbare lokale Markdown-Strukturen unter Wahrung von Provenienz und Integrität.

```
┌────────────────────────────────────────────────────────────────────────┐
│                   Externe Cloud-Speicher (Originale)                   │
│         (DOCX, PPTX, XLSX, PDF, Bilddaten, unstrukturierte Bäume)      │
└───────────────────────────────────┬────────────────────────────────────┘
                                    │ Read-Only Scan & Hashing
                                    ▼
┌────────────────────────────────────────────────────────────────────────┐
│                          cloud-atlas Engine                            │
│  ├─ sync_project_cloud.py (Orchestrierter Sync-Runner)                 │
│  ├─ gen_filemap.py (Filemap-Generator, Schema 1, 53 KB)                │
│  ├─ convert_cloud_docs.py (Pandoc-, LibreOffice- & OCR-Pipeline, 103 KB)│
│  └─ core/ (Hilfsmodule)                                                │
│      ├─ metadata.py (Frontmatter- & Metadaten-Schemas)                 │
│      ├─ curation.py (Curation-Overlays & manuelle Overrides)           │
│      └─ mirror_paths.py (Pfadberechnung & Kollisionsvermeidung)        │
└───────────────────────────────────┬────────────────────────────────────┘
                                    │ Atomare Disk-Writes (Lock-geschützt)
                                    ▼
┌────────────────────────────────────────────────────────────────────────┐
│             Ziel-Workspace Cloud-Zone (memory/cloud/)                  │
│  ├─ projects/<id>/filemap.json (Maschinenlesbare Bestandsaufnahme)     │
│  ├─ projects/<id>/filemap-curation.json (Manuelle Kuratierungsregeln)  │
│  └─ projects/<id>/mirrors/ (Generierte Markdown-Derivate mit Metadaten)│
└────────────────────────────────────────────────────────────────────────┘
```

---

## 2. Modul-Topographie (`scripts/`)

| Komponente | Dateipfade | Primäre Verantwortlichkeit |
| :--- | :--- | :--- |
| **Sync-Orchestrierung** | [`scripts/sync_project_cloud.py`](../scripts/sync_project_cloud.py) | Geordneter 2-Phasen-Lauf: Konvertierung gefolgt von Filemap-Aktualisierung für Projekte oder Topics. |
| **Filemap-Engine** | [`scripts/gen_filemap.py`](../scripts/gen_filemap.py) | Rekursiver Dateisystemscan, SHA-256 Hashing, Schema-1-Generierung, Validierung gegen `filemap.schema.json`. |
| **Konvertierungs-Engine** | [`scripts/convert_cloud_docs.py`](../scripts/convert_cloud_docs.py) | Pandoc-/LibreOffice-Bridge, PDF-OCR-Pipeline (`local_derivative` vs. `enrich_source`), Frontmatter-Injektion. |
| **Metadaten & Frontmatter** | [`scripts/core/metadata.py`](../scripts/core/metadata.py) | Validierung der 12 Pflichtschlüssel und kanonischen Frontmatter-Eigenschaften. |
| **Kuratierungs-Overlay** | [`scripts/core/curation.py`](../scripts/core/curation.py) | Laden und Mergen manueller Kuratierungsentscheidungen aus `filemap-curation.json`. |
| **Spiegelpfad-Berechnung** | [`scripts/core/mirror_paths.py`](../scripts/core/mirror_paths.py) | Deterministische Transformation von Cloud-Pfaden in lokale Mirror-Pfade, Vermeidung von Namenskollisionen. |

---

## 3. Navigationsmatrix

| Dimension | Dokument | Inhalt |
| :--- | :--- | :--- |
| **Nomen** (Struktur & Zustand) | [`objects.md`](objects.md) | `filemap.json` (Schema 1), `filemap-curation.json`, Frontmatter-Schemas (12 Pflichtfelder), Mirror-Payloads. |
| **Verben** (Ablauf & Transformation) | [`processes.md`](processes.md) | Filemap-Generierung, Dokumentenkonvertierung, OCR-Verzweigung, Sync-Lifecycle. |
| **Seiteneffekte** (Umwelt & Grenzen) | [`effects.md`](effects.md) | Pandoc-/LibreOffice-Isolation, Tesseract-OCR, 240-Zeichen Pfadlimit (Windows), Lock-Schutz. |

---

## 4. Die 5 fundamentalen Cloud-Atlas-Invarianten

1. **Unveränderlichkeit externer Originale:** Cloud-Speicher bleiben standardmäßig read-only. Modifikationen finden als lokale Markdown-Spiegel in der Workspace-Zone `memory/cloud/` statt.
2. **OCR-Policy Disziplin:** Standard ist `local_derivative` (lokales Textderivat). `enrich_source` (In-Place OCR im Cloud-PDF) ist nur für un-signierte, beschreibbare PDFs mit voller Provenienz zulässig.
3. **Zirkelfreies Payload-Hashing:** `artifact_sha256` im Frontmatter repräsentiert exakt die Bytes des reinen Markdown-Bodys, niemals der Datei inklusive Frontmatter (Ausschluss selbstreferenzieller Hash-Drifts).
4. **Strikte Frontmatter-Konformität:** Erzeugte Markdown-Dateien erzwingen exakt die 12 kanonischen Pflichtfelder (`REQUIRED_CLOUD_FRONTMATTER_KEYS`).
5. **Kuratierungs-Stabilität:** Manuelle Overrides in `filemap-curation.json` übersteuern automatisierte Heuristiken deterministisch.
