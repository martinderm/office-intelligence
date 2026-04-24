# Skill: mail-processor

Bindet die Mail-Verarbeitungskomponente **mail-processor** als OpenClaw-Skill in einen Agenten ein.
Im größeren Bild ist sie Teil von **office-intelligence**, bleibt technisch aber klar auf Mail-Triage, Extraktion/Klassifizierung und sicheres Routing bzw. kontrollierte Move-/Sync-Aktionen via Himalaya/IMAP fokussiert.

## Was der Skill bereitstellt

- **Einheitlicher Entry Point** über `HIMALAYA_COMMAND` (direktes Himalaya-Binary oder Agent-Gate)
- **Konventionen** für Ordner/Dateien im Agent-Workspace (`/memory`, `/data`)
- **Shadow-Mode** als Standard (erst klassifizieren/loggen; kein COPY)
- **Automatischer Mailbox-Folder-Sync** mit TTL-Prüfung vor normalen Mail-Runs; Force-Refresh nur bei Bedarf
- **Pending-Actions-Queue** für klassifizierte Mail-Artefakte, die noch kontextuell nachverarbeitet werden müssen
- **Weekly Action Logs** für abgearbeitete/verwerfene Pending Actions
- **Pending-Decisions-Queue** für fehlende referenzierte Ordner und echte menschliche Entscheidungen
- **Gezielte Folder-Inspektion** eines existierenden Mailbox-Ordners ohne Routing-Aktion
- **Gezielter Folder-Sync** eines existierenden Mailbox-Ordners in die lokale Artefaktstruktur
- **Guardrails**: Locking/Idempotenz, kontrollierte Mailbox-Aktionen nur über offizielle Pfade, Fail-safe Defaults

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
      pending-actions.json
      pending-decisions.json
      memory_suggestions.jsonl
      logs/
        actions/
          YYYY-Www.json
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
- `PENDING_ACTIONS_FILE=./data/mail-processor/pending-actions.json`
- `ACTION_LOG_DIR=./data/mail-processor/logs/actions`
- `MAIL_COPY_SEMANTICS=acts_like_move` bei BOKU/GroupWise, weil `copy` dort de-facto wie `move` wirkt

Sicherer Start:
- `MAIL_ROUTING_ENABLED=false`  (Shadow-Mode)

Runner-Konvention:
- Agent-spezifische Konfiguration liegt in `<agent-workspace>/.env`.
- `skills/mail-processor/scripts/run-shadow.mjs`, `run-run.mjs` und `run-discover-projects.mjs` laden nur diese `.env`.
- In den Runnern sind keine mailbox-/proxy-/pfadbezogenen Hardcodes erlaubt.
- Runner toggeln nur den Modus (`MAIL_ROUTING_ENABLED`) und optional `MAIL_FETCH_LIMIT`.
- Runner bauen **nicht** automatisch. Sie starten die bereits gebaute `dist/cli.js` im zentralen Projekt.
- Build ist ein expliziter Dev-/Test-Schritt: einmal `npm run build` im Repo ausführen. Optional können Runner mit `--build` oder `MAIL_PROCESSOR_BUILD_BEFORE_RUN=true` dennoch vorher bauen.

Hinweis:
- Der operative Klassifikationspfad läuft über das OpenClaw-Tool `mail-classify`.
- `LLM_*` Variablen bleiben nur für getrennte Discovery-/Suggestion-Pfade relevant und sind für den normalen Shadow-/Routing-Pfad nicht die primäre Abhängigkeit.

Siehe vollständige Liste: `/.env.example` im Repo.

## Kommandos / Aktionen

### 1) Shadow Run
- `npm run shadow`
- Im Agent-Workspace: `node skills/mail-processor/scripts/run-shadow.mjs --fetch-limit=1`

### 2) Routing Run (gated)
- `npm run run`
- Im Agent-Workspace: `node skills/mail-processor/scripts/run-run.mjs --fetch-limit=1`

Hinweis: Agent-Runner nutzen standardmäßig die vorhandene `dist/cli.js` und bauen nicht automatisch. Bei fehlender `dist/cli.js` brechen sie mit klarer Fehlermeldung ab. Explizit bauen: `--build` oder `MAIL_PROCESSOR_BUILD_BEFORE_RUN=true`.

### 3) Kontrollierter Move einer explizit ausgewählten Mail
- `node dist/cli.js --mode=move --source-folder="INBOX" --target-folder="Projekte/USAGE-NG" --ids=7588`
- Optional nur zur Vorschau: `node dist/cli.js --mode=move --source-folder="INBOX" --target-folder="Projekte/USAGE-NG" --ids=7588 --dry-run`
- Der Move-Pfad nutzt die bestehende Copy/Move-Semantik des Systems und hängt lokale Artefakte (`msgs`, `exports`, `history`, `folders`, `routing`) anschließend auf den Zielordnerzustand um.

### 4) Mailbox-Folder-Sync / Pending Decisions

- Normale Mail-Runs prüfen automatisch vorab, ob `data/mail-processor/mailbox-folders.json` fehlt oder älter als `MAILBOX_FOLDERS_MAX_AGE_HOURS` ist. Nur dann wird live neu geholt.
- Force-Refresh: `npm run sync:mailbox-folders`
- Source of truth für Zielordner bleibt in `projects.json` und `topics.json`; `mailbox-folders.json` ist nur beobachteter Snapshot.
- Fehlende referenzierte Ordner landen in `data/mail-processor/pending-decisions.json`.
- Wenn der Skill in einer aktiven Chat-Session läuft und offene `pending-decisions` existieren, sollen diese **direkt kurz abgefragt** werden, statt still liegenzubleiben.
- Der CLI-Output eines normalen Runs enthält dafür `pendingDecisions.count`, `pendingDecisions.prompts` und `pendingDecisions.path`; diese Felder sind im Live-Chat aktiv aufzugreifen statt nur zu loggen.
- Wenn keine aktive Session vorhanden ist, bleiben Entscheidungen in `pending-decisions.json`, bis sie im nächsten aktiven Kontext aufgegriffen werden.
- Für gezielte Prüfung eines existierenden Ordners: `node dist/cli.js --mode=shadow --inspect-folder="Projekte/USAGE-NG"`.
- Dieser Pfad liest nur die letzten `MAIL_FETCH_LIMIT` Envelopes des angegebenen Ordners und eignet sich für Review/Diagnose ohne Routing.
- Für gezielte Materialisierung bzw. Konsolidierung eines existierenden Ordners in die lokale Struktur: `node dist/cli.js --mode=shadow --sync-folder="Projekte/USAGE-NG"`.
- Dieser Pfad liest die letzten `MAIL_FETCH_LIMIT` Nachrichten des angegebenen Ordners, schreibt bzw. konsolidiert Msg-/EML-Artefakte im gleichen Schema wie normale Runs. Wenn dieselbe Mail lokal bereits unter einem anderen Ordnerpfad existiert, wird die lokale Repräsentation per `stableId` auf den synchronisierten Zielordner umgehängt statt dupliziert. Die Sync-Metadaten landen direkt im passenden Folder-Eintrag von `data/mail-processor/mailbox-folders.json`. Verschachtelte Mailbox-Ordner werden lokal ebenfalls verschachtelt abgebildet.

### 5) Pending Actions / Nachverarbeitung

- Jeder erfolgreich klassifizierte Mail-JSON-Artefakt erzeugt/aktualisiert ein schlankes Item in `data/mail-processor/pending-actions.json`.
- Diese Datei enthält nur Finder-Informationen: `stable_id`, `file_id`, JSON-Artefaktpfad, Target (`project|topic|none`), Confidence, `needs_reply`, Klassifikationsquelle.
- Keine Mailinhalte, Zusammenfassungen, Wissensvorschläge oder Entscheidungen in `pending-actions.json` speichern.
- Abgearbeitete Items werden aus `pending-actions.json` entfernt und in das Weekly Log unter `data/mail-processor/logs/actions/YYYY-Www.json` geschrieben.
- CLI-Helfer:
  - `node dist/cli.js --pending-actions-list`
  - `node dist/cli.js --pending-actions-mark-done=<action-id>`
- `pending-decisions.json` bleibt strikt für menschliche Entscheidungen; `target.kind=none` ist keine Entscheidung.

### 6) Needs-Reply-Routing

- Bei normaler Copy-Semantik kann eine Mail in Parent-Ordner **und** `_Needs-Reply`-Unterordner landen.
- Bei `MAIL_COPY_SEMANTICS=acts_like_move` (BOKU/GroupWise) gilt Single-Target-Routing:
  - Project + `needs_reply=true` → `<project.mailbox_folder>/_Needs-Reply`
  - Topic ohne Project + `needs_reply=true` → `<topic.mailbox_folder>/_Needs-Reply`
  - kein Project/Topic + `needs_reply=true` → `Inbox/_Needs-Reply`
  - ohne Antwortbedarf → Parent-Ordner, soweit Project/Topic gematcht
- Fehlende `_Needs-Reply`-Unterordner werden als `pending-decisions` gemeldet; nicht still annehmen.

### 7) Wissenspflege aus Mail-Artefakten (reviewed)
- Discovery (Default: lokale `exports/**/*.eml`): `node skills/mail-processor/scripts/run-discover-projects.mjs --discover-last=200`
- Optional IMAP-Quelle: `node skills/mail-processor/scripts/run-discover-projects.mjs --discover-source=imap --discover-last=200`
- Review-Queue: `memory/references/projects/inbox/*.json`
- Apply: `npm run apply:suggestions -- --input=<datei.json>`
- Wirkung: aktualisiert `projects.json`, pflegt `changelog.md`, erstellt fehlende Projektordner (`<id>/index.md`, `signals.md`, `evidence/`, `topics/`)

### 8) Konsolidierung in Wissens-/Projektordner (Agent-basiert)
- Die Konsolidierung wird **vom OpenClaw-Agenten** durchgeführt, nicht durch ein lokales Merge-Skript.
- Input: verarbeitete Mail-Artefakte unter `data/mail-processor/msgs/**/*.json`.
- Ziel: Managed-Sections in `index.md`/`signals.md` aktualisieren und Evidenz in `evidence/YYYY-MM.md` ergänzen.
- Regel: nur bei klarer Zuordnung/hoher Confidence; bei Ambiguität in Review-Queue statt Direkt-Write.
- Erweiterung: bei kritisch fehlenden Infos stellt der Agent kurze, gezielte Rückfragen (statt Annahmen zu treffen).
- User-Trigger möglich: aktive Vervollständigung von Projekt-Metadaten (inkl. Workpackages).
- Referenz-Task: `skills/mail-processor/PROJECT_MEMORY_AGENT_TASK.md`

## Safety / Guardrails (müssen im Skill enforcebar sein)

- Mailbox-Schreibaktionen nur über explizite offizielle Pfade (Routing-Run / kontrollierter Move), nie implizit durch Review-/Sync-Hilfspfade
- Shadow-Mode liest/klassifiziert nur und schreibt lokale Artefakte/Pending Actions; keine Mailbox-Aktion
- Safe default: bei Ambiguität **keine Aktion**
- Hard negative rules für needsReply (Newsletter/Auto-Reply/no-reply)
- JSON-Schema-Validation für Tool-/LLM-Extrakt; bei Fehlern skip+log
- Retention für `data/mail-processor/msgs/**/*.json` (z.B. 30 Tage)
- Guard gegen Doppelverarbeitung: wenn vollständiges `msgs/**/<fileId>.json` existiert (inkl. LLM-Feld, falls aktiv), dann skip (Legacy `<stableId>.json` wird weiterhin erkannt)

## Output / Artefakte

- `data/mail-processor/state.jsonl` — Idempotenz-Log
- `data/mail-processor/msgs/<folder-path>/<fileId>.json` — Extrakte/Debug inkl. `history[]`, `routing` und zusätzlichem `folders`-Block
- `data/mail-processor/mailbox-folders.json` — beobachteter Mailbox-Ordnerbaum (Cache/Snapshot), optional pro Folder mit `sync`-Block zu gezielten Folder-Sync-Läufen
- `data/mail-processor/exports/<folder-path>/<fileId>.eml` — lokale EML-Ablage
- `fileId` wird im Msg-Artefakt unter `local.fileId` mitgeführt
- `fileId` wird deterministisch aus `stableId` abgeleitet: `sha256(stableId)` → `base64url` → auf 16 Zeichen gekürzt (kompakter Dateiname, minimales Kollisionsrisiko)
- `data/mail-processor/pending-actions.json` — offene Mail-Postprocessing-Queue; schlank, keine Mailinhalte/Entscheidungen
- `data/mail-processor/logs/actions/YYYY-Www.json` — Weekly Action Logs für erledigte/verwerfene Pending Actions
- `data/mail-processor/pending-decisions.json` — offene/gelöste Entscheidungen zu fehlenden referenzierten Ordnern oder echten Review-Fragen
- `data/mail-processor/memory_suggestions.jsonl` — Vorschläge zur Katalogpflege
- `data/mail-processor/capabilities/<MAILBOX_KEY>.json` — Capabilities + Policy-Cache

## Betriebsgrenze (aktuell)

- Ein Run arbeitet gegen genau **eine** Mailbox-Instanz (`HIMALAYA_COMMAND` + `MAILBOX_KEY`).
- Mehrere Mailboxen erfordern getrennte Instanzen/Runs mit jeweils eigenem Data-Dir.

