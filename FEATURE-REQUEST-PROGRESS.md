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
- `FR-15` — **MD-E1 (`MD-E1-T01`–`T07`) ist vollständig implementiert, reviewt,
  verifiziert und als Paket abgenommen; `FR-15` insgesamt bleibt offen; `MD-E2-T01` und
  `MD-E2-T02` sind implementiert, `MD-E2-T03`–`T04` sind offen.** Der staged
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
  `still_ambiguous` umetikettiert), und ein deterministischer, PII-freier Run-ID je
  Nachricht erreicht im zweiten Default-Lauf MD-E1 `already_fetched` ohne Doppel-Fetch.
  `MD-E2-T03` (opt-in `inspect`-Vorschlag) und `MD-E2-T04` (Paketabnahme) sind offen.
  Fokussierter Nachweis `skills/mail-desk/tests/test_batch_runner_mde2_hardening.py` 30/30
  grün, `test_batch_runner_mde2_draft.py` 14/14 grün, `test_batch_runner_modes.py` 21/21
  grün, `test_maildesk_attachment_evaluation_mde1.py` 112/112 grün, vollständige entdeckte
  Mail-Desk-Suite 693/693 grün (aktueller Nachweis, kein permanenter Abnahmewert).
  Prozesshinweis: Die T02-Red-vor-Implementierung-Sequenz wurde für diesen Dispatch
  nicht eingehalten (Implementierung begann vor dem ersten Testlauf); der Dispatch-
  Hinweis wird im T02-Handoff-Bericht offen dokumentiert.

## Nächste Pakete nach Freigabe

- **FR-09:** `MD-P1` — hashgebundene Approval-Receipt und read-only Promotion-Preflight (nach ausdrücklicher Human-Freigabe). Paketkarte und Abnahmebedingungen stehen in [`FEATURE-REQUESTS.md`](FEATURE-REQUESTS.md).
- **FR-15:** `MD-E2` — Draft-Integration und einmalige Neuklassifikation. `MD-E2-T01` (standardmäßig aktive `draft`-Verdrahtung, `--evaluate-attachments`/`--no-evaluate-attachments`, einmalige Neuklassifikation, `DraftManifest`-Installation) und `MD-E2-T02` (fail-closed-Härtung, Revisionsdeterminismus, Idempotenz) sind implementiert; als Nächstes folgen `MD-E2-T03` (opt-in `inspect`-Vorschlag) und `MD-E2-T04` (Paketabnahme). Spezifikation und Abnahme stehen in [`FEATURE-REQUESTS.md`](FEATURE-REQUESTS.md).
