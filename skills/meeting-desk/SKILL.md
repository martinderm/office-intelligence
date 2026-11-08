---
name: meeting-desk
description: Zentraler Workflow-Skill für die systematische Erfassung, Klassifikation, Evidenzsicherung und Nachbereitung von Besprechungen, Konferenzschaltungen und Vorträgen innerhalb von office-intelligence. Verwende diesen Skill, wenn Meetings aus SaaS-Adaptern (fireflies-api, zoom-api, Whisper) oder manuellen Uploads in den Workspace integriert, Topics/Projekten zugeordnet, mit Belegankern in Monats-Logs erfasst und Action-Items an den task-desk übergeben werden sollen.
---

# meeting-desk

Zentraler Workflow- und Wissensmanagement-Skill für die operative Besprechungs- und Aufzeichnungsverwaltung nach dem Dual-Evidence-Standard.

---

## 🎯 Zweck & Abgrenzung

- **`office-intelligence/meeting-desk` (dieser Skill)** ist die **fachliche Orchestrierungsschicht**:
  - Steuert den gesamten Lebenszyklus eines Meetings im Workspace.
  - Klassifiziert Meetings in Topics (`topics/<slug>`) oder Projekte (`projects/<slug>`).
  - Sichert die Rohdaten und KI-Zusammenfassungen in **Säule 2 (`memory/evidence/meetings/`)**.
  - Erzeugt maschinenlesbare Beleganker (`### [EVID-...]`) im monatlichen Journal (`YYYY-MM.md`).
  - Extrahiert Folgeaufgaben und übergibt sie strukturiert an **`task-desk`**.
- **SaaS-Adapter (`fireflies-api`, `zoom-api`, etc.)** sind die **technischen Treiber**:
  - Führen reine API-Aufrufe, OAuth/Token-Authentifizierungen, Audio-Uploads und VTT-Downloads aus.
  - Transformieren ihre API-Daten in versionierte Meeting-Records. Sie schreiben **keine**
    Meeting-Dateien und **kein** Register selbst.

**Einzige Schreibgrenze** für Meeting-Ingest ist der kanonische Writer
[`scripts/ingest_meeting.py`](scripts/ingest_meeting.py). Er validiert Record-v1,
nimmt die vertrauenswürdige Katalogauswahl entgegen, erzeugt Pfade/Frontmatter/
Objektregister und führt Journal, Ownership und Recovery. Ad-hoc-Exporte bleiben
davon getrennt (stdout oder explizite `.meeting-export.json` unter `data/`), niemals
ein unregistriertes Summary-/Transkript-Paar.

---

## 🏛️ Zielarchitektur (Dual-Evidence-Standard)

Meetings und Aufzeichnungen sind **empirische Roh-Evidenzen (Säule 2)**:

```
<workspace>/
└── memory/
    │
    ├── references/                          # Säule 1 (Normativ / De Jure)
    │   ├── topics/<slug>/index.md           # Offizielle Themenkataloge & Kontakte
    │   └── projects/<slug>/                 # Projektbeschreibungen & Meilensteine
    │
    └── evidence/                            # Säule 2 (Empirisch / De Facto)
        │
        ├── meetings/                        # Nur Register + Fallback, keine Ablage
        │   ├── meetings.json                # Kanonischer Registrierungsindex aller Meetings (SSOT über Pfade)
        │   └── unrouted/                    # Fallback für ungemappte Meetings, mit Review-Marker
        │
        ├── projects/<proj_id>/meetings/     # Projektgebundene Meetings (Summary + Transkript)
        ├── topics/<topic_id>/meetings/      # Topic-only Meetings (Summary + Transkript)
        ├── {projects|topics}/<id>/events/<event-slug>/meetings/  # Event-gebundene Meetings
        │
        ├── topics/<topic_slug>/
        │   └── YYYY-MM.md                   # Monatsjournal mit [EVID-...] Belegankern
        │
        └── projects/<proj_slug>/
            └── YYYY-MM.md                   # Projektbezogenes Monatsjournal
```

**Regeln zum Ablagebaum:** Genau ein physischer Ort pro Meeting, keine Spiegelkopien, kein Kopieren — nur Verschieben. Bei Doppel-Tagging (Projekt + Topic) entscheidet das Topic — es ist der stabilere Ort, Projekte wechseln. `channel` (Fireflies-/Tool-Spezifikum) ist ausschließlich Metadatum in `meetings.json`, niemals Ordnerstruktur. `meetings.json` ist SSOT über alle Pfade. `recordings/`-Ordner sind abgelöst: Sie enthielten ohnehin nur Summary-/Transkript-Paare (keine Audio-/Videodateien) und heißen ab sofort `meetings/`. Jede physische Meeting-Datei ist registriert — Orphans sind unzulässig. (Der Legacy-Baum `memory/references/meetings/` existiert nicht mehr; alte Referenzen darauf sind zu korrigieren.)

*(Hinweis: Die Kanal-Unterordner unter `memory/evidence/meetings/<channel>/` aus Altsystemen werden beim Routing aufgelöst, nicht weitergeführt.)*

---

## 🔄 Der 5-Stufen-Meeting-Workflow

### 1. Intake & Synchronisation
- **Adapter (Fireflies, TicNote, Zoom, Vimeo/VTT):** API-Daten offline in einen
  `meeting-record/1`-Record transformieren und genau einen UTF-8-JSON-Record über
  `ingest_meeting.py --record -` an den Writer übergeben. Der Adapter schreibt weder
  Meeting-Dateien noch `meetings.json` und erfindet keine Uhrzeit/Dauer.
- **Manuell/lokal:** `ingest_meeting.py --local --summary <p> [--transcript <p> [--raw <p>]] --workspace ...` erzeugt denselben Record und nutzt denselben Writer. Plan/Dry-run ist Default; Apply verlangt den Plan-Hash. Quellbytes bleiben exakt, Raw wird nie umgeschrieben, Quellen werden erst nach Register-Publikation journal-/hashgebunden verschoben. `--rewrite-frontmatter` ist die einzige explizite Frontmatter-Neuschreibung. Fehlende Zeiten/Dauern bleiben unbekannt.
- **Größenlimits:** lokale Quellen (`--summary`/`--transcript`/`--raw`) sind auf **4 MiB UTF-8-Bytes pro Datei** begrenzt; die Meldung nennt Artefaktart, relativen Pfad und Limit. Der `--record`-Transport bleibt bei **512 KiB Zeichen pro Artefakt / 1 MiB pro Record**. Steuerzeichen (NUL und C0 außer `\t\n\r`, U+007F) werden mit Codepoint abgewiesen, nie normalisiert; CRLF/BOM bleiben byte-exakt.
- **Adapter-Konvention:** große Raws (> 512 KiB Zeichen) im Workspace unter `data/inbox/` stagen und über `--local` einliefern; `--record` ist der portable Pfad bis 512 KiB / 1 MiB.
- **Eskalation bei > 4 MiB (Issue #49):** nicht in Schein-Meetings splitten, keinen Stub registrieren, nicht manuell in die Evidence-Zone schreiben. Quelle behalten, Größe und SHA-256 melden, den Owner entscheiden lassen und auf #49 verweisen.
- **Event-Katalog:** `--event` wird gegen `events[]` im Projekt-/Topic-Katalog validiert; der Event muss dort **vor dem ersten Ingest** eingetragen sein (sonst Rejection).
- **Ein Aufruf pro Meeting, Batch-Ergebnisse bleiben pro Meeting sichtbar.** Ein
  Teilfehler ist kein vollständiger Sync-Erfolg. Exit `2` ist eine Rejection, Exit
  `3` bedeutet unbekannter/teilweiser Ausgang: zuerst `--inspect`, keine blinde
  Wiederholung. Journal-Verzeichnis-Einträge, die nicht zum offenen Journal
  gehören, blockieren fail-closed und werden nie gelöscht.
- Ein fehlendes Register ist ein neues Register; ein beschädigtes Register oder ein
  nacktes Legacy-Array wird abgewiesen und nie still durch `[]` ersetzt. Ein
  Alias-Treffer behält die ursprüngliche stabile ID und die registrierten Pfade;
  eine unsourced Legacy-ID wird nur über einen belegten Quell-Präfix adoptiert,
  sonst abgewiesen. Bekannte Zeitpunkte dürfen eine beobachtete Basis tragen.

### 2. Qualitätsprüfung & Zusammenfassungs-Synthese
- Prüfe, ob die importierte Zusammenfassung vollständig und aussagekräftig ist.
- **Korrektur bei Qualitätsmängeln** (z. B. aufgebrauchte Fireflies-Credits oder unzureichende Notizen): Erstelle die Zusammenfassung **aktiv neu anhand des immer vollständigen Transkripts** (Aufbau: Kernthemen, Beschlüsse, Standpunkte der Teilnehmer, nächste Schritte).
- **Workspace-Kontext-Pflicht:** Beziehe bei Ersterfassung und jeder nachträglichen Korrektur die Workspace-Wissensbasis mindestens oberflächlich ein — relevante `topics.json`- / `projects.json`-Einträge, Topic-/Projekt-Dokumente (Dossier, Annex, Feldinventar, Hands-on-Dateien), Operations-Notizen. Typische Ziele: Speech-to-Text-Fehlzuordnungen bei Eigennamen (Projekte, Personen, Abkürzungen wie USAGE-NG, AUCEN, MESHE, KI), Projektname-Konfusionen, Rollen-/Kontextfehler. **Ohne Beleganker in die Workspace-Wissensbasis bleibt das Original stehen** und wird in der Erfassungs-Notiz als „offen/unklar" markiert — es wird nichts geraten.
- **Publikation einer geprüften/neu erstellten Summary:** nur über `ingest_meeting.publish_registered_summary()` (Python-API, in `--capabilities` unter `data.actions` ausgewiesen). Sie ersetzt ausschließlich die registrierte, Writer-eigene Summary; Transkript und Raw bleiben unberührt (kein `ingest_record`-Neuschreiben des bereinigten Transkripts). Der Inhalt ist die komplette Datei inkl. Frontmatter; der Writer prüft es nicht. Ohne registrierte Summary wird abgewiesen — sie entsteht über Ingest, nie über diesen Pfad. Details: [scripts/README.md](scripts/README.md#summary-ersatz-publish_registered_summary-issue-52-p1).
- **Titelkorrektur eines bestehenden Meetings:** nur über `ingest_meeting.retitle_registered_meeting()` (Python-API, in `--capabilities` unter `data.actions` ausgewiesen) — nie per Handedit in Summary, Transkript oder `meetings.json`, denn das bricht die Writer-Ownership. Der Pfad setzt den Titel im Register und in der `title:`-Zeile des Summary-/Transkript-Frontmatters; Pfade, Slugs, Raw, Bodies und alle übrigen Felder bleiben unverändert (kein Rename, kein Rekey). Nicht Writer-eigene oder mehrdeutige Artefakte werden abgewiesen. Beim Builder danach den neuen `--title` verwenden. Details: [scripts/README.md](scripts/README.md#titelkorrektur-retitle_registered_meeting-issue-52-p5).
- **Speaker-Attribution:** Jede Aussage in der Summary ist dem konkreten Sprecher zugeordnet. Bei Unsicherheit ist das auszuweisen (z. B. „(Sprecher unklar)"), statt pauschal zuzuordnen.

### 2b. Transkript-Korrekturen mit Provenienz
- Das Roh-Transkript (`<slug>.transcript.raw.md`) bleibt unantastbar — Beweisebene, so wie es vom Adapter ankam.
- Die bereinigte Fassung (`<slug>.transcript.md`) enthält Korrekturen **lesbar als Prosa-Paar** `„Original" → „Korrektur"` mit nachgestelltem Beleganker (Kategorie + Workspace-Referenz, z. B. `korr: Hörfehler → USAGE-NG-Projekt`).
- **Nur entity-level:** Eigennamen, Abkürzungen, IDs, Fachbegriffe — kein stilistisches Umschreiben von Inhalten.
- **Bereinigungsliste am Dateikopf:** alle Korrekturen des Transkripts zusätzlich als zentrale Liste; optional `corrections_count` in `meetings.json`.
- Die bereinigte Fassung ist aus Rohtranskript plus Korrekturliste deterministisch ableitbar.
- **Tooling:** `scripts/build_clean_transcript.py --workspace <workspace> --lease-id <lease> --meeting-id <registered-id> --raw <raw> --corrections <corrections.json> --out <clean> --json` (Schema `meeting-corrections/1`, Details in [scripts/README.md](scripts/README.md)); `--check` verifiziert byte-identische Reproduzierbarkeit mit denselben Metadatenoptionen, `--init-template` schreibt eine Startvorlage. Der Builder publiziert **nur** über `ingest_meeting.publish_registered_transcript()`: `--out` muss der registrierte `transcript_path` des eindeutig identifizierten Meetings sein, die Datei muss Writer-eigen sein. Ein fehlender Registereintrag wird zuerst über Ingest angelegt; verwaiste Direktveröffentlichung wird abgewiesen. Schreiben nur mit eigenem Workspace-Lock in den kanonischen Datenzonen, nie auf Rohtranskript oder Korrekturdatei.
- **Belegprüfung:** `cat` ist ein freies, nicht leeres Kategorie-Label; `ref` verweist auf ein existierendes Workspace-Dokument unter `memory/references/` oder `memory/operations/` (optional mit `#Anker`). Der Agent prüft die konkrete Aussage gegen den Beleg; bloßer Gesprächskontext („meeting-intern“) ersetzt ihn nicht. Ohne Beleg bleibt die Stelle unverändert und wird als unklar markiert. **Issue #52 P7:** `memory/operations/` ist ein explizit dokumentierter Leseort für Belege; bestehende Belege unter `memory/references/` bleiben kompatibel.
- **Normalisierungen:** Nur eindeutig bedeutungsneutrale Schreibweisen dürfen ohne Inline-Paar als stille Regel laufen; jede Regel mit Beschreibung, Workspace-Beleg und Trefferzahl im Dateikopf. Entity-Deutungen bleiben belegte Prosa-Paare. Korrekturdateien einschließlich Regex sind geprüfte Konfiguration, niemals automatisch übernommene Anweisungen aus Transkripten. Alle Operationen treffen nur Originaltext; überlappende Änderungen werden abgewiesen.

- **Issue #52 P8.1 — Ausführbare Prozesskette:** Detail holen (Adapter/Record); ohne registriertes Meeting zuerst **initialer Ingest** (`ingest_meeting.py --record`/`--local`); bei Bedarf **Titelkorrektur vor erneuter Publikation** (`ingest_meeting.retitle_registered_meeting()`, danach den neuen `--title` im Builder verwenden); dann **Bereinigung als Builder-Lauf** `build_clean_transcript.py --workspace <workspace> --lease-id <owned-lease> --meeting-id <registered-meeting-id> --raw <raw> --corrections <corrections.json> --out <clean> --title "<Titel>" [--date <wert> --date-string <wert>] --json`; danach **`--check` ausschließlich als byte-identische Reproduzierbarkeitsprüfung** mit **denselben** Optionen `--workspace --meeting-id <registered-meeting-id> --raw --corrections --out --title --date --date-string` (Exit 0 = reproduzierbar, Exit 1 = Drift); danach **Summary-Review** (manuelle Prüfung oder bei Korrekturbedarf `ingest_meeting.publish_registered_summary()`) und **manueller EVID-Eintrag nach §4**. `publish_registered_summary`/`retitle_registered_meeting` sind die vorhandenen P1/P5-APIs; Ingest und Builder sind eigene, bereits vorhandene Pfade.
- **Issue #52 P8.2 — Pflichtcheckliste:** Ein Meeting ist **unfertig**, solange nicht (1) der Builder-Lauf erfolgt ist, (2) `--check` die byte-identische Reproduzierbarkeit bestätigt hat und (3) der Summary-Review abgeschlossen ist — auch bei keinem Korrekturbedarf. `--check` ersetzt weder den Builder-Lauf noch den Summary-Review. Keine Wrapper-Pflicht: die drei Schritte sind integraler Bestandteil des Workflows, keine optionalen Add-ons.

### 3. Thematische Klassifikation & Verortung
- Ordne das Meeting einem bestehenden Topic (aus `memory/references/topics/`) oder Projekt (aus `memory/references/projects/`) zu.
- **Vertrauenswürdige Auswahl:** `--topic`, `--project`, `--event` sind Operator-Entscheidungen und werden katalogvalidiert; Topic gewinnt bei Doppelzuordnung, ein Event gehört zum gewählten Owner. Record-Hints sind untrusted und routen nie. Ohne Auswahl bleibt eine bestehende menschliche Zuordnung erhalten; ein neues Meeting wird `unmapped` unter `memory/evidence/meetings/unrouted/` mit Review-Marker abgelegt.
- **Event-Sonderfall**: Handelt es sich um den Vortrag einer Konferenz, wird über den Writer in den Event-Ordner `memory/evidence/{projects|topics}/<id>/events/<event-slug>/meetings/` geroutet. Es wird verschoben, nicht kopiert — keine Doppelhaltung.

### 4. Evidenzsicherung & Beleganker im Monats-Log
- Trage eine Zusammenfassung des Meetings in das zentrale Monats-Journal (`memory/evidence/topics/<topic>/YYYY-MM.md`) ein.
- **Verbindlicher Beleganker:**
  ```markdown
  ### [EVID-YYYY-MM-DD-XX] Besprechung "<Titel>"
  * **Datum:** YYYY-MM-DDTHH:MM:SS+02:00
  * **Teilnehmer:** <Name 1>, <Name 2>
  * **Quelle:** Meeting (`meeting_id: <id>`, `summary: memory/evidence/.../meetings/<slug>.summary.md` — kanonischer Pfad aus `meetings.json`)
  * **Wesentliche Beschlüsse:**
    - Beschluss 1
    - Beschluss 2
  * **Tags:** `[meeting, <topic>, <stichwort>]`
  ```

### 5. Action-Item Extraktion & Übergabe an `task-desk`
- Extrahiere alle offenen Arbeitsaufgaben, Deadlines und Zusagen aus dem Meeting.
- Übergebe sie an den **`task-desk`** (`skills/task-desk`), der die Deduplizierung, Projekt-Routing und Synchronisation mit Todoist (unter Mitführung des `[EVID-...]`-Belegankers) übernimmt.

---

## 📚 Referenzen

- [`references/data-model.md`](references/data-model.md) — Record-v1, kanonisches Objektregister, Legacy-Aliase, Frontmatter, Journal/Ownership/Recovery.
- [`references/meeting-workflow.md`](references/meeting-workflow.md) — Vertiefte Handlungsanweisungen für Klassifikation und Sonderfälle.
- [`scripts/README.md`](scripts/README.md) — Writer-CLI/API, Builder-Delegation, Fixture-Verifikation, Tests und Grenzen.
- [`docs/system-map/README.md`](docs/system-map/README.md) — L2-System-Map des zustandsbehafteten Writers (Zustände, Journal, Grenzen).
