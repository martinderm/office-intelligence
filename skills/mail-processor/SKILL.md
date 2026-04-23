# Skill: mail-processor

Bindet die Mail-Verarbeitungskomponente **mail-processor** als OpenClaw-Skill in einen Agenten ein.
Im größeren Bild ist sie Teil von **office-intelligence**, bleibt technisch aber klar auf Mail-Triage, Extraktion/Klassifizierung und sicheres Routing (COPY-only) via Himalaya/IMAP fokussiert.

## Was der Skill bereitstellt

- **Einheitlicher Entry Point** über `HIMALAYA_COMMAND` (direktes Himalaya-Binary oder Agent-Gate)
- **Konventionen** für Ordner/Dateien im Agent-Workspace (`/memory`, `/data`)
- **Shadow-Mode** als Standard (erst klassifizieren/loggen; kein COPY)
- **Automatischer Mailbox-Folder-Sync** mit TTL-Prüfung vor normalen Mail-Runs; Force-Refresh nur bei Bedarf
- **Pending-Decisions-Queue** für fehlende referenzierte Ordner, damit Entscheidungen nicht verloren gehen
- **Guardrails**: Locking/Idempotenz, COPY-only, Fail-safe Defaults

## Voraussetzungen im Agent

1) Himalaya ist konfiguriert (Account im Agent vorhanden, via Gate oder direktem Account-Setup).
2) OpenClaw-Gateway-Zugang für `mail-classify` ist verfügbar (`OPENCLAW_BASE_URL`, `OPENCLAW_GATEWAY_TOKEN`, optional `OPENCLAW_SESSION_KEY`).
3) Der Agent darf das Tool `mail-classify` verwenden.
4) Der Agent-Workspace enthält die Memory-Struktur:

- `memory/references/projects/projects.json`
- optional: `memory/references/projects/<id>/index.md` (+ `signals.md`, `evidence/`, `topics/`)
- `memory/references/projects/README.md` (Doku/Schema)

## Empfohlene Workspace-Struktur

```
<agent-workspace>/
  memory/
    references/
      projects/
        projects.json
        README.md
        <id>/             (optional, pro Projekt)
          index.md
          signals.md
          evidence/
          topics/
  data/
    mail-processor/
      state.jsonl
      msgs/
      exports/
      capabilities/
      mailbox-folders.json
      pending-decisions.json
      memory_suggestions.jsonl
      router.lock
```

## Konfiguration (Env)

Minimal (aktueller Stand):
- `HIMALAYA_COMMAND=<command-or-path>`
- optional `HIMALAYA_ACCOUNT=<account-name>`
- `MAIL_SOURCE_FOLDER=INBOX`
- `MAILBOX_KEY=<stable-mailbox-key>` (für kurze/stabile Capability-Cache-Dateinamen)
- `MAIL_FETCH_LIMIT=20`
- `MAILBOX_FOLDERS_MAX_AGE_HOURS=12`
- `PROJECTS_JSON_PATH=./memory/references/projects/projects.json`
- `TOPICS_JSON_PATH=./memory/references/topics/topics.json`
- `PROJECT_MATCH_THRESHOLD=0.65`
- `NEEDS_REPLY_THRESHOLD=0.70`
- `NEEDS_REPLY_NEGATIVE_HINTS=no-reply,newsletter,autoreply`

Sicherer Start:
- `MAIL_ROUTING_ENABLED=false`  (Shadow-Mode)

Runner-Konvention:
- Agent-spezifische Konfiguration liegt in `<agent-workspace>/.env`.
- `skills/mail-processor/scripts/run-shadow.mjs`, `run-run.mjs` und `run-discover-projects.mjs` laden nur diese `.env`.
- In den Runnern sind keine mailbox-/proxy-/pfadbezogenen Hardcodes erlaubt.
- Runner toggeln nur den Modus (`MAIL_ROUTING_ENABLED`) und optional `MAIL_FETCH_LIMIT`.

Hinweis:
- Der operative Klassifikationspfad läuft über das OpenClaw-Tool `mail-classify`.
- `LLM_*` Variablen bleiben nur für getrennte Discovery-/Suggestion-Pfade relevant und sind für den normalen Shadow-/Routing-Pfad nicht die primäre Abhängigkeit.

Siehe vollständige Liste: `/.env.example` im Repo.

## Kommandos / Aktionen

### 1) Shadow Run
- `npm run shadow`

### 2) Routing Run (COPY-only, gated)
- `npm run run`

### 3) Mailbox-Folder-Sync / Pending Decisions
- Normale Mail-Runs prüfen automatisch vorab, ob `data/mail-processor/mailbox-folders.json` fehlt oder älter als `MAILBOX_FOLDERS_MAX_AGE_HOURS` ist. Nur dann wird live neu geholt.
- Force-Refresh: `npm run sync:mailbox-folders`
- Source of truth für Zielordner bleibt in `projects.json` und `topics.json`; `mailbox-folders.json` ist nur beobachteter Snapshot.
- Fehlende referenzierte Ordner landen in `data/mail-processor/pending-decisions.json`.
- Wenn der Skill in einer aktiven Chat-Session läuft und offene `pending-decisions` existieren, sollen diese **direkt kurz abgefragt** werden, statt still liegenzubleiben.
- Der CLI-Output eines normalen Runs enthält dafür `pendingDecisions.count`, `pendingDecisions.prompts` und `pendingDecisions.path`; diese Felder sind im Live-Chat aktiv aufzugreifen statt nur zu loggen.
- Wenn keine aktive Session vorhanden ist, bleiben Entscheidungen in `pending-decisions.json`, bis sie im nächsten aktiven Kontext aufgegriffen werden.

### 4) Wissenspflege aus Mail-Artefakten (reviewed)
- Discovery (Default: lokale `exports/**/*.eml`): `node skills/mail-processor/scripts/run-discover-projects.mjs --discover-last=200`
- Optional IMAP-Quelle: `node skills/mail-processor/scripts/run-discover-projects.mjs --discover-source=imap --discover-last=200`
- Review-Queue: `memory/references/projects/inbox/*.json`
- Apply: `npm run apply:suggestions -- --input=<datei.json>`
- Wirkung: aktualisiert `projects.json`, pflegt `changelog.md`, erstellt fehlende Projektordner (`<id>/index.md`, `signals.md`, `evidence/`, `topics/`)

### 4) Konsolidierung in Wissens-/Projektordner (Agent-basiert)
- Die Konsolidierung wird **vom OpenClaw-Agenten** durchgeführt, nicht durch ein lokales Merge-Skript.
- Input: verarbeitete Mail-Artefakte unter `data/mail-processor/msgs/**/*.json`.
- Ziel: Managed-Sections in `index.md`/`signals.md` aktualisieren und Evidenz in `evidence/YYYY-MM.md` ergänzen.
- Regel: nur bei klarer Zuordnung/hoher Confidence; bei Ambiguität in Review-Queue statt Direkt-Write.
- Erweiterung: bei kritisch fehlenden Infos stellt der Agent kurze, gezielte Rückfragen (statt Annahmen zu treffen).
- User-Trigger möglich: aktive Vervollständigung von Projekt-Metadaten (inkl. Workpackages).
- Referenz-Task: `skills/mail-processor/PROJECT_MEMORY_AGENT_TASK.md`

## Safety / Guardrails (müssen im Skill enforcebar sein)

- COPY-only (nie MOVE/DELETE)
- Safe default: bei Ambiguität **keine Aktion**
- Hard negative rules für needsReply (Newsletter/Auto-Reply/no-reply)
- JSON-Schema-Validation für Tool-/LLM-Extrakt; bei Fehlern skip+log
- Retention für `data/mail-processor/msgs/**/*.json` (z.B. 30 Tage)
- Guard gegen Doppelverarbeitung: wenn vollständiges `msgs/**/<fileId>.json` existiert (inkl. LLM-Feld, falls aktiv), dann skip (Legacy `<stableId>.json` wird weiterhin erkannt)

## Output / Artefakte

- `data/mail-processor/state.jsonl` — Idempotenz-Log
- `data/mail-processor/msgs/<folder-slug>/<fileId>.json` — Extrakte/Debug inkl. `history[]`
- `data/mail-processor/exports/<folder-slug>/<fileId>.eml` — lokale EML-Ablage
- `fileId` wird im Msg-Artefakt unter `local.fileId` mitgeführt
- `fileId` wird deterministisch aus `stableId` abgeleitet: `sha256(stableId)` → `base64url` → auf 16 Zeichen gekürzt (kompakter Dateiname, minimales Kollisionsrisiko)
- `data/mail-processor/mailbox-folders.json` — beobachteter Mailbox-Ordnerbaum (Cache/Snapshot)
- `data/mail-processor/pending-decisions.json` — offene/gelöste Entscheidungen zu fehlenden referenzierten Ordnern
- `data/mail-processor/memory_suggestions.jsonl` — Vorschläge zur Katalogpflege
- `data/mail-processor/capabilities/<MAILBOX_KEY>.json` — Capabilities + Policy-Cache

## Betriebsgrenze (aktuell)

- Ein Run arbeitet gegen genau **eine** Mailbox-Instanz (`HIMALAYA_COMMAND` + `MAILBOX_KEY`).
- Mehrere Mailboxen erfordern getrennte Instanzen/Runs mit jeweils eigenem Data-Dir.

