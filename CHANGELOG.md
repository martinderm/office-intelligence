# Changelog

Alle nennenswerten Änderungen am `office-intelligence`-Bundle. Format orientiert
an [Keep a Changelog](https://keepachangelog.com/de/1.1.0/); jedes Release fasst
die abgeschlossenen Feature Requests (FR) zusammen. Details: Feature-Records im
Archiv [`docs/features/_archive.md`](docs/features/_archive.md) und System Map
([L1](docs/system-map/README.md), [L2](skills/mail-desk/docs/system-map/README.md)).

## [Unreleased]

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