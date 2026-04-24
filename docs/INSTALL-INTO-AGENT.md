# Install office-intelligence into existing Agent (Operator Runbook)

Ziel: Ein Main-Agent installiert/aktualisiert die relevanten `office-intelligence`-Skills in einem bestehenden Agent-Workspace reproduzierbar und sicher. Für einen bestehenden Mail-Agenten umfasst der Standard-Rollout bis auf Weiteres drei Skill-Verzeichnisse:

- `skills/mail-desk`
- `skills/project-catalog-entry`
- `skills/topic-catalog-entry`

`skills/mail-processor` wird **nicht mehr standardmäßig ausgerollt**. Nur bei expliziter Legacy-/Experiment-Anforderung mitnehmen.

Wichtig: `mail-desk` ist der bevorzugte leichte Agenten-Workflow für Einzelmail-Bearbeitung und nutzt den vorhandenen mailbox-spezifischen Himalaya-Skill. Der alte `mail-processor` enthält operative Runner für die schwerere Pipeline, wird aber bis auf Weiteres nicht regulär installiert. Der eigentliche TypeScript-/Node-Projektcode (`package.json`, `src/`, zentrale `scripts/`) bleibt im Projekt-Repo `projects/office-intelligence`.

## 1) Voraussetzungen

- Mail-Agent-Workspace existiert (z. B. `D:/users/dagobert/.openclaw/agents/<agent-id>/workspace`)
- Himalaya-Zugriff ist für **genau eine Mailbox** verfügbar (direkt oder via Gate)
- Projekt ist lokal verfügbar unter:
  - `C:/Users/dagobert-ai/.openclaw/workspace/projects/office-intelligence`

## 2) Skill-Dateien im Ziel-Agent bereitstellen

Standard-Rollout in den Ziel-Agenten:

- `skills/mail-desk/`
- `skills/project-catalog-entry/`
- `skills/topic-catalog-entry/`

Diese drei Verzeichnisse werden als komplette Skill-Ordner in den Agent-Workspace kopiert. Andere bereits vorhandene Skills des Ziel-Agenten bleiben unberührt.

Nicht standardmäßig kopieren:

- `skills/mail-processor/` — nur auf expliziten Wunsch für Legacy-/Experiment-Pipeline.

Hinweis zu mailbox-gebundenem Himalaya-Aufruf:
- Empfohlen ist ein Gate (fixe Account-/Command-Policy).
- Alternativ kann ein `.mjs`-Wrapper genutzt werden.
- Für allgemeine Wrapper-Erzeugung liegt ein Beispiel im Projekt: `scripts/create-himalaya-account-proxy.mjs`.


Legacy-Hinweis zu `mail-processor`:

- Nur bei expliziter Anforderung `skills/mail-processor/` zusätzlich kopieren.
- Dann liegen dort typischerweise `SKILL.md`, `PROJECT_MEMORY_AGENT_TASK.md` und Runner unter `scripts/`.
- Die Runner nutzen `MAIL_PROCESSOR_PROJECT_DIR` und die bereits gebaute `dist/cli.js` im zentralen Repo.
- Für den normalen Mail-Desk-Betrieb ist das alles nicht erforderlich.

Empfohlene `.env`-Felder im Agent-Workspace für Legacy-`mail-processor` nur bei expliziter Nutzung:
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

## 3) Mail-Desk bevorzugen

Für normale Einzelmail-Arbeit zuerst `skills/mail-desk/SKILL.md` verwenden:

- Mail direkt über den mailbox-spezifischen Himalaya-Skill lesen
- Projekt-/Topic-Kontext aus `memory/references/...` laden
- Entscheidung und leichte Logs unter `data/mail-desk/` schreiben
- nur bei klarer Lage verschieben/kopieren

`mail-processor` bleibt verfügbar, soll aber nicht der erste Reflex für normale Triage sein.

## 4) Wissenskataloge sicherstellen

Im Ziel-Agent-Workspace muss vorhanden sein:

- `memory/references/projects/projects.json`

Hinweis: Ohne gepflegte Kataloge bleibt Matching schwach/unzuverlässig.

## 5) Build & Smoke-Test

Im Projektverzeichnis (also im zentralen Repo, nicht im Agent-Skill-Ordner):

```bash
npm install
npm run build
npm run check
```

Dieser Build erzeugt/aktualisiert `projects/office-intelligence/dist/`. Agenten nutzen danach direkt diese gebauten Dateien; sie bauen im Normalbetrieb nicht selbst.

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

## 6) Optional: Discovery + reviewed Apply

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

## 7) Go-Live Pipeline-Pfad (erst nach Shadow-Validierung)

Vor Go-Live sicherstellen, dass im zentralen Repo ein aktueller Build vorliegt (`npm run build`). Danach nutzen Agent-Runner direkt `dist/cli.js`.

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

## 8) Betriebsgrenzen (aktuell)

- Ein Run arbeitet gegen genau **eine** Mailbox (`HIMALAYA_COMMAND` + `MAILBOX_KEY`).
- Mehrere Mailboxen erfordern getrennte Instanzen/Runs mit eigenem Data-Dir.
- Live-Routing-Mirroring ist implementiert, aber noch nicht end-to-end produktiv getestet.
