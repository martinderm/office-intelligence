# Mail-Desk — Subsystem System Map: Verben (Processes)

> **Typ**: ICM Form 6 (`system-map`), Dimension: Verben  
> **Subsystem**: [`skills/mail-desk`](../SKILL.md)  
> **Ziel**: Detaillierte Darstellung aller Transformations-, Batch- und Quarantäne-Pipelines mit Code-Referenzen.  
> **Gültig für**: `skills/mail-desk/` relativ zum Repository-Root

---

## 1. Übersicht der Lifecycles im Mail-Desk

```
[Mailbox / Himalaya]
         │
         ▼
[1. Preflight & Search] ──────► scripts/core/himalaya.py
         │
         ▼
[2. Draft-Phase] ─────────────► scripts/core/classifier.py (Katalog-Matching)
         │                       └─► Erzeugt Review-Hash & Manifest
         ▼
[3. Human / Agent Gate] ──────► Freigabe erzeugt Review-Receipt
         │
         ▼
[4. Execute-Phase] ───────────► scripts/core/modes/execute.py
         │                       ├─► Optional: Quarantäne-Pipeline (MD-Q1/MD-Q2)
         │                       └─► final-location-index.json Update
         ▼
[5. Verify-Phase] ────────────► scripts/core/modes/verify.py (Zielordner-Check)
         │
         ▼
[6. Dossier & Handoff] ───────► scripts/core/modes/dossier_synthesis.py
                                 └─► Action Candidates an task-desk
```

---

## 2. Der reguläre Batch-Runner Lifecycle

Definiert in [`references/batch-runner.md`](../references/batch-runner.md) und implementiert in [`scripts/core/modes/`](../scripts/core/modes/):

### Phase 1: `draft` ([`scripts/core/modes/draft.py`](../scripts/core/modes/draft.py))
1. Lädt verarbeitete Mails aus `final-location-index.json` und `sent-index.json`.
2. Ruft unprozessierte E-Mails aus dem Zielordner ab (`get_unprocessed_emails()`).
3. Führt konservative Klassifikation durch:
   - Extrahiert Betreff, Sender und Text-Snippets.
   - Prüft Projektcodes (`WP...`, Projekt-IDs) und Topic-Schlagworte gegen `memory/references/`.
   - Bei Mehrdeutigkeit verbleibt das Item ohne automatische Verschiebung in `INBOX`.
4. Berechnet den `review_hash` über alle vorgeschlagenen Aktionen und emittiert das `DraftManifest`.

### Phase 2: Review-Gate
* Ohne einen gültigen, kryptographisch bindenden Receipt mit identischem `review_hash` weigert sich die Folgestufe strikt zu starten.

### Phase 3: `execute` ([`scripts/core/modes/execute.py`](../scripts/core/modes/execute.py))
1. Validiert den Workspace-Lock (`require_workspace_lock()`).
2. Prüft die Gültigkeit des übergebenen Receipts gegen das Manifest.
3. Führt IMAP-Operationen (Verschieben, Flaggen) atomar aus.
4. Schreibt neue Einträge in `data/mail-desk/final-location-index.json` via atomarem Tempfile-Replace.

### Phase 4: `verify` ([`scripts/core/modes/verify.py`](../scripts/core/modes/verify.py))
1. Fragt die Zielordner via Himalaya ab.
2. Bestätigt das physische Vorhandensein der verschobenen Mails am Zielort.

---

## 3. Die Attachment-Quarantäne-Pipeline (MD-Q1 / MD-Q2)

Implementiert in [`scripts/core/attachment_quarantine_index.py`](../scripts/core/attachment_quarantine_index.py) und [`scripts/core/attachment_fetch.py`](../scripts/core/attachment_fetch.py):

```
[MIME-Part Erkennung] ──► [Symlink / Reparse Check] ──► [Download in attachments/<run_id>/]
                                                                   │
                                                                   ▼
[Atomarer Index-Write] ◄── [SHA-256 Re-Verifikation] ◄── [Inventar .quarantine-inventory.json]
```

**Tracked-Quarantäne-Preflight (FR-15/MD-E1-T02):** Nach der Lock-Ownership-Prüfung und vor dem ersten Write prüft [`scripts/core/quarantine_preflight.py`](../scripts/core/quarantine_preflight.py) den Git-Index am vertrauenswürdigen `workspace_root` (`git ls-files`, read-only, ohne Shell, mit Timeout). Jede getrackte Datei unter `data/mail-desk/attachments/` (inklusive `**/.quarantine-inventory.json`/`.lock`) stoppt fail-closed (`TrackedQuarantineError`); Non-Zero-Exit, Timeout oder unlesbarer Index stoppen fail-closed (`QuarantinePreflightError`). Der Preflight verändert `.gitignore` nie autonom. Derselbe Preflight läuft vor dem ersten mutierenden OCR-Derivat-Write der Extraktionsstufe.

1. **MIME-Inspektion (`inspect_mime_tree`):**
   - Traversiert die MIME-Struktur der Mail und identifiziert Binäranhänge.
2. **Containment- & Sicherheitsprüfung:**
   - Quarantänepfad wird workspace-relativ aufgelöst: `data/mail-desk/attachments/<run_id>/<clean_filename>`.
   - Prüfung auf Symlinks und Windows Reparse-Points (Attribut-Bit `0x400`).
3. **Physischer Download & Inventar-Erzeugung:**
   - Binärdatei wird geschrieben.
   - `.quarantine-inventory.json` wird mit dem tatsächlichen SHA-256 Hash der Disk-Bytes erstellt.
4. **Deterministische ID-Ableitung (`compute_attachment_id`):**
   - SHA-256 Hash über `{"inventory_sha256", "message_id", "part_locator"}`.
5. **Re-Verifikation:**
   - `verify_quarantine_attachment_artifact()` verifiziert die Datei unmittelbar vor dem Index-Write erneut gegen Disk-Bytes.
6. **Atomarer Index-Eintrag:**
   - `save_quarantine_index_atomic()` lädt den bestehenden Index, validiert Schema 1, prüft auf Drift (`AttachmentIndexDriftError`), fügt das Item ein und speichert atomar via `tempfile` + `os.replace`.

### 3.1 Policygebundene Anhang-Evaluierung (`attachment_evaluate`, FR-15/MD-E1-T04/T05/T06/T07)

Implementiert in [`scripts/core/attachment_evaluation.py`](../scripts/core/attachment_evaluation.py). Der Orchestrator revalidiert die echten MIME-Parts, autorisiert intern, komponiert seit **FR-15/MD-E1-T05** die bestehenden kanonischen Seams linear und schließt mit **FR-15/MD-E1-T06** die negative Fehler-/Reason-Matrix fail-closed; **MD-E1 selbst endet vor der Reklassifikation und der `DraftManifest`-Installation** (beide gehören zu MD-E2). **FR-15/MD-E1-T07** nimmt das Paket ab: ein hermetischer End-to-End-Test beweist Inspect → policygebundenen Fetch → begrenzte Extraktion → validierten Handoff für einen klarstellenden erlaubten Anhang bei null Mailbox-/Promotion-/Export-/Dispositions-/Cleanup-/Classifier-Writes; die Reklassifikation und die `DraftManifest`-Installation sind **nicht Teil der MD-E1-Laufzeit**, sondern werden mit **FR-15/MD-E2-T01/T02** für den `draft`-Happy-Path implementiert (§3.2; T03–T04 offen).

```
[Body-/Full-Read-Decision] ──► Trigger? ──nein──► not_needed/classification_clear
                                   │ ja
                                   ▼
                 [inspect_mime_tree] ──► keine Anhänge ──► not_needed/no_attachments
                                   │
                                   ▼
        [canonicalize_and_bind_attachments] ──► kein allowed+available ──► not_needed/no_allowed_attachments
                                   │ ≥1 zulässiger Part
                                   ▼
   [verify_workspace_lock (upfront, kein I/O davor)] ──► Lock fehlt/fremd ──► failed/lock_unavailable
                                   │ owned
                                   ▼  je Part:
   [verify_attachment_drift] ──► [compute_review_hash] ──► [create_machine_authorization]
                                   ──► [guard_context_authorization(CONTEXT_EVALUATION)]
                                   ──► [op_attachment_fetch] ──► [extract_attachment_content]
                                   │ erwartete kanonische Ausnahme ──► failed/{policy_blocked,quota_exceeded,fetch_failed}
                                   │ Status extraction_failed ──► failed/extraction_failed
                                   ▼ (eine run_id je Mail)
   [build_attachment_analysis_handoff(default_materiality="required_for_decision")]
                                   ──► [validate_attachment_handoff]
                                   │ Handoff-Ablehnung ──► failed/handoff_invalid
                                   ──► staged: completed/handoff_ready bzw. completed/still_ambiguous
```

1. **Trigger:** `decision.kind == "unknown"`, `decision.id == "unclassified"`, `decision.confidence == "low"`, `decision.review_required == true` oder eine dokumentierte `read_escalation` (`status` `failed`/`completed`) ohne eindeutige Zuordnung. Ein klarer Entscheid wird durch eine frühere Eskalation nicht erneut ausgewertet.
2. **Revalidierung:** `inspect_mime_tree` und `canonicalize_and_bind_attachments` (inkl. `check_attachment_policy` mit kumulativen Quoten) binden die echten MIME-Parts; `verify_attachment_drift` prüft Account, Folder, Message-ID, Envelope-ID, Part-Locator und Hash gegen die aktuellen Parts.
3. **Autorisierung:** Für jeden zulässigen (`fetch_status: "available"`, `policy_status: "allowed"`) Part wird der kanonische `review_hash` berechnet, die interne Maschinen-Autorisierung gemint und sofort im `evaluation`-Kontext geprüft. Die effektive `policy_revision` stammt ausschließlich aus dem `version`-Feld der effektiven Policy.
4. **Upfront-Lock & Fetch:** Vor der Fetch-Schleife läuft der kanonische `attachment_fetch.verify_workspace_lock`; kein I/O davor. Ein fehlender oder fremder Lock endet fail-closed `failed`/`lock_unavailable`. `op_attachment_fetch` behält seinen eigenen Lock-/Preflight-/Drift-Check und erhält erst nach bestandenem Guard den nicht-autoritativen `capability.to_dict()`-Snapshot als `approval_receipt`. Alle Anhänge einer Mail teilen eine `run_id`.
5. **Extraktion & Handoff:** `fetched` und `already_fetched` laufen identisch durch `extract_attachment_content`; aus den angereicherten Envelopes und den kanonisch gebundenen Parts entsteht ein `build_attachment_analysis_handoff(default_materiality="required_for_decision")`, das mit `validate_attachment_handoff` validiert wird. Die 15k/30k-Budgets samt sichtbaren Markern stammen unverändert aus dem Builder.
6. **Bounded Ausgang:** klarer Entscheid → `not_needed`/`classification_clear`; unklar ohne Anhang → `no_attachments`; unklar ohne zulässigen Anhang → `no_allowed_attachments`; unklar mit zulässigem Anhang und validiertem `ready`-Handoff → `completed`/`handoff_ready`/`auto_evaluated` mit sicheren `files[]`; validierter `blocked_on_required_attachment`-Handoff → `completed`/`still_ambiguous` (nie `supplementary`). Das staged Objekt trägt immer `used_for_classification: false` und `classifier_revision: null`.
7. **T06 Fail-closed:** Nur exakt erwartete kanonische Ausnahmen werden gefangen (kein `except Exception`; Programmierfehler bleiben fail-loud) und auf bounded `failed`-Envelopes abgebildet: `lock_unavailable` (fehlender/fremder Lock), `policy_blocked` (aktiver Inhalt/disallowed Extension, MIME-/Extension-Drift, getrackte Quarantäne/Preflight), `quota_exceeded`, `fetch_failed` (Identity-/Hash-Drift, Kollision/Inventar, Symlink-Escape), `extraction_failed` (terminaler Extraktionsstatus inkl. Timeout) und `handoff_invalid` (Builder-/Validator-Ablehnung). Jeder `failed`-Envelope trägt `authorization: "not_applicable"`, `files: []`, `used_for_classification: false`, `classifier_revision: null`, kein Handoff-Geschwister und keinen Exception-Text/Rohinhalt/absoluten Pfad. Kanonisch gültige, aber unvollständige erforderliche Evidenz (`corrupt_attachment`/`attachment_conversion_unavailable`) bleibt `required_for_decision`. Die `DraftManifest`-Installation erfolgt in MD-E2 (T01, §3.2), nicht in MD-E1.


---

### 3.2 Draft-Integration und einmalige Neuklassifikation (`attachment_reclassification`, FR-15/MD-E2-T01/T02)

Implementiert in [`scripts/core/attachment_reclassification.py`](../scripts/core/attachment_reclassification.py)
und aufgerufen aus [`scripts/core/modes/draft.py`](../scripts/core/modes/draft.py) **nach** der
bestehenden Preview-/Body-/Full-Read-Klassifikation und **vor** `add_draft_contract` (der
Review-Hash bindet die installierte Auswertung):

```
[DraftManifest-Items + Quell-Mails] ──► evaluate_attachments?
        │ nein ──────────────────────► skipped/evaluation_disabled (false/null)
        │ ja, je Item:
        ▼
   [decision_triggers_evaluation?] ──nein──► not_needed/classification_clear (kein Rohabruf)
        │ ja (autoritativer MD-E1-Trigger)
        ▼
   [Identität/Quelle paaren] ──Fehler──► failed/handoff_invalid (Bindungsfehler, false/null)
        ▼
   [Roh-MIME-Abruf] ──Fehler──► failed/fetch_failed (false/null)
        ▼
   [attachment_evaluate genau einmal; deterministic run_id je Nachricht]
        │ bounded Fehler/No-Op ──► staged Objekt (failed/not_needed/still_ambiguous; false/null)
        │ nicht-Mapping/ungültige Vocabulary ──► AttachmentReclassificationContractError (fail-loud)
        ▼ validierter ready-Handoff
   [validate_attachment_handoff gegen Identität/Entscheid/Inventar] ──Fehler──► failed/handoff_invalid
        ▼ konsumierte Hashes gebunden (unique, 64-Hex, == staged files)
   [classify_email(untrusted_external_text=prompt_content) genau einmal]
        │ weiter mehrdeutig ──► completed/still_ambiguous (auto_evaluated, files erhalten, false/null)
        │ nicht-Mapping ──► AttachmentReclassificationContractError (fail-loud)
        ▼ eindeutig
   [installiere completed/classification_clear/auto_evaluated, used_for_classification: true, 64-Hex-Revision]
```

1. Der Rohabruf und die MD-E1-Auswertung erfolgen ausschließlich für ein unklares Item; ein klares
   Item führt keinen Fetch, keine Auswertung und keine Neuklassifikation aus.
2. Der validierte Handoff bleibt gekapselt und wird genau einmal als getrenntes `untrusted_external`
   an die bestehenden Classifier-Regeln übergeben (keine neuen Zieltypen, keine Katalogerfindung).
3. Jedes Item erhält genau ein additives finales `attachment_evaluation`. `used_for_classification:
   true` und eine 64-Hex-Revision nur bei erfolgreicher, eindeutiger Neuklassifikation; sonst
   `false`/`null` (siehe [`objects.md`](objects.md) §6).
4. `--evaluate-attachments`/`--no-evaluate-attachments` sind gegenseitig exklusiv und nur für
   direktes `draft`/`inspect` gültig (`draft` default an, `inspect` default aus); `--pipeline` bleibt
   unverändert. MD-E2-T03 (inspect-Vorschlag) und MD-E2-T04 (Paketabnahme) sind noch offen.
5. **T02 fail-closed:** Identitäts-/Quellen-Pairing-Fehler sind Bindungsfehler (`handoff_invalid`).
   Der `ready`-Handoff wird vor der Klassifikation kanonisch revalidiert; manipulierte, fehlende,
   doppelte oder hash-abweichende Handoffs stoppen ohne Classifier-Aufruf. Fortbestehende
   Mehrdeutigkeit erhält `completed`/`still_ambiguous` mit `auto_evaluated` und sicheren `files[]`.
   Unerwartete Backend-/Programmiervertragsfehler schlagen fail-loud über
   `AttachmentReclassificationContractError` fehl, statt als bounded Policy-Outcome umetikettiert
   zu werden. Der deterministische, PII-freie Run-ID je Nachricht erreicht im zweiten Default-Lauf
   MD-E1 `already_fetched` ohne Doppel-Fetch (kein zweiter Cache/Index).

---

## 4. Die Attachment-Disposition & Sichere Bereinigung (MD-Q3)

Implementiert in [`scripts/core/attachment_disposition_log.py`](../scripts/core/attachment_disposition_log.py) und der CLI [`scripts/mail_desk_attachment_disposition.py`](../scripts/mail_desk_attachment_disposition.py):

```
┌────────────────────────────────┐
│   Quarantäneindex-Eintrag      │
└───────────────┬────────────────┘
                │
                ▼
┌────────────────────────────────┐       Lock-Prüfung & Verifizierbares Receipt
│  Record Disposition (append)   │──────► Request-Hash-Bindung (canonical_disposition_request_sha256)
└───────────────┬────────────────┘       Audit-Trail in attachment-disposition-log.jsonl
                │
       ┌────────┴────────┐
       ▼                 ▼
[Read-Only Report]   [Apply Discard (mutierend & zweiphasig)]
  - eligible           Verifizierbares Apply-Receipt (exakter Scope-Hash)
  - protected       ──► 1. Journal-Initialisierung (state: prepared)
  - invalid         ──► 2. Synchroner 10-Vorbedingungen-Pre-Unlink-Check
                    ──► 3. Physisches os.unlink (state: file_deleted)
                    ──► 4. Atomares .quarantine-inventory.json Update (state: inventory_updated)
                    ──► 5. Atomare Quarantäneindex-Entfernung (state: index_updated)
                    ──► 6. Journal-Finalisierung (state: completed)
```

### 4.1 Entscheidungserfassung (`record_disposition_entry`)
1. **Lock- & Schema-Prüfung:** Prüft Workspace-Lock (`require_workspace_lock`).
2. **Kanonische Request-Bindung:** Baut `build_disposition_request()` und verifiziert das übergebene `approval_receipt` gegen `canonical_disposition_request_sha256()`. Rohe Hashes ohne verifizierten Contract werden fail-closed abgewiesen.
3. **Index-Hash-Bindung:** Lädt den Quarantäneindex-Eintrag und validiert Identität des kanonischen Eintrags-Hashs (`canonical_index_entry_sha256`).
4. **Append-Only Write:** Schreibt deterministisch gebildeten Eintrag (`decision_id`) append-only mit `flush` und `os.fsync` in `attachment-disposition-log.jsonl`.
5. Der Quarantäneindex wird in dieser Phase bewusst nicht verändert, um den kanonischen Hash für Folgeschritte stabil zu halten.

### 4.2 Read-Only Reporting (`report_dispositions`)
* Rein lesende Inspektion von Index, Dispositionslog, physischer Disk und Inventaren.
* Klassifiziert jedes Quarantäne-Item disjunkt in:
  * `eligible`: Berechtigt für physische Löschung (gültige `discard`-Entscheidung, alle 10 Vorbedingungen erfüllt).
  * `protected`: Vor Löschung geschützt (Entscheidung `retain`, unvollständige `promote`-Ablage, fehlende Freigabe, aktive Sperre/Run).
  * `invalid`: Schemadefekt, Pfadtraversierung, Symlink/Reparse-Point oder Integritätsdrift.

### 4.3 Sichere Physische Bereinigung & Recovery Journal (`apply_discard`)
1. **Exklusiver Lock & Einzel-Scope:** Erfordert verbindlichen Workspace-Lock und explizite `attachment_id` (Bulk-Apply ist strikt verboten).
2. **Verifizierbares Apply-Receipt:** Rekonstruiert den exakten `ApplyRequest` und verifiziert das übergebene `apply_receipt` gegen `canonical_apply_request_sha256()`.
3. **Lineare monotone Zustandsmaschine im Discard-Journal:**
   $$\text{prepared (1)} \longrightarrow \text{file\_deleted (2)} \longrightarrow \text{inventory\_updated (3)} \longrightarrow \text{index\_updated (4)} \longrightarrow \text{completed (5)}$$
   - `prepared`: Etabliert den unveränderlichen Bindungseintrag im Journal (`attachment-discard-journal.json`) vor jeder Disk-Mutation.
   - `file_deleted`: Physisches `os.unlink` der Datei nach 10 Vorprüfungen (Pfad-Containment, Symlink-/0x400-Check, SHA-256, etc.).
   - `inventory_updated`: Aktualisiert `.quarantine-inventory.json` unter Verzeichnis-Lock (`_QuarantineInventoryLock`) via atomarem Sibling-Tempfile.
   - `index_updated`: Entfernt das Item atomar aus `attachment-quarantine-index.json`.
   - `completed`: Markiert den Eintrag im Journal als erfolgreich abgeschlossen (`status: "completed"`).
4. **Fehler-Resumability (Wiederanlauf bei Teilausfall):**
   - Tritt nach physischem `unlink` ein Fehler auf (z. B. Inventar-Disk-Fehler), wird `last_successful_state` **nicht** überschrieben, sondern bleibt auf `file_deleted`.
   - `record_journal_failure()` erfasst `status: "failed"`, `failure_stage: "inventory_update"` und strukturiertes `error` in Journal und History.
   - Der Quarantäneindex bleibt unberührt, damit die Item-Metadaten für die spätere Recovery erhalten bleiben.
   - Ein nachfolgender Aufruf von `apply_discard` mit demselben Receipt erkennt die fehlende Festplattendatei, verifiziert das Journal (`last_successful_state == "file_deleted"`), setzt an Stufe 3 an, aktualisiert das Inventar, entfernt den Indexeintrag und schließt den Vorgang sauber ab (`recovered: True`).
5. **Bereinigung bei bereits bereinigtem Index:**
   - Fehlt der Anhang im Quarantäneindex, wird der Vorgang gegen das Journal geprüft.
   - Ein Teilerfolgszustand (`last_successful_state == "index_updated"`) wird im Journal atomar zu `completed` weitergeführt.
   - Ein bereits abgeschlossener Eintrag wird idempotent bestätigt (`deleted_count: 0`).
   - Widersprüchliche Einträge (`state == "completed"` bei `status == "failed"`) werden fail-closed abgewiesen.

---

## 5. Der Read-Only Reconcile Flow

Implementiert in [`scripts/core/modes/reconcile.py`](../scripts/core/modes/reconcile.py):

* **Ziel:** Vollständige Konsistenzprüfung zwischen Dateisystem, Inventaren und Index ohne jede Schreiboperation.
* **Ergebnisse:**
  * `consistent`: Alle Dateien auf Disk stimmen exakt mit Inventaren und Index überein.
  * `missing_review`: Quarantäne-Dateien existieren, wurden aber noch nicht indiziert oder freigegeben.
  * `drift`: Ein Feld (Hash, Größe, Pfad) weicht ab → Sofortiger Alarmstop (`AttachmentIndexDriftError`).

---

## 6. Himalaya Invocation Lifecycle

Implementiert in [`scripts/core/himalaya.py`](../scripts/core/himalaya.py):

1. **Fail-Fast Bootstrap (`resolve_himalaya_invocation`):**
   - Wählt den Konfigurationspfad: expliziter `HIMALAYA_CONFIG`-Override (mit `expanduser`) oder, wenn nicht gesetzt, der plattformspezifische Default (`%APPDATA%\himalaya\config.toml` unter Windows, sonst `~/.config/himalaya/config.toml`).
   - Normalisiert **beide** Pfadquellen über `normalize_himalaya_config_path()` **vor** der Existenzprüfung und der Kommando-Konstruktion; Windows-Laufwerkspfade werden zu UNC (`C:\...` → `\\localhost\C$\...`). Relative Overrides bleiben unverändert.
   - Fehlt die Konfigurationsdatei → Sofortiger Abbruch mit Stopcode `himalaya_config_missing` (verhindert den interaktiven Setup-Wizard). Das umfasst auch den Fall, dass die vorausgesetzte lokale Admin-Freigabe `\\localhost\<drive>$` nicht erreichbar ist.
   - Fehlt die Binärdatei → Abbruch mit `himalaya_unavailable`.
2. **Kommando-Konstruktion (`build_himalaya_command`):**
   - Erzeugt tokenisierte Argumentliste ohne Shell (`shell=False`).
   - Fügt Account-Parameter `-a <account>` an vordefinierter Position ein.
3. **Ausführung mit Timeout:**
   - Führt den Subprozess aus; bei Überschreiten des Timeouts erfolgt sofortiger harter Abbruch ohne Retries (`himalaya_timeout`).
