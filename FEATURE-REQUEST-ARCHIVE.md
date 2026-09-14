# Feature-Request-Archiv

Dieses Archiv enthält vollständig abgeschlossene Feature Requests. Laufende und
geplante Arbeit steht ausschließlich in [`FEATURE-REQUESTS.md`](FEATURE-REQUESTS.md).
Die ausführliche Implementierung bleibt über Git-Historie, Tests und die genannten
Codeverträge nachvollziehbar; das Archiv ist kein zweiter aktiver Backlog.

## Archivstatus

| ID | Abschluss | Kernergebnis | Nachweis |
| --- | --- | --- | --- |
| `FR-01` | ✅ | Schema v3 für Projekte, Workpackages, Tasks, Deliverables und projektweite Milestones; konservative Migration | Schema-/Validator-/Migrations-Tests und produktiver BOKU-Backfill |
| `FR-02` | ✅ | Hierarchisches Projektartefakt-Matching, Zwei-Pass-Full-Body-Eskalation und strukturierte Evidence | FR-02a–c-Regressionen und Mail-Desk-Gesamtsuite |
| `FR-03` | ✅ | Subtopics, Cloud-Sync, Operations und Events mit konservativer Signalauflösung | FR-03a, FR-03b1 und FR-03b2a–c; Katalog-/Classifier-Tests |
| `FR-04` | ✅ | Kataloggestützter Dossier-, Apply-, Synthese- und Fach-Handoff-Pfad | FR-04a–d-Contract- und Acceptance-Tests |
| `FR-05` | ✅ | Acht Batch-Runner-Handler modularisiert, CLI-Rand kompatibel gehalten | Handler-, Dispatch- und Gesamtsuite |
| `FR-06` | ✅ | Zweistufiger Post-Batch-Synthesevertrag mit Telemetrie, Targets und verifiziertem Handoff | U-1–U-5, manueller Pilot und Synthese-Handoff-Tests |
| `FR-07` | ✅ | Kontrollierter Batch-Einstieg, Workspace-Bindung, Readiness, Recovery und Completion-Gate | H0-Recovery, MD-H1–H5, Fault-Injection und BOKU-Pilot |

## FR-01 — Projektkatalog Schema v3

Das normative Schema modelliert Workpackages mit Tasks und Deliverables sowie
projektweite Milestones mit `related_wps`. Validator, Vorlagen und das
standardmäßig read-only Migrationswerkzeug sind vorhanden. Der BOKU-Katalog wurde
nach Gesamtdry-run atomar migriert; unsichere Checkpoints blieben bewusst offen,
statt IDs zu erfinden.

## FR-02 — Projektartefakte und Lesegrad

Mail-Desk erkennt nach eindeutiger Projektwahl WP-, Task-, Deliverable- und
Milestone-Signale. Exakte Codes haben Vorrang, Gleichstände bleiben Kandidaten.
Definierte Signale lösen einen zweiten Full-Body-Pass derselben Mail aus; Fehler
halten das Item in Review. Evidence nutzt nur eindeutige Decision-Scalars und
Katalogdaten, keinen Rohbody.

## FR-03 — Topic-, Subtopic-, Operations- und Event-Vertrag

Topics besitzen getrennte Subtopic-Signale, optionale katalogisierte Cloud-Syncs,
Dauerprozesse und terminierte Events. Parent-Routing bleibt stabil. Operations und
Events erhalten eigene Evidence-Pfade; Events besitzen keinen eigenen Cloud-
Speicher und dürfen nur explizit deklarierte Parent-/Subtopic-Storages referenzieren.
Mehrdeutigkeiten und nichtkanonische Pfade bleiben fail-closed.

## FR-04 — Dossier-Workflow

Der Dossier-Modus erzeugt einen katalogbasierten Inspect-Auftrag und Cloud-Atlas-
Preflight. Apply benötigt eine separate hashgebundene Review und delegiert an
Execute→Verify. Synthese und Fach-Handoff bleiben quellengebunden und reviewbar;
Mail-Desk ruft dabei weder LLM, Cloud-Atlas noch Task-Desk autonom auf.

## FR-05 — Batch-Runner-Modularisierung

`search`, `resolve`, `inspect`, `draft`, `sync_sent`, `execute`, `verify` und
`pipeline` liegen in `scripts/core/modes/`. Der Haupt-Runner behält CLI, Dispatch,
Konfiguration und Kompatibilitätsfassaden; bestehende Envelope-, Cleanup- und
Partial-Failure-Semantik blieb erhalten.

## FR-06 — Post-Batch-Projektsynthese

Execute erzeugt Telemetrie und einen nicht freigegebenen Synthese-Candidate.
Erst erfolgreicher Verify oder abgeschlossener Reconcile erzeugt den versionierten
Handoff und Completion-Report. Die inhaltliche Synthese bleibt eine LLM-geführte,
quellengebundene Laufzeitpflicht; der Python-Runner behauptet keinen Fachabschluss.

## FR-07 — Batch- und Recovery-Härtung

- `MD-H1`: Runner-Korrektheit für `skip_known`, Sent-Failures, Delete, Partial
  Failures, nullable Himalaya-Felder und fehlende Message-Header.
- `MD-H2`: Standardfluss Draft → sichtbare Review → Execute → Verify mit
  Count-, Scope- und hashgebundener Approval-Receipt.
- `MD-H3`: Backend/Account aus `.agents/mail-desk-backend.json` und begrenzter
  Readiness-Preflight vor Mutationen.
- `MD-H4`: Atomisches Recovery-Journal, read-only Reconcile und idempotenter Resume
  ohne Doppel-Copy/-Delete/-Logs/-Evidence.
- `MD-H5`: Completion- und Synthese-Gate; Partial/Abort/Verify-Fehler liefern
  `recovery_required` statt vorzeitigen Abschluss.

Der produktive BOKU-Recovery-Lauf reconciliierte bereits verschobene Nachrichten
per normalisierter Message-ID ohne erneute Mailboxmutation. Das daraus abgeleitete
Luna-Profil bleibt: drei bis fünf Mails, sichtbare starke/humane Review und lineare
Ausführung innerhalb eines Batches; frische Sessions nur zwischen unabhängigen
Batches.

## Archivierungsregel

Ein FR wird erst hierher verschoben, wenn alle seine Submodule abgeschlossen,
unabhängig reviewt, getestet und committed sind. Teilweise abgeschlossene FRs
bleiben mit ihren erledigten und offenen Submodulen im aktiven Backlog.
