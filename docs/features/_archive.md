# Feature-Request-Archiv

Dieses Archiv enthält vollständig abgeschlossene Feature Requests. Laufende und
geplante Arbeit steht ausschließlich im Katalog
[`../../FEATURE-REQUESTS.md`](../../FEATURE-REQUESTS.md); die Record-Files der
aktiven FRs liegen in diesem Ordner (`docs/features/`).
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
| `FR-08` | ✅ | Manifestgebundener Anhangsfluss vom RFC-822-Inventar bis zum read-only Ablagevorschlag | MD-A1–MD-A5, 449 Mail-Desk-Gesamttests und unabhängige Reviews |
| `FR-11` | ✅ | Git-schlanke, indexierte Attachment-Quarantäne mit kontrolliertem Retain-, Promote- und Discard-Lebenszyklus | MD-Q1–MD-Q3, 519 Mail-Desk-Gesamttests und unabhängige Reviews |
| `FR-14` | ✅ | Sichtbarer, hashgebundener Vollständigkeits- und Truncation-Status analysierter Mail-Anhänge | MD-C1, 15 fokussierte und 534 Mail-Desk-Gesamttests |
| `FR-13` | ✅ | Domänenorientierte Classifier-Entflechtung: kanonisches `core/matching/`-Paket (MD-M1 T01–T04) und Quarantäne-Paketierung unter `core/quarantine/` mit identitätserhaltenden Legacy-Shims (MD-M2); 804 Tests grün | Kompatibilitätsvertrag (`core.__init__`-Re-Exports, Monkeypatch-Seams), Facade 810 Zeilen ≤ 813, einmalige `classifier_revision`-Rotation; unabhängige Paketreviews |
| `FR-15` | ✅ | Automatische Anhang-Auswertung bei unklaren Mails: MD-E1 staged `attachment_evaluation` + validierter Handoff, MD-E2 standardmäßig aktive Draft-Verdrahtung mit einmaliger `untrusted_external`-Neuklassifikation und finaler `DraftManifest`-Installation | MD-E1 (T01–T07) und MD-E2 (T01–T04) mit Red-Gates, unabhängigen Reviews und hermetischer Paketabnahme |
| `FR-16` | ✅ | Dokumentations-/Metrik-Hygiene: Metrik-SSOT (L2-Kopfzeile kanonisch, §3.1-Checkliste), Zellen-Splitting mit Null-Informationsverlust, Daedalus-Referenz korrigiert | DOC-M1/M2/M3 in einem Dokumentations-Commit; `git grep`-Verifikation der Metrik-Stellen |
| `FR-17` | ✅ | Routing-Katalogtreue (routing_priority, DNR, Newsletter-Mapping), Batch-Determinismus, MIME-Inventarkette, Inline-Bild-Policy, MD-R8-Reply-Heuristik (B-1–B-11) | MD-R1–R8 mit Red-Gates, Fix-Runden und unabhängigen Reviews; Live-Nachweis an Env 9412 (`reply_downgrade`) |
| `FR-18` | ✅ | Workspace-Agnostizismus: Desk-Signals-Katalog `mail-desk.json` (Schema 1, fail-loud), sent_indexer-Account-Bindung, Zoom-Routing im Topic-Katalog | MD-S1–S3 mit Red-Gates und unabhängigen Reviews; 1 Fix-Runde (Schema-Gate) |
| `FR-21` | ✅ | Desk-Signals-Doku (SKILL.md/batch-runner.md) + Pattern-Semantik (topic-catalog-entry) + Workspace-Katalog-Validator `catalog_validator.py` | MD-S4/MD-S5 im Kernel-Loop (Docs-Contract-Test + 35 Validator-Tests), 1 Fix-Runde Root-vs-Nested Min-3 |
| `FR-22` | ✅ | Identity-freier Desk-Signals-Fallback: neutraler leerer Fallback + owner-generierte Trigger (Schema 2 mit Schema-1-Legacy), sent_indexer/internal-domain/Spam-Gegenindikatoren → Katalog bzw. entfernt, `spam_sender_allowlist`-Gate | MD-ID1–ID4 im Kernel-Loop (12 Dispatches mit Red-Gates), 1 Fix-Runde Validator-Owner-Gate; 1015 Tests grün |
| `FR-24` | ✅ 2026-09-24 | Pflicht-Skill-Routing für Batch-Läufe: SKILL.md-Description routet Batch-Work nicht mehr weg; kanonischer Pflicht-Ladeblock + Consumer-Migrationsbaustein im Record | MD-R9 im Kernel-Loop (Doku-Contract-Test, 5 Tests); 1037 Tests grün |
| `FR-23` | ✅ 2026-09-24 | Workspace-Lock-Delegation an Runner-Subprozesse: `--workspace-lease-id`/`--workspace-conversation-id` reichen die Agent-Lease an die Anhang-Bewertung weiter; fremde/abgelaufene Leases bleiben fail-closed | MD-L1 im Kernel-Loop (8 Tests, fail-closed-Regression gepinnt); 1045 Tests grün |

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

## FR-08 — Manifestgebundene Mail-Anhänge und Ablagevorschläge

Der abgeschlossene Anhangsfluss inventarisiert ausschließlich reale RFC-822-MIME-
Parts, ruft freigegebene Anhänge review- und accountgebunden in eine lokale
Quarantäne ab, extrahiert Inhalte innerhalb fester Ressourcen- und Sicherheitsgrenzen
und erzeugt einen gegen Mailidentität, Inventar und Decision gebundenen LLM-Handoff.
Der abschließende `attachment_filing_candidate` in Schema 1 verwendet ausschließlich
Katalog- und frische Filemap-Evidenz und bleibt strikt read-only; FR-08 autorisiert
weder Cloud-Uploads noch Ordner-, Filemap- oder Katalogänderungen.

- `MD-A1`: RFC-822-MIME-Inventar und kanonische Part-Bindung (`6f86c67`).
- `MD-A2`: Reviewgebundener Quarantäne-Abruf und Lock-/Manifest-Härtung
  (`1f31629`, `9e7c7d8`).
- `MD-A3`: Begrenzte Extraktion und lokales OCR-Derivat (`8bb87b5`, `51a583b`).
- `MD-A4`: Materialitäts-Gate und integritätsgebundener LLM-Handoff
  (`a50652f`, `5eea87e`).
- `MD-A5`: Katalog-/Filemap-gestützter Ablagevorschlag und geschlossene
  Account-/Quarantäne-Vertrauensgrenzen (`286e238`, `e655b02`).

Die finale Abnahme umfasste 41 fokussierte MD-A5-Tests und 449 grüne
Mail-Desk-Gesamttests. Die eigentliche Cloud-Promotion bleibt ausschließlich
Gegenstand von FR-09 und dessen separatem Human Gate.

## FR-11 — Attachment-Quarantäne-Hygiene und Lebenszyklus

FR-11 hält Binärdateien und run-lokale Inventare als ignorierte Laufzeitdaten aus
Git heraus, führt aber alle erfolgreich analysierten und weiterhin lokal
quarantänisierten Anhänge in einem kleinen versionierten Index. Der Final Location
Index bleibt ausschließlich der Nachweis der verifizierten Mailboxposition und
enthält keine lokalen Attachment-Pfade.

- `MD-Q1` dokumentiert und testet die Git-Ignore-Grenze für Consumer-Workspaces.
- `MD-Q2` stellt den atomaren, lockgebundenen Quarantäneindex mit physischer
  Re-Verifikation, Drift-Erkennung und read-only Reconcile bereit.
- `MD-Q3` dokumentiert Entscheidungen append-only und trennt `retain`, die reine
  FR-09-Promotion-Übergabe und receiptgebundenes `discard` mit atomarem
  Recovery-Journal.

Die Abschlussabnahme umfasste 29 fokussierte MD-Q3-Tests, 55 Quarantäne-Tests und
519 grüne Mail-Desk-Gesamttests. Quarantäne-Binärdateien bleiben unversioniert;
eine tatsächliche Cloud-Promotion bleibt ausschließlich FR-09 vorbehalten.

## FR-14 — Sichtbarer Truncation-Status analysierter Anhänge

FR-14 macht sichtbar und hashgebunden, ob ein technisch abgeschlossen analysierter
Mail-Anhang vollständig, gekürzt, teilweise oder gar nicht inhaltlich ausgewertet
wurde. `MD-C1` reicht die additive Coverage-Evidenz vom Extraktions- und Handoff-
Vertrag über den Filing-Candidate bis zum Quarantäneindex Schema 1 weiter; bestehende
Einträge bleiben unverändert und erscheinen beim Lesen als `unknown`.

Die Abschlussabnahme umfasste 15 fokussierte MD-C1-Tests und 534 grüne Mail-Desk-
Gesamttests. Die Implementierung wurde mit `494f2fc` abgeschlossen; FR-14 führt
weder ein neues Schema noch Migration, Promotion oder zusätzliche Trust-
Infrastruktur ein.

---

## FR-13: Domänenorientierte Binnengliederung und Matcher-Entflechtung von mail-desk

**Status:** ✅ **Abgeschlossen** (reine Refactoring- und Modularisierungsmaßnahme ohne
Verhaltens- oder Schnittstellenänderung). **`MD-M1` ist mit T01–T04 abgeschlossen**
(kanonisches `core/matching/`, Facade auf 810 physische Zeilen kontrahiert, vollständiger
Kompatibilitätsvertrag, dokumentierte einmalige `classifier_revision`-Rotation);
**`MD-M2` ist abgeschlossen und paketabgenommen (FR-13 geschlossen).**

### Problem & Motivation

`skills/mail-desk/scripts/core/` enthält derzeit 24 Module mit über 500 KB Code in einer
völlig flachen Verzeichnisstruktur. Darin befindet sich mit `classifier.py` (**91,6 KB,
~2.000 Zeilen**) ein weiterer massiver Monolith, der Datums-Parsing, Katalog-Laden,
Projekt-Workpackage-Matching, Deliverables-, Task- und Milestone-Matching, Topic-Matching,
Mehrdeutigkeits-Filter und Manifest-Drafting in einer Datei bündelt. Zudem sind die
sicherheitskritischen Quarantäne-Dateien (MD-A/MD-Q) nicht als geschlossene Domäne
abgegrenzt.

### Ziel & Invarianten

- Strukturierung von `mail-desk/scripts/core/` in fachliche Subpakete:
  - `quarantine/` (MD-A/MD-Q Subsystem für Anhänge)
  - `matching/` (Triage- und Katalog-Abgleich-Heuristiken)
  - `transport/` (Himalaya CLI, Search by ID, Preflight)
- **100 % Abwärtskompatibilität:** Alle bestehenden CLI-Fassaden
  (`mail_desk_batch_runner.py`, `mail_desk_attachment_quarantine_index.py`, etc.) und
  alle Re-Exports in `scripts/core/__init__.py` bleiben erhalten.
- Keine Änderung an den 16 Pflichtfeldern des Quarantäneindex oder den Hash-Garantien.
- Alle >489 Tests der Mail-Desk-Suite müssen ohne Assertion-Brüche grün bleiben.

### MD-M1 — Entflechtung des 2.000-Zeilen-Monolithen classifier.py

**Scope:**
1. Erstellung des Unterpakets `skills/mail-desk/scripts/core/matching/`:
   - `date_parser.py`: Datums-Parsing (`parse_date_to_year_month`).
   - `project_matching.py`: Workpackage-, Task-, Deliverable- und Milestone-Matching
     gegen Schema v3.
   - `topic_matching.py`: Topic- und Subtopic-Matching gegen `topics.json`.
   - `ambiguity.py`: Filterung mehrdeutiger Kandidaten, Beibehaltung in INBOX bei Unklarheit.
2. `classifier.py` importiert die Sub-Matcher und bleibt als konsolidierte API-Fassade
   bestehen; Reduzierung der Dateigröße um > 60 %.

**MD-M1-Abnahme (T01–T04, abgeschlossen):** Das Unterpaket
`skills/mail-desk/scripts/core/matching/` besitzt die kanonischen Owner `date_parser.py`,
`ambiguity.py`, `project_matching.py` und `topic_matching.py`; `classifier.py` bleibt die
kompatible Facade (810 physische Zeilen, ≤ 813; Baseline 2.035) und hält Katalog-I/O,
Full-Reader-I/O, Zwei-Pass-Orchestrierung, Thread-Referenzparsing/-Parent-Lookup,
Anhangsbindung und Manifest-Drafting. Die Thread-Ordner-Inheritance und der
Full-Read-Evidenz-Rebuild sind als `match_thread_project_inheritance`/
`resolve_full_read_project_evidence` (Projekt) bzw. `match_thread_topic_inheritance`/
`resolve_full_read_topic_evidence` (Topic) an diese Owner geroutet (Reihenfolge
Projekt-vor-Topic, exakte Katalogobjekte und Entscheidungs-/Evidenz-/Notiz-/Zielsemantik
erhalten). Der Kompatibilitätsvertrag
`skills/mail-desk/tests/test_classifier_compatibility_contract.py` sichert die gesamte
Basissymbol-, Re-Export-, DI-, Lazy-Import- und Monkeypatch-Oberfläche. Der
`classifier_revision`-Fingerprint bindet weiterhin exakt die geordnete Fünf-Quellen-AST-
Menge (`classifier.py`, `matching/ambiguity.py`, `matching/date_parser.py`,
`matching/project_matching.py`, `matching/topic_matching.py`); da sich diese Quellen in
MD-M1 einmalig geändert haben, rotierten vorhandene `classifier_revision`-Werte genau
einmal (genehmigt). Git-Index-Metrik: 119 getrackte Dateien / 58 unter `scripts/`
(48 unter `scripts/core`) / 46 Testmodule / 786 Tests.

### MD-M2 — Paketierung der Quarantäne-Module unter core/quarantine/ (abgeschlossen)

**MD-M2-Abnahme:** Die sechs zusammengehörigen Quarantäne-/Anhangsmodule sind kanonisch
unter `skills/mail-desk/scripts/core/quarantine/` paketiert: `quarantine_index.py`
(umbenannt aus `attachment_quarantine_index.py`), `attachment_fetch.py`,
`attachment_extract.py`, `attachment_filing.py`, `attachment_policy.py` und
`attachment_handoff.py`. Inhalt byte-erhalten (bis auf zwei Relative-Import-Korrekturen
in `attachment_filing.py`); die alten `core/attachment_*.py`-Pfade sind dünne
`sys.modules`-aliasende Shims, die Objektidentität für Legacy-Importe, `mock.patch`-Strings,
`patch.object`-Seams und alle 33 `core.__init__`-Re-Exports garantieren; die 17 kanonischen
Schema-1-Pflichtfelder, Hash-Garantien und der `classifier_revision`-Fingerprint (bindet
weiterhin ausschließlich `classifier.py` + `matching/*`) sind unverändert. Struktureller
Nachweis: `tests/test_quarantine_package_structure.py` (Paketstruktur, Owner-Vollständigkeit,
Cloud-Atlas-Discovery vom tieferen Level) und
`tests/test_quarantine_compatibility_contract.py` (Modul-/Symbolidentität, Seam-Wirksamkeit,
Fingerprint-Quellenmenge, eingefrorenes `core.__all__`) — genuine strukturelle Red-Tests
(`ModuleNotFoundError: core.quarantine`) vor der Implementierung. Vollständige Mail-Desk-Suite
804/804 grün; Compileall, Skill-Katalog, Workspace-Validator und `git diff --check` sauber.
Git-Index-Metrik: 128 getrackte Dateien / 65 unter `scripts/` (55 unter `scripts/core`) /
48 Testmodule / 804 Tests.

**Scope:**
1. Überführung der zusammengehörigen Quarantäne- und Anhangsmodule in
   `skills/mail-desk/scripts/core/quarantine/`:
   - `attachment_quarantine_index.py`
   - `attachment_fetch.py`
   - `attachment_extract.py`
   - `attachment_filing.py`
   - `attachment_policy.py`
   - `attachment_handoff.py`
2. Bereitstellung transparenter Re-Exports in `scripts/core/` zur Garantie nahtloser
   Kompatibilität für bestehende Test-Suites und externe Konsumenten.

**Abnahme:** Vollständige Mail-Desk-Suite (>489 Tests) grün, Compileall,
`validate-skills-catalog.py` und `git diff --check` sauber.

---

## FR-15: Automatische Anhang-Auswertung bei unklaren Mails

**Status:** ✅ **FR-15 ist abgeschlossen.** **MD-E1 ist vollständig implementiert,
getestet und als Paket abgenommen**, **MD-E2 ist mit T01–T04 vollständig implementiert,
getestet und paketabgenommen (hermetischer Real-Pfad-Akzeptanztest)**. FR-08, FR-11 und FR-14
stellen die erforderlichen MIME-, Quarantäne-, Extraktions-, Handoff- und Coverage-Verträge
bereit; deren Sicherheitsgrenzen bleiben unverändert. FR-15 orchestriert diese
bestehenden Bausteine und baut keine zweite Fetch-, Extraktions- oder
Klassifikationslogik.

**Stand:** Der staged `attachment_evaluation`-Vertrag (`MD-E1` → `MD-E2`), die
Receipt-Klassen-Grenze, die Lock-Legacy-Schließung, der Tracked-Quarantäne-Preflight,
die Materialitätsbindung und die Aufräum-Verantwortung sind unten verbindlich
beschrieben. Die unter „Blockierende Sicherheitsvoraussetzungen für MD-E1" genannten
Punkte sind implementiert, getestet und mit der System Map synchronisiert und damit
Teil der abgenommenen MD-E1-Abnahme; sie waren keine separaten, aufschiebbaren
Tickets. MD-E1 endet am validierten Handoff; **Reklassifikation und
`DraftManifest`-Installation sind ausschließlich MD-E2 und in MD-E1 weder
implementiert noch behauptet.** Mit **MD-E2-T01** ist die standardmäßig aktive
`draft`-Verdrahtung, die Option `--evaluate-attachments`/`--no-evaluate-attachments`
und die einmalige Neuklassifikation samt additiver `DraftManifest`-Installation
implementiert. Mit **MD-E2-T02** ist diese Grenze fail-closed gehärtet: jede bounded
MD-E1-Fehler-/No-Op-Ursache bleibt item-lokal in Review/`INBOX` (`lock_unavailable`,
`policy_blocked`, `quota_exceeded`, `fetch_failed`, `extraction_failed`,
`handoff_invalid`), ein `ready`-Handoff wird vor der Klassifikation kanonisch gegen
Identität, Vorab-Entscheid und Anhangs-Inventar revalidiert, fortbestehende
Mehrdeutigkeit erhält `completed`/`still_ambiguous` mit `auto_evaluated` und sicheren
`files[]`, `classifier_revision` bindet zusätzlich den normalisierten AST des
Classifier-Regelmoduls, unerwartete Backend-/Programmiervertragsfehler schlagen über
`AttachmentReclassificationContractError` fail-loud fehl, und ein deterministischer,
PII-freier Run-ID je Nachricht erreicht im zweiten Default-Lauf MD-E1
`already_fetched` ohne Doppel-Fetch. Mit **MD-E2-T03** ist der opt-in `inspect`-Vorschlag
(`core/modes/inspect.py`) implementiert: `inspect` bleibt ohne Opt-in rein lesend, nur
`evaluate_attachments: true` erzeugt einen top-level, nicht ausführbaren
`manifest_proposal` über denselben Item-Flow, und eine ausführbare Batch-Manifest-Datei
entsteht weiterhin nur bei explizitem `manifest_file`. Mit **MD-E2-T04** ist die
Paketabnahme über den hermetischen Akzeptanztest
`skills/mail-desk/tests/test_batch_runner_mde2_acceptance.py` abgeschlossen: ein einziger
realer Pfad Body/Full-Read → mehrdeutig → realer `text/plain`-Anhang → genau eine
`untrusted_external`-Neuklassifikation → persistiertes Projekt-`DraftManifest` mit bounded
`attachment_evaluation` (`used_for_classification: true`, 64-Hex-`classifier_revision`
gebunden an Classifier-Regeln plus konsumierten Anhangs-Hash) bei null Mailbox-/Netzwerk-
und null Execute-/Promote-/Export-/Filing-/Dispositions-/Katalog-/Cloud-Seiteneffekten.
**FR-15 ist damit geschlossen.**

### Problem und Ziel

Der aktuelle Draft-/Classify-Pfad wertet Header und Body aus, orchestriert aber
die bereits vorhandene Anhangspipeline nicht automatisch. Dadurch bleiben Mails
mit schwachem Body trotz routing-relevanter realer Anhänge als `unknown` /
`unclassified`, mit niedriger Confidence oder `review_required` in `INBOX`. Reale
Beispiele sind eine Mail mit generischem Betreff und Arbeitsplatzbeschreibung
sowie eine Alumni-/LLL-Mail, deren Office-Anhänge erst den fachlichen Kontext
eindeutig machen.

FR-15 wertet bei unklaren Mails ausschließlich policykonforme, aus der echten
RFC-822-MIME-Struktur gebundene Anhänge innerhalb fester Quoten aus und führt den
begrenzt extrahierten Inhalt als `untrusted_external` einer zweiten
Klassifikation zu. Ein weiterhin unklares oder nicht sicher auswertbares Item
bleibt fail-closed in Review/`INBOX`. Die Auswertung verändert keine Mailbox,
promotet oder exportiert keine Datei und autorisiert keine Disposition.

### Verbindlicher Trigger

Eine automatische Auswertung ist nur zulässig, wenn nach der bestehenden
Body-/Full-Read-Klassifikation mindestens eines gilt:

- `decision.kind == "unknown"` oder `decision.id == "unclassified"`;
- `decision.confidence == "low"`;
- `decision.review_required == true`;
- eine dokumentierte `read_escalation` konnte keine eindeutige Zuordnung erzeugen;

und mindestens ein kanonisch revalidierter MIME-Part `fetch_status: "available"`
sowie `policy_status: "allowed"` besitzt und innerhalb aller Einzel- und
Gesamtquoten liegt. Caller-seitig behauptete Policy-, Fetch- oder Part-Werte sind
keine Autorität. Unklare Mails ohne geeigneten Anhang verhalten sich unverändert.

### Autorisierungs- und Sicherheitsvertrag

Der vorgeschlagene maschinelle Receipt-Pfad ist eine bewusste neue
Autorisierungsform und darf den generischen MD-A2-Review-Gate nicht in eine frei
aufrufbare Selbstfreigabe verwandeln:

- Die Evaluierungsautorisierung wird ausschließlich intern aus der
  vertrauenswürdigen Draft-Control-Plane erzeugt, nie aus Mailinhalt,
  Eingabemanifest oder einem vom Caller gelieferten Receipt übernommen. Caller,
  Mail und Manifest können die maschinelle Evaluierungsautorisierung weder liefern
  noch auswählen; ein vom Caller übergebenes Receipt wird vom MD-E1-Pfad nicht als
  Autorität akzeptiert.
- Die maschinelle Autorisierung trägt `receipt_class: "machine"` und
  `receipt_type: "attachment_auto_evaluation"` und bindet mindestens `receipt_id`,
  `request_hash` gleich dem kanonischen MD-A2-`review_hash`, `approved_at`,
  `approved_by: "mail_desk_auto_evaluator"`, Policy-Revision, Account,
  Message-ID, Folder, Envelope-ID, Part-Locator und Inventar-Hash. Ausgabe und
  Auditstatus kennzeichnen sie ausdrücklich als `auto_evaluated`.
- **Receipt-Klassen-Grenze (schmal, nicht pauschal):**
  `attachment_auto_evaluation` ist ausschließlich eine interne
  Maschinen-Autorisierung für den begrenzten MD-E1-Fetch/Evaluate-Flow. Sie ist
  als Human Approval fail-closed abzulehnen von Filing, Promotion, Export,
  Disposition und jedem nicht zum MD-E1-Flow gehörenden direkten Fetch-Pfad.
- **Keine pauschale Bruch-Migration für Human-Receipts:** Die seit FR-08
  ausgelieferte und persistierte Form typenloser menschlicher MD-A2-Receipts (ohne
  `receipt_type`) bleibt gültig und wird durch MD-E1 nicht stillschweigend
  invalidiert. MD-E1 führt daher keine pauschale Schema-Migration ein, sondern nur
  eine schmale, kontext- bzw. receipt-klassenbewusste Validierungsänderung: Der
  erwartete Receipt-Typ wird je Aufrufkontext explizit festgelegt.
  Human-Approval-Aufrufstellen weisen jede maschinelle Receipt-Klasse ab, während
  der MD-E1-Fetch/Evaluate-Pfad ausschließlich die intern erzeugte Maschinenklasse
  akzeptiert.
- **Kein Duplikat des Hash-Validators, aber Pflicht zum Klassen-Guard:** MD-E1
  implementiert keinen zweiten Hash-/Request-Validator, sondern verwendet die
  bestehenden Hash-/Struktur-Validatoren weiter. Der MD-E1-Scope meint damit „kein
  Duplikat des Hash-Validators", nicht „kein Receipt-Guard". Der schmale
  Receipt-Klassen-/Issuer-/Policy-Guard zur Durchsetzung obiger Grenze ist
  ausdrücklich Teil der MD-E1-Implementierung.
- `op_attachment_fetch()` behält seinen bestehenden expliziten Receipt- und
  Drift-Vertrag. Der neue Orchestrator darf ihn nur nach aktiver, zum ausführenden
  Harness gehörender Workspace-Lock-Prüfung aufrufen. Fehlender oder fremder Lock
  stoppt vor jedem Quarantäne-Write.
- Promotion, Export, Filing und Disposition akzeptieren diese
  Evaluierungsautorisierung niemals als Human Approval. Ihre bestehenden Human
  Gates bleiben unverändert.
- `detect_mime_and_active_content`, Extension-/MIME-Konsistenz, 15 MB je Datei,
  25 MB je Mail, maximal fünf Dateien, Pfad-Containment, Symlink-/Reparse-Schutz,
  SHA-256-Revalidierung und atomare No-Clobber-Writes bleiben fail-closed bindend.
- **Materialität:** Jeder MD-E1-Anhang, der ausgewertet wird, um die initiale
  Mehrdeutigkeit einer unklaren Mail aufzulösen, wird über die MD-A4-Materialität
  `required_for_decision` gebunden (Handoff-Erzeugung mit
  `default_materiality: "required_for_decision"`). Teilweise oder nicht verfügbare
  erforderliche Evidenz bleibt fail-closed/Review (`blocked_on_required_attachment`)
  und wird niemals stillschweigend als bloß ergänzend (`supplementary`)
  herabgestuft.
- Extraktion und Handoff verwenden die bestehenden Limits von maximal 15.000
  Zeichen je Anhang und 30.000 Zeichen je Mail einschließlich sichtbarer
  Truncation-Marker. Der Classifier erhält nur den validierten, gekapselten
  `attachment_analysis_handoff`, nie freie Extraktionsergebnisse.
- `data/mail-desk/attachments/` bleibt flüchtig und git-ignoriert. Ein bereits
  getrackter Quarantänepfad ist eine Stop-Bedingung; FR-15 verändert keine
  Consumer-`.gitignore`-Datei autonom.

#### Blockierende Sicherheitsvoraussetzungen für MD-E1 (Teil der MD-E1-Abnahme)

Diese Punkte waren keine separaten, aufschiebbaren Tickets; sie sind mit MD-E1
implementiert, getestet und mit der System Map synchronisiert, weshalb MD-E1 als
abgenommen gilt.

1. **Lock-Legacy-Bypass geschlossen:** Der frühere Drift, dass
   `attachment_fetch.verify_workspace_lock()` `WORKSPACE_LOCK_ALLOW_LEGACY`
   auswertete und `allow_legacy` an den Guard weiterreichte (und
   `attachment_extract.py` denselben Parameter führte), ist mit MD-E1 beseitigt:
   der env-basierte Legacy-Bypass ist aus den Attachment-Fetch-/Quarantäne-Pfaden
   entfernt, und weder Runtime, Caller noch Manifest können Legacy aktivieren.
   Zulässig ist ausschließlich die vertrauenswürdige Lease-/Conversation-ID aus der
   Harness-Control-Plane. Tests belegen, dass weder Env noch Parameter-/Manifest-
   Bypass einen Schreibpfad öffnen.
2. **Produktions-Preflight gegen getrackte Quarantäne implementiert:** Der frühere
   Zustand, dass die Prüfung auf getrackte Quarantänedateien nur in Tests existierte,
   ist mit MD-E1 geschlossen. Vor dem ersten Quarantäne-Write wurzelt ein
   Produktions-Preflight am vertrauenswürdigen `workspace_root` und behandelt jede
   getrackte Datei unter `data/mail-desk/attachments/` als begrenzten Stop
   (fail-closed). Der Preflight verändert `.gitignore` nicht autonom und definiert
   ein testbares, begrenztes Fail-Closed-Verhalten ohne unsichere Shell.
3. **Receipt-Klassen-Guard:** Die unter „Receipt-Klassen-Grenze" beschriebene
   schmale, kontextbewusste Validierung ist Bestandteil von MD-E1 und mit Tests für
   jeden Human-Approval-Pfad sowie den direkten/unrelated Fetch-Pfad nachgewiesen
   (implementiert).

#### Lebenszyklus und Aufräum-Verantwortung

- MD-E1 bewahrt verifizierte Quarantäne-Artefakte und das Inventar für MD-E2 auf.
  Weder MD-E1 noch MD-E2 löschen sie stillschweigend.
- Nach erfolgreicher Neuklassifikation oder terminalem Fehler bleibt das Aufräumen
  eine ausdrückliche Aktion der integrierenden Control-Plane unter aktivem Lock
  und dem bestehenden, validierten Quarantäne-/Dispositions-Lebenszyklus. FR-15
  führt keine automatische Garbage Collection ein.

### Staged Manifest-Vertrag (MD-E1 → MD-E2)

MD-E1 gibt ein kanonisches **Zwischenergebnis** `attachment_evaluation` zurück und
schreibt/behauptet **kein** final klassifiziertes Draft-Item. Im Zwischenergebnis
sind `used_for_classification` immer `false` und `classifier_revision` immer
`null`. Das Feld `status` beschreibt ausschließlich die Auswertungsstufe, niemals
das Klassifikationsergebnis. MD-E1 installiert das Zwischenergebnis nicht in ein
persistiertes `DraftManifest`-Item.

MD-E2 installiert das Feld als genau **ein additives Feld je Draft-Item** in das
`DraftManifest`, sobald der einmalige Reklassifikationsversuch einen terminalen
Ausgang erreicht hat; MD-E1 installiert es nicht. MD-E2 ist nicht Teil des
MD-E1-Implementierungsumfangs.

`used_for_classification: true` zusammen mit einem 64-Hex-`classifier_revision`
wird **nur** gesetzt, wenn kumulativ gilt: genau **eine** tatsächliche
Neuklassifikation mit dem validierten `attachment_analysis_handoff` wurde
ausgeführt **und** ihr Ergebnis ist erfolgreich und nicht mehrdeutig **und** sie
hat die gebundenen Anhangs-Eingaben tatsächlich verwendet. Das
`classifier_revision` bindet die kanonischen Klassifikationsregeln und die
tatsächlich verwendeten Input-Hashes. In allen anderen Fällen bleiben
`used_for_classification: false` und `classifier_revision: null` — auch dann, wenn
ein Reklassifikationsversuch tatsächlich stattgefunden hat. Das umfasst
ausdrücklich `not_needed`, `skipped`, `failed`, `still_ambiguous` und „kein
Versuch". Ein ausgeführter Klassifikationsaufruf allein führt niemals zu `true`.

MD-E1-Zwischenergebnis (staged, `used_for_classification` immer `false`):

> **Notation:** Pipe-getrennte Werte wie `completed|not_needed|skipped|failed`,
> `auto_evaluated|not_applicable` oder `full|truncated` sind Notation für sich
> gegenseitig ausschließende Alternativen und **niemals** literale Laufzeitwerte.
> In spitzen Klammern gesetzte Werte wie `<64-hex>` oder `<safe-run-id>` sind
> Platzhalter, keine Literale.

```json
{
  "attachment_evaluation": {
    "status": "completed|not_needed|skipped|failed",
    "reason": "bounded_machine_code",
    "authorization": "auto_evaluated|not_applicable",
    "files": [
      {
        "filename": "source.docx",
        "sha256": "<64-hex>",
        "mime_type": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "chars": 15000,
        "coverage": "full|truncated",
        "run_id": "<safe-run-id>"
      }
    ],
    "used_for_classification": false,
    "classifier_revision": null
  }
}
```

MD-E2-finales Draft-Item (illustrativ, nur bei erfolgreicher, nicht mehrdeutiger Neuklassifikation nach genau einem Versuch):

```json
{
  "attachment_evaluation": {
    "status": "completed",
    "reason": "classification_clear",
    "authorization": "auto_evaluated",
    "files": ["... unverändert aus dem MD-E1-Zwischenergebnis ..."],
    "used_for_classification": true,
    "classifier_revision": "<64-hex>"
  }
}
```

`reason` verwendet eine begrenzte, dokumentierte Wertemenge, mindestens
`classification_clear`, `no_attachments`, `no_allowed_attachments`,
`lock_unavailable`, `policy_blocked`, `quota_exceeded`, `fetch_failed`,
`extraction_failed`, `handoff_invalid` und `still_ambiguous`. Langlebige Outputs
enthalten keine absoluten Pfade oder Rohinhalte. `classifier_revision` bindet die
kanonischen Klassifikationsregeln und tatsächlich verwendeten Input-Hashes; eine
freie Versionszeichenfolge genügt nicht.

**Status für fortbestehende Mehrdeutigkeit (verbindlich):** Die Paketkarte
definiert keinen eigenen Status für `still_ambiguous`. Es wird kein neuer Status
eingeführt; `still_ambiguous` verwendet den bestehenden Status `completed`
(`status: "completed"`, `reason: "still_ambiguous"`). `used_for_classification`
bleibt dabei `false` und `classifier_revision` `null`.

### MD-E1 — Policygebundener Evaluierungs-Orchestrator (✅ abgenommen)

**Status:** ✅ Implementiert, getestet und als Paket abgenommen (FR-15/MD-E1-T01–T07);
Evidenz in [`../../FEATURE-REQUEST-PROGRESS.md`](../../FEATURE-REQUEST-PROGRESS.md). MD-E1 endet am
validierten Handoff; Reklassifikation und `DraftManifest`-Installation gehören ausschließlich
zu MD-E2.

**Ziel:** Einen schmalen, separat testbaren `attachment_evaluate`-Orchestrator
bereitstellen, der reale MIME-Kandidaten revalidiert, die interne
Evaluierungsautorisierung deterministisch erzeugt und die bestehenden MD-A2/A3/A4-
Bausteine linear ausführt.

**Scope:** Bestehende öffentliche Funktionen aus `attachment_fetch.py`,
`attachment_policy.py`, `attachments.py`, `attachment_extract.py` und
`attachment_handoff.py` wiederverwenden. Kein eigener Downloader, MIME-Parser,
Office-Konverter, OCR-Pfad und kein zweiter Hash-/Request-Validator (Duplikat der
bestehenden Validierung). Ein schmaler, kontext-/receipt-klassenbewusster
Receipt-Guard ist dagegen ausdrücklich Teil des Scopes, weil er die unter
„Receipt-Klassen-Grenze" geforderte Autorisierungsgrenze durchsetzt. Der
Orchestrator liefert das kanonische, staged `attachment_evaluation`
(`used_for_classification: false`, `classifier_revision: null`) und den validierten
`attachment_analysis_handoff`; er klassifiziert noch nicht, installiert das Feld
nicht in ein persistiertes Draft-Item und führt keine Mailbox-, Evidence-,
Katalog-, Cloud- oder Dispositionsmutation aus.

**Blockierende Sicherheitsvoraussetzungen (Teil der MD-E1-Abnahme):** Die drei
unter „Blockierende Sicherheitsvoraussetzungen für MD-E1" genannten Punkte —
Lock-Legacy-Bypass-Schließung, Produktions-Preflight gegen getrackte Quarantäne und
der Receipt-Klassen-Guard — sind mit MD-E1 implementiert und getestet; MD-E1 ist
damit abgenommen. Sie waren keine separaten, aufschiebbaren Tickets.

**Pflichttests:** Trigger-Matrix; echte Part-Bindung; caller-seitig gefälschte
Policy-/Receipt-Werte; fehlender/fremder Lock; aktiver Inhalt; disallowed Extension;
MIME-/Extension-Drift; Einzel-/Gesamtgröße und Anzahl; idempotentes
`already_fetched`; Hash-/Inventar-/Identity-Drift; `.docx`, `.doc`, `.pdf`, `.xlsx`
und `.pptx`; Extraktions-Timeout; 15.000-/30.000-Zeichenbudgets und `truncated`;
keine Mailbox-, Promotion-, Export- oder Dispositionsoperation. Zusätzlich:

- Env- und Parameter-/Manifest-Legacy-Bypass öffnen keinen Schreibpfad;
- der Produktions-Preflight stoppt bei einer getrackten Datei unter
  `data/mail-desk/attachments/` vor dem ersten Quarantäne-Write und verändert
  `.gitignore` nicht;
- Human-Approval-Pfade (Filing, Promotion, Export, Disposition) lehnen die
  maschinelle Receipt-Klasse fail-closed ab, während typenlose menschliche
  MD-A2-Receipts unverändert funktionieren;
- der MD-E1-Pfad akzeptiert kein vom Caller/Mail/Manifest geliefertes maschinelles
  Receipt;
- das staged `attachment_evaluation` hat immer `used_for_classification: false`
  und `classifier_revision: null`;
- ausgewertete Anhänge sind über Materialität `required_for_decision` gebunden,
  und teilweise/nicht verfügbare erforderliche Evidenz bleibt fail-closed/Review.

**Abnahme:** Fokussierte MD-E1-Tests, vollständige Mail-Desk-Suite, Compileall,
Skill-/Workspace-Validierung und `git diff --check` grün. Nachweis, dass
(a) der staged Contract eingehalten wird, (b) die drei
Sicherheitsvoraussetzungen implementiert und getestet sind und (c) MD-E1 null
Mailbox-, Promotion-, Export- und Dispositionswrites ausführt. Paket, Progress-
und System-Map-Update werden gemeinsam committed.

### MD-E2 — Draft-Integration und Neuklassifikation

**Status:** ✅ Abgeschlossen; **MD-E2-T01–T04 implementiert und paketabgenommen**
(standardmäßig aktive `draft`-Verdrahtung, `--evaluate-attachments`/`--no-evaluate-attachments`,
einmalige Neuklassifikation, additive `DraftManifest`-Installation; fail-closed-Outcome-Matrix,
kanonische Handoff-Revalidierung, Code-/Katalog-/Hash-gebundene
`classifier_revision`, `still_ambiguous`-Erhalt und deterministische
`already_fetched`-Idempotenz; opt-in `inspect`-`manifest_proposal` über denselben
Item-Flow mit `manifest_file`-Grenze; T04 hermetischer Real-Pfad-Akzeptanztest). **FR-15 ist
damit geschlossen.**

**Ziel:** Nach der bestehenden Body-/Full-Read-Klassifikation nur unklare Items
über MD-E1 anreichern und exakt einmal mit dem validierten Anhangs-Handoff erneut
klassifizieren.

**Staged Übergang aus MD-E1:** MD-E2 übernimmt das staged `attachment_evaluation`
aus MD-E1 und installiert es als genau ein additives Feld je Draft-Item in das
`DraftManifest`. `used_for_classification: true` und ein 64-Hex-`classifier_revision`
setzt MD-E2 **nur** unter den oben im staged Manifest-Vertrag definierten
kumulativen Bedingungen: genau eine tatsächliche Neuklassifikation mit validiertem
Handoff, erfolgreiches und nicht mehrdeutiges Ergebnis und tatsächliche Verwendung
der gebundenen Eingaben. Für `not_needed`, `skipped`, `failed`, `still_ambiguous`,
„keine Neuklassifikation" und jeden erfolglosen oder mehrdeutigen Versuch bleiben
beide Felder `false`/`null`. MD-E2 ist **nicht** Teil des
MD-E1-Implementierungsumfangs; die Umsetzung erfolgt in einem eigenen, frisch
freigegebenen Paket nach grünem MD-E1.

**Aufräum-Verantwortung:** MD-E2 bewahrt verifizierte Quarantäne-Artefakte und
Inventar für einen späteren expliziten Cleanup auf. Ein Aufräumen ist eine
ausdrückliche Aktion der integrierenden Control-Plane unter aktivem Lock und folgt
dem bestehenden validierten Quarantäne-/Dispositions-Lebenszyklus. Weder MD-E1 noch
MD-E2 führen eine automatische Garbage Collection ein.

**Scope:** `draft` erhält die Auswertung standardmäßig aktiviert; eine explizite
CLI-Option `--evaluate-attachments` und ihr sicherer Deaktivierungsgegenpart werden
im Paket spezifiziert, ohne bestehende autonome `pipeline`-Freigaben auszuweiten.
`inspect` darf die gleiche Auswertung optional anbieten. Die zweite Klassifikation
darf `kind`, `id`, kataloggebundene Unterentscheidungen und `needs_reply` nur über
die bestehenden Classifier-Regeln neu bestimmen. Sie erfindet keine Ziele. Bei
Fehler, Teildeckung ohne ausreichende Evidenz oder fortbestehender Mehrdeutigkeit
bleiben `keep_in_folder`, `review_required: true` und niedrige Confidence erhalten.
Eine eindeutige Entscheidung erzeugt Evidence erst im bestehenden nachgelagerten,
quellengebundenen Flow; die Evaluierung selbst schreibt keine Evidence.

**Pflichttests:** Routing-relevanter Anhang macht ein unklares Item eindeutig;
unklare Mail ohne Anhang bleibt unverändert in Review; blockierter, zu großer oder
aktiver Anhang bleibt fail-closed; kein Mailbox-Write; zweiter Lauf erzeugt keinen
Doppel-Fetch; Truncation bleibt sichtbar; Anhangstext bleibt gekapseltes
`untrusted_external`; manipuliertes Handoff stoppt; Batch-Mischfall isoliert Fehler
auf das betroffene Item; bestehender klarer Draft wird nicht ausgewertet.

### Dokumentation und Abnahme

Die Dokumentation ist mit der **abgenommenen MD-E1-Implementierung** synchron
nachgezogen. Der frühere Spezifikations-Commit hatte ausschließlich den damaligen
Implementierungsdrift in der Mail-Desk-Subsystem-System-Map bereinigt und den
MD-E1-Blocker markiert; er dokumentierte bewusst noch **keine** MD-E1-Laufzeit als
bereits implementiert. Mit der MD-E1-Abnahme sind synchron aktualisiert:

- `skills/mail-desk/SKILL.md`: `attachment_evaluate`-Seam in Kernfluss Schritt 3 und
  die MD-E2-Grenze für `draft`/`inspect`-Verdrahtung und `--evaluate-attachments`;
- `skills/mail-desk/references/batch-runner.md`: `attachment_evaluation`-Schema,
  Beispiele und die explizite MD-E2-Abgrenzung;
- `skills/mail-desk/references/cli-operations.md`: kanonischer Ablauf
  Inspect → policygebundener Fetch → begrenzte Extraktion → validierter Handoff
  (Ende MD-E1) mit bounded Fehlern und MD-E2-Grenze;
- `skills/mail-desk/references/backends/himalaya.md`: Raw-MIME-/Fetch-Grenze,
  Lock-/Preflight-/Policy-Kontrollen, Extraktions-Timeout → `extraction_failed`
  und fehlender automatischer Retry/Reclassify/Write;
- L2 `skills/mail-desk/docs/system-map/{README,objects,processes,effects}.md` und
  L1 `docs/system-map/{README,objects,processes,effects}.md`: automatische
  Evaluierung, neue Autorisierungsgrenze und MD-E1-Paketabnahme;
- diese Statuszeile, die Paketkarten und `FEATURE-REQUEST-PROGRESS.md`.

**Gesamtabnahme MD-E1:** Alle oben genannten Verhaltens- und Sicherheitstests, die
vollständige entdeckte Mail-Desk-Suite, Compileall, Skill-/Workspace-Validierung und
`git diff --check` sind grün. Ein hermetischer End-to-End-Test beweist
Inspect → policygebundenen Fetch → begrenzte Extraktion → validierten Handoff für
einen klarstellenden Anhang und zugleich null Mailbox-, Promotion-, Export- und
Dispositionswrites. **MD-E1 endet am Handoff; Reclassify und
`DraftManifest`-Installation sind ausschließlich MD-E2 und werden von MD-E1 weder
ausgeführt noch behauptet.** FR-15 ist mit der abgenommenen
MD-E2-Umsetzung (T01–T04) geschlossen. FR-15 und FR-13 verändern dieselben
Classifier-/Attachment-Grenzen und dürfen nicht parallel umgesetzt werden; **MD-E2 wurde
zuerst abgeschlossen (FR-15 geschlossen), danach wurde FR-13/`MD-M1` (T01–T04) gegen diesen
abgenommenen MD-E2-Baseline implementiert und paketabgenommen; danach wurde
`MD-M2` (Quarantäne-Paketierung unter `core/quarantine/`) implementiert und
paketabgenommen.** **FR-13 ist geschlossen.**

**T02/T03-Synchronisation:** Die fail-closed-Härtung (MD-E2-T02) wurde im selben
Arbeitsschritt in `skills/mail-desk/scripts/core/attachment_reclassification.py`,
den fokussierten Tests (`test_batch_runner_mde2_hardening.py`,
`test_batch_runner_mde2_draft.py` mit kanonischen Fixtures in `mde2_fixtures.py`),
den Operator-Docs (`SKILL.md`, `references/batch-runner.md`,
`references/cli-operations.md`, `references/backends/himalaya.md`), beiden
System-Map-Ebenen und dieser Status-/Fortschrittsdatei nachgezogen. Der opt-in
`inspect`-Vorschlag (MD-E2-T03) wurde synchron in
`skills/mail-desk/scripts/core/modes/inspect.py` und dem neuen fokussierten Testmodul
`test_batch_runner_mde2_inspect.py` (12 Tests) sowie denselben Operator-Docs, beiden
System-Map-Ebenen und dieser Status-/Fortschrittsdatei nachgezogen. Die Paketabnahme
(MD-E2-T04) wurde über das neue fokussierte Testmodul
`skills/mail-desk/tests/test_batch_runner_mde2_acceptance.py` (1 Test) und dieselben
Operator-Docs, beide System-Map-Ebenen sowie diese Status-/Fortschrittsdatei synchron
nachgezogen; die Git-Index-Metrik ist auf 111 getrackte Dateien / 54 unter `scripts/`
(43 unter `scripts/core`) / 42 Testmodule / 706 Tests aktualisiert. **FR-15 ist geschlossen;
FR-13 ist mit MD-M1 (T01–T04) und MD-M2 (Quarantäne-Paketierung) abgeschlossen und geschlossen.**

---

## FR-16: Dokumentations- und Metrik-Hygiene aus der MD-M2-Retrospektive

**Status:** ✅ Abgeschlossen (2026-09-22, Pakete DOC-M1/DOC-M2/DOC-M3 in einem
Dokumentations-Commit; Orchestrator-Direktumsetzung, da reine Dokumentationsmaßnahme
ohne Produktionscode). Quelle: Retrospektive des Runs
`daedalus/runs/2026-09-22-office-intelligence-fr13-md-m2` (FR-13/MD-M2).

**Umsetzungsnachweis:** DOC-M1 — Metrik-SSOT über L2-Kopfzeile mit
Änderungscheckliste in L1 §3.1 (inkl. Frische-Erhebungsregel und Freeze-Markern
für historische Ledger-Snapshots); L2-Kopfzeile auf 141/65/55/57/896 aktualisiert.
DOC-M2 — L1-mail-desk-Zelle (2.159 → 509 Zeichen) und L2-Quarantäne-Zelle
(3.744 → 1.465 Zeichen) gekappt; Langfassungen als §2.1 mit Ankern, null
Informationsverlust. DOC-M3 — Daedalus-`memory/references/office-intelligence.md`
zeigt 141/896/17 bzw. 28/138; `git grep` belegt keine „257/>489/16"-Vorkommnisse
mehr in den aktiven Dokumentstellen. `SKILL.md` nennt 17 Pflichtfelder.
Verifikation: Mail-Desk-Suite 896/896 grün, Cloud-Atlas 138/138 grün, compileall,
`git diff --check` sauber.

**Planungsupdate (2026-09-22, nach FR-17):** Die ursprüngliche Spezifikation
nannte als Zielmetrik „128 Dateien, 804 Tests, 17 Pflichtfelder" (Stand nach
MD-M2). FR-17 hat seither 9 Produktions-/Testdateien ergänzt und die Suite auf
896 Tests gebracht. FR-16 setzt daher die **zum Umsetzungszeitpunkt reale
Git-Index-Metrik** als kanonischen Wert ein (per `git ls-files` vor jedem
Dokumentations-Commit frisch erhoben): aktuell **141 getrackte Dateien
(mail-desk), 65 unter `scripts/` (55 unter `scripts/core`, 6 Quarantäne-Owner),
57 Testmodule, 896 Tests**. Die DOC-M1-Checkliste verpflichtet künftig auf
Frische-Erhebung statt auf eingefrorene Literale; historische Snapshots in
Ledgern bleiben als bewusst eingefrorene Abnahmewerte gekennzeichnet.

### Problem & Motivation

Während der MD-M2-Paketierung offenbarte die System-Map- und Ledger-Pflege drei
systematische Dokumentationsrisiken, die Fehlversuche und Copy-Paste-Fehler
verursachen:

1. **Metrik-Vervielfältigung ohne SSOT:** Die Git-Index-Metrik (Dateien,
   `scripts/`-Anzahl, `scripts/core`-Anzahl, Testmodule, Testanzahl; aktuell
   128/65/55/48/804) steht in mindestens sechs Dateien (L1 `docs/system-map/README.md`
   §2, L2 `skills/mail-desk/docs/system-map/README.md` Kopfzeile, beide FR-Ledger,
   Progress-Datei). Jede Änderung ist manuelles Copy-Paste-Roulette über alle Stellen;
   im MD-M2-Run mussten sechs Stellen einzeln gefunden und synchronisiert werden, ohne
   Checkliste.
2. **Monolithische Tabellenzellen:** Die L1/L2-README-Komplexitätsmatrizen enthalten
   Zellen mit >2.000 Zeichen (L1 §2 mail-desk-Zelle, L2 §2 Quarantäne-Engine-Zelle).
   Sie widersprechen dem eigenen Anti-Context-Bloat-Ziel der System Map, sind mit
   Editier-Tools kaum atomar editierbar (im MD-M2-Run vier fehlgeschlagene Edit-Versuche
   an einer einzigen Zeile) und verschleiern die Struktur (Verantwortlichkeiten als
   Fließtext-Wand statt zitierbarer Sub-Abschnitte).
3. **Bekannter Stale-Verweis im Konsumenten-Workspace:** Daedalus'
   `memory/references/office-intelligence.md` (föderierte Referenz auf dieses Bundle)
   nennt weiterhin „257 Dateien, >489 Tests, 16 Pflichtfelder" für den Mail-Desk;
   real sind es 128 Dateien, 804 Tests, 17 Pflichtfelder. Der Verweis wurde von zwei
   unabhängigen Subagenten-Reviews als driftend gemeldet, aber nie korrigiert. Ein
   Konsumenten-Workspace, der von dieser Datei ausgeht, trifft Fehlentscheidungen.

### Ziel & Invarianten

- **DOC-M1 — Metrik-SSOT:** Die Git-Index-Metrik erhält eine einzige kanonische
  Definition pro Ebene (L1-Kopfzeile referenziert die L2-Kopfzeile oder umgekehrt;
  Ledger referenzieren „siehe System Map" statt Literalwerte, außer in
  paketabnahme-spezifischen historischen Snapshots, die bewusst eingefroren sind).
  Dazu eine Checkliste aller Metrik-Stellen (mindestens: L1 README §2, L2 README
  Kopfzeile, `FEATURE-REQUESTS.md`-Katalog-Statuszeilen, `FEATURE-REQUEST-PROGRESS.md`) im
  L1-README §3 (Navigationsmatrix), damit jede künftige Metrik-Änderung deterministisch
  nachvollziehbar ist.
- **DOC-M2 — Zellen-Splitting:** Die >2.000-Zeichen-Zellen der L1- und L2-Komplexitätsmatrizen
  werden gekappt: Die Tabellenzelle behält maximal einen Kurzstatus (≤ 200 Zeichen) plus
  Link auf einen eigenen Sub-Abschnitt im selben Dokument, der die Verantwortlichkeiten
  strukturiert (ggf. eigene Liste statt Fließtext). Keine Informationslöschung —
  Umstrukturierung mit identischem Informationsgehalt, zitierfähigen Ankern.
- **DOC-M3 — Stale-Reference-Fix:** Korrektur der Daedalus-seitigen
  `memory/references/office-intelligence.md` auf die realen Werte
  (128/804/17); da diese Datei außerhalb dieses Repositories liegt, wird sie als
  Orchestrator-Aktion im Daedalus-Workspace durchgeführt und hier nur als erledigt
  vermerkt (Föderations-Disziplin: Quell-Map ist SSOT, Referenz folgt).

### Abnahme

- Alle Metrik-Stellen zeigen identische Werte; die Stellen-Checkliste existiert und
  deckt alle Vorkommnisse ab (per `git grep` über die Zahlen nachweisbar).
- Keine Tabellenzelle der L1/L2-Komplexitätsmatrizen überschreitet ~200 Zeichen
  Status plus Link.
- Die Daedalus-Referenz zeigt 128/804/17; nachweislich keine „257/>489/16"-Vorkommnisse
  mehr.
- Dokumentations-Validierung, Skill-Katalog, Workspace-Validator und
  `git diff --check` grün; keine Code- oder Teständerungen.

### Out of Scope

- Jede inhaltliche Neuschreibung von Verantwortlichkeitsbeschreibungen.
- Änderungen an Test- oder Produktionscode.
- Automatische Metrik-Generierung per Skript (möglicher künftiger FR).

---

## FR-17: Routing-Katalogtreue, Batch-Determinismus und Vertragshygiene

**Status:** ⬜ Geplant. Ergebnis einer produktiven Testbatch des konsumierenden
BOKU-Workspace (10 Mails, Envelope 9387–9404, Account `BOKU-MARTIN`, 2026-09-22).
Alle Befunde wurden mit drei identischen `draft`-Läufen und zusätzlich offline
gegen die echten Kataloge reproduziert. Es ist eine **verhaltensändernde
Korrektur im Classifier-/Routing-Pfad** plus begleitende Vertrags- und
Hygiene-Fixes. Keine Mailbox-Mutation, keine Promotion, kein Cloud-/Task-Pfad.

### Reproduktionsumfeld

- Konsumierender Workspace mit Backend-Bindung `.agents/mail-desk-backend.json`
  (`schema_version: 1`, `backend: himalaya`, `account: BOKU-MARTIN`), also der
  kanonische MD-H3-Pfad; `--account BOKU-MARTIN` stimmte exakt überein.
- Aufruf (dreimal identisch, jeweils mit gültigem Workspace-Lock und
  `WORKSPACE_LOCK_LEASE_ID` aus der Harness-Control-Plane):
  `mail_desk_batch_runner.py --draft 10 --order oldest --folder INBOX --skip-known
  --data-dir data/mail-desk --account BOKU-MARTIN`.
- Wirkung: ausschließlich read-only Mailboxzugriffe plus zwei MD-E1-Quarantäne-Fetches;
  **keine** Mailbox-, Index-, Log-, Evidence- oder Execute-Mutation.
- Kandidaten: Envelope 9387–9393 und 9400/9403/9404 aus `INBOX` (chronologisch,
  `skip_known` übersprang bereits indizierte ältere Mails).

### Befunde

| ID | Schwere | Befund | Kern-Evidenz |
|---|---|---|---|
| B-1 | hoch | `routing_priority` ist ein toter Katalogwert; Katalogreihenfolge entscheidet, ein reiner Kontakt-Treffer schlägt einen Exaktcode im Betreff | Env 9388 → `usage-ng` statt `atael`, offline reproduziert |
| B-2 | hoch | Topic-`do_not_route_if` wird nie ausgewertet; Newsletter landen trotz Ausschluss im Topic | Env 9400 → `netzwerke`/`eu-projekte-und-oead`, offline reproduziert |
| B-3 | hoch | `draft` ist unter transienten MIME-Timeouts nicht deterministisch; Outcome kippt zwischen Klassifikation und Review+Fetch | 3 Läufe, 2 verschiedene Manifeste (9392/9393 kippen) |
| B-4 | mittel | `notes` und Anhangs-Felder widersprechen der finalen Entscheidung | `unknown` + Notes „Themenbezogene Zuordnung…“ + leeres Inventar + befüllte `files[]` |
| B-5 | mittel | Automatische Auswertung fetcht Inline-Signaturbilder als `required_for_decision` | 2 Fetches mit `is_inline: true`, `chars: 0`, beide `still_ambiguous` |
| B-6 | niedrig | `keep_in_folder` fehlt in der dokumentierten `action.type`-Menge | produktiv genutzt an 6 Code-Stellen |
| B-7 | niedrig | `progress_*.tmp` bleiben als untracked Rauschen liegen | 2 Dateien nach 3 Läufen, nicht git-ignoriert |
| B-8 | hoch | Client-Suche ohne Ordnerliste hat keinen begrenzten Gesamt-Timeout; Hänger blockiert danach ~10 min auch Einzelabfragen | Suche >5 min ohne Fehler; danach `himalaya_timeout` bei `envelope list -s 1` |
| B-9 | mittel | Standalone-`verify` kann bei gemischten Batches (Projekt/Topic + Archiv) den Handoff nicht freigeben | zwei reale Läufe: Scope 10 und Scope 9 beide `not_required` |
| B-10 | mittel | `verify`-Evidence-Fallback globt `memory/references/**/evidence` (im Consumer leer) → `in_evidence: None`, Evidence wird nicht geprüft | 10/10 „consistent“ bei 0 gefundenen Evidence-Dateien |
| B-11 | mittel | Abschluss-/Dankesmails werden als reply-pflichtig klassifiziert und landen im `_Needs-Reply`-Ordner, obwohl die Mail keine Frage/Bitte/Frist enthält | Env 9412 (Dankes-Abschluss) → `needs_reply: true` |

#### B-1 — `routing_priority` ist wirkungslos; Katalogreihenfolge und Kontakttreffer dominieren

- `routing_priority` wird in `skills/mail-desk/scripts/**` **nirgends** gelesen
  (Ripgrep über den gesamten Skriptbaum: 0 Treffer), obwohl beide Kataloge das
  Feld pflegen (`projects.json`: `week` 80, `li4lam` 75, `atael` 65, `rellde` 65,
  `meshe`/`evolve`/`usage-ng` 50, abgeschlossene 40; `topics.json`:
  `netzwerke` 60).
- `core/matching/project_matching.py::select_project_match` iteriert die
  **Katalogreihenfolge** und gibt den ersten Treffer zurück. Stufe 1a (ID/`kuerzel`/
  Alias im Betreff) wäre für `atael` erfolgreich (Betreff-Token `ATAEL`), wird aber
  nie erreicht, weil `usage-ng` an Katalogindex 3 vor `atael` (Index 5) steht und in
  Stufe 1c bereits über einen reinen Kontakt-Treffer mit `confidence: high` gewinnt.
- Offline-Reproduktion mit den echten Katalogen (ohne Mailboxzugriff, nur
  `memory/references/{projects,topics}/*.json` des Consumer-Workspace):
  - Betreff `WG: For Action - ATAEL - 101323118 - GAP-101323118 - Evaluation results  FYI`,
    `parties` enthält `sybille.michaelis@tum.de` → `{"id": "usage-ng", "confidence": "high"}`.
  - Identischer Betreff **ohne** TUM-Kontakt → `{"id": "atael", "confidence": "high",
    "folder": "Projekte/In Ausarbeitung/ATAEL"}`.
- Reale Folge: Env 9388 (`message_id: 52270af9e92f47e2a4c9a2a124fa6266@tum.de`) wurde als
  `decision.kind: project`, `id: usage-ng` gedraftet, obwohl Betreff, Anhänge
  (`Rejection decision Information Letter.pdf`, sha256
  `932f9e6f9bb9ce30ca48b6a193336f1397c34e1f378eec771d5a11b657f68818`;
  `101323118_ATAEL_ESR.pdf`, sha256
  `be92b098a8263fff4697f6a23b0c880f3363aaa51c697deeac3b18ff12057717`)
  und Katalog eindeutig `atael` sind.
- Sibling-Split: Env 9387 (`message_id:
  734474456.1162619.1784818593223@wlldb00951.cc.cec.eu.int`, Betreff
  `For Information - ATAEL - …`) bleibt `unknown/unclassified` in `INBOX`, weil der
  Absender `EC-NO-REPLY-GRANT-MANAGEMENT@…` über `do_not_route_if: ['newsletter','no-reply']`
  jedes Projekt-Routing unterdrückt. Derselbe Vorgang landet damit potenziell in zwei
  verschiedenen Zielen, ohne dass der Reviewer den unterdrückten Kandidaten sieht.

#### B-2 — Topic-`do_not_route_if` ist nicht implementiert

- `do_not_route_if` existiert ausschließlich in `core/matching/project_matching.py`
  (Prüfblock vor der Match-Schleife). `core/matching/topic_matching.py` hat keine
  entsprechende Behandlung; ein Grep über `core/**` findet `do_not_route` nur im
  Projekt-Matcher.
- `topics.json` → `netzwerke.do_not_route_if: ['newsletter', 'no-reply']` ist damit
  wirkungslos.
- Offline-Reproduktion: Betreff `OeAD / Hochschule International Newsletter 7/2026`
  → `netzwerke` (`high`) mit `preselected_subtopic: eu-projekte-und-oead`, obwohl
  der Betreff das DNR-Signal `Newsletter` enthält.
- Reale Folge: Env 9400 (`message_id:
  838d70b285107ce4b6b19c334.14d8e54149.20260728071005.e7b64fd196.86f35121@mail141.atl271.mcdlv.net`)
  wurde nach `Themen/Netzwerke` gedraftet.
- Zielkonflikt: Der konsumierende Workspace führt externe Newsletter nach
  `pipelines/mail-desk-batch.md` → `Newsletter` (dort liegt z. B. Env 9377,
  `message_id: d112dc7be456467a80d59d29d2c7a32a@1017`, `decision.kind: archive`,
  final `Newsletter`-Envelope 77). Eine Mailbox-Suche nach `Hochschule International`
  über `Newsletter`, `Themen/Netzwerke` und `INBOX` fand **keine** früheren
  OeAD-Ausgaben (nur `INBOX` 9585); der Konflikt ist also nicht durch Historie
  entschieden und wiederholt sich je Ausgabe.
- Kein Katalog kennt ein Ziel `Newsletter` (`topics.json` hat keinen
  `newsletter`-Eintrag); der Pfad ist ein Sonderfall außerhalb der
  `references/folder-rules.md`-Routingtabelle.

#### B-3 — Nicht-Determinismus bei transienten MIME-Timeouts

- Drei identische Läufe, Ergebnis: Lauf 1 und Lauf 3 sind byte-identisch
  (`review.execute_request_sha256:
  75ae9c41456aa3746c765f413b6aa45d71c7a0e7af3c8af18d3939515121f465`), Lauf 2 weicht
  ab (`55bf1bc2b963fc3115c2cb472c39381e875aba8d76440abe92aaca7813e12891`).
- Gekippte Items (jeweils gleicher Input, gleicher Mailboxzustand):

  | Env | Message-ID | Lauf 1/3 | Lauf 2 |
  |---|---|---|---|
  | 9392 | `6a637340020000f1000d514d@gwia1.boku.ac.at` | `netzwerke/medium`, `not_needed/classification_clear` | `unclassified/low`, `completed/still_ambiguous`, `attachment_status: attachment_inventory_unavailable` |
  | 9393 | `7fd93e10-a0f2-4759-8383-89a444245a96@eucen.eu` | `unclassified/low`, `completed/still_ambiguous` | `netzwerke/medium`, Subtopic `eucen`, `not_needed/classification_clear` |

- Fehlertext des Review-Zweigs: `attachment_error: "Failed to export or inspect
  message MIME: Himalaya timed out; refusing to retry the mailbox command."`
- Mechanik: Der primäre MIME-Export (`inspect_attachments`) läuft transient in einen
  Timeout; `core/classifier.py` erzwingt daraus Review
  (`attachment_inventory_unavailable`, ~Zeile 646). MD-E2 startet danach dennoch
  `attachment_evaluate`, dessen eigener MIME-Abruf im selben Lauf gelingt → es
  entsteht ein Quarantäne-Fetch, der die Ausgangsmehrdeutigkeit nicht auflöst.

#### B-4 — Widersprüchliche Item-Felder und irreführende `notes`

- Gleichzeitig im selben Item: `attachments: []`,
  `attachment_status: "attachment_inventory_unavailable"`, `attachment_error` gesetzt
  **und** `attachment_evaluation.status: "completed"`, `reason: "still_ambiguous"`,
  `authorization: "auto_evaluated"` mit befülltem `files[]` auf eine real gefetchte
  Datei (Lauf 1 Env 9393; Lauf 2 Env 9392).
- `notes` behaupten eine Zuordnung („Themenbezogene Zuordnung zu Netzwerke
  (Betreff: …)“), obwohl `decision.kind: "unknown"`/`id: "unclassified"` bleibt.
  Ein Reviewer kann nicht erkennen, welche Aussage gilt.

#### B-5 — Inline-Signaturbilder werden als `required_for_decision` gefetcht

- Env 9393: `35-years-signature_XS.png`, sha256
  `b2e14e6fb0d7cb3a9c36e350f587ecad6ea1a75a97d33afa5134ba1e0fdd561a`,
  `is_inline: true`, `content_disposition: inline`, `chars: 0` → Quarantäne-Run
  `eval_68ef4d21fded25ab8527843bf253bad2`.
- Env 9392: `IMAGE.png`, sha256
  `8fb95ec6029dfa51c618a5a4519bb4d656cd31e4f32d35e143b822b740a98af3`,
  `is_inline: true`, `chars: 0` → Quarantäne-Run
  `eval_b12670e2d7acf37dfbcb5ab1105571a4` (Lauf 2).
- Beide Auswertungen endeten `still_ambiguous`, die Mails blieben in Review.
  Nutzen: keiner; Nebenwirkung: Quarantäne-/PII-/Quoten-Fläche und ein zusätzlicher
  fehleranfälliger MIME-Abruf.

#### B-6 — `keep_in_folder` fehlt in der dokumentierten Vertragsmenge

- Produktiv genutzt: `core/classifier.py` (Vorlage, Zielwahl, Review-Zweig),
  `core/attachment_reclassification.py`, `core/quarantine/attachment_handoff.py`,
  `core/modes/execute.py`.
- Dokumentiert: `references/batch-runner.md` (execute-Schema) führt `action.type`
  als `["copy_as_move", "move", "copy", "none", "archive"]`;
  `references/folder-rules.md` beschreibt die Retention-in-`INBOX`-Semantik nicht.
  Die Dokumentation ist damit enger als der akzeptierte Vertrag.

#### B-7 — `progress_*.tmp` bleiben liegen

- Nach den Läufen blieben `data/mail-desk/progress_1wv8jnx_.tmp` und
  `progress_vana2exn.tmp` zurück (in der Vor-Batch ebenfalls drei Dateien). Sie sind
  nicht git-ignoriert (`.gitignore` des Consumer-Workspace enthält nur
  `/data/mail-desk/attachments/`) und erzeugen untracked Rauschen in einem
  versionierten Verzeichnis.

#### Ausdrücklich verifiziert und zu erhalten (kein Fixbedarf)

- **Run-ID-Determinismus:** `core/attachment_reclassification.py::derive_evaluation_run_id`
  (bindet Account, Folder, Envelope-ID und normalisierte Message-ID) ist
  deterministisch und PII-frei; die offline nachgerechneten Werte stimmen exakt mit
  den beobachteten `files[].run_id` überein (Env 9393 →
  `eval_68ef4d21fded25ab8527843bf253bad2`; Env 9392 →
  `eval_b12670e2d7acf37dfbcb5ab1105571a4`).
- **Kein Doppel-Fetch:** Die erneute Auswertung von Env 9393 in Lauf 3 nutzte
  denselben Quarantäne-Run; `35-years-signature_XS.png` behielt seine mtime
  (14:27:26), nur `.quarantine-inventory.json` wurde neu geschrieben (14:36:15).
  Das FR-15/MD-E2-Idempotenzversprechen hält.
- **Quarantäne-Preflight erfüllt:** `git ls-files data/mail-desk/attachments/` ist
  leer; `.gitignore` enthält `/data/mail-desk/attachments/`.
- **Draft ohne Mailbox-Mutation**, Lock-Ownership und Backend-Bindung wirksam.

### Ziel & Invarianten

- Routing wird **katalogtreu und deterministisch**: Priorität, Exaktcodes,
  Kontakt-/Domänensignale und Ausschlüsse folgen einer dokumentierten, testbaren
  Ordnung; gleicher Input und gleicher Mailboxzustand ergeben dasselbe Manifest.
- Exaktcode-/Betrefftreffer schlagen reine Kontakt-/Domänentreffer; `routing_priority`
  ist die dokumentierte erste Ordnungsinstanz, die Katalogreihenfolge nur der
  stabile Gleichstands-Tiebreaker.
- `do_not_route_if` gilt für **Projekte und Topics** mit identischer, begrenzter
  Semantik; unterdrückte Kandidaten bleiben als Review-Hinweis sichtbar.
- Keine Abschwächung der FR-15-Grenzen: null neue Schreibpfade,
  `allow_legacy=False` unverändert, Receipt-Klassen-Guard unverändert, keine
  Doppel-Fetches, keine automatische Garbage Collection.
- Vertrags- und Schemaänderungen werden synchron in `SKILL.md`, `references/*`,
  L1/L2-System-Map und beiden FR-Ledgern nachgeführt (Bundle-`AGENTS.md`-Regel).
- Änderungen an `classifier.py`/`core/matching/*` rotieren `classifier_revision`
  genau einmal (genehmigt, wie bei FR-13/MD-M1); betroffene Tests und
  Dokumentationen werden mitgezogen.

### MD-R1 — Prioritäts- und Ausschlusslogik für Projekte und Topics

**Scope:**
1. `core/matching/project_matching.py`: `routing_priority` aus dem Katalog anwenden
   (numerisch, höher zuerst); Gleichstand und fehlender Wert fallen auf die stabile
   Katalogreihenfolge zurück. Innerhalb der Kandidatenbewertung müssen exakte
   ID-/`kuerzel`-/Alias-/`typical_subject_patterns`-Treffer im Betreff einen reinen
   Kontakt-/Domänentreffer eines anderen Projekts schlagen (Signalgüte vor
   Katalogreihenfolge). Die bestehende Treffersemantik (Schwellen, Confidence,
   `do_not_route_if`) bleibt erhalten.
2. `core/matching/topic_matching.py`: `do_not_route_if` mit derselben Semantik und
   denselben Prädikaten wie im Projekt-Matcher anwenden; die Prädikatlogik in einen
   gemeinsamen, testbaren Helper ziehen (kein Duplikat). `newsletter` darf dabei
   **nur** über Betreff-/Header-Signale greifen, nicht über beliebigen Body-Text
   (sonst unterdrückt jedes beiläufige Wort „Newsletter“ ein Topic).
3. Unterdrückte Kandidaten sichtbar machen: Wenn alle Projekt-/Topic-Treffer durch
   DNR entfallen, entsteht kein stilles `unknown`, sondern ein begrenzter
   Review-Grund plus `decision.suppressed_candidates[]` (ausschließlich
   Katalogdaten: `kind`, `id`, `suppression_reason`; niemals Mailinhalt als Ziel).
4. Newsletter-Zielpfad explizit definieren: Ein durch `newsletter` unterdrücktes
   Topic/Projekt wird deterministisch auf den bestehenden `Newsletter`-Pfad
   abgebildet (Archiv-/Massenmail-Semantik, `copy_as_move`) **oder** bleibt mit
   Review-Grund in `INBOX`. Die gewählte Regel wird in
   `references/folder-rules.md` und in der Consumer-Pipeline dokumentiert; der
   Katalog erhält – falls nötig – einen kanonischen Ziel-Eintrag statt eines
   Sonderpfads.

**Pflichttests:** Prioritätsordnung inkl. Gleichstand und fehlendem Wert; Exaktcode
schlägt Kontakt; 9388-Analogon endet bei `atael`; 9387-Analogon erhält Review mit
`suppressed_candidates`; 9400-Analogon endet nicht in `netzwerke`; DNR-Prädikate
inkl. Body-Regressionsfall; Newsletter-Zielpfad deterministisch; bestehende
Mail-Desk-Suite ohne unbegründete Assertion-Brüche.

### MD-R2 — Thread-/Sibling-Kohärenz

**Scope:** Forward-/Referenzketten (`references`, `in_reply_to`) mit identischem,
exakt aufgelöstem Katalogcode dürfen nicht in unterschiedliche Projekte laufen.
Trägt die Klassifikation eines Siblings einen eindeutigen Projektcode, erhält der
blockierte Partner mindestens einen Review-Hinweis mit demselben Kandidaten. Ein
automatisches Umrouten des blockierten Originals ist nur zulässig, wenn der Kandidat
exakt, eindeutig und nicht DNR-verboten ist. Keine Zielableitung aus Mailinhalt;
`candidates`-Semantik bei Mehrdeutigkeit bleibt erhalten.

**Pflichttests:** 9387/9388-Paar; Kette mit unbekanntem Sibling; widersprüchliche
Kandidaten bleiben `candidates`; DNR bleibt wirksam; keine Routing-Änderung ohne
eindeutigen Katalogcode.

### MD-R3 — Deterministische MIME-Inventarkette und Feldkonsistenz

**Scope:**
1. Innerhalb eines Laufs genau **einen** kanonischen MIME-Abruf je Item verwenden;
   Classifier und MD-E2 dürfen nicht zwei unterschiedlich getimte Exporte sehen. Ein
   transienter Timeout führt zu einem definierten, wiederholbaren Ergebnis (Review
   mit `attachment_inventory_unavailable`), ohne im selben Lauf nachzulagern.
2. `attachments[]`, `attachment_status`, `attachment_error` und
   `attachment_evaluation` müssen zueinander konsistent sein. Ein befülltes
   `files[]` mit real gefetchter Datei bei gleichzeitigem
   `attachment_inventory_unavailable`/leerem Inventar ist unzulässig; entweder wird
   das Inventar vorher erneut aufgelöst oder das MD-E2-Ergebnis verworfen.
3. `notes` werden ausschließlich aus der **finalen** Entscheidung erzeugt; keine
   Zuordnungsformulierung bei `unknown`/`unclassified`.
4. Die FR-15/MD-E2-Idempotenz (deterministischer Run, kein Doppel-Fetch) bleibt
   erhalten und wird um den Timeout-Fall erweitert (zweiter Lauf erreicht denselben
   Run).

**Pflichttests:** Timeout-Injektion beim ersten vs. zweiten Export; drei Läufe
ergeben ein identisches Manifest; Feldkonsistenz-Assertions; Notes-Konsistenz;
Quarantäne-Reuse ohne Neu-Fetch.

### MD-R4 — Inline-Bild-Policy für die automatische Auswertung

**Scope:** Inline-Parts (`content_disposition: inline` mit `content_id`) und
Bild-MIME-Typen ohne extrahierbaren Text sind **kein**
`required_for_decision`-Trigger der automatischen Auswertung. Sie bleiben
Inventar-Metadaten; ein Fetch erfolgt nur über den bestehenden, menschlich
freigegebenen MD-A2-Pfad. Die Regel ist eng zu fassen, zu begründen (PII, Quoten,
Signalarmut) und verändert die MD-A1/MD-A2/MD-A5-Verträge nicht.

**Pflichttests:** 9393/9392-Analoga werden nicht automatisch gefetcht; echte
routing-relevante Anhänge (Office/PDF mit Extrakt) lösen weiter aus;
`chars: 0`-Grenzfälle; Human-Approval-Pfad unverändert.

### MD-R5 — Vertragsdokumentation und Laufzeit-Hygiene

**Scope:**
1. `keep_in_folder` in `references/batch-runner.md` (Schema-Enum plus Semantik
   „verbleibt im Quellordner, kein Mailbox-Write“) und in
   `references/folder-rules.md` dokumentieren; ein Contract-Test stellt sicher,
   dass die dokumentierte Menge exakt der akzeptierten Menge entspricht.
2. Progress-Temp-Hygiene: `core/progress.py` bzw. der Runner räumen eigene
   `progress_*.tmp` in jedem Endzustand (success/failed/aborted) auf oder legen sie
   unter einen git-ignorierten Unterpfad; ein Test deckt die Abbruchpfade ab.
3. Consumer-Abstimmung (Dokumentation, kein Code): Newsletter-Regel der
   BOKU-Pipeline und `folder-rules.md`/Katalog auf **eine** Quelle der Wahrheit
   bringen; eine Abweichung ist ein Review-Grund, kein stiller Sonderfall.

**Pflichttests:** Enum-/Doku-Contract-Test; Temp-Aufräumen nach Erfolg, Fehler und
Abbruch; keine neue Ignore-Regel nötig, oder die Ignore-Ergänzung ist dokumentiert
und getestet.

### Gemeinsame Abnahme (FR-17)

**Determinismus-Vertrag (Präzisiert nach dem realen BOKU-Verifikationslauf
2026-09-22, 3× `--draft 10` über die nachfolgenden 10 Mails):**

- Verbindlich ist **Lauf-In-Determinismus**: gleicher Lauf-Input (Mailbox-Zustand,
  Kataloge, Quoten) ergibt ein byte-identisches Manifest; Receipts binden an genau
  das Manifest, gegen das reviewt wurde; `already_fetched`-Idempotenz gilt je
  Nachricht; Item-Felder sind innerhalb eines Laufs konsistent.
- **Nicht** verbindlich ist Byte-Gleichheit über Läufe mit unterschiedlichen
  transienten Infrastruktur-Ergebnissen (z. B. ein Full-Read-Timeout in Lauf 1,
  Erfolg in Lauf 2): ein transienter Infrastrukturfehler erzeugt ein fail-closed
  Review-Item (kein Same-Run-Nachlagern, kein widersprüchliches Item) und wird im
  Folgelauf regulär aufgelöst. Retries bleiben verboten (B-8-Klasse). Der reale
  Lauf bestätigte beide Seiten: der Timeout-Fall blieb sauber in Review ohne
  MD-E2-Fetch (Trigger-Gate wirksam), der Folgelauf klassifizierte korrekt.
- Reale Reproduktion 2026-09-22: kein stiller Hänger (B-8 wirksam), kein
  Inline-PNG-Fetch (B-5 wirksam), Feldkonsistenz und Notes-Wortwahl sauber, keine
  `progress_*.tmp`-Rückstände (B-7 wirksam). Katalog-/Pflichtabstimmungen der
  drei beobachteten Fehl- bzw. Grenzrouten (9408 „eucen Highlights" →
  `netzwerke`; 9419 BeyondTrust → `BOKU-Organisation`; 9412 „LE-LLL" → `AIxLLL`)
  sind **Consumer-Katalog-Pflege** im konsumierenden Workspace und kein
  Bundle-Code-Defekt.


- Fokussierte Tests je Paket, vollständige Mail-Desk-Suite, `compileall`,
  Skill-Katalog-/Workspace-Validierung und `git diff --check` grün.
- Reproduktionsnachweis: Das 10-Mail-Manifest der Testbatch ist nach `MD-R1`–`MD-R4`
  über **drei** identische `draft`-Läufe byte-identisch; `9388 → atael`,
  `9387 → Review mit ATAEL-Kandidat`, `9400 → definierter Newsletter-Pfad`,
  `9392/9393 → deterministischer, konsistenter Endzustand ohne automatischen
  Inline-PNG-Fetch`.
- Nachweis, dass keine neuen Schreibpfade, keine Legacy-Lock-Bypässe und kein
  Doppel-Fetch entstehen; die FR-15-Tests bleiben grün (Ausnahme: die bewusste
  MD-R4-Inline-Policy-Erweiterung).
- Synchronisation: `SKILL.md`, `references/*`, L1/L2-System-Map,
  `FEATURE-REQUESTS.md`, `FEATURE-REQUEST-PROGRESS.md` konsistent;
  `classifier_revision`-Rotation dokumentiert.

### Ausführungsprofil und Reihenfolge

- `MD-R1` und `MD-R2` ändern dieselben Classifier-/Matching-Dateien und werden
  **nicht parallel** umgesetzt; `MD-R2` startet erst nach grünem `MD-R1`.
- `MD-R3` und `MD-R4` berühren die abgeschlossene FR-15-Fläche
  (`attachment_reclassification.py`, `attachment_evaluation.py`, Policy) und werden
  ebenfalls seriell umgesetzt; keine parallelen Writer auf denselben Dateien.
- `MD-R5` ist unabhängig und kann jederzeit laufen (reine Dokumentations-/
  Hygieneänderung), darf aber `MD-R3`-Temp-Pfade nicht überschreiben.
- Jedes Paket: frische Session, Tests zuerst, kleinste Implementierung, fokussierte
  Tests, Gesamtsuite, unabhängiges Review, dann ein Commit.

### Nachtrag 2026-09-22 — Befunde aus der Execute-/Verify-Phase derselben Testbatch

Nach der Review-Korrektur wurden dieselben 10 Mails real ausgeführt: `execute`
10/10 erfolgreich, `verify` 10/10 konsistent, `reconcile` (read-only) `completed`
mit quellengebundenem Handoff (9 Items). Dabei kamen drei weitere, reproduzierbare
Befunde hinzu.

#### B-8 — Client-Suche ohne Ordnerliste hat keinen begrenzten Gesamt-Timeout

- Aufruf: `mail_desk_himalaya_client.py --input …` mit
  `{"action": "search", "query": "Besuch von Univ. Kenia"}` **ohne** `folders`.
- Ergebnis: Lauf hing >5 Minuten (Abbruch durch die aufrufende Shell); die
  Ausgabedatei blieb 0 Bytes, es gab keinen strukturierten Timeout-Fehler.
- Folge: Rund 10 Minuten lang scheiterten danach auch einfache Abfragen
  (`execute`-Readiness-Preflight mit `envelope list -s 1` sowie ein direktes
  `list_envelopes`) mit `himalaya_timeout`, obwohl TCP auf dem IMAP-Endpunkt
  erreichbar war; nach ca. 2 Minuten Wartezeit antwortete der Server wieder in 0,9 s.
- Vermutete Ursache: viele parallele Ordnerabfragen ohne Gesamt-Deadline; eine
  serverseitige Session-Limitierung wirkt danach kurzzeitig nach. Der Batch selbst
  blieb fail-closed (Readiness stoppte vor jeder Mutation).
- Anforderung: begrenzter Gesamt-Timeout (Deadline) für `search` und alle
  Client-Operationen; Hänger enden als `himalaya_timeout`, nie als stiller Hänger;
  optional `folders` verpflichtend machen oder den Hinweis dokumentieren; kein Retry.

#### B-9 — Standalone-`verify` kann bei gemischten Batches keinen Handoff freigeben

- `_verified_execute_candidate` (`core/modes/verify.py`) verlangt gleichzeitig
  `result_ids == verify_scope_ids` **und** bei `pending`-Kandidat
  `candidate_ids == verify_scope_ids`.
- Der `synthesis_candidate` enthält konstruktiv nur `project`/`topic`-Items. Ein
  Batch mit Archiv-/Newsletter-Item (hier Env 9400, `kind: archive`) besitzt daher
  keine Scope-Menge, die beide Gleichheiten erfüllt:
  - Scope = alle 10 Items → Kandidat 9 ≠ 10 → `synthesis_handoff: not_required`,
    kein `completion_report`.
  - Scope = Kandidat 9 Items → `results` 10 ≠ 9 → ebenfalls `not_required`.
- Beide Läufe sind im Consumer-Archiv belegt
  (`2026-09-22-batch-1-batch-verify.json` und
  `2026-09-22-batch-1-batch-verify-candidate-scope.json`). Nur `reconcile` — bzw.
  der interne `pipeline`-Pfad, der diesen Gate nicht durchläuft — setzt den Handoff
  auf `pending` (9 Items, `completion_report: completed`).
- Zusätzlich: Der Runner-Envelope trägt im `data`-Objekt kein `mode`/`ok`; ein
  gespeichertes Execute-Ergebnis wird daher auch über `batch_file` nicht als
  Provenienz akzeptiert (`batch_data.get("mode") == "execute"`).
- Anforderung: Teilmengen-Semantik (`candidate_ids ⊆ verify_scope_ids ==
  result_ids`) oder explizite, dokumentierte Scope-Bildung; Tests für gemischte
  Batches; der Runner-Envelope soll als Provenienz nutzbar sein oder seine exakt
  erwartete Form ist zu dokumentieren.

#### B-10 — `verify`-Evidence-Fallback prüft einen leeren Root und kann nicht `False` melden

- Ohne explizite `evidence`-Spezifikation (z. B. bei reinen `message_ids`) globt
  `core/modes/verify.py` `memory/references/**/evidence/*.md` und liefert
  `in_evidence = None`, wenn nichts gefunden wird.
- Im consumerenden Workspace existiert unter `memory/references/` **kein**
  `evidence`-Ordner (0 Dateien); die kanonische Evidenz liegt unter
  `memory/evidence/**`.
- Folge: Ein `verify` mit reinen Message-IDs meldete 10/10 „consistent“, ohne die
  Evidence-Ebene zu prüfen (`in_evidence: None` für alle Items). Die
  `reconcile`-Referenz führte denselben Batch anschließend korrekt mit `ev: True`.
- Anforderung: Fallback an das kanonische Evidenz-Layout angleichen (oder eine
  explizite `evidence`-Map verlangen); fehlende kanonische Evidenz muss `False`
  ergeben und `consistent` beeinflussen; Test.

#### B-11 — Abschluss-/Dankesmails werden als reply-pflichtig klassifiziert

- Beobachtung: Env 9412 (`message_id: 6a6cc07f0200003e0012c6a5@gwia1.boku.ac.at`,
  Claus Rainer Michalek, 31.07.2026) ist der **abschließende Dank** in einem Thread
  („vielen Dank für das Zusammenstellen der Unterlagen. Liebe Grüße Claus“); die
  Vorgängermail enthält die eigentliche Lieferung (Link + Ankündigung einer
  Ergänzung). Es gibt keine Frage, Bitte, Frist, Entscheidung, Freigabe oder
  Beitragsanforderung.
- Ist-Verhalten: `needs_reply: true` → die Mail würde als Reply-Fall in
  `replies-needed.jsonl` und im `_Needs-Reply`-Ordner des Ziels landen. Der im
  Testlauf beobachtete falsche Reply-Ordner entstand zusätzlich durch eine
  konsumerseitige Katalog-Fehlroute (LE-LLL → AIxLLL), die unabhängig korrigiert
  wurde; der `needs_reply`-Wert selbst bleibt davon unberührt.
- Abweichung zur fachlichen Regel: `SKILL.md` bindet Antwortbedarf an „eine
  konkrete Bitte, Frage, Frist, Entscheidung, Freigabe oder einen Beitrag“ und
  nennt „reine Information“ als gewöhnlich nicht reply-pflichtig.
- Anforderung: Ein Abschluss-/Dankeschön **ohne** konkrete Anforderung setzt
  `needs_reply: false`. Wird die Reply-Entscheidung aus dem Preview abgeleitet und
  ergibt der Full-Read ein reines Abschluss-/Dankmuster, muss sie auf `false`
  herabgestuft werden. Bei verbleibender Unklarheit gilt die bestehende
  Review-Semantik statt einer stillen `true`-Behauptung.
- Pflichttests: Dankes-/Abschlussmails („vielen Dank“, „danke für …“, „passt für
  mich“, „liebe Grüße“ ohne Rückfrage) → `false`; echte Bitten/Fragen/Fristen →
  `true`; Thread, in dem nur die **erste** Mail eine Bitte enthielt und die letzte
  abschließt → `false`; Review-Fall bleibt erhalten.
- Paket: `MD-R8` (Reply-Heuristik), unabhängig von `MD-R1`/`MD-R2` (Classifier-
  Regeln), `MD-R3`/`MD-R4` (Attachment) und `MD-R5` (Doku/Hygiene).
- **Umsetzungsnachweis (2026-09-22, MD-R8 abgeschlossen):** Kanonischer Owner
  `core/matching/reply_heuristics.py` (Closing-/Dankesmarker, Request-Signal-Gate,
  `is_closing_or_thanks`, `needs_reply_review`, `downgrade_if_closing` mit
  `reply_downgrade`-Provenienz und `rule_revision: md-r8`); Facade-Verdrahtung im
  einzigen `_finish`-Punkt von `classify_email_two_pass` (wirkt in beiden Pässen:
  Preview- und Full-Read-Korrektur in beide Richtungen). Pflichttests erfüllt:
  Dankes-/Abschlussmails → `false`; echte Bitten/Fragen/Fristen → nie herabgestuft;
  Thread-Abschluss → `false`; Review-Semantik bei verbleibender Unklarheit
  unverändert. 7 neue Tests (`test_reply_heuristics.py`); Suite 903/903 grün.
- **Live-Nachweis und Quote-Härtung (2026-09-23):** Die Live-Re-Klassifikation von
  Env 9412 (Ablage `Themen/Lifelong-Learning`) zeigte nach MD-R8 weiterhin
  `needs_reply: true`: Das Request-Gate prüfte den gesamten Plaintext inklusive der
  zitierten Vorgängermail („… wird morgen noch eine Präsentation **ergänzen**“) und
  blockierte das Downgrade; außerdem leckten die internen `_closing_check_*`-Felder
  in die Draft-Decision. Fix im kanonischen Owner: `_strip_quoted_history` schneidet
  zitierte `>`/`>>>`-Blöcke, Header-Seperatoren (`-----Ursprüngliche Nachricht-----`,
  `____…`) und `Am … schrieb …:`-Zeilen ab, bevor Closing-/Request-Gate greift;
  `downgrade_if_closing` nimmt den Prüftext jetzt explizit als `subject`/`body` an
  (keine Debug-Felder mehr in der Decision). Gegengeprüft: Live-Draft 2026-09-23 von
  Env 9412 → `needs_reply: false` mit
  `reply_downgrade: {reason: closing_or_thanks, rule_revision: md-r8}`;
  `test_reply_heuristics.py` um 5 Fälle erweitert (Live-Body mit Zitat,
  Quote-Grenz-Varianten, Request im Neutext, Debug-Key-Freiheit); Suite 908/908 grün.

**Paket-Zuordnung der Nachträge:** B-8 → neues Paket `MD-R6` (Client-Deadlines und
Hänger-Freiheit), B-9 und B-10 → neues Paket `MD-R7` (Standalone-Verify-Provenienz,
Scope-Bildung und Evidence-Prüfung), B-11 → neues Paket `MD-R8` (Reply-Heuristik).
Alle drei sind unabhängig von `MD-R1`–`MD-R5` (keine gemeinsamen Dateien mit
`MD-R1`/`MD-R2`; `MD-R7` berührt `verify.py`, `MD-R8` die Reply-Erkennung) und
werden in die gemeinsame Abnahme aufgenommen.

### Out of Scope

- Änderung realer Mailbox-Ordner oder Umlabeln bereits gerouteter Mails.
- Neue Katalogfelder jenseits der vorhandenen `routing_priority`/`do_not_route_if`.
- Promotions-, Cloud-, Task- oder Dispositionspfade (FR-09/FR-10/FR-16 bleiben
  unberührt); `attachment_evaluation` bleibt ohne Promotion-/Export-Seiteneffekt.
- Automatische Bereinigung bestehender Quarantäne-Runs (weiterhin explizite
  Control-Plane-Aktion).

---

## FR-18: Workspace-Agnostizismus des Mail-Desk — Desk-Signals-Katalog und Identitäts-Bindung

**Status:** ✅ Abgeschlossen (2026-09-23, MD-S1–MD-S3 im Kernel-Loop mit Subagenten umgesetzt; ein unabhängiges Review verlangte eine Fix-Runde am MD-S1-Schema-Drift-Gate, nachgezogen und re-approviert). Verhaltensänderung im Reply-Bedarfs-Pfad des Basis-Klassifikators
plus neue Katalogstruktur im konsumierenden Workspace. Quelle: kritische Durchsicht der
Reply-Triggers (2026-09-23, Nachbefund zu MD-R8/B-11 und zur Nebenbeobachtung im
Quote-Härtungs-Review). Keine Mailbox-Mutation, keine Promotion-/Export-/Cloud-Pfade.

### Problem & Motivation

Die Reply-Bedarfsheuristik des Basis-Klassifikators ist **weder workspace- noch
account-bezogen**: `reply_triggers` (classifier.py, alle 9 Trigger enthalten literal
`martin`: „martin bitte", „hallo martin", „@martin", …) ist eine Module-Konstante.
Ein konsumierender Workspace mit anderem Desk-Owner erhält für denselben Mailbestand
aus dieser Heuristik **niemals** einen Reply-Bedarf — stille Unter-Klassifikation.
Gleichgelagert: `no_reply_sender_tokens` („no-reply", „quarantine", …) und die
Fest-Domain-Liste `{"boku.ac.at", "gmail.com", "yahoo.com", "hotmail.com"}` in
`sent_indexer.py` sind ebenfalls hardcoded Identitätsannahmen. Keine der Stellen ist
durch Tests vertraglich abgesichert, und der generische Bundle-Claim wird verletzt.

Bewertung der Integrationsvariante „Sub-Array `signals.reply_triggers` je Projekt-/Topic-
Entry": **verworfen.** Reply-Bedarf ist Desk-global (wird vor der Projekt-/Topic-Match-
Logik bestimmt und auch für `unknown`-Items gebraucht), Entry-Arrays würden die
DOC-M1-Problemklasse (Metrik-/Literal-Vervielfältigung) zurückbringen und den
`unknown`-Fall nicht abdecken.

### Ziel & Invarianten

- **Neuer Desk-Signals-Katalog** im konsumierenden Workspace:
  `memory/references/mail-desk/mail-desk.json`, Schema 1:
  `{"schema_version": 1, "reply_heuristics": {"reply_triggers": [...],
  "no_reply_sender_tokens": [...], "owner_address": null | str}, "updated_at": ...}`.
  Pflichtfeld `reply_triggers`; `no_reply_sender_tokens` optional mit Default;
  `owner_address` optional (`null` = Verhalten wie heute).
- **Bundle liest, konsumiert:** `load_reply_heuristics(workspace_root)` als kanonischer
  Loader (analog `load_catalogs`); **Datei fehlt → dokumentierter Fallback** auf die
  bisherigen Default-Trigger (Kompatibilität); **Datei invalide/Schema-Drift →
  fail-loud** (Konsistenz mit der Katalog-Drift-Behandlung). Bundle bleibt
  Zero-Mutation: die Datei gehört dem konsumierenden Workspace.
- **Trigger-Semantik bleibt erhalten:** Substring-Anrede-Trigger mit Wortgrenzen
  ersetzt die bare Substring-Matches („Smartin?"-Klasse ausgeschlossen);
  `FULL_BODY_ACTION_REQUEST` und die MD-R8-Quote-Härtung bleiben unverändert
  zuständig (Escalation-Trigger bzw. Downgrade).
- **Optional `owner_address`:** wenn gesetzt, wertet der Sent-Reply-Check die echte
  Desk-Identität gegen `to`/`cc` aus (statt Keyword-Heuristik); `sent_indexer`-Domain-
  Liste wird über `no_reply_sender_tokens`/Desk-Katalog konfigurierbar (keine
  verhaltensändernde Migration bestehender Workspaces ohne Katalogdatei).
- **MD-S2 — sent_indexer-Account-Bindung:** Das `"mailbox": "BOKU-MARTIN"`-Literal
  in `sent_indexer.py` wird durch den verifizierten `account`-Parameter ersetzt, der
  bereits durch `sync_sent_items`/den Batch-Lauf fließt; jeder Sent-Index-Eintrag wird
  mit dem echten, gebundenen Account getaggt (fehlender Account → fail-loud, konsistent
  zur Attachment-Bindung). Keine Workspace-Datei nötig; das Literal entfällt.
- **MD-S3 — Zoom-Recording-Routing in den Katalog:** Der hardcoded Zoom-Recording-
  Pfad (`target_folder: "Themen/BOKU-Organisation"`, `id: "boku-organisation"`) wird
  aus dem Bundle entfernt; das Routing gehört in den Topic-Katalog des konsumierenden
  Workspace (`typical_subject_patterns`/`keywords` am Topic-Entry inkl. Zoom-Signalen,
  optional `do_not_route_if`-Gegenregeln). Das Bundle behält nur die generische
  `zoom-join-ping` → `Trash`-Heuristik (workspace-unabhängig). Keine Migration
  bestehender Mails; ein fehlender Katalog-Eintrag ergibt `unknown`/Review statt
  stiller Fehlroute.
- **Schema-Owner:** Validierung analog `project-catalog-entry` (kleiner Abschnitt im
  bestehenden Schema-Owner-Skill oder eigener Desk-Entry-Abschnitt); Drift stoppt
  fail-closed.

### Abnahme

- Trigger-Liste ist ausschließlich aus der Workspace-Datei geladen; ein Workspace mit
  anderem Owner-Namen erhält korrekte Reply-Bedarfe (hermetischer Test mit alternativer
  Identität).
- Fehlende Datei → Default-Verhalten unverändert (Regressionstest gegen heutige
  Trigger-Liste); invalide Datei → fail-loud mit strukturiertem Fehler.
- `no_reply_sender_tokens` und die sent_indexer-Domain-Liste sind aus der Datei
  konfigurierbar; Default-Werte = heutige Konstanten.
- **MD-S2:** Sent-Index-Einträge tragen den verifizierten Batch-`account`; ein Lauf
  ohne Account erzeugt fail-loud einen strukturierten Fehler; Regressionstest belegt
  identische Indexstruktur bei identischem Account.
- **MD-S3:** Kein `Themen/BOKU-Organisation`-Literal mehr in Produktionsskripten (`git grep`-Nachweis über `skills/mail-desk/scripts/`; Test-Fixtures dokumentieren das Muster bewusst);
  ein konsumierender Workspace mit Zoom-Recording-Topic-Entry routet identisch, ohne
  Eintrag ergibt sich `unknown`/Review (hermetischer Test).
- Hermetische Tests, null Mailbox-Zugriffe; Mail-Desk-Suite grün; Metrik-Stellen per
  L1 §3.1-Checkliste nachgezogen; System-Map-Sync (L1/L2 + ggf. objects.md).
- Hermetische Tests, null Mailbox-Zugriffe; Mail-Desk-Suite grün; Metrik-Stellen per
  L1 §3.1-Checkliste nachgezogen; System-Map-Sync (L1/L2 + ggf. objects.md).

**Umsetzungsnachweis (2026-09-23):** MD-S1 — Loader `load_reply_heuristics` + fester
`ReplyHeuristicsConfig`-Vertrag (frozen), Wortgrenzen-Predicate
`matches_reply_trigger`, fail-loud Schema-Gate (bool/float/str abgewiesen), Classifier
konsumiert die Konfiguration für `unknown`-Items mit; MD-S2 — fail-loud Account-Bindung
vor Write/Himalaya-Aufruf, `mailbox` = verifizierter Account, `draft_manifest`-Pass-
through; MD-S3 — Recording-Branch entfernt, himalaya-Such-Fallback neutral, Join-Ping-
Regression unverändert. 24 neue Tests über drei Module; Suite 932/932 grün; Metriken
146/66/56/61/932 (L2-Kanonik). Consumer-Migrationshinweis: BOKU-User-Workspace braucht
für Zoom-Recordings den `boku-organisation`-Topic-Entry in `topics.json` (bereits
vorhanden) und optional `memory/references/mail-desk/mail-desk.json` für individuelle
Trigger; ohne die Datei greifen die Kompatibilitäts-Defaults.

### Out of Scope (FR-18)

- Änderung von `FULL_BODY_ACTION_REQUEST` oder der MD-R8-Closing-/Quote-Semantik
  (beide unangetastet).
- Promotion-/Export-/Cloud-/Task-Pfade (FR-09/FR-10 unberührt).
- Automatische Identitätsableitung aus dem Mailbestand; `owner_address` wird nur
  explizit aus der Katalogdatei gelesen.
- Freemail-Spam-Domain-Heuristik (`@yahoo.` etc.): bewusst workspace-unabhängige
  Anti-Phishing-Policy, bleibt im Bundle.

---

## FR-21: FR-18-Nachtrag — dynamische Desk-Signals-Pflege und Katalog-Validator

**Status:** ✅ Abgeschlossen (2026-09-23; MD-S4 + MD-S5 im Kernel-Loop mit Subagenten, 1 Fix-Runde nach unabhängigem Review; Umsetzungsnachweis unten). Aus der Consumer-Migration von FR-18 im Workspace `boku-user`
(2026-09-23): der Desk-Signals-Katalog (`memory/references/mail-desk/mail-desk.json`)
wurde angelegt und die Zoom-Recording-Routing-Signale per `topics.json`-Entry migriert.
Dabei zeigten sich zwei strukturelle Lücken: die Katalogpflege ist undokumentiert,
und Workspace-Kataloge haben keinen ausführbaren Validator.

### Problem & Motivation

1. **Skill-Doku fehlt:** `skills/mail-desk/SKILL.md` und
   `skills/mail-desk/references/batch-runner.md` nennen den Desk-Signals-Katalog
   nicht (`git grep mail-desk.json skills/mail-desk/SKILL.md` leer). Der Loader
   (`load_reply_heuristics`, `core/matching/reply_heuristics.py`) ist implementiert
   und fail-loud, aber Pflegeinstruktionen fehlen: Agents wissen nicht, dass
   Reply-Trigger **nur** im Workspace-Katalog ergänzt werden (Bundle-Default =
   reiner Kompatibilitäts-Fallback), welches Schema gilt und was `owner_address`
   bewirkt. Risiko: Trigger werden im Bundle-Code geändert oder das Schema wird
   erraten.
2. **Match-Semantik undokumentiert:** `typical_subject_patterns` werden in
   `core/matching/topic_matching.py` (`_subject_signal_matches` /
   `_topic_subject_signal_matches`) als **Literal-Signale mit Wortgrenzen**
   verarbeitet (`re.escape(signal)`, `(?<!\w)…(?!\w)`, Minimum 3 Zeichen,
   `[-_]`-Normalisierung) — **keine Regex**. `skills/topic-catalog-entry/SKILL.md`
   dokumentiert das Feld, nicht die Semantik. Ein Migrationsprompt nutzte
   „Meeting-Objekte für .* sind bereit" regex-artig; solche Patterns matchen
   niemals (der `.*` wird literal gesucht).
3. **Kein Workspace-Katalog-Validator:** `topic-catalog-entry` und
   `project-catalog-entry` definieren Schemata in SKILL.md, aber es existiert kein
   ausführbares Prüfwerkzeug für die Consumer-Kataloge
   (`topics.json`, `projects.json`, `mail-desk.json`). Schema-Drift in
   `mail-desk.json` wird erst beim nächsten Draft-Lauf fail-loud sichtbar; Drift in
   Topic-/Projektkatalogen wird vom Classifier je nach Feld still oder spät
   bemerkt. FR-18 hatte den Schema-Owner („Validierung analog
   project-catalog-entry") in den Zielinvarianten, aber nicht umgesetzt.

### Ziel & Invarianten

**MD-S4 — Doku + Skill (Pflegevertrag statt Auto-Learning):**
- `skills/mail-desk/SKILL.md` (+ bei Bedarf `references/batch-runner.md`): neuer
  Abschnitt „Desk-Signals-Katalog": Pfad
  `memory/references/mail-desk/mail-desk.json`, Schema 1, Pflichtfeld
  `reply_heuristics.reply_triggers`, optionale `no_reply_sender_tokens`/
  `owner_address`, Missing-File-Fallback auf die dokumentierten Defaults,
  fail-loud-Drift-Behandlung. Pflegeinstruktion: Trigger und Desk-Patterns werden
  **ausschließlich** im Workspace-Katalog ergänzt/angepasst (dynamisch ohne
  Bundle-Änderung); Bundle-Code wird nicht angefasst.
- `owner_address`-Semantik: gesetzt → Sent-Reply-Check wertet die echte Desk-
  Identität gegen `to`/`cc` aus; `null` = Keyword-Heuristik wie bisher. Empfehlung
  für Desk-Owner dokumentiert (z. B. die eigene BOKU-Adresse), ohne Verhalten zu
  ändern.
- `skills/topic-catalog-entry/SKILL.md` (und analog `project-catalog-entry`):
  Match-Semantik der Subject-Patterns dokumentieren — Literal-Signale mit
  Wortgrenzen, kein Regex, ≥3 Zeichen, `[-_]`-Normalisierung, case-insensitive;
  ein Gegenbeispiel („… .* …" matcht nie) als Warnung.
- Consumer-Hinweis: Workspace-AGENTS/Router-Dokumentation verweist auf den
  Katalog als Pflegeort (Consumer-Dateien gehören dem Workspace; das Bundle
  dokumentiert nur).
- **Hermetischer Migrationstest:** ein Topic-Entry mit den vier Live-Patterns
  (`Meeting-Objekte`, `Cloud-Aufzeichnung`, `Zoom-Aufzeichnung`,
  `recording is now available`) routet die drei BOKU-Notification-Serien
  („Meeting-Objekte für … sind bereit", „Cloud-Aufzeichnung … verfügbar",
  englisches „recording is now available") identisch zum entfernten FR-18-Zoom-
  Zweig; null Mailbox-Zugriffe.

**MD-S5 — Katalog-Validator:**
- Ein read-only CLI (Beispielname `skills/mail-desk/scripts/mail_desk_validate_catalogs.py`;
  der Dateiname ist **nicht bindend** — implementiert als
  `skills/mail-desk/scripts/catalog_validator.py`)
  validiert **alle drei** Workspace-Kataloge in einem Lauf:
  - `mail-desk.json` gegen exakt die `load_reply_heuristics`-Regeln
    (Schema-1-Strict-Gate inkl. bool/float-Abweisung, Pflichtfeld, String-Listen),
  - `topics.json`: geprüft werden die Arbeitsmodus-Pflichtfelder `id`/`title`/
    `mailbox_folder`, `typical_subject_patterns` (Liste nicht-leerer Strings;
    verschachtelte Sub-/Operation-/Event-Patterns zusätzlich ≥ 3 Zeichen nach
    `strip()`) und Listen-Typen. `reference_md`, `schema_version` und
    Slug-Einheitlichkeit sind dokumentierter Folge-Scope (der reale Bestand würde
    sie bestehen),
  - `projects.json` analog zum Projekt-Schema.
- Strukturierte Fehlerausgabe (Datei, Entry-ID, Feld, Erwartung, Ist-Wert-Pfad),
  Exit-Code fail-closed; kein Mailbox-/Netzwerkzugriff; keine Mutation.
- Fehlende optionale Datei (`mail-desk.json`) = Hinweis mit Default-Verweis, kein
  Failure; fehlende Pflichtkataloge = Failure.
- Optionale Verdrahtung in den Draft-Preflight (Warn-only oder Gate) wird im Paket
  entschieden; Minimalvariante ist Standalone-CLI plus Doku.
- Hermetische Tests: valide Beispielkataloge grün; je Drift-Klasse (bool
  `schema_version`, leere Trigger-Liste, Nicht-String-Pattern, fehlendes
  Pflichtfeld, Regex-förmiges Pattern) → strukturiertes Failure.

### Abnahme

- `git grep -n "mail-desk.json" skills/mail-desk/SKILL.md` liefert den
  Dokumentationsabschnitt; Reply-Trigger sind nur noch am Katalog dokumentiert,
  Bundle-Default ausschließlich als Fallback-Verweis.
- Pattern-Semantik mit Gegenbeispiel ist in beiden Katalog-Entry-Skills
  dokumentiert.
- Der Migrationstest belegt identisches Routing der drei Notification-Serien
  über den Katalogpfad.
- Validator über alle drei Kataloge: hermetische Grün-/Fail-Matrix; Standalone-
  Aufruf im Workspace `boku-user` gegen den realen Bestand grün (mit Hinweisspalte
  für optionale Datei).
- Mail-Desk-Suite grün; Metrik-Stellen per FR-16-Checkliste nachgezogen;
  System-Map-Sync (L1/L2).

### Umsetzungsnachweis (2026-09-23)

- **MD-S4 (Doku):** `skills/mail-desk/SKILL.md` (Desk-Signals-Katalog, Schema,
  Pflegevertrag, `owner_address`-Contract), `skills/mail-desk/references/
  batch-runner.md` (Katalog-Verweis) und `skills/topic-catalog-entry/SKILL.md`
  (Literal-/Lookaround-Semantik, Root-vs-Nested-Unterschied, Gegenbeispiel) sind
  dokumentiert; `skills/mail-desk/tests/test_catalog_docs_contract.py` pinnt die
  drei Abschnitte mit 13 grünen Tests.
- **MD-S5 (Validator):** `skills/mail-desk/scripts/catalog_validator.py` (read-only,
  kein Mailbox-/Netzwerkzugriff) validiert `topics.json`, `projects.json` und das
  optionale `mail-desk.json`; kanonischer Envelope mit Aktion `catalog_validator`
  und Exit `0` (valide) / `1` (Drift) / `2` (Input/Runtime). Testmodul
  `skills/mail-desk/tests/test_catalog_validator.py`: 31 grün zum MD-S5-Abschluss.
- **Fix-Runde MD-S4-S5-fix-001:** Min-3-Gate korrekt auf verschachtelte Patterns
  (`subtopics[]`/`operations[]`/`events[]`) begrenzt, Root-Patterns nur noch
  Nicht-Leer-String (der reale `boku-user`-`QC`-Fall war ein False Positive);
  `owner_address`-Doku auf Contract-Semantik korrigiert (Consumer ist FR-18-
  Target-Verhalten, nicht implementiert). Testmodul danach 35 grün.
- **Realer Bestand:** `python -B skills/mail-desk/scripts/catalog_validator.py
  --workspace boku-user --json` liefert nach dem Fix `valid: true` und Exit `0`.
- **Metriken:** Die berührten Metrik-Stellen (Testzahlen, Validator-Objekt) werden
  im L2-System-Map-Sync durch den Orchestrator nachgezogen.

### Out of Scope (FR-21)

- Automatisches Lernen von Reply-Triggern/Patterns aus Sent-/Reply-Verhalten oder
  Batch-Korrekturen (Verhaltensdrift ohne Review-Gate; bei Bedarf separater FR mit
  Vorschlags- statt Auto-Apply-Modus).
- Verhaltensänderungen am Trigger-/Pattern-Matching selbst
  (FR-18/MD-S1-Semantik unverändert; nur Doku/Validierung).
- Änderungen an Consumer-Dateien durch das Bundle (Workspace-Dateien bleiben
  Workspace-Eigentum).

---

## FR-22: Identity-freier Desk-Signals-Fallback und Katalogisierung der BOKU-Restbestände

**Status:** ✅ Abgeschlossen (2026-09-23; MD-ID1-ID4 im Kernel-Loop mit Subagenten: 12 Dispatches mit Red-Gates, 1 Fix-Runde nach unabhängigem Review — Validator-Gate für unbenutzbare Owner-Local-Parts; 1015 Tests grün; Umsetzungsnachweis unten). Analysequelle: Fallback-Befund-Analyse vom 2026-09-23
(Orchestrator); es wurde keine Codeänderung vorgenommen. Die Analyse folgte der
Frage, inwiefern der `reply_needed`-Trigger-Fallback von den boku-user-Spezifika
entfernt und durch einen generischen Fallback ersetzt werden sollte. Ausgewählte
Strategie (Human-Entscheidung 2026-09-23): **B + D** — neutraler Fallback plus
owner-generierte Trigger; alle Restbestände in diesem FR.

### Problem & Motivation

1. **Identity-Leak im Fallback:** `DEFAULT_REPLY_TRIGGERS`
   ([`reply_heuristics.py:158-168`](skills/mail-desk/scripts/core/matching/reply_heuristics.py)
   enthält 9 Trigger, **alle** mit dem literalen „martin" („martin bitte",
   „hallo martin", „@martin", …). Der Fallback ist der dokumentierte
   Kompatibilitäts-Pfad für Workspaces **ohne** Desk-Signals-Katalog
   (FR-18/MD-S1) — er trägt aber einen personen- statt workspace-bezogenen
   Identitätsnamen und verletzt damit denselben generischen Bundle-Claim, den
   FR-18 für die hartcodierten Stellen festgestellt hat.
2. **Toter Code für den einzigen Consumer:** `boku-user` ist der einzige echte
   Mail-Desk-Consumer (2026-09-23 verifiziert: kein anderer Agent-Workspace hat
   `data/mail-desk`) und besitzt eine eigene `mail-desk.json` mit exakt denselben
   9 Triggern — der Fallback wird dort nie konsultiert.
3. **`needs_reply` hat genau eine Quelle:** Der Reply-Bedarf entsteht
   ausschließlich über `matches_reply_trigger(full_text, reply_triggers)`
   ([`classifier.py:364-367`](skills/mail-desk/scripts/core/classifier.py)); es
   gibt keine zweite Trigger-Quelle. Ein neutraler Fallback ist damit vollständig
   vorhersagbar (keine verdeckte Kompensation).
4. **FR-18-Restlücke:** Die FR-18-Zielinvariante „`sent_indexer`-Domain-Liste wird
   über `no_reply_sender_tokens`/Desk-Katalog konfigurierbar" wurde im
   Umsetzungsnachweis nicht realisiert — der Umsetzungsnachweis deckt nur die
   Account-Bindung (MD-S2) ab. Die Fest-Domain-Liste
   `{"boku.ac.at", "gmail.com", "yahoo.com", "hotmail.com"}` steht unverändert
   hardcoded in [`sent_indexer.py:377`](skills/mail-desk/scripts/core/sent_indexer.py).
5. **Weitere BOKU-Restbestände (7 Stellen in 4 Dateien, 2026-09-23 inventarisiert):**
   - `sent_indexer.py:362` — Stopwort „boku" in der Subject-Keyword-Zerlegung.
   - `sent_indexer.py:377` — Fest-Domain-Liste (Whitelist „keine
     Fremd-Domain-Heuristik").
   - [`project_matching.py:491-492,506`](skills/mail-desk/scripts/core/matching/project_matching.py)
     — `@boku.ac.at` als internal/external-Grenze im Contact-Matching (3 Stellen).
   - [`topic_matching.py:727`](skills/mail-desk/scripts/core/matching/topic_matching.py)
     — `boku.ac.at` in der generischen Domain-Ausschlussliste.
   - [`classifier.py:560`](skills/mail-desk/scripts/core/classifier.py) —
     Freemail-Spam-Gegenindikatoren („weiterbildung", „lebenslanges lernen",
     „focus group", „lehrgang") als BOKU-Content im Anti-Phishing-Zweig.

### Ziel & Invarianten

**MD-ID1 — Neutraler Fallback + owner-generierte Trigger (Schema 2):**
- `load_reply_heuristics` ohne Katalogdatei → `reply_triggers` = **leere Liste**
  (dokumentierte Semantik: „ohne Katalog keine Anrede-Trigger"; `needs_reply`
  bleibt in diesem Fall durch Trigger false — Review- und
  FULL_BODY_ACTION_REQUEST-Semantik unverändert).
- Katalog mit `owner_address`, aber ohne `reply_triggers` → der Loader generiert
  aus dem lokalen Teil der Owner-Adresse (Vorname) die generische Anrede-Trigger-
  Klasse („hallo <vorname>", „<vorname>, bitte", „@<vorname>", …) mit unveränderter
  Wortgrenzen-Predicate (`matches_reply_trigger` bleibt Owner).
- **Schema 2** mit striktem Gate (bool/float/str-Abweisung wie Schema 1,
  fail-loud): `reply_triggers` optional, wenn `owner_address` gesetzt ist;
  Schema-1-Dateien bleiben valide (Migration kompatibel — der reale
  `boku-user`-Katalog bleibt unverändert verwendbar).
- `catalog_validator.py` (MD-S5) wird synchron auf Schema 2 erweitert (inkl.
  Schema-1-Akzeptanz als Legacy).

**MD-ID2 — sent_indexer-Domain-Liste und Stopwörter → Katalog:**
- Die Domain-Whitelist (Zeile 377) und das „boku"-Stopwort (Zeile 362) wandern
  in konfigurierbare Desk-Signals-Katalogfelder (neu in Schema 2); fehlende
  Felder = dokumentierte Defaults (identisches Verhalten für Bestandskataloge).

**MD-ID3 — Internal-domain-Matching → Katalog:**
- `project_matching.py:491-492,506` und `topic_matching.py:727` konsumieren die
  internal-domain-Liste aus dem Desk-Signals-Katalog statt der
  `@boku.ac.at`-Literale; die generic-freemail-Ausschlussliste
  (gmail/outlook/yahoo/hotmail) bleibt als generische Bundle-Konstante.

**MD-ID4 — Spam-Gegenindikatoren (Entscheidung im Paket):**
- `classifier.py:560`: Die 4 BOKU-Content-Gegenindikatoren wandern in ein
  optionales Katalogfeld; fehlt es, bleibt die Freemail-Spam-Heuristik ohne
  Gegenindikatoren wirksam (strenger). Alternative: bewusster Verbleib mit
  dokumentierter Begründung. Die Entscheidung wird im Paket mit Befundbasis
  getroffen und im Umsetzungsnachweis begründet.
- **Analysevorbefund (2026-09-23, verifiziert):** Der Spam-Zweig läuft nur, wenn
  kein Projekt-/Topic-/Thread-Match existiert (classifier.py:544 — Katalog-Match
  gewinnt vor Spam); die Gegenindikatoren retteten im realen BOKU-Bestand 0 Mails
  (34 Keyword-Mails kamen alle von institutionellen Adressen); kein Test pinnt
  sie. Der Spam-Zweig kombiniert bereits 2 Gates (Phishing-Betreffmuster UND
  Freemail-Domain), aber **Freemail allein ist kein guter Spam-Indikator** — in
  anderen Mailboxen können legitime Freemail-Kontakte (@gmail-Professoren,
  Vereinsmails) solche Muster in harmloser Korrespondenz tragen. Daraus folgt
  als Arbeitshypothese: **Streichung der Content-Gegenindikatoren** (Option B)
  mit Ersatz durch ein **absenderbasiertes Vertrauenssignal im Katalog** —
  kataloggetreue Kontakte/Domains des Workspace gewinnen ohnehin vor dem
  Spam-Zweig (Zweig-Reihenfolge), und optionale
  `spam_sender_allowlist`-Einträge (Schema 2) decken den Randfall
  „Freemail-Kontakt ohne Katalog-Entry" ab. Die finale Streichungsentscheidung
  fällt im Paket gegen diese Begründungsbasis.

**Abgrenzungen (unverändert):**
- Die Freemail-Spam-**Domain-Liste** selbst (`@yahoo.` etc.) bleibt
  workspace-unabhängige Anti-Phishing-Policy im Bundle (FR-18-Out-of-Scope).
- `FULL_BODY_ACTION_REQUEST` und die MD-R8-Closing-/Quote-Semantik bleiben
  unangetastet.
- Kein Verhaltenswechsel für Workspaces mit vorhandenem Katalog (Trigger
  identisch aus der Datei).
- Kein Auto-Learning; die Katalogpflege bleibt explizit (FR-21-Vertrag).

### Abnahme

- Hermetische Tests: leerer Fallback (kein Trigger, `needs_reply: false`),
  owner-generierte Trigger (Vornamen-Klasse mit Wortgrenzen-Gegenprobe),
  Schema-1-Dateien weiterhin grün (Legacy-Migration), je Drift-Klasse ein
  Fail-loud-Test, internal-domain-Matching mit alternativer Domain.
- `test_reply_trigger_catalog.py`: der Default-Regressionstest wird auf den
  neutralen Fallback umgeschrieben (kein „martin"-Literal mehr als erwarteter
  Fallback); Docs-Contract-Tests sync SKILL.md/batch-runner.md.
- Docs: SKILL.md (Fallback-Semantik + Schema 2), batch-runner.md-Verweis,
  ggf. catalog-entry-Skill.
- Mail-Desk-Suite grün; Metrik-Stellen per FR-16-Checkliste; System-Map-Sync
  (L1/L2 + objects.md-Desk-Signals-Eintrag).

### Umsetzungsnachweis (2026-09-23)

- **MD-ID1 (Schema 2 + neutraler Fallback):** `load_reply_heuristics` liefert ohne
  Katalogdatei eine **leere** Trigger-Liste; `DEFAULT_REPLY_TRIGGERS` entfernt
  (dokumentierende Konstante `GREETING_TRIGGER_TEMPLATES` verbleibt);
  `derive_greeting_triggers(owner_address)` leitet die 9er-Anrede-Klasse aus dem
  Vorname-Segment des Local-Parts ab (fail-loud bei unbenutzbarem Local-Part).
  Schema 2 (striktes Gate) mit `reply_triggers` optional-wenn-owner;
  Schema-1-Dateien bleiben valide Legacy. Vier neue optionale Felder auf der
  frozen `ReplyHeuristicsConfig`: `sent_sender_domain_whitelist`,
  `sent_subject_stopwords`, `internal_domains`, `spam_sender_allowlist` (je
  dokumentierte Defaults = bisherige Bundle-Konstanten). Predicate
  `matches_reply_trigger` byte-identisch. SKILL.md/batch-runner.md auf neutralen
  Fallback + Schema 2 umgeschrieben; `catalog_validator.py` akzeptiert Schema 1+2.
- **MD-ID2 (sent_indexer):** Stopwort- und Domain-Whitelist-Mengen kommen aus der
  Konfiguration (Re-Export der Defaults via `assertIs`-identische Objekte);
  `check_if_replied` optionaler `identity_config`-Parameter (None = Defaults ohne
  Datei-I/O); `auto_resolve_replies_from_sent` threadt `workspace_root`; classifier
  übergibt die geladene Konfiguration. Kein „boku"-Literal in `sent_indexer.py`.
- **MD-ID3 (internal-domain-Matching):** `select_project_match`/`select_topic_match`
  erhalten optional `internal_domains` (Default = Katalog-Default `boku.ac.at`);
  generische Freemail-Menge als `GENERIC_FREEMAIL_DOMAINS`-Konstante; classifier
  threadt die Katalogliste in beide Selektoren. Default-Verhalten byte-identisch.
- **MD-ID4 (Spam-Zweig):** Die 4 Content-Gegenindikatoren entfernt (Zweig =
  Phishing-Betreffmuster UND Freemail-Domain); `_sender_is_allowlisted`
  (Exaktadresse ODER Domain, case-insensitive, `extract_email_address`-basiert) als
  erste Bedingung des Zweigs. Branch-Reihenfolge und Freemail-Domain-Liste
  unverändert. `focus group` verbleibt in `FULL_BODY_ARTIFACT_SIGNALS` (frozen
  Baseline-Konstante, separater Vertrag).
- **Fix-Runde MD-ID-fix-001:** Validator-Gate für unbenutzbare Owner-Local-Parts —
  `derive_greeting_triggers` wiederverwendet (keine duplizierte Logik); 5 Subtests
  („", „   ", „@", „.", „   @example.org") fail-loud.
- **Verifikation:** 1015/1015 Tests grün (7 geänderte/2 neue Testmodule); compileall,
  `git diff --check`, Index clean; Live-Lauf
  `catalog_validator.py --workspace boku-user` exit 0 (Schema-1-Legacy akzeptiert).
  Metriken 151/67/56/65/1015 (L2-Kanonik, FR-16-Checkliste nachgezogen).

**Consumer-Migrationshinweis (boku-user):** Der bestehende Schema-1-Katalog bleibt
vollständig valide — **keine Migration zwingend erforderlich**. Optionale Schema-2-
Erweiterung für neue Freiheiten (Copy-Prompt an den Workspace-Owner):
1. `schema_version` auf `2` setzen (eröffnet `reply_triggers`-Optionalität und die
   neuen Felder),
2. `reply_triggers`-Block optional entfernen (dann generiert der Loader die
   Anrede-Trigger aus `owner_address: martin.mayr@boku.ac.at` automatisch — die
   9 bisherigen Trigger entstehen identisch aus der Ableitung),
3. neue Felder nur bei Bedarf pflegen (alle optional mit dokumentierten Defaults:
   `sent_sender_domain_whitelist`, `sent_subject_stopwords`, `internal_domains`,
   `spam_sender_allowlist`).
Ohne Änderung bleibt das Verhalten byte-identisch (Schema-1-Legacy-Pfad).

### Out of Scope (FR-22)

- Automatische Identitätsableitung aus dem Mailbestand (Owner-Adresse kommt
  ausschließlich explizit aus dem Katalog; FR-18-Invariante).
- Verhaltensänderung des Trigger-Matching-Prädikats selbst (Wortgrenzen-Predicate
  bleibt byte-identisch).
- Promotion-/Export-/Cloud-/Task-Pfade (FR-09/FR-10 unberührt).
- Threading des Desk-Signals-Katalogs in `core/modes/resolve.py`
  (`auto_resolve_replies_from_sent` läuft dort weiter mit Defaults —
  byte-identisch zu HEAD; dokumentierter Follow-up-Kandidat).

## FR-24: Pflicht-Skill-Routing für Mail-Desk-Batches

**Status:** ✅ Abgeschlossen (2026-09-24; MD-R9 im Kernel-Loop mit Subagenten,
Doku/Routing-only). Befund Batch 2026-W39/4 (boku-user): der Batch-Lauf
arbeitete ausschließlich über die Pipeline-SOP; `skills/mail-desk/SKILL.md`, die
Adapter-Referenz und `references/cli-operations.md` wurden nicht geladen. Folge:
drei Werkzeugregel-Verletzungen in einem einzigen Lauf - (1) Final-Index-Prüfung
per ad-hoc-JSON-Lesen statt ausschließlich kanonischem
`mail_desk_final_location_index.py`, (2) direkte `himalaya envelope list`-Aufrufe
statt des JSON-Manifest-Client `mail_desk_himalaya_client.py`, (3)
`projects.json`-Katalogedit inline per JSON-Roundtrip statt über das
`project-catalog-entry`-Muster.

### Kernergebnis

- **SKILL.md-Description:** die selbst-ausschließende Phrase "führt keine
  Massenpipeline aus" ist entfernt; Batch-/Stapelverarbeitung
  (batch pipelines, draft→execute→verify) läuft fachlich **durch** diesen Skill
  (Körper trägt die Batch-Verträge: Draft-Bindung expected_count/allow_fewer/
  Review-Hash, Final-Index-Hardrules, JSON-Manifest-Client, Katalogpflege-Router).
- **Kanonischer Pflicht-Ladeblock** (kopierbarer Migrationsbaustein, Reihenfolge
  bindend): 1. `skills/mail-desk/SKILL.md` vollständig, 2. gewählte
  Adapter-Referenz (`references/backends/himalaya.md`) vollständig,
  3. `references/cli-operations.md`, 4. bei Bedarf `references/batch-runner.md`,
  `references/folder-rules.md`, `references/log-schema.md`.
- **Consumer-Migrationsbaustein (workspace-owned, boku-user):** Phase 0
  "Pflicht-Referenzen" in `pipelines/mail-desk-batch.md` **vor** Phase 1
  (lädt den Pflicht-Ladeblock, bevor Kommandos laufen); Wording-Schärfung in
  `CONTEXT.md`/`AGENTS.md` von "Pipeline **oder** Fachanleitung" zu
  "**Pipeline und Fachvertrag**".

### Umsetzungsnachweis

- Bundle: `skills/mail-desk/SKILL.md`-Description (Doku/Routing-only, kein
  Produktionscode-Change).
- Doku-Contract-Test `skills/mail-desk/tests/test_skill_routing_contract.py`
  (5 Tests: Description-Anker, Pflicht-Ladeblock, Migrationshinweis);
  Suite 1037/1037 grün.
- Der vollständige Migrationsbaustein ist über die Git-Historie
  (`docs/features/FR-24.md` vor der Archivierung) nachvollziehbar.

## FR-23: Workspace-Lock-Delegation an Batch-Runner-Subprozesse

**Status:** ✅ Abgeschlossen (2026-09-24; MD-L1 im Kernel-Loop mit Subagenten,
1 unabhängiges Review). Befund Batch 2026-W39/4 Draft (Env 9451, boku-user):
die Anhang-Bewertung endete fail-closed `lock_unavailable`, obwohl ein
Workspace-Lock aktiv bestand — der Runner-Subprozess besaß die Lease nicht
und konnte die Agent-Session-Lease weder erkennen noch benutzen.

### Kernergebnis

- Runner akzeptiert `--workspace-lease-id` / `--workspace-conversation-id`
  (optional, nur valid mit `--draft`/`--inspect`, sonst `ArgumentParseError`
  mit Flag-Nennung).
- Flags befüllen `cfg["lease_id"]`/`cfg["conversation_id"]` nur bei gesetztem
  Flag; ohne Flags bleibt die Config byte-identisch (kein Verhaltenswechsel
  für Offline-/Test-Runs).
- Bestehende Threading-Kette unangetastet: draft/inspect →
  `install_draft_attachment_evaluations` → `evaluate_attachment` →
  `verify_workspace_lock` — Eigentumsprüfung (Lease-ID passt, Lease aktiv);
  fremde oder abgelaufene Leases bleiben fail-closed `lock_unavailable`;
  kein Fetch ohne Lock (auch mit Flag).
- Doku: `references/cli-operations.md` + `references/batch-runner.md`
  dokumentieren die Flags und die fail-closed-Semantik.

### Umsetzungsnachweis

- Doku-Contract-Test `skills/mail-desk/tests/test_runner_lease_delegation.py`
  (8 Tests: Flag-Threading draft/inspect, Byte-Identität ohne Flags,
  Reject außerhalb draft/inspect inkl. reconcile, fail-closed-Regression-
  Pins); Suite 1045/1045 grün.
- Unabhängiges Review: 1 Major (Red-Gate-Historie-Docstring) + 2 Minors —
  alle adjudiziert; Testkorrektur-Runde 1 (dokumentiert): Docstring korrigiert
  (5 von 8 roten am Red-Gate, 3 Source-Pins grün), ungenutzter Import
  entfernt, `tempfile_namespace`-Helfer nach oben gezogen; Help-Text-Divergenz
  des `--workspace-conversation-id`-Flags als bewusste Präzisierung
  adjudiziert (dokumentiert im Run-Manifest).

## Archivierungsregel

Ein FR wird erst hierher verschoben, wenn alle seine Submodule abgeschlossen,
unabhängig reviewt, getestet und committed sind. Teilweise abgeschlossene FRs
bleiben mit ihren erledigten und offenen Submodulen im aktiven Backlog.
