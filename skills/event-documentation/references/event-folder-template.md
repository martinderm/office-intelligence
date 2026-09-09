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
    ├── YYYY-MM.md              # Quellengebundene Mail- und Veranstaltungs-Evidence
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
title: "<Event-Titel>"
starts_on: "YYYY-MM-DD"
ends_on: "YYYY-MM-DD"
status: active
phase: planned
location: "<Ort, Land>"
website: "<Link zur Event-Website>"
cloud_storage_id: "<storage_id>"
cloud_storage_scope: "topic|subtopic"
cloud_filemap: "<workspace-relativer Pfad aus cloud_sync.<storage_id>.output_json>"
---

# <Event-Titel>

Kompakte Zusammenfassung der wichtigsten Informationen, Termine und Quellen zum Event.

---

## 📅 Allgemeine Eckdaten
* **Start:** YYYY-MM-DD
* **Ende:** YYYY-MM-DD (optional)
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
  ➡️ **Aufzeichnung (Topic/Subtopic):** [Meeting-Zusammenfassung](../../../../../../../evidence/topics/<topic>/events/<event-slug>/recordings/YYYY-MM-DD-<slug>.summary.md) (ID: `<meeting-id>`)
  ➡️ **Aufzeichnung (Projekt):** [Meeting-Zusammenfassung](../../../../../evidence/projects/<project>/events/<event-slug>/recordings/YYYY-MM-DD-<slug>.summary.md) (ID: `<meeting-id>`)

---

## 💰 Gebühren & Fristen
* **Early Bird (bis DD.MM.YYYY):** <Preis> (z. B. Mitglieder: X € / Nicht-Mitglieder: Y €)
* **Wichtige Fristen:**
  * Abstract-Einreichung: DD.MM.YYYY
  * Registrierungsschluss: DD.MM.YYYY

---

## 🔗 Wichtige Quellen & Kontakte
* **Offizielle Event-Seite:** [<Name>](<Link>)
* **Cloud-Atlas-Speicher:** `cloud_sync.<storage_id>` — Filemap: `<cloud_filemap>`
* **Programm-Download (PDF):** [<Name>](<Link-Online>) | [Cloud-Atlas-Original](<relativer-Link-aus-scan_dir>) | [Cloud-Atlas-Markdown-Mirror](<relativer-Link-aus-output_dir>)
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
1. **Katalog zuerst:** Ein Topic-Event wird vor jeder Ordneranlage als strukturell valider `subtopics[].events[]`-Eintrag erfasst: slug, Titel, ISO-Start/optional Ende, kanonisches Dossier und `cloud_storage` mit Scope/ID. Die Storage-ID muss im deklarierten Scope einem bestehenden `cloud_sync.<storage_id>` entsprechen. Ein nichtkanonisches oder fehlendes Dossier sowie unpassender Storage sind Review, nie ein improvisierter Pfad.
2. **Workspace-Links:** Innerhalb des Workspace immer **relative** Pfade verwenden. Vom Topic-/Subtopic-Event-`index.md` führt ein Link zu `memory/evidence/topics/<topic>/events/<event-slug>/recordings/...` über `../../../../../../../evidence/topics/<topic>/events/<event-slug>/recordings/...`; vom Projekt-Event-`index.md` über `../../../../../evidence/projects/<project>/events/<event-slug>/recordings/...`. Bei anderen Quell- oder Zielpfaden den relativen Link aus den tatsächlichen Workspace-Pfaden ableiten, nicht eines dieser Beispiele übernehmen.
3. **Cloud-Atlas-Routing:** `cloud_storage.scope: topic` löst die ID nur im Parent-`cloud_sync` auf, `scope: subtopic` nur im Subtopic-`cloud_sync`; es gibt keinen Fallback. Fehlt die Konfiguration, sie ausschließlich mit `project-catalog-entry` beziehungsweise `topic-catalog-entry` pflegen; erst danach Cloud Atlas für Synchronisation, Konvertierung und Filemap nutzen. `scan_dir`, `output_dir` und die Filemap-Ausgaben daraus auflösen; alle müssen workspace-relativ sein. Keine neuen Mounts, absoluten Benutzerpfade oder unkonfigurierten Legacy-Ablagen.
4. **Cloud-Atlas-Links:** Originale werden aus dem aufgelösten `scan_dir`, Markdown-Mirrors aus dem aufgelösten `output_dir` und Filemap-Verweise aus `output_json`/`output_md` relativ zum Event-Ordner verlinkt. `cloud_storage_scope`, `cloud_storage_id` und `cloud_filemap` müssen zur gewählten Konfiguration passen.
5. **Deadlines & Todoist-Attribution:** Zukunftsfristen in Todoist eintragen und im Feld `description` stets den Beleganker mitführen.
