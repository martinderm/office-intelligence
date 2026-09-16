# Feature Request Progress & Code Review

Diese ephemere Arbeitsdatei enthält nur den aktuellen Übergabestand zwischen
Implementierung und Review. Abgeschlossene Feature Requests stehen kompakt in
[`FEATURE-REQUEST-ARCHIVE.md`](FEATURE-REQUEST-ARCHIVE.md); ihre Details bleiben
über Git-Historie und Tests nachvollziehbar.

## Aktueller Stand

- `FR-11 / MD-Q2` — Versionierter Quarantäneindex ist zur Review bereit:
  - Implementierung des Datenzugriffs und der Schema-1-Verifikation in
    `skills/mail-desk/scripts/core/attachment_quarantine_index.py` sowie der CLI-
    Fassade `skills/mail-desk/scripts/mail_desk_attachment_quarantine_index.py` für
    `data/mail-desk/attachment-quarantine-index.json`.
  - Vertragliche Absicherungen:
    - Atomarer Replace via Temp-Datei und zwingender Nachweis des Workspace-Locks (`WorkspaceLockRequiredError`).
    - Deterministische 64-Hex-`attachment_id` aus normalisierter `message_id`, `part_locator` und Inventar-SHA-256.
    - Vollständige Pflichtfelder (Account, ursprünglicher Folder, Part-Locator, bereinigter Dateiname, normalisierter MIME-Typ, SHA-256, Dateigröße, Run-ID, relativer Pfad, `analysis_status: "completed"`, `analyzed_at`, `contract_version`, `lifecycle_state: "quarantined"`, `disposition_ref: null`).
    - Strikte Containment- und Absolute-Path-Prüfung: ausschließlich workspace-relative Quarantänepfade unter `data/mail-desk/attachments/<run_id>/`.
    - Fail-Closed Symlink- und Windows-Reparse-Point-Erkennung (0x400).
    - Physische Re-Verifikation gegen Disk-Bytes und `.quarantine-inventory.json` vor Index-Eintrag.
    - Idempotente Wiederholung identischer Einträge ist No-op (`status: "unchanged"`).
    - Abweichungen stoppen fail-closed als Drift (`AttachmentIndexDriftError`).
    - Verbotene Inhalte (`text`, `extracted_text`, `body`, `prompt`, `credentials`, `envelope_id`) und unbekannte Felder werden abgewiesen.
    - Read-only `reconcile` meldet `consistent`, `missing_review` und `drift` ohne Mutation von Index oder Disk.
    - Byte-Identität von `data/mail-desk/final-location-index.json` garantiert.
  - Review-Verifikation: 13 fokussierte MD-Q2-Tests, vollständige Mail-Desk-Suite (482 Tests), Compileall, Skill-Catalog-Validierung und `git diff --check` sauber.
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
- **FR-11:** `MD-Q3` — Disposition, Promotion-Link und sichere Bereinigung (`attachment-disposition-log.jsonl`).
