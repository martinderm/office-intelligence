# References-Katalog (Projects + Topics)

Zentrale Doku für die Kataloge unter `memory/references/`.

## Ziel

- Strukturierte, reviewbare Katalogpflege für Projektmanagement und Mail-Routing/Triage
- Klare Trennung von Projekten und Topics
- Keine doppelten Inhalte zwischen Katalogen
- Allgemeine Datenbasis für Projekte und Topics: zentrale, konsistente Fachdokumentation außerhalb der Katalogfelder, referenzierbar über `index.md` und thematische Unterseiten in den jeweiligen Ordnern

## Wo stehen allgemeine Projekt-/Topic-Infos?

- **Nicht** in diesem Katalog-README und **nicht** in `projects.json`/`topics.json`-Feldern ausformulieren.
- Pro Projekt liegt die allgemeine Doku im jeweiligen Projektordner:
  - `.../projects/<projekt>/index.md` = Einstieg/Navigation
  - `.../projects/<projekt>/<slug>.md` = Detailseiten je Thema (z. B. `architecture.md`, `workflow.md`, `contacts.md`)
- Pro Topic liegt die allgemeine Doku im jeweiligen Topic-Ordner:
  - `.../topics/<topic>/index.md` = Einstieg/Navigation
  - `.../topics/<topic>/signals.md`, `.../topics/<topic>/contacts.md`, `.../topics/<topic>/subtopics/`
- In Katalogdateien (`projects.json`, `topics.json`) nur routingrelevante, strukturierte Metadaten pflegen.

## Pfad-Konvention

- `memory/references/projects/projects.json` — Projektkatalog
- `memory/references/topics/topics.json` — Topic-Katalog
- `memory/references/projects/inbox/` — Review-Queue für Discovery-Vorschläge
- `memory/references/projects/changelog.md` — Änderungsprotokoll

Env:
- `PROJECTS_JSON_PATH=./memory/references/projects/projects.json`
- `TOPICS_JSON_PATH=./memory/references/topics/topics.json`

## Modellregeln

- **Projects** und **Topics** sind gleichrangige Kataloge.
- **Workpackages** bleiben strikt unter `projects[].workpackages[]`.
- **Subtopics** bleiben strikt unter `topics[].subtopics[]`.
- Routing darf auf Project-, Topic-, Subtopic- und Workpackage-Ebene matchen.

## Anti-Duplikat-Regel (wichtig)

- Inhalte nicht doppelt in `projects.json` und `topics.json` pflegen.
- Kanonische Information lebt genau an einer Stelle.
- In der anderen Datei nur sparsame Querverweise (IDs/Links), nur wenn nötig.

## Minimal-Schema: projects.json

```json
[
  {
    "id": "usage-ng",
    "title": "USAGE-NG",
    "mailbox_folder": "Projekte/USAGE-NG",
    "aliases": ["USAGE NG"],
    "keywords": ["usage"],
    "domains": ["usage-ng.example.org"],
    "contacts": [{ "name": "Jane Doe", "email": "jane.doe@example.org" }],
    "workpackages": [
      {
        "id": "wp1",
        "title": "Curriculum Design",
        "aliases": ["WP1"],
        "keywords": ["syllabus"],
        "contacts": [{ "email": "jane.doe@example.org" }],
        "status": "active"
      }
    ],
    "typical_subject_patterns": ["USAGE"],
    "routing_priority": 50,
    "do_not_route_if": ["newsletter", "no-reply"],
    "updated_at": "2026-03-13",
    "schema_version": 1
  }
]
```

Pflicht pro Projekt:
- `id`, `title`, `mailbox_folder`

## Minimal-Schema: topics.json

```json
[
  {
    "id": "rpl-validation",
    "title": "Recognition of Prior Learning (RPL)",
    "mailbox_folder": "Topics/RPL",
    "reference_md": "memory/references/topics/rpl-validation/index.md",
    "aliases": ["RPL"],
    "keywords": ["validation", "prior learning"],
    "domains": ["example.org"],
    "contacts": [
      { "name": "Topic Lead", "email": "topic.lead@example.org", "role": "topic-lead" }
    ],
    "subtopics": [
      {
        "id": "portfolio-assessment",
        "title": "Portfolio Assessment",
        "aliases": ["Portfolio"],
        "keywords": ["portfolio", "assessment"],
        "contacts": [{ "email": "topic.lead@example.org" }],
        "status": "active"
      }
    ],
    "description": "Rahmen und Prozesse zur Anerkennung von Vorkenntnissen.",
    "typical_subject_patterns": ["RPL"],
    "routing_priority": 70,
    "do_not_route_if": ["newsletter", "no-reply"],
    "updated_at": "2026-03-24",
    "schema_version": 1
  }
]
```

Pflicht pro Topic:
- `id`, `title`, `mailbox_folder`

Empfohlen (Default bei Neuanlage):
- `reference_md` → `memory/references/topics/<slug>/index.md`

## Review-Flow

1. Discovery erzeugt Vorschläge in `projects/inbox/*.json`
2. Vorschläge reviewen
3. Nur freigegebene Änderungen in Kataloge übernehmen
4. Keine Blindübernahme

## Hinweise

- Bei Ambiguität: lieber nicht routen als falsch routen.
- Für bessere Trefferquote zuerst `domains` + `contacts` pflegen.
