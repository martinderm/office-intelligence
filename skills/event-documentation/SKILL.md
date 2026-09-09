---
name: event-documentation
description: Event- und Konferenzdokumentation innerhalb von office-intelligence nach dem Dual-Evidence-Standard. Verwende diesen Skill, wenn größere Veranstaltungen (Konferenzen, Tagungen, Seminare) dokumentiert werden sollen. Er regelt die 2-Säulen-Ordnerstruktur (index.md in references/ vs. recordings/, notes/, action-items.md in evidence/), das Extrahieren von Programmen, die Aufbereitung von Vortrags-Transkripten und die Triage von Fristen mit Belegankern (Todoist-Synchronisation).
---

# event-documentation

Systematische Dokumentation, Aufzeichnungspflege und Action-Item-Triage für Events und Konferenzen nach dem Dual-Evidence-Standard.

## Zielbild (2-Säulen-Architektur)

Größere Veranstaltungen werden nach dem Dual-Evidence-Standard sauber in normative Vorgaben (Säule 1) und operative Aufzeichnungen/Ergebnisse (Säule 2) getrennt:

### Säule 1: De Jure / Normativ (`memory/references/`)
- **Topic-Pfad:** `memory/references/topics/<topic>/subtopics/<subtopic>/events/<event-slug>/index.md`
- **Projekt-Pfad:** `memory/references/projects/<projekt>/events/<event-slug>/index.md`
- **Inhalt:**
  - `index.md` — Die offizielle Event-Übersicht (Programm, Keynotes, Metadaten, Session-Beschreibungen und relative Links zu den über Cloud Atlas erschlossenen Originalen und Mirrors).

### Säule 2: De Facto / Empirisch (`memory/evidence/`)
- **Topic-Pfad:** `memory/evidence/topics/<topic>/events/<event-slug>/`
- **Projekt-Pfad:** `memory/evidence/projects/<projekt>/events/<event-slug>/`
- **Inhalt:**
  - `action-items.md` — Liste offener To-Dos und Folgeaufgaben aus Sitzungen (Triage vor Todoist).
  - 📂 `recordings/` — Lokale Meeting-Zusammenfassungen und Transkripte des Events (`*.summary.md`, `*.transcript.md`).
  - 📂 `notes/` — Manuelle Notizen, Mitschriften oder Beobachtungen.
  - **Monats-Log:** Zusammenfassender Eintrag mit Beleganker (`### [EVID-...]`) in `memory/evidence/topics/<topic>/events/<event-slug>/YYYY-MM.md`.

*(Hinweis: Zur Abwärtskompatibilität in noch nicht migrierten Legacy-Workspaces wird auch die Altablage im kombinierten Unterordner unter `memory/references/.../events/<event-slug>/` fehlerfrei erkannt.)*

---

## Cloud-Atlas-Speicher und Linkregeln (CRITICAL)

- **Workspace-Links**: Verweise zwischen Säule 1 (`index.md`) und Säule 2 (`recordings/`, `action-items.md`) müssen **relativ** angegeben werden. Für einen Topic-/Subtopic-Event-Pfad lautet der Link von `memory/references/topics/<topic>/subtopics/<subtopic>/events/<slug>/index.md` nach `memory/evidence/topics/<topic>/events/<slug>/recordings/2026-06-04-keynote.summary.md` beispielsweise `../../../../../../../evidence/topics/<topic>/events/<slug>/recordings/2026-06-04-keynote.summary.md`. Für einen Projekt-Event-Pfad lautet er von `memory/references/projects/<project>/events/<slug>/index.md` nach `memory/evidence/projects/<project>/events/<slug>/recordings/2026-06-04-keynote.summary.md` beispielsweise `../../../../../evidence/projects/<project>/events/<slug>/recordings/2026-06-04-keynote.summary.md`. Bei abweichenden Quell- oder Zielpfaden den Link deterministisch aus den tatsächlichen Workspace-Pfaden ableiten, niemals eine Einheitsroute übernehmen.
- **Kein Event-Speicher**: Ein Event besitzt und benötigt keinen eigenen Cloud-Speicher. Sein Dossier und seine lokale Evidence bleiben auch ohne `cloud_sync` vollständig gültig.
- **Optionale geerbte Speicherwahl**: Nur wenn Cloud-Assets archiviert oder erschlossen werden sollen, werden geeignete `cloud_sync`-Konfigurationen des zugehörigen Projekts beziehungsweise des Parent-Topics und Subtopics betrachtet. Bei genau einem geeigneten Speicher wird er automatisch geerbt; bei mehreren wählt der optionale Event-Selektor `cloud_storage` exakt einen Scope und eine vorhandene ID. Bei keinem geeigneten Speicher keinen Event-Speicher improvisieren: Das Event bleibt gültig, Cloud-Atlas-Verarbeitung ist jedoch nicht verfügbar, bis der Katalogeigentümer über `project-catalog-entry` beziehungsweise `topic-catalog-entry` eine Konfiguration erhält.
- **Konfigurierter Mount**: Falls ein geerbter Speicher verwendet wird, ist `cloud_sync.<storage_id>.scan_dir` der einzige physische Cloud-Mount und muss ein workspace-relativer Pfad sein, üblicherweise unter `data/cloud/...`. Der Event-Workflow legt weder eigene Mounts noch absolute Benutzerpfade an.
- **Originale und Mirrors**: Cloud-archivierte Programm-PDFs, Audio/Video und andere externe Originale liegen im konfigurierten `scan_dir`. Ihre Markdown-Mirrors und Filemaps werden aus demselben `cloud_sync.<storage_id>` über dessen `output_dir`, `output_json` und `output_md` bestimmt. Vor dem Verlinken Filemap und Mirror auflösen; keine Pfade oder Speicher-IDs erraten.
- **Portable Links**: `index.md` verlinkt auf vorhandene Originale, Mirrors und Filemap-Ausgaben nur mit vom Event-Ordner aus abgeleiteten relativen Workspace-Links. Storage- und Filemap-Metadaten werden nur bei tatsächlichem Cloud-Bezug im Frontmatter festgehalten.

### Migration vorhandener Altlinks

Bestehende `/Agent-Share/`-Links sind Migrationsaltbestand: Sie werden nicht neu erzeugt, nicht automatisch ersetzt und begründen keine Speicherwahl. Erst nach der Auflösung auf eine konkrete `cloud_sync.<storage_id>`-Konfiguration dürfen sie kontrolliert auf die daraus abgeleiteten relativen Cloud-Atlas-Links migriert werden.

---

## Arbeitsmodus & Workflow

### 1. Initialisierung
- Für Topic-Events zuerst den Eintrag unter `subtopics[].events[]` katalogisieren und
  prüfen: slug-ID, Titel, ISO-Start/optional Ende, Routingstatus, optionale Phase,
  kanonisches Dossier und Event-Signale. `cloud_storage` ist nicht erforderlich.
- Ermittle nur bei tatsächlichem Cloud-Bezug die geeigneten Speicher des Besitzers:
  bei Projekt-Events aus dem Projekt, bei Topic-Events aus Parent-Topic und
  ausgewähltem Subtopic. Keine Kandidaten blockieren Cloud Atlas, aber nicht das
  Event; ein Kandidat wird geerbt; mehrere Kandidaten erfordern den expliziten
  `cloud_storage`-Selektor. Einen vorhandenen Selektor strikt im deklarierten Scope
  auflösen (`topic` nur gegen Parent, `subtopic` nur gegen Subtopic), ohne Fallback.
- Erst nach gültigem Katalogeintrag die 2-Säulen-Ordner gemäß
  `references/event-folder-template.md` anlegen und das kanonische `index.md` als
  Dossier verwenden. Das Event-Dossier wird nicht aus Mailtext abgeleitet.
- Trage die Eckdaten immer ein. Storage-Scope, Storage-ID und Filemap-Pfad nur dann,
  wenn Cloud-Assets über eine konkret aufgelöste geerbte Konfiguration vorliegen.

### 2. Programm-Extraktion & Archivierung
- Verlinke die offizielle Online-Quelle des Programms (meist PDF) im Dossier.
- Soll das Original zusätzlich in der Cloud archiviert werden, nutze ausschließlich den aufgelösten geerbten `cloud_sync.<storage_id>.scan_dir`. Ohne geeignete Parent-Konfiguration keine Ablage erfinden und die übrige Event-Dokumentation fortsetzen.
- Führe Konvertierung und Filemap-Aktualisierung über `cloud-atlas` nur bei einem konkret aufgelösten Speicher aus; der Markdown-Mirror bleibt im konfigurierten lokalen `output_dir`.
- Verlinke vorhandene Originale, Markdown-Mirrors und Filemaps aus `index.md` mit den daraus abgeleiteten relativen Workspace-Links.

### 3. Keynote- & Meeting-Verarbeitung
- Für aufgezeichnete Vorträge/Sitzungen:
  - **Intake-Quelle**: Lokalisiere die neu synchronisierten Meeting-Dateien an ihrem Speicherort gemäß `fireflies-api` / `zoom-api` Skill unter `memory/evidence/meetings/` (bzw. Fallback `memory/references/meetings/`).
  - Verschiebe bzw. kopiere die Dateien (Transkript und Zusammenfassung) aus diesem Intake-Ordner in den Event-Ordner `memory/evidence/topics/<topic>/events/<event-slug>/recordings/`.
  - **Qualitäts-Check (Summary)**: Wenn die importierte Zusammenfassung unzureichende Inhalte hat (z. B. leere Abschnitte aufgrund aufgebrauchter Fireflies-Credits), erstelle die Zusammenfassung **aktiv neu anhand des immer verfügbaren Transkripts** (gemäß den Formatregeln aus dem `fireflies-api` Skill).
  - Speichere das Transkript als `<YYYY-MM-DD>-<vortrag>.transcript.md` und die Zusammenfassung als `<YYYY-MM-DD>-<vortrag>.summary.md` im `recordings/`-Ordner des Events.
  - Verlinke die Zusammenfassung im entsprechenden Programmpunkt in der `index.md` des Events.
  - Trage Mail- und Veranstaltungsbelege in das Event-Monatslog
    `memory/evidence/topics/<topic>/events/<event>/<YYYY-MM>.md` ein;
    `recordings/`, `notes/` und `action-items.md` bleiben dort kompatibel.
  - **CRITICAL**: Aktualisiere den `summary_path` und `transcript_path` des entsprechenden Meetings in `meetings.json` auf die neuen Speicherorte im Event-Verzeichnis.

### 4. Fristen & Action-Items Triage (Todoist-Integration)
- **Zukunftsfristen**: Analysiere das Programm und Dokumente nach Fristen (Early Bird, Abstract Submission, Registrierung).
  - Fristen, die in der Zukunft liegen, werden **in Todoist** eingetragen (`todoist-api`).
  - Bereits abgelaufene Fristen werden nur dokumentiert, aber nicht synchronisiert.
- **Action-Items & Factored Attribution**:
  - Extrahiere Aufgaben und To-Dos aus Vorträgen und Meetings und halte sie in `action-items.md` fest.
  - Nutze `action-items.md` als Triage-Station.
  - Aufgaben, die nach Todoist übertragen werden, **müssen im `description`-Feld stets den Herkunftsnachweis (Beleganker)** tragen:
    ```markdown
    Quelle: [EVID-YYYY-MM-DD-XX] Event "<Event-Titel>" (Vortrag: <Keynote-Titel>)
    Meeting-ID: <fireflies_or_zoom_id>
    ```

---

## Template-Referenz

Das vollständige Markdown-Template für die Event-Ordnerstruktur befindet sich unter:
[`references/event-folder-template.md`](references/event-folder-template.md)
