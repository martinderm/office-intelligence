# Init-Checklist (project-catalog-entry)

Einmalige Workspace-Initialisierung (nicht Teil des regulären Projekt-Arbeitsmodus).

Ziel: Mindeststruktur für Projektkatalog + kompakte Workspace-Übersicht sicherstellen.

## Scope

- Workspace-Root
- `memory/references/projects/`
- Skill-Referenzen unter `skills/project-catalog-entry/references/`

## Reihenfolge (verbindlich)

1. **Prüfen: `PROJECTS.md` im Workspace-Root vorhanden**
   - Sollpfad: `<workspace>/PROJECTS.md`
   - Wenn fehlt: aus `references/projects-overview-template.md` initial anlegen.

2. **Prüfen: `AGENTS.md` referenziert `PROJECTS.md` in der Workspace-File-Map**
   - Soll: Eintrag zu `PROJECTS.md` mit Kurzbeschreibung.
   - Wenn fehlt: minimal patchen (nur fehlende Zeile ergänzen).

3. **Prüfen: Struktur für Projektwissen vorhanden**
   - `memory/references/projects/projects.json`
   - `memory/references/projects/<slug>/index.md` (bei Neuanlage)
   - `memory/references/projects/<slug>/contacts.md` (bei Neuanlage)
   - `memory/references/projects/<slug>/signals.md` (bei Neuanlage)
   - `memory/references/projects/<slug>/workpackages/` (bei Neuanlage)

4. **Review vor Write**
   - Geplante Dateiänderungen inkl. Initialisierungs-Ergebnis als Kurzblock anzeigen:
     - `OK` (bereits vorhanden)
     - `ANLEGEN` (fehlt, wird erstellt)
     - `PATCH` (minimaler Ergänzungspatch)

5. **Write nur nach expliziter Freigabe**
   - Keine stillen Strukturänderungen ohne Bestätigung.

## Ergebnisformat (für Review)

- `PROJECTS.md`: `OK | ANLEGEN`
- `AGENTS.md -> PROJECTS.md-Referenz`: `OK | PATCH`
- `Projektstruktur <slug>`: `OK | ANLEGEN`
