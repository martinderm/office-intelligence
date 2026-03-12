# Install into existing Mail Agent (Operator Runbook)

Ziel: Ein Main-Agent installiert/aktualisiert den `mail-processor` in einem bestehenden Mail-Agent-Workspace reproduzierbar und sicher.

## 1) Voraussetzungen

- Mail-Agent-Workspace existiert (z. B. `D:/users/dagobert/.openclaw/agents/<agent-id>/workspace`)
- Himalaya-Zugriff ist für **genau eine Mailbox** verfügbar (direkt oder via Gate)
- Projekt ist lokal verfügbar unter:
  - `C:/Users/dagobert-ai/.openclaw/workspace/projects/mail-processor`

## 2) Skill-Dateien im Ziel-Agent bereitstellen

Im Ziel-Agent-Workspace unter `skills/mail-processor/`:

- `SKILL.md`
- `scripts/run-shadow.ps1`
- `scripts/run-run.ps1`

Die Run-Skripte müssen mindestens setzen:

- `HIMALAYA_COMMAND=<agent-spezifischer command/gate>`
- `MAILBOX_KEY=<kurzer stabiler key>` (z. B. `boku-martin`)
- `MAIL_SOURCE_FOLDER=INBOX` (oder Instanzwert)
- `PROJECTS_JSON_PATH=./memory/references/projects/projects.json`
- `MAIL_PROCESSOR_DATA_DIR=<agent-workspace>/data/mail-processor`
- `MAIL_ROUTING_ENABLED=false` im Shadow-Skript
- `MAIL_ROUTING_ENABLED=true` im Run-Skript

## 3) Projektkatalog sicherstellen

Im Ziel-Agent-Workspace muss vorhanden sein:

- `memory/references/projects/projects.json`

Hinweis: Ohne gepflegten Katalog bleibt Matching schwach/unzuverlässig.

## 4) Build & Smoke-Test

Im Projektverzeichnis:

```bash
npm install
npm run build
npm run check
```

Dann im Ziel-Agent:

```powershell
skills/mail-processor/scripts/run-shadow.ps1 -FetchLimit 1
```

Erwartung:

- `data/mail-processor/state.jsonl` existiert/wird ergänzt
- `data/mail-processor/capabilities/<MAILBOX_KEY>.json` existiert
- Shadow-Run ohne Routing-Aktionen

## 5) Optional: Discovery-Lauf

```bash
npm run discover-projects -- --discover-last=200
```

- Ergebnis prüfen (`project-candidates.json`)
- Kandidaten manuell nach `projects.json` übernehmen

## 6) Go-Live (erst nach Shadow-Validierung)

```powershell
skills/mail-processor/scripts/run-run.ps1
```

Empfehlung:

- zuerst kleine Batches (`FetchLimit` klein)
- State/Artefakte nach jedem Lauf prüfen

## 7) Betriebsgrenzen (aktuell)

- Ein Run arbeitet gegen genau **eine** Mailbox (`HIMALAYA_COMMAND` + `MAILBOX_KEY`).
- Mehrere Mailboxen erfordern getrennte Instanzen/Runs mit eigenem Data-Dir.
- Live-Routing-Mirroring ist implementiert, aber noch nicht end-to-end produktiv getestet.
