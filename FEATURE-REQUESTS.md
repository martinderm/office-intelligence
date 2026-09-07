# Feature Requests — Office Intelligence & Mail-Desk

Dieses Dokument fasst die in der Session ab 01.09.2026 erarbeiteten Architektur- und Funktionserweiterungen für das Repository `office-intelligence` (insbesondere die Skills `project-catalog-entry` und `mail-desk`) zusammen. Der Status wurde am 07.09.2026 gegen den Session-Ausgangspunkt `fb9ba3e5` und den geprüften Abschlussstand `96ef354f` abgeglichen.

## Statusabgleich zur Session

| ID | Status | In dieser Session umgesetzt | Kurzurteil |
| --- | --- | --- | --- |
| `FR-01` | 🟠 geplant, nicht implementiert | Nur Backlog und Skill-Verweis (`73e86e6`) | Schema, Templates, Validator und Migration fehlen weiterhin. |
| `FR-02` | ⬜ offen | Nein | Es gibt weiterhin nur Projekt-Root-Matching und ein auf 30 Zeilen begrenztes Preview; keine hierarchische Artefaktauflösung und keine Full-Body-Eskalation. |
| `FR-03` | 🟡 teilweise, bereits vor der Session | Nein, abgesehen von einer redaktionellen Frontmatter-Korrektur | Subtopics besitzen bereits Aliase, Keywords, Kontakte und optionales `cloud_sync`; der Classifier konsumiert jedoch nur Aliase und Keywords und gibt keinen Subtopic-Treffer aus. |
| `FR-04` | ⬜ offen | Nein | Kein `dossier`-Modus, kein `batch-dossier.json` und keine kataloggestützte Dossier-Suche vorhanden. |
| `FR-05` | 🟡 Vorarbeiten umgesetzt | Ja, aber nicht die Zielstruktur | Envelope-/Common-Helper, atomare Writes und die Progressive-Disclosure-Dokumentation wurden verbessert; die Modusfunktionen liegen weiterhin im 1.468-zeiligen Runner. |

`🟠` bezeichnet dokumentierte Planung ohne Funktionsimplementierung, `🟡` eine belastbare Teilgrundlage und `⬜` ein noch nicht begonnenes Ziel im beschriebenen Scope.

---

## Inhaltsübersicht

1. [FR-01: Schema-Erweiterung für `projects.json` & `project-catalog-entry`](#fr-01-schema-erweiterung-für-projectsjson--project-catalog-entry)
2. [FR-02: Hierarchische WP-/Deliverable-Erkennung & Full-Body-Eskalation in `mail-desk`](#fr-02-hierarchische-wp-deliverable-erkennung--full-body-eskalation-in-mail-desk)
3. [FR-03: Subtopic-Strukturierung & Signalisierung in `topics.json`](#fr-03-subtopic-strukturierung--signalisierung-in-topicsjson)
4. [FR-04: Thematischer Dossier-Modus / Projekt-Fokus-Workflow (`batch-dossier.json`)](#fr-04-thematischer-dossier-modus--projekt-fokus-workflow-batch-dossierjson)
5. [FR-05: Modulare Reorganisation des Batch-Runners (`scripts/core/modes/`)](#fr-05-modulare-reorganisation-des-batch-runners-scriptscoremodes)

---

## FR-01: Schema-Erweiterung für `projects.json` & `project-catalog-entry`

**Status:** 🟠 Geplant, nicht implementiert. In dieser Session wurden mit Commit `73e86e6` lediglich `skills/project-catalog-entry/TODO.md` und der Verweis darauf im Skill ergänzt. Das produktive Schema, die Vorlagen und der Beispielkatalog verwenden weiterhin die flache Workpackage-Struktur.

### Problemstellung
In [`projects.json`](../../boku-user/memory/references/projects/projects.json) werden Workpackages bisher flach geführt. Tasks (z. B. `T1.7`), Deliverables (z. B. `D1.2`) und Milestones (z. B. `MS1`) liegen unstrukturiert als Strings in `"aliases"`.
- Dem Skill [`project-catalog-entry`](skills/project-catalog-entry/SKILL.md) fehlt ein normatives Schema für Teilaufgaben und Meilensteine.
- Es ist maschinell nicht ersichtlich, welche Aufgaben bei der BOKU liegen (`boku_role`, z. B. *Co-Lead Quality*) und welche Partner welche Deliverables verantworten.

### Ziel-Spezifikation

1. **Strukturierung von `workpackages`:**
   - Jedes WP erhält strukturierte Listen für `tasks` und `deliverables`.
   - Felder für `lead` und `boku_role`.
2. **Milestones auf Projektebene:**
   - Meilensteine spannen oft über mehrere WPs und markieren Phasenabschlüsse des Gesamtprojekts. Sie liegen daher direkt auf Projektebene (`milestones: [...]`) mit Verweisen (`related_wps`) und optionalen Vorbedingungen (`prerequisites`).

```json
{
  "id": "meshe",
  "title": "MESHE – Microcredentials Exchange System for Higher Education in Europe",
  "kuerzel": "MESHE",
  "mailbox_folder": "Projekte/MESHE",
  "workpackages": [
    {
      "id": "wp1",
      "number": 1,
      "title": "Project Management, Coordination, Quality and Evaluation",
      "lead": "EUCEN",
      "boku_role": "Co-Lead Quality",
      "status": "active",
      "tasks": [
        {
          "id": "T1.7",
          "title": "Drafting the Quality and Evaluation Plan",
          "lead": "BOKU",
          "keywords": ["Quality Plan", "QM Plan", "Handbook", "Evaluation Plan"]
        },
        {
          "id": "T1.8",
          "title": "Individual Risk Management Grid",
          "lead": "BOKU",
          "keywords": ["Risk Management Grid", "Risk Grid", "Risikoanalyse"]
        }
      ],
      "deliverables": [
        {
          "id": "D1.2",
          "title": "Quality and evaluation plan, including tools IPR and reports",
          "lead": "BOKU",
          "type": "R — Document, report",
          "due_month": "Ongoing, M1–M36"
        }
      ]
    }
  ],
  "milestones": [
    {
      "id": "MS1",
      "title": "Kick-off meeting held",
      "due_month": "M2",
      "related_wps": ["wp1"],
      "prerequisites": "Grant Agreement signed"
    },
    {
      "id": "MS5",
      "title": "Quality Plan approved by consortium",
      "due_month": "M6",
      "related_wps": ["wp1"],
      "prerequisites": "Draft Quality Plan delivered (D1.2)"
    }
  ]
}
```

### Migrations-Bedarf
- Erstellung eines Backfill-Skripts (`skills/project-catalog-entry/scripts/migrate_project_wps.py`), das vorhandene Markdown-Workpackages unter `memory/references/projects/*/workpackages/*.md` parst und strukturiert in `projects.json` überführt.
- Aktualisierung von [`skills/project-catalog-entry/SKILL.md`](skills/project-catalog-entry/SKILL.md) und der Vorlagen unter `skills/project-catalog-entry/references/`.

### Kommentar und Schärfung

Die Erweiterung ist sinnvoll und Voraussetzung für FR-02. Vor der Implementierung muss jedoch eine Inkonsistenz bereinigt werden: Dieses Dokument ordnet `milestones` auf Projektebene an, während `skills/project-catalog-entry/TODO.md` sie derzeit innerhalb eines Workpackages zeigt. Projektebene plus `related_wps` ist für WP-übergreifende Meilensteine das robustere Modell.

Das Migrationsskript gehört als wiederverwendbares Skill-Werkzeug unter `skills/project-catalog-entry/scripts/`, nicht unter `scratch/`. Es sollte zunächst nur `--dry-run`-Diffs erzeugen, idempotent sein, atomar schreiben und die bestehende `schema_version` bewusst migrieren. Vor dem Backfill sind ein formales Katalogschema und Fixture-Tests für mindestens ein EU-Projekt sowie ein Projekt ohne Workpackages nötig.

---

## FR-02: Hierarchische WP-/Deliverable-Erkennung & Full-Body-Eskalation in `mail-desk`

**Status:** ⬜ Offen. Der aktuelle Classifier wertet bei Projekten ausschließlich Root-Felder aus. `tasks`, `deliverables` und `milestones` werden nicht gelesen. `get_single_email_details()` ruft zwar `himalaya message read --preview` auf, schneidet den Body aber weiterhin auf standardmäßig 30 Zeilen ab; eine zweite, signalgesteuerte Volltextstufe fehlt.

### Problemstellung
Derzeit matched [`scripts/core/classifier.py`](skills/mail-desk/scripts/core/classifier.py) Mails nur gegen Root-Metadaten von Projekten. Wird eine Mail wie *„Mesche QM Plan Handbook 1. Draft“* verarbeitet, entsteht lediglich ein generischer Evidenzeintrag (*„Projektbezogene Abstimmung zu MESHE“*), ohne Bezug zu Deliverable `D1.2` oder Task `T1.7`. Zudem werden standardmäßig nur die ersten 30 Zeilen Preview geladen.

### Ziel-Spezifikation

1. **Hierarchisches Matching (`classifier.py`):**
   - Auswertung der neuen `tasks`-, `deliverables`- und `milestones`-Objekte im Speicher.
   - Erkennung von Codes (`T1.7`, `D1.2`, `MS5`) sowie zugehörigen Fachbegriffen im Betreff und Mailtext.
2. **Programmatische Lesegrad-Eskalation (`Full Body`):**
   - Sobald Artefakt-Signale (*„QM Plan“*, *„Draft“*, *„Handbook“*, *„Deliverable“*, *„Agreement“*, *„Red Flags“*, *„Audit“*) oder Handlungsbedarfe (`needs_reply: true`) vorliegen, ruft der Runner automatisch `himalaya message read <id>` (Volltext) ab.
3. **Präzise Evidenz-Generierung:**
   - Deterministische Strukturierung des Monatslogs:
     ```markdown
     - 2026-02-17 — Mesche QM Plan Handbook 1. Draft.
       - Message-ID: `69949823020000f1000ce9b8@gwia1.boku.ac.at` (Christina Paulus)
       - Kontext: [MESHE | WP1 / T1.7 / D1.2 (Quality and Evaluation Plan, Lead: BOKU)]
       - Aussagekern: BOKU legt ersten Entwurf für D1.2 / T1.7 (QM Plan Handbook) vor.
     ```

### Kommentar und Schärfung

FR-02 sollte erst nach dem stabilen Schema aus FR-01 umgesetzt werden. Das Matching sollte exakte Codes (`T1.7`, `D1.2`, `MS5`) höher gewichten als freie Keywords und bei Mehrdeutigkeit mehrere Kandidaten ausgeben, statt einen scheinbar sicheren Treffer zu erfinden. Der erkannte Kontext sollte zusätzlich strukturiert im Decision-/Evidence-Objekt stehen; die Markdown-Zeile allein ist keine gute Maschinenschnittstelle.

Die Full-Body-Logik sollte als klarer Zwei-Pass-Flow gebaut werden: Preview klassifizieren, definierte Signale oder unzureichende Evidenz feststellen, dann genau diese Nachricht vollständig lesen und erneut klassifizieren. `needs_reply` allein ist als Trigger zu zirkulär, weil es zunächst selbst aus dem gekürzten Inhalt abgeleitet wird. Contract-Tests sollten außerdem sicherstellen, dass Volltext nur bei den dokumentierten Triggern geladen wird und ein Read-Fehler keine unvollständige Aussage als gesichert protokolliert.

---

## FR-03: Subtopic-Strukturierung & Signalisierung in `topics.json`

**Status:** 🟡 Teilweise vorhanden, aber nicht in dieser Session funktional erweitert. Das bestehende Topic-Schema enthält bereits `subtopics[].aliases`, `keywords`, `contacts`, `cloud_sync` und `status`; der Classifier übernimmt daraus derzeit nur Aliase und Keywords in das Parent-Topic-Matching. Subtopic-Kontakte, eigene Betreffmuster und die Identität des getroffenen Subtopics werden nicht verarbeitet. Event-Unterordner sind bereits dokumentiert, ein normativer `operations/`-Vertrag fehlt.

### Problemstellung
Themen (Topics) sind Linien-, Dauer- und Betriebsaufgaben ohne starre EU-Workpackages. Bisher sind `subtopics` in [`topics.json`](../../boku-user/memory/references/topics/topics.json) häufig leere Arrays ohne funktionale Routing-Signale, was zu unpräziser Klassifikation führt.

### Ziel-Spezifikation
Strukturierte Anreicherung der Subtopics mit eigenen Signal-Attributen:
- `keywords`: Spezifische Fachbegriffe (z. B. *„stundensaldo“*, *„argedata“*, *„zeiterfassung“*).
- `typical_subject_patterns`: Typische Betreff-Muster (z. B. *„Übertragung Stundensaldo“*, *„[Ticket#...“*).
- `contacts`: Funktionsadressen und zuständige Kolleg:innen (z. B. `h17700_argedata@boku.ac.at`).
- Saubere Trennung von Subtopics, Events/Konferenzen (`events/<slug>/`) und Dauerprozessen (`operations/`).

### Kommentar und Schärfung

Die Idee ist fachlich stimmig, sollte aber das bereits etablierte Feld `typical_subject_patterns` verwenden und nicht mit `typical_patterns` ein zweites Vokabular einführen. Der Classifier sollte einen Subtopic-Treffer als strukturierten Kontext (`topic_id`, `subtopic_id`, Match-Gründe) zurückgeben; bloßes Zusammenführen aller Signale auf Parent-Ebene verliert genau die gewünschte Präzision.

Für `contacts` ist zu definieren, ob sie Root-Kontakte ergänzen oder auf das Subtopic einschränken. `operations/` sollte erst nach einer kurzen fachlichen Definition eingeführt werden: Dauerprozess, Ablageobjekt und Evidenzpfad müssen klar von einem Subtopic und einem Event unterscheidbar sein. FR-03 ist klein genug für ein eigenes Paket aus Schema/Template, Classifier und gezielten Tests.

---

## FR-04: Thematischer Dossier-Modus / Projekt-Fokus-Workflow (`batch-dossier.json`)

**Status:** ⬜ Offen. Im Runner existieren nur `inspect`, `draft`, `sync_sent`, `execute`, `verify`, `pipeline`, `search` und `resolve`. Es gibt weder einen Dossier-Manifestkandidaten noch Dossier-spezifische Handler oder Tests.

### Problemstellung
Bisher erfolgt die Abarbeitung der Mailbox rein chronologisch (Tag für Tag über alle Themen gemischt). Wenn der Nutzer jedoch gezielt an einem Projekt (z. B. `MESHE`) arbeiten möchte, müssen alle dazu gehörenden Mails aus der `INBOX` extrahiert, verarbeitet und die entsprechenden Projektunterlagen vorab synchronisiert werden.

### Ziel-Spezifikation

```mermaid
flowchart LR
    A["Harvest<br>(INBOX Filter)"] --> B["Triage & Transfer<br>(Move, Index, Evidenz)"]
    B --> C["Memory-Sync<br>(Statusampel, Signale)"]
    C --> D["Action Handover<br>(Cloud-Docs, Word-Edit)"]
```

1. **Manifest-Format (`batch-dossier.json`):**
   ```json
   {
     "mode": "dossier",
     "project": "meshe",
     "source_folder": "INBOX",
     "auto_query_from_catalog": true,
     "max_count": 50,
     "actions": {
       "route_and_index": true,
       "flush_evidence": true,
       "sync_project_references": true,
       "check_todos": true
     },
     "delete_input_on_success": true
   }
   ```
2. **Katalog-gestützter IMAP-Filter:**
   - Der Runner baut aus den Projektmetadaten (Kürzel, Aliase, Domains `@eucen.eu`, Kontakte) automatisch eine optimierte IMAP-Suche auf (`SEARCH (OR (SUBJECT "MESHE") (FROM "eucen.eu"))`).
3. **Integrierter Synthese-Schritt:**
   - Nach dem Verschieben der Mails aktualisiert der Agent automatisch:
     - `statusampel-*.md`
     - `signals.md` (Fristen, Risiken)
     - `contacts.md`
4. **Übergabe in die Arbeitsdokumente:**
   - Ausgabe eines Dossier-Briefings mit direkten Links zu betroffenen Dateien im konfigurierten Cloud-Atlas-Mirror (z. B. `memory/cloud/projects/<project>/<storage_id>/...`) via passendem Dokument-Skill.

### Kommentar und Schärfung

Das Ziel ist nützlich, aber als einzelnes Paket zu groß und überschreitet mehrere Skill-Grenzen. Sinnvoll sind mindestens vier getrennte Schritte: kataloggestützte Suche und Manifest, kontrolliertes Routing/Indexing, reviewbare Synthese sowie Übergabe an `cloud-atlas` und `task-desk`. Der Mail-Desk sollte diese Skills orchestrieren, ihre Fachlogik aber nicht duplizieren.

Die Reihenfolge ist derzeit widersprüchlich: Die Problemstellung verlangt eine Synchronisation der Projektunterlagen vor der Mailbearbeitung, das Diagramm setzt `Memory-Sync` danach. Empfehlenswert ist `Cloud-Atlas Preflight/Sync → Mail Harvest/Triage → Synthese → Action Handover`. Automatische Änderungen an `statusampel`, `signals.md` und `contacts.md` sollten zunächst als Vorschlag mit Quellenankern entstehen; nur deterministische Ergänzungen dürfen ohne inhaltliches Human Gate geschrieben werden. IMAP-/Himalaya-Abfragen müssen adaptergerecht erzeugt, begrenzt und vor der Mutation als Dry-Run sichtbar sein.

---

## FR-05: Modulare Reorganisation des Batch-Runners (`scripts/core/modes/`)

**Status:** 🟡 Vorarbeiten in dieser Session umgesetzt, Ziel noch offen. OI-10 bis OI-14 brachten den zentralen Envelope-Helper, CLI-Vertragsprüfungen und die Refactor-/Progressive-Disclosure-Dokumentation; OI-17 ergänzte gemeinsame atomare Schreibfunktionen. Die acht `run_*_mode`-Funktionen verbleiben jedoch in `mail_desk_batch_runner.py`; die Datei umfasst aktuell 1.468 physische Zeilen und `scripts/core/modes/` existiert nicht.

### Problemstellung
[`mail_desk_batch_runner.py`](skills/mail-desk/scripts/mail_desk_batch_runner.py) ist auf 1.468 physische Zeilen angewachsen. Zwar sind Basis-Hilfsfunktionen bereits in `scripts/core/` ausgelagert, die einzelnen Modus-Routinen (`run_inspect_mode`, `run_draft_mode`, `run_execute_mode`, `run_verify_mode` etc.) liegen jedoch noch linear im Hauptskript.

### Ziel-Spezifikation
Sobald weitere Modi hinzukommen (z. B. der Dossier-Modus oder erweiterte AI-Pipelines) oder die Dateigröße 1.500 Zeilen überschreitet, wird die Modi-Logik in ein Untermodul ausgelagert:

```text
skills/mail-desk/scripts/
├── mail_desk_batch_runner.py       # Schlanker CLI-Dispatcher (~150 Zeilen)
└── core/
    ├── himalaya.py
    ├── index.py
    ├── classifier.py
    ├── evidence.py
    ├── sent_indexer.py
    ├── action_log.py
    └── modes/                      # Ausgelagerte Modus-Handler
        ├── __init__.py
        ├── inspect.py              # run_inspect_mode
        ├── draft.py                # run_draft_mode
        ├── execute.py              # run_execute_mode
        ├── verify.py               # run_verify_mode
        ├── pipeline.py             # run_pipeline_mode
        ├── dossier.py              # run_dossier_mode (FR-04)
        ├── search.py               # run_search_mode
        └── resolve.py              # run_resolve_mode
```

### Kommentar und Schärfung

FR-05 sollte vor FR-04 erfolgen: Mit dem Dossier-Modus würde der Runner die im Request genannte 1.500-Zeilen-Schwelle unmittelbar überschreiten. Die Zielgröße von ungefähr 150 Zeilen für den Dispatcher ist plausibel, sollte aber kein hartes Abnahmekriterium sein; wichtiger sind stabile Imports, genau ein kanonischer Envelope am CLI-Rand und unveränderte Modussemantik.

Die Auslagerung sollte inkrementell erfolgen. Zuerst eignen sich die relativ abgeschlossenen Modi `search` und `resolve`, danach `inspect`/`draft`, zuletzt die eng gekoppelten Mutationspfade `execute`/`verify`/`pipeline`. Nach jedem Schritt müssen die bestehenden Envelope-, Partial-Failure-, Cleanup- und Manifest-Sicherheitstests grün bleiben. Erst danach sollte `dossier.py` aus FR-04 hinzukommen.
