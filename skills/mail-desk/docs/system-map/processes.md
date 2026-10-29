# Mail-Desk — Subsystem System Map: Verben (Processes)

> **Typ**: ICM Form 6 (`system-map`), Dimension: Verben  
> **Subsystem**: [`skills/mail-desk`](../SKILL.md)  
> **Ziel**: Detaillierte Darstellung aller Transformations-, Batch- und Quarantäne-Pipelines mit Code-Referenzen.  
> **Gültig für**: `skills/mail-desk/` relativ zum Repository-Root

---

## 1. Übersicht der Lifecycles im Mail-Desk

### Event-Dossier-Fassade (Issue #8, separater Read-only-Lifecycle)

`mail_desk_event_dossier.process_request` delegiert an `core/event_dossier.py`:
1. `plan`: explizite Ordner/Begriffe, bounded erste Envelope-Seite, foldergebundene
   Inspect-IDs; `search_requests` als Batch-Runner-Configs (`mode: "search"`),
   `inspect_requests` im Client-Manifest-Vertrag (`action: "read"`); keine
   Vollständigkeit, keine Live-Account-Behauptung.
2. `review`: Search/Read/MIME-Locatoren exakt binden, Messages mit konsistentem
   Hash und Anhänge nach Hash deduplizieren, alle Provenienzen behalten.
   Content-Drift scheitert; Kalenderkonflikte/fehlende Parents verlangen Review.
   `needs_reply` strikt als JSON-Boolean; `synthesis_targets` mit dem kanonischen
   Execute-Validator vor Annahme geprüft (Freigabe-Bytes unverändert).
3. `apply`: beide externen Receipts validieren (innerer Batch-Hash ohne Review,
   äußerer Dossier-Hash einschließlich finalem innerem Receipt). Nur Request-
   Übergabe, **keine Ausführung** (`applied: false`). Der Batch-Owner revalidiert
   Katalog, Account, Zielordner und Lock vor der eigentlichen Mutation.
4. `handoff`: `_require_completed_apply` plus exakte Request-/Receipt-/Quellen-
   Paarung; Syntheseziele werden an die **kanonische Normalisierung** des
   genehmigten Requests gebunden (nicht an Rohbytes), nie Partial-Apply
   übernehmen. Work-Order an `event-documentation`, weiterhin
   `pending_content_review`.
5. `report`: Work-Order-/Source-Snapshot-Bindung und vollständige Output-Hashliste
   gegen echte lokale Dateien prüfen. Keine Content-Freigabe.

Der separate Event-Writer (GitHub #14) schreibt ausschließlich lokale ungeprüfte
Entwürfe. `test_event_dossier_writer_integration.py` prüft echte Handoff→Writer→
Report-Komposition und Topic-Writer→Report; nur das vorgelagerte operative Apply
bleibt synthetisch. Keine Live-Mailbox-E2E-Behauptung und kein Topic-Handoff.
Die historischen synthetischen Boundary-Pins bleiben unverändert erhalten.
Vollständige DTOs und Bounds: `references/event-dossier.md`.

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
* Jeder Execute-Auftrag benötigt alle H2-Felder; auch vollständig ungebundene
  Altmanifeste stoppen mit `incomplete_contract`. Die Fassade prüft vor Readiness,
  der Execute-Owner vor Journal/Temp-Cleanup/Progress/Index/Log/Evidence/Mailbox.
  Der CLI-Wrapper prüft vor eigenen Logs/Pruning und übergibt den geprüften
  Snapshot per stdin; der Runner revalidiert.
* `pipeline` ist ausschließlich lesende Vorbereitung: Pending-Draft im Ergebnis,
  kein Sent-Sync, Execute, Verify oder Handoff. Konfidenz/Optionen umgehen kein Gate.
* FR-04/`dossier_apply`: innere H2-Receipt zuerst, bestehende äußere Dossier-Receipt
  danach über den vollständigen inneren Request; beide vor Delegation prüfen.

### Phase 3: `execute` ([`scripts/core/modes/execute.py`](../scripts/core/modes/execute.py))
1. Validiert den Workspace-Lock (`require_workspace_lock()`).
2. Prüft die Gültigkeit des übergebenen Receipts gegen das Manifest.
3. Führt IMAP-Operationen (Verschieben, Flaggen) atomar aus.
4. Schreibt neue Einträge in `data/mail-desk/final-location-index.json` via atomarem Tempfile-Replace.
5. Nach dem Copy verifiziert die Zielordner-Listung **bounded-eskalierend**: bis zu 4 Versuche mit Backoff 0.3/0.5/1.0/2.0s und einer frischen Himalaya-Listung je Versuch. Erschöpft sich das Budget, bleibt der kanonische Fehler „Target verification failed after copy." byte-identisch und trägt einen Diagnose-Suffix (Versuche, gesehene Envelopes, Kandidaten, `elapsed_ms`); dieselben Fakten gehen als freie Details in `journal.transition(...)`. Kein neuer Kanon-Zustand und kein neuer Result-Schema-Key.

### Phase 4: `verify` ([`scripts/core/modes/verify.py`](../scripts/core/modes/verify.py))
1. Fragt die Zielordner via Himalaya ab.
2. Bestätigt das physische Vorhandensein der verschobenen Mails am Zielort.
3. Bleibt der unabhängige Gesamt-Batch-Nachlauf; die execute-seitige Copy-Verifikation (Phase 3, Punkt 5) reduziert den zuvor flakigen Recovery-Fall, ersetzt Phase 4 aber nicht.

### 2.1 Release & Task-Desk-Handoff (Issue #16, doc-only)

`execute` ist bewusst **keine** Completion-Grenze: es hält nur einen
quellengebundenen `synthesis_candidate`, während `data.synthesis_handoff` kanonisch
leer bleibt. Erst ein erfolgreicher, quellengebundener `verify` (direkt oder in
`dossier_apply`) oder ein abgeschlossener `reconcile` liefert genau einen freigegebenen
`synthesis_handoff` plus `completion_report`. Konsumierbar für `task-desk` ist der
Handoff nur bei `completion_report.status: "completed"` **und**
`handoff_released: true` mit gedeckten `verified_message_ids`. Die alte
`dossier_handoff`-Fassade bleibt unverändert; `mail-desk` schreibt **keine**
Task-Desk-/Todoist-Zustände. Reply-Kandidaten entstehen über
`data/mail-desk/replies-needed.jsonl` (Actual Writer `modes/execute.py`). Details:
[`objects.md`](objects.md) §5.5/§6 und die L2-§34.

---

## 3. Die Attachment-Quarantäne-Pipeline (MD-Q1 / MD-Q2)

Implementiert in [`scripts/core/quarantine/quarantine_index.py`](../scripts/core/quarantine/quarantine_index.py) und [`scripts/core/quarantine/attachment_fetch.py`](../scripts/core/quarantine/attachment_fetch.py):

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

Implementiert in [`scripts/core/attachment_evaluation.py`](../scripts/core/attachment_evaluation.py). Der Orchestrator revalidiert die echten MIME-Parts, autorisiert intern, komponiert seit **FR-15/MD-E1-T05** die bestehenden kanonischen Seams linear und schließt mit **FR-15/MD-E1-T06** die negative Fehler-/Reason-Matrix fail-closed; **MD-E1 selbst endet vor der Reklassifikation und der `DraftManifest`-Installation** (beide gehören zu MD-E2). **FR-15/MD-E1-T07** nimmt das Paket ab: ein hermetischer End-to-End-Test beweist Inspect → policygebundenen Fetch → begrenzte Extraktion → validierten Handoff für einen klarstellenden erlaubten Anhang bei null Mailbox-/Promotion-/Export-/Dispositions-/Cleanup-/Classifier-Writes; die Reklassifikation und die `DraftManifest`-Installation sind **nicht Teil der MD-E1-Laufzeit**, sondern werden mit **FR-15/MD-E2-T01/T02** für den `draft`-Happy-Path und mit **FR-15/MD-E2-T03** als opt-in `inspect`-Vorschlag implementiert und mit **FR-15/MD-E2-T04** über den hermetischen Real-Pfad-Akzeptanztest paketabgenommen (FR-15 geschlossen; §3.2/§3.3).

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

### 3.2 Draft-Integration und einmalige Neuklassifikation (`attachment_reclassification`, FR-15/MD-E2-T01/T02/T03/T04)

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
   unverändert. MD-E2-T03 verdrahtet den opt-in `inspect`-Vorschlag (§3.3); mit **MD-E2-T04**
   ist die Paketabnahme über den hermetischen Akzeptanztest
   [`tests/test_batch_runner_mde2_acceptance.py`](../tests/test_batch_runner_mde2_acceptance.py)
   abgeschlossen (**FR-15 geschlossen**): ein einziger realer Pfad Body/Full-Read → mehrdeutig →
   realer `text/plain`-Anhang → genau eine `untrusted_external`-Neuklassifikation →
   persistiertes Projekt-`DraftManifest` mit bounded `attachment_evaluation`
   (`used_for_classification: true`, 64-Hex-`classifier_revision` gebunden an Classifier-Regeln
   plus konsumierten Anhangs-Hash) bei null Mailbox-/Netzwerk- und null
   Execute-/Promote-/Export-/Filing-/Dispositions-/Katalog-/Cloud-Seiteneffekten.
5. **T02 fail-closed:** Identitäts-/Quellen-Pairing-Fehler sind Bindungsfehler (`handoff_invalid`).
   Der `ready`-Handoff wird vor der Klassifikation kanonisch revalidiert; manipulierte, fehlende,
   doppelte oder hash-abweichende Handoffs stoppen ohne Classifier-Aufruf. Fortbestehende
   Mehrdeutigkeit erhält `completed`/`still_ambiguous` mit `auto_evaluated` und sicheren `files[]`.
   Unerwartete Backend-/Programmiervertragsfehler schlagen fail-loud über
   `AttachmentReclassificationContractError` fehl, statt als bounded Policy-Outcome umetikettiert
   zu werden. Der deterministische, PII-freie Run-ID je Nachricht erreicht im zweiten Default-Lauf
   MD-E1 `already_fetched` ohne Doppel-Fetch (kein zweiter Cache/Index).

---

### 3.3 Opt-in `inspect`-Vorschlag (`inspect.py`, FR-15/MD-E2-T03)

[`scripts/core/modes/inspect.py`](../scripts/core/modes/inspect.py) bleibt ohne
`evaluate_attachments`/`propose_manifest` rein lesend. `evaluate_attachments: true` impliziert
den top-level `manifest_proposal`: der Inspektor baut das Manifest über
`draft_manifest(ordered_emails, …, source_sink=…)` (die initiale Preview-/Body-/Full-Read-
Klassifikation schließt zuerst ab) und reicht die transienten effektiven Quellen an denselben
`install_draft_attachment_evaluations(...)`-Seam wie `draft` weiter. `propose_manifest: true`
ohne Auswertung installiert je Proposal-Item genau ein `skipped`/`evaluation_disabled`. Ein
gemischter Batch (klar/geklärt/weiterhin mehrdeutig/bounded failed) bleibt geordnet und
item-lokal; ein ausführbares Batch-Manifest entsteht nur bei explizitem `manifest_file`. Die
T01/T02-Semantik (genau eine Neuklassifikation je Item, `already_fetched`-Idempotenz,
Zero-Mutation, kein Rohinhalt/`prompt_content`/absoluter Pfad/Capability im Output) wird
unverändert geerbt. Mit **MD-E2-T04** ist die Paketabnahme abgeschlossen
(`tests/test_batch_runner_mde2_acceptance.py`); **FR-15 ist geschlossen**.

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

Implementiert in [`scripts/core/quarantine/quarantine_index.py`](../scripts/core/quarantine/quarantine_index.py):

* **Ziel:** Vollständige Konsistenzprüfung zwischen Dateisystem, Inventaren und Index ohne jede Schreiboperation.
* **Ergebnisse:**
  * `consistent`: Alle Dateien auf Disk stimmen exakt mit Inventaren und Index überein.
  * `missing_review`: Quarantäne-Dateien existieren, wurden aber noch nicht indiziert oder freigegeben.
  * `drift`: Ein Feld (Hash, Größe, Pfad) weicht ab → Sofortiger Alarmstop (`AttachmentIndexDriftError`).

Hinweis (Referenzkorrektur 2026-09-24, FR-19/MD-RC1): dieser Abschnitt beschreibt die
Quarantäne-Reconciliation; der **Batch-Reconcile-Flow** (Recovery-Journal-Assessment,
read-only Report + optionaler Repair-Pfad über `scripts/core/modes/reconcile.py`) ist
separat — siehe die Modul-Zelle „Integrität & Reconcile" in der L2-Karte.

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
   - **Optionaler binärer Payload-Seam (Issue #11):** `run_himalaya(..., stdin_bytes=<bytes>)` validiert den Puffer strict als UTF-8 **vor** Executable-/Config-Auflösung und Subprozess (ungültige Bytes → begrenzter `ValueError` ohne Payload-Inhalt; sonst würde Himalaya 1.2.0 an `lines().map_while(Result::ok)` still abschneiden), verlangt bei gesetztem Payload genau ein explizites `-o plain` (sonst `ValueError`), schreibt exakte Bytes in die stdin-Pipe (`subprocess.run(input=...)`, kein `text=True`, keine Newline-Transformation) und führt Payload-Kommandos genau einmal aus (kein APPEND-Retry). `OSError` (Start **oder** Kommunikation) wird auf **beiden** Pfaden als `himalaya_launch_failed` klassifiziert — Wortlaut „outcome may be unknown, not retried" —, `BrokenPipeError` als `himalaya_stdin_failed`, jeweils ohne Payload-Leak. Ohne Payload bleiben `stdin=DEVNULL`, `text=True` sowie Timeout-/Retry-Defaults unverändert.

### 6.1 Ungesendete Draft-Operationen (Client, ohne Approval-Gate)

Implementiert in [`scripts/core/drafts.py`](../scripts/core/drafts.py) und verdrahtet im
JSON-Manifest-Client ([`scripts/mail_desk_himalaya_client.py`](../scripts/mail_desk_himalaya_client.py),
Aktionen `save_draft`/`delete_draft`/`replace_draft`). Der Ablauf ist standalone: kein
Batch-Modus, kein Approval-Gate, kein SMTP-/`send`-Schritt; das Batch-Recovery-Journal
bleibt unberührt.

1. **Zielordner auflösen (`resolve_drafts_folder`):**
   - `folder list -o json` lesen (führende Log-Zeile wird toleriert); Fehler beim
     Listing enden fail-loud als `drafts_folder_unresolved`.
   - Kette: RFC-6154-`\Drafts`-Attribut → konventioneller Name `Drafts` → GroupWise
     `Work In Progress`. Ist nichts davon vorhanden, bricht die Operation begrenzt ab;
     es gibt keinen hartkodierten Ordnernamen als stillen Fallback. Ein explizites
     `-f/--folder` überspringt die Auflösung vollständig.
2. **Entwurf anlegen (`save_draft`, Issue #11):** `build_draft_message` erzeugt den
   rohen RFC-822-Text (SMTPUTF8, CRLF, keine RFC-2047-Kodierung, Body als UTF-8).
   Seine exakten UTF-8-Bytes gehen über die **binäre stdin-Pipe** von
   `message save -o plain -f <folder>` (IMAP APPEND) — nie als argv-Literal, weil
   Himalaya 1.2.0 im Nicht-TTY-/Nicht-JSON-Modus nur stdin liest (`lines()` +
   CRLF-Join, finaler Zeilenabschluss entfällt) und ein großes argv-Argument
   zusätzlich am Windows-Limit (`WinError 206`) scheitert. *Jede* Größe nutzt
   stdin (Schwelle 0, kein Größen-Split); ein Transport-CRLF kompensiert die
   `lines()`-Normalisierung, `-o plain` verhindert den JSON-Bypass. Ein
   APPEND-Fehler endet klassifiziert (`draft_save_failed`) ohne Verifikation,
   ohne Journal-Eintrag, ohne argv-Fallback und ohne Wiederholung. Danach wird der
   neue Entwurf über den kanonischen `verify_in_target_folder`-Seam wiedergefunden;
   scheitert das, endet der Lauf als `draft_verification_failed` statt als
   scheinbarer Erfolg. Ergebnis: `{envelope_id, folder, subject, to, date}`.
3. **Entwurf löschen (`delete_draft`, ohne UID MOVE):** `flag add deleted <id> -f <folder>`
   setzt `\Deleted` direkt am Envelope, `folder expunge <folder>` entfernt die markierte
   Nachricht. `message delete` wird bewusst nicht verwendet, weil es eine Nachricht
   außerhalb des Trash-Ordners in den Trash verschiebt (UID MOVE).
4. **Entwurf ersetzen (`replace_draft`):** neu zuerst (`message save`), alt danach
   (`flag add deleted` + `folder expunge`). Mit Journal wird der Eintrag mit beiden
   Envelope-IDs **vor** dem Löschschritt geschrieben, sodass das Crash-Fenster
   dazwischen reconcile-sichtbar ist: beide Entwürfe sind sichtbar, weil der neue
   bereits durabel angehängt ist. Einziges Restfenster bleibt ein Crash nach dem
   APPEND, aber vor dem Journal-Schreibvorgang (angehängter Entwurf ohne Eintrag).
5. **Journal:** Jede Operation schreibt atomar einen Eintrag in
   `data/mail-desk/draft-journal.json` (Schema 1, siehe [`objects.md`](objects.md) §5.4):
   `envelope_id`, `folder`, `subject`, `to`, `date`, `action`, beim Ersetzen zusätzlich
   `old_envelope_id` und `crash_window`. Ein unlesbares Journal endet fail-loud
   (`draft_journal_corrupted`).

### 6.2 Gebundene Draft-Diagnose (Issue #19, opt-in, Default aus)

Implementiert in [`scripts/core/diagnostics.py`](../../scripts/core/diagnostics.py)
und im JSON-Manifest-Client als Subcommand `diagnose-draft`. Der Lifecycle ist
strikt sequenziell und fail-closed:

1. **Opt-in-Gate:** ohne `opt_in=True` (Code *und* Request) kein lokaler Write und
   kein Prozess.
2. **Request-Shape:** geschlossener, versionierter `DiagnosticRequest`; genau
   `save_draft`, portabler `workspace_id`, keine Account-Overrides.
3. **Review-Input:** separater `DiagnosticReviewReceipt`, hashgebunden an den
   exakten kanonischen Request (Workspace/Account/Folder/Operation); Drift →
   `diagnostic_approval_drift`.
4. **Preflight (read-only, vor dem Marker):** Account read-only über
   `readiness.bind_workspace_account` gegen `.agents/mail-desk-backend.json`
   (Account explizit und nicht-null, exakte Übereinstimmung; Drift →
   `diagnostic_workspace_binding_failed`); Workspace-Identität
   (`<root>/data/mail-desk`, `workspace_id`==Rootname, keine Link-/Junction-
   Vorfahren); Timeout-Validierung; Draft-Bau inkl. Header-Injektions-Abweisung;
   strikte UTF-8-Validierung des Payloads; Config-/Executable-Auflösung. Ein
   fehlgeschlagener Preflight verbraucht **keinen** Versuch.
5. **Lock:** shared `workspace_lock_guard` in place mit `allow_legacy=False`
   (begrenzter vertrauenswürdiger Root; kein `sys.modules`-Vertrauen); Lease/
   Conversation aus Argument oder `WORKSPACE_LOCK_LEASE_ID`/
   `WORKSPACE_LOCK_CONVERSATION_ID`; fehlend/fremd → `diagnostic_lock_unavailable`.
6. **Einmal-Marker:** atomarer `O_CREAT|O_EXCL`-Marker unter
   `data/mail-desk/diagnostics/attempts/<request_hash>.json` **vor** dem ersten
   Prozess; Replay → `diagnostic_replay_refused`.
7. **Genau ein Kindprozess über `run_himalaya`:** der Payload-Seam erhält den
   keyword-only `diagnostic_capture` (Default `None` = unverändert) und startet
   genau ein `message save -o plain -f <reviewter Ordner>` mit festem Kindfilter
   und Popen-Nebendrain (kein `communicate`, begrenzter Speicher); stdout/stderr
   gleichzeitig, stdin in eigenem Thread mit Write-All-Schleife (kein Deadlock,
   keine Kurzschreib-Verluste). Kein `folder list`, keine Verifikation, kein
   Retry, kein Fallback, keine Aufräumprozesse.
8. **Parser & Evidenz:** der begrenzte `BoundedTraceParser` (Cap 64 KiB / 128
   sichere Events) emittiert nur feste Kinds plus begrenzte Zählwerte; die
   Continuation-Warnung wird positiv **nur** als vollständiges 3-Zeilen-Record
   erkannt (Roh-Signatur im Serverinhalt ergibt keinen Event; Cap zählt nur
   klassifizierte Events). Geteilte Puffer/Flags laufen unter einem Lock; nach
   **einem** begrenzten Cleanup-Budget (~5 s) wird eingefroren. stderr-/stdout-
   Überlauf, Reader-Fehler, Timeout, `cleanup_incomplete` oder unbekannter Exit
   setzen `child_reaped` nur bei echtem Reap und enden
   `diagnostic_append_outcome_unknown` (nur ein realer Nicht-Null-Exit ist
   `failed`). Strukturierte Evidenz unter `data/mail-desk/diagnostics/evidence/`
   ohne Mailfelder/Zugangsdaten/Pfade; der kanonische Erfolgs-Envelope bleibt
   unverändert (roher Kind-stdout wird **nicht** durchgereicht).

Details: L2-§35 und [`objects.md`](objects.md) §5.6.
