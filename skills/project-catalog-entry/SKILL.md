---
name: project-catalog-entry
description: Projektkatalog-Pflege für mail-processor. Verwende diesen Skill, wenn neue Projekte strukturiert in memory/references/projects/projects.json angelegt oder bestehende Projektdaten aktualisiert/ergänzt werden sollen – entweder interaktiv per Einzelfragen (id, title, mailbox_folder, domains, contacts, aliases, keywords, subject patterns, workpackages) oder durch Einlesen einer Markdown-Vorlage mit denselben Feldern.
---

# project-catalog-entry

Lege neue Projekte strukturiert und reviewbar an oder aktualisiere bestehende Katalogeinträge gezielt.

## Ziel

- Konsistente Einträge in `memory/references/projects/projects.json`
- Strukturierte Neuanlage **und** Updates bestehender Projekte
- Keine Freitext-Wildwest-Felder
- Entweder **interaktiv (Q&A)** oder **Markdown-Vorlage**

## Arbeitsmodus

1. Ermittele den Modus:
   - Wenn der User eine Vorlage liefert → `template-mode`
   - Sonst → `questionnaire-mode`

2. Sammle die Daten im gemeinsamen Zielschema.

3. Validiere Pflichtfelder:
   - `id` (slug)
   - `title`
   - `mailbox_folder`

4. Erzeuge einen JSON-Block im projects.json-Format.

5. Zeige vor dem Schreiben immer eine kurze Review-Zusammenfassung.

6. Schreibe erst nach expliziter Freigabe in `projects.json`.

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
2. Gewünschte Projekt-ID (`id`, sonst aus Titel vorschlagen)
3. Zielordner (`mailbox_folder`)
4. Referenzdatei (`reference_md`, optional)
5. Domains
6. Kontakte (Name + E-Mail)
7. Aliases
8. Keywords
9. Typical subject patterns
10. Workpackages (optional)
11. Routing-Priorität / do_not_route_if

Regeln:
- Wenn Feld unbekannt ist: leeres Array oder sinnvoller Default.
- Keine Felder erfinden, die nicht genannt wurden.

## Template-Mode

Wenn der User eine Markdown-Vorlage liefert, parse nach den Überschriften aus `references/project-template.md`.

- Fehlende Pflichtfelder aktiv nachfragen.
- Leere optionale Felder als `[]` oder weglassen (je nach bestehendem Stil in projects.json).

## Validierung

Vor Ausgabe prüfen:
- `id` nur `[a-z0-9-]`
- keine doppelten Domains/Kontakte
- `contacts[].email` syntaktisch plausibel
- `workpackages[].id` ebenfalls slug
- `schema_version = 1`

## Schreibregeln

- Nie blind überschreiben.
- Immer bestehenden JSON-Stil beibehalten.
- Nur minimal patchen.
- Bei mehreren neuen Projekten: gesammelt als ein Patch.
