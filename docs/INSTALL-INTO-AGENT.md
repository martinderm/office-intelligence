# Install office-intelligence into existing Agent (Operator Runbook)

Ziel: Ein Main-Agent installiert/aktualisiert die relevanten `office-intelligence`-Skills in einem bestehenden Agent-Workspace reproduzierbar und sicher. Für einen bestehenden Mail-Agenten umfasst der Standard-Rollout aktuell drei Skill-Verzeichnisse:

- `skills/mail-processor`
- `skills/project-catalog-entry`
- `skills/topic-catalog-entry`

Wichtig: Nur `mail-processor` enthält operative Runner für die Mail-Verarbeitung. Der eigentliche TypeScript-/Node-Projektcode (`package.json`, `src/`, zentrale `scripts/`) bleibt im Projekt-Repo `projects/office-intelligence`. In den Agent-Workspace werden die Skill-Verzeichnisse aus `projects/office-intelligence/skills/` kopiert; der `mail-processor`-Skill greift dann über `MAIL_PROCESSOR_PROJECT_DIR` auf das zentrale Projekt zu.

## 1) Voraussetzungen

- Mail-Agent-Workspace existiert (z. B. `D:/users/dagobert/.openclaw/agents/<agent-id>/workspace`)
- Himalaya-Zugriff ist für **genau eine Mailbox** verfügbar (direkt oder via Gate)
- Projekt ist lokal verfügbar unter:
  - `C:/Users/dagobert-ai/.openclaw/workspace/projects/office-intelligence`

## 2) Skill-Dateien im Ziel-Agent bereitstellen

Standard-Rollout in den Ziel-Agenten:

- `skills/mail-processor/`
- `skills/project-catalog-entry/`
- `skills/topic-catalog-entry/`

Diese drei Verzeichnisse werden als komplette Skill-Ordner in den Agent-Workspace kopiert. Andere bereits vorhandene Skills des Ziel-Agenten bleiben unberührt.

Hinweis zu mailbox-gebundenem Himalaya-Aufruf:
- Empfohlen ist ein Gate (fixe Account-/Command-Policy).
- Alternativ kann ein `.mjs`-Wrapper genutzt werden.
- Für allgemeine Wrapper-Erzeugung liegt ein Beispiel im Projekt: `scripts/create-himalaya-account-proxy.mjs`.


Für `mail-processor` liegen im Ziel-Agent-Workspace unter `skills/mail-processor/` typischerweise:

- `SKILL.md`
- `PROJECT_MEMORY_AGENT_TASK.md`
- `scripts/run-shadow.mjs`
- `scripts/run-run.mjs`
- `scripts/run-discover-projects.mjs`

Run-Skript-Konvention (verbindlich):

- Agent-spezifische Konfiguration liegt in `<agent-workspace>/.env`.
- Die Runner laden diese `.env` und reichen sie an `npm run build/shadow/run` weiter.
- Runner dürfen **keine** mailbox-/proxy-/pfadbezogenen Hardcodes enthalten.
- Runner setzen nur Modus-Toggles:
  - `MAIL_ROUTING_ENABLED=false` im Shadow-Skript
  - `MAIL_ROUTING_ENABLED=true` im Run-Skript
  - optional `MAIL_FETCH_LIMIT` via CLI-Flag

Wichtige Abgrenzung:

- Die Dateien unter `projects/office-intelligence/src/` werden bei diesem Rollout **nicht** in den Agent-Workspace kopiert.
- Die Dateien unter `projects/office-intelligence/scripts/` werden ebenfalls **nicht** pauschal in den Agent-Workspace kopiert.
- Im Agent-Workspace landen die Skill-Dateien unter `skills/...`; für `mail-processor` sind das vor allem `SKILL.md`, `PROJECT_MEMORY_AGENT_TASK.md` und die Runner unter `skills/mail-processor/scripts/`.
- Die Runner verwenden `MAIL_PROCESSOR_PROJECT_DIR`, um Build und Lauf gegen das zentrale Projekt-Repo auszuführen.

Empfohlene `.env`-Felder im Agent-Workspace:
- `MAIL_PROCESSOR_PROJECT_DIR=<pfad-zum-office-intelligence-projekt>`
- `HIMALAYA_COMMAND=<agent-spezifischer command/gate oder node-wrapper>`
- `MAILBOX_KEY=<kurzer stabiler key>`
- `MAIL_SOURCE_FOLDER=INBOX` (oder Instanzwert)
- `PROJECTS_JSON_PATH=<agent-workspace>/memory/references/projects/projects.json`
- `MAIL_PROCESSOR_DATA_DIR=<agent-workspace>/data/mail-processor`
- optional explizit: `PENDING_ACTIONS_FILE=<agent-workspace>/data/mail-processor/pending-actions.json`
- optional explizit: `ACTION_LOG_DIR=<agent-workspace>/data/mail-processor/logs/actions`
- bei BOKU/GroupWise: `MAIL_COPY_SEMANTICS=acts_like_move` setzen, weil Himalaya `message copy` dort de-facto wie ein Move wirkt
- `OPENCLAW_BASE_URL`, `OPENCLAW_GATEWAY_TOKEN`, optional `OPENCLAW_SESSION_KEY`, `LLM_TIMEOUT_MS` (Gateway-Zugang für `mail-classify`; mit Session-Key kann gezielt das Modell des Ziel-Agents genutzt werden; das Plugin selbst hält den eingebetteten Modellaufruf bewusst minimal und setzt keine harten Sampling-/Format-Parameter wie `temperature` oder `responseFormat`)
- `LLM_BASE_URL`, `LLM_API_KEY`, `LLM_MODEL` nur dann, wenn zusätzlich Discovery/Suggestion-Pfade genutzt werden

## 3) Wissenskataloge sicherstellen

Im Ziel-Agent-Workspace muss vorhanden sein:

- `memory/references/projects/projects.json`

Hinweis: Ohne gepflegte Kataloge bleibt Matching schwach/unzuverlässig.

## 4) Build & Smoke-Test

Im Projektverzeichnis (also im zentralen Repo, nicht im Agent-Skill-Ordner):

```bash
npm install
npm run build
npm run check
```

Dann im Ziel-Agent:

```bash
node skills/mail-processor/scripts/run-shadow.mjs --fetch-limit=1
```

Erwartung:

- `data/mail-processor/state.jsonl` existiert/wird ergänzt
- `data/mail-processor/capabilities/<MAILBOX_KEY>.json` existiert
- `data/mail-processor/pending-actions.json` enthält für klassifizierte Mails schlanke Postprocessing-Items
- ggf. `data/mail-processor/pending-decisions.json` enthält fehlende Parent- oder `_Needs-Reply`-Ordner
- Shadow-Run ohne Mailbox-Routing-Aktionen

## 5) Optional: Discovery + reviewed Apply

```bash
node skills/mail-processor/scripts/run-discover-projects.mjs --discover-last=200
```

- Ergebnis liegt in `memory/references/projects/inbox/*.json`
- Danach reviewed übernehmen:

```bash
npm run apply:suggestions -- --input=memory/references/projects/inbox/<datei>.json
```

- Wirkung: `projects.json` aktualisiert, `changelog.md` ergänzt, fehlende Projektordner (`<id>/index.md`, `signals.md`, `evidence/`, `topics/`) werden erzeugt

Danach Konsolidierung durch den Agenten ausführen lassen (OpenClaw-Task, nicht lokales Merge-Skript; siehe `skills/mail-processor/PROJECT_MEMORY_AGENT_TASK.md`).

- Wirkung: Managed-Bereiche in `index.md`/`signals.md` werden aus klar zugeordneten Mails aktualisiert; Evidenz landet monatlich in `evidence/YYYY-MM.md`.

### Optional: Skill-Sync validieren

Nach dem Kopieren der Skill-Dateien kann der Stand gegen das Repo geprüft werden:

```bash
npm run check:sync -- --pair skills/mail-processor <agent-workspace>/skills/mail-processor
```

Mit `--strict` werden auch zusätzliche Dateien im Ziel als Drift markiert.

Instanzspezifische Zielpfade bitte lokal in `docs/INSTALL_PATHS.local.md` dokumentieren (Vorlage: `docs/INSTALL_PATHS.example.md`).

## 6) Go-Live (erst nach Shadow-Validierung)

```bash
node skills/mail-processor/scripts/run-run.mjs
```

Empfehlung:

- zuerst kleine Batches (`FetchLimit` klein)
- State/Artefakte nach jedem Lauf prüfen
- Bei `MAIL_COPY_SEMANTICS=acts_like_move` ist Needs-Reply ein Single-Target-Move:
  - Project + `needs_reply=true` → `<project.mailbox_folder>/_Needs-Reply`
  - Topic ohne Project + `needs_reply=true` → `<topic.mailbox_folder>/_Needs-Reply`
  - kein Project/Topic + `needs_reply=true` → `Inbox/_Needs-Reply`
- Fehlende `_Needs-Reply`-Unterordner vor Go-Live über `pending-decisions.json` klären.

## 7) Betriebsgrenzen (aktuell)

- Ein Run arbeitet gegen genau **eine** Mailbox (`HIMALAYA_COMMAND` + `MAILBOX_KEY`).
- Mehrere Mailboxen erfordern getrennte Instanzen/Runs mit eigenem Data-Dir.
- Live-Routing-Mirroring ist implementiert, aber noch nicht end-to-end produktiv getestet.
