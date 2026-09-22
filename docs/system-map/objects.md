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
  * Root-Felder: `kuerzel`, `project_website`, `project_reference`, `laufzeit`, `gesamtbudget`, `institution_budget`, `boku_budget`, `reference_md`, `aliases`, `keywords`, `domains`, `contacts`, `typical_subject_patterns`, `routing_priority` (Zahl), `do_not_route_if` (String-Array), `description`, `updated_at`.
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
* **Validierungsskript:** *(kein eigenes Skript; `topics.json` hat keinen Validator)*
* **Schema-Kern (Topic-Ebene):**
  * Pflichtfelder: `id` (Lowercase Slug), `title`, `mailbox_folder`.
  * Root-Felder: `reference_md`, `aliases`, `keywords`, `domains`, `contacts`, `typical_subject_patterns`, `subtopics`, `description`, `routing_priority` (Zahl), `do_not_route_if` (String-Array), `updated_at`, `schema_version`.
  * `subtopics`: Array von Subtopic-Objekten mit `id`, `title`, optional `aliases`, `keywords`, `typical_subject_patterns`, `contacts`, `status` (`active` oder statuslos = aktiv).

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
* **Staged `attachment_evaluation` + validierter `attachment_analysis_handoff` (FR-15/MD-E1-T04/T05/T06)**: `attachment_evaluate` gibt im Erfolgsfall `{"attachment_evaluation": {…}, "attachment_analysis_handoff": {…}}` zurück; ein `failed`-Envelope enthält nur `attachment_evaluation`. Das staged Objekt trägt `status ∈ {completed, not_needed, skipped, failed}`, bounded `reason` (Laufzeit: `classification_clear`, `no_attachments`, `no_allowed_attachments`, `handoff_ready`, `still_ambiguous`, `lock_unavailable`, `policy_blocked`, `quota_exceeded`, `fetch_failed`, `extraction_failed`, `handoff_invalid`; `evaluation_pending` ist nur eine historische Konstante), `authorization ∈ {auto_evaluated, not_applicable}`, ein bounded `files[]` mit genau `{filename, sha256, mime_type, chars, coverage, run_id}` (`coverage ∈ {full, truncated}`), `used_for_classification` **immer** `false` und `classifier_revision` **immer** `null`. Nur der validierte, gekapselte Handoff darf begrenzten Inhalt tragen; das staged Objekt enthält nie absolute Pfade oder Rohtext. T05 komponiert die bestehenden Seams linear (`op_attachment_fetch` → `extract_attachment_content` → `build_attachment_analysis_handoff` → `validate_attachment_handoff`) unter einer `run_id`; erfolgreich → `completed`/`handoff_ready`, validiert blockiert → `completed`/`still_ambiguous`. T06 schließt die negative Matrix fail-closed (bounded `failed`-Envelopes mit `authorization: "not_applicable"`, `files: []` und ohne Handoff-Geschwister; kanonisch gültige, unvollständige erforderliche Evidenz bleibt `required_for_decision`); `DraftManifest`-Installation erfolgt in MD-E2-T01. L2-Detail: [`../../skills/mail-desk/docs/system-map/objects.md`](../../skills/mail-desk/docs/system-map/objects.md) §6.
* **Finales `attachment_evaluation` im DraftManifest und opt-in `inspect`-Vorschlag (FR-15/MD-E2-T01/T02/T03/T04)**: `install_draft_attachment_evaluations(...)` ([`../../skills/mail-desk/scripts/core/attachment_reclassification.py`](../../skills/mail-desk/scripts/core/attachment_reclassification.py)) gibt jedem Draft-Item genau ein additives Feld in der gebundenen MD-E1-Form. Klares Item → `not_needed/classification_clear`, deaktiviert → `skipped/evaluation_disabled`, fortbestehend mehrdeutig → `completed/still_ambiguous`; nur eine erfolgreiche, eindeutige Neuklassifikation setzt `completed/classification_clear` mit `used_for_classification: true` und 64-Hex-`classifier_revision` (Regel-Fingerprint plus sortierte konsumierte Hashes). Alle anderen Ausgänge bleiben `false`/`null`. **T02** härtet: bounded MD-E1-Fehler/No-Ops bleiben item-lokal in Review/`INBOX`, Identitäts-/Quellen-Pairing-Fehler sind Bindungsfehler (`handoff_invalid`), der `ready`-Handoff wird kanonisch revalidiert, `still_ambiguous` erhält `auto_evaluated` plus sichere `files[]`, der Regel-Fingerprint bindet zusätzlich die normalisierten ASTs der geordneten aktiven Regelmodule (`classifier.py`, `matching/ambiguity.py`, `matching/date_parser.py`, `matching/project_matching.py`, `matching/topic_matching.py`), unerwartete Vertragsfehler schlagen über `AttachmentReclassificationContractError` fail-loud fehl, und ein deterministischer PII-freier Run-ID je Nachricht erreicht MD-E1 `already_fetched` ohne Doppel-Fetch. **T03** ([`../../skills/mail-desk/scripts/core/modes/inspect.py`](../../skills/mail-desk/scripts/core/modes/inspect.py)) verdrahtet den opt-in `inspect`-Vorschlag: ohne Opt-in rein lesend, mit `evaluate_attachments: true` ein top-level, nicht ausführbares `manifest_proposal` über denselben Flow (initiale Klassifikation zuerst, `source_sink`), `propose_manifest: true` ohne Auswertung installiert `skipped/evaluation_disabled`, und eine ausführbare Batch-Manifest-Datei entsteht nur bei explizitem `manifest_file`. **T04** ([`../../skills/mail-desk/tests/test_batch_runner_mde2_acceptance.py`](../../skills/mail-desk/tests/test_batch_runner_mde2_acceptance.py)) nimmt das Paket ab: ein einziger hermetischer realer Pfad (`run_draft_mode` mit echtem `draft_manifest`, echter Classifier-Regelbasis und echtem `attachment_evaluate`) endet im persistierten Projekt-`DraftManifest` mit `used_for_classification: true` und 64-Hex-Revision ohne Leck von Roh-/`prompt_content`-/Pfad-/Capability-/Receipt-Inhalten; **FR-15 ist geschlossen**. L2-Detail: [`../../skills/mail-desk/docs/system-map/objects.md`](../../skills/mail-desk/docs/system-map/objects.md) §6.
* **`final-location-index.json`**: Mapping von normalisierter `message_id` auf die Ablageposition.
* **Dossier- & Envelope-Modelle**: Strukturierte Fallakten und Handlungs-Empfehlungen.

### 4.2 Cloud-Atlas Objekte → [`../../skills/cloud-atlas/docs/system-map/objects.md`](../../skills/cloud-atlas/docs/system-map/objects.md)
* **`filemap.json`**: Vollständiges Verzeichnisabbild der Cloud-Speicher inklusive Metadaten und Hashes.
* **`filemap-curation.json`**: Kuratierungs-Overlay zur manuellen Steuerung von Konvertierungsregeln.
* **Markdown-Derivate**: Konvertierte Office-Dokumente und OCR-Ergebnisse.
