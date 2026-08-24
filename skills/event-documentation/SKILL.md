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
  - `index.md` — Die offizielle Event-Übersicht (Programm, Keynotes, Metadaten, Session-Beschreibungen und Verlinkungen zu BokuDrive `/Agent-Share/...`).

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

## Pfad- und Linkregeln (CRITICAL)

- **Workspace-Links**: Verweise zwischen Säule 1 (`index.md`) und Säule 2 (`recordings/`, `action-items.md`) müssen **relativ** angegeben werden (z. B. von `references/topics/<topic>/subtopics/<subtopic>/events/<slug>/index.md` nach `../../../../../evidence/topics/<topic>/events/<slug>/recordings/2026-06-04-keynote.summary.md`).
- **Agent-Share-Links**: Größere Originaldateien (wie Original-PDFs, Keynote-Audio/Video, XMLs) gehören auf das BokuDrive (`Agent-Share/`) und müssen **systemneutral** mit dem Präfix `/Agent-Share/...` verlinkt werden (z. B. `/Agent-Share/LLL-Networks/EUCEN-2026/program.pdf`). Verwende **niemals** absolute Pfade mit benutzerspezifischen Home-Verzeichnissen!

---

## Arbeitsmodus & Workflow

### 1. Initialisierung
- Lege die Verzeichnisse gemäß der 2-Säulen-Konvention an.
- Nutze die Vorlage unter `references/event-folder-template.md` als Basis für `index.md` (Säule 1) und `action-items.md` (Säule 2).
- Trage alle grundlegenden Eckdaten (Datum, Ort, Webseite) im Frontmatter der `index.md` ein.

### 2. Programm-Extraktion & Archivierung
- Lade das offizielle Programm (meist PDF) herunter.
- Speichere das Original-PDF auf dem BokuDrive unter `Agent-Share/` im passenden Event-Unterordner.
- Konvertiere das PDF mit einem Konvertierungswerkzeug (z. B. `markitdown`) in reines Markdown.
- Speichere die Markdown-Fassung ebenfalls im BokuDrive.
- Verlinke beide Versionen (PDF und Markdown) in der `index.md` des Events mit dem systemneutralen `/Agent-Share/`-Präfix.

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
