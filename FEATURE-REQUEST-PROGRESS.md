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
  verifiziert und als Paket abgenommen; `FR-15` insgesamt bleibt offen, `MD-E2` ist
  nicht gestartet.** Der staged `attachment_evaluation`-Vertrag
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
  `validate_workspace.py --json` und `git diff --check` sauber. `MD-E2` (automatische
  `draft`/`inspect`-Verdrahtung, `--evaluate-attachments`, Neuklassifikation,
  `DraftManifest`-Installation) ist **nicht gestartet** und bleibt hinter einem
  frischen, ausdrücklichen Human Gate.

## Nächste Pakete nach Freigabe

- **FR-09:** `MD-P1` — hashgebundene Approval-Receipt und read-only Promotion-Preflight (nach ausdrücklicher Human-Freigabe). Paketkarte und Abnahmebedingungen stehen in [`FEATURE-REQUESTS.md`](FEATURE-REQUESTS.md).
- **FR-15:** `MD-E2` — Draft-Integration und einmalige Neuklassifikation inklusive `draft`/`inspect`-Verdrahtung, `--evaluate-attachments` und `DraftManifest`-Installation (nach frischem, ausdrücklichem Human Gate; `MD-E1` ist abgenommen). Spezifikation und Abnahme stehen in [`FEATURE-REQUESTS.md`](FEATURE-REQUESTS.md).
