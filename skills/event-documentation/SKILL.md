---
name: event-documentation
description: Event- und Konferenzdokumentation innerhalb von office-intelligence. Verwende diesen Skill, wenn größere Veranstaltungen (Konferenzen, Tagungen, Seminare) dokumentiert werden sollen. Er regelt das Erzeugen der Event-Ordnerstruktur (index.md, action-items.md, recordings/, notes/), das Extrahieren von Programmen, die Zusammenfassung von Vortrags-Transkripten und die Triage von Fristen (Todoist-Synchronisation).
---

# event-documentation

Systematische Dokumentation, Aufzeichnungspflege und Action-Item-Triage für Events und Konferenzen.

## Zielbild (verbindlich)

Größere Veranstaltungen werden nicht flach abgelegt, sondern erhalten eine eigene, gekapselte Verzeichnisstruktur unter dem jeweiligen Subtopic eines Topics oder dem jeweiligen Projekt:

- **Topic-Pfad:** `memory/references/topics/<topic>/subtopics/<subtopic>/events/<event-slug>/`
- **Projekt-Pfad:** `memory/references/projects/<projekt>/events/<event-slug>/`

### Verbindliche Ordnerstruktur bei Neuanlage

Erzeuge für jedes neue Event folgende Struktur:

1. `index.md` — Die zentrale Event-Übersicht (Programm, Keynotes, Metadaten und Verlinkungen).
2. `action-items.md` — Liste offener To-Dos und Folgeaufgaben aus Sitzungen (Vorstufe/Triage vor Todoist).
3. 📂 `recordings/` — Lokale Meeting-Zusammenfassungen und Transkripte des Events (z. B. `*.summary.md`, `*.transcript.md`).
4. 📂 `notes/` — Manuelle Notizen, Mitschriften oder Gedanken.

---

## Pfad- und Linkregeln (CRITICAL)

- **Workspace-Links**: Verweise innerhalb des Workspace (z. B. von der `index.md` auf `action-items.md` oder in `recordings/`) müssen **immer relativ** angegeben werden (z. B. `./action-items.md` oder `./recordings/2026-06-04-keynote.summary.md`).
- **Agent-Share-Links**: Größere Originaldateien (wie Original-PDFs, Keynote-Audio/Video, XMLs) gehören auf das BokuDrive (`Agent-Share/`) und müssen **systemneutral** mit dem Präfix `/Agent-Share/...` verlinkt werden (z. B. `/Agent-Share/LLL-Networks/EUCEN-2026/program.pdf`). Verwende **niemals** absolute Pfade, die benutzerspezifische Home-Verzeichnisse (`/Users/martin/...`) enthalten!

---

## Arbeitsmodus & Workflow

### 1. Initialisierung
- Lege das Verzeichnis gemäß der Pfadkonvention an.
- Nutze die Vorlage unter `references/event-folder-template.md` als Basis für `index.md` und `action-items.md`.
- Trage alle grundlegenden Eckdaten (Datum, Ort, Webseite) im Frontmatter der `index.md` ein.

### 2. Programm-Extraktion & Archivierung
- Lade das offizielle Programm (meist PDF) herunter.
- Speichere das Original-PDF auf dem BokuDrive unter `Agent-Share/` im passenden Event-Unterordner.
- Konvertiere das PDF mit einem Konvertierungswerkzeug (z. B. `markitdown`) in reines Markdown.
- Speichere die Markdown-Fassung ebenfalls im BokuDrive.
- Verlinke beide Versionen (PDF und Markdown) in der `index.md` des Events mit dem systemneutralen `/Agent-Share/`-Präfix.

### 3. Keynote- & Meeting-Verarbeitung
- Für aufgezeichnete Vorträge/Sitzungen:
  - **Erste Quelle (Intake)**: Lokalisiere die neu synchronisierten Meeting-Dateien an ihrem Standard-Speicherort gemäß `fireflies-api` Skill unter `memory/references/meetings/` (bzw. dem jeweiligen Channel-Unterordner wie `meetings/<channel-slug>/`).
  - Verschiebe bzw. kopiere die Dateien (Transkript und Zusammenfassung) aus diesem Standardordner.
  - **Qualitäts-Check (Summary)**: Wenn die importierte Zusammenfassung unzureichende Inhalte hat (z. B. leere Abschnitte, unvollständige Notizen aufgrund aufgebrauchter Fireflies-Credits), erstelle die Zusammenfassung **aktiv neu anhand des immer verfügbaren Transkripts** (gemäß den Formatregeln aus dem `fireflies-api` Skill).
  - Speichere das Transkript als `<YYYY-MM-DD>-<vortrag>.transcript.md` und die Zusammenfassung als `<YYYY-MM-DD>-<vortrag>.summary.md` im `recordings/`-Ordner des Events.
  - Verlinke die Zusammenfassung direkt im entsprechenden Programmpunkt in der `index.md` des Events.
  - Trage eine Zusammenfassung des Meetings in das zentrale, monatliche Evidenz-Log des Topics/Projekts ein (z. B. `topics/<topic>/evidence/<YYYY-MM>.md`).
  - **CRITICAL**: Aktualisiere den `summary_path` und `transcript_path` des entsprechenden Meetings in `memory/references/meetings/meetings.json` auf die neuen Speicherorte im Event-Verzeichnis, um die zentrale JSON-Registrierung konsistent zu halten.

### 4. Fristen & Action-Items Triage
- **Zukunftsfristen**: Analysiere das Programm und Dokumente nach Fristen (Early Bird, Abstract Submission, Registrierung).
  - Fristen, die in der Zukunft liegen, müssen **automatisch in Todoist** eingetragen werden.
  - Bereits abgelaufene Fristen werden nur dokumentiert, aber nicht synchronisiert.
- **Action-Items**:
  - Extrahiere Aufgaben und To-Dos aus Vorträgen und Meetings und halte sie in `action-items.md` fest.
  - Nutze `action-items.md` als Triage-Station. Aufgaben, die dem User oder dem Agenten zugewiesen sind und konkrete Deadlines haben, können ebenfalls nach Todoist übertragen werden.

---

## Template-Referenz

Das vollständige Markdown-Template für die Event-Ordnerstruktur befindet sich unter:
`references/event-folder-template.md`
