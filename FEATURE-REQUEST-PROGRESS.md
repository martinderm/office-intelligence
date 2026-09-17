# Feature Request Progress & Code Review

Diese ephemere Arbeitsdatei enthält nur den aktuellen Übergabestand zwischen
Implementierung und Review. Abgeschlossene Feature Requests stehen kompakt in
[`FEATURE-REQUEST-ARCHIVE.md`](FEATURE-REQUEST-ARCHIVE.md); ihre Details bleiben
über Git-Historie und Tests nachvollziehbar.

- `FR-11 / MD-Q3` — Disposition, Promotion-Link und sichere Bereinigung ist umgesetzt und gehärtet:
  - Implementierung des versionierten, append-only Dispositionslogs in
    `skills/mail-desk/scripts/core/attachment_disposition_log.py` sowie der CLI-
    Fassade `skills/mail-desk/scripts/mail_desk_attachment_disposition.py` für
    `data/mail-desk/attachment-disposition-log.jsonl`.
  - Verifizierbare Receipt-Contracts: Rohe 64-Hex-Hashes autorisieren weder Entscheidungen noch
    Löschungen. Approval-Receipts erfordern einen validierten Contract (`receipt_id`,
    `request_hash`, `approved_at`, `approved_by`).
  - Deterministische Request-Hash-Bindung:
    - Disposition-Request: Bindet `attachment_id`, `index_entry_sha256`, `decision`, `review_after`, `rationale`, `candidate_review_hash`, `promotion_id`, `promotion_status`.
    - Apply-Request: Bindet den exakten Löschumfang (`action: discard`, `attachment_id`, `decision_id`, `index_entry_sha256`, `quarantine_path`, `sha256`, `size_bytes`, `run_id`, `schema_version: 1`). Bulk-Apply ist strikt verboten (`attachment_id` zwingend).
  - Persistiertes Apply-/Recovery-Journal (`data/mail-desk/attachment-discard-journal.json`):
    - Vollständige fail-closed Schema-Validierung in `load_discard_journal()`: Unbekannte Root-, Entry- und History-Felder (`DISCARD_JOURNAL_HISTORY_ALLOWED_KEYS`), verbotene Inhalte, deterministische `journal_entry_id`-Prüfung gegen Key, Hash-Formate (64-Hex), Run-ID-Format, relative Pfad-Containment-Prüfung, strikte RFC-3339-Timestamps und Re-Berechnung von `apply_request_hash` gegen die gespeicherten Scope-Felder (`RecoveryJournalCorruptedError`).
    - Strenge History-Sequentialität: Die History muss zwingend mit `prepared` (`rank == 1`) beginnen; verkürzte oder direkt mit `completed` startende Historien werden fail-closed abgewiesen. Folge-Zustände müssen strikt monoton in Einzelschritten (`last_rank + 1`) fortschreiten. Failure-Einträge dürfen nicht vor `prepared` auftreten und erfordern `transition: "failed"`, `status: "failed"`, `stage` und `error`.
    - Vollständige Feld-Konsistenzprüfung:
      - `status: "completed"` erfordert `state == "completed"`, `last_successful_state == "completed"`, `failure_stage == None` und `error == None`.
      - `status: "in_progress"` erfordert nicht-abgeschlossenen Zwischenzustand (`state == last_successful_state != "completed"`), `failure_stage == None` und `error == None`.
      - `status: "failed"` erfordert nicht-leere `failure_stage`, ein valides `error`-Dictionary und verbietet `state == "completed"` (`state != "completed"`). Ein fehlerhafter Eintrag wird niemals als idempotenter Erfolg gewertet.
    - Strikt positive Bytegröße: `size_bytes` wird in `build_apply_request`, `load_discard_journal`, `record_journal_state`, `record_journal_failure` und `apply_discard` strikt als Integer `> 0` validiert (`0`, negative Werte und Booleans führen sofort zum Abbruch).
    - Monotone Zustandsmaschine: `prepared -> file_deleted -> inventory_updated -> index_updated -> completed`. Rückwärtige Sprünge (z. B. `completed -> prepared`) oder übersprungene Schritte (z. B. `prepared -> completed`) werden strikt fail-closed abgewiesen; idempotente Wiederholungen sind No-Ops.
    - Fehler-Resumability ohne Monotonie-Bruch (`record_journal_failure()`): Bei Fehlschlägen (z. B. Inventarfehler nach physischem `unlink`) wird `last_successful_state` niemals durch `failed` überschrieben. Stattdessen bleiben `state` und `last_successful_state` auf `file_deleted`, während `status: "failed"`, `failure_stage` und strukturiertes `error` gesetzt werden. Anschließende Retries setzen deterministisch an `last_successful_state == "file_deleted"` an und schließen den Vorgang erfolgreich ab.
    - Wiederholte Ableitung der Zustandsintegrität: `load_discard_journal()` validiert die gesamte `history`-Sequenz und erzwingt, dass `last_successful_state` exakt mit dem aus der monotonen History abgeleiteten Zustand übereinstimmt.
    - Bindungsinvarianten: Bei bestehenden Journaleinträgen verifiziert `record_journal_state()` alle 9 unveränderlichen Scope- und Bindungsfelder (`attachment_id`, `decision_id`, `apply_receipt_hash`, `apply_request_hash`, `previous_index_entry_sha256`, `quarantine_path`, `sha256`, `size_bytes`, `run_id`).
    - Strenge Verifikation & Teilerfolgsbereinigung bei bereits entferntem Indexeintrag: Ist ein Anhang nicht mehr im Quarantäneindex, wird die Anfrage nicht blind akzeptiert. Der Apply-Request wird kanonisch aus dem Journal rekonstruiert, das Receipt dagegen verifiziert und ein exakter Match (`attachment_id`, `decision_id`, Receipt-Hash) erzwungen. Inkonsistente Zustände (`state: "completed" / status: "failed"`) werden abgewiesen; Teilerfolgszustände (`last_successful_state == "index_updated"`) werden im Journal sauber zu `completed` weitergeführt (`status: "completed"`, bereinigte Fehlerlage).
  - Atomare Inventar-Mutation (`update_quarantine_inventory_atomic`):
    - Re-validiert `.quarantine-inventory.json` vor und nach der Mutation.
    - Schreibt atomar über Sibling-Temp-Datei (`os.replace` mit fsync); verschluckt keine Fehler (`InventoryUpdateError`).
    - Schlägt die Inventar-Aktualisierung nach `unlink` fehl, bleibt der Quarantäneindex **unberührt** für die spätere Recovery (`DispositionApplyError`).
  - Synchrone Pre-Unlink-Sicherheitsprüfung: Re-Verifikation aller 10 Vorbedingungen (Workspace-Lock, Run-Evidence, Pfad-Containment, Symlink-/0x400-Reparse-Schutz, physische Bytes/SHA-256, Inventar, Index-Hash, Discard-Entscheidung, Apply-Receipt) unmittelbar vor dem `unlink`.
  - Rekursive Inhaltsprüfung schließt verbotene Inhalte (`prompt`, `credentials`, `extracted_text`, etc.) in Receipts und Logs fail-closed aus (`ForbiddenContentError`).
  - Read-only Reporting (`report_dispositions`) klassifiziert alle Quarantäne-Anhänge mutationsfrei in `eligible`, `protected` oder `invalid`.
  - Schlanke FR-09-Promotion-Verknüpfung (`promote` bindet `candidate_review_hash`, FR-09-Ergebnis trägt `promotion_id` nach) ohne doppelte Promotion-Engine oder Cloud-Sync.
  - Byte-Identität von `data/mail-desk/final-location-index.json` garantiert.
  - Review-Verifikation: 27 fokussierte MD-Q3-Tests (inkl. aller adversarieller Tests für manipulierte Zustände, verkürzte Historien, widersprüchliche Status/State-Lagen, size_bytes <= 0, unbekannte Felder, Scope-Drift, Transition-Invarianten, echten Inventarfehlern mit End-to-End-Recovery und Idempotenz), 53 Quarantäne-Tests (MD-Q1–Q3), Compileall, Skill-Catalog-Validierung und `git diff --check` sauber.
  - Grenzen: Keine eigenständige Promotion-Ausführung oder Cloud-Remote-Sync (Scope von FR-09).
- `FR-11 / MD-Q2` — Versionierter Quarantäneindex ist abgenommen:
  - Implementierung des Datenzugriffs und der Schema-1-Verifikation in
    `skills/mail-desk/scripts/core/attachment_quarantine_index.py` sowie der CLI-
    Fassade `skills/mail-desk/scripts/mail_desk_attachment_quarantine_index.py` für
    `data/mail-desk/attachment-quarantine-index.json`.
  - Vertragliche Absicherungen & Hardening:
    - Atomarer Replace via Temp-Datei und zwingender Nachweis des Workspace-Locks (`WorkspaceLockRequiredError`), vollständiger Ausschluss von Legacy-Lock-Bypässen (`allow_legacy=False`, `--allow-legacy` entfernt, `WORKSPACE_LOCK_ALLOW_LEGACY` ignoriert).
    - Deterministische 64-Hex-`attachment_id` aus normalisierter `message_id`, `part_locator` und Inventar-SHA-256.
    - Vollständige Pflichtfelder (Account, ursprünglicher Folder, Part-Locator, bereinigter Dateiname, normalisierter MIME-Typ, SHA-256, Dateigröße, Run-ID, relativer Pfad, `analysis_status: "completed"`, `analyzed_at`, `contract_version`, `lifecycle_state: "quarantined"`, `disposition_ref: null`).
    - Fail-closed Schema-1-Root- und Entry-Validierung in `load_quarantine_index`: Unbekannte Root-Felder, ungültige/nicht-Dictionary `items` (kein stiller Fallback), Key/`attachment_id`-Drift oder ungültige Einträge brechen sofort ab.
    - Strikte Containment- und Absolute-Path-Prüfung: Quarantänepfade müssen workspace-relativ unter `data/mail-desk/attachments/<run_id>/` liegen; `reconcile` prüft Containment vor jedem I/O und öffnet/liest niemals Dateien außerhalb des Quarantäne-Run-Baums.
    - Fail-Closed Symlink- und Windows-Reparse-Point-Erkennung (0x400).
    - Physische Re-Verifikation gegen Disk-Bytes und `.quarantine-inventory.json` vor Index-Eintrag.
    - Idempotente Wiederholung prüft alle 17 kanonischen Felder auf Identität (`status: "unchanged"`).
    - Jede Abweichung auf einem der 17 Felder stoppt fail-closed als Drift (`AttachmentIndexDriftError`).
    - Widersprüchliche Aliasfelder (`folder` vs `original_folder`, `clean_filename` vs `filename`, `mime_type` vs `effective_mime_type`) stoppen fail-closed als Drift (`AttachmentIndexDriftError`).
    - `contract_version` (verpflichtender, deterministisch begrenzter Bezeichner) und `contract_hash` (optionaler, strikt validierter 64-Hex-SHA-256) sind sauber entkoppelt und keine Aliase; die Version wird niemals als Hash persistiert, `"1"` wird fail-closed abgewiesen und nie als Hash emittiert; Idempotenz und Hash-Drift bei gleicher Version werden nach eigener Semantik fail-closed geprüft.
    - `disposition_ref` ist strikt auf `null` oder deterministisch begrenzte Strings (max. 128 Zeichen) beschränkt; beliebige Dictionaries oder verschachtelte verbotene Inhalte (`prompt`, `credentials`, `body`, etc.) werden rekursiv fail-closed abgewiesen (`ForbiddenContentError` bzw. `AttachmentIndexSchemaError`).
    - Verbotene Inhalte (`text`, `extracted_text`, `body`, `prompt`, `credentials`, `envelope_id`) und unbekannte Felder werden abgewiesen.
    - Read-only `reconcile` meldet `consistent`, `missing_review` und `drift` ohne Mutation von Index oder Disk.
    - Byte-Identität von `data/mail-desk/final-location-index.json` garantiert.
  - Unabhängige Review-Verifikation: 21 fokussierte MD-Q2-Tests (inklusive umfassender adversarieller Testsuiten), vollständige Mail-Desk-Suite (490 Tests), Compileall, Skill-Catalog-Validierung und `git diff --check` sauber.
  - Grenzen: Noch keine Disposition oder physische Löschung (MD-Q3); Quarantäne-Binärdateien und Inventare bleiben unversioniert.
- `FR-11 / MD-Q1` — Git-Hygiene und Vertragsabsicherung ist abgenommen:
  - `skills/mail-desk/references/cli-operations.md` dokumentiert einen getesteten
    Integrationsvorschlag für Consumer-Workspaces (`data/*`, `!data/mail-desk/`,
    `!data/mail-desk/**`, `/data/mail-desk/attachments/`), die Nicht-Mutation durch
    den Skill, die Einstufung als flüchtige Laufzeitdaten und die Stop-Bedingung bei
    getrackten Dateien. Das Skill-Bundle selbst bleibt von der Quarantäne-Ignore-Regel frei.
  - Hermetischer Ignore-Vertragstest `test_maildesk_attachment_quarantine_mdq1.py`
    extrahiert die Integrationsvorschläge direkt aus der Dokumentation und beweist
    alle Ignore- und Trackbarkeit-Invarianten (sowohl Vollblock als auch Einzelregel-Ergänzung)
    in einem temporären Git-Repository.
  - Review-Verifikation: 5 fokussierte MD-Q1-Tests und 469 Tests der vollständigen
    Mail-Desk-Suite grün; Compileall, Quick-Validate und `git diff --check` sauber.
- `MD-H6` — Himalaya Invocation & Fail-Fast Bootstrap ist zur Review bereit:
  `HIMALAYA_CONFIG` wird als absoluter Config-Pfad via `-c` gebunden,
  fehlende Config oder Executable stoppen vor jedem Prozessstart und der
  interaktive Wizard ist damit ausgeschlossen. `HIMALAYA_COMMAND` bleibt
  bewusst unsupported. Strukturierte Stopcodes sind
  `himalaya_config_missing`, `himalaya_config_invalid`,
  `himalaya_unavailable`, `himalaya_command_failed`, `himalaya_transient` und
  `himalaya_timeout`; ausschließlich klar erkannte Transport-/TLS-Fehler werden
  begrenzt wiederholt, ein Prozess-Timeout stoppt sofort ohne Retry.
  Review-Verifikation: 50 fokussierte H6-/H3-/MD-A3-Tests und 454 Tests der
  vollständigen Mail-Desk-Suite grün.
- `FR-08` ist mit `MD-A1` bis `MD-A5` vollständig umgesetzt, unabhängig reviewt,
  getestet und committed. Der Abschluss ist archiviert.
- Der read-only `attachment_filing_candidate` ist als Schema 1 eingefroren.
- `FR-09` ist noch nicht gestartet. Vor `MD-P1` bleibt die ausdrückliche Human-
  Freigabe für den mutierenden Cloud-Promotion-Pfad erforderlich.

## Nächste Pakete nach Freigabe (parallele Stränge)

- **FR-09:** `MD-P1` — hashgebundene Approval-Receipt und read-only Promotion-Preflight (nach ausdrücklicher Human-Freigabe). Paketkarte und Abnahmebedingungen stehen in [`FEATURE-REQUESTS.md`](FEATURE-REQUESTS.md).
- **FR-11:** Vollständig abgeschlossen (`MD-Q1`, `MD-Q2`, `MD-Q3`).
