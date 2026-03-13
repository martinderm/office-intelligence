---
name: topic-catalog-entry
description: Topic-Katalog-Pflege für mail-processor. Verwende diesen Skill, wenn neue Topics strukturiert in memory/references/topics/topics.json angelegt oder bestehende Topic-Einträge aktualisiert/ergänzt werden sollen – entweder interaktiv per Einzelfragen (id, title, mailbox_folder, domains, contacts, aliases, keywords, subject patterns) oder durch Einlesen einer Markdown-Vorlage mit denselben Feldern.
---

# topic-catalog-entry

Lege neue Topics strukturiert und reviewbar an oder aktualisiere bestehende Topic-Katalogeinträge gezielt.

## Ziel

- Konsistente Einträge in `memory/references/topics/topics.json`
- Strukturierte Neuanlage **und** Updates bestehender Topics
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

4. Erzeuge einen JSON-Block im `topics.json`-Format.

5. Zeige vor dem Schreiben immer eine kurze Review-Zusammenfassung.

6. Schreibe erst nach expliziter Freigabe in `topics.json`.

## Zielschema (pro Topic)

```json
{
  "id": "string",
  "title": "string",
  "mailbox_folder": "string",
  "aliases": ["string"],
  "keywords": ["string"],
  "domains": ["string"],
  "contacts": [{ "name": "string", "email": "string", "role": "string" }],
  "typical_subject_patterns": ["string"],
  "routing_priority": 70,
  "do_not_route_if": ["newsletter", "no-reply"],
  "updated_at": "YYYY-MM-DD",
  "schema_version": 1
}
```

## Questionnaire-Mode

Frage in dieser Reihenfolge, kurz und präzise:

1. Topic-Titel (`title`)
2. Topic-ID (`id`, sonst aus Titel vorschlagen)
3. Zielordner (`mailbox_folder`)
4. Domains
5. Kontakte (Name + E-Mail + Rolle optional)
6. Aliases
7. Keywords
8. Typical subject patterns
9. Routing-Priorität / do_not_route_if

Regeln:
- Wenn Feld unbekannt ist: leeres Array oder sinnvoller Default.
- Keine Felder erfinden, die nicht genannt wurden.

## Template-Mode

Wenn der User eine Markdown-Vorlage liefert, parse nach den Überschriften aus `references/topic-template.md`.

- Fehlende Pflichtfelder aktiv nachfragen.
- Leere optionale Felder als `[]` oder weglassen (je nach bestehendem Stil in topics.json).

## Validierung

Vor Ausgabe prüfen:
- `id` nur `[a-z0-9-]`
- keine doppelten Domains/Kontakte
- `contacts[].email` syntaktisch plausibel
- `schema_version = 1`

## Schreibregeln

- Nie blind überschreiben.
- Immer bestehenden JSON-Stil beibehalten.
- Nur minimal patchen.
- Bei mehreren neuen/aktualisierten Topics: gesammelt als ein Patch.

## Anti-Duplikat-Regel

- Keine inhaltliche Doppelpflege zwischen `projects.json` und `topics.json`.
- Querverweise sparsam halten (IDs/Links), nur wenn routing- oder kontextrelevant.
- Workpackages bleiben ausschließlich unter Projekten.
