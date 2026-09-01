# project-catalog-entry — Aufgaben, Backlog & Schema-Erweiterungsplan

Zentrales Backlog und Planungspapier für geplante Erweiterungen am Projektkatalog-Schema (`projects.json`) und den zugehörigen Vorlagen/Skills.

> [!NOTE]
> **Status:** Planungsstand / Backlog (**unter Vorbehalt — noch keine Umsetzung erfolgt**).

---

## 1. Geplante Schema-Erweiterung: Strukturierte Workpackages

### 1.1 Problemstellung & Ziel
Bisher wurden Workpackages, Tasks (z. B. `T1.7`), Deliverables (z. B. `D1.2`) und Milestones (z. B. `MS1`) in `projects.json` primär als flache String-Listen in `"aliases"` oder `"keywords"` geführt.
Ziel ist ein formal erweitertes, maschinenlesbares Schema für Workpackages in `projects.json`, das hierarchisch Tasks, Deliverables und Milestones abbildet, während die bestehenden Routing- und Matching-Eigenschaften (`aliases`, `keywords`, `contacts`, `status`) für `mail-processor` und andere Konsumenten voll abwärtskompatibel erhalten bleiben.

### 1.2 Zielschema (Workpackage-Ebene)

```json
"workpackages": [
  {
    "id": "wp1",
    "number": 1,
    "title": "Project Management, Coordination, Quality and Evaluation",
    "lead": "EUCEN",
    "boku_role": "Co-Lead Quality",
    "status": "active",
    "tasks": [
      {
        "id": "T1.7",
        "title": "Drafting the Quality and Evaluation Plan",
        "lead": "BOKU",
        "keywords": ["Quality Plan", "QM Plan", "Handbook", "Evaluation Plan"]
      }
    ],
    "deliverables": [
      {
        "id": "D1.2",
        "title": "Quality and evaluation plan, including tools IPR and reports",
        "lead": "BOKU",
        "type": "R — Document, report",
        "due_month": "Ongoing, M1–M36"
      }
    ],
    "milestones": [
      {
        "id": "MS1",
        "title": "Kick-off meeting held",
        "lead": "EUCEN",
        "due_month": "M2"
      }
    ],
    "aliases": ["WP1", "T1.1", "T1.7", "D1.1", "D1.2", "MS1"],
    "keywords": ["project management", "quality", "evaluation", "governance"],
    "contacts": [
      { "email": "carme.royo@eucen.eu" }
    ]
  }
]
```

### 1.3 Felddefinitionen

| Feld | Typ | Pflicht | Beschreibung |
|---|---|---|---|
| `id` | `string` | ja | Eindeutiger Slug innerhalb des Projekts (z. B. `wp1`, `ac3-swot`) |
| `number` | `integer` | nein | WP-Nummer (z. B. `1`), falls anwendbar |
| `title` | `string` | ja | Vollständiger Name des Workpackages |
| `lead` | `string` | nein | Federführende Institution (z. B. `EUCEN`, `MFHEA`) |
| `boku_role` | `string` | nein | Spezifische Rolle der BOKU (z. B. `Co-Lead Quality`, `Contributor`) |
| `status` | `string` | ja | `active` \| `completed` \| `planned` \| `paused` |
| `tasks` | `array[object]` | nein | Strukturierte Liste von Tasks/Arbeitspaketen |
| `tasks[].id` | `string` | ja | Task-ID (z. B. `T1.7`, `Task 1.1`) |
| `tasks[].title` | `string` | ja | Titel der Task |
| `tasks[].lead` | `string` | nein | Verantwortliche Institution |
| `tasks[].keywords` | `array[string]`| nein | Relevante Fachbegriffe / Signale |
| `deliverables` | `array[object]` | nein | Strukturierte Deliverables |
| `deliverables[].id` | `string` | ja | Deliverable-ID (z. B. `D1.2`) |
| `deliverables[].title`| `string` | ja | Titel des Deliverables |
| `deliverables[].lead` | `string` | nein | Lead Beneficiary |
| `deliverables[].type` | `string` | nein | Typ (z. B. `R — Document, report`, `DEC`, `DEM`) |
| `deliverables[].due_month`| `string` | nein | Fälligkeit (z. B. `M18`, `Ongoing, M1–M36`) |
| `milestones` | `array[object]` | nein | Strukturierte Meilensteine |
| `milestones[].id` | `string` | ja | Meilenstein-ID (z. B. `MS1`) |
| `milestones[].title` | `string` | ja | Titel des Meilensteins |
| `milestones[].lead` | `string` | nein | Verantwortlicher Partner |
| `milestones[].due_month` | `string` | nein | Fälligkeitsmonat (z. B. `M2`, `M18`) |
| `aliases` | `array[string]` | nein | Abwärtskompatible Routing-Kürzel |
| `keywords` | `array[string]` | nein | Thematische Schlagwörter für Mail-Desk & Suche |
| `contacts` | `array[object]` | nein | WP-spezifische E-Mail-Kontakte |

---

## 2. Geplante Dokumenten- & Template-Anpassungen

### 2.1 `skills/project-catalog-entry/SKILL.md`
- [ ] Schema-Definition unter `## Zielschema (pro Projekt)` um strukturierte Workpackages erweitern.
- [ ] Questionnaire-Mode und Template-Mode anpassen, um Tasks, Deliverables und Milestones gezielt abzufragen bzw. zu parsen.
- [ ] Querverweis auf dieses Backlog (`TODO.md`) verankern.

### 2.2 `references/project-template.md`
- [ ] `workpackages`-Abschnitt mit vollständigem Beispiel (Tasks, Deliverables, Milestones) aktualisieren.

### 2.3 `references/project-folder-template.md`
- [ ] Vorlage `workpackages/<wp-id>-<slug>.md` um strukturierte Abschnitte für Scope (Lead, BOKU-Rolle, Laufzeit), Tasks, Deliverables und Milestones ergänzen.

---

## 3. Geplantes Migrationsskript (`scripts/migrate_project_wps.py`)

### 3.1 Architektur & Konformität
- **Ablage:** `skills/project-catalog-entry/scripts/migrate_project_wps.py`
- **Standard:** Python 3 Standard Library only (keine externen pip-Dependencies).
- **Dateisystem & I/O:** `pathlib.Path`, Cross-Platform-Pfade, atomare Schreibweise via `tempfile` und `os.replace`.
- **Encoding:** UTF-8 mit Erhaltung nativer deutscher Umlaute (`ensure_ascii=False`).
- **CLI:** Standard-Envelope mit `--json`, `--dry-run` und optionalem Pfadargument `--catalog`.

### 3.2 Parsing- & Extraktionsmatrix je Projekt

| Projekt | Quelle | Extraktionslogik |
|---|---|---|
| **MESHE** | `memory/references/projects/meshe/workpackages/wp*.md` | WP1–WP5 vollständig parsen: Tasks (`T1.1`–`T5.6`), Deliverables (`D1.1`–`D5.3`), Milestones (`MS1`–`MS14`), Leads (`EUCEN`, `UCC`, `JGU`, `ESU`), BOKU-Rolle (`Co-Lead Quality` in WP1). |
| **EVOLVE** | `memory/references/projects/evolve/workpackages/wp*.md` | WP1–WP5 parsen: Tasks (`Task 1.1`–`5.3`), Deliverables (`D1.1`–`D5.2`), Checkpoints/Milestones, Leads (`MFHEA`, `UoA`, `UM`, `ACS`, `HHUAS`), BOKU-Beitrag. |
| **WEEK** | `memory/references/projects/week/index.md` & `workpackages/*.md` | Activity Clusters `ac1` bis `ac6` als strukturierte Einheiten beibehalten; `ac3-swot` aus Detaildatei anreichern. |
| **LI4LAM** | `memory/references/projects/li4lam/workpackages/README.md` | WP1 bis WP9 aus README/Katalog mit Nummerierung `1..9` strukturieren, bestehende Keywords/Aliases beibehalten. |
| **ATAEL** | `memory/references/projects/atael/workpackages/README.md` | WP1 mit Nummer `1` strukturieren, Status `active`/`proposal` beibehalten. |
| **USAGE-NG** | `memory/references/projects/usage-ng/index.md` | Beibehaltung von `workpackages: []` (abgeschlossenes Projekt ohne aktive WPs). |
| **RELLDE** | `memory/references/projects/rellde/index.md` | Beibehaltung von `workpackages: []` (Antrag in Ausarbeitung). |

---

## 4. Offene Punkte & Vorbehalte

1. **Abwärtskompatibilität für Routing:**
   - Sicherstellen, dass alle bestehenden Root-Felder (`domains`, `contacts`, `aliases`, `typical_subject_patterns`, `cloud_sync`, etc.) und WP-Aliase unberührt bleiben, damit bestehende Mail-Desk-Klassifizierungen 1:1 weiterfunktionieren.
2. **Harmonisierung von Activity Clusters (WEEK):**
   - In WEEK existieren formal keine WPs, sondern Activity Clusters (`ac1`–`ac6`). Klären, ob das Feld `number` hier optional entfällt oder als `1..6` abgebildet wird.
3. **Validierung vor Ausführung:**
   - Vor jedem produktiven Schreibvorgang auf `projects.json` muss ein `--dry-run` Diff ausgegeben und geprüft werden.
