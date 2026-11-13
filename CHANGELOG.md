# Changelog

Alle nennenswerten Änderungen am `office-intelligence`-Bundle. Format orientiert
an [Keep a Changelog](https://keepachangelog.com/de/1.1.0/); jedes Release fasst
die abgeschlossenen Feature Requests (FR) zusammen. Details: Feature-Records im
Archiv [`docs/features/_archive.md`](docs/features/_archive.md) und System Map
([L1](docs/system-map/README.md), [L2](skills/mail-desk/docs/system-map/README.md)).

## [Unreleased]

### 2026-11-13 - Issue #88: Pending-/Ledger-Konsum-Adapter

#### Added

- **`pending` (read-only):** `mail_desk_sent_ledger.py pending` liefert die offene
  Ledger-/Handoff-Sicht als stabile JSON-Envelope (`kind: mail-desk.sent-ledger.pending-view`,
  `view_version: 1`) mit `handoff_state` (`none`/`pending`, nie `written`),
  `handoff_present`/`handoff_item_count` (fehlende vs. leere Datei), `pending_count`,
  `consumed_ignored`, `consumed_outcomes`/`consumed_sources` (inkl. `no_evidence` und
  `legacy_import`), `counts` und Ledger-Histogramm. `handoff_item_count > 0` bei
  `pending_count == 0` sind ausschließlich konsumierte Reste und blockieren den Sync nicht;
  eine fehlende Datei bleibt kein Leerbeweis. `items[]` enthält nur gekürzte
  SHA-256-Referenzen (`item_ref`, `thread_ref`, `waits_for`; **keine** Anonymisierung, keine
  Unumkehrbarkeits-Zusage), `headers_complete`,
  `truncated_fields` (Feldnamen), `anchored` und `anchor`; keine Betreffzeile, Adresse, Datum,
  Envelope-ID oder rohe/normalisierte Message-ID. `--account` prüft jedes offene Item gegen die
  Workspace-Bindung (`mailbox_mismatch` stoppt die ganze Runde, gehashte `mismatch_refs`).
  Keine Lease nötig; kaputtes JSON, Schema- und Aliasfehler enden fail-closed ohne Write. Der
  Körperleseweg bleibt ein ausdrücklich offener Gate (kein freigegebener `read`-Aufruf).
- **`record` (append = Konsum):** genau ein Selektor (`--item-ref` aus der Pending-Sicht oder
  `--message-id`); Lease **und** kanonische `<workspace>/data/mail-desk`-Bindung werden vor dem
  ersten Append verifiziert (`record_outcome` hat selbst keinen Guard). `synthesized` verlangt eine
  exakte `vorgang_ref`, verbietet `reason_code`, erlaubt optional `evid_id` und prüft fremde
  EVID-Bindungen sowie die frische Ankerreferenz gegen **alle** Ledger-Zeilen (`anchor_conflict`,
  `anchor_ambiguous`, `reference_anchor_unresolved`, `handoff_metadata_incomplete`); der Anker-Gate
  läuft nur für `synthesized`. `no_evidence` verlangt einen schema-gültigen Begründungscode und
  verbietet jeden Anker, hat aber keinen Anker-Gate. Offene
  Vorfahren blockieren (`ancestor_pending`); passende dauerhafte Identität ist ein No-op
  (`already_consumed`), abweichende `ledger_conflict`. Das Handoff bleibt byte-gleich.
  `norm_message_id` erscheint nur bei expliziter `--message-id`-Auswahl; `--account` ist optional
  (ohne ihn kein Account-Gate, Consumer-Pflicht).
- **`consume` (Batch-Konsum bereits ausgewerteter Items):** liest ein consumer-eigenes
  Evaluationsdokument (`schema_version: 1`, `kind: mail-desk.sent-evaluation`, `items[]`) und
  appendet in **einer** lease-gebundenen Runde nur `synthesized` (exakte `vorgang_ref`, optionale
  `evid_id`) und `no_evidence` (schema-gültiger `reason_code`). `not_evaluated` und Items ohne
  Eintrag bleiben offen und werden nur als gehashte `item_ref` gemeldet; es wird kein Outcome
  erfunden. Selektoren sind `item_ref` aus `pending` oder die rohe `message_id`. Das gesamte
  Dokument wird **vor** dem ersten Append validiert; doppelte Ziele (`evaluation_duplicate`), eine
  widersprüchliche dauerhafte Identität (`ledger_conflict`) und eine mehrfach gebundene EVID-ID
  (`evid_shared_conflict`) stoppen ohne Write. Jeder Append nutzt dieselben Gates wie `record`
  (Lease/Bindung, frischer Anker, offene Vorfahren, Account); ein Item mit offenem Vorfahren wird
  `deferred`/`ancestor_pending` und nach dem Vorfahren in derselben Runde erneut versucht. Ein
  fachlicher Item-Konflikt endet als `consume_blocked` (Exit 2), bereits geschriebene Zeilen bleiben
  dauerhaft, ein zweiter Lauf ist idempotent. `--dry-run` nimmt keine Lease, schreibt nichts und
  meldet `would_consume`/`projected_pending_after`. Der Envelope trägt
  `pending_before`/`pending_after`, `consumed`/`appended`, `already_consumed`, `not_evaluated`,
  `deferred`, `blocked`, `evaluation_entries`, `unmatched_evaluations`, Ledger-/Handoff-Hashes und
  `handoff_unchanged`; rohe Message-IDs werden nicht ausgegeben. Der Consumer behält damit nur die
  inhaltliche Auswertung, keine eigene Ledger-Schreiblogik.
- **`crash-check` (read-only):** exakte Triade `message_id` + `vorgang_ref` + `EVID-ID`
  (normalisierte Message-ID, byte-/case-exakte Token, keine Substrings). `unique`/`absent` = Exit 0;
  `conflict` = Fehler-Envelope mit Exit 2 (doppelte/widersprüchliche Ledger-Zeilen, fremde EVID auch
  ohne Ziel-Datensatz, Thread-Anker-Konflikte, offener Vorfahre). Mit Handoff-/Evidence-Metadaten
  werden alle Anker geprüft (`promotion_anchor_conflict`, `promotion_anchor_ambiguous`,
  `promotion_anchor_unresolved`, `handoff_metadata_incomplete`, `promotion_metadata_conflict`);
  Evidence-Thread-Metadaten sind strikt typgeprüft (`references` nur Liste nichtleerer Strings,
  `in_reply_to`/`thread_key` nur Strings), sonst `promotion_metadata_invalid` statt
  `norm_id`-Coercion. Ein offenes Item mit offenem Vorfahren ist `ancestor_pending`;
  `safe_to_append` ist dann **nie** `true`. Optionales `--evidence-file` ergänzt `evidence_state`
  und `safe_to_append` (nur bei eindeutiger
  Evidence, fehlender Zeile und konsistentem Anker ohne offenen Vorfahren); der Aufruf schreibt nie
  und ersetzt `recover-promotion` nicht — beide nutzen dieselbe konservative Anker- und
  Vorfahrenprüfung.
- **`recover-promotion` (gehärtet):** liest das Handoff-Item und alle Anker-Kandidaten; abweichende
  Doppelzeilen, fremde Anker-Vorgänge, referenzlose Anker, unvollständige Header, ein noch offener
  Vorfahre (`ancestor_pending`), malformed Evidence-Thread-Metadaten (`promotion_metadata_invalid`)
  und widersprechende Handoff-/Evidence-Metadaten stoppen vor jedem Append.
- **`--json`:** für den dokumentierten Consumer-Aufruf akzeptiert (JSON bleibt der einzige
  Ausgabemodus); alle Befehle liefern die kanonische sechs-Schlüssel-Envelope.
- **API:** `core.sent_ledger.build_pending_view`, `record_pending_outcome`,
  `consume_evaluated_items`, `read_sent_evaluations`, `check_crash_triad`;
  `core.sent_handoff.pending_handoff_view` als reiner Anker-/Ordnungsschritt auf einem bereits
  geladenen Ledger (kein zweiter, potenziell inkonsistenter Read).
- **Doku:** L1/L2-System-Map (§17/§46), SKILL, Log-Schema, CLI-Operations,
  Batch-Runner-Vertrag und `sent-ledger-migration.md` beschreiben den Adapter; Tests:
  `skills/mail-desk/tests/test_sent_ledger_adapter.py` mit
  `skills/mail-desk/tests/sent_ledger_adapter_fixtures.py`.

#### Notes

- Kein Live-/Mailbox-Zugriff, kein Apply, kein Consumer-Write, keine neue Retention-/MIME-/
  Transportfunktion und keine zweite CLI. Der Körperleseweg bleibt ein offener Gate (kein
  freigegebener `read`-Aufruf). Offene Consumer-Entscheidungen (u. a. strengere
  No-Evidence-Politik, referenzloser Anker, Datenschutz-/Staging-Freigabe) bleiben offen; das
  Bundle erzwingt nur Schema und Sicherheitsvertrag.
- **Ehrlichkeit der Abnahme:** Die positiven Lease-Pfade (`record`, `consume`, `apply`,
  `recover-promotion`) und die Lease-Ablehnung sind in den Offline-Tests mit einem
  **gemockten** Verifier (`verify_workspace_lock`) geprüft; der echte `workspace-lock`-Guard
  und ein Live-/Apply-Lauf in `boku-user` sind **nicht** Teil dieser Abnahme und bleiben ein
  offener Gate. Die Zielbindung (fremder Workspace, Traversal) läuft dagegen ungemockt und
  fail-closed vor dem Verifier.

### 2026-11-13 — Issue #103: Transport-Guard-Item-Isolation, Telemetrie und Progress-Liveness

#### Fixed

- **Item-Timeout tötet nicht mehr den Lauf:** `message read`/`message export` für genau
  einen Envelope werden über Kommandoklasse + dezimale Envelope-ID als item-bezogen erkannt.
  Ein `himalaya_timeout` dieser Aufrufe ist ein Item-Fehler (begrenzt als
  `read_error_reason: "timeout"` sichtbar) und der Batch läuft weiter; der Draft-Pfad kann
  den Preview-Fail-safe ausführen (`read_escalation.fallback: "preview"`,
  `review_required: true`). Erst `ITEM_FAILURE_MAX_CONSECUTIVE = 5` Item-Timeouts ohne
  dazwischenliegenden erfolgreichen Befehl brechen mit
  `consecutive_item_failures_without_progress` ab.
- **F6 unverändert:** Netz-/Start-/Stdin-Fehler sowie Timeouts von
  `envelope list`/`envelope thread`/`folder list` behalten den Zwei-Fehler-Schwellwert
  `two_consecutive_transport_errors`; `mailbox_transport_blocked` bleibt der einzige
  strukturierte Abbruch und wird weiter durch Fallback-Pfade re-raised.
- **Kein `status: running` eines gekillten Laufs:** `runner-progress.json` trägt additiv
  `pid`/`heartbeat_at`. `core.progress.read_runner_progress` liefert eine read-only,
  liveness-geprüfte Sicht und meldet einen verwaisten `running`-Zustand als `aborted` mit
  `interrupted_at` und genullter ETA, ohne die Datei zu verändern;
  `settle_stale_runner_progress` (mutierend, im lease-gebundenen Lauf) oder der nächste
  Tracker schreibt den Zustand fest und bewahrt die Identität unter
  `interrupted_previous_run`. Reguläre Prozessenden beenden aktive Tracker über
  `abort_active_trackers`; Legacy-Zustände ohne `pid` bleiben unangetastet. Die Pid-Prüfung
  ist best-effort und konservativ (unbekannt = lebend); `heartbeat_at` ist informativ.
- **Stabiler Review-Vertrag:** `read_escalation.error.type` bleibt bei
  Full-Read-Timeouts `RuntimeError`; der Item-Grund steht additiv getrennt in
  `read_escalation.reason` (`timeout`/`read_failed`) und `fallback: "preview"`.

#### Added

- **Transport-Telemetrie:** `transport_stats.commands` bleibt im stabilen
  `starts`/`retries`/`errors`-Format; additiv liefert `command_telemetry` je Kommandoklasse
  `duration_ms_total`, die letzten 20 `durations_ms`, `last_duration_ms`, `last_exit_code`
  und `last_reason_code`. Bei einem Block nennt `block_trigger` (und im Envelope
  `transport_trigger`) den auslösenden Aufruf mit Klasse, Envelope-ID, Dauer, Exit und
  Reason; `transport_stats` ergänzt `consecutive_item_failures` und
  `duration_samples_per_command`.
- **Doku:** L1/L2-System-Map, Batch-Runner-, Himalaya-Backend- und Log-Schema-Referenz
  beschreiben Item-Isolation, Telemetrie, Fortschrittsbegriff und Progress-Liveness. Tests:
  `skills/mail-desk/tests/test_issue103_transport_item_isolation.py` (19 Fälle:
  Item-Timeouts für Read/Export, Nicht-Item-/Netz-/Start-/Listen-Negativa, gemischte
  Fehlerfolge, Fünf-Item-Schwelle, Preview-Fallback, Per-Class-Dauern/Exit,
  Block-Envelope-Trigger, Dead-/Live-Pid, Read-only-View, Tracker-Settlement).

#### Notes

- Kein Live-Lauf, keine Mailbox-Aktion, keine Consumer-Daten, keine Änderung an der
  Skip-Mechanik aus #105; `execute`/`verify`/`search`/`sync_sent` bleiben ohne
  Transport-Session und unverändert.

### 2026-11-11 - Issue #107: Sent-Ledger-Outcome, Legacy-Import und Crash-Idempotenz

#### Added

- **Ledger-Outcome (Schema 1, additiv):** `sent-synthesis-ledger.jsonl` trägt optional `outcome`
  (`synthesized`/`no_evidence`), `source` (`synthesis`/`legacy_import`), `reason_code` und `evid_id`.
  Altzeilen ohne `outcome`/`source` gelten unverändert als `synthesized`/`synthesis`; alte Reader lehnen
  neue Felder als `ledger_invalid` ab (fail-closed, Bundle-Pin vor erweitertem Write).
- **`no_evidence`:** `core.sent_handoff.record_no_evidence(data_dir, items, reason_code=...)` konsumiert mit
  Begründungscode, aber ohne Vorgang/EVID-/Thread-Anker. Kanonische Codes: `no_matching_vorgang`,
  `no_relevant_evidence`, `thread_unresolved`, `source_unavailable`, `headers_incomplete`,
  `conflicting_evidence`, `legacy_status_no_evidence`. `record_synthesized(..., evid_id=...)` schreibt die
  Synthese-Zeile mit optionalem EVID-Anker.
- **Konfliktschutz:** Leseindizes bleiben first-line-wins; neue Appends, Legacy-Import und Promotion-Recovery
  prüfen alle Zeilen einer Message-ID (`load_ledger()["all_by_message_id"]`) und stoppen bei abweichendem
  Outcome/Vorgang/EVID/Grund mit `ledger_conflict`; identische Dubletten sind ein idempotenter Skip.
  Ein wiederholter `record_synthesized`-Aufruf darf optionale `vorgang_ref`/`evid_id` weglassen (Wildcard
  gegen die dauerhafte Zeile); aus späteren Thread-Ankern **abgeleitete** Werte brechen alte No-ops nicht,
  explizit übergebene, abweichende Werte bleiben `ledger_conflict`.
- **Workspace-Bindung:** Mutierende Import-/Recovery-Pfade binden das aufgelöste Ziel vor der Lease und vor
  jedem Write an exakt `<workspace>/data/mail-desk`. Ein `--data-dir` in einem anderen Workspace, Traversal
  oder eine Symlink-/Junction-/Reparse-Komponente in `data`/`data/mail-desk` stoppt mit
  `workspace_binding_invalid`; eine Lease für Workspace A autorisiert nie Workspace B.
- **EVID-Eindeutigkeit:** Legacy-Dry-run blockiert `evid_shared_conflict`, wenn dieselbe `evid_id` innerhalb
  des Inventars oder gegen eine andere Message-ID im bestehenden Ledger verwendet wird.
- **Legacy-Import:** `core/sent_ledger.py` und `scripts/mail_desk_sent_ledger.py` mit striktem JSONL-Inventar,
  lesendem Dry-run (`build_legacy_import_plan`), deterministischem `plan_id` aus Target-Fingerprint
  (`target_workspace_id`, kein absoluter Pfad im Report), normalisiertem Inventar und aktuellen
  Ledger-/Handoff-Bytes sowie explizitem `apply_legacy_import(..., approval=plan_id)` unter verifizierter
  Workspace-Lease. Ein Token von Workspace A ist für identische Daten in B stale. Konflikte, fehlende Triade
  (`vorgang_ref` + `EVID-ID`) oder ein veralteter Token schreiben nichts; Teilimporte werden über einen
  frischen Dry-run idempotent fortgesetzt, nach vollständigem Import ist Apply ein sicherer No-op. Keine
  Report-Sonderdatei.
- **Crash-Recovery:** `core.sent_ledger.recover_promotion(...)` prüft das exakte Tripel `message_id` +
  `vorgang_ref` + `EVID-ID` inklusive Teil- und Mehrdeutigkeitskonflikten, doppelten Ledger-Zeilen und fremd
  gebundener EVID-ID. Konkurrierende Evidence mit derselben EVID-ID aber fehlender `message_id` stoppt
  `promotion_recovery_incomplete`; die globale EVID-Prüfung läuft vor dem `already_consumed`-No-op. Löst der
  Thread-Anker einen anderen nichtleeren Vorgang auf, stoppt `promotion_anchor_conflict` (kein `follow_up`
  auf einen fremden Vorgang). Genau ein Evidence-Treffer und höchstens eine passende Ledger-Zeile ⇒
  höchstens eine fehlende Zeile nachtragen; sonst stoppen. At-least-once bleibt, exactly-once bleibt
  außerhalb des Bundles.
- **Schutz:** Mutierende Import-/Recovery-Pfade verlangen eine invocation-eigene, verifizierte
  `workspace-lock`-Lease; statische Symlink-/Hardlink-Aliase und `.torn`-Verhalten bleiben fail-closed.
- **Doku:** L1/L2-System-Map, SKILL, Log-Schema, Batch-Runner und CLI-Operations beschreiben Outcome,
  Provenienz, Dry-run/Approval, Konfliktmatrix, Promotion-Tripel und Versionsskew; neuer Vertrag
  `skills/mail-desk/references/sent-ledger-migration.md`. Tests:
  `skills/mail-desk/tests/test_sent_ledger_{outcomes,legacy_import,recovery,cli}.py`.

#### Notes

- Kein Live-/Consumer-Zugriff, kein realer Backfill, keine Änderung an der externen SOP in boku-user, kein
  zweiter Index und keine neue Retention-/MIME-/Transportfunktion. Hermetische Fixtures only.

### 2026-11-11 — Issue #103: CLI-Batch-Skip über `envelope thread` + Transportzähler

#### Added

- **mail-desk Scan:** Ist `skip_known` aktiv, liest jeder Scan einmal
  `himalaya envelope thread -f <folder> -o json` und baut eine prozesslokale Map
  `Envelope-ID -> normalisierte Message-ID`. Ein bekannter Envelope wird ohne
  Einzel-Read übersprungen; neue und unbekannte Mails laufen unverändert über
  `get_single_email_details` (`core/known_skip.py`).
- **Fail-closed F1–F6:** Map nur aus genau einem erfolgreichen Prozess mit
  gültigem JSON-Array aus `[source, target, weight]`-Tripeln; dezimale
  `id != "0"` und genau eine wohlgeformte, normalisierte Message-ID; Bindung
  nur bei identischer `id` und identischem `subject`; Map-Treffer erzeugt nie
  `collected`; fehlende ID bleibt unbekannt (Einzel-Read); bereits der zweite
  aufeinanderfolgende Transportfehler bricht sofort strukturiert mit
  `mailbox_transport_blocked` ab (kein dritter Start, kein Verschlucken durch
  Fallback-Pfade). Wiederholte IDs bleiben nur bei ausschließlich gültigen,
  identischen Vorkommen erhalten.
- **Transportzähler:** `run_himalaya` zählt erfolgreich erzeugte Prozessstarts,
  Retries und Fehler je Kommandoklasse; das Ergebnis trägt `transport_stats`
  (nur Zähler). Budgetprüfung, Blockveröffentlichung und `Popen` laufen unter
  einem Spawn-Lock, damit parallele Worker weder den letzten Budgetplatz doppelt
  belegen noch nach veröffentlichtem Block starten. Optionales Start-Budget
  über `transport_start_budget`/`--transport-start-budget`; Default 10.000
  (2.500er-Fallbackfenster × bis zu zwei Prozesse je Envelope plus Reserve).
  Die Session gilt nur für `inspect`/`draft`/`pipeline`; `execute`, `verify`,
  `search` und `sync_sent` bleiben unverändert. Classifier-Full-Read,
  Sent-Sync-Aufrufe innerhalb von `inspect`/`draft` und Attachment-Raw-MIME
  reichen F6 durch; bei F6 entstehen keine Manifest- oder Inspect-Ausgabedateien.
- **Thread-Kontext:** Inspect-Threadpools kopieren den `contextvars`-Kontext pro
  Submission, damit Transportzähler und F6-Schutz auch in Worker-Threads gelten.
- **Drafts-Grenze:** Die F6-Durchreichung in `core/drafts.py` ist defensiv. Die
  Drafts-API läuft normalerweise außerhalb der Batch-Transport-Session und ist
  dort inert; innerhalb einer Session bleiben Ordnerliste,
  Eindeutigkeitsprüfung, APPEND-/Readback-Journal und Replace-Lookup/Delete
  fail-closed. Ein bereits erfolgter APPEND wird vor dem Block als
  `appended_unverified` journaliert. Tests:
  `skills/mail-desk/tests/test_draft_transport.py`.
- **Doku:** L1/L2-System-Map, Batch-Runner-Vertrag und Himalaya-Backend
  beschreiben Skip, Normalisierung, Zähler und Abbruch. Tests:
  `skills/mail-desk/tests/test_batch_runner_cli_batch_skip.py`,
  `skills/mail-desk/tests/test_batch_runner_mde2_hardening.py`.

#### Notes

- Kein eigener IMAP-Transport, kein `imaplib`, keine Credentials, kein
  `UIDVALIDITY`, kein Cache über den Scan hinaus; PR #101 und die
  MIME-/Anhangsstrecke bleiben unberührt. Keine Live-Läufe in Tests.

### MIME-Anhangsinventur: Fail-closed-Strukturprüfung

- Nichtleere RFC-822-Quellen werden vor der Inventur auf Defekte im gesamten
  MIME-Baum, mehrfache Singleton-Header und dekodierbare Nutzlasten geprüft.
  `message/rfc822`, unbekannte Transferkodierungen und defekte oder mehrdeutige
  Strukturen ergeben `MimeStructureError` statt einer Teilinventur. Die
  Draft-Reklassifikation setzt den konkreten Fehler elementlokal auf Review/INBOX.
  Leerinput bleibt unverändert.

### 2026-11-11 — Cloud-Atlas: DOC-/POSIX-Härtung (#102)

- **DOC-Konvertierung:** Short-Staging startet initial nur unter Windows ab
  `>= 240` Zeichen geplantem Quell- oder Derivatpfad. Meldet LibreOffice einen
  eindeutigen Pfadlängenfehler, folgt einmalig derselbe Short-Staging-Zweig —
  auch unterhalb von 240 Zeichen und auf POSIX; andere Konverterfehler lösen
  keinen Retry aus. POSIX startet direkt (Vorabprüfung immer `False`) und
  normalisiert Separatoren nicht. Kein neuer POSIX-E2E-Nachweis.
  Vertrag: [effects.md §2](skills/cloud-atlas/docs/system-map/effects.md),
  [Cloud-L2](skills/cloud-atlas/docs/system-map/README.md).

### 2026-11-11 — Cloud-Atlas: Rein lesender Retention-Audit (#104)

- **Retention-/Quarantäne-Policy:** `retention_audit.py` klassifiziert den
  Version-Root als `active`, `retained` oder `quarantine_candidate`
  (>365 Tage hat Vorrang) und meldet unerwartete Layouts/Links. Der Audit ist
  ein separater, rein lesender Einstieg: keine Writes, kein Move/Delete, keine
  Archivierung, keine physische Quarantäne; alle Einträge bleiben
  `action: "none"`, `safe_to_delete: false`, `safe_to_move: false`.
  Vertrag: [effects.md §6.1](skills/cloud-atlas/docs/system-map/effects.md),
  [Cloud-L2](skills/cloud-atlas/docs/system-map/README.md).

### 2026-11-10 — Cloud-Atlas: Einzeldatei-Modus `--only-file` (#99)

- **Einzeldatei-Konvertierung (Vertrag Issue #22):** `convert_cloud_docs.py
  --only-file` konvertiert genau eine Quelle und merged genau einen
  Filemap-Eintrag per Roh-Splice. Mirror und Derivat werden direkt in ein neues,
  unveränderliches Versionsverzeichnis unter `<output_dir>/_versions/`
  geschrieben; der atomare Filemap-Replace ist der einzige Commit-Punkt, das
  Journal folgt `planned → staged → published → committed`. Kein Scan, keine
  Cleanup-/Move-/Delete-Logik, keine Fremdreferenz-Ermittlung; Recovery meldet
  nur und endet sonst fail-closed ohne Writes.
  Vertrag: [Cloud-SKILL](skills/cloud-atlas/SKILL.md),
  [effects.md §6](skills/cloud-atlas/docs/system-map/effects.md),
  [Cloud-L2](skills/cloud-atlas/docs/system-map/README.md).

### 2026-11-09 — Issue #88: Sent-Items-Handoff für die Synthese (Teillieferung)

#### Added

- **mail-desk `sync-sent`:** Envelope-Felder `new_count`/`new_items[]` (inkl. `Cc`,
  `In-Reply-To`, `References`, Envelope-ID, Thread-Schlüssel) und `handoff`; begrenzter (200),
  atomar geschriebener, idempotenter Handoff `data/mail-desk/new-sent-items.json`, der **vor**
  dem Sent-Index fortgeschrieben wird. Konsum-/Dedupe-Ledger
  `sent-synthesis-ledger.jsonl` mit Thread-Ankern (`core/sent_handoff.py`).
  `get_single_email_details` liefert zusätzlich `cc` und `headers_complete`.
- **Fail-closed:** `handoff_pending` (unkonsumierter Handoff + weitere neue Items, nichts
  indexiert), `handoff_invalid`, `sent_window_truncated`, `sync_window_failed`; Exit ≠ 0. >200 neue Items
  werden in 200er-Batches committet (Rest nach Konsum).
- Nicht enthalten: Synthese/LLM-Logik und die Consumer-Pipeline (extern). Issue #88 bleibt
  offen. Vertrag: [L2 §43](skills/mail-desk/docs/system-map/README.md).

#### Fixed (Review-/Audit-Findings zum Erstcommit)

- `sync_sent_items_by_dates` committet einmal statt je Tag (Selbstblockade durch `handoff_pending`).
- Überhang > 200: in der dritten Runde durch deterministische 200er-Batches ersetzt (siehe unten); kein
  Prune-Write (konsumierte Reste werden abgeleitet ignoriert).
- `handoff.state` ist überall `none|written|pending|invalid`; `new_count`/`new_items`/`handoff` auch im
  Legacy-Tupel und in allen Fehler-Envelopes (`sync_failed` statt Erfolg/ungeformtem Fehler).
- Handoff voll schema- und identitätskonsistent validiert, Ledger strikt gegen das Schema (`ledger_invalid`),
  abgerissene (auch UTF-8-)Zeilen blockieren nichts und gehen nicht verloren (Sidecar `.torn`).
- Konsumvertrag: `load_pending_handoff` (frisch verankert, Vorfahren zuerst) und `record_synthesized` mit
  Neuauflösung des Ankers; Verzeichnis-`fsync` best-effort, Crash-Vertrag = Prozess-Absturz.

#### Fixed (zweite Review-/Audit-Runde)

- Überhang > 200 wird in 200er-Batches committet (älteste zuerst, Rest nach Ledger-Konsum, kein ID-Skip).
  Recovery eines Tages nur über `sync_sent_items_by_dates_report(..., include_next_day=False)` (nicht
  `sync_sent_items(dates=[D])`, das D+1 mitliest) und nicht über `--sync-sent N`/`count`.
- Fehler-/Legacy-Envelope vollständig: `reason_code`, `handoff` (inkl. `written_count`/`pending_count`/
  `deferred_count`/`file`) und `write_state` auch bei Bindungs- und Legacy-Fällen; `sync_invalid_count` für
  ungültige Manifest-`count`-Werte.
- Handoff-Grenzen werden auch beim Lesen durchgesetzt (kanonisch 200 Items, 50 References, 100 To/Cc,
  1000 Betreffzeichen); ein dateieigenes höheres `max_items` erweitert nichts.
- Ledger-Dubletten: die erste gültige Zeile je `message_id` gilt für alle Indizes und führt keinen
  widersprüchlichen Thread-Anker ein.
- Fehlervertrag trennt `write_state` `none`/`unknown` (`partial` reserviert); ein fehlgeschlagener
  Handoff-Write nach möglicher Publikation oder Append meldet `new_count: null` statt fälschlich null Writes,
  der Wiederanlauf bleibt idempotent.
- Datumsfenster arbeiten transaktional auf ID-Kopien; ein Lesefehler verwirft Kandidaten und ID-Marken, sodass
  eine später erneut gelistete Mail nicht still übersprungen wird. Ein scheiterndes Fenster bricht den ganzen
  Lauf vor jedem Write ab (`sync_window_failed`), damit das Wasserzeichen keine ältere Lücke überspringt.
- Envelope-Seiten werden bis zur kurzen/leeren Seite paginiert (Dedupe per ID); erst nach `max_pages` volle
  Seiten ergeben `sent_window_truncated` (kein Watermark-Advance, höheres-`max_pages`-Recovery). Das
  Wasserzeichen-Alter blockiert nie: Coverage ab Watermark-Tag (inklusive), leere Tage zählen; Erstlauf mit
  eingefrorenem Laufdatum und 60-Tage-Backfill bzw. `since`. `count` ist ein Item-Budget; bei Erreichen
  `truncated`/`coverage_*` plus Recovery-Aufruf.
- Handoff-/Ledger-`schema_version` und `count` müssen exakt `int` sein (nicht `true`/`1.0`); tief
  verschachteltes JSON (`RecursionError`) ergibt `handoff_invalid`/`ledger_invalid` statt eines Absturzes.
- Untrusted Ledger-/Handoff-Typen (`role`, `truncated_fields`, `match_type`) werden vor Mengenoperationen
  validiert und fail-closed (`ledger_invalid`/`handoff_invalid`) statt `TypeError`.
- Thread-Index verfeinert den Vorgang innerhalb eines Threads über verschiedene Message-IDs, ohne die
  Dubletten-Regel (erste Zeile je Message-ID) zu verletzen.
- L1/L2-Maps, Vertrag, Log-Schema, CLI-Referenz und SKILL synchron aktualisiert.

#### Fixed (dritte Review-/Audit-Runde)

- Persistente, atomare Coverage-Schranke `sent-sync-state.json` (`initial_start`, `covered_through`): jede
  Rückspulung (explizite Daten, älterer `since`, Legacy-Marker) wird **vor** Handoff/Index geschrieben; nach dem
  Append rückt sie nur über einen zusammenhängend angeforderten Prefix, nie über heute/Punktabfragen vor.
  Der heutige Tag wird nie vollständig markiert; Multi-Day-Deferrals und später konsumierte Items können den
  Watermark nicht mehr über unvollständige Tage springen.
- Source-Fenster (`window_date`) statt Mail-Header-Datum bestimmt Sortierung, Barrier und Recovery; fehlende,
  widersprüchliche oder unparsebare Header verschieben kein Item.
- Singular-/plural-int-Datums-API melden `handoff_deferred` und laufen über den Report/State-Pfad; Account-
  Bindung wird vor jedem State-Write geprüft, leere Punktfenster nennen `next_start`/Gap-Recovery. 200er-Batches
  (201/250, Crash/Retry) erzeugen alle IDs eventual. Explizite Datumsaufrufe erzeugen ohne Schranke keine
  jüngere `covered_through` und überspringen keinen Altbestand.
- Legacy `sent-sync-initial-start.json` wird nur gelesen und non-destruktiv migriert; ein älterer `initial_start`
  bleibt erhalten und setzt die Schranke davor zurück.
- `count`-Item-Budget blockiert schon den ersten nichtleeren Tag über Budget (kein Überschreiten); nur
  tatsächlich gelesene/akzeptierte Tage rücken die Schranke vor, der blockierte Tag bleibt `next_start` mit
  erreichbarer Recovery.
- `since`/Initialmarker springen nie über offene Coverage; future `since` wird vor jeder Mutation abgewiesen,
  der Erstlauf mit gültigem `since` persistiert den Marker. State-Schema ist exakt (drei Felder, nicht-null
  `initial_start`); malformed/null/future State und ungültige Kalenderdaten werden vor Read/Write abgewiesen,
  nur autorisierte Reparatur/Quarantäne, kein `since`-Bypass eines malformed State.
- Pagination beendet nur bei klassifiziertem `HimalayaInvocationError`/`himalaya_command_failed` mit exaktem
  „envelope page out of bound“ ab Seite 2 (offline nicht verifizierte **Annahme**); Transport-„buffer index out
  of bounds“ bleibt fail-closed.
- `write_state` unterscheidet `none`/`state`/`unknown` inklusive State-Publish-Fehler und `sync_no_progress`;
  Doku/L1/L2/objects bereinigt.

#### Fixed (vierte Review-/Audit-Runde)

- State-`initial_start`/`covered_through` werden gegen das eingefrorene Laufdatum geprüft; future Werte,
  malformed/null-both State und future `since`/`since` mit Zeitanteil fail-closed vor Read/Write. Altbestand
  `initial_start:null` mit `covered_through` ist nicht sicher migrierbar (auch der Wasserzeichen beweist keine
  lückenlose Untergrenze) und fail-closed; fehlt bei nichtleerem Index jede State-Datei, ebenfalls
  `sent_initial_state_required` (keine Wasserzeichen-Autoinitialisierung). Eine bloße Quarantäne reicht nicht:
  vor Resync muss ein autorisierter Operator einen verifizierten konservativen State/`since` anlegen. Jeder
  originale State/Marker wird vor Merge auf Future geprüft, `covered_through` gleich heute vor dem Read auf
  gestern zurückgesetzt. Nur autorisierte Reparatur/Quarantäne mit validem Schema-JSON, keine autonome Löschung.
- Validierungsfehler setzen keinen synthetischen Handoff-State mehr; der echte Peek-Zustand/pending_count bleibt.
- Count-Break rückt nur tatsächlich akzeptierte Tage vor; ungültige Kalenderdaten und Account-Bindung werden vor
  State-Writes geprüft. Der optionale Draft-Sent-Sync akzeptiert nur echt parsebare ISO-Datum/ISO-Datetime/
  RFC-2822-Header (kein Fallback 2026-01-01 bei „not a date“/unbekanntem Monat) und bricht keinen vollständigen
  Sync ab; der Draft-Pfad schluckt Sync-Ausnahmen wie bisher (Inbound-Semantik unverändert).

#### Fixed (fünfte Review-/Audit-Runde)

- Ein fehlender Coverage-State bei **irgendeinem** bestehenden Sent-Index-Eintrag (auch mit unparsebarem `at`)
  fail-closed `sent_initial_state_required` in allen Einstiegen: Default, `dates`, Report, Singular- und
  Plural-API. Punktdaten, der neueste Watermark und CLI-Defaults initialisieren nie mehr; Handoff/Index bleiben
  unangetastet.
- Datierte APIs reichen ein explizites verifiziertes `since` durch; es ist der exakte Start (kein stilles
  `min(Watermark, since)`), wird vor jedem Read/Write validiert und erlaubt den Resync. Angeforderte Tage vor
  `since` und ein `since` mit Zeitanteil/future sind fail-closed. Crash/Retry wiederholt dieselbe Ablehnung ohne
  Writes; ein pending Handoff bleibt byte-gleich.
- Tests decken alle Einstiege, unparsebares `at`, `since` älter/jünger als der Watermark sowie den
  Repro-Fall (Watermark 2026-01-04, `dates=[2026-01-05]`, Start 2026-01-03) ab; Log-Schema, L1/L2, objects,
  effects, processes, SKILL und Batch-Runner-Vertrag sind synchron.

#### Fixed (sechste Review-/Audit-Runde)

- Recovery bei `sent_initial_state_required` ist exakt dokumentiert und geprüft: Runner ohne `--sync-sent` mit
  `--input`/`--stdin`-Manifest `{"mode":"sync_sent","since":"YYYY-MM-DD","count":N}`, `--keep-input`, Pfade
  unter `data/mail-desk/`. Der direkte `--sync-sent N` setzt den direkten Modus und ignoriert `--input`/`--stdin`;
  der Wrapper `sync-sent --count N` kann kein `since` führen. Ein direkter Runner-Lauf setzt dieselbe gehaltene/
  verifizierte Workspace-Lease voraus wie der Wrapper.
- Ein jüngeres `since` bei bestehendem State wird nie angewendet (kein Coverage-Skip); der Erfolgs-Envelope und
  die CLI-Summary melden additiv `since_requested`, `since_ignored: true`,
  `since_ignored_reason: "coverage_starts_before_requested_since"` und `effective_coverage_start`. Älteres und
  gleiches `since` wenden den Start an (`since_ignored: false`).
- Tests prüfen den Manifest-/stdin-Aufruf offline über gemockte Konfiguration/Dispatch und den Ignore-Hinweis
  (älter/gleich/jünger); `cli-operations.md`, Log-Schema, L1/L2 und SKILL sind synchron.

#### Fixed (siebte Review-/Audit-Runde)

- Der bestehende Wrapper kann jetzt `sync-sent --since YYYY-MM-DD --account <verifiziert>`; er leitet ein
  stdin-Manifest an den Runner und führt die vorhandene maschinelle `verify_workspace_lock`-Prüfung **vor**
  jedem Read/Write aus. Fehlende/fremde Lease stoppt ohne lokale Writes oder Kindprozess. Der direkte
  Runner-Fallback verlangt expliziten Account und dieselbe gehaltene/verifizierte Lease; `--sync-sent`
  bleibt dabei ungesetzt.
- Dokumentation ist portabel (`$MAIL_DESK_SKILL/scripts/...`, kein Workspace-Root-Pfad) und trennt das
  optionale Eingabe-Manifest `batch-sync-sent.json` vom Wrapper-Envelope/Progress unter `tmp/mail-batch/`.
- `sync_sent_items(dates=[...], since=...)` wendet ein älteres `since` nun mit derselben konservativen
  Rewind-Logik wie der Report vor Reads/Commit an; älter/gleich/jünger werden korrekt als
  `since_ignored: false`/`true` samt `effective_coverage_start` gemeldet. Tests decken Barrier-Rewind,
  Retry nach fehlgeschlagenem Fenster, alle drei Formen und die echte Account-Binding-Seam mit
  Offline-Workspace-Fixture ab.

#### Fixed (achte Review-/Audit-Runde)

- Der Wrapper bindet `--data-dir` jetzt kanonisch an den lease-geprüften `--workspace`: relativ oder
  absolut muss der aufgelöste Pfad exakt `<workspace>/data/mail-desk` sein und der Runner-Root-Inferenz
  (`workspace_root_from_data_dir`) entsprechen. Symlinks/Junctions werden aufgelöst; externe Consumer,
  Escapes und fehlende Workspaces stoppen mit `WorkspacePathError` vor Child/Read/Write. Ein fehlendes
  `data/mail-desk` im geprüften Workspace bleibt zulässig; nicht vom Wrapper verwendete Pfade bleiben
  unberührt.
- Tests decken relative/absolute Pfade, externes `--data-dir`, fehlenden Workspace, reale Verzeichnis-Links
  (Symlink oder Windows-Junction, mit Skip wenn nicht verfügbar), fehlende/fremde Lease sowie `--since` und
  Normalpfad ab. CLI-Operations, L1/L2, SKILL und Batch-Runner-Vertrag sind synchron.

#### Fixed (neunte Review-/Audit-Runde)

- Auch das Wrapper-Ausgabeverzeichnis `<workspace>/tmp/mail-batch` wird vor jedem `mkdir`, Prune und Child
  kanonisch an denselben lease-geprüften Workspace gebunden. Zeigen `tmp` oder `mail-batch` per
  Symlink/Junction aus dem Workspace, stoppt der Wrapper mit `WorkspacePathError`, ohne fremde Dateien
  anzulegen, zu überschreiben oder zu löschen; fehlende interne Pfade bleiben zulässig.
- Tests reproduzieren die Lock-Lücke mit realen Links auf beiden Ebenen (`tmp` und `mail-batch`), bestehenden
  externen reconcile-Dateien und `--keep 1`: Dateien bleiben byte-gleich, Guard/Child/Prune werden nicht
  gerufen. Normale Pfade schreiben weiterhin unter `<workspace>/tmp/mail-batch`; CLI-Operations, L1/L2,
  SKILL und Batch-Runner-Vertrag sind aktualisiert.

#### Statische Härtung & akzeptierte Trust-Grenze (zehnte Review-/Audit-Runde)

- Statische Härtung: Jede vorhandene Symlink-/Junction-/Reparse-Point-Komponente in `data`, `data/mail-desk`,
  `tmp` oder `tmp/mail-batch` wird vor mkdir/Prune/Child abgewiesen, auch intern, gebrochen oder
  selbstreferenziell. `durable_append_jsonl` lehnt statische Symlink-/Hardlink-Aliase der Index-/Ledger-Datei
  ab, bevor sie geöffnet wird.
- **Scope-Grenze (akzeptiert, Follow-up Issue #91):** Ein konkurrierender Same-User-Prozess mit
  Schreibrechten im selben Workspace kann Pfade zwischen statischer Prüfung und Zugriff austauschen (TOCTOU)
  oder nach der Prüfung Hardlink-/Symlink-Aliase anlegen. Das liegt ausdrücklich außerhalb des kooperativen
  Lease-Scope; die Lease ist keine OS-Sicherheitsgrenze gegen denselben Benutzer. Vollständige Isolation
  erfordert OS-Mittel (separater Benutzer/Namespace/Container/ACL) oder eine fd-/handle-basierte
  Neuarchitektur und ist als Issue #91 verfolgt. Die Dokumentation nennt diese Grenze ausdrücklich, statt
  Race-Sicherheit zu behaupten.
- Tests: reale interne/externe Junctions, gebrochene und selbstreferenzielle Symlinks, Hardlink-/Symlink-Alias
  der Sent-Index-Datei mit byte-gleicher externer Datei; Race-Injection wird bewusst nicht als „sicher“
  getestet, solange keine belastbare handle-basierte Verankerung existiert.

#### Fixed (elfte Review-/Audit-Runde)

- `<datei>.torn`-Sidecars werden jetzt vor **jeder** Mutation statisch auf Symlink-/Hardlink-Aliase geprüft.
  Ein aliaster Sidecar führt zu `sent_index_alias_invalid`/`ledger_alias_invalid`, ohne Hauptdatei oder externes
  Ziel zu verändern.
- Der statische Alias-Preflight für `sent-index.jsonl` und das Ledger (inkl. Sidecars) läuft jetzt **vor**
  Handoff-Publikation und Index-Append. Direktes `record_synthesized` prüft das Ledger samt Sidecar vor dem
  Lesen/Append. Die Fehler sind keine transienten `sync_failed`/`write_state: unknown`-Fälle: `write_state`
  bleibt `none`/`state`, Handoff/Index bleiben unangetastet; Recovery ist ausschließlich manuelle
  Reparatur/Quarantäne, ein Rerun ohne Korrektur scheitert identisch (keine automatische Löschung).
- Reason-Codes, Sidecar-/Recovery-Semantik und `write_state` sind in Log-Schema, Effects, L1/L2, SKILL,
  CLI-Operations und Batch-Runner-Vertrag synchronisiert; Tests decken Index- und Ledger-Aliase als Hauptdatei
  und Sidecar ab (Hardlink immer, Symlink wo verfügbar).

#### Fixed (zwölfte Review-/Audit-Runde)

- Der Alias-Preflight läuft jetzt in **allen** Einstiegen (Default, `dates`, Report, Singular/Plural und direkter
  Append-API) **vor** Index-/State-Nutzung, Mailbox-Read und jedem Coverage-/Handoff-/Index-Write. Damit kann
  der Report-Pfad bei leerer/dedupten Kandidatenliste keine Coverage mehr fortschreiben, bevor der Alias
  erkannt wird; Index, externes Ziel, State und Handoff bleiben bytegleich, der Collector wird nicht gerufen.
- Alias-Fehler-Details heißen `alias_file`/`alias_kind`/`alias_label`; sie überschreiben nicht mehr
  `handoff.file`. Envelope- und Summary-Tests prüfen Index-/Ledger-/Sidecar-Aliase bei `handoff.state`
  `pending` und `none`, inklusive unverändertem Handoff-Pfad.
- Der Fehlertext behauptet nur noch, dass diese Operation keine Mailbox-, Index-/Ledger-, State-, Handoff- oder
  Append-Ziel-Inhalte gelesen/geschrieben hat (bereits erfolgte Konfig-/Lease-Reads bleiben außen vor).
  Die akzeptierte Same-User-Race-Grenze (#91) wird weiterhin nicht als gelöst dargestellt.

#### Fixed (dreizehnte Review-/Audit-Runde)

- Statische Alias-Fehler lesen den abgewiesenen Pfad nicht mehr für Diagnosen: `failure_envelope` überspringt den
  Handoff-Peek und meldet `handoff.state: unknown` mit `pending_count: null`; auch `peek_handoff_state` prüft das
  Ledger statisch und liest einen Alias nicht. Ein fremdes Hardlink-Ledger kann damit kein falsches `none`/`0`
  mehr vortäuschen.
- `append_sent_index_entries` prüft als direkte API jetzt alle vier Ziele (Index, Ledger und beide
  `.torn`-Sidecars) vor Index-Read/Write; ebenso die private Einzel-Datum-API vor `_known_index_ids`.
  Tests decken Ledger-/Sidecar-Hardlinks für den Direkt-Append und die private API ab.
- `handoff.state` erlaubt additiv `unknown`; Log-Schema, Effects, Objects, L1/L2, SKILL, CLI und Batch-Runner
  sind synchronisiert. Die Same-User-Race-Grenze (#91) bleibt unverändert außerhalb des kooperativen Scope.

#### Fixed (vierzehnte Review-/Audit-Runde)

- `_file_alias_kind` behandelt `PermissionError`/andere `OSError` als `unverifiable` statt sie entweichen zu
  lassen. `peek_handoff_state` wirft dadurch nie und meldet bei unverifizierbarem Ledger `unknown`/
  `pending_count: null`; auch `failure_envelope` bleibt bei invalid-count/sync-Fehlern vollständig geformt.
- Consumer-Reads sind fail-closed: `load_ledger`, `load_pending_handoff` und `load_sent_index` prüfen ihre
  Zieldateien (inklusive `.torn`) statisch vor dem Lesen. Ein fremdes Hardlink-Ledger kann keine
  Konsumentscheidung mehr verfälschen; Tests decken pending Handoff, fremde bereits synthetisierte IDs,
  keine Lese-/Schreib-/Promotion-Aufrufe und bytegleiche externe Dateien ab.
- SOP-Trigger ist maschinenlesbar: `handoff_allows_noop` erlaubt nur `pending_count == 0` bei `state`
  `none`/`written`; `unknown`/`invalid` oder `pending_count: null` blockieren Synthese **und** No-op bis zur
  manuellen Behebung. SKILL, Batch-Runner, L1/L2, Log-Schema und Effects sind synchronisiert.

#### Fixed (fünfzehnte Review-/Audit-Runde)

- Der Classifier verschluckt `sent_index_alias_invalid` nicht mehr: Einzelklassifikation und Draft propagieren
  den strukturierten Fehler, statt den fehlenden Lookup als `needs_reply: true` zu deuten. Der Runner gibt ihn
  ohne Traceback mit `reason_code`/Alias-Details aus; der reguläre Nicht-Alias-Reply (`replied_via_sent`) bleibt
  unverändert.
- `load_pending_handoff`/`read_handoff` prüfen auch das Handoff-Dokument statisch (`handoff_alias_invalid`) vor
  dem Lesen; `peek_handoff_state` prüft Ledger **und** Handoff und liefert bei Alias `unknown`/`null`.
  Tests mit echtem Pending- und leerem Handoff-Hardlink belegen keine Lese-/Schreib-/Promotion-Aufrufe und
  bytegleiche externe Dateien.
- `lstat`-Permission-/IO-Fehler werden als `alias_kind: unverifiable` (`reason: alias_unverifiable`) fail-closed
  behandelt; Log-Schema, Batch-Runner, Effects, L1/L2, SKILL und CLI dokumentieren `unknown`/`null`. Die
  Same-User-Race-Grenze (#91) bleibt unverändert außerhalb des kooperativen Scope.

#### Fixed (sechzehnte Review-/Audit-Runde)

- Der gemeinsame `assert_append_aliases`-Preflight prüft jetzt zusätzlich `new-sent-items.json` statisch
  (`handoff_alias_invalid`) **vor** State-/Index-Nutzung, Mailbox-Read und jeder Coverage-/Handoff-/Index-Mutation.
  Ein Handoff-Hardlink kann damit im Default-/Report-/Datums-/Singular-/Plural-Pfad und bei der direkten
  Append-API keine Reads oder Writes mehr auslösen.
- Tests mit echtem Handoff-Hardlink (leer und pending), vorhandenem und fehlendem State über alle Einstiege:
  0 Mailbox-Aufrufe, State/Index/Handoff/externe Datei bytegleich, keine neuen Dateien, `handoff_alias_invalid`.
- Doku korrigiert: Der Draft-Pfad schluckt weiterhin **nicht-Alias**-Fehler, propagiert aber statische
  `sent_index_alias_invalid`/`ledger_alias_invalid`/`handoff_alias_invalid`; Log-Schema, Effects, Processes,
  L1/L2, SKILL und Batch-Runner sind synchron. Issue #91 bleibt außerhalb des kooperativen Scope.

#### Fixed (siebzehnte Review-/Audit-Runde)

- `assert_append_aliases` prüft den Handoff jetzt mit demselben `reject_static_aliases` wie die Handoff-Reader:
  `new-sent-items.json` **und** `.torn`-Sidecar. Ein Handoff-Sidecar-Hardlink stoppt damit in allen Einstiegen
  vor State-/Index-Nutzung, Mailbox-Read und jedem Write; der Report kann bei leerem Tag keine Coverage mehr
  vorrücken.
- Tests decken echte Handoff-JSON- und `.torn`-Hardlinks, leer/pending und State vorhanden/fehlend über Default,
  `dates`, Report, Singular/Plural und Direkt-Append ab: 0 Mailbox-/State-Writes, unveränderte Dateien, keine
  neuen Dateien, Diagnose `unknown`/`null`. Fehlendes Sidecar blockiert weiterhin nicht.
- Log-Schema, Effects, L1/L2, SKILL und CLI präzisieren Handoff inkl. `.torn`-Sidecar; Issue #91 bleibt
  unverändert außerhalb des kooperativen Scope.

- Root-Katalog und Progress sind historische Linkstubs; unveränderte Snapshots
  samt Provenienz stehen unter [docs/archive/feature-ledgers](docs/archive/feature-ledgers/README.md).
  Einziger aktiver Backlog: [GitHub Issues](https://github.com/martinderm/office-intelligence/issues).
  Aktuelle Vertrags-/Map-Pflege bleibt erhalten; Workstatus und Run-Abnahme im
  Issue/PR/Run. Frühere Ledger-/Record-Anweisungen in datierten Einträgen unten
  beschreiben ausschließlich den damaligen Prozess, keine heutige Pflicht.

### 2026-09-27

#### Added

- **FR-26/MD-V1 — Verify-Evidenz-Scope:** keep_in_folder/unknown-Items sind von
  der Evidenzpflicht ausgenommen (`in_evidence=null`, Scan geskippt) — Batches
  mit keep-Items schließen regelmäßig über `verify` (Handoff released), statt
  fail-closed zu enden; moved-Items behalten den Evidenz-Check. `reconcile`
  bleibt Recovery-Pfad.
- **FR-26/MD-SE1+MD-SE2 — Sent-Sync-Coverage & fail-closed:** das No-Dates-
  Fenster leitet sich aus dem Index-Watermark ab (max `at` → heute, Cap 60
  Tage mit `follow_up_hint`); `--sync-sent N` wird respektiert (N neueste
  Tage); Per-Datum-Himalaya-Fehler propagieren fail-closed (nie still
  `ok: true`); Telemetrie getrennt (`date_windows_synced` vs.
  `envelopes_examined`); page-full-Tage als `truncated_days` sichtbar.
- Guard-Tests: `test_verify_keep_evidence_scope.py` (3),
  `test_sent_sync_watermark.py` (4), `test_review_reason_read_failure.py` (2).
  **Suite 1073/1073 grün.**

#### Fixed

- **FR-26/MD-A5:** `_full_read_failure` trägt
  `review_reason: "read_escalation_failed"` (maschinenlesbare Triage).

### 2026-09-24

#### Added

- **FR-20/MD-A3 — Anhang-Quota Inline vs. Datei:** getrennte Quoten
  (`max_inline_per_message: 3` für Inline-Teile, `max_attachments_per_message: 5`
  für Datei-Anhänge); neuer transparenter Policy-Reason `skipped_inline_limit`;
  Inline-Signaturbilder verdrängen echte Anhänge nicht mehr
  (Befund Env 9438). Nicht-boolsche `is_inline`-Angaben werden fail-closed
  abgewiesen.
- **FR-19/MD-RC1 — Reconcile-Repair-Härtung:** `apply_local_repairs` korrigiert
  neben fehlenden jetzt auch **stale** Index-/Log-Records nach frischer
  Ziel-Verifikation (append-only `reconciled: true`-Log-Eintrag, idempotent);
  `runner-progress.json` wird im Repair-Pfad deterministisch nachgeführt
  (`completed`/`repaired`) statt auf `failed` stehen zu bleiben
  (Befund Env 9428).
- **FR-23/MD-L1 — Workspace-Lock-Delegation:** Batch-Runner akzeptiert
  `--workspace-lease-id`/`--workspace-conversation-id` (nur mit
  `--draft`/`--inspect`) und reicht die Agent-Session-Lease an die
  Anhang-Bewertung weiter; fremde/abgelaufene Leases bleiben fail-closed
  `lock_unavailable` (Befund Env 9451).
- **Batch-CLI-Werkzeuge adoptiert und gehärtet:** `mail_desk_batch_cli.py`
  (Runner-Wrapper: stdout/stderr nach `tmp/mail-batch/`, kompakter Summary-
  Envelope, `--keep N`-Prune, korrektes `--reconcile`-Threading) und
  `catalog_inspect.py` (read-only Katalog-Feldinspektion, `--fields` auch im
  `--id`-Modus, kanonische Envelope-Fehlerpfade).
- `tests/test_skill_routing_contract.py`, `test_runner_lease_delegation.py`,
  `test_reconcile_stale_repair.py`, `test_attachment_inline_quota.py`,
  `test_batch_cli_wrapper.py`, `test_catalog_inspect.py` — Dokument- und
  Verhaltensverträge für die obigen Änderungen.

#### Changed

- **FR-24/MD-R9 — Pflicht-Skill-Routing:** die mail-desk-`SKILL.md`-Description
  routet Batch-Work nicht mehr weg (die Phrase „führt keine Massenpipeline aus"
  ist entfernt); Batch-/Stapelverarbeitung (draft→execute→verify) läuft fachlich
  durch den Skill. Kanonischer Pflicht-Ladeblock (SKILL.md → Adapter-Referenz →
  cli-operations.md → bei Bedarf batch-runner/folder-rules/log-schema) und
  Consumer-Migrationsbaustein (Phase 0 in der Pipeline-SOP; „Pipeline **und**
  Fachvertrag") sind als Migrationsartefakte dokumentiert
  (Befund Batch 2026-W39/4: drei Werkzeugregel-Verletzungen durch ungeladene
  Fachverträge).
- **FR-System nach ICM restrukturiert:** FR-Records mit YAML-Frontmatter liegen
  unter `docs/features/` (Record library); `FEATURE-REQUESTS.md` ist ein schlanker
  Routing-Katalog; Archiv nach `docs/features/_archive.md` verschoben.
- Doku: `cli-operations.md` (Reconcile-Beispiele, Lease-Delegation, `--keep`,
  `--fields`), `batch-runner.md` (getrennte Quoten, Repair-Nachführung,
  Lease-Delegation, Pflicht-Ladeblock-Kontext), `log-schema.md`
  (runner-progress-Status um `aborted`/`repaired` erweitert).
- System Map: L2-Abschnitte 19–23 (MD-BC, MD-R9, MD-L1, MD-RC1, MD-A3);
  Referenzkorrektur in `processes.md` §5 (Quarantäne-Reconciliation →
  `quarantine_index.py`); Metriken 159 Dateien / 71 Testmodule / 1056 Tests.

#### Archived

- FR-13/15/16/17/18/21/22 (Katalog-Ausdünnung) sowie FR-19/20/23/24 nach
  Abschluss — Records im Langzeit-Archiv, aktiver Backlog: FR-09, FR-10, FR-12.
- **`skills/mail-desk/TODO.md` entfernt, historisiert und disponiert:** das frühere
  gemischte Backlog/Changelog-File (operative boku-user-Aufgaben, technische
  Kandidaten Bulk-Copy/Multi-ID-Move, Multi-Batch-Pipelining, persistenter
  IMAP-Session-Pool, Runner-Modularisierungs-Trigger und die Meilensteine
  Januar–September 2026) wurde in
  [`docs/features/_archive.md`](docs/features/_archive.md) überführt (Sektion
  „Mail-Desk-Historik"); Backlog-Pflege läuft jetzt ausschließlich über das
  FR-System (Record library + Katalog).
- **Disposition der technischen Kandidaten (2026-09-24):** Bulk-Copy
  überholt (GroupWise-POA-Sperrverhalten beweist den sequentiellen Flow als
  stabilen Pfad); persistenter IMAP-Pool entwertet (Himalaya bleibt
  Backend-Architektur); Runner-Modularisierung realisiert (Trigger-Kriterium
  1.147 < 1.500 Zeilen). Konsumerspezifische Betriebsaufgaben entfernt
  (Workspace-Sache von boku-user). Verbleibender Restwert als **FR-25
  (low priority)** formuliert: Multi-Batch-Pipelining/Chunk-Modus für
  historische Großaufarbeitung.
- **Verteilte Notiz-Files aufgeräumt:** `skills/project-catalog-entry/TODO.md`
  (reines FR-01a/b1/b2-Nachweisdokument, kein offenes Backlog) in das Archiv
  überführt (Sektion „Project-Catalog-Entry-Historik") und gelöscht; der
  `project-catalog-entry`-SKILL-Verweis zeigt auf die Archiv-Sektion. Das
  run-scoped Bundle-Root-`TODO.md` (Batch-CLI-Adoptions-Abrechnung) ist mit
  dem Run-Manifest abgerechnet und entfernt. Stale refactor-map-Zelle
  („Backlog & Anstehende Optimierungen") als erledigt markiert.
- **Batch-Runner-Referenz Slimming (DOC-S1):** `references/batch-runner.md`
  von 8.754 auf 7.088 Wörter (−19%, 1.643 → 1.401 Zeilen) — Prosa-Dedup und
  repräsentative JSON-Beispiele; **alle 5 JSON-Schemata byte-identisch**, 21
  Sektionen, Quoten/Limits, Lock-/Lease-Verträge und fail-closed-Regeln
  vollständig erhalten; Size-Gate-Test (`test_batch_runner_slim.py`) pinnt das
  Fenster 5.300–7.100 Wörter dauerhaft.
- **FR-Records mit `sub_skills:`-Pflichtfeld:** alle aktiven FR-Records führen
  jetzt das Frontmatter-Feld `sub_skills:` (betroffene Sub-Skills je Paket);
  die Record-Regel ist in `docs/features/CONTEXT.md` verankert.

### 2026-09-23

#### Added

- **FR-22/MD-ID1–ID4 — Identity-freier Desk-Signals-Fallback:** neutraler
  leerer Fallback ohne `mail-desk.json` (keine „martin"-Trigger im Bundle),
  owner-generierte Anrede-Trigger (Schema 2 mit Schema-1-Legacy),
  sent_indexer/internal-domain/Spam-Gegenindikatoren katalogisiert bzw.
  entfernt, `spam_sender_allowlist`-Gate.
- **FR-21/MD-S4+MD-S5 — Desk-Signals-Doku + Katalog-Validator:**
  Dokumentationsverträge (SKILL.md/batch-runner.md/topic-catalog-entry) und
  ausführbarer Workspace-Katalog-Validator `catalog_validator.py` (Exit 0/1/2).
- **FR-18/MD-S1–S3 — Workspace-Agnostizismus:** Desk-Signals-Katalog
  `mail-desk.json` (Schema 1, fail-loud), sent_indexer-Account-Bindung,
  Zoom-Routing im Topic-Katalog.
- **FR-17/MD-R8 — Reply-Heuristik Closing-Mails:** quote-aware
  Downgrade-Verhalten (B-1–B-11), Live-Verifikation an Env 9412.
- Edge-Pins für `ensure_sentence_end` (satz-sichere Evidence-Betreffs).

### 2026-09-22

#### Added

- **FR-17 — Routing-Katalogtreue und Batch-Determinismus:** routing_priority,
  DNR-/Newsletter-Mapping, MIME-Inventarkette, Inline-Bild-Policy,
  MD-R8-Reply-Heuristik.
- **FR-16 — Dokumentations-/Metrik-Hygiene:** Metrik-SSOT (L2-Kopfzeile kanonisch,
  §3.1-Checkliste), Zellen-Splitting, Daedalus-Referenzkorrektur.

### Frühere Meilensteine (Kurzfassung)

- FR-11 — Attachment-Quarantäne mit Retain-/Promote-/Discard-Lebenszyklus.
- FR-08 — Manifestgebundener Anhangsfluss (MD-A1–MD-A5).
- FR-07 — Kontrollierter Batch-Einstieg, Readiness, Recovery, Completion-Gate.
- FR-05/FR-06 — Batch-Runner-Modularisierung und Post-Batch-Synthese.
- FR-01–FR-04 — Projektkatalog Schema v3, Artefakt-Matching, Subtopics/
  Cloud-Sync/Operations/Events, Dossier-/Apply-/Synthese-/Handoff-Pfad.
