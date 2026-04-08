---
name: topic-catalog-entry
description: Topic-Katalog- und Topic-Arbeitsstruktur-Pflege innerhalb von office-intelligence. Verwende diesen Skill, wenn Topics in `memory/references/topics/topics.json` angelegt/aktualisiert werden oder die zugehörige Topic-Referenz unter `memory/references/topics/<slug>/` als thematische Arbeits- und Wissensstruktur gepflegt werden soll. `mail-processor` nutzt diese Strukturen für Topic-Matching und Routing, ist aber nicht der gesamte fachliche Rahmen. Nutze ihn für Neuanlagen und Updates per Q&A oder Markdown-Vorlage (id, title, mailbox_folder, domains, contacts, aliases, keywords, subject patterns, subtopics).
---

# topic-catalog-entry

Pflege thematische Arbeitsstruktur, Topic-Routingdaten und Topic-Dokumentation getrennt, konsistent und reviewbar, als Teil von `office-intelligence`.

## Zielbild (verbindlich)

Unterscheide immer zwei Ebenen:

1. **Strukturierte Topic-Metadaten** → `memory/references/topics/topics.json`
2. **Inhaltliche und operative Topic-Doku** → `memory/references/topics/<slug>/`

Diese Ebene gehört fachlich zu `office-intelligence`; `mail-processor` konsumiert davon nur die routing- und matchingrelevanten Teile.

Für neue Topics gilt: **nicht nur JSON-Eintrag**, sondern auch **Topic-Ordner-Struktur** anlegen.

## Verbindliche Ordnerstruktur bei Neuanlage

Lege für neue Topics an:

- `memory/references/topics/<slug>/index.md`
- `memory/references/topics/<slug>/contacts.md`
- `memory/references/topics/<slug>/signals.md`
- `memory/references/topics/<slug>/subtopics/` (Ordner)
- optional: `memory/references/topics/<slug>/evidence/` (Ordner)

Regeln:

- `reference_md` zeigt standardmäßig auf `memory/references/topics/<slug>/index.md`.
- **Keine ausführliche Topic-Doku in `topics.json`.**
- **Keine Einzeldatei `memory/references/topics/<slug>.md` als Hauptreferenz.**
- Falls eine alte Einzeldatei existiert: nur als kurzer Redirect/Deprecation-Hinweis verwenden.

## Arbeitsmodus

1. Modus ermitteln:
   - Vorlage vorhanden → `template-mode`
   - sonst → `questionnaire-mode`
2. Daten im gemeinsamen Zielschema sammeln.
3. Pflichtfelder validieren (`id`, `title`, `mailbox_folder`).
4. Bei Neuanlage: Topic-Ordner-Struktur planen/erzeugen.
5. JSON-Block erzeugen (`topics.json`-Format).
6. Vor Schreiben immer eine kurze Review-Zusammenfassung zeigen (JSON + Dateipfade).
7. Erst nach expliziter Freigabe schreiben.

## Zielschema (pro Topic)

```json
{
  "id": "string",
  "title": "string",
  "mailbox_folder": "string",
  "reference_md": "string",
  "aliases": ["string"],
  "keywords": ["string"],
  "domains": ["string"],
  "contacts": [{ "name": "string", "email": "string", "role": "string" }],
  "subtopics": [
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
  "routing_priority": 70,
  "do_not_route_if": ["newsletter", "no-reply"],
  "updated_at": "YYYY-MM-DD",
  "schema_version": 1
}
```

## Questionnaire-Mode

Frage in dieser Reihenfolge, kurz und präzise:

1. Topic-Titel (`title`)
2. Topic-ID (`id`, sonst aus Titel als slug vorschlagen)
3. Zielordner (`mailbox_folder`)
4. Domains
5. Kontakte (Name + E-Mail + Rolle optional)
6. Aliases
7. Keywords
8. Typical subject patterns
9. Subtopics (optional)
10. Routing-Priorität / `do_not_route_if`

Dann:

11. `reference_md` auf `memory/references/topics/<slug>/index.md` setzen (Default)
12. Fehlende Inhalte für `index.md`, `contacts.md`, `signals.md` kurz abfragen (oder mit Platzhaltern anlegen)

Regeln:

- Wenn Feld unbekannt: leeres Array oder sinnvoller Default.
- Keine zusätzlichen Felder erfinden.

## Template-Mode

Wenn eine Markdown-Vorlage geliefert wird, parse nach `references/topic-template.md`.

- Fehlende Pflichtfelder aktiv nachfragen.
- Leere optionale Felder als `[]` oder weglassen (gemäß bestehendem Stil).
- Auch im Template-Mode bei Neuanlage die Topic-Ordner-Struktur anlegen.

## Validierung

Vor Ausgabe prüfen:

- `id` nur `[a-z0-9-]`
- keine doppelten Domains/Kontakte
- `contacts[].email` syntaktisch plausibel
- `subtopics[].id` ebenfalls slug
- `schema_version = 1`
- `reference_md` passt zum `<slug>/index.md`-Pfad (außer bewusstes Legacy-Override)

## Schreibregeln

- Nie blind überschreiben.
- Bestehenden JSON-Stil beibehalten.
- Nur minimal patchen.
- Bei mehreren neuen Topics: gesammelt als ein Patch.
- Struktur zuerst konsistent planen, dann in einem sauberen Schritt schreiben.

## Anti-Duplikat-Regel

- Keine inhaltliche Doppelpflege zwischen `projects.json` und `topics.json`.
- Querverweise sparsam halten (IDs/Links), nur wenn routing- oder kontextrelevant.
- Workpackages bleiben bei Projekten; Topics verwenden stattdessen `subtopics`.
