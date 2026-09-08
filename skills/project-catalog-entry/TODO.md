# project-catalog-entry — Aufgaben, Backlog & Schema-Erweiterungsplan

Zentrales Backlog für `projects.json` und die zugehörigen Vorlagen/Skills.

## FR-01a — Schema-v3-Vertrag

**Status: abgeschlossen.**

- [x] Formales v3-Schema mit kompatibler Listen- und Objekt-Rootform.
- [x] Projektweite Milestones, strikte WP/Task/Deliverable/Milestone-Objekte und kompatible Routing-/Cloud-Felder.
- [x] Read-only Validator mit JSON-Envelope, präzisen Pfaden und Fixture-Tests.
- [x] Questionnaire, Vorlagen und generischer Beispielkatalog auf v3 aktualisiert.

## FR-01b1 — Migrationswerkzeug

**Status: abgeschlossen.** FR-01a/FR-01b1 migrieren keine realen Projektkataloge.

### Architektur & Konformität

- **Ablage:** `skills/project-catalog-entry/scripts/migrate_project_wps.py`
- **Standard:** Python 3 Standard Library only (keine externen pip-Dependencies).
- **Dateisystem & I/O:** `pathlib.Path`, Cross-Platform-Pfade, atomare Schreibweise via `tempfile` und `os.replace`.
- **Encoding:** UTF-8 mit Erhaltung nativer deutscher Umlaute (`ensure_ascii=False`).
- **CLI:** Standard-Envelope mit `--json`, standardmäßigem `--dry-run`, optionalem Pfadargument `--catalog`, `--projects-root`, wiederholbarem `--project` und explizitem `--apply`.
- [x] Deterministischer Dry-run-Diff, atomarer Apply, UTF-8 und nachgelagerte v3-Validierung.
- [x] Apply fail-closed über den kanonischen `workspace_lock_guard.py` mit Lease-/Conversation-Ownership.
- [x] Stabile `### Task … — …`-Überschriften und T/D/MS-Listen werden konservativ gelesen; unklare Quellen blockieren als `PendingReview`, Checkpoints ohne stabile MS-ID bleiben sichtbare Warnungen und Managed-/Freitext wird nicht normativ übernommen.

## FR-01b2 — Produktiver Dry-run und Backfill

**Status: offen.** Erst nach separatem Scope, Lock-Ownership und Human Review die nachfolgende Matrix gegen reale Quellen anwenden.

### Parsing- & Extraktionsmatrix je Projekt

| Projekt | Quelle | Extraktionslogik |
|---|---|---|
| **MESHE** | `memory/references/projects/meshe/workpackages/wp*.md` | WP1–WP5 vollständig parsen: Tasks (`T1.1`–`T5.6`), Deliverables (`D1.1`–`D5.3`), projektweite Milestones (`MS1`–`MS14`), Leads (`EUCEN`, `UCC`, `JGU`, `ESU`) und BOKU-Rolle (`Co-Lead Quality` in WP1). |
| **EVOLVE** | `memory/references/projects/evolve/workpackages/wp*.md` | WP1–WP5 parsen: Tasks (`Task 1.1`–`5.3`), Deliverables (`D1.1`–`D5.2`), Checkpoints/Milestones auf Projektebene, Leads (`MFHEA`, `UoA`, `UM`, `ACS`, `HHUAS`) und BOKU-Beitrag. |
| **WEEK** | `memory/references/projects/week/index.md` & `workpackages/*.md` | Activity Clusters `ac1` bis `ac6` als strukturierte Einheiten beibehalten; `ac3-swot` aus Detaildatei anreichern. |
| **LI4LAM** | `memory/references/projects/li4lam/workpackages/README.md` | WP1 bis WP9 mit Nummerierung `1..9` strukturieren, bestehende Keywords/Aliases beibehalten. |
| **ATAEL** | `memory/references/projects/atael/workpackages/README.md` | Overview-only: erst nach einer eindeutigen strukturierten Quelle migrieren; vorhandenen Status fachlich prüfen und auf erlaubte v3-Statuswerte abbilden. |
| **USAGE-NG** | `memory/references/projects/usage-ng/index.md` | `workpackages: []` und `milestones: []` für das abgeschlossene Projekt beibehalten. |
| **RELLDE** | `memory/references/projects/rellde/index.md` | `workpackages: []` und `milestones: []` für den Antrag in Ausarbeitung beibehalten. |

### Offene Punkte & Vorbehalte

1. **Abwärtskompatibilität für Routing:** Root-Felder (`domains`, `contacts`, `aliases`, `typical_subject_patterns`, `cloud_sync` usw.) und WP-Aliase unberührt halten, damit Mail-Desk-Klassifizierungen kompatibel bleiben.
2. **Activity Clusters (WEEK):** Klären, ob `number` dort entfällt oder `1..6` verwendet wird.
3. **Human Gate:** Produktive Quellen, Scope-Erweiterungen und jeder Backfill benötigen Lock-Ownership und explizite Freigabe.
