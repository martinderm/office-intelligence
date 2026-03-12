# Install into existing Mail Agent (Operator Runbook)

Ziel: Ein Main-Agent installiert/aktualisiert den `mail-processor` in einem bestehenden Mail-Agent-Workspace reproduzierbar und sicher.

## 1) Voraussetzungen

- Mail-Agent-Workspace existiert (z. B. `D:/users/dagobert/.openclaw/agents/<agent-id>/workspace`)
- Himalaya-Zugriff ist für **genau eine Mailbox** verfügbar (direkt oder via Gate)
- Projekt ist lokal verfügbar unter:
  - `C:/Users/dagobert-ai/.openclaw/workspace/projects/mail-processor`

## 2) Skill-Dateien im Ziel-Agent bereitstellen

Hinweis zu mailbox-gebundenem Himalaya-Aufruf:
- Empfohlen ist ein Gate (fixe Account-/Command-Policy).
- Alternativ kann ein `.mjs`-Wrapper genutzt werden.
- Für allgemeine Wrapper-Erzeugung liegt ein Beispiel im Projekt: `scripts/create-himalaya-account-proxy.mjs`.


Im Ziel-Agent-Workspace unter `skills/mail-processor/`:

- `SKILL.md`
- `scripts/run-shadow.mjs`
- `scripts/run-run.mjs`

Die Run-Skripte müssen mindestens setzen:

- `HIMALAYA_COMMAND=<agent-spezifischer command/gate oder node-wrapper>`
- `MAILBOX_KEY=<kurzer stabiler key>` (z. B. `primary-mailbox`)
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

```bash
node skills/mail-processor/scripts/run-shadow.mjs --fetch-limit=1
```

Erwartung:

- `data/mail-processor/state.jsonl` existiert/wird ergänzt
- `data/mail-processor/capabilities/<MAILBOX_KEY>.json` existiert
- Shadow-Run ohne Routing-Aktionen

## 5) Optional: Discovery + reviewed Apply

```bash
npm run discover-projects -- --discover-last=200
```

- Ergebnis liegt in `memory/references/projects/inbox/*.json`
- Danach reviewed übernehmen:

```bash
npm run apply:suggestions -- --input=memory/references/projects/inbox/<datei>.json
```

- Wirkung: `projects.json` aktualisiert, `changelog.md` ergänzt, fehlende Projektordner (`<id>/index.md`, `signals.md`, `evidence/`, `topics/`) werden erzeugt

## 6) Go-Live (erst nach Shadow-Validierung)

```bash
node skills/mail-processor/scripts/run-run.mjs
```

Empfehlung:

- zuerst kleine Batches (`FetchLimit` klein)
- State/Artefakte nach jedem Lauf prüfen

## 7) Betriebsgrenzen (aktuell)

- Ein Run arbeitet gegen genau **eine** Mailbox (`HIMALAYA_COMMAND` + `MAILBOX_KEY`).
- Mehrere Mailboxen erfordern getrennte Instanzen/Runs mit eigenem Data-Dir.
- Live-Routing-Mirroring ist implementiert, aber noch nicht end-to-end produktiv getestet.
