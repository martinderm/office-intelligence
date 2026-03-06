# Skill: mail-processor

Bindet das Projekt **mail-processor** als OpenClaw-Skill in einen Agenten ein.
Ziel: Mail-Triage (Extraktion/Klassifizierung) + sicheres Routing (COPY-only) via Himalaya/IMAP.

## Was der Skill bereitstellt

- **Einheitlicher Entry Point** (Wrapper), der `mail-processor` aus dem Agent-Workspace startet
- **Konventionen** für Ordner/Dateien im Agent-Workspace (`/memory`, `/data`)
- **Shadow-Mode** als Standard (erst klassifizieren/loggen; kein COPY)
- **Guardrails**: Locking/Idempotenz, COPY-only, Fail-safe Defaults

## Voraussetzungen im Agent

1) Himalaya ist konfiguriert (Account im Agent vorhanden, via Wrapper/Gate).
2) LLM-Endpoint ist verfügbar (OpenAI-kompatibel).
3) Der Agent-Workspace enthält die Memory-Struktur:

- `memory/references/projects/projects.json`
- optional: `memory/references/projects/<id>.md`
- `memory/references/projects/README.md` (Doku/Schema)

## Empfohlene Workspace-Struktur

```
<agent-workspace>/
  memory/
    references/
      projects/
        projects.json
        README.md
        <id>.md           (optional, pro Projekt)
  data/
    mail-routing/
      state.jsonl
      msgs/
      memory_suggestions.jsonl
      router.lock
```

## Konfiguration (Env)

Minimal:
- `HIMALAYA_ACCOUNT=<account>`
- `LLM_BASE_URL=<url>`
- `LLM_API_KEY=<key>`
- `LLM_MODEL=<model>`
- `PROJECTS_JSON_PATH=./memory/references/projects/projects.json`

Sicherer Start:
- `MAIL_ROUTING_ENABLED=false`  (Shadow-Mode)

Siehe vollständige Liste: `/.env.example` im Repo.

## Kommandos / Aktionen

### 1) Shadow Run (empfohlen zum Start)
- liest Mails, extrahiert Signale, trifft Routing-Entscheidungen
- schreibt Logs/Debug-Artefakte
- führt **keine COPY-Aktionen** aus

Command (Wrapper):
- `scripts/run-mail-processor.ps1 -Mode shadow`

### 2) Routing Run (COPY-only, gated)
- führt COPY in Projektordner nur bei hoher Sicherheit aus
- optional zusätzlich COPY nach `_Needs-Reply`

Command:
- `scripts/run-mail-processor.ps1 -Mode run`

## Wrapper-Skript (Skizze)

Der Skill sollte ein Wrapper-Skript bereitstellen, das:

- `.env` lädt (oder env passthrough)
- Lockfile/Single-Runner erzwingt
- Node/CLI aufruft
- Exit-Codes sauber nach OpenClaw zurückgibt

Beispiel (Pseudo):

- `scripts/run-mail-processor.ps1`
  - Parameter: `-Mode shadow|run`
  - setzt `MAIL_ROUTING_ENABLED` abhängig von Mode
  - ruft `node dist/router.js` (oder `npm run cli`) auf

## Setup-Automation (geplant)

Optionaler Initializer:

- `mail-processor init-project-catalog`
  - legt `memory/references/projects/` an
  - kopiert `README.md`
  - erstellt `projects.json` mit Beispielen
  - erstellt `_TEMPLATE-project.md`

## Safety / Guardrails (müssen im Skill enforcebar sein)

- COPY-only (nie MOVE/DELETE)
- Safe default: bei Ambiguität **keine Aktion**
- Hard negative rules für needsReply (Newsletter/Auto-Reply/no-reply)
- JSON-Schema-Validation für LLM-Extrakt; bei Fehlern skip+log
- Retention für `data/mail-routing/msgs/*.json` (z.B. 30 Tage)

## Output / Artefakte

- `data/mail-routing/state.jsonl` — Idempotenz-Log
- `data/mail-routing/msgs/*.json` — Extrakte/Debug (optional)
- `data/mail-routing/memory_suggestions.jsonl` — Vorschläge zur Katalogpflege

## Open Questions

- Soll der Skill direkt Himalaya nutzen oder über ein Agent-spezifisches Gate/Wrapper laufen?
- Wo sollen Secrets liegen: `.env` vs OpenClaw secret-store/env injection?
- Welche Scheduler-Integration: Cron/Heartbeat vs manueller Run?
