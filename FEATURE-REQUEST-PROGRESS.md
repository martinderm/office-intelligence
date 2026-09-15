# Feature Request Progress & Code Review

Diese ephemere Arbeitsdatei enthält nur den aktuellen Übergabestand zwischen
Implementierung und Review. Abgeschlossene Feature Requests stehen kompakt in
[`FEATURE-REQUEST-ARCHIVE.md`](FEATURE-REQUEST-ARCHIVE.md); ihre Details bleiben
über Git-Historie und Tests nachvollziehbar.

## Aktueller Stand

- `FR-08` ist mit `MD-A1` bis `MD-A5` vollständig umgesetzt, unabhängig reviewt,
  getestet und committed. Der Abschluss ist archiviert.
- Der read-only `attachment_filing_candidate` ist als Schema 1 eingefroren.
- `FR-09` ist noch nicht gestartet. Vor `MD-P1` bleibt die ausdrückliche Human-
  Freigabe für den mutierenden Cloud-Promotion-Pfad erforderlich.

## Nächstes Paket nach Freigabe

`FR-09 / MD-P1` — hashgebundene Approval-Receipt und read-only Promotion-
Preflight. Paketkarte und Abnahmebedingungen stehen in
[`FEATURE-REQUESTS.md`](FEATURE-REQUESTS.md).
