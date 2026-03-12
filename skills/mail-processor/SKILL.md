# Skill: mail-processor

Bindet das Projekt **mail-processor** als OpenClaw-Skill in einen Agenten ein.
Ziel: Mail-Triage (Extraktion/Klassifizierung) + sicheres Routing (COPY-only) via Himalaya/IMAP.

## Was der Skill bereitstellt

- **Einheitlicher Entry Point** über `HIMALAYA_COMMAND` (direktes Himalaya-Binary oder Agent-Gate)
- **Konventionen** für Ordner/Dateien im Agent-Workspace (`/memory`, `/data`)
- **Shadow-Mode** als Standard (erst klassifizieren/loggen; kein COPY)
- **Guardrails**: Locking/Idempotenz, COPY-only, Fail-safe Defaults

## Voraussetzungen im Agent

1) Himalaya ist konfiguriert (Account im Agent vorhanden, via Gate oder direktem Account-Setup).
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
    mail-processor/
      state.jsonl
      msgs/
      exports/
      capabilities/
      memory_suggestions.jsonl
      router.lock
```

## Konfiguration (Env)

Minimal (aktueller Stand):
- `HIMALAYA_COMMAND=<command-or-path>`
- `MAIL_SOURCE_FOLDER=INBOX`
- `MAILBOX_KEY=<stable-mailbox-key>` (für kurze/stabile Capability-Cache-Dateinamen)
- `MAIL_FETCH_LIMIT=20`
- `PROJECTS_JSON_PATH=./memory/references/projects/projects.json`
- `PROJECT_MATCH_THRESHOLD=0.65`
- `NEEDS_REPLY_THRESHOLD=0.70`
- `NEEDS_REPLY_NEGATIVE_HINTS=no-reply,newsletter,autoreply`

Sicherer Start:
- `MAIL_ROUTING_ENABLED=false`  (Shadow-Mode)

Hinweis:
- `LLM_*` Variablen sind im aktuellen Codepfad aktiv (Extraktion via LLM), bleiben aber optional konfigurierbar.

Siehe vollständige Liste: `/.env.example` im Repo.

## Kommandos / Aktionen

### 1) Shadow Run
- `npm run shadow`

### 2) Routing Run (COPY-only, gated)
- `npm run run`

### 3) Memory-Update aus Mails (reviewed)
- Discovery: `npm run discover-projects`
- Review-Queue: `memory/references/projects/inbox/*.json`
- Apply: `npm run apply:suggestions -- --input=<datei.json>`
- Wirkung: aktualisiert `projects.json`, pflegt `changelog.md`, erstellt fehlende `<id>.md`

## Safety / Guardrails (müssen im Skill enforcebar sein)

- COPY-only (nie MOVE/DELETE)
- Safe default: bei Ambiguität **keine Aktion**
- Hard negative rules für needsReply (Newsletter/Auto-Reply/no-reply)
- JSON-Schema-Validation für LLM-Extrakt; bei Fehlern skip+log
- Retention für `data/mail-processor/msgs/**/*.json` (z.B. 30 Tage)
- Guard gegen Doppelverarbeitung: wenn vollständiges `msgs/**/<stableId>.json` existiert (inkl. LLM-Feld, falls aktiv), dann skip

## Output / Artefakte

- `data/mail-processor/state.jsonl` — Idempotenz-Log
- `data/mail-processor/msgs/<folder-slug>/<stableId>.json` — Extrakte/Debug inkl. `history[]`
- `data/mail-processor/exports/<folder-slug>/<stableId>.eml` — lokale EML-Ablage
- `data/mail-processor/memory_suggestions.jsonl` — Vorschläge zur Katalogpflege
- `data/mail-processor/capabilities/<MAILBOX_KEY>.json` — Capabilities + Policy-Cache

## Betriebsgrenze (aktuell)

- Ein Run arbeitet gegen genau **eine** Mailbox-Instanz (`HIMALAYA_COMMAND` + `MAILBOX_KEY`).
- Mehrere Mailboxen erfordern getrennte Instanzen/Runs mit jeweils eigenem Data-Dir.

