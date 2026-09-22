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
  **`MD-R6` (abgenommen):** `search_mailbox`/`op_search` akzeptieren
  `overall_deadline_seconds` (Default 120, `None` deaktiviert, fail-closed
  Validierung vor jedem Mailbox-Kommando); Deadline-Exhaustion ist terminal mit
  deadline-unterscheidbarem `himalaya_timeout`-Reason, ein per-call Timeout bricht
  den Sweep sofort ab (B-8 behoben). Red-Gate `MD-R6-red-001` (5 Failures);
  Review nach Fix-Runde 1 (fail-closed Validierung) `approve`. Commit `90c8acd`.
  **`MD-R7` (abgenommen):** Subset-Scope (`candidate_ids ⊆ verify_scope_ids ==
  result_ids`) gibt den Synthesis-Handoff für gemischte Batches frei (B-9 behoben);
  Runner-Envelope bewahrt `mode`/`ok` unter `data` und der Verify-Reader nimmt
  kanonische Envelopes als Provenienz an; Evidence-Fallback liest kanonisch
  `memory/evidence/**`, fehlende Evidenz ergibt `False` und Inkonsistenz (B-10
  behoben). Red-Gate `MD-R7-red-001` (4 Failures); Review `approve`. Commit
  `9678b65`. **`MD-R3` (abgenommen):** Trigger-Gate überspringt Items mit
  unverfügbarer Inventur vor `read_raw_mime` (ein kanonischer Export je Item/Lauf,
  B-3 behoben); Konsistenz-Gate verwirft widersprüchliche `completed`-Evaluationen
  (B-4-Fläche); Notes ausschließlich aus der finalen Entscheidung. Red-Gate
  `MD-R3-red-001` (5 Failures); Review `approve`. Commit `ebb7ef2`.
  **`MD-R4` (abgenommen):** Bild-MIME-Parts ohne extrahierbaren Text sind kein
  `required_for_decision`-Trigger mehr (inline Signaturen und angehängte Bilder;
  B-5 behoben); texttragende Parts lösen weiterhin aus; MD-A1-Gates und der
  menschliche MD-A2-Pfad unverändert. Red-Gate `MD-R4-red-001` (3 Failures);
  Review `approve`. Commit `3dda09b`. **`MD-R5` (abgenommen):** `keep_in_folder`
  vertragsdokumentiert mit Enum-Contract-Test (B-6); `progress_*.tmp` wird in
  jedem In-Process-Endzustand aufgeräumt, Atomarität erhalten (B-7 behoben);
  Newsletter-Regel konsistent. Review `approve`. Commit folgt im Paket-Commit.
  **FR-17 ist mit MD-R5 geschlossen.** Mail-Desk-Suite: 896/896 grün.
  **Reale Verifikation (User, 2026-09-22):** 3× identischer `draft 10` über
  die nachfolgenden 10 Mails — kein stiller Hänger (B-8), kein Inline-PNG-Fetch
  (B-5, `IMAGE.png` korrekt als `no_allowed_attachments` ausgeschlossen),
  Feldkonsistenz und Notes-Wortwahl sauber, keine `progress_*.tmp`-Rückstände
  (B-7). Der B-3-Timeout-Fall blieb fail-closed in Review ohne MD-E2-Fetch und
  wurde im Folgelauf korrekt aufgelöst; der Determinismus-Vertrag ist
  entsprechend präzisiert (Lauf-In-Determinismus verbindlich, Byte-Gleichheit
  über transiente Infrastruktur-Unterschiede hinweg bewusst nicht). Drei
  beobachtete Grenzrouten (9408 eucen Highlights, 9419 BeyondTrust, 9412
  LE-LLL) sind Consumer-Katalog-Pflege, kein Bundle-Defekt. **FR-17 ist
  abnahmebestätigt.**
- **Nachtrag B-11 / MD-R8 (User, 2026-09-22):** Nach dem Katalog-Fix im
  Consumer-Workspace verblieb die Befundklasse „Abschluss-/Dankesmails werden
  reply-pflichtig klassifiziert" (Env 9412). Der Nachtrag ist in
  [FEATURE-REQUESTS.md](FEATURE-REQUESTS.md) als **B-11 → Paket `MD-R8`
  (Reply-Heuristik)** dokumentiert und bleibt offen; er ist unabhängig von
  MD-R1–R7 und wird separat umgesetzt. Damit ist FR-17 formal für B-1–B-10
  abgeschlossen; B-11 folgt als eigenes Paket nach FR-16.

  `compileall`, `validate-skills-catalog.py`, `validate_workspace.py --json` und
  `git diff --check` sauber. Git-Index-Metrik: 128 getrackte Dateien
  / 65 unter `scripts/` (55 unter `scripts/core`) / 48 Testmodule / 804 Tests.

## Nächste Pakete nach Freigabe

- **FR-17 (aktiv, 2026-09-22 gestartet):** Routing-Katalogtreue, Batch-Determinismus und
  Vertragshygiene — 10 reproduzierte Befunde (B-1 bis B-10) aus der BOKU-Testbatch
  (10 Mails, Envelope 9387–9404, Account `BOKU-MARTIN`), Paketkarte in
  [`FEATURE-REQUESTS.md`](FEATURE-REQUESTS.md) § FR-17.
  **`MD-R1` (abgeschlossen und paketabgenommen):** `select_project_match` wählt nach
  `(Signalstärke desc, routing_priority desc, Katalogreihenfolge)` — `routing_priority`
  ist wirksam (B-1 behoben: Exaktcode schlägt Kontakttreffer unabhängig von der
  Priorität), fehlende/ungültige Werte gelten als neutral-niedrigste. Der
  `do_not_route_if`-Prädikatsblock ist als `evaluate_do_not_route_signal` kanonischer
  Owner in `core/matching/project_matching.py` und von `topic_matching.py` importiert;
  `do_not_route_if` gilt für Projekte und Topics (einmal vor 2a, einmal vor dem
  Subtopic-Fallback); das `newsletter`-Prädikat ist strikt Betreff-/Header-scoped
  (der `list.`-Token bleibt from/to-only); unterdrückte Kandidaten erscheinen als
  `decision.suppressed_candidates[]` (Katalogdaten only); Newsletter-unterdrückte Mails
  mappen deterministisch auf `classifier.NEWSLETTER_TARGET_FOLDER` (`Newsletter`,
  `copy_as_move`, `review_required: false`), außer ein starker nicht-unterdrückter
  Kandidat gewinnt; nicht-Newsletter-Unterdrückung endet mit Review-Grund
  (`do_not_route_suppressed`) in `INBOX` (`keep_in_folder`). Thread-Vererbung und
  `ambiguity`-Policy unverändert. TDD-Nachweis: Red-Gate `MD-R1-red-001` (7+6
  Assertion-Failures in `test_classifier_routing_priority.py` /
  `test_classifier_do_not_route.py`, hermetische BOKU-Analog-Fixtures in
  `routing_fixtures.py`), nach `MD-R1-prod-001` und Fix-Runde 1 (`MD-R1-fix1-prod-001`,
  `list.`-Token header-only zurückgestuft, Strength-dominates-Priority-Regressionstest,
  Intent-revealing Rename) Review `MD-R1-fix1-review-001` = `approve` mit null Findings.
  Vollständige Mail-Desk-Suite 836/836 grün (aktueller Nachweis, kein permanenter
  Abnahmewert); Compileall, Skill-Katalog, Workspace-Validator und `git diff --check`
  sauber. `classifier_revision` rotierte genau einmal (genehmigt). Verbleibende FR-17-
  Pakete: MD-R2 → MD-R6 → MD-R7 → MD-R3 → MD-R4 → MD-R5. Verbindliche Ausführungsentscheidungen
  (Human-Gate-Protokoll 2026-09-22, Orchestrator): (1) Newsletter-Zielpfad **deterministisch**
  — durch `newsletter`-DNR unterdrückte Mails werden auf den bestehenden `Newsletter`-Pfad
  abgebildet (`copy_as_move`), im Zweifel/fall-abhängig Review-Grund + `suppressed_candidates`
  in `INBOX`; die gewählte Regel wird in `references/folder-rules.md` und der Consumer-Pipeline
  dokumentiert. (2) Reihenfolge **MD-R1 → MD-R2 → MD-R6 → MD-R7 → MD-R3 → MD-R4 → MD-R5**
  (seriell, je Paket frische Session, Tests zuerst, unabhängiges Review, ein Commit; R1 rotiert
  `classifier_revision` genau einmal — genehmigt). (3) **Full-Auto** für die gesamte
  FR-17-Session genehmigt. (4) Der reale BOKU-Verifikationslauf (3× identischer `draft`
  über die 10 Mails nach MD-R1–R4) wird vom **User** im Consumer-Workspace durchgeführt
  und ist kein Bundle-Commit-Bestandteil. (5) Hermetische Tests bauen die Pflichtfälle als
  Analoga aus den im FR dokumentierten Feldern (Message-IDs, Betreffs, Parties, SHA-256)
  — null Mailbox-Zugriffe in Tests. (6) Write-Scope-Pfade seit MD-M2: `core/quarantine/`-
  Owner; alte `core/attachment_*.py`-Pfade sind Shims. (7) Metrik-Stellen werden pro Paket
  synchron nachgezogen (128/65/55/48/804-Baseline); FR-16 konsolidiert danach.
- **FR-09:** MD-P1 — hashgebundene Approval-Receipt und read-only Promotion-Preflight (nach ausdrücklicher Human-Freigabe). Paketkarte und Abnahmebedingungen stehen in [FEATURE-REQUESTS.md](FEATURE-REQUESTS.md).
- **FR-13:** geschlossen — `MD-M1` (`T01`–`T04`) und `MD-M2` (Quarantäne-Paketierung unter
  `skills/mail-desk/scripts/core/quarantine/` mit identitätserhaltenden Legacy-Shims) sind
  abgeschlossen und paketabgenommen (siehe oben).
- **FR-16:** geplant — Dokumentations- und Metrik-Hygiene aus der MD-M2-Retrospektive
  (`DOC-M1` Metrik-SSOT, `DOC-M2` Zellen-Splitting, `DOC-M3` Stale-Reference-Fix der
  Daedalus-`memory/references/office-intelligence.md`). Reine Dokumentationsmaßnahme;
  Paketkarte in [`FEATURE-REQUESTS.md`](FEATURE-REQUESTS.md). Wird **nach FR-17** umgesetzt,
  da die MD-R-Pakete dieselben Metrik-/Map-Stellen bewegen.
- **FR-15:** abgeschlossen; `MD-E2-T01`–`T04` (standardmäßig aktive `draft`-Verdrahtung,
  `--evaluate-attachments`/`--no-evaluate-attachments`, einmalige Neuklassifikation, additive
  `DraftManifest`-Installation, fail-closed-Härtung, Revisionsdeterminismus, Idempotenz, opt-in
  `inspect`-Vorschlag und hermetische Paketabnahme) sind implementiert und paketabgenommen;
  **FR-15 ist geschlossen**. Spezifikation und Abnahme stehen in
  [`FEATURE-REQUESTS.md`](FEATURE-REQUESTS.md).
