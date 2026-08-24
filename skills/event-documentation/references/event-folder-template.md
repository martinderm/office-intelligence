---
document_type: template
evidence_level: normative
status: accepted
title: "Template — Event / Conference Folder Layout"
---

# Template — Event / Conference Folder Layout

Für jedes größere Event (z. B. Jahrestagung, Konferenz, mehrtägiges Seminar) wird die Ablage nach dem Dual-Evidence-Standard sauber in Säule 1 (Normativ) und Säule 2 (Empirisch) gegliedert:

- **Säule 1 (Programm & Offizielle Metadaten):**
  `memory/references/topics/<topic>/subtopics/<subtopic>/events/<event-slug>/index.md`
  *(Oder entsprechend bei Projekten: `memory/references/projects/<projekt>/events/<event-slug>/index.md`)*
- **Säule 2 (Aufzeichnungen, Mitschriften & Aufgaben):**
  `memory/evidence/topics/<topic>/events/<event-slug>/`
  *(Oder entsprechend bei Projekten: `memory/evidence/projects/<projekt>/events/<event-slug>/`)*

---

## 2-Säulen-Ordnerstruktur

```
memory/
├── references/topics/<topic>/subtopics/<subtopic>/events/<event-slug>/
│   └── index.md                # Die offizielle Event-Übersicht (Programm, Keynotes, Links)
│
└── evidence/topics/<topic>/events/<event-slug>/
    ├── action-items.md         # Triage-Liste offener Aufgaben vor Todoist
    ├── recordings/             # Lokale Meeting-Zusammenfassungen & Transkripte (*.summary.md)
    └── notes/                  # Manuelle Notizen, Mitschriften & Beobachtungen
```

---

## `index.md` (Template — Säule 1)

```md
---
document_type: event-spec
evidence_level: normative
status: accepted
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
*(Hier wird das Programm tageweise aufgelistet. Aufzeichnungen/Zusammenfassungen in Säule 2 werden relativ verlinkt)*

### Tag 1 — YYYY-MM-DD
* **HH:MM Uhr:** <Programmpunkt-Name> (z. B. Eröffnung)
* **HH:MM Uhr:** **Keynote 1:** <Vortragstitel> (Speaker: <Name>)  
  ➡️ **Aufzeichnung:** [Meeting-Zusammenfassung](../../../../../evidence/topics/<topic>/events/<event-slug>/recordings/YYYY-MM-DD-<slug>.summary.md) (ID: `<meeting-id>`)

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

## `action-items.md` (Template — Säule 2)

```md
---
document_type: action-items
evidence_level: observed
status: draft
topic: <topic>
event: <event-slug>
---

# Action Items — <Event-Titel>

Aus den Sitzungen extrahierte Aufgaben und To-Dos. Nach Durchsicht und Triage werden Zukunftsfristen und Aktionspunkte nach Todoist übertragen.

## Aus Keynote/Vortrag <Name>
- [ ] **<Aufgabe>:** <Detaillierte Beschreibung der Aktion>
  * *Todoist-Sync:* `description: "Quelle: [EVID-YYYY-MM-DD-01] Event <Event-Titel> (Keynote <Name>)"`
- [ ] **<Aufgabe>:** <Detaillierte Beschreibung der Aktion>
```

---

## Pfad- und Linkregeln
1. **Workspace-Links:** Innerhalb des Workspace immer **relative** Pfade verwenden (z. B. von Säule 1 `index.md` nach Säule 2 `recordings/...`).
2. **Agent-Share-Links:** Links zu Originaldokumenten auf BokuDrive (`Agent-Share/`) beginnen mit dem systemneutralen Präfix `/Agent-Share/` (z. B. `/Agent-Share/LLL-Networks/...`).
3. **Deadlines & Todoist-Attribution:** Zukunftsfristen in Todoist eintragen und im Feld `description` stets den Beleganker mitführen.
