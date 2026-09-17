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
- `FR-08` ist mit `MD-A1` bis `MD-A5` vollständig umgesetzt, unabhängig reviewt,
  getestet und committed. Der Abschluss ist archiviert.
- Der read-only `attachment_filing_candidate` ist als Schema 1 eingefroren.
- `FR-09` ist noch nicht gestartet. Vor `MD-P1` bleibt die ausdrückliche Human-
  Freigabe für den mutierenden Cloud-Promotion-Pfad erforderlich.

## Nächste Pakete nach Freigabe

- **FR-14:** `MD-C1` — Analysevollständigkeit und Truncation-Provenienz vom bestehenden Extraktions-/Handoff-Vertrag bis zum Quarantäneindex Schema 2. Kein Human Gate erforderlich.
- **FR-09:** `MD-P1` — hashgebundene Approval-Receipt und read-only Promotion-Preflight (nach ausdrücklicher Human-Freigabe). Paketkarte und Abnahmebedingungen stehen in [`FEATURE-REQUESTS.md`](FEATURE-REQUESTS.md).
