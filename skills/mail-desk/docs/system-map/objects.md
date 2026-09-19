# Mail-Desk — Subsystem System Map: Nomen (Objects)

> **Typ**: ICM Form 6 (`system-map`), Dimension: Nomen  
> **Subsystem**: [`skills/mail-desk`](../SKILL.md)  
> **Ziel**: Vollständige Dokumentation aller Datenstrukturen, Indizes, Schemas und DTOs des Mail-Desks mit exakten Felddefinitionen und Quellcode-Zitaten.  
> **Gültig für**: `skills/mail-desk/` und `data/mail-desk/` im Ziel-Workspace

---

## 1. Versionierter Quarantäneindex (`attachment-quarantine-index.json`)

Der Quarantäneindex ist die zentrale Buchführung über alle isolierten Dateianhänge.

* **Dateipfad:** `data/mail-desk/attachment-quarantine-index.json`
* **Implementierungsdatei:** [`scripts/core/attachment_quarantine_index.py`](../scripts/core/attachment_quarantine_index.py)
* **CLI-Fassade:** [`scripts/mail_desk_attachment_quarantine_index.py`](../scripts/mail_desk_attachment_quarantine_index.py)
* **Aktuelle Schema-Version:** `1`

### 1.1 Root-Struktur
Das Wurzelelement erlaubt ausschließlich drei Felder (`ALLOWED_ROOT_FIELDS`):
```json
{
  "schema_version": 1,
  "updated_at": "2026-09-17T10:00:00Z",
  "items": {
    "<attachment_id>": { "..." : "..." }
  }
}
```

### 1.2 Die kanonischen Eintragsfelder
Schema 1 normalisiert 17 kanonische Basisfelder; `contract_hash` bleibt optional. Zusätzlich können 6 Coverage-Felder nur als konsistenter Gesamtblock vorhanden sein. `CANONICAL_ENTRY_FIELDS` bindet diese insgesamt 23 Felder bei der Idempotenzprüfung:

#### 1.2.1 Basisfelder (Schema 1)
| Feldname | Typ / Regex | Beschreibung |
| :--- | :--- | :--- |
| `attachment_id` | `^[0-9a-fA-F]{64}$` | Deterministischer 64-Hex SHA-256 Hash (siehe unten). |
| `message_id` | String | Normalisierte Message-ID (RFC 822). |
| `account` | String | Mailbox-Account-Bezeichnung. |
| `folder` | String | Ursprünglicher Mailbox-Ordner (z. B. `INBOX`). |
| `part_locator` | `^\d+(?:\.\d+)*$` | MIME-Part Locator (z. B. `1`, `2.1`). |
| `clean_filename` | String | Bereinigter Dateiname ohne Sonderzeichen/Steuerzeichen. |
| `mime_type` | `^[a-zA-Z0-9!#$&^_.+-]+/[a-zA-Z0-9!#$&^_.+-]+$` | Normalisierter MIME-Typ. |
| `sha256` | `^[0-9a-fA-F]{64}$` | Physischer SHA-256 Hash der Binärdatei auf Disk. |
| `size_bytes` | Integer (`> 0`) | Exakte Dateigröße in Bytes (strikt positiv). |
| `run_id` | String | Eindeutige Kennung des Extraktionslaufs. |
| `quarantine_path` | String | Workspace-relativer Pfad: `data/mail-desk/attachments/<run_id>/<file>`. |
| `analysis_status` | Enum | Fest auf `"completed"` (`ALLOWED_ANALYSIS_STATUSES`). Bezeichnet rein den technischen Durchlauf! |
| `analyzed_at` | RFC 3339 String | Zeitstempel der Extraktion und Analyse. |
| `contract_version` | String | Version des Extraktionsvertrags (z. B. `"1.0"`). |
| `contract_hash` | String / Hex | Hash des bindenden Prüfvertrags (optionaler 64-Hex-SHA-256). |
| `lifecycle_state` | Enum | Fest auf `"quarantined"` (`ALLOWED_LIFECYCLE_STATES`). |
| `disposition_ref` | String / `null` | Max. 128 Zeichen (`DISPOSITION_REF_REGEX`) oder `null`. |

#### 1.2.2 Additive Coverage-Felder (Schema 1 optional, nur als vollständiger konsistenter Block)
| Feldname | Typ / Wertebereich | Beschreibung |
| :--- | :--- | :--- |
| `analysis_completeness` | Enum: `full`, `truncated`, `partial`, `unavailable` | Fachliche Abdeckung der Anhangsanalyse (in-memory `unknown` bei unannotierten Altdaten). |
| `truncation_reason` | String / `null` (`ALLOWED_TRUNCATION_REASONS`) | Grund bei Kürzung (z. B. `max_chars_exceeded`, `ocr_page_limit_exceeded`, `timeout_exceeded`). |
| `truncation_stage` | Enum: `none`, `extraction`, `handoff_per_attachment`, `handoff_cumulative_mail` | Verarbeitungsstufe, auf der die Kürzung eintrat. |
| `handoff_character_count` | Integer (`>= 0`) | Tatsächlich im Handoff bereitgestellte Textlänge. |
| `analysis_character_budget` | Integer (`>= 0`) | Wirksames Zeichenbudget für diesen Anhang. |
| `source_character_count` | Integer (`>= 0`) / `null` | Vor der Kürzung ermittelte Gesamtzeichenzahl (oder `null`, wenn vor Zählung geboundet). |

### 1.3 Schema-1-Integrität & In-Memory-Darstellung
* **Single-Schema-Design:** Der Quarantäneindex verbleibt strikt auf `SCHEMA_VERSION = 1`. Es gibt kein Schema 2 und keine Migrationsbefehle.
* **In-Memory-Legacy-Darstellung:** Beim Lesen von Schema-1-Einträgen ohne Coverage-Felder liefert `lookup_quarantine_entry()` in-memory standardmäßig `analysis_completeness: "unknown"`. Die Datei auf Disk bleibt vollständig unberührt und byte-identisch.
* **Fail-Closed-Invariante:** Ein Status `analysis_completeness: "full"` darf niemals mit einem gesetzten `truncation_reason` oder `truncation_stage != "none"` kombiniert werden (`AttachmentIndexSchemaError`).

### 1.4 Ableitung der deterministischen `attachment_id`
Definiert in [`scripts/core/attachment_quarantine_index.py`](../scripts/core/attachment_quarantine_index.py):
```python
canonical_dict = {
    "inventory_sha256": norm_sha,
    "message_id": norm_mid,
    "part_locator": norm_loc,
}
canonical_json = json.dumps(canonical_dict, sort_keys=True, separators=(",", ":"))
attachment_id = hashlib.sha256(canonical_json.encode("utf-8")).hexdigest()
```

### 1.4 Verbotene Inhaltsfelder (`FORBIDDEN_ENTRY_FIELDS`)
Die Anwesenheit folgender Felder (auch verschachtelt) führt über `_find_forbidden_content_keys()` zum sofortigen `ForbiddenContentError`:
`text`, `extracted_text`, `content`, `body`, `prompt`, `llm_prompt`, `response`, `model_response`, `credentials`, `password`, `token`, `tokens`, `api_key`, `envelope_id`, `himalaya_id`.

---

## 2. Versioniertes Append-Only Dispositionslog (`attachment-disposition-log.jsonl`)

Das Dispositionslog ist der unveränderliche, append-only Prüfpfad aller getroffenen Dispositionen (`retain`, `discard`, `promote`).

* **Dateipfad:** `data/mail-desk/attachment-disposition-log.jsonl` (JSON-Lines)
* **Implementierungsdatei:** [`scripts/core/attachment_disposition_log.py`](../scripts/core/attachment_disposition_log.py)
* **CLI-Fassade:** [`scripts/mail_desk_attachment_disposition.py`](../scripts/mail_desk_attachment_disposition.py)
* **Aktuelle Schema-Version:** `1`

### 2.1 Eintragsfelder Schema 1
Jede Zeile im JSONL-Format repräsentiert eine Disposition und erzwingt:

| Feldname | Typ / Format | Pflicht | Beschreibung |
| :--- | :--- | :--- | :--- |
| `schema_version` | Integer (`1`) | Ja | Schema-Version des Eintrags. |
| `decision_id` | `^[0-9a-fA-F]{64}$` | Ja | Deterministischer SHA-256 Hash aus `attachment_id`, `index_entry_sha256`, `decision`, `human_receipt_hash`, `timestamp`. |
| `attachment_id` | `^[0-9a-fA-F]{64}$` | Ja | Referenzierte Attachment-ID aus dem Quarantäneindex. |
| `index_entry_sha256` | `^[0-9a-fA-F]{64}$` | Ja | Kanonischer Hash des Quarantäneindex-Eintrags zum Entscheidungszeitpunkt (`canonical_index_entry_sha256`). |
| `decision` | Enum | Ja | `"retain"`, `"discard"`, `"promote"` (`ALLOWED_DECISIONS`). |
| `timestamp` | RFC 3339 String | Ja | Zeitpunkt der Entscheidungserfassung. |
| `human_receipt_hash` | `^[0-9a-fA-F]{64}$` | Ja | SHA-256 Nachweis der menschlichen Freigabe / des Receipts. |
| `rationale` | String (max 500 Zeichen) | Nein | Optionale fachliche Begründung (ohne verbotene Inhalte). |
| `review_after` | RFC 3339 String | Nein | Nur bei `retain`: Wiedervorlagezeitpunkt. |
| `candidate_review_hash` | `^[0-9a-fA-F]{64}$` | Nein | Bei `promote`: Bindung an den FR-08/FR-09 Filing-Candidate. |
| `promotion_id` | String (max 128 Zeichen)| Nein | Nach FR-09-Vollzug: Eindeutige Kennung der Ablage. |
| `promotion_status` | String (max 64 Zeichen) | Nein | Nach FR-09-Vollzug: Status der Cloud-Promotion. |

---

## 3. Versioniertes Discard-Recovery-Journal (`attachment-discard-journal.json`)

Das Discard-Recovery-Journal sichert den zweiphasigen Bereinigungsprozess (`apply_discard`) transaktions- und wiederanlaufsicher ab.

* **Dateipfad:** `data/mail-desk/attachment-discard-journal.json`
* **Implementierungsdatei:** [`scripts/core/attachment_disposition_log.py`](../scripts/core/attachment_disposition_log.py)
* **Aktuelle Schema-Version:** `1`

### 3.1 Root-Struktur
Das Wurzelelement erzwingt ausschließlich drei Root-Schlüssel (`ALLOWED_JOURNAL_ROOT_KEYS`):
```json
{
  "schema_version": 1,
  "updated_at": "2026-09-17T09:00:00Z",
  "entries": {
    "<journal_entry_id>": { "..." : "..." }
  }
}
```

### 3.2 Deterministische `journal_entry_id`
Der Dictionary-Key und das Feld `journal_entry_id` werden deterministisch berechnet:
```python
journal_entry_id = hashlib.sha256(
    f"{norm_att_id}:{norm_dec_id}:{norm_rcpt_hash}:{norm_prev_hash}".encode("utf-8")
).hexdigest()
```

### 3.3 Eintragsfelder (`DISCARD_JOURNAL_ENTRY_ALLOWED_KEYS`)
Jeder Eintrag unter `entries` erzwingt exakt folgende 17 Felder:

| Feldname | Typ / Format | Beschreibung |
| :--- | :--- | :--- |
| `journal_entry_id` | `^[0-9a-fA-F]{64}$` | Deterministischer SHA-256 Bindungshash. |
| `attachment_id` | `^[0-9a-fA-F]{64}$` | Referenzierte Quarantäne-Attachment-ID. |
| `decision_id` | `^[0-9a-fA-F]{64}$` | Gebundene Dispositionsentscheidung. |
| `apply_receipt_hash` | `^[0-9a-fA-F]{64}$` | SHA-256 Hash des autorisierenden Apply-Receipts. |
| `apply_request_hash` | `^[0-9a-fA-F]{64}$` | Re-berechneter Hash des kanonischen Apply-Requests über alle Scope-Felder. |
| `previous_index_entry_sha256` | `^[0-9a-fA-F]{64}$` | Kanonischer Hash des Quarantäneindex-Eintrags vor Löschung. |
| `quarantine_path` | String | Workspace-relativer Quarantänepfad unter `data/mail-desk/attachments/<run_id>/`. |
| `sha256` | `^[0-9a-fA-F]{64}$` | Physischer SHA-256 Hash der gelöschten Datei. |
| `size_bytes` | Integer (`> 0`) | Physische Dateigröße (strikt positiv). |
| `run_id` | String | Eindeutige Kennung des Quarantänelaufs. |
| `state` | Enum | Aktueller Zustand (`prepared`, `file_deleted`, `inventory_updated`, `index_updated`, `completed`). |
| `last_successful_state` | Enum | Letzter nachweislich erfolgreicher Schritt (bleibt auch bei Fehlern stabil für Recovery). |
| `status` | Enum | `"in_progress"`, `"completed"`, `"failed"`. |
| `created_at` | RFC 3339 String | Erstellungszeitpunkt des Eintrags. |
| `updated_at` | RFC 3339 String | Letzter Aktualisierungszeitpunkt. |
| `history` | Array von Dicts | Lückenlose, monotone History aller Zustandsübergänge und Fehler. |
| `failure_stage` | String / `null` | Bei `status == "failed"`: Name der fehlgeschlagenen Phase (z. B. `"inventory_update"`). |
| `error` | Dict / `null` | Bei `status == "failed"`: Strukturiertes Fehler-Dictionary (ohne verbotene Inhalte). |

### 3.4 History-Integrität & Konsistenzregeln
* **Erlaubte History-Felder:** Ausschließlich `state`, `status`, `timestamp`, `transition`, `stage`, `error` (`DISCARD_JOURNAL_HISTORY_ALLOWED_KEYS`).
* **Startzustand:** Der erste Eintrag muss zwingend `state == "prepared"` (`rank == 1`) sein; verkürzte Historien werden fail-closed abgewiesen.
* **Monotone Schrittfolge:** Jeder erfolgreiche Folgeschritt muss exakt `rank == last_rank + 1` entsprechen (kein Überspringen von Zwischenstufen, keine Rückwärtssprünge).
* **Kopplung von Status und History:**
  * `status == "completed"`: Verlangt `state == "completed"`, `last_successful_state == "completed"`, `failure_stage == null`, `error == null` und `history[-1]` muss ein Erfolgs-Eintrag sein.
  * `status == "in_progress"`: Verlangt Zwischenzustand (`state == last_successful_state != "completed"`), keine Fehlerfelder und `history[-1]` darf kein unbehandelter Failure-Eintrag sein.
  * `status == "failed"`: Verlangt nicht-leere `failure_stage`, strukturiertes `error`-Dict, verbietet `state == "completed"`, und `history[-1]` muss zwingend ein Failure-Eintrag sein, dessen `stage` und kanonischer Fehler exakt mit den Top-Level-Feldern übereinstimmen.

---

## 4. Quarantäne-Inventar (`.quarantine-inventory.json`)

* **Speicherort:** `data/mail-desk/attachments/<run_id>/.quarantine-inventory.json`
* **Implementierungsdatei:** [`scripts/core/attachment_fetch.py`](../scripts/core/attachment_fetch.py) und [`attachment_disposition_log.py`](../scripts/core/attachment_disposition_log.py)
* **Zweck:** Dient als physischer Bindungsnachweis zwischen extrahierter Datei auf Disk und Index. Bevor ein Eintrag in den Quarantäneindex geschrieben wird, prüft `verify_quarantine_attachment_artifact()` physisch, ob:
  1. Die Datei auf Disk existiert.
  2. Sie weder Symlink noch Windows-Reparse-Point ist (`os.lstat().st_file_attributes & 0x400`).
  3. Ihr physischer SHA-256 Hash exakt mit dem Inventar übereinstimmt.
* **Atomare Mutation bei Löschung:** Während `apply_discard` wird das Inventar unter exklusiver Verzeichnis-Sperre (`_QuarantineInventoryLock`) über ein Sibling-Tempfile atomar aktualisiert (`update_quarantine_inventory_atomic`). Schlägt die Inventar-Mutation fehl, bleibt der Quarantäne-Index unberührt für die spätere Recovery.
* **Produktions-Preflight gegen getrackte Quarantäne (FR-15/MD-E1-T02):** Vor dem ersten Quarantäne-Write (`op_attachment_fetch`) und vor dem ersten mutierenden OCR-Derivat-Write (`extract_attachment_content`/`_extract_content_internal`) prüft [`scripts/core/quarantine_preflight.py`](../scripts/core/quarantine_preflight.py) den Git-Index des vertrauenswürdigen `workspace_root`: jede getrackte Datei unter `data/mail-desk/attachments/` sowie jede getrackte `**/.quarantine-inventory.json`/`**/.quarantine-inventory.lock` ist ein begrenzter fail-closed Stop (`TrackedQuarantineError`). Die Git-Abfrage (`git ls-files`, ohne Shell, mit Timeout) ist strikt read-only; Non-Zero-Exit, Timeout oder unlesbarer Output stoppen fail-closed (`QuarantinePreflightError`). `.gitignore` wird dabei nie autonom verändert. Die Detection-Semantik ist mit dem MD-Q1-Helper (`find_tracked_quarantine_files`/`assert_no_tracked_quarantine_files`) single-sourced.

---

## 5. Begleitende Indizes & Control-Plane-Objekte

### 4.1 Final Location Index (`final-location-index.json`)
* **Dateipfad:** `data/mail-desk/final-location-index.json`
* **Implementierungsdatei:** [`scripts/core/index.py`](../scripts/core/index.py#L12-L28)
* **Schema-Version:** `1`
* **Zweck:** Verhindert Doppelverarbeitung. Mappt normalisierte `message_id` auf Ablageort, Verarbeitungsstatus und Timestamp.
* **Erlaubte Eintragsfelder (`ALLOWED_FIELDS`):**
  `message_id`, `mailbox`, `backend`, `final_folder`, `final_label`, `envelope_id`, `gmail_message_id`, `gmail_thread_id`, `in_reply_to`, `references`, `subject`, `from`, `date`, `updated_at`.

### 5.2 Sent Items Index (`sent-index.jsonl`)
* **Dateipfad:** `data/mail-desk/sent-index.jsonl` (JSON-Lines-Format)
* **Implementierungsdatei:** [`scripts/core/sent_indexer.py`](../scripts/core/sent_indexer.py#L25-L43)
* **Zweck:** Hält gesendete E-Mails nach, um Antwortzustände (`check_if_replied()`) für eingehende Mails präzise zu ermitteln.
* **In-Memory-Lookup-Maps:**
  * `by_message_id`: Direkte Normalisierte Message-ID-Zuordnung
  * `by_in_reply_to`: Zuordnung über Header `In-Reply-To`
  * `by_reference`: Zuordnung über `References`-Ketten
  * `by_subject_clean`: Zuordnung über normalisierten Betreff (`clean_subject()`, befreit von Re/Aw/Wg/Fwd)

### 5.3 Backend-Konfiguration (`mail-desk-backend.json`)
* **Dateipfad:** `.agents/mail-desk-backend.json` (im Ziel-Workspace)
* **Zweck:** Credentials-freie Deklaration des Mailbox-Backends (`"himalaya"` / `"gmail"`), des Standard-Accounts und Quellordners.

---

## 6. DTOs, Verträge und In-Memory-Strukturen

* **`DraftManifest` & Review Contract** ([`scripts/core/batch_contract.py`](../scripts/core/batch_contract.py)):
  * Bindende Parameter: `expected_count`, `allow_fewer`, `candidate_count`, `source_folder`, `account`, `skip_known`, `review`.
  * `canonical_execute_request_sha256()`: Berechnet Hash über das Request-Objekt unter bewusstem Ausschluss des Feldes `review`.
* **Verifizierbare Receipt-Contracts & Scope-Requests (MD-Q3)** ([`scripts/core/attachment_disposition_log.py`](../scripts/core/attachment_disposition_log.py)):
  * `ApprovalReceipt`: Pflichtfelder `receipt_id`, `request_hash`, `approved_at`, `approved_by`. Unbekannte Felder, verbotene Inhalte oder abweichende Hashes werden fail-closed abgewiesen (`ReceiptMalformedError`, `ReceiptDriftError`).
  * **Receipt-Klassen-Grenze (FR-15/MD-E1-T03, implementiert):** Die bestehenden Receipts bleiben typenlos (kein `receipt_type`) und unverändert gültig; eine pauschale Schema-Migration findet nicht statt. Die schmale, kontext-/receipt-klassenbewusste Grenze ist in [`scripts/core/attachment_authorization.py`](../scripts/core/attachment_authorization.py) implementiert: `create_machine_authorization()` erzeugt intern eine Maschinen-Autorisierung (`receipt_class: "machine"`, `receipt_type: "attachment_auto_evaluation"`), und `guard_context_authorization()` akzeptiert diese ausschließlich im `evaluation`-Kontext. Human-Approval-Aufrufstellen (Filing, Promotion, Export, Disposition, Apply-Discard, direkter Fetch) weisen die Maschinen-Klasse fail-closed ab; ein strukturell identisches Caller-/Mail-/Manifest-Dictionary ist wegen fehlender prozessinterner Provenienz keine Autorität. Details: [`effects.md`](effects.md) §3.
  * `DispositionRequest` (`build_disposition_request`): Bindet kanonisch `attachment_id`, `index_entry_sha256`, `decision`, `review_after`, `rationale`, `candidate_review_hash`, `promotion_id`, `promotion_status`.
  * `ApplyRequest` (`build_apply_request`): Bindet den exakten, unteilbaren Löschumfang (`action: "discard"`, `attachment_id`, `decision_id`, `index_entry_sha256`, `quarantine_path`, `sha256`, `size_bytes`, `run_id`, `schema_version: 1`). Bulk-Apply ist strikt verboten (`attachment_id` zwingend).
  * `canonical_apply_request_sha256()`: Deterministischer SHA-256 Hash des serialisierten JSON-Objekts zur kryptographischen Bindung des Apply-Receipts.
* **Attachment Analysis Handoff (MD-A4 / MD-C1)** ([`scripts/core/attachment_handoff.py`](../scripts/core/attachment_handoff.py)):
  * Bereinigtes Übergabe-Envelope für Downstream-Analysen (LLM-Dossier-Synthese).
  * Trennt strikt technischen Abschluss (`analysis_status: "completed"`) von inhaltlicher Abdeckung (`analysis_completeness: "full" | "truncated" | "partial" | "unavailable"`).
  * Bindet alle 6 Coverage-Felder (`analysis_completeness`, `truncation_reason`, `truncation_stage`, `handoff_character_count`, `analysis_character_budget`, `source_character_count`) kanonisch in `compute_handoff_hash()`.
  * Verhindert Drift über `HandoffDriftError` (`handoff_character_count != len(text)`).
* **Attachment Filing Candidate (MD-A5 / MD-C1)** ([`scripts/core/attachment_filing.py`](../scripts/core/attachment_filing.py)):
  * Vorschlag für Cloud-Ablage mit `promotion_status: "pending_human_review"`. Rein lesend; führt keine unautorisierten Cloud-Mutationen aus.
  * Bindet `coverage_evidence` deterministisch in `compute_candidate_hash()` ein.
  * Ergänzt bei eingeschränkter Abdeckung (`analysis_completeness != "full"`) einen transparenten Hinweistext im `reason`-Feld.
* **`Dossier`** ([`scripts/core/modes/dossier.py`](../scripts/core/modes/dossier.py)): Strukturierte Fallakte mit klassifizierten Workpackages (`WP...`), Aufgaben und Signalstärken.
* **`SynthesisHandoff`** ([`scripts/core/synthesis_handoff.py`](../scripts/core/synthesis_handoff.py)): Bereinigtes Übergabepaket für identifizierte Action Items zur Weitergabe an `task-desk`.
* **Staged `attachment_evaluation` + validierter `attachment_analysis_handoff` (FR-15 / MD-E1-T04/T05/T06)** ([`scripts/core/attachment_evaluation.py`](../scripts/core/attachment_evaluation.py)):
  * Kanonisches, staged Zwischenergebnis des öffentlichen `attachment_evaluate`-Orchestrators. Der Erfolgs-Envelope ist exakt `{"attachment_evaluation": {…}, "attachment_analysis_handoff": {…}}`; der zweite Schlüssel trägt den mit `validate_attachment_handoff` validierten, gekapselten Handoff. Ein `failed`-Envelope enthält **nur** `attachment_evaluation` (kein Handoff-Geschwister).
  * Felder des staged Objekts: `status ∈ {completed, not_needed, skipped, failed}` (beschreibt nur die Evaluierungsstufe), ein bounded `reason` (Laufzeit: `classification_clear`, `no_attachments`, `no_allowed_attachments`, `handoff_ready`, `still_ambiguous`, `lock_unavailable`, `policy_blocked`, `quota_exceeded`, `fetch_failed`, `extraction_failed`, `handoff_invalid`; `evaluation_pending` bleibt nur als historische T04-Konstante und ist **nicht** Teil der bounded runtime-Menge), `authorization ∈ {auto_evaluated, not_applicable}`, ein bounded `files[]` mit genau `{filename, sha256, mime_type, chars, coverage, run_id}` (`coverage ∈ {full, truncated}`), `used_for_classification` **immer** `false`, `classifier_revision` **immer** `null`.
  * `attachment_evaluate` akzeptiert nur rohes MIME plus trusted Message-/Binding-Kontext, Decision-/Read-Escalation-Metadaten, die effektive Policy und minimale Fetch-/Extract-Control-Plane-Bindungen; Kandidaten-, Policy-, Fetch-Status-, Receipt-, Materiality-, Status-/Reason-/Files- oder Handoff-Werte des Callers sind keine Autorität. `inventory_sha256` wird aus dem revalidierten MIME-Kandidaten-SHA-256 abgeleitet (MD-A2-Fetch-Semantik), der kanonische `review_hash` via `compute_review_hash` gebildet und die interne Maschinen-Autorisierung (T03) gemint und sofort im `evaluation`-Kontext geprüft.
  * **T05 komponiert linear:** `attachment_fetch.verify_workspace_lock` (upfront) → `op_attachment_fetch` → `extract_attachment_content` → `build_attachment_analysis_handoff(default_materiality="required_for_decision")` → `validate_attachment_handoff`, alles unter einer `run_id` je Mail. Erfolgreich → `completed`/`handoff_ready`; validiert blockiert → `completed`/`still_ambiguous` (nie `supplementary`). Nur der validierte Handoff trägt begrenzten Inhalt; das staged Objekt enthält nie Rohinhalt oder absolute Pfade.
  * **T06 Fail-closed-Matrix (implementiert):** Exakt erwartete kanonische Ausnahmen und der terminale Extraktionsstatus werden auf bounded `failed`-Envelopes abgebildet – fehlender/fremder Lock (`WorkspaceLockError`) → `lock_unavailable`; aktiver Inhalt/disallowed Extension (`ActiveContentBlockedError`/`DisallowedExtensionError`), MIME-/Extension-Drift (`MimeDriftError`/`ExtensionMimeDriftError`), getrackte Quarantäne (`TrackedQuarantineError`/`QuarantinePreflightError`) → `policy_blocked`; Quote (`QuotaExceededError`) → `quota_exceeded`; Identity-/Hash-Drift (`AttachmentDriftError`), Kollision/Inventar (`QuarantineCollisionError`/`QuarantineInventoryError`), Symlink-Escape (`SymlinkEscapeError`) → `fetch_failed`; terminaler Extraktionsstatus `extraction_failed` (inkl. `timeout_exceeded`) → `extraction_failed`; Handoff-Ablehnung (`AttachmentHandoffError`/`HandoffDriftError`/`InvalidMaterialityError`) → `handoff_invalid`. Jeder `failed`-Envelope hat `authorization: "not_applicable"`, `files: []`, `used_for_classification: false`, `classifier_revision: null`, **kein** Handoff-Geschwister und keinen Exception-Text, Rohinhalt oder absoluten Pfad. Kanonisch gültige, aber unvollständige erforderliche Evidenz (`corrupt_attachment`/`attachment_conversion_unavailable`) bleibt `required_for_decision` und endet `completed`/`still_ambiguous`. Jede `DraftManifest`-Installation bleibt **MD-E2**.
