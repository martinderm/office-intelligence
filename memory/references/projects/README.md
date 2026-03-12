# projects.json — Projektkatalog für Mail-Routing & Triage

Diese Struktur ist die **Source of truth** für die inhaltliche Zuordnung von E-Mails zu Projekten.
`mail-processor` nutzt den Katalog für:

- **Routing**: COPY in projektbezogene Mailbox-Ordner
- **Triage**: Projekt-Kandidaten + Evidenz (Domain/Kontakt/Subject-Muster)
- **Governance**: Änderungen an Projektdaten sind reviewbar (Git), statt „LLM schreibt Fakten“

> Ziel: Ein Agent soll diese Struktur **selbst anlegen können**, ohne vorheriges Wissen über deine Projekte.

---

## Pfad-Konvention

Empfohlener Pfad im Agent-Workspace:

- `memory/references/projects/projects.json`   (Katalog)
- `memory/references/projects/README.md`       (diese Doku)
- `memory/references/projects/inbox/`          (Review-Queue für Discovery-Vorschläge)
- `memory/references/projects/changelog.md`    (Änderungsprotokoll)
- `memory/references/projects/<id>/index.md`   (Projekt-Überblick)
- `memory/references/projects/<id>/signals.md` (Routing/Konsolidierung)
- `memory/references/projects/<id>/evidence/`  (append-only Evidenz, z. B. monatlich)
- `memory/references/projects/<id>/topics/`    (optionale Sub-Themen)

Im `.env` wird der Pfad referenziert:

- `PROJECTS_JSON_PATH=./memory/references/projects/projects.json`

---

## Minimaler Inhalt (MVP)

Für das erste funktionierende Routing brauchst du pro Projekt mindestens:

- eine **stabile ID** (`id`)
- einen **Titel** (`title`)
- einen **Mailbox-Zielordner** (`mailbox_folder`)

Alles andere verbessert Trefferquote und Robustheit.

---

## Vorschlag: Schema (v1)

`projects.json` ist ein JSON-Array (oder ein Objekt mit `projects: [...]` – entscheide dich für eins und bleib dabei).

Empfohlen: **Array** für einfache Verarbeitung.

Beispiel:

```json
[
  {
    "id": "usage-ng",
    "title": "USAGE-NG",
    "mailbox_folder": "Projekte/USAGE-NG",
    "reference_md": "memory/references/projects/usage-ng/index.md",

    "aliases": ["USAGE NG", "Usage NextGen"],
    "keywords": ["usage", "next gen"],
    "domains": ["usage-ng.example.org"],
    "contacts": [
      {"name": "Jane Doe", "email": "jane.doe@example.org"}
    ],

    "description": "Kurzbeschreibung, worum es in dem Projekt geht.",
    "typical_subject_patterns": ["USAGE", "[USAGE]"],

    "routing_priority": 50,
    "do_not_route_if": ["newsletter", "no-reply"],

    "updated_at": "2026-03-06",
    "schema_version": 1
  }
]
```

### Feld-Erklärung

Pflichtfelder (MVP):
- `id`: stabiler Identifier (slug; nur a-z0-9-)
- `title`: human-readable Name
- `mailbox_folder`: IMAP-Ordner, in den COPY erfolgen soll

Starke Routing-Signale (empfohlen):
- `domains`: Domains, die häufig vorkommen (From/Reply-To/Links)
- `contacts`: bekannte Ansprechpartner:innen (From/To/Cc)
- `aliases`: alternative Schreibweisen / Abkürzungen
- `typical_subject_patterns`: typische Betreffmuster (z. B. Prefixes)

RAG/Erklärbarkeit (empfohlen):
- `description`: 1–2 Sätze Kontext für LLM-Extraktion
- `keywords`: thematische Begriffe (vorsichtig dosieren, sonst False Positives)

Governance/Steuerung (optional, aber nützlich):
- `routing_priority`: bei Konflikten (höher = bevorzugt)
- `do_not_route_if`: Negativsignale (Substrings), die Routing verhindern sollen
- `updated_at`: wann zuletzt gepflegt
- `schema_version`: ermöglicht spätere Migration

---

## Projekt-Referenzen als Markdown (optional, empfohlen)

Zusätzlich zu `projects.json` soll pro Projekt ein **Ordner** mit mehreren Markdown-Dateien existieren.
Diese Dateien sind für konsolidierte Inhalte gedacht: Überblick, Signale, Evidenz, Sub-Themen.

**Ordner-Konvention:**
- `memory/references/projects/<id>/index.md`
- `memory/references/projects/<id>/signals.md`
- `memory/references/projects/<id>/evidence/YYYY-MM.md`
- `memory/references/projects/<id>/topics/*.md` (optional)

Beispiel:
- `memory/references/projects/usage-ng/index.md`

Regeln:
- `<id>` muss exakt der `id` in `projects.json` entsprechen
- `<id>` ist ein String-Slug (z. B. `usage-ng`), kein Integer
- Die Dateien verwenden einen Hybrid-Ansatz:
  - **Managed Sections** für automatisch konsolidierte Inhalte
  - **Freie Notizen** für manuelle Ergänzungen

Empfohlenes Feld in `projects.json`, um die Einstiegsdatei maschinenlesbar zu finden:
- `reference_md`: z. B. `"memory/references/projects/usage-ng/index.md"`

Konvention für Managed Sections:
- `<!-- BEGIN:managed-summary --> ... <!-- END:managed-summary -->` (in `index.md`)
- `<!-- BEGIN:managed-signals --> ... <!-- END:managed-signals -->` (in `signals.md`)
- `<!-- BEGIN:managed-evidence --> ... <!-- END:managed-evidence -->` (in `evidence/YYYY-MM.md`)

Nur Inhalte innerhalb dieser Marker dürfen von Skripten überschrieben/aktualisiert werden.

> Der Router darf auch ohne diese MDs funktionieren; sie verbessern aber die Klassifizierung (RAG-Kontext).

---

## Ordner-Konventionen (Mailbox)

`mailbox_folder` sollte **existieren** (oder der Setup-Step legt ihn an).

Konvention (Beispiel):
- `Projekte/<Projektname>`
- Reply-Sammelordner: `Projekte/_Needs-Reply`

Wichtig:
- `mail-processor` macht **COPY-only** (kein Move/Delete)
- Unklare Mails bleiben in der Inbox (kein `_Unclassified`)

---

## Reviewter Update-Flow aus Mails (empfohlen)

1) Discovery ausführen (`npm run discover-projects`) → erzeugt JSON in `memory/references/projects/inbox/`.
2) Vorschlag prüfen (keine Blindübernahme).
3) Übernehmen mit `npm run apply:suggestions -- --input=<inbox-datei.json>`.
4) Änderungen landen in `projects.json`; `changelog.md` wird ergänzt; fehlende Projektordner (`<id>/index.md`, `signals.md`, `evidence/`, `topics/`) werden erstellt.

## Wie ein Agent das anlegt (Setup-Checklist)

1) Ordner anlegen:
   - `memory/references/projects/`
2) `projects.json` erstellen:
   - starte mit 3–10 wichtigsten Projekten
3) Pro Projekt Mail-Ordner definieren (`mailbox_folder`)
4) Optional: `domains` und `contacts` ergänzen (beste Signale)
5) Repo committen (oder im Agent-Workspace versionieren), damit Änderungen nachvollziehbar sind

---

## Qualität: Wie du Trefferquote schnell steigerst

- **Domains/Kontakte zuerst** → liefert hohe Präzision.
- `keywords` sparsam verwenden (eher spezifisch als generisch).
- Bei Ambiguität lieber:
  - `aliases`/`subject_patterns` präzisieren
  - `do_not_route_if` ergänzen
  - Projekte hierarchisieren (später: `parent_id`)

---

## Hinweise zu Privacy/Retention

Wenn der Router Debug-Artefakte speichert (`data/mail-routing/msgs/*.json`), können dort sensible Inhalte landen.
Empfehlung:
- Retention (z. B. 30 Tage)
- optional Redaction (nur Header + extrahierte Features speichern)

---

## TODO (geplant)

- Schema v2 (Parent/Subprojects, Ambiguity-Gruppen)
- Validierungs-Skript (JSON Schema) + Linting
- Generator: `mail-processor init-project-catalog` (legt Struktur + Beispiel an)
