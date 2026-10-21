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
│   │   ├── meetings/        (nur meetings.json-Register + unrouted/-Fallback)
│   │   ├── projects/<id>/meetings/   (projektgebundene Protokolle & Transkripte)
│   │   ├── topics/<id>/meetings/     (topic-gebundene Protokolle & Transkripte)
│   │   ├── */events/<slug>/meetings/ (event-gebundene Protokolle & Transkripte)
│   │   ├── mail/            (Verifizierte Mail-Dossiers & Intake-Logs)
│   └── cloud/               ◄── Lokale Spiegel und Konvertierungs-Derivate
│       ├── projects/        (Cloud-Atlas Projekt-Mirrors & Derivate)
│       └── topics/          (Cloud-Atlas Topic-Mirrors & Derivate)
└── data/                    ◄── Flüchtige / maschinengenerierte Laufzeit- & Betriebsdaten
    ├── mail-desk/           (Quarantäne-Dateien, Quarantäne-Indizes)
    └── meeting-desk/        (Ingest-Journal, Staging-Bytes, Ownership-Record)
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
* **Validierungsskript:** `skills/mail-desk/scripts/catalog_validator.py` (FR-21/MD-S5; read-only CLI, validiert zusätzlich `projects.json` und `mail-desk.json`; Root-Patterns = Nicht-Leer-String, nested Patterns ≥ 3 Zeichen)
* **Schema-Kern (Topic-Ebene):**
  * Pflichtfelder: `id` (Lowercase Slug), `title`, `mailbox_folder`.
  * Root-Felder: `reference_md`, `aliases`, `keywords`, `domains`, `contacts`, `typical_subject_patterns`, `subtopics`, `description`, `routing_priority` (Zahl), `do_not_route_if` (String-Array), `updated_at`, `schema_version`.
  * `subtopics`: Array von Subtopic-Objekten mit `id`, `title`, optional `aliases`, `keywords`, `typical_subject_patterns`, `contacts`, `status` (`active` oder statuslos = aktiv).

### 3.3 Desk-Signals-Katalog (`mail-desk.json`)
Reply-Heuristik- und Identitäts-Konfiguration des konsumierenden Workspaces.

* **Dateipfad:** `memory/references/mail-desk/mail-desk.json`
* **Validierungsskript:** `skills/mail-desk/scripts/catalog_validator.py` (read-only CLI, Exit 0/1/2, kanonischer Envelope `catalog_validator`)
* **Read-Inspektor:** `skills/mail-desk/scripts/catalog_inspect.py` (read-only Feld-Inspektion der drei Kataloge als kanonischer Envelope; `--id` respektiert `--fields`; MD-BC)
* **Batch-CLI-Wrapper:** `skills/mail-desk/scripts/mail_desk_batch_cli.py` (`draft`/`execute`/`reconcile`/`sync-sent`; vor lokaler Persistenz/Pruning eigener shared Workspace-Lock ohne Legacy-Bypass; explizite Lease-/Conversation-Flags nur bei `draft` an Runner delegiert; Account-/Review-Gates unverändert. Runner-stdout/-stderr nach `<workspace>/tmp/mail-batch/`, kompakter Summary-Envelope mit Fehlerstatus/Nichtnull-Exit bei Runner-Fehlern, `--keep N`-Prune; MD-BC, Issue #5). `sync-sent` liest die Mailbox, schreibt aber Sent-Index und lokale Ergebnisse.
* **Schema 1 (Legacy) / Schema 2 (FR-22):** striktes `schema_version`-Gate (bool/float/str abgewiesen). Schema 1: `reply_triggers` Pflicht. Schema 2: `reply_triggers` optional, wenn `owner_address` gesetzt ist — der Loader leitet die generische Anrede-Trigger-Klasse aus dem Owner-Local-Part ab (`derive_greeting_triggers`, Wortgrenzen-Predicate unverändert).
* **Optionale Felder (Schema 1+2):** `no_reply_sender_tokens`, `owner_address` (string|null), `sent_sender_domain_whitelist`, `sent_subject_stopwords`, `internal_domains`, `spam_sender_allowlist` (je Liste nicht-leerer Strings mit dokumentierten Defaults; FR-22/MD-ID1-ID4).
* **Fallback-Vertrag:** Fehlende Datei = **leere** Trigger-Liste (keine Anrede-Trigger ohne Katalog); Schema-Drift = fail-loud `ValueError`.

### 3.4 Feature-Request-System (`docs/features/`)

* **Aktiver Backlog:** [GitHub Issues](https://github.com/martinderm/office-intelligence/issues);
  Workstatus und Abnahme im Issue/PR und dessen Run, kein paralleles Root-Ledger.
* **Aktuelle Verträge/Specs:** Fachreferenzen und betroffene L1/L2-System-Maps;
  Änderungen werden dort synchron dokumentiert, nicht in alten Statuszeilen.
* **Legacy-Records:** `docs/features/FR-<n>.md` — unverändertes historisches
  Frontmatter und Paketkarten; offene Altstatus werden nicht als erledigt umgedeutet.
* **Archiv:** `docs/features/_archive.md` bleibt historisch erhalten;
  `FEATURE-REQUEST-ARCHIVE.md`, `FEATURE-REQUESTS.md` und
  `FEATURE-REQUEST-PROGRESS.md` sind nur Linkstubs.
* **Snapshot/Provenienz:** [`docs/archive/feature-ledgers/README.md`](../archive/feature-ledgers/README.md),
  bytegetreue Textkopien der Root-Ledger, keine neue Fortschrittstabelle.
* **Routing:** [`docs/features/CONTEXT.md`](../features/CONTEXT.md).
  Alte `catalog`/`progress`- und externe `spec_reference`-Verweise bleiben über
  Stubs historisch lesbar; sie erteilen keine Kernel-Bindung oder neue Ledgerpflicht.

### 3.5 Task-Desk-Register (Issue #16, Zielverträge)

Doc-only Zielverträge der beiden `task-desk`-Register im konsumierenden Workspace
(`memory/evidence/todos/`); vollständig in
[`../../skills/task-desk/references/task-schemas.md`](../../skills/task-desk/references/task-schemas.md).
**Nicht** laufzeitunterstützt und **nicht** mit dem beobachteten Legacy-Bestand
kompatibel; Aktivierung/Migration separat autorisiert.

* **`review-queue.json` (Zielvertrag Version 2):** Root exakt `version: 2`,
  `updated_at` (UTC RFC3339 mit `Z`), `entries`; Eintrag mit `action_id` (UUID4),
  `source` (`{type, id}`), `action_ref` (`{action_id, source}`), `admitted_at`,
  `status` (`pending_triage`, `awaiting_dispatch`, `dispatch_unknown`, `dispatched`,
  `review_required`, `resolved`), nichtleerem `title` und geprüftem
  `evidence_ref={file,anchor}`. Kleines Audit-/Candidate-Register, kein
  Todoist-Spiegel; `action_id` wird bei der ersten Aufnahme vergeben. Optionale
  `context={kind,id}` nur aus project/topic-Handoff; `routing`/`due_date` optional
  (`due_date: null` = bewusst keine Frist). Bestätigter Erfolg führt **zuerst** zu
  `dispatched` (mit `external_ref` + identischem `created`-Eintrag), **danach**
  optional `resolved(outcome: created)`; `resolved` ohne Creation trägt `outcome`
  (`dismissed`/`no_action`) + `reason`. Ein resumed `awaiting_dispatch` ohne
  Nichtversandnachweis geht zuerst `dispatch_unknown`/Review (kein blindes Retry).
* **`created-tasks.json` (Zielvertrag Version 2):** Root wie oben; Eintrag mit
  derselben `action_id`/`source`/`action_ref`, `created_at`, `title`, `evidence_ref`
  (gleicher Wert) und `external_ref` (`{adapter, task_id}`, optional
  `project_id`/`section_id`) nach bestätigtem Adaptererfolg; 1:1 zu einem
  `dispatched`- oder `resolved(outcome created)`-Queue-Eintrag; nur letzter
  bestätigter Snapshot, **kein** Livezustand.
* **Identität/`action_ref`:** Wiedererkennung ausschließlich über vorhandenen
  `action_ref` mit exakt passender `source`; `action_id` ist opak/zufällig (UUID4),
  **nicht** aus Text/Message-ID/Fingerprint abgeleitet. `fingerprint`→`action_id`
  ist **kein** Alias.
* **Legacy `version: 1` (beobachtet):** read-only `legacy_read_only`/
  `review_required`; bestehende Bytes/IDs/Referenzen bleiben erhalten; keine
  automatische Normalisierung oder Migration.
* **`replies-needed.jsonl` (Actual Writer `modes/execute.py`):** Eintrag mit
  `timestamp`, `envelope_id`, `message_id`, `subject`, `from`, `folder`,
  `reply_status`, `reply_note`; `reply_status` genau `needed` nur mit passender
  normalisierter Quelle im freigegebenen `synthesis_handoff` (sonst Review, kein
  Dispatch).

---

## 4. Sub-Skill-spezifische Objektwelten (Verweise)

### Source-bound Event-Writer (Issue #14)

`skills/event-documentation/scripts/event_documentation.py:apply_event` nimmt
Schema 1 mit genau `work_order/content` neben der Schemaversion entgegen.
Zwei strikt getrennte Source-Profile: Basis ohne Attachments/Locator-Behauptung;
Rich mit gebundenen Locations, Thread-/Kalenderdaten, MIME-Provenienz und
kanonischem Scope/Completeness. Work-Order- und Snapshot-Hashes binden die
unveränderten Eingaben; `output_receipt` bindet alle fünf Output-Datei-Bytes.
Projekt-Owner und Topic→Subtopic→Event werden kataloggebunden aufgelöst.
Topic-Referenz enthält Subtopic, Topic-Evidence ausdrücklich nicht; gleiche
Event-IDs in Geschwister-Subtopics stoppen wegen Pfadkollision.
Index: `evidence_level: unreviewed-draft`, `review_state: unreviewed`,
`status: draft`; kein Content-Approval. Operative Entscheidungen bleiben als
ungeprüfte Meldungen in `action-items.md`, ergänzt durch `source-log.json`,
`attachment-inventory.json`, `compliance-report.json`. Vollständiger Vertrag:
[`event-writer.md`](../../skills/event-documentation/references/event-writer.md).

Für die detaillierten Schemas der beiden Code-Schwergewichte existieren spezialisierte L2-Objektkarten:

### Meeting-Desk-Objekte (Issue #35)

Vollständig in [`../../skills/meeting-desk/references/data-model.md`](../../skills/meeting-desk/references/data-model.md)
und [`../../skills/meeting-desk/docs/system-map/README.md`](../../skills/meeting-desk/docs/system-map/README.md) §2/§3.

* **`meeting-record/1` (Transport):** exakt `schema`, `source`, `meeting_id`,
  `title`, `start`, `duration_minutes`, `participants`, `artifacts`,
  `classification_hint`, `review`, `source_metadata`; Erweiterungen nur inert in
  `source_metadata`. Hints sind untrusted und routen nie. Bekannte Zeitpunkte
  dürfen eine beobachtete Basis tragen; nur Schätzungen brauchen eine Basis.
* **`meetings.json` (Objektregister):** Root-Objekt mit `meetings`-Liste; Root-
  Extras und nicht betroffene/legacy Einträge bleiben erhalten. Neue Einträge
  tragen 25 kanonische Felder, `record_schema: "meeting-record/1"` und
  `content_status: "provisional"`. Nackte Legacy-Arrays werden abgewiesen, nie
  konvertiert; Identität ausschließlich über `meeting_id`/`id`/Quellpaar.
  `meeting_id`/`external_id` sind opake, exakt erhaltene Unicode-IDs (≤512,
  keine Steuerzeichen/Surrogate); `/`, `\`, `+`, `=`, Leerzeichen und `..` sind
  darin gültig, Dateinamen nutzen nur einen Hash. Treffer behalten ihre
  ursprüngliche stabile ID und Pfade; unsourced Legacy wird
  nur über einen belegten Quell-Präfix adoptiert, sonst abgewiesen.
* **Meeting-Artefakte:** `*.summary.md` und `*.transcript.md` mit generierter
  Frontmatter (`document_type`, `evidence_level: observed`,
  `status: provisional`), byte-exaktem Body; `*.transcript.raw.md` byte-exakt
  ohne Frontmatter und unveränderlich. `null` im Record bedeutet nicht
  geliefert, nie Löschen; bestehende registrierte Pfade/Bytes bleiben bei
  partiellen Updates erhalten, inklusive Raw und menschlich abgeleiteter
  Clean-Fassung. Kein Record-Deletionsfeld.
* **Ownership (`data/meeting-desk/ownership.json`, Schema
  `meeting-desk-ownership/1`):** Pfad → Meeting-ID, SHA-256, Größe, Art,
  Writer-Version, Zeit. Generierte Updates nur gegen diesen Hash; nicht
  Writer-eigene Dateien werden nicht überschrieben.
* **Ingest-Journal (`data/meeting-desk/journal/`, Schema
  `meeting-desk-journal/1`):** Integritätsdigest über den Body, per Step
  Vor-/Nachzustand-Hashes, Stage-Payload-Hashes, Register-/Ownership-Prestate und
  Workspace-Fingerprint. Offen = jede Mutation blockiert (`recovery_required`,
  Exit 3); `--inspect` read-only, `--recover` explizit, lease-gebunden,
  forward-only, fremde Bytes stoppen. Keine Multi-File-ACID-/Power-Loss-Garantie.

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
* **Draft-Diagnose-Objekte (Issue #19, opt-in)**: `DiagnosticRequest`/
  `DiagnosticReviewReceipt` (Schema 1, geschlossene Schlüsselmengen) plus
  Einmal-Marker und additive Evidenz unter `data/mail-desk/diagnostics/`; keine
  Mailfelder/Zugangsdaten/Pfade. Owner
  [`../../skills/mail-desk/scripts/core/diagnostics.py`](../../skills/mail-desk/scripts/core/diagnostics.py).
  L2-Detail: [`../../skills/mail-desk/docs/system-map/objects.md`](../../skills/mail-desk/docs/system-map/objects.md) §5.6.

### 4.2 Cloud-Atlas Objekte → [`../../skills/cloud-atlas/docs/system-map/objects.md`](../../skills/cloud-atlas/docs/system-map/objects.md)
* **`filemap.json`**: Vollständiges Verzeichnisabbild der Cloud-Speicher inklusive Metadaten und Hashes.
* **`filemap-curation.json`**: Kuratierungs-Overlay zur manuellen Steuerung von Konvertierungsregeln.
* **Markdown-Derivate**: Konvertierte Office-Dokumente und OCR-Ergebnisse.
* **`files[<quelle>].form_fields`** (Issue #20): additiver, begrenzter Statusvertrag der optionalen AcroForm-Formularfelder-Extraktion (`status` ∈ `complete`/`no_fields`/`partial`/`dependency_missing`/`error`, `count`, `tool`/`tool_version`, bounded `warnings`, `source_sha256`, `extracted_before_ocr`). Reserviertes **generated Feld** (nicht per Curation-Overlay setzbar/überschreibbar) und nur bei unverändertem Quell-Hash weitergereicht. Er bleibt **außerhalb** des kanonischen Mirror-Frontmatters.
