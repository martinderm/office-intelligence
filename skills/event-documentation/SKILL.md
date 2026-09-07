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
  - **Monats-Log:** Zusammenfassender Eintrag mit Beleganker (`### [EVID-...]`) in `memory/evidence/topics/<topic>/YYYY-MM.md`.

*(Hinweis: Zur Abwärtskompatibilität in noch nicht migrierten Legacy-Workspaces wird auch die Altablage im kombinierten Unterordner unter `memory/references/.../events/<event-slug>/` fehlerfrei erkannt.)*

---

## Cloud-Atlas-Speicher und Linkregeln (CRITICAL)

- **Workspace-Links**: Verweise zwischen Säule 1 (`index.md`) und Säule 2 (`recordings/`, `action-items.md`) müssen **relativ** angegeben werden. Für einen Topic-/Subtopic-Event-Pfad lautet der Link von `memory/references/topics/<topic>/subtopics/<subtopic>/events/<slug>/index.md` nach `memory/evidence/topics/<topic>/events/<slug>/recordings/2026-06-04-keynote.summary.md` beispielsweise `../../../../../../../evidence/topics/<topic>/events/<slug>/recordings/2026-06-04-keynote.summary.md`. Für einen Projekt-Event-Pfad lautet er von `memory/references/projects/<project>/events/<slug>/index.md` nach `memory/evidence/projects/<project>/events/<slug>/recordings/2026-06-04-keynote.summary.md` beispielsweise `../../../../../evidence/projects/<project>/events/<slug>/recordings/2026-06-04-keynote.summary.md`. Bei abweichenden Quell- oder Zielpfaden den Link deterministisch aus den tatsächlichen Workspace-Pfaden ableiten, niemals eine Einheitsroute übernehmen.
- **Verpflichtliche Speicherwahl**: Vor dem Anlegen eines Events den passenden Projekt- oder Topic-Katalogeintrag prüfen und einen **bestehenden** `cloud_sync.<storage_id>` auswählen. Fehlt er, keinen Event-Speicher improvisieren, sondern den Katalogeintrag mit `project-catalog-entry` (Projekt) beziehungsweise `topic-catalog-entry` (Topic oder Subtopic) pflegen. Erst danach löst `cloud-atlas` den Speicher auf und führt Synchronisation, Konvertierung sowie Filemap-Erzeugung aus.
- **Konfigurierter Mount**: `cloud_sync.<storage_id>.scan_dir` ist der einzige physische Cloud-Mount und muss ein workspace-relativer Pfad sein, üblicherweise unter `data/cloud/...`. Der Event-Workflow legt weder eigene Mounts noch absolute Benutzerpfade an.
- **Originale und Mirrors**: Programm-PDFs, Audio/Video und andere externe Originale liegen im konfigurierten `scan_dir`. Ihre Markdown-Mirrors und Filemaps werden aus demselben `cloud_sync.<storage_id>` über dessen `output_dir`, `output_json` und `output_md` bestimmt. Vor dem Verlinken Filemap und Mirror auflösen; keine Pfade oder Speicher-IDs erraten.
- **Portable Links**: `index.md` verlinkt auf Originale, Mirrors und Filemap-Ausgaben nur mit vom Event-Ordner aus abgeleiteten relativen Workspace-Links. Der gewählte `storage_id` sowie der aufgelöste workspace-relative Filemap-Pfad werden im Frontmatter festgehalten.

### Migration vorhandener Altlinks

Bestehende `/Agent-Share/`-Links sind Migrationsaltbestand: Sie werden nicht neu erzeugt, nicht automatisch ersetzt und begründen keine Speicherwahl. Erst nach der Auflösung auf eine konkrete `cloud_sync.<storage_id>`-Konfiguration dürfen sie kontrolliert auf die daraus abgeleiteten relativen Cloud-Atlas-Links migriert werden.

---

## Arbeitsmodus & Workflow

### 1. Initialisierung
- Lege die Verzeichnisse gemäß der 2-Säulen-Konvention an.
- Nutze die Vorlage unter `references/event-folder-template.md` als Basis für `index.md` (Säule 1) und `action-items.md` (Säule 2).
- Prüfe den zuständigen Projekt- oder Topic-Katalog, wähle einen vorhandenen `cloud_sync.<storage_id>` und löse dessen workspace-relativen `scan_dir` sowie Filemap-Ausgaben auf.
- Trage alle grundlegenden Eckdaten (Datum, Ort, Webseite), `cloud_storage_id` und den aufgelösten `cloud_filemap`-Pfad im Frontmatter der `index.md` ein.

### 2. Programm-Extraktion & Archivierung
- Lade das offizielle Programm (meist PDF) herunter.
- Lege das Original ausschließlich im durch `cloud_sync.<storage_id>.scan_dir` konfigurierten Cloud-Atlas-Speicher ab.
- Führe die Konvertierung und Filemap-Aktualisierung über `cloud-atlas` für genau diesen `storage_id` aus; der Markdown-Mirror bleibt im konfigurierten lokalen `output_dir`.
- Verlinke Original, Markdown-Mirror und gegebenenfalls Filemap aus `index.md` mit den daraus abgeleiteten relativen Workspace-Links.

### 3. Keynote- & Meeting-Verarbeitung
- Für aufgezeichnete Vorträge/Sitzungen:
  - **Intake-Quelle**: Lokalisiere die neu synchronisierten Meeting-Dateien an ihrem Speicherort gemäß `fireflies-api` / `zoom-api` Skill unter `memory/evidence/meetings/` (bzw. Fallback `memory/references/meetings/`).
  - Verschiebe bzw. kopiere die Dateien (Transkript und Zusammenfassung) aus diesem Intake-Ordner in den Event-Ordner `memory/evidence/topics/<topic>/events/<event-slug>/recordings/`.
  - **Qualitäts-Check (Summary)**: Wenn die importierte Zusammenfassung unzureichende Inhalte hat (z. B. leere Abschnitte aufgrund aufgebrauchter Fireflies-Credits), erstelle die Zusammenfassung **aktiv neu anhand des immer verfügbaren Transkripts** (gemäß den Formatregeln aus dem `fireflies-api` Skill).
  - Speichere das Transkript als `<YYYY-MM-DD>-<vortrag>.transcript.md` und die Zusammenfassung als `<YYYY-MM-DD>-<vortrag>.summary.md` im `recordings/`-Ordner des Events.
  - Verlinke die Zusammenfassung im entsprechenden Programmpunkt in der `index.md` des Events.
  - Trage einen Belegeintrag in das zentrale Monats-Evidenz-Log ein (z. B. `memory/evidence/topics/<topic>/<YYYY-MM>.md` mit Anker `### [EVID-YYYY-MM-DD-XX]`).
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
