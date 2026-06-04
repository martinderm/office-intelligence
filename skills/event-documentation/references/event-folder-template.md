---
type: Template
---

# Template — Event / Conference Folder Layout

Für jedes größere Event (z. B. Jahrestagung, Konferenz, mehrtägiges Seminar) wird ein eigener Ordner unter dem jeweiligen Subtopic angelegt:

- **Pfad:** `memory/references/topics/<topic>/subtopics/<subtopic>/events/<event-slug>/`
  *(Oder entsprechend bei Projekten: `memory/references/projects/<projekt>/events/<event-slug>/`)*

---

## Ordnerstruktur
* `index.md` — Die zentrale Event-Übersicht (Programm, Keynotes, Metadaten und Verlinkungen).
* `action-items.md` — Liste offener To-Dos und Folgeaufgaben aus Sitzungen (Vorstufe/Triage vor Todoist).
* 📂 `recordings/` — Lokale Meeting-Zusammenfassungen und Transkripte des Events (z. B. `*.summary.md`).
* 📂 `notes/` — Manuelle Notizen, Mitschriften oder Gedanken.

---

## `index.md` (Template)

```md
---
title: "<Event-Titel>"
date: "YYYY-MM-DD"
location: "<Ort, Land>"
website: "<Link zur Event-Website>"
---

# <Event-Titel>

Kompakte Zusammenfassung der wichtigsten Informationen, Termine und Quellen zum Event.

---

## 📅 Allgemeine Eckdaten
* **Datum:** YYYY-MM-DD
* **Ort:** <Ort, Land>
* **Gastgeber:** <Institution/Veranstalter>
* **Thema/Titel:** *<Fokus/Thema der Veranstaltung>*

---

## 📂 Thematische Schwerpunkte (Strands / Tracks)
* **Strand 1:** <Titel> — <Kurzbeschreibung>
* **Strand 2:** <Titel> — <Kurzbeschreibung>

---

## 🗓️ Programmablauf & Aufzeichnungen
*(Hier wird das Programm tageweise aufgelistet. Aufzeichnungen/Zusammenfassungen werden direkt beim jeweiligen Programmpunkt verlinkt)*

### Tag 1 — YYYY-MM-DD
* **HH:MM Uhr:** <Programmpunkt-Name> (z. B. Eröffnung)
* **HH:MM Uhr:** **Keynote 1:** <Vortragstitel> (Speaker: <Name>)  
  ➡️ **Aufzeichnung:** [Meeting-Zusammenfassung](./recordings/YYYY-MM-DD-<slug>.summary.md) (ID: `<meeting-id>`)

---

## 💰 Gebühren & Fristen
* **Early Bird (bis DD.MM.YYYY):** <Preis> (z. B. Mitglieder: X € / Nicht-Mitglieder: Y €)
* **Wichtige Fristen:**
  * Abstract-Einreichung: DD.MM.YYYY
  * Registrierungsschluss: DD.MM.YYYY

---

## 🔗 Wichtige Quellen & Kontakte
* **Offizielle Event-Seite:** [<Name>](<Link>)
* **Programm-Download (PDF):** [<Name>](<Link-Online>) | [Lokales PDF](/Agent-Share/<Pfad-zu-PDF>) | [Lokales Markdown](/Agent-Share/<Pfad-zu-MD>)
* **Kontakte:** [Name <email>](mailto:email)
```

---

## `action-items.md` (Template)

```md
# Action Items — <Event-Titel>

Aus den Sitzungen extrahierte Aufgaben und To-Dos. Nach Durchsicht und Triage können diese nach Todoist übertragen werden.

## Aus Keynote/Vortrag <Name>
- [ ] **<Aufgabe>:** <Detaillierte Beschreibung der Aktion>
- [ ] **<Aufgabe>:** <Detaillierte Beschreibung der Aktion>
```

---

## Pfad- und Linkregeln
1. **Workspace-Links:** Innerhalb des Workspace (z. B. von `index.md` auf `recordings/...` oder `action-items.md`) immer **relative** Pfade verwenden.
2. **Agent-Share-Links:** Links to original documents (PDFs, large XMLs, etc.) on BokuDrive must begin with the system-neutral prefix `/Agent-Share/` (e.g. `/Agent-Share/LLL-Networks/...`).
3. **Deadlines**: All deadlines in the future must be automatically entered into Todoist.
