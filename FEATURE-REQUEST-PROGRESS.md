# Feature Request Progress & Code Review

Diese ephemere Arbeitsdatei enthält nur den aktuellen Übergabestand zwischen
Implementierung und Review. Abgeschlossene Feature Requests stehen kompakt in
[`FEATURE-REQUEST-ARCHIVE.md`](FEATURE-REQUEST-ARCHIVE.md); ihre Details bleiben
über Git-Historie und Tests nachvollziehbar.

- `MD-H6` — Himalaya Invocation & Fail-Fast Bootstrap ist unabhängig reviewt und
  freigegeben: `HIMALAYA_CONFIG` wird als absoluter Config-Pfad via `-c` gebunden,
  fehlende Config oder Executable stoppen vor jedem Prozessstart und der
  interaktive Wizard ist damit ausgeschlossen. `HIMALAYA_COMMAND` bleibt bewusst
  unsupported. Der plattformspezifische Default
  (`%APPDATA%\himalaya\config.toml` unter Windows, sonst
  `~/.config/himalaya/config.toml`) und ein expliziter `HIMALAYA_CONFIG`-Override
  durchlaufen jetzt dieselbe `normalize_himalaya_config_path()`-Normalisierung,
  bevor `is_file` und die Kommando-Konstruktion ausgeführt werden. Die
  UNC-Konvertierung (`\\localhost\<drive>$\...`) setzt eine erreichbare lokale
  administrative Freigabe voraus; Unerreichbarkeit stoppt vor jedem Prozessstart
  als `himalaya_config_missing`. Ein relativer Override bleibt fail-closed als
  `himalaya_config_invalid`. Der begrenzte Stopcode-Vertrag bleibt unverändert:
  `himalaya_config_missing`, `himalaya_config_invalid`, `himalaya_unavailable`,
  `himalaya_command_failed`, `himalaya_transient` und `himalaya_timeout`;
  ausschließlich klar erkannte Transport-/TLS-Fehler werden begrenzt wiederholt,
  ein Prozess-Timeout stoppt sofort ohne Retry.
  Evidenz: Der neue Default-Pfad-Regressionstest
  `test_default_appdata_config_path_is_normalized_before_validation_and_command`
  war vor dem Fix RED (`resolved_path` roher `C:\...`-Pfad statt
  `\\localhost\C$\...`) und ist danach GREEN. Die unabhängige Verifikation im
  isolierten lockfreien Worktree (kanonischer `workspace-lock`-Skill auffindbar)
  ergab: fokussierte MD-H6-Tests 15/15 grün, MD-H3-plus-MD-H6 24/24 grün,
  vollständige Mail-Desk-Suite 536/536 grün. `compileall` und `git diff --check`
  sind sauber.
- `FR-08` ist mit `MD-A1` bis `MD-A5` vollständig umgesetzt, unabhängig reviewt,
  getestet und committed. Der Abschluss ist archiviert.
- `FR-09` ist noch nicht gestartet. Vor `MD-P1` bleibt die ausdrückliche Human-
  Freigabe für den mutierenden Cloud-Promotion-Pfad erforderlich.
- `FR-15` — **MD-E1 (`MD-E1-T01`–`T07`) und MD-E2 (`MD-E2-T01`–`T04`) sind vollständig
  implementiert, reviewt, verifiziert und paketabgenommen; `FR-15` ist geschlossen.** Der staged
  `attachment_evaluation`-Vertrag
  (`used_for_classification` immer `false`, `classifier_revision` immer `null`) und die
  bounded Status-/Reason-Menge sind eingehalten. Alle drei blockierenden
  Sicherheitsvoraussetzungen sind implementiert und getestet: (1) die
  Lock-Legacy-Schließung (`allow_legacy=False`; weder Env- noch Parameter-/
  Manifest-Bypass öffnet einen Schreibpfad), (2) der bounded read-only
  Produktions-Preflight gegen getrackte Quarantäne (`quarantine_preflight.py`) und
  (3) der kontextbewusste Receipt-Klassen-Guard (`attachment_authorization.py`; die
  interne Maschinen-Autorisierung gilt nur im `evaluation`-Kontext und wird von
  Human-Approval-Pfaden fail-closed abgewiesen, typenlose Human-MD-A2-Receipts
  bleiben gültig). `MD-E1-T07` fügt einen hermetischen End-to-End-Akzeptanztest hinzu
  (`skills/mail-desk/tests/test_maildesk_attachment_evaluation_mde1.py`), der in einem
  erfolgreichen Lauf reale Inspect → policygebundene Fetch → Extraktion → validierter
  Handoff für einen klarstellenden erlaubten Anhang beweist, das staged Ergebnis
  (`completed`/`handoff_ready`/`auto_evaluated`), `false`/`null` und die begrenzten
  `files[]` prüft und zugleich **null** Mailbox- (`op_copy_message`/`op_move_message`/
  `op_delete_message`) sowie null Promotion-/Filing-/`DraftManifest`-Install-/
  Dispositions-/Cleanup-/Classifier-Operationen nachweist; Promotion und Export
  besitzen keinen MD-E1-Laufzeitpfad (statische Import-/Call-Grenze). Verifikation aus
  dem Target-Root: `test_maildesk_attachment_evaluation_mde1.py` 112/112 grün, die
  vollständige entdeckte Mail-Desk-Suite 649/649 grün (aktueller Nachweis, kein
  permanenter Abnahmewert), `compileall`, `validate-skills-catalog.py`,
  `validate_workspace.py --json` und `git diff --check` sauber. Seit **`MD-E2-T01`**
  (`skills/mail-desk/scripts/core/attachment_reclassification.py`) ist die standardmäßig
  aktive `draft`-Verdrahtung, die gegenseitig exklusiven Optionen
  `--evaluate-attachments`/`--no-evaluate-attachments` (nur direktes `draft`/`inspect`) und
  die genau einmalige `untrusted_external`-Neuklassifikation samt additivem
  `attachment_evaluation` je Draft-Item implementiert. **`MD-E2-T02`** härtet diese Grenze
  fail-closed: jede bounded MD-E1-Fehler-/No-Op-Ursache bleibt item-lokal in Review/`INBOX`
  (`lock_unavailable`, `policy_blocked`, `quota_exceeded`, `fetch_failed`,
  `extraction_failed`, `handoff_invalid`), Identitäts-/Quellen-Pairing-Fehler sind
  Bindungsfehler (`handoff_invalid`), ein `ready`-Handoff wird vor der Klassifikation
  kanonisch mit `validate_attachment_handoff` gegen Identität, Vorab-Entscheid und
  Anhangs-Inventar revalidiert, fortbestehende Mehrdeutigkeit erhält
  `completed`/`still_ambiguous` mit `auto_evaluated` und sicheren `files[]` (inkl.
  Coverage/Truncation), `classifier_revision` bindet zusätzlich den normalisierten AST des
  Classifier-Regelmoduls (Kommentare/Formatierung/Host-Pfade bewegen ihn nicht), unerwartete
  Backend-/Programmiervertragsfehler schlagen über
  `AttachmentReclassificationContractError` fail-loud fehl (nie als `fetch_failed`/
  `still_ambiguous` umetikettiert),   und ein deterministischer, PII-freier Run-ID je
  Nachricht erreicht im zweiten Default-Lauf MD-E1 `already_fetched` ohne Doppel-Fetch.
  Mit **`MD-E2-T03`** (`skills/mail-desk/scripts/core/modes/inspect.py`) ist der opt-in
  `inspect`-Vorschlag implementiert: `inspect` bleibt ohne Opt-in rein lesend, nur
  `evaluate_attachments: true` erzeugt einen top-level, nicht ausführbaren
  `manifest_proposal` über denselben Item-Flow, `propose_manifest: true` ohne Auswertung
  installiert je Proposal-Item `skipped`/`evaluation_disabled`, und eine ausführbare
  Batch-Manifest-Datei entsteht nur bei explizitem `manifest_file`. Mit **`MD-E2-T04`** ist die
  Paketabnahme über den neuen hermetischen Akzeptanztest
  `skills/mail-desk/tests/test_batch_runner_mde2_acceptance.py` abgeschlossen: ein einziger
  realer Pfad (`run_draft_mode` mit echtem `draft_manifest`, echter Classifier-Regelbasis und
  echtem MD-E1 `attachment_evaluate`) Body/Full-Read → mehrdeutig → realer `text/plain`-Anhang
  → genau eine `untrusted_external`-Neuklassifikation → persistiertes Projekt-`DraftManifest`
  mit bounded `attachment_evaluation` (`used_for_classification: true`,
  64-Hex-`classifier_revision` gebunden an Classifier-Regeln plus konsumierten Anhangs-Hash)
  bei null Mailbox-/Netzwerk- und null Execute-/Promote-/Export-/Filing-/Dispositions-/
  Katalog-/Cloud-Seiteneffekten. **`FR-15` ist damit geschlossen.**
  Fokussierter Nachweis `skills/mail-desk/tests/test_batch_runner_mde2_hardening.py` 30/30
  grün, `test_batch_runner_mde2_draft.py` 14/14 grün, `test_batch_runner_mde2_inspect.py`
  12/12 grün, `test_batch_runner_mde2_acceptance.py` 1/1 grün, `test_batch_runner_modes.py`
  21/21 grün, `test_maildesk_attachment_evaluation_mde1.py` 112/112 grün, vollständige
  entdeckte Mail-Desk-Suite 706/706 grün (aktueller Nachweis, kein permanenter Abnahmewert).
  Git-Index-Metrik: 111 getrackte Dateien / 54 unter `scripts/` (43 unter `scripts/core`) /
  42 Testmodule / 706 Tests.
  Prozesshinweis: Die T02-Red-vor-Implementierung-Sequenz wurde für diesen Dispatch
  nicht eingehalten (Implementierung begann vor dem ersten Testlauf); der Dispatch-
  Hinweis wird im T02-Handoff-Bericht offen dokumentiert.
  Akzeptierte Residuen (bewusst, nicht Teil eines Fixes): (1) Ein opt-in
  `inspect --evaluate-attachments`-`manifest_proposal` erbt beim Manifestbau den
  bestehenden `DraftManifest`-Sent-Index-Synchronisations-/lokalen Index-Schreibpfad;
  das ist **keine** Mailbox-Mutation, bleibt aber operator-sichtbar (ein ausführbares
  Manifest entsteht weiterhin nur bei explizitem `manifest_file`). (2) `manifest_file_created`
  darf ein operator-konfiguriertes Manifest-Ziel echoen; FR-15 verbietet
  anhang-abgeleitete absolute Pfade, nicht diesen operator-konfigurierten Pfad. (3)
  `classifier_revision` bewahrt absichtlich die Katalog-Listenreihenfolge, weil das
  Classifier-Matching reihenfolge-sensitiv ist. (4) Die bestehende defensive Behandlung
  von Nicht-dict-Items und `files: []`-Fehlerfällen benötigt keine Produktionsänderung.
- `FR-13` — **`MD-M1` (`MD-M1-T01`–`T04`) und `MD-M2` sind implementiert und paketabgenommen;
  `FR-13` ist geschlossen.** Das kanonische Unterpaket
  `skills/mail-desk/scripts/core/matching/` besitzt die Owner `date_parser.py`,
  `ambiguity.py`, `project_matching.py` und `topic_matching.py`; `classifier.py` bleibt die
  kompatible Facade und ist auf 810 physische Zeilen kontrahiert (≤ 813; Baseline 2.035)
  und hält Katalog-I/O, Full-Reader-I/O, Zwei-Pass-Orchestrierung,
  Thread-Referenzparsing/-Parent-Lookup, Anhangsbindung und Manifest-Drafting. Die
  Thread-Ordner-Inheritance und der Full-Read-Evidenz-Rebuild sind an die Domänen-Owner
  geroutet (`match_thread_project_inheritance`/`resolve_full_read_project_evidence` bzw.
  `match_thread_topic_inheritance`/`resolve_full_read_topic_evidence`) und wahren die
  Reihenfolge Projekt-vor-Topic sowie exakte Katalogobjekte und die Entscheidungs-/
  Evidenz-/Notiz-/Zielsemantik. Der Kompatibilitätsvertrag
  `skills/mail-desk/tests/test_classifier_compatibility_contract.py` sichert die gesamte
  Basissymbol-, Re-Export-, DI-, Lazy-Import- und Monkeypatch-Oberfläche sowie die neue
  Owner-Identität/-Routing. Der `classifier_revision`-Fingerprint bindet weiterhin exakt
  die geordnete Fünf-Quellen-AST-Menge (`classifier.py`, `matching/ambiguity.py`,
  `matching/date_parser.py`, `matching/project_matching.py`,
  `matching/topic_matching.py`); da sich diese Quellen in MD-M1 einmalig geändert haben,
  rotierten vorhandene `classifier_revision`-Werte genau einmal (genehmigt). Fokussierter
  Nachweis: `test_classifier_compatibility_contract.py` 15/15 grün,
  `test_classifier_matching_modules.py` 19/19 grün, `test_full_body_escalation.py` 11/11
  grün, `test_classifier_evidence_context.py` 6/6 grün, `test_batch_runner_modes.py` 21/21
  grün, `test_batch_runner_mde2_acceptance.py` 1/1 grün, vollständige entdeckte
  Mail-Desk-Suite 786/786 grün (aktueller Nachweis, kein permanenter Abnahmewert). Bewusst
  **nicht** enthalten: Änderungen an FR-15-Anhangssemantik oder neue Matching-Regeln.
  **`MD-M2` (Quarantäne-Paketierung, abgenommen):** Die sechs Quarantäne-Owner
  (`quarantine_index.py` — umbenannt aus `attachment_quarantine_index.py` —,
  `attachment_fetch.py`, `attachment_extract.py`, `attachment_filing.py`,
  `attachment_policy.py`, `attachment_handoff.py`) liegen kanonisch unter
  `skills/mail-desk/scripts/core/quarantine/`; die alten `core/attachment_*.py`-Pfade sind
  dünne `sys.modules`-aliasende Shims mit Objektidentität für Legacy-Importe,
  `mock.patch`-Strings, `patch.object`-Seams und alle 33 `core.__init__`-Re-Exports; die
  17 Schema-1-Pflichtfelder, Hash-Garantien und der `classifier_revision`-Fingerprint sind
  unverändert (keine Rotation). Struktureller Nachweis mit genuine Red-Tests vor der
  Implementierung: `test_quarantine_package_structure.py` 9/9 grün (nach Red mit
  `ModuleNotFoundError: core.quarantine`) und
  `test_quarantine_compatibility_contract.py` 9/9 grün. Vollständige entdeckte
  Mail-Desk-Suite 804/804 grün (aktueller Nachweis, kein permanenter Abnahmewert);
  `compileall`, `validate-skills-catalog.py`, `validate_workspace.py --json` und
  `git diff --check` sauber. Git-Index-Metrik: 128 getrackte Dateien
  / 65 unter `scripts/` (55 unter `scripts/core`) / 48 Testmodule / 804 Tests.

## Nächste Pakete nach Freigabe

- **FR-09:** `MD-P1` — hashgebundene Approval-Receipt und read-only Promotion-Preflight (nach ausdrücklicher Human-Freigabe). Paketkarte und Abnahmebedingungen stehen in [`FEATURE-REQUESTS.md`](FEATURE-REQUESTS.md).
- **FR-13:** geschlossen — `MD-M1` (`T01`–`T04`) und `MD-M2` (Quarantäne-Paketierung unter
  `skills/mail-desk/scripts/core/quarantine/` mit identitätserhaltenden Legacy-Shims) sind
  abgeschlossen und paketabgenommen (siehe oben).
- **FR-15:** abgeschlossen; `MD-E2-T01`–`T04` (standardmäßig aktive `draft`-Verdrahtung,
  `--evaluate-attachments`/`--no-evaluate-attachments`, einmalige Neuklassifikation, additive
  `DraftManifest`-Installation, fail-closed-Härtung, Revisionsdeterminismus, Idempotenz, opt-in
  `inspect`-Vorschlag und hermetische Paketabnahme) sind implementiert und paketabgenommen;
  **FR-15 ist geschlossen**. Spezifikation und Abnahme stehen in
  [`FEATURE-REQUESTS.md`](FEATURE-REQUESTS.md).
