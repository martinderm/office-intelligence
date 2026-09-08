---
name: project-catalog-entry
description: Projektkatalog- und Projektarbeitsstruktur-Pflege innerhalb von office-intelligence. Verwende diesen Skill für schema-v3-konforme Projekte, Routing-Metadaten, Workpackages, Tasks, Deliverables und projektweite Milestones.
---

# project-catalog-entry

Pflege projektmanagement-relevante Projektdaten, Projekt-Routingdaten und Projektdokumentation getrennt, konsistent und reviewbar, als Teil von `office-intelligence`. `mail-processor` nutzt die Strukturen für Root-Matching und Routing, ist aber nicht der gesamte fachliche Rahmen. FR-01a erweitert `mail-desk` nicht funktional.

## Zielbild (verbindlich — Dual Evidence Standard)

Unterscheide immer die beiden Säulen des Dual-Evidence-Standards:

1. **Säule 1 (De Jure / Normativ & Struktur):**
   - Strukturierte Projekt-Metadaten → `memory/references/projects/projects.json`
   - Inhaltliche & projektbezogene Referenzdoku → `memory/references/projects/<slug>/` (`index.md`, `contacts.md`, `signals.md`, `workpackages/`)
2. **Säule 2 (De Facto / Empirisch & Operativ):**
   - Chronologische Evidenz-Logs & operative Nachweise → `memory/evidence/projects/<slug>/` (`YYYY-MM.md`)

Für neue Projekte gilt: **nicht nur JSON-Eintrag**, sondern auch Projektordner-Struktur in beiden Säulen anlegen.

## Verbindliche Ordnerstruktur bei Neuanlage

Lege für neue Projekte an:

- `memory/references/projects/<slug>/index.md`
- `memory/references/projects/<slug>/contacts.md`
- `memory/references/projects/<slug>/signals.md`
- `memory/references/projects/<slug>/workpackages/`
- `memory/evidence/projects/<slug>/` für chronologische Evidenz-Logs `YYYY-MM.md`
- optional `memory/references/projects/<slug>/events/` für Events und Konferenzen

Regeln & Dual-Path-Fallback:

- Lesezugriff auf Evidenzen prüft zuerst `memory/evidence/projects/<slug>/`, danach als Legacy-Fallback `memory/references/projects/<slug>/evidence/`.
- Neue Evidenzeinträge, Transkripte und Logs werden stets in `memory/evidence/projects/<slug>/` abgelegt.
- `mailbox_folder` ist der fachliche Parent-Ordner. Antwortbedürftige Projektmails gehören operativ in `<mailbox_folder>/_Needs-Reply`.
- Der `_Needs-Reply`-Child wird nicht als eigenes Katalogfeld gepflegt; `mail-processor` leitet ihn ab und meldet fehlende Ordner als `pending-decisions`.
- `reference_md` zeigt standardmäßig auf `memory/references/projects/<slug>/index.md`.
- Keine ausführliche Projektdoku in `projects.json` und keine Einzeldatei `memory/references/projects/<slug>.md` als Hauptreferenz. Alte Einzeldateien bleiben nur kurze Redirect-/Deprecation-Hinweise.

Frontmatter-Regel:

- Katalog-/domänenspezifische Frontmatter-Metadaten sind erlaubt.
- Bedeutung und Felddefinitionen liegen in `memory/references/frontmatter-spec-*.md`.
- Bei Unklarheit zuerst die passende Spezifikation prüfen, dann schreiben.

## Arbeitsmodus

1. Modus ermitteln: Vorlage vorhanden → `template-mode`, sonst `questionnaire-mode`.
2. Daten im gemeinsamen Zielschema sammeln.
3. Pflichtfelder validieren (`id`, `title`, `mailbox_folder`).
4. Bei Neuanlage die Projektordner-Struktur planen/erzeugen.
5. JSON-Block erzeugen.
6. Vor Schreiben eine kurze Review-Zusammenfassung mit JSON und Dateipfaden zeigen.
7. Erst nach expliziter Freigabe schreiben.

Vor jeder Mutation ist außerdem die verifizierte `workspace-lock`-Ownership des Ziel-Workspaces erforderlich.

## Zielschema v3 (pro Projekt)

Der formale Vertrag ist [`references/projects.schema.json`](references/projects.schema.json). Der Root akzeptiert aus Kompatibilitätsgründen entweder eine Projektliste oder ein Objekt mit `projects`-Liste. Bestehende Root-Routingfelder und `cloud_sync` bleiben offen und kompatibel.

```json
{
  "id": "eu-example",
  "title": "EU Example Collaboration",
  "kuerzel": "EUEX",
  "mailbox_folder": "Projects/EU Example",
  "reference_md": "memory/references/projects/eu-example/index.md",
  "aliases": ["EUEX"],
  "keywords": ["quality"],
  "domains": ["example.eu"],
  "contacts": [{"name": "Example Contact", "email": "coordination@example.eu"}],
  "workpackages": [
    {
      "id": "wp1",
      "number": 1,
      "title": "Coordination and quality",
      "lead": "Example University",
      "boku_role": "Contributor",
      "status": "active",
      "aliases": ["WP1"],
      "keywords": ["quality"],
      "contacts": [{"email": "wp1@example.eu"}],
      "tasks": [{"id": "T1.1", "title": "Kick-off", "lead": "Example University", "keywords": ["kick-off"]}],
      "deliverables": [{"id": "D1.1", "title": "Quality plan", "lead": "BOKU", "type": "Report", "due_month": "M6"}]
    }
  ],
  "milestones": [
    {"id": "MS1", "title": "Kick-off held", "lead": "Example University", "due_month": "M2", "related_wps": ["wp1"], "prerequisites": "Grant agreement signed"}
  ],
  "typical_subject_patterns": ["[EUEX]"],
  "routing_priority": 50,
  "do_not_route_if": ["newsletter", "no-reply"],
  "cloud_sync": {"default": {"scan_dir": "cloud/projects/eu-example"}},
  "schema_version": 3
}
```

Jedes Projekt benötigt mindestens `id` (lowercase slug), `title`, `mailbox_folder`, `workpackages`, `milestones` und exakt `schema_version: 3`. Bestehende Felder wie `project_website`, `project_reference`, `laufzeit`, `gesamtbudget`, `institution_budget`, `boku_budget`, `description`, `updated_at`, Routing-Signale und Cloud-Metadaten bleiben zulässig.

`workpackages[].id` ist ein lowercase slug; `number` ist optional positiv; `status` ist `active`, `completed`, `planned` oder `paused`. WP-Objekte enthalten `tasks` und `deliverables`; deren Objekte benötigen jeweils `id` und `title`. Task- und Deliverable-IDs sind innerhalb des gesamten Projekts case-insensitiv eindeutig. Milestones liegen ausschließlich auf Projektebene, haben `id` und `title` und können `lead`, `due_month`, `related_wps` und `prerequisites` führen. `related_wps` verweist auf vorhandene WP-IDs. Unbekannte Felder sind in WP-, Task-, Deliverable- und Milestone-Objekten unzulässig; Kontakt- und Cloud-Objekte bleiben absichtlich offen.

## Questionnaire-Mode

Frage in dieser Reihenfolge kurz und präzise:

1. Projektname (`title`)
2. Projekt-ID (`id`, sonst slug vorschlagen)
3. Zielordner (`mailbox_folder`; `_Needs-Reply` wird abgeleitet)
4. Domains
5. Kontakte (Name + E-Mail)
6. Aliases
7. Keywords
8. Typical subject patterns
9. Workpackages: pro WP ID, optionale Nummer, Titel, Lead, BOKU-Rolle, Status, Routing-Signale, Tasks und Deliverables
10. Projektweite Milestones mit `related_wps` und optionalen Vorbedingungen
11. Routing-Priorität / `do_not_route_if`

Dann `reference_md` auf `memory/references/projects/<slug>/index.md` setzen und fehlende Inhalte für `index.md`, `contacts.md`, `signals.md` abfragen oder mit Platzhaltern anlegen. Wenn ein Feld unbekannt ist, leere optionale Arrays oder sinnvolle Defaults verwenden; keine zusätzlichen normativen Felder erfinden. Projekte ohne WPs führen `"workpackages": []` und `"milestones": []`.

## Template-Mode

Wenn eine Markdown-Vorlage geliefert wird, nach [`references/project-template.md`](references/project-template.md) parsen.

- Fehlende Pflichtfelder aktiv nachfragen.
- Leere optionale Felder als `[]` oder gemäß bestehendem Stil weglassen.
- WP-Dateien aus [`references/project-folder-template.md`](references/project-folder-template.md) erstellen; die relevante Kopie unter `memory/references/projects/_TEMPLATE-project.md` synchron halten.
- Auch im Template-Mode bei Neuanlage die Projektordner-Struktur anlegen.

## Validierung

Vor Ausgabe prüfen:

- `id` und `workpackages[].id` sind lowercase Slugs und eindeutig.
- Domains/Kontakte sind nicht doppelt, `contacts[].email` ist syntaktisch plausibel.
- WP-Nummern sind positiv, Statuswerte zulässig und alle erforderlichen Strings nicht leer.
- Task-, Deliverable- und Milestone-IDs sind projektweit case-insensitiv eindeutig.
- `related_wps` referenziert nur vorhandene WP-IDs.
- `reference_md` passt zum `<slug>/index.md`-Pfad, außer bei bewusstem Legacy-Override.

Nutze zusätzlich den read-only Standardbibliotheks-Validator:

```powershell
python skills/project-catalog-entry/scripts/validate_projects.py --catalog memory/references/projects/projects.json --json
```

Exit-Code `0` bedeutet `Valid`, `1` Schema-/Invariantenfehler mit JSON-Pfaden und `2` Input-, JSON-, Argument- oder Runtime-Fehler. Der Validator schreibt nie Dateien.

## Migration von v2 auf v3

Vor einem Backfill zuerst einen schreibfreien Gesamtlauf ausführen:

```powershell
python skills/project-catalog-entry/scripts/migrate_project_wps.py --catalog memory/references/projects/projects.json --projects-root memory/references/projects --json
```

`--project <slug>` darf für wiederholbare, projektweise Dry-runs verwendet werden; dabei wird die Vollkatalogvalidierung ausdrücklich als `deferred` ausgewiesen. Ein Apply wird nur ausgeführt, wenn der gesamte resultierende Katalog v3-valid ist, keine Diagnostics einschließlich Warnungen verbleiben und die Lock-Ownership des Ziel-Workspaces nachgewiesen ist:

```powershell
python skills/project-catalog-entry/scripts/migrate_project_wps.py --catalog memory/references/projects/projects.json --projects-root memory/references/projects --apply --workspace-root . --lease-id <lease-id> --json
```

`PendingReview` und `Invalid` schreiben nie. Vor jedem Apply den vollständigen Dry-run-Diff und alle Diagnostics human reviewen; reale Quellen oder Statuswerte nicht ergänzen, wenn sie nicht eindeutig belegt sind.

## Schreibregeln

- Nie blind überschreiben.
- Bestehenden JSON-Stil beibehalten.
- Nur minimal patchen.
- Mehrere neue Projekte gesammelt als einen Patch behandeln.
- Struktur zuerst konsistent planen, dann in einem sauberen Schritt schreiben.

## Backlog

FR-01a liefert v3-Vertrag, Validator, Vorlagen, Tests und den generischen Beispielkatalog. **FR-01b1** liefert zusätzlich `migrate_project_wps.py`: standardmäßig read-only Dry-run, nur eindeutig strukturierte Markdown-Quellen, `PendingReview` bei Mehrdeutigkeit und Apply ausschließlich mit kanonisch verifizierter Lock-Ownership. **FR-01b2 ist abgeschlossen:** Der produktive BOKU-Katalog wurde nach Human Review auf v3 migriert; Details und die bewusst akzeptierte EVOLVE-Warnung stehen in [`TODO.md`](TODO.md). Jeder spätere reale Backfill bleibt ein eigener, erneut freizugebender Lauf.

Ein menschlich freigegebener, konkreter Warning-Code kann für einen Apply explizit dokumentiert werden, etwa `--accept-warning unstable_checkpoint`. Der Code muss im selben Lauf tatsächlich auftreten; unbekannte Codes, Blocking-Diagnostics und pauschale Ignore-/Force-Mechanismen sind nicht zulässig.
