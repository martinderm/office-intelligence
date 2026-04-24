---
name: project-catalog-entry
description: Projektkatalog- und Projektarbeitsstruktur-Pflege innerhalb von office-intelligence. Verwende diesen Skill, wenn Projekte im Katalog `memory/references/projects/projects.json` angelegt/aktualisiert werden oder die zugehörige Projektreferenz unter `memory/references/projects/<slug>/` als Projektmanagement-, Arbeits- und Wissensstruktur gepflegt werden soll. `mail-processor` nutzt diese Strukturen für Projekt-Matching und Routing, ist aber nicht der gesamte fachliche Rahmen. Nutze ihn für Neuanlagen und Updates per Q&A oder Markdown-Vorlage (id, title, mailbox_folder, domains, contacts, aliases, keywords, subject patterns, workpackages).
---

# project-catalog-entry

Pflege projektmanagement-relevante Projektdaten, Projekt-Routingdaten und Projektdokumentation getrennt, konsistent und reviewbar, als Teil von `office-intelligence`.

## Zielbild (verbindlich)

Unterscheide immer zwei Ebenen:

1. **Strukturierte Projekt-Metadaten** → `memory/references/projects/projects.json`
2. **Inhaltliche und operative Projektdoku** → `memory/references/projects/<slug>/`

Diese Ebene gehört fachlich zu `office-intelligence`; `mail-processor` konsumiert davon nur die routing- und matchingrelevanten Teile.

Für neue Projekte gilt: **nicht nur JSON-Eintrag**, sondern auch **Projektordner-Struktur** anlegen.

## Verbindliche Ordnerstruktur bei Neuanlage

Lege für neue Projekte an:

- `memory/references/projects/<slug>/index.md`
- `memory/references/projects/<slug>/contacts.md`
- `memory/references/projects/<slug>/signals.md`
- `memory/references/projects/<slug>/workpackages/` (Ordner)
- optional: `memory/references/projects/<slug>/evidence/` (Ordner)

Regeln:

- `mailbox_folder` ist der fachliche Parent-Ordner des Projekts.
- Antwortbedürftige Projektmails landen operativ im Child-Ordner `<mailbox_folder>/_Needs-Reply`.
- Der `_Needs-Reply`-Child muss nicht in `projects.json` als eigenes Feld gepflegt werden; `mail-processor` leitet ihn ab und meldet fehlende Ordner als `pending-decisions`.
- `reference_md` zeigt standardmäßig auf `memory/references/projects/<slug>/index.md`.
- **Keine ausführliche Projektdoku in `projects.json`.**
- **Keine Einzeldatei `memory/references/projects/<slug>.md` als Hauptreferenz.**
- Falls eine alte Einzeldatei existiert: nur als kurzer Redirect/Deprecation-Hinweis verwenden.

## Arbeitsmodus

1. Modus ermitteln:
   - Vorlage vorhanden → `template-mode`
   - sonst → `questionnaire-mode`
2. Daten im gemeinsamen Zielschema sammeln.
3. Pflichtfelder validieren (`id`, `title`, `mailbox_folder`).
4. Bei Neuanlage: Projektordner-Struktur planen/erzeugen.
5. JSON-Block erzeugen (projects.json-Format).
6. Vor Schreiben immer eine kurze Review-Zusammenfassung zeigen (JSON + Dateipfade).
7. Erst nach expliziter Freigabe schreiben.

## Zielschema (pro Projekt)

```json
{
  "id": "string",
  "title": "string",
  "mailbox_folder": "string",
  "reference_md": "string",
  "aliases": ["string"],
  "keywords": ["string"],
  "domains": ["string"],
  "contacts": [{ "name": "string", "email": "string" }],
  "workpackages": [
    {
      "id": "string",
      "title": "string",
      "aliases": ["string"],
      "keywords": ["string"],
      "contacts": [{ "email": "string" }],
      "status": "active"
    }
  ],
  "description": "string",
  "typical_subject_patterns": ["string"],
  "routing_priority": 50,
  "do_not_route_if": ["newsletter", "no-reply"],
  "updated_at": "YYYY-MM-DD",
  "schema_version": 1
}
```

## Questionnaire-Mode

Frage in dieser Reihenfolge, kurz und präzise:

1. Projektname (`title`)
2. Projekt-ID (`id`, sonst aus Titel als slug vorschlagen)
3. Zielordner (`mailbox_folder`, Parent-Ordner; `_Needs-Reply` wird davon abgeleitet)
4. Domains
5. Kontakte (Name + E-Mail)
6. Aliases
7. Keywords
8. Typical subject patterns
9. Workpackages (optional)
10. Routing-Priorität / `do_not_route_if`

Dann:

11. `reference_md` auf `memory/references/projects/<slug>/index.md` setzen (Default)
12. Fehlende Inhalte für `index.md`, `contacts.md`, `signals.md` kurz abfragen (oder mit Platzhaltern anlegen)

Regeln:

- Wenn Feld unbekannt: leeres Array oder sinnvoller Default.
- Keine zusätzlichen Felder erfinden.

## Template-Mode

Wenn eine Markdown-Vorlage geliefert wird, parse nach `references/project-template.md`.

- Fehlende Pflichtfelder aktiv nachfragen.
- Leere optionale Felder als `[]` oder weglassen (gemäß bestehendem Stil).
- Auch im Template-Mode bei Neuanlage die Projektordner-Struktur anlegen.

## Validierung

Vor Ausgabe prüfen:

- `id` nur `[a-z0-9-]`
- keine doppelten Domains/Kontakte
- `contacts[].email` syntaktisch plausibel
- `workpackages[].id` ebenfalls slug
- `schema_version = 1`
- `reference_md` passt zum `<slug>/index.md`-Pfad (außer bewusstes Legacy-Override)

## Schreibregeln

- Nie blind überschreiben.
- Bestehenden JSON-Stil beibehalten.
- Nur minimal patchen.
- Bei mehreren neuen Projekten: gesammelt als ein Patch.
- Struktur zuerst konsistent planen, dann in einem sauberen Schritt schreiben.
