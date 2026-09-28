# Feature Request Progress & Code Review

Diese ephemere Arbeitsdatei enthält nur den aktuellen Übergabestand zwischen
Implementierung und Review. Abgeschlossene Feature Requests stehen kompakt in
[`docs/features/_archive.md`](docs/features/_archive.md); ihre Details bleiben
über Git-Historie und Tests nachvollziehbar.

- `FR-19`/`MD-RC1` - Reconcile-Repair-Härtung abgeschlossen und archiviert
  (2026-09-24): `apply_local_repairs` korrigiert stale Index-/Log-Records nach
  frischer Verifikation (append-only `reconciled: true`, idempotent, Gates
  unverändert); `runner-progress.json` wird im Repair-Pfad deterministisch
  nachgeführt (completed/repaired). 6 Tests, Suite 1051/1051 grün; Details im
  Archiv (`docs/features/_archive.md`, FR-19-Sektion).
- `FR-20`/`MD-A3` - Anhang-Quota Inline vs. Datei abgeschlossen und archiviert
  (2026-09-24): getrennte Quoten (5 Datei / 3 Inline), neuer Reason
  `skipped_inline_limit`, fail-closed is_inline-Typvalidierung. 5 Tests
  (Env-9438-Fall, drei Zustände je Teilklasse), Suite 1056/1056 grün;
  Details im Archiv (`docs/features/_archive.md`, FR-20-Sektion).
- `TODO-Historik` - Disposition abgeschlossen (2026-09-24): Bulk-Copy überholt,
  IMAP-Pool entwertet, Runner-Modularisierung realisiert (1.147 < 1.500 Zeilen);
  konsumerspezifische Betriebsaufgaben entfernt (boku-user). Restwert als
  FR-25 (low priority, Multi-Batch-Pipelining) im Katalog; Details im Archiv
  (`docs/features/_archive.md`, Sektion Mail-Desk-Historik).
- `FR-26` - Verify/Sent-Sync/Read-Eskalations-Härtung geplant (2026-09-27,
  Befund Batch W40/1): keep-Items blockieren Verify-Completion (MD-V1),
  Sent-Sync Watermark/count/fail-closed + Telemetrie (MD-SE1/SE2),
  review_reason read_escalation_failed (MD-A5). Sequenziell A5 → SE1/SE2 → V1.
- `FR-26` - Verify/Sent-Sync/Read-Eskalations-Haertung abgeschlossen und
  archiviert (2026-09-27): keep-Items aus Evidenzpflicht (Completion regulaeber
  verify), Sent-Sync Watermark/count/fail-closed + Telemetrie-Trennung,
  review_reason read_escalation_failed. 9 Tests, Suite 1073/1073 gruen;
  Details im Archiv (`docs/features/_archive.md`, FR-26-Sektion).
- `FR-09` - Human-gated Cloud-Promotion abgeschlossen und archiviert
  (2026-09-28): MD-P1 hashgebundene Approval-Receipts + read-only Preflight
  (78 Tests), MD-P2 atomarer no-clobber Storage-Writer mit Re-Verify-before-
  Cleanup-Invariante (74 Tests), MD-P3 Cloud-Atlas-Handoff + kataloggebundener
  Refresh ueber schmalen Consumer promotion_refresh.py (36+29 Tests). 6
  dokumentierte Fix-Runden + 1 Test-Vertragsrotation, Reviews APPROVE; Suiten
  1261 + 167 gruen. Details im Archiv (`docs/features/_archive.md`,
  FR-09-Sektion).
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
- `FR-09`/`MD-P1` — **Approval-Bindung und read-only Promotion-Preflight implementiert
  und unabhängig reviewt (2026-09-27, TDD red→green, 2 dokumentierte Fix-Runden,
  Review APPROVE).** Neu:
  `skills/mail-desk/scripts/core/attachment_promotion.py` (exakt drei öffentliche
  Funktionen `compute_promotion_review_hash()`, `verify_promotion_approval_receipt()`,
  `preflight_attachment_promotion()`) und das bindende Testmodul
  `skills/mail-desk/tests/test_maildesk_attachment_promotion_mdp1.py` (78 Tests,
  genuine Red gegen das fehlende Modul: `ImportError`, Exit 1). Öffentliche Verträge:
  kanonischer Review-Payload bindet Candidate-Schema/`candidate_hash`, Quarantäne-
  Identität, Storage/`scan_dir`/Ziel und den kanonischen Filemap-Snapshot-Hash;
  Schema-1-Receipt (`receipt_type: attachment_promotion_approval`,
  `decision: approved`, `review_hash`, timezone-aware `approved_at`/`expires_at`,
  optionaler Kommentar) mit injizierbarer Uhr und fail-closed bei fehl/
  maschinell/abgelaufen/zukünftig/unplausibel-lang; deterministisches
  `attachment_promotion_preflight` Schema 1 (`ready`/`already_present`/Stopcode) ohne
  absolute Pfade. Trust-Boundaries: nur Schema-1-`attachment_filing_candidate` mit
  `status: proposed`/`promotion_status: pending_human_review` erreicht überhaupt den
  Preflight; `candidate_hash` wird kanonisch neu berechnet; Lock-Ownership kommt
  ausschließlich aus der Control Plane (eingebettete `lease_id`/`conversation_id`/
  `workspace_root`/`allow_legacy` werden ignoriert); Storage/`scan_dir`/Ziel werden aus
  aktuellem Katalog + Kandidat neu aufgelöst und gegen den Receipt geprüft; Zielpfad
  ausschließlich `workspace_root / scan_dir / target_relative_path`; absolute Pfade,
  `..`, Windows-Gerätenamen und Symlink/Junction/Reparse-Escapes stoppen; Ziel-Parent
  muss existieren; Storage-Gate per Property-Markern (`archived`/`archive: true`,
  `read_only: true`, `active: false`, `enabled: false`, `disabled: true`,
  `status` in `{inactive, disabled, archived}`) → `storage_not_writable`, fehl/
  mehrfach/abwesend → `catalog_drift`. Wiederverwendet (keine Reimplementation):
  `compute_candidate_hash`/`validate_cloud_atlas_filemap`/`resolve_catalog_storage`,
  `verify_quarantine_attachment_artifact`/`verify_workspace_lock`,
  `verify_no_tracked_quarantine`, `canonical_json_sha256`. Nachweis: fokussierte Suite
  78/78 grün, vollständige entdeckte Mail-Desk-Suite 1151/1151 grün, `compileall` und
  `git diff --check` sauber; Schreibfallen (open-write/`mkdir`/`unlink`/`replace`/
  Himalaya/atomic writer) beweisen null Mutationen auf Erfolgs- und Fehlerpfaden.
  Fix-Runden (dokumentiert): Runde 1 — real-Junction-Regression (Junction am
  unresolved Ziel-Parent → `unsafe_path`, Windows-only ungemockt) und
  Multi-Storage-Gate-Reihenfolge (kandidatgebundener Storage vor Unique-Check;
  non-Mapping → `storage_not_writable`); Runde 2 — Walk auf die `scan_dir`-Innensubtree
  begrenzt (Mount-Junction am `scan_dir` selbst bleibt legitim `ready`, Outside-Fälle
  über resolved containment) und ID-Präfix-Heuristik entfernt (Property-Marker
  entscheiden). Review: 2× REQUEST_CHANGES (1 Major + 2 Minor; 2 Minor prozessual
  orchestratorseitig) → APPROVE (0 actionable). Bewusste Grenzen:
  `references/batch-runner.md` und die System Map wurden orchestratorseitig im
  selben Arbeitsschritt synchronisiert; der shared Macht-Guard ist auf typenlose
  Human-Receipts ausgelegt, daher prüft der Promotion-Pfad die FR-09-Schema-1-Receipts
  über einen reinen Klassen-/Issuer-Probe gegen Maschinen-Receipts (fail-closed).
  Vor `MD-P2`/`MD-P3` bleibt die ausdrückliche Human-Freigabe für den mutierenden
  Cloud-Promotion-Pfad erforderlich. Commit-Kandidat:
     `feat(mail-desk): add hash-bound approval and read-only promotion preflight (MD-P1)`.
- `FR-09`/`MD-P2` — **Atomarer, idempotenter, no-clobber Storage-Writer implementiert
  und unabhängig reviewt (2026-09-27, TDD red→green, 1 dokumentierte Fix-Runde,
  Review APPROVE mit 2 orchestratorseitig geschlossenen Doku-Minors).** Neu:
  `skills/mail-desk/scripts/core/attachment_promotion.py` (klar getrennter
  Writer-Abschnitt; MD-P1-API unverändert) und das bindende Testmodul
  `skills/mail-desk/tests/test_maildesk_attachment_promotion_mdp2.py` (74 Tests,
  genuine Red gegen die fehlende Writer-API: `ImportError: cannot import name
  'PromotionJournalError'`, Exit 1). Öffentliche Writer-Verträge:
  `derive_promotion_id(review_hash, candidate_hash) -> str` (deterministisch 64-Hex),
  `load_promotion_journal(journal_path, *, expected_promotion_id/candidate_hash/
  review_hash/source_sha256/target_relative_path) -> dict` (fail-closed bei
  beschädigtem/vertauschtem Journal, gebrochener Hash-Kette, unbekannter/
  übersprungener/widersprüchlicher Phasenfolge) und `promote_attachment(candidate,
  catalogs, preflight, *, receipt, filemap, workspace_root=None, decision=None,
  lease_id=None, conversation_id=None, data_dir=None, current_time=None,
  max_filemap_age_seconds=86400, _fault_hook=None) -> dict`. Zusätzlich exportiert:
  `JOURNAL_PHASES`, `PROMOTION_RESULT_STATUSES`, `PromotionJournalError`. Journal:
  `data/mail-desk/attachment-promotions/<promotion_id>/promotion-journal.json` Schema 1,
  atomar (Sibling-Temp + `fsync` + `os.replace`), Phasen
  `approved`/`preflight_verified`/`temp_written`/`target_promoted`/`target_verified`/
  `source_cleanup_pending`/`completed` plus terminal `failed`/`recovery_required`; jede
  Phase bindet vorherigen Entry-Hash, Quell-/Zielhash, relative Pfade, Zeitstempel und
  begrenzten Fehlercode. Ergebnis: `attachment_promotion_result` Schema 1; zulässige
  Endzustände ausschließlich `promotion_completed`, `already_present_verified`,
  `source_cleanup_pending`, `collision_detected`, `recovery_required`. Trust-Boundaries:
  das MD-P1-Preflight-Envelope ist Evidenz, nicht Autorität — sein `preflight_hash` und
  alle gebundenen Felder werden revalidiert und der vollständige MD-P1-Preflight unmittelbar
  vor dem ersten Write erneut ausgeführt (Drift/Lock/Receipt/Katalog/Filemap/Quelle
  stoppen fail-closed); Ziel wird direkt vor dem Write erneut geprüft (gleicher Hash →
  idempotent `already_present_verified`, anderer Hash → `collision_detected`, kein Write);
  Promotion ausschließlich per atomarem `os.link`-No-Clobber (`EEXIST` honoriert), never
  `os.replace()`-Fallback — unsupported FS stoppt fail-closed und entfernt nur die eigene
  Temp-Datei; Temp-Sibling exklusiv erzeugt (`O_EXCL`), `fsync`, Größe/SHA-256 neu
  verifiziert; finales Ziel wird neu geöffnet, Größe/SHA-256 geprüft, Parent wo portabel
  geflusht; **Re-Verify-before-Cleanup-Invariante:** jeder Resume-/Completed-Journal-Pfad
  öffnet das reale Ziel erneut und verifiziert Größe+SHA-256 gegen die journalgebundenen
  `source_size_bytes`/`source_sha256`, BEVOR die Quarantänequelle berührt wird und BEVOR
  `promotion_completed` gemeldet wird (fehlend → `recovery_required`, anderer Hash →
  `collision_detected`, Quelle bleibt); Quarantänequelle erst nach durablem Zielnachweis
  entfernt, Inventar atomar unter dem bestehenden Inventory-Lock (`_QuarantineInventoryLock`)
  aktualisiert (Inventar-Root identisch zum MD-P1-Verifier, auch unter `data_dir`), andere
  Run-Anhänge unberührt; Cleanup-Fehler → erfolgreiche Promotion mit
  `source_cleanup_pending`; raced-same-Pfad (Crash zwischen `os.link` und Journal-Append)
  reconciliert das Journal bis `completed` statt dauerhaft `in_progress`; stale eigene
  Journal-Temp-Dateien werden bei Start/Resume entfernt; Retry aus jeder Phase ohne zweite
  Zieldatei; keine Filemap-/Katalog-/Mailbox-/Cloud-Atlas-Mutation. Wiederverwendet (keine
  Reimplementation): `compute_candidate_hash`, `compute_promotion_review_hash`/
  `verify_promotion_approval_receipt`/`preflight_attachment_promotion`,
  `_QuarantineInventoryLock`/`_load_quarantine_inventory`, `canonical_json_sha256`.
  Nachweis: fokussierte Suite 74/74 grün, MD-P1-Modul 78/78 unverändert grün,
  vollständige entdeckte Mail-Desk-Suite 1225/1225 grün (1151 Baseline + 74),
  `compileall` und `git diff --check` sauber; Fault-Injection nach jeder Journalphase
  (`_fault_hook`) beweist eindeutig reconcilierbare Zustände und Retry aus jeder Phase;
  Pflichttests umfassen Cross-Volume-Quelle (EXDEV simuliert), realer Junction-/Reparse-
  Race am Parent/Ziel (Windows, skip-if-unavailable), Abort nach
  `source_cleanup_pending`, Retry aus `failed`-Phase, `fsync`/Close-Fehler-Injection
  und literalses `errno.ENOSPC`.
  **Fix-Runde (dokumentiert):** Review REQUEST_CHANGES (1 Major + 4 Minor) → Runde 1:
  Re-Verify-before-Cleanup-Invariante (gelöschtes/beschädigtes Ziel nach Abort bei
  `target_verified`/`source_cleanup_pending`/`completed` → keine Quell-Löschung, kein
  `promotion_completed`), raced-same-Journal-Reconciliation, 6 zusätzliche
  Pflichttest-Familien, Inventar-Root-Symmetrie, Journal-Temp-Hygiene. 2 dokumentierte
  Test-Defekt-Korrekturen innerhalb der TDD-Runde (8.3-Short-Path-Mismatch im
  Temp-Detektor; fd-Wiederverwendung im fsync-Injector — mechanisch, keine
  Produktionsannahme abgeschwächt). Re-Review: alle 5 Fixes am realen Code verifiziert
  (Falsifikation inkl. Lock-Forging- und Journal-Tampering-Versuche), 0 Code-Findings;
  2 Doku-Sync-Minors (dieser Entry + System Maps) wurden orchestratorseitig im selben
  Arbeitsschritt geschlossen. Residuen (nicht blockierend): TOCTOU-Fenster zwischen
  Re-Verify und Quell-Unlock (external racer, spec-konform begrenzt); Journal bleibt bei
  externem Same-Hash-Target vor Write deterministisch `in_progress` mit sicherem
  `already_present_verified`-Retry. Commit-Kandidat:
  `feat(mail-desk): add atomic no-clobber storage writer (MD-P2)`.
- `FR-09`/`MD-P3` — **Cloud-Atlas-Refresh-Handoff und schmaler Consumer implementiert
  und unabhängig reviewt (2026-09-28, TDD red→green, 3 dokumentierte Fix-Runden,
  Review APPROVE; FR-09 damit vollständig abgeschlossen).** Neu/geändert:
  `skills/mail-desk/scripts/core/attachment_promotion.py` (neuer klar getrennter
  MD-P3-Abschnitt; MD-P1/MD-P2-API unverändert),
  `skills/cloud-atlas/scripts/promotion_refresh.py` (neuer schmaler Adapter) sowie die
  bindenden Testmodule `skills/mail-desk/tests/test_maildesk_attachment_promotion_mdp3.py`
  (36 Tests) und `skills/cloud-atlas/tests/test_promotion_refresh.py` (29 Tests). Genuine
  Red gegen die fehlenden Symbole: `ImportError: cannot import name
  'CLOUD_ATLAS_REFRESH_HANDOFF_KIND'` (Exit 1) bzw.
  `FileNotFoundError .../promotion_refresh.py` (Exit 1). Öffentliche Verträge:
  `build_cloud_atlas_refresh_handoff(promotion_result, candidate, filemap, *,
  workspace_root=None, data_dir=None, journal_path=None) -> dict` erzeugt
  `cloud_atlas_refresh_handoff` Schema 1 ausschließlich aus einem revalidierten MD-P2-Ergebnis
  (`promotion_completed`/`already_present_verified`): `result_hash` wird nachgerechnet, das
  Promotion-Journal via `load_promotion_journal` revalidiert und ist der Trust-Anchor —
  journallose `already_present_verified`-Ergebnisse werden fail-closed abgewiesen,
  `preflight_hash` wird gegen das Journal kreuzgeprüft, die Subtopic-Herleitung
  (`_bound_subtopic_id`, Spiegelbild von `resolve_catalog_storage`) wird im Journal
  persistiert und vom Builder bevorzugt; gebunden sind
  Scope (`project|topic`), katalogisierte Entity-ID, optionale Subtopic-ID, Storage-ID,
  `scan_dir`, Zielrelativpfad, Ziel-SHA-256, Größe, vorheriger Filemap-Snapshot-Hash,
  `required_receiving_steps`, `prohibited_automatic_steps` und `handoff_hash`
  (`canonical_json_sha256`); absolute Pfade, Beschreibungen und Mail-/Anhangstexte werden
  nicht übernommen (Test beweist Textfreiheit). `write_cloud_atlas_refresh_handoff(handoff,
  output_path) -> Path` persistiert optional atomar (Hash-Recheck, Sibling-Temp+`fsync`+
  `os.replace`). `compose_promotion_outcome(promotion_result, refresh_outcome=None, *,
  workspace_root=None, journal_path=None) -> dict`
  koppelt ohne Mutation und revalidiert das Journal als Anker: fehlender Adapter/Refresh-
  Fehler/Timeout/Verify-Fehler/unvollständiges
  Refresh-Ergebnis → `promotion_completed_refresh_pending` mit unveränderter `promotion_id`;
  verifizierter Refresh für dasselbe Ziel → `promotion_completed`/`refresh_completed`;
  Journal-/Ziel-/Promotion-Binding-Drift → `recovery_required`; ein Retry führt nie MD-P2 aus
  (Test patcht `promote_attachment` auf `AssertionError`, 0 Calls). Consumer:
  `consume_promotion_refresh_handoff(handoff, workspace_root=None, *, lease_id=None,
  conversation_id=None, current_time=None, data_dir=None) -> dict` (Ergebnis
  `cloud_atlas_refresh_result` Schema 1) prüft (1) eigenen Workspace-Lock
  (`workspace_lock_guard.require_workspace_lock`, `allow_legacy=False`; der Mail-Desk-Handoff
  liefert keine Cloud-Atlas-Autorisierung), (2) Handoff-Hash (pop+recompute), Promotion-Journal
  (dynamisch geladenes Mail-Desk-`load_promotion_journal`) inkl. `subtopic_id`- und
  Status-Cross-Check gegen den Journal-Anchor (`journal_drift` deny vor Engine) und reale
  Zieldatei gegen SHA-256/Größe, (3) genau den kataloggebundenen Storage (synthesized
  Fallback-Storage → `storage_unbound`; katalogfremde Entity erreicht die Engine nie) über
  die kanonischen Cloud-Atlas-Funktionen
  (`resolve_all_sync_configs` mit `storage_id`; Konvertierung nur für konvertierbare Typen via
  `convert_cloud_docs.run_conversion`, danach `gen_filemap.run_generation`; `list(configs) ==
  [storage_id]` erzwungen, nie Workspace-weit), (4) nach dem Refresh die neue Filemap über den
  MD-A5-Wrapper `validate_cloud_atlas_filemap` plus exakten Ziel-Entry-Check
  (`scan_dir/target_relative_path` mit erwartetem SHA-256) → erst dann `refresh_completed`.
  Trust-Boundaries: Handoff ist Evidenz, nicht Autorisierung; unbekannte Schlüssel, absolute/
  traversierende Pfade, `journal_relative_path`/`-hash`-Asymmetrie, Prompt-Injection-Metadaten
  und jeder Drift stoppen fail-closed ohne Filemap-/Mirror-Mutation; Binär-/Office-Inhalte
  bleiben `untrusted_external`; keine In-Place-OCR-Sonderbehandlung; die Cloud-Atlas-Kataloge
  werden vom Consumer bewusst nicht mutiert (das kanonische `last_synced_at`-Bookkeeping wird
  für den gebundenen Scan unterdrückt, da die MD-P3-Autorität nur Filemap/Mirror umfasst).
  **Fix-Runden (dokumentiert):** Runde 1 — journallose `already_present_verified`-Handoffs
  abgeschafft (Journal-Anker beidseitig; MD-P2 journaled jetzt auch den
  already-present-Pfad — dokumentierte Test-Vertragsrotation
  `test_already_present_writes_no_journal` → `test_already_present_writes_journal_trust_anchor`),
  Catalog-Origin-Proof (synthesized Fallback-Storage → `storage_unbound`, katalogfremde
  Entity erreicht die Engine nie, Baum-Snapshot null Mutation), offset-invarianter
  Frische-Check, 4 neue Pinning-Familien, Cleanups. Runde 2 —
  failed@temp_written-Retry reconciliert das Journal (nie `completed` an `failed`;
  un-retryable → `recovery_required`) statt des widersprüchlichen Shortcuts;
  `subtopic_id` aus dem Journal hergeleitet (Subtopic-Storage erreichbar);
  `compose_promotion_outcome` revalidiert das Journal (additive keyword-only Parameter).
  Runde 3 — Consumer-Cross-Check: Journal-`subtopic_id` (None-aware) und Status
  `completed` müssen zum Handoff passen, sonst `journal_drift` deny vor Engine
  (Falsifikation „tampered sub-2 vs. Journal sub-1" geschlossen). Review-Verlauf:
  REQUEST_CHANGES (1 Major + 2 Minor) → Fix 1 → REQUEST_CHANGES (1 Major + 1 Minor)
  → Fix 2 → REQUEST_CHANGES (1 falsifizierter Minor) → Fix 3 → **APPROVE (0 actionable)**.
  Nachweis: fokussierte Suiten 36/36 und 29/29 grün (inkl. hermetischem End-to-End
  Candidate → Approval → MD-P1 → MD-P2 → Handoff → Cloud-Atlas-Verify, das beweist, dass ein
  Refresh-Retry keine zweite Promotion ausführt); vollständige Mail-Desk-Suite 1261/1261 grün
  (1225 Baseline + 36), Cloud-Atlas-Suite 167/167 (138 Baseline + 29), `compileall` Exit 0,
  `git diff --check` sauber. Bewusste Grenzen/Residuen: TOCTOU-Fenster zwischen Consumer-
  Re-Hash und Engine-Scan ist inhärent und dokumentiert (fail-closed-Richtung); vollständiges
  Journal-Forging bleibt außerhalb des Trust-Modells des Consumers (unsigned Hash-Bindung,
  dokumentiert); der bekannte MDA3-Timing-Flake trat in Baseline-Läufen unter Last auf,
  Post-Change-Läufe grün. System Map und `references/batch-runner.md` wurden
  orchestratorseitig im selben Arbeitsschritt synchronisiert. Commit-Kandidat:
  `feat(cloud-atlas): consume mail-desk promotion refresh handoff (MD-P3)`.
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
- **FR-18:** abgeschlossen (2026-09-23) — **Workspace-Agnostizismus des Mail-Desk**
  im Kernel-Loop mit Subagenten umgesetzt: (MD-S1) Desk-Signals-Katalog
  `memory/references/mail-desk/mail-desk.json` (Schema 1) mit kanonischem Loader
  `load_reply_heuristics` (Defaults bei fehlender Datei, fail-loud bei Drift,
  Wortgrenzen-Predicate), Classifier konsumiert die Konfiguration; (MD-S2)
  `sent_indexer` bindet Sent-Index-Einträge an den verifizierten Batch-`account`
  (fail-loud ohne Account, vor Write/Himalaya); (MD-S3) Zoom-Recording-Routing aus
  dem Bundle entfernt, im Topic-Katalog des konsumierenden Workspace verwaltet,
  himalaya-Such-Fallback neutral. Ein Fix-Runde (MD-S1-Schema-Gate: bool/float/str
  abweisen) nach unabhängigem Review. 24 neue Tests, Suite 932/932 grün,
  Metriken 146/66/56/61/932. Paketkarte und Umsetzungsnachweis in
  [`FEATURE-REQUESTS.md`](FEATURE-REQUESTS.md).
- **FR-21:** abgeschlossen (2026-09-23) — **Desk-Signals-Doku + Workspace-Katalog-Validator**
  im Kernel-Loop mit Subagenten: (MD-S4) Pflegevertrag + Schema des Desk-Signals-Katalogs
  in SKILL.md/batch-runner.md, Literal-/Lookaround-Semantik der Subject-Patterns
  (mit Root-vs-Nested-Unterschied und Gegenbeispiel) in topic-catalog-entry, gepinnt
  durch ein Docs-Contract-Testmodul (13 Tests); (MD-S5) `catalog_validator.py`
  (read-only, kanonischer Envelope, Exit 0/1/2) validiert topics/projects/mail-desk-Kataloge,
  Root-Patterns ohne Min-3-Gate, nested mit Min-3 (35 Tests). Fix-Runde
  MD-S4-S5-fix-001 nach unabhängigem Review (Root-vs-Nested-Split: boku-user-QC war
  False Positive; owner_address-Doku auf Contract-Semantik korrigiert — Consumer ist
  FR-18-Target-Verhalten ohne heutigen Consumer). Live-Lauf gegen boku-user: valid.
  48 neue Tests, Suite 980/980 grün; Metriken 149/67/56/63/980 (L2-Kanonik).
  Paketkarte und Umsetzungsnachweis in [`FEATURE-REQUESTS.md`](FEATURE-REQUESTS.md).
- **Nachtrag B-11 / MD-R8 (2026-09-22, abgeschlossen):** Nach dem Katalog-Fix im
  Consumer-Workspace verblieb die Befundklasse „Abschluss-/Dankesmails werden
  reply-pflichtig klassifiziert" (Env 9412). **MD-R8 ist umgesetzt und
  paketabgenommen:** kanonischer Owner `core/matching/reply_heuristics.py`, Facade-
  Downgrade im `_finish`-Punkt beider Pässe (`reply_downgrade`-Provenienz,
  `rule_revision: md-r8`), 7 neue Tests, Suite 903/903 grün. **Damit ist FR-17
  vollständig geschlossen (B-1–B-11).**
- **Quote-Härtung und Live-Nachweis (2026-09-23):** Der Live-Re-Check von Env 9412
  zeigte nach MD-R8 weiterhin `needs_reply: true`, weil das Request-Gate die zitierte
  Vorgängermail mitprüfte („… wird … ergänzen"); zusätzlich leckten
  `_closing_check_*`-Felder in die Draft-Decision. Fix: `_strip_quoted_history`
  schneidet zitierte Blöcke und Header-Seperatoren vor der Bewertung ab,
  `downgrade_if_closing` nimmt den Prüftext explizit als `subject`/`body` an.
  Re-Check: `needs_reply: false` mit `reply_downgrade`; `test_reply_heuristics.py`
  um 5 Fälle erweitert, Suite 908/908 grün.

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
- **FR-22 (neu, 2026-09-23, geplant):** Identity-freier Desk-Signals-Fallback und
  Katalogisierung der BOKU-Restbestände — neutraler `reply_triggers`-Fallback plus
  owner-generierte Trigger (Schema 2, MD-ID1), sent_indexer-Domain-Liste/Stopwörter
  in den Katalog (MD-ID2, FR-18-Restlücke), internal-domain-Matching aus dem
  Katalog (MD-ID3), Spam-Gegenindikatoren (MD-ID4, Entscheidung im Paket).
  Strategie B + D per Human-Entscheidung; Paketkarte in
  [`FEATURE-REQUESTS.md`](FEATURE-REQUESTS.md) § FR-22.
- **FR-22:** abgeschlossen (2026-09-23) — **Identity-freier Desk-Signals-Fallback und
  Katalogisierung der BOKU-Restbestände** im Kernel-Loop mit Subagenten (12
  Dispatches, Red-Gates je Ticket, 1 Fix-Runde nach unabhängigem Review):
  (MD-ID1) neutraler leerer Fallback + `derive_greeting_triggers` aus
  `owner_address` (Schema 2, Schema-1-Legacy valide, 4 neue optionale Felder);
  (MD-ID2) sent_indexer-Domain-Whitelist/Stopwörter aus dem Katalog (kein
  „boku"-Literal); (MD-ID3) internal-domain-Matching aus dem Katalog
  (`GENERIC_FREEMAIL_DOMAINS` als Bundle-Konstante); (MD-ID4) Gegenindikatoren
  entfernt + `spam_sender_allowlist`-Gate. Fix-Runde: Validator-Gate für
  unbenutzbare Owner-Local-Parts (reuse `derive_greeting_triggers`). 35 neue Tests,
  Suite 1015/1015 grün; Metriken 151/67/56/65/1015 (L2-Kanonik); Live-Lauf
  boku-user valid (Schema-1-Legacy). Kein Consumer-Zwang: der bestehende
  BOKU-Katalog bleibt unverändert verwendbar; optionale Schema-2-Migration als
  Copy-Prompt dokumentiert. Paketkarte und Umsetzungsnachweis in
  [`FEATURE-REQUESTS.md`](FEATURE-REQUESTS.md).
- **FR-13:** geschlossen — `MD-M1` (`T01`–`T04`) und `MD-M2` (Quarantäne-Paketierung unter
  `skills/mail-desk/scripts/core/quarantine/` mit identitätserhaltenden Legacy-Shims) sind
  abgeschlossen und paketabgenommen (siehe oben).
- **FR-16:** abgeschlossen (2026-09-22) - `DOC-M1` Metrik-SSOT (L2-Kopfzeile kanonisch,
  Änderungscheckliste in L1 §3.1, Frische-Erhebung + Freeze-Marker für historische
  Snapshots; L2 auf 141/65/55/57/896), `DOC-M2` Zellen-Splitting (L1-mail-desk-Zelle
  2.159 → 509 Zeichen, L2-Quarantäne-Zelle 3.744 → 1.465 Zeichen; Langfassungen als
  §2.1 mit Ankern, Null-Informationsverlust), `DOC-M3` Daedalus-Referenz korrigiert
  (141/896/17 bzw. 28/138; keine „257/>489/16"-Vorkommnisse mehr in aktiven Stellen);
  `SKILL.md` nennt 17 Pflichtfelder. Verifikation: Mail-Desk 896/896, Cloud-Atlas
  138/138, compileall, `git diff --check` grün. Reine Dokumentationsmaßnahme; Paketkarte
  und Umsetzungsnachweis in [`FEATURE-REQUESTS.md`](FEATURE-REQUESTS.md).
- **FR-15:** abgeschlossen; `MD-E2-T01`–`T04` (standardmäßig aktive `draft`-Verdrahtung,
  `--evaluate-attachments`/`--no-evaluate-attachments`, einmalige Neuklassifikation, additive
  `DraftManifest`-Installation, fail-closed-Härtung, Revisionsdeterminismus, Idempotenz, opt-in
  `inspect`-Vorschlag und hermetische Paketabnahme) sind implementiert und paketabgenommen;
  **FR-15 ist geschlossen**. Spezifikation und Abnahme stehen in
  [`FEATURE-REQUESTS.md`](FEATURE-REQUESTS.md).
