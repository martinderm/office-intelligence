# Office Intelligence — Paket System Map: Verben (Processes)

> **Typ**: ICM Form 6 (`system-map`), Dimension: Verben  
> **Ziel**: Beschreibung aller desk-übergreifenden Abläufe, Handoff-Punkte und Datentransformationen im Zusammenspiel der 8 Sub-Skills.  
> **Gültig für**: Repository Root & konsumierende Workspaces (relativ)

---

## 1. Übersicht der übergreifenden Workflows

Die Sub-Skills arbeiten nach dem Prinzip modularer Stufenübergänge:

```
[E-Mail / Himalaya] ──► (mail-desk) ──────┬──► [Quarantäne / data/]
                                         ├──► [Dossier / memory/evidence/mail/]
                                         └──► (task-desk) ──► [Action Items]
                                                  │
[Meeting / Adapter] ──► (meeting-desk) ───────────┤
[Event / Adapter]   ──► (event-documentation) ───┘
                                                  │
[CalDAV Server]     ──► (calendar-desk) ──────────┤──► [memory/evidence/calendar/ & data/]
                                                  │
                                                  ▼
                                      (project-catalog-entry)
                                      (topic-catalog-entry)
                                                  │
[Cloud Storage]     ──► (cloud-atlas) ────────────┴──► [memory/cloud/ & references/]
```

---

## 2. Handoff-Workflow: Mail-Desk → Task-Desk

Die Bearbeitung von E-Mails und die Erfassung von Aufgaben sind strikt getrennt:

1. **Intake & Triage (`mail-desk`):**
   - E-Mail wird eingelesen, klassifiziert und fachlich bewertet.
   - Falls Dateianhänge existieren: Durchlaufen der Quarantäne- und Dispositions-Pipeline (MD-Q1/MD-Q2/MD-Q3: Extraktion, Indizierung, Entscheidung/Promotion-Link/Discard).
   - `mail-desk` erstellt eine Fallakte (Dossier) und markiert identifizierte Handlungsbedarfe.
2. **Handoff an `task-desk`:**
   - Der Agent übergibt die identifizierten Punkte an `task-desk`.
   - `task-desk` prüft auf Duplikate, priorisiert nach Dringlichkeit/Impact und bereitet die Synchronisation mit dem Task-Tracker (z. B. Todoist) vor.
   - `mail-desk` selbst legt **keine** Todos im Aufgaben-Tracker an.

**Zweiter Batch-Eingang (Issue #16, doc-only):** Neben dem bestehenden
`dossier_handoff` (unverändert; EVID-Anker `mail_message_id`, `action_candidates`,
`state: review_and_dedupe_required`) konsumiert `task-desk` den
`mail-desk`-`synthesis_handoff` **nur** über das quellengebundene Release-Gate:
`completion_report.status: "completed"` **und** `handoff_released: true`, dessen
`verified_message_ids` alle Quell-IDs decken, plus passender freigegebener
`data.synthesis_handoff` mit `status: "pending"`. Erzeugt wird der Report
ausschließlich über die bestehenden Seams `core/completion.py`, `modes/verify.py`
und `modes/reconcile.py` (Vertragsreferenz:
`skills/mail-desk/references/synthesis.md` §Synthese-Handoff/§Completion-Gate);
ein bare `pending` oder ein `results: success`
genügt **nicht**. Bei der ersten Kandidatenaufnahme vergibt `task-desk` eine
persistierte, opake `action_id` (UUID4) und schreibt `review-queue.json` (inkl.
nichtleerem `title` und geprüftem `evidence_ref={file,anchor}`); die
Wiedererkennung erfolgt ausschließlich über `action_ref` mit exakt passender
`source`. Reply-Kandidaten stammen aus `data/mail-desk/replies-needed.jsonl` und sind
nur bei `reply_status: "needed"` mit passender normalisierter Quelle im freigegebenen
`synthesis_handoff` zulässig (nur dieser zweite Eingang; `dossier_handoff` bleibt ein
separater Vertrag). Register-Zielverträge:
[`../../skills/task-desk/references/task-schemas.md`](../../skills/task-desk/references/task-schemas.md).

**Post-Batch-Synthese mit Kontext-Konsultation (Issue #64):** Nach dem freigegebenen
`synthesis_handoff` (`status: "pending"`, quellengebundener `completion_report`)
konsultiert das LLM je berührtem Projekt/Topic zuerst den kanonisch deklarierten
Kontext in fester Reihenfolge — (1) Event-Dossier (bei Projekten die
`events/<slug>/index.md`-Ablage, bei Topics/Subtopics die katalogdeklarierte
`subtopics[].events[]`-Referenz), (2) Subtopic-Spezialnotiz bzw.
Operations-/Projekt-/Topic-Steuerdatei, (3) deklarierte `cloud_sync`-Maps — und
entscheidet erst danach je Befund `updated`, `linked`,
`noop` oder `review_required`. Geschrieben wird nur das Single Home (Event-Dossier >
Subtopic-Notiz > Steuerdatei); das Evidenzlog bleibt Chronologie und erhält den
`Message-ID`-Anker. Der Bericht führt je Kontext einen Konsultationsnachweis
(`context`, `canonical_file`, `message_ids`, `outcome`, `reason`). Der Runner selbst
schreibt weder Wissensdateien noch löst er Cloud- oder Task-Mutation aus. Vertrag:
[`skills/mail-desk/references/synthesis.md`](../../skills/mail-desk/references/synthesis.md).

---

## 3. Meeting- & Event-Intake-Workflow

**Mail → Event, Issue #8:**
`skills/mail-desk/scripts/mail_desk_event_dossier.py` komponiert read-only
`plan → review → apply → handoff → report` über `core/event_dossier.py`.
Explizite Such-/Inspect-Bounds, Locator-/Account-/Hash-Prüfung und geordnete
innere/äußere Approval-Bindungen gehen der operativen Übergabe voraus.
`apply` liefert nur `batch_request`, `dispatch_required: true`, `applied: false`;
Ausführung und Live-Katalog-/Lock-Prüfung bleiben beim bestehenden Batch-Owner.
Nur vollständige, quellenidentische Execute-/Verify-Ergebnisse erlauben den
`event-documentation`-Work-Order (`pending_content_review`). `report` prüft
Source-/Work-Order-/Output-Hashes und alle receiptierten Dateien im exakten
Projekt-/Event-Scope; nie Content-Approval oder Vollständigkeit behaupten.
Der separate Event-Writer (GitHub #14) führt nach vollständigem Katalog-/Source-/
Lock-/Pfad-/Konflikt-Preflight lokale No-Clobber-Writes aus. Projekte und
Topic/Subtopic-Events sind unterstützt. **Normative Bootstrap-Ausnahme:** Ein
gültig katalogisiertes Topic-Event darf sein fehlendes `index.md` erstmals als
ausdrücklich ungeprüften mailbasierten Entwurf erhalten; vorhandene menschliche
Dossiers werden nie überschrieben, Matcher-Anforderungen bleiben unverändert.
Vollständiger gleicher Dateisatz → unveränderte Wiederholung; Teilbestand oder
Staging-Reste → Stop, keine automatische Recovery. Echte lokale Integration:
Fassaden-Handoff → Writer → Report (operatives Mailbox-Apply davor synthetisch);
Topic-Writer → Report ohne erfundenen Topic-Handoff. Verträge:
[Writer](../../skills/event-documentation/references/event-writer.md) und
[`event-dossier.md`](../../skills/mail-desk/references/event-dossier.md).

1. **Intake (`meeting-desk`):**
   - Adapter (Fireflies, TicNote, Zoom, Vimeo/VTT) transformieren API-Daten offline
     in genau einen `meeting-record/1`-Record; sie schreiben keine Meeting-Dateien
     und kein Register. Manuelle/lokale Dateien nutzen `--local --summary/--transcript
     [--raw]` mit demselben Record/Writer (Plan/Dry-run als Default, Apply nur mit
     Plan-Hash; Quellen werden erst nach Register-Publikation hashgebunden verschoben).
   - Grenzen (Issue #49): `--record` 512 KiB Zeichen je Artefakt / 1 MiB je Record,
     `--local` 4 MiB UTF-8-Bytes je Quelldatei; große Raws werden in `data/inbox/`
     gestaged und per `--local` eingeliefert. Bei > 4 MiB nicht splitten, keinen Stub
     registrieren, nicht manuell in die Evidence-Zone schreiben: Quelle behalten,
     Größe/SHA-256 melden, Owner entscheiden lassen. `--event` muss vor dem ersten
     Ingest in `events[]` des Projekt-/Topic-Katalogs stehen.
   - Trennung nach Granularität:
     - Einzel-Meeting, Call oder Vortrag → [`meeting-desk`](../../skills/meeting-desk/SKILL.md)
     - Mehrtägige Tagung, Konferenz mit Vortragsprogramm → [`event-documentation`](../../skills/event-documentation/SKILL.md)
2. **Evidenz-Erzeugung:**
   - Der kanonische Writer validiert Record-Version/-typen/-grenzen, löst die
     vertrauenswürdige Katalogauswahl (`--topic`/`--project`/`--event`) auf und
     publiziert Summary/Transkript/Raw unter
     `memory/evidence/{projects|topics}/<id>/meetings/` bzw.
     `*/events/<slug>/meetings/`; ohne Auswahl neu `unmapped` unter
     `memory/evidence/meetings/unrouted/`.
   - Register ist ein Objekt `{"meetings": [...]}`; Root-Extras und nicht
     betroffene/legacy Einträge bleiben erhalten. Nackte Arrays, defektes JSON
     und mehrdeutige Identitäten stoppen vor Writes; keine stille Migration.
   - Journal/Ownership unter `data/meeting-desk/`; neue Dateien no-clobber,
     generierte Updates hash-gebunden, Raw unveränderlich, Register zuletzt.
     `null` in Record-Artefakten bedeutet nicht geliefert, nie Löschen: nicht
     gelieferte registrierte Pfade/Bytes (inkl. Raw/Clean) bleiben erhalten; ein
     explizites Rerouting ohne vollständigen Snapshot wird vor Mutation
     abgewiesen.
     Unbekannte Journal-Verzeichnis-Einträge/Stage-Orphans blockieren mit Exit 3
     und bleiben bytegenau erhalten. Ein offenes Journal blockiert Mutationen
     mit Exit 3 (`recovery_required`); `--inspect` ist read-only, `--recover`
     explizit/lease-gebunden/forward-only. Die Lease wird vor jedem mutierenden
     Schritt erneut geprüft; stdout ist UTF-8-bytesicher.
   - Transkript-Bereinigung mit Provenienz (Issues #30/#32, Skill §2b): [Builder-Vertrag](../../skills/meeting-desk/scripts/README.md) — `*.transcript.raw.md` plus geprüfte Korrekturdatei (`meeting-corrections/1`) und gleiche Metadatenoptionen ergeben reproduzierbar `*.transcript.md`. Operationen treffen ausschließlich Originaltext; Überschneidungen werden abgewiesen. Inline-Paare brauchen Workspace-Belege; stille Regeln sind auf bedeutungsneutrale Schreibweisen beschränkt, sonst `[unklar]`. Der Builder publiziert nur über den Writer auf den registrierten `transcript_path` des eindeutig identifizierten Meetings; `--check` ist read-only. **Issue #52 P7:** `ref` in `pairs`/`regex_pairs`/`rules` darf auf ein existierendes Dokument unter `memory/references/` oder dem reinen Leseort `memory/operations/` zeigen (optional mit `#Anker`); fehlende Dateien, Traversal, absolute Pfade und ausbrechende Symlinks werden abgewiesen, Schreibzonen bleiben die vier Data Zones. **Issue #52 P8:** Die Bereinigung ist der Builder-Lauf (`--raw --corrections --out`); `--check` prüft danach ausschließlich die byte-identische Reproduzierbarkeit (Exit 1 = Drift) mit denselben Metadatenoptionen und ersetzt den Lauf nicht; danach folgt der Summary-Review, den Abschluss bildet der manuelle EVID-Eintrag nach SKILL §4. Pflicht sind die drei Schritte auch ohne Korrekturbedarf, keine Wrapper-Pflicht. [Tests](../../skills/meeting-desk/tests/test_ingest_meeting.py) sichern diese Grenzen mit synthetischen Daten; die [L2-Karte](../../skills/meeting-desk/docs/system-map/README.md) hält Zustände/Journal/Metriken.
3. **Katalog-Verknüpfung:**
   - Zuordnung des Meetings zu einem Projekt (`projects.json`) oder Thema (`topics.json`).
   - Action Items werden wie bei Mail an den `task-desk` übergeben.

---

## 4. Cloud-Atlas Workspace-Integration

1. **Filemap-Generierung:**
    - `cloud-atlas` erfasst Cloud-Verzeichnisse rekursiv und erzeugt `filemap.json`.
    - Projekt-, Topic- und Subtopic-Storages können mit `cloud_sync.<storage_id>.exclude_dirs`
      literale scan-relative Verzeichnisse ausnehmen (Issue #2). Beide Scanner nutzen
      `skills/cloud-atlas/scripts/core/scan_scope.py`: Validierung und Pruning vor
      Hashing/Konvertierung, effektive Ausschlüsse in der Scan-Provenienz.
2. **Kuratierung & Konvertierung:**
   - Binärformate (Word, Excel, PowerPoint) werden via Pandoc / LibreOffice nach Markdown übersetzt.
   - Bildbasierte PDFs durchlaufen OCR (Default: `local_derivative`).
   - Gefüllte PDF-AcroForm-Felder werden optional über PyMuPDF rein lesend extrahiert
     (Issue #20) und mit begrenztem Status/Warnungen in `files[<quelle>].form_fields`
     persistiert; fehlt PyMuPDF, bleibt der Mirror unverändert. Bei `enrich_source`
     werden die Felder vor der destruktiven In-Place-OCR gelesen.
3. **Spiegelung in den Workspace:**
   - Ablage unter `memory/cloud/projects/<project-id>/` bzw. `memory/cloud/topics/<topic-id>/`.
    - Das Projekt-Desk verlinkt diese lokalen Spiegel in den Projekt-Notizen (`memory/references/projects/<id>/`).
    - Bei geänderten Ausschlüssen bleiben Alt-Artefakte erhalten, verschwinden aber
      aus dem Inventar. Cleanup mit Ausschlüssen verlangt vorherige Ownership durch
      eine Quelle im verbleibenden Scope; unbekannte Artefakte bleiben unangetastet.
      Überlappende Quellwurzeln nutzen getrennte Output-Wurzeln und Filemaps.

---

## 5. Katalog-Mutations-Lifecycle

Für Änderungen an den zentralen Registern (`projects.json`, `topics.json`) gilt ein strikter 4-Schritte-Zyklus:

```
[1. Lock anfordern] ──► [2. Schema-Validierung] ──► [3. Atomarer Replace] ──► [4. Lock freigeben]
  (workspace-lock)        (validate_projects.py)      (tempfile + os.replace)    (workspace-lock)
```

1. **Lock anfordern:** Exklusive Lease über `workspace-lock` sichern.
2. **Vorab-Validierung:** Skript prüft semantische Gültigkeit, Pflichtfelder und ID-Eindeutigkeit im Speicher.
3. **Atomarer Write:** Schreiben in temporäre Datei auf demselben Dateisystem, gefolgt von `os.replace`. Bei Abbruch bleibt der alte Stand unbeschädigt.
4. **Lock freigeben:** Freigabe der Lease für nachfolgende Harnesses.

---

## 6. Sub-Skill-spezifische Lifecycles (Verweise)

Die detaillierten Abläufe der beiden komplexen Subsysteme sind in den jeweiligen L2-Prozesskarten dokumentiert:

* **Mail-Desk Deep Dive:** [`../../skills/mail-desk/docs/system-map/processes.md`](../../skills/mail-desk/docs/system-map/processes.md)  
  *(Himalaya-Client-Flow, Mailbox-Preflight, Quarantäne-Engine MD-Q1/MD-Q2 inkl. bounded read-only Tracked-Quarantäne-Preflight `quarantine_preflight.py` FR-15/MD-E1-T02, Disposition, Verifiable Receipts & Discard Recovery Journal MD-Q3, policygebundene Anhang-Evaluierung `attachment_evaluate` mit linearer Fetch/Extraktions/Handoff-Komposition (`completed`/`handoff_ready` bzw. `still_ambiguous`) FR-15/MD-E1-T05, fail-closed Fehler-/Reason-Matrix FR-15/MD-E1-T06, MD-E1-Paketabnahme mit hermetischem End-to-End-Nachweis und Zero-Write-Garantie FR-15/MD-E1-T07, standardmäßig aktive `draft`-Integration mit einmaliger `untrusted_external`-Neuklassifikation und additivem `attachment_evaluation` FR-15/MD-E2-T01, fail-closed-Härtung mit kanonischer Handoff-Revalidierung, `still_ambiguous`-Erhalt, AST-/Katalog-/Hash-gebundener `classifier_revision` und deterministischer `already_fetched`-Idempotenz FR-15/MD-E2-T02, opt-in `inspect`-`manifest_proposal` mit wiederverwendetem Item-Flow und `manifest_file`-Grenze
  FR-15/MD-E2-T03, MD-E2-Paketabnahme mit hermetischem Real-Pfad-Nachweis bis zum
  persistierten Projekt-`DraftManifest` FR-15/MD-E2-T04, Read-only Reconcile,
  Basename-Contract-Reconcile Issue #9: Raw-MIME-`original_filename`-Provenienz
  ohne Sanitization-Launderung, fail-closed Alias-/Traversal-Validierung vor I/O,
  Dossier-Synthese, sowie die gebundene Draft-Diagnose Issue #19: opt-in/Default-aus,
  geschlossener Request plus separater hashgebundener Review-Input, Einmal-Marker vor
  dem einzigen `message save`, fester Kindfilter und begrenzter fail-closed Parser;
  lokale Injection-Barrier Issue #62: deterministischer Mail-Ingress-Prefilter mit
  Fencing, Quarantäne-Evidence unter `data/mail-desk/injection-quarantine/` und
  hash-gebundenem `injection_quarantine_present`-Gate, L2 §39)*
* **Cloud-Atlas Deep Dive:** [`../../skills/cloud-atlas/docs/system-map/processes.md`](../../skills/cloud-atlas/docs/system-map/processes.md)  
  *(Filemap-Scan, Dokumentenkonvertierung, OCR-Verzweigung `local_derivative` vs. `enrich_source`, optionale AcroForm-Formularfelder-Extraktion mit Capture-vor-OCR-Reihenfolge Issue #20, Mirror-Sync)*

---

## 6. Calendar-Desk CalDAV & Zwei-Phasen-Write-Lifecycle (Paket CD-1 bis CD-4a)

1. **CalDAV Discovery & Agenda-Abfrage:**
   - Der Client führt `PROPFIND Depth: 1` auf der konfigurierten `principal_url` aus, um verfügbare Kalendersammlungen (`collection_url`), Anzeigenamen und Eigenschaften aufzulösen.
   - `REPORT` mit `calendar-query` und `time-range` filtert Ereignisse serverseitig in einem definierten Zeitfenster.
2. **Free/Busy-Berechnung & Slot-Suche:**
   - Termine aus allen aktiven Kalendern werden aggregiert und nach Startzeit sortiert.
   - Ganztages- und Zeitzonentermine werden kanonisch nach UTC normalisiert.
   - `find-slots` berechnet freie Zeitfenster unter Berücksichtigung von Arbeitszeiten (z. B. 08:00–18:00 UTC), Mindestdauer und Pufferzeiten (Buffer). Bei unvollständigen Daten (`skipped[]` nicht leer oder `truncated: true`) brechen `compute_freebusy` und `find-slots` fail-closed mit `CALDAV_INCOMPLETE_FREEBUSY` (Exit 1) ab.
3. **Zwei-Phasen-Write-Engine (`plan` ──► `apply`):**
   - **Phase 1 (`plan`):** Validiert Eingaben, serialisiert den RFC-5545-Payload mit Fencing, berechnet den Content-SHA256, generiert und persistiert ein diff-fähiges Plan-Manifest (`data/calendar-desk/proposals/<plan_id>.json`) und legt die ETag-Precondition fest (`If-None-Match: *` bei Neuanlage, `If-Match: "<etag>"` bei Update/Delete). Es erfolgen **keine** mutierenden Netzwerkaufrufe (Exit 0 `Completed`, kein Lock erforderlich).
   - **Phase 2 (`apply`):**
     1. Verifiziert die exklusive `workspace-lock`-Lease (`allow_legacy=False`) sowie `write_policy == "dry_run_and_approval"`.
     2. Öffnet ein atomares Recovery-Journal unter `data/calendar-desk/journal/<plan_id>.json` (`intent` -> `in_flight`).
     3. Führt die HTTP-Mutation (PUT / DELETE) unter Übergabe der ETag-Precondition aus.
     4. Bei HTTP 412 (Precondition Failed), Konflikt, `CALENDAR_WRITE_FORBIDDEN` oder Precondition-Verletzung: Bricht fail-closed ab (Exit 2 `PreconditionFailed` / `Failed`), ohne fremde Änderungen zu überschreiben.
     5. Bei unterbrochenem Schreibvorgang, HTTP 5xx/408/429 oder Netzwerkfehler: Mappt auf `unknown_remote_outcome` und das Journal bleibt aktiv (Exit 3 `RecoveryRequired` / `Failed`).
     6. Bei allgemeinen Fehlern: Bricht ab (Exit 1 `Failed`).
     7. Bei Erfolg: Aktualisiert das Evidenzregister (calendars.json & raw/) und setzt das Journal auf completed (Exit 0 `Completed`).
4. **Recovery-Workflow:**
   - `calendar_desk_client.py inspect`: Prüft das Vorhandensein offener Journale rein lesend.
   - `calendar_desk_client.py recover`: Führt rollbacks oder Nachsynchronisationen unter Lease-Schutz durch.
5. **mail-desk .ics Bridge:**
   - Eingehende `.ics`-Anhänge werden in `mail-desk` über `core.calendar_extract.extract_calendar_objects` extrahiert.
   - Die Objekte folgen bit-identisch dem 4-Key-Schema von `event_dossier.py` (`{"uid", "sequence", "recurrence_id", "content_sha256"}`).

