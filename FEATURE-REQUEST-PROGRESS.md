# Feature Request Progress & Code Review

Diese ephemere Arbeitsdatei enthält nur den aktuellen Übergabestand zwischen
Implementierung und Review. Abgeschlossene Feature Requests stehen kompakt in
[`FEATURE-REQUEST-ARCHIVE.md`](FEATURE-REQUEST-ARCHIVE.md); ihre Details bleiben
über Git-Historie und Tests nachvollziehbar.

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
- `FR-14` / `MD-C1` — Additive Coverage-Felder in Schema 1 (Handoff bis Quarantäneindex) ist zur Review bereit:
  `analysis_status: "completed"` bezeichnet ausschließlich den technischen Abschluss.
  Extraktion, Handoff, Filing-Candidate und Quarantäneindex Schema 1 transportieren die tatsächliche
  Analyseabdeckung und jede Truncation über optionale additive Coverage-Felder:
  `analysis_completeness` (`full`, `truncated`, `partial`, `unavailable`),
  `truncation_reason`, `truncation_stage` (`none`, `extraction`, `handoff_per_attachment`, `handoff_cumulative_mail`),
  `handoff_character_count`, `analysis_character_budget` und `source_character_count`.
  Die Coverage-Werte sind kryptographisch in `compute_handoff_hash()` und `compute_candidate_hash()`
  eingebunden; bei eingeschränkter Analyse weist der Filing-Candidate im `reason`-Feld transparent darauf hin.
  Der Quarantäneindex verbleibt strikt auf Schema 1 (kein Schema 2, keine Migrationen). Bestehende
  Schema-1-Einträge ohne Coverage-Felder melden bei Lookup in-memory `analysis_completeness: "unknown"`,
  bleiben jedoch auf Disk unverändert und byte-identisch.
  Review-Verifikation: 15/15 fokussierte TDD-Tests in `test_maildesk_attachment_coverage_mdc1.py` grün
  (inkl. P1 unknown-Persistierungs-Abweisung, gruppenweiser Kern-Coverage-Validierung mit optionalem `source_character_count`,
  P2 Writer-Fail-Closed bei unbekannten Feldern, ursprünglicher typisierter Handoff-Signatur ohne `*args`/`**kwargs`
  und byte-identischer Beibehaltung historischer Einträge auf Disk), 534/534 Tests der gesamten Mail-Desk-Suite grün,
  `final-location-index.json` byte-identisch.
- `FR-08` ist mit `MD-A1` bis `MD-A5` vollständig umgesetzt, unabhängig reviewt,
  getestet und committed. Der Abschluss ist archiviert.
- `FR-09` ist noch nicht gestartet. Vor `MD-P1` bleibt die ausdrückliche Human-
  Freigabe für den mutierenden Cloud-Promotion-Pfad erforderlich.

## Nächste Pakete nach Freigabe

- **FR-09:** `MD-P1` — hashgebundene Approval-Receipt und read-only Promotion-Preflight (nach ausdrücklicher Human-Freigabe). Paketkarte und Abnahmebedingungen stehen in [`FEATURE-REQUESTS.md`](FEATURE-REQUESTS.md).
