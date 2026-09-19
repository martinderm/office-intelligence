# Office Intelligence — Paket System Map: Nomen (Objects)

> **Typ**: ICM Form 6 (`system-map`), Dimension: Nomen  
> **Ziel**: Vollständige Dokumentation aller Datenstrukturen, Schemas, Indizes und Metadaten-Objekte, die bundle-weit oder über Desk-Grenzen hinweg verwendet werden.  
> **Gültig für**: Repository Root & konsumierende Workspaces (relativ)

---

## 1. Das Datenzonen-Modell (Target Workspace Contract)

Das Skill-Bundle mutiert keine eigenen Daten im Bundle-Verzeichnis, sondern erzeugt, pflegt und liest Datenstrukturen in den vier kanonischen Zonen eines konsumierenden Agent-Workspaces:

```
<workspace-root>/
├── memory/
│   ├── references/          ◄── Dauerhafte Referenz- und Katalogdaten (SSOT)
│   │   ├── projects/        (projects.json, Workpackages, Projekt-Notizen)
│   │   └── topics/          (topics.json, Subtopics, Themen-Notizen)
│   ├── evidence/            ◄── Auditsichere, unveränderliche Fach-Nachweise
│   │   ├── meetings/        (Protokolle, Aufzeichnungsmetadaten)
│   │   ├── events/          (Konferenzprogramme, Vortragsmitschriften)
│   │   └── mail/            (Verifizierte Mail-Dossiers & Intake-Logs)
│   └── cloud/               ◄── Lokale Spiegel und Konvertierungs-Derivate
│       ├── projects/        (Cloud-Atlas Projekt-Mirrors & Derivate)
│       └── topics/          (Cloud-Atlas Topic-Mirrors & Derivate)
└── data/                    ◄── Flüchtige / maschinengenerierte Laufzeit- & Betriebsdaten
    └── mail-desk/           (Quarantäne-Dateien, Quarantäne-Indizes)
```

---

## 2. Übergreifende Kontroll- & Steuerungs-Objekte

### 2.1 Workspace Lock Lease (`workspace-lock`)
Vor jeder mutierenden Dateioperation in einem Ziel-Workspace muss eine exklusive Lock-Lease gehalten werden.

* **Speicherort:** `<workspace-root>/.agents/session.lock`
* **Guard-Modul:** `workspace-lock/scripts/workspace_lock_guard.py` (SSOT im Nachbar-Skill)
* **Felder der Lease:**
  * `conversation_id`: String (aktive Harness-Sitzung)
  * `harness`: String (z. B. `antigravity`, `daedalus`, `claude-code`)
  * `acquired_at`: ISO 8601 Timestamp
  * `expires_at`: ISO 8601 Timestamp
  * `intent`: Kurzbeschreibung der geplanten Mutation
* **Invariant:** Schreibende Skripte prüfen via `require_workspace_lock()`. Ohne Lease bricht die Ausführung sofort mit `WorkspaceLockRequiredError` ab.

### 2.2 Structured CLI Envelope
Alle Python-Skripte des Bundles emittieren maschinenlesbare JSON-Envelopes nach dem Standard des Shared Skills Authoring Guides:

```json
{
  "status": "ok",
  "data": {
    "summary": "Operation erfolgreich abgeschlossen",
    "details": {}
  },
  "error": null,
  "stopcode": null
}
```

Bei Fehlern:
```json
{
  "status": "error",
  "data": null,
  "error": "Fehlermeldung im Klartext",
  "stopcode": "WORKSPACE_LOCK_REQUIRED",
  "details": {
    "reason": "Keine gültige Lease für aktuellen Workspace gefunden",
    "target_workspace": "rel/path/to/ws"
  }
}
```

---

## 3. Zentrale Wissens- & Katalog-Objekte

### 3.1 Projektkatalog (`project-catalog-entry`)
Zentrale Registrierung aller aktiven und archivierten Projekte.

* **Dateipfad:** `memory/references/projects/projects.json`
* **Validierungsskript:** `skills/project-catalog-entry/scripts/validate_projects.py`
* **Migrationsskript:** `skills/project-catalog-entry/scripts/migrate_project_wps.py`
* **Schema-Version:** `Schema v3`
* **Schema-Kern (Projekt-Ebene):**
  * Pflichtfelder: `id` (Lowercase Slug), `title`, `mailbox_folder` (Pfad im Mailbox-Account).
  * Root-Felder: `kuerzel`, `project_website`, `project_reference`, `laufzeit`, `gesamtbudget`, `institution_budget`, `boku_budget`, `reference_md`, `description`, `updated_at`.
* **Workpackages (`workpackages`):**
  * Pflichtfelder: `id` (Slug, z. B. `wp1-management`), `title`, `status` (Enum: `active`, `completed`, `planned`, `paused`).
  * Felder: `number` (positive Zahl), `lead`, `boku_role`, `aliases`, `keywords`, `contacts`, `tasks`, `deliverables`.
  * `tasks`: `id`, `title`, `lead`, `keywords`.
  * `deliverables`: `id`, `title`, `lead`, `type`, `due_month`.
* **Milestones (`milestones`):**
  * Pflichtfelder: `id`, `title`.
  * Felder: `lead`, `due_month`, `related_wps` (müssen existierende Workpackage-IDs referenzieren), `prerequisites`.


### 3.2 Themenkatalog (`topic-catalog-entry`)
Hierarchisches Wissensregister über Themengebiete, Technologien und Domänen.

* **Dateipfad:** `memory/references/topics/topics.json`
* **Schema-Kern:**
  * `topics`: Array von Topic-Objekten:
    * `id`: Eindeutiger Identifier
    * `name`: Fachbegriff
    * `aliases`: Array von alternativen Bezeichnungen / Schreibweisen
    * `subtopics`: Hierarchische Unterthemen
    * `tags`: Semantische Schlagworte zur Klassifikation

---

## 4. Sub-Skill-spezifische Objektwelten (Verweise)

Für die detaillierten Schemas der beiden Code-Schwergewichte existieren spezialisierte L2-Objektkarten:

### 4.1 Mail-Desk Objekte → [`../../skills/mail-desk/docs/system-map/objects.md`](../../skills/mail-desk/docs/system-map/objects.md)
* **`attachment-quarantine-index.json`**: Schema 1 (17 Basisfelder + bis zu 6 optionale additive Coverage-Felder: `analysis_completeness`, `truncation_reason`, `truncation_stage`, `handoff_character_count`, `analysis_character_budget`, `source_character_count`), deterministische 64-Hex-`attachment_id`, Drift-Erkennung (`AttachmentIndexDriftError`).
* **`.quarantine-inventory.json`**: Physisches SHA-256-Abbild der extrahierten Binärdateien auf Disk mit `size_bytes > 0` und Concurrency-Lock (`_QuarantineInventoryLock`).
* **`attachment-disposition-log.jsonl`**: Revisionssicheres, zeilenbasiertes Append-Only-Auditlog für alle Dispositionsentscheidungen, Promotion-Links und Discard-Löschungen.
* **`attachment-discard-journal.json`**: Transaktionales 5-Stufen-Recovery-Journal (`prepared → file_deleted → inventory_updated → index_updated → completed`) mit lückenloser History-Integrität und Resumability.
* **Coverage- & Handoff-Verträge (MD-C1)**: Trennung von technischem Status (`analysis_status: "completed"`) und inhaltlicher Abdeckung (`analysis_completeness`), Hash-Bindung in `compute_handoff_hash()` und `compute_candidate_hash()`.
* **Verifiable Receipts**: `ApprovalReceipt`, `DispositionRequest`, `ApplyRequest` mit kanonischem Request-Hashing und Drift-Prüfung. Die kontextgebundene Receipt-Klassen-Grenze (FR-15/MD-E1-T03) trennt die interne Maschinen-Autorisierung (`receipt_class: "machine"`, `receipt_type: "attachment_auto_evaluation"`, `mail_desk_auto_evaluator`) vom typenlosen Human-Receipt: nur der `evaluation`-Kontext akzeptiert die Maschinenklasse, Human-Approval-Kontexte weisen sie fail-closed ab.
* **Staged `attachment_evaluation` + validierter `attachment_analysis_handoff` (FR-15/MD-E1-T04/T05)**: `attachment_evaluate` gibt `{"attachment_evaluation": {…}, "attachment_analysis_handoff": {…}}` zurück. Das staged Objekt trägt `status ∈ {completed, not_needed, skipped, failed}`, bounded `reason` (T05-Laufzeit: `classification_clear`, `no_attachments`, `no_allowed_attachments`, `handoff_ready`, `still_ambiguous`), `authorization ∈ {auto_evaluated, not_applicable}`, ein bounded `files[]` mit genau `{filename, sha256, mime_type, chars, coverage, run_id}` (`coverage ∈ {full, truncated}`), `used_for_classification` **immer** `false` und `classifier_revision` **immer** `null`. Nur der validierte, gekapselte Handoff darf begrenzten Inhalt tragen; das staged Objekt enthält nie absolute Pfade oder Rohtext. T05 komponiert die bestehenden Seams linear (`op_attachment_fetch` → `extract_attachment_content` → `build_attachment_analysis_handoff` → `validate_attachment_handoff`) unter einer `run_id`; erfolgreich → `completed`/`handoff_ready`, validiert blockiert → `completed`/`still_ambiguous`. Die negative Fehler-/Reason-Matrix bleibt MD-E1-T06; `DraftManifest`-Installation bleibt MD-E2. L2-Detail: [`../../skills/mail-desk/docs/system-map/objects.md`](../../skills/mail-desk/docs/system-map/objects.md) §6.
* **`final-location-index.json`**: Mapping von normalisierter `message_id` auf die Ablageposition.
* **Dossier- & Envelope-Modelle**: Strukturierte Fallakten und Handlungs-Empfehlungen.

### 4.2 Cloud-Atlas Objekte → [`../../skills/cloud-atlas/docs/system-map/objects.md`](../../skills/cloud-atlas/docs/system-map/objects.md)
* **`filemap.json`**: Vollständiges Verzeichnisabbild der Cloud-Speicher inklusive Metadaten und Hashes.
* **`filemap-curation.json`**: Kuratierungs-Overlay zur manuellen Steuerung von Konvertierungsregeln.
* **Markdown-Derivate**: Konvertierte Office-Dokumente und OCR-Ergebnisse.
