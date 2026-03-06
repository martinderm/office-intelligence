# mail-processor

Standalone Mail-Triage/Routing/Processing pipeline (concept-first).

## Agent-Workspace Struktur

`mail-processor` ist so gedacht, dass es in einem Agent-Workspace läuft. Der Agent soll dafür eine **Memory-Struktur unter `/memory`** anlegen (relativ zum Workspace-Root).

Wichtige Pfade:
- `memory/references/projects/projects.json` — Projektkatalog (Source of truth für Routing)
- `memory/references/projects/README.md` — Doku + Schema, wie der Katalog aufgebaut sein soll

Siehe: `memory/references/projects/README.md`

