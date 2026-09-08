---
name: topic-catalog-entry
description: Topic-Katalog- und Topic-Arbeitsstruktur-Pflege innerhalb von office-intelligence. Verwende diesen Skill, wenn Topics in `memory/references/topics/topics.json` angelegt/aktualisiert werden oder die zugehörige slug-spezifische Topic-Referenz als thematische Arbeits- und Wissensstruktur gepflegt werden soll. `mail-processor` nutzt diese Strukturen für Topic-Matching und Routing, ist aber nicht der gesamte fachliche Rahmen. Nutze ihn für Neuanlagen und Updates per Q&A oder Markdown-Vorlage (id, title, mailbox_folder, domains, contacts, aliases, keywords, subject patterns, subtopics).
---

# topic-catalog-entry

Pflege thematische Arbeitsstruktur, Topic-Routingdaten und Topic-Dokumentation getrennt, konsistent und reviewbar, als Teil von `office-intelligence`.

## Zielbild (verbindlich — Dual Evidence Standard)

Unterscheide immer die beiden Säulen des Dual-Evidence-Standards:

1. **Säule 1 (De Jure / Normativ & Struktur):**
   - Strukturierte Topic-Metadaten → `memory/references/topics/topics.json`
   - Inhaltliche & thematische Referenzdoku → `memory/references/topics/<slug>/` (`index.md`, `contacts.md`, `signals.md`, `subtopics/`)
2. **Säule 2 (De Facto / Empirisch & Operativ):**
   - Chronologische Evidenz-Logs & operative Nachweise → `memory/evidence/topics/<slug>/` (`YYYY-MM.md`)

Diese Ebene gehört fachlich zu `office-intelligence`; `mail-processor` konsumiert davon nur die routing- und matchingrelevanten Teile.

Für neue Topics gilt: **nicht nur JSON-Eintrag**, sondern auch **Topic-Ordner-Struktur in beiden Säulen** anlegen.

## Verbindliche Ordnerstruktur bei Neuanlage

Lege für neue Topics an:

- `memory/references/topics/<slug>/index.md`
- `memory/references/topics/<slug>/contacts.md`
- `memory/references/topics/<slug>/signals.md`
- `memory/references/topics/<slug>/subtopics/` (Ordner)
- `memory/evidence/topics/<slug>/` (Ordner für chronologische Evidenz-Logs `YYYY-MM.md`)
- optional: `memory/references/topics/<slug>/subtopics/<subtopic-slug>/events/` (Ordner für Events und Konferenzen; Dokumentations-Workflow siehe Skill `event-documentation`)

Ein Subtopic ist Taxonomie. Ein wiederkehrender oder laufender Dauerprozess wird
nicht als Event modelliert, sondern optional als
`subtopics[].operations[]`. Der kanonische Operations-Index ist
`memory/references/topics/<topic>/subtopics/<subtopic>/operations/<operation>/index.md`;
der aktive Arbeitsstand liegt unter
`memory/operations/topics/<topic>/subtopics/<subtopic>/<operation>/` und seine
quellengebundene Mail-Evidence unter
`memory/evidence/topics/<topic>/subtopics/<subtopic>/operations/<operation>/YYYY-MM.md`.

Regeln & Dual-Path-Fallback:

- **Dual-Path Lesezugriff:** Bei Lesezugriffen auf Evidenzen wird zuerst `memory/evidence/topics/<slug>/` geprüft. Existiert dieser nicht, greift als Abwärtskompatibilität der Fallback auf das Legacy-Verzeichnis `memory/references/topics/<slug>/evidence/`.
- **Standard für Schreibzugriffe:** Neue Evidenzeinträge, Transkripte und Logs werden **stets in `memory/evidence/topics/<slug>/`** abgelegt.
- `mailbox_folder` ist der fachliche Parent-Ordner des Topics.
- Antwortbedürftige Topic-Mails landen operativ im Child-Ordner `<mailbox_folder>/_Needs-Reply`.
- Der `_Needs-Reply`-Child muss nicht in `topics.json` als eigenes Feld gepflegt werden; `mail-processor` leitet ihn ab und meldet fehlende Ordner als `pending-decisions`.
- `reference_md` zeigt standardmäßig auf `memory/references/topics/<slug>/index.md`.
- **Keine ausführliche Topic-Doku in `topics.json`.**
- **Keine Einzeldatei `memory/references/topics/<slug>.md` als Hauptreferenz.**
- Falls eine alte Einzeldatei existiert: nur als kurzer Redirect/Deprecation-Hinweis verwenden.

Frontmatter-Regel:

- Katalog-/domänenspezifische Frontmatter-Metadaten sind erlaubt.
- Bedeutung und Felddefinitionen werden in `memory/references/frontmatter-spec-*.md` gepflegt.
- Bei Unklarheiten zuerst die passende `frontmatter-spec-*.md` prüfen, dann schreiben.

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
      "typical_subject_patterns": ["string"],
      "contacts": [{ "email": "string" }],
      "reference_md": "memory/references/topics/<topic-slug>/subtopics/<subtopic-slug>.md (optional)",
      "cloud_sync": {
        "<storage_id>": {
          "scan_dir": "string",
          "output_json": "string",
          "output_md": "string",
          "output_dir": "string",
          "last_synced_at": "string (optional/automatisch, z. B. YYYY-MM-DD HH:MM:SS)"
        }
      },
      "operations": [
        {
          "id": "string-slug",
          "title": "string",
          "aliases": ["string"],
          "keywords": ["string"],
          "typical_subject_patterns": ["string"],
          "reference_md": "memory/references/topics/<topic-slug>/subtopics/<subtopic-slug>/operations/<operation-slug>/index.md (optional)",
          "status": "active"
        }
      ],
      "status": "active"
    }
  ],
  "description": "string",
  "typical_subject_patterns": ["string"],
  "routing_priority": 70,
  "do_not_route_if": ["newsletter", "no-reply"],
  "cloud_sync": {
    // Immer als Dictionary von Cloud-Speichern (z. B. {"default": {...}} bei einem Speicher):
    "<storage_id>": {
      "scan_dir": "string",
      "output_json": "string",
      "output_md": "string",
      "output_dir": "string",
      "last_synced_at": "string (optional/automatisch, z. B. YYYY-MM-DD HH:MM:SS)"
    }
  },
  "updated_at": "YYYY-MM-DD",
  "schema_version": 1
}
```

## Questionnaire-Mode

Frage in dieser Reihenfolge, kurz und präzise:

1. Topic-Titel (`title`)
2. Topic-ID (`id`, sonst aus Titel als slug vorschlagen)
3. Zielordner (`mailbox_folder`, Parent-Ordner; `_Needs-Reply` wird davon abgeleitet)
4. Domains
5. Kontakte (Name + E-Mail + Rolle optional)
6. Aliases
7. Keywords
8. Typical subject patterns
9. Subtopics (optional; je Subtopic Aliase, Keywords, `typical_subject_patterns` und Kontakte getrennt erheben)
10. Dauerprozesse je Subtopic (optional; je Operation ID, Titel, Aliase, Keywords,
    `typical_subject_patterns`, optionaler kanonischer Index und Status; keine
    Kontakte oder Cloud-Felder ohne konkret belegten Bedarf)
11. Routing-Priorität / `do_not_route_if`

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
- `subtopics[].typical_subject_patterns` ist das einzige Feld für Subtopic-Betreffmuster; keine parallelen Feldnamen einführen.
- `status: active` wird automatisch klassifiziert; für Legacy-Kataloge gilt ein fehlender Status kompatibel als aktiv, während explizit inaktive Einträge ignoriert werden. Ein Subtopic-Kontakt darf nur bei einem unabhängigen Parent-Topic-Betreffsignal als ergänzendes, eindeutiges Signal wirken.
- Ein optionales `subtopics[].reference_md` ist nur dann ein automatisches Syntheseziel, wenn es exakt auf die vorhandene kanonische Datei `memory/references/topics/<topic-slug>/subtopics/<subtopic-slug>.md` verweist; Event- oder Abschnittssemantik wird nicht geraten.
- `subtopics[].operations[].id` ist ein eindeutiger slug innerhalb seines
  Subtopics; fehlender Status bleibt Legacy-kompatibel aktiv, explizit inaktive
  Operations werden ignoriert. Doppelte IDs sind ein Review-Fall und dürfen nicht
  automatisch aufgelöst werden.
- `operations[].typical_subject_patterns` ist das einzige Operations-Feld für
  Betreffmuster. Ein optionales `operations[].reference_md` ist ausschließlich
  dann ein Syntheseziel, wenn es exakt auf die vorhandene kanonische
  `.../operations/<operation-slug>/index.md` zeigt; nichtkanonische Pfade bleiben
  ohne Ziel und reviewbar. Operations erhalten keine Kontakt- oder Cloud-Felder,
  solange dafür kein belegter Fachbedarf besteht.
- Bei Arbeit an einem Subtopic den `cloud_sync` des übergeordneten Topics als potenzielle Kontextquelle berücksichtigen und bei plausibler Relevanz dessen Filemap oder passende Spiegelungen prüfen; zusätzliche, ausschließlich subtopic-spezifische Speicher dürfen als `subtopics[].cloud_sync` im Cloud-Atlas-Schema gepflegt werden.
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
