# mail-processor

Standalone Mail-Triage/Routing/Processing pipeline (concept-first).

Für die Installation in einen bestehenden Mail-Agent siehe:
- `docs/INSTALL-INTO-AGENT.md`

Geplante Weiterentwicklungen:
- `docs/ROADMAP.md`

Memory-Update-Flow (kurz):
1) Discovery über Runner starten: `node skills/mail-processor/scripts/run-discover-projects.mjs --discover-last=200`
2) Vorschlag in `memory/references/projects/inbox/*.json` prüfen
3) `npm run apply:suggestions -- --input=<datei.json>`
4) Konsolidierung durch den OpenClaw-Agenten (nicht per lokales Merge-Skript)

Agent-Deploy-Konvention:
- Agent-spezifische Mailbox/Proxy/Pfade stehen in `<agent-workspace>/.env`.
- Skill-Runner dürfen diese Werte nicht hardcoden.
- Siehe `docs/INSTALL-INTO-AGENT.md`.

## Defaults & instanzspezifische Pflichtwerte

Der `mail-processor` hat im Code bereits konservative Defaults (Shadow-first, Routing standardmäßig aus, sinnvolle Schwellwerte/Timeouts/Sanitizing). Das heißt: Für einen sicheren Start musst du **nicht** alles konfigurieren.

Typisch instanzspezifisch und daher immer explizit zu setzen/prüfen:
- `HIMALAYA_COMMAND` (konkreter Himalaya-Binary-/Gate-/Wrapper-Pfad deiner Instanz; **muss mailbox-gebunden sein**)
- `MAILBOX_KEY` (stabiler, kurzer Mailbox-Schlüssel für capability cache-Dateien)
- `PROJECTS_JSON_PATH` (dein Projektkatalog im jeweiligen Workspace)
- `MAIL_PROCESSOR_DATA_DIR` (pro Instanz/Agent eigener Datenpfad)
- LLM-Zugang (`LLM_BASE_URL`, `LLM_API_KEY`, `LLM_MODEL`), falls LLM-Extraktion genutzt wird
- `MAIL_SOURCE_FOLDER`, wenn nicht `INBOX`

Alle übrigen Parameter können in der Regel auf Default bleiben und nur bei Bedarf nachgeschärft werden.

**Wichtige Betriebsgrenze (aktuell):** Ein `mail-processor`-Run arbeitet derzeit immer gegen **genau eine Mailbox/Instanz** (ein `HIMALAYA_COMMAND` + ein `MAILBOX_KEY` pro Lauf). Mehrere Mailboxen parallel erfordern aktuell getrennte Instanzen/Runs mit jeweils eigenem Data-Dir.

## Agent-Workspace Struktur

`mail-processor` ist so gedacht, dass es in einem Agent-Workspace läuft. Der Agent soll dafür eine **Memory-Struktur unter `/memory`** anlegen (relativ zum Workspace-Root).

Wichtige Pfade:
- `memory/references/projects/projects.json` — Projektkatalog (Source of truth für Routing)
- `memory/references/projects/README.md` — Doku + Schema, wie der Katalog aufgebaut sein soll

Siehe: `memory/references/projects/README.md`

## Quickstart

```bash
npm install
npm run build
```

1) `.env.example` nach `.env` kopieren und Werte setzen.
2) `memory/references/projects/projects.json` anlegen.
3) Optional für lokalen Trockenlauf ohne Mailbox: `HIMALAYA_COMMAND=mock` setzen.
4) Shadow-Run starten:

```bash
npm run shadow
```

Optional gezielt nur bestimmte Envelope-IDs verarbeiten:

```bash
npm run shadow -- --ids=7588,7587
```

Optional (nur wenn explizit erlaubt):

```bash
npm run run
```

> `run` bricht absichtlich ab, wenn `MAIL_ROUTING_ENABLED` nicht auf `true` gesetzt ist.

Projektkandidaten aus den letzten Mails vorschlagen (Default: lokale `exports/**/*.eml`):

```bash
npm run discover-projects -- --discover-last=200
```

Optional IMAP als Quelle erzwingen:

```bash
npm run discover-projects -- --discover-source=imap --discover-last=200
```

Optionaler Output-Pfad:

```bash
npm run discover-projects -- --discover-last=200 --discover-output=./memory/references/projects/inbox/project-candidates-manual.json
```

## Plattformübergreifende Runner-Skripte

Für Agent-Workspaces gibt es plattformneutrale Runner:

```bash
node scripts/run-shadow.mjs --fetch-limit=1
node scripts/run-run.mjs
node skills/mail-processor/scripts/run-discover-projects.mjs --discover-last=200
```

Warum Discovery-Runner: Bei Agent-Setups liegen Gate-/Mailbox-Bindung und Pfade in `<agent-workspace>/.env`.
Der Runner lädt diese `.env` zuverlässig; ein direkter Aufruf `npm run discover-projects` im Projektkontext kann sonst auf ein ungebundenes `himalaya` zurückfallen.

Wichtig für alle Runner (`run-shadow`, `run-run`, `run-discover-projects`):
- `AGENT_WORKSPACE_ROOT` als Umgebungsvariable setzen (Pfad zum Agent-Workspace, der die `.env` enthält).
- `MAIL_PROCESSOR_PROJECT_DIR` im Agent-`.env` auf das echte Repo setzen (z. B. `<workspace>/projects/mail-processor`).
- Ohne diesen Wert kann der Prozess im Skill-Ordner landen (`skills/mail-processor`) und dort fehlt erwartungsgemäß `package.json`.

Beispiel:
```bash
AGENT_WORKSPACE_ROOT=/path/to/agent/workspace node skills/mail-processor/scripts/run-discover-projects.mjs --discover-last=200
```

Die Skripte setzen nur sichere Defaults (`MAIL_ROUTING_ENABLED`) und rufen dann die normalen npm-Commands auf.

Health-Check (Lock + State + TLS-Fehlerrate):
```bash
node skills/mail-processor/scripts/check-health.mjs
```
Mit optionaler Sanierung:
```bash
node skills/mail-processor/scripts/check-health.mjs --fix-stale-lock --cleanup-orphaned-runs
```
Optional über `.env` steuerbar:
- `HEALTH_STALE_LOCK_MAX_SECONDS` (Default `300`)
- `HEALTH_RECENT_WINDOW_MINUTES` (Default `60`)
- `HEALTH_MAX_TLS_ERRORS` (Default `3`)
- `HEALTH_ORPHAN_RUN_MAX_AGE_SECONDS` (Default `900`)

## Sync-Check für ausgerollte Skill-Dateien

Für Deployments in Agent-Workspaces gibt es einen generischen Datei-Sync-Check:

```bash
npm run check:sync -- --pair skills/mail-processor <agent-workspace>/skills/mail-processor
```

Optional streng (meldet auch zusätzliche Dateien im Ziel):

```bash
npm run check:sync -- --strict --pair <source-dir> <target-dir>
```

Instanzpfade gehören nicht ins öffentliche README. Tracke deine konkreten Deploy-Pfade in `docs/INSTALL_PATHS.local.md` (Vorlage: `docs/INSTALL_PATHS.example.md`).

## Aktueller Implementierungsstand

- ✅ TypeScript-CLI mit `shadow` / `run`
- ✅ `.env`-Loading + Config Defaults
- ✅ Lockfile (Single-Runner, TTL)
- ✅ `projects.json`-Validation (MVP-Felder + Slug-ID)
- ✅ JSONL-State-Logging (`run_started`, `message_processed`, `message_skipped`, `message_error`, `run_finished`)
- ✅ Himalaya-Adapter für `envelope list`, `message export --full` (bevorzugt), `message read` (Fallback), `message copy`/`message move`
- ✅ Deterministischer Matcher + needsReply-Heuristik + Debug-Artefakte pro Mail (`data/mail-processor/msgs/*.json`)
- ✅ Mock-Mode (`HIMALAYA_COMMAND=mock`) für lokale Tests ohne echte Mailbox
- ✅ LLM-Extraktion über OpenAI-kompatible API (`/chat/completions`, Fallback `/v1/chat/completions`)
- ✅ Modell frei wählbar über `LLM_MODEL`
- ✅ Prompt anpassbar über `LLM_PROMPT_PATH` (Fallback auf eingebauten Default)
- ✅ Antwort-Preprocessing priorisiert aktuelle Nachricht und gewichtet ältere Thread-Blöcke niedriger
- ✅ MIME-aware Body-Extraktion (multipart: bevorzugt `text/html`, fallback `text/plain`) inkl. quoted-printable/base64-Dekodierung
- ✅ Header-Extraktion in strukturiertes `mailMeta` (u. a. From, Subject, Date, Message-ID, List-/Auth-Signale)
- ✅ HTML-/Newsletter-Sanitizing (aktive Inhalte entfernen, Tracking-Parameter strippen, Footer-Trim)
- ✅ Zusätzliche Tokenreduktion: Layout-Noise-Cleanup + Dedupe wiederholter Links
- ✅ Idempotenz auf stabiler ID (primär normalisierte `Message-ID`, fallback Content-Hash) statt folder-lokaler Envelope-ID
- ✅ State speichert `sourceFolder`, `copyTargets`, `lastKnownEnvelopeId`, `lastKnownFolder`
- ✅ Lokale Artefakte nutzen `fileId` (aus `stableId` abgeleitet) und folder-basierte Struktur:
  - `exports/<folder-slug>/<fileId>.eml`
  - `msgs/<folder-slug>/<fileId>.json`
  - `fileId` = `sha256(stableId)` → `base64url` → erste 16 Zeichen
  - inkl. `history[]` (wann/wohin geroutet)
- ✅ Discovery-Mode: erkennt aus den letzten X Mails potenzielle neue Projekte + Kontaktvorschläge für bestehende Projekte (`--discover-projects`)
- ✅ Guard gegen Doppelverarbeitung über bestehende vollständige Artefakte (`msgs/**/<fileId>.json` inkl. LLM-Feld; Legacy `<stableId>.json` wird weiterhin erkannt)

### Msg-Artefakt-Schema (Ausschnitt)

`msgs/<folder-slug>/<fileId>.json` enthält u. a.:

```json
{
  "stableId": "<normalized-message-id-or-fallback>",
  "mailMeta": { "messageId": "<raw-message-id>" },
  "local": {
    "fileId": "<16-char-id>",
    "msgPath": ".../msgs/<folder>/<fileId>.json",
    "exportPath": ".../exports/<folder>/<fileId>.eml",
    "folder": "INBOX"
  }
}
```

Hinweis: `stableId` bleibt der fachliche Idempotenz-Key; `fileId` ist der kompakte Dateiname für lokale Artefakte.

- ⚠️ Live-Routing-Mirroring ist implementiert, aber **noch nicht end-to-end im Produktivmodus getestet**.
- ⏳ Retry/Backoff-Härtung für LLM-Requests folgt als nächster Schritt

## Wichtige Qualitätsvoraussetzung: Projektkatalog

Die Qualität der Klassifizierung hängt stark von `memory/references/projects/projects.json` ab.

Wenn die Projektliste dünn/unscharf ist, wird Routing unzuverlässig (oder bleibt leer). Für gute Ergebnisse braucht der Katalog:
- klare `id` + `title` pro Projekt
- gepflegte `aliases`, `domains`, `contacts`
- sinnvolle `keywords` (spezifisch statt generisch)
- optional `reference_md` mit semantischem Kontext pro Projekt

Kurz: **Gutes LLM + schwacher Projektkatalog = schwaches Routing**.

## Idempotenz & Stable IDs

- Primärer Idempotenz-Key: normalisierte `Message-ID` (global/stabil)
- Fallback bei fehlender `Message-ID`: deterministischer Hash aus Header-/Body-Signal
- Envelope-ID wird nur für Live-Operationen (`read`/`copy`) verwendet
- Vorteil: Copy/Move in andere Ordner erzeugt neue Envelope-IDs, aber keine Doppelverarbeitung

### IMAP-Server-Spezialfall: COPY kann wie MOVE wirken

Einige IMAP-Server/Backends verhalten sich so, dass ein `message copy <target> <id>` **effektiv einem Move entspricht** (beobachtet z. B. bei OpenText GroupWise 25.2.0.148299):
- die Nachricht ist danach im Source-Ordner nicht mehr vorhanden
- im Zielordner erscheint sie unter einer **neuen Envelope-ID**

Konsequenzen für `mail-processor`:
- Envelope-IDs sind **nicht stabil** und dürfen nicht als dauerhafter Verarbeitungsschlüssel verwendet werden (darum: `Message-ID`/Hash).
- Multi-Target-Routing (mehrere `copyTargets`) kann auf solchen Systemen fehlschlagen oder nur den ersten Target erreichen.

Empfehlung:
- Wenn du so ein Backend hast: **Single-Target-Routing** erzwingen (oder klare Priorität: „best match wins“).
- Nach dem Copy optional verifizieren (Source/Target envelope list), wenn Konsistenz kritisch ist.

## Inkrementelle Auswahlsteuerung (neu)

Der Runner kann jetzt steuern, **welche** Mails pro Lauf ausgewählt werden:

- `MAIL_SCAN_MODE=tail` → nur Seite 1 (neueste Mails)
- `MAIL_SCAN_MODE=backfill` → arbeitet seitenweise historisch ab (`cursor.json`)
- `MAIL_SCAN_MODE=auto` → kombiniert Tail + Backfill

Wichtige Parameter:
- `MAIL_ENVELOPE_PAGE_SIZE` (z. B. 100)
- `MAIL_SELECT_MAX_SCAN_PAGES` (z. B. 10)
- `MAIL_CURSOR_FILE` (default `data/mail-processor/cursor.json`)

Explizite Einzelsteuerung pro Lauf:
- `--ids=7588,7587,...`

Hinweis: durch Idempotenz (`stableId`) sind Mehrfach-Scans unkritisch; bereits verarbeitete Mails werden übersprungen.

## Datenpfad & Retention

- Datenpfad frei konfigurierbar über `MAIL_PROCESSOR_DATA_DIR` (relativ oder absolut)
- Empfehlung in Multi-Agent-Setups: **pro Agent eigener Pfad**, z. B. `<agent-workspace>/data/mail-processor`
- Server-Capabilities werden mailbox-spezifisch unter `capabilities/*.json` gespeichert
  - Standardpfad: `<MAIL_PROCESSOR_DATA_DIR>/capabilities`
  - Override möglich über `MAIL_PROCESSOR_CAPABILITIES_DIR`
  - Optionaler Key-Override: `MAILBOX_KEY` (oder `HIMALAYA_MAILBOX_KEY`)
  - Zusätzlich wird ein kleiner **Capability-Policy-Layer** abgeleitet und mitgespeichert
- Retention für Debug-Dateien (`msgs/*.json`) über `MAIL_DEBUG_RETENTION_DAYS`
  - Zahl in Tagen (z. B. `30`)
  - `unlimited` = keine automatische Löschung

## Capability Policy Layer

Aus den Capabilities wird eine kompakte Policy abgeleitet (`capabilities/<mailbox-key>.json`):

- `supportsImap4Rev1`
- `supportsUidPlus`
- `supportsMove`
- `supportsIdle`
- `supportsCondstore`
- `supportsQresync`
- `supportsSpecialUse`
- `supportsNamespace`
- `supportsUtf8Accept`
- `recommendedRoutingMode` (`normal` | `single-target`)
- `rationale[]` (kurze Begründung)

Der Run schreibt zusätzlich ein Event `mailbox_capabilities_loaded` nach `state.jsonl`, inkl. abgeleiteter `policy`.

Hinweis: Der Policy-Layer ist bewusst klein und transparent. Er dient als Grundlage für Routing-Entscheidungen pro Backend, ohne hartcodierte server-spezifische Sonderfälle.

### Routing-Strategie aus Policy + Konfiguration

Zusätzlich zur Policy steuern diese Env-Variablen das operative Verhalten:

- `MAIL_ROUTE_ACTION=auto|copy|move`
- `MAIL_COPY_SEMANTICS=normal|acts_like_move`
- `MAIL_ROUTE_STRICT=true|false`
- `MAIL_USE_UIDPLUS=true|false` (optional; nur wirksam wenn `supportsUidPlus=true`)

Logik:
- `auto` nutzt `move`, wenn `supportsMove=true`, sonst `copy`.
- `move` ohne Capability führt bei `MAIL_ROUTE_STRICT=true` zum Fehler, sonst Fallback auf `copy`.
- `MAIL_COPY_SEMANTICS=acts_like_move` erzwingt **Single-Target-Routing** (auch bei `copy`), um serverseitige Copy→Move-Semantik sauber zu behandeln.

Pro Run wird ein Event `routing_policy_resolved` geschrieben; pro Mail wird die effektive Routing-Entscheidung in `message_processed` mitgeloggt.
Wenn `MAIL_USE_UIDPLUS=true` und der Server `UIDPLUS` anbietet, wird bei COPY zusätzlich ein Event `uidplus_copy_mapping` (COPYUID-Mapping) in `state.jsonl` geschrieben.

## Himalaya Command Beispiele

Wichtig: `HIMALAYA_COMMAND` soll immer eine **explizit gebundene Mailbox** erzwingen.
Empfohlen über ein Gate oder ein kleines Wrapper-Skript (`.mjs`), das den Account fest setzt.

```bash
# EMPFOHLEN: Agent-Gate mit fixer Mailbox-Bindung
HIMALAYA_COMMAND=./skills/himalaya-gate/scripts/himalaya-gate.exe

# ALTERNATIVE: mjs-Wrapper mit festem Account
HIMALAYA_COMMAND=node ./scripts/himalaya-account-proxy.mjs

# lokaler Test ohne Mailbox
HIMALAYA_COMMAND=mock
```

Hinweis: Ein nacktes `HIMALAYA_COMMAND=himalaya` ist nur dann sinnvoll,
wenn die Mailbox-Bindung an anderer Stelle technisch erzwungen wird.

### Beispiel: generischer Proxy-Generator

Für Umgebungen ohne Gate gibt es ein allgemeines Beispielskript, das einen `.mjs`-Wrapper mit fixer Account-Bindung erzeugt:

```bash
node scripts/create-himalaya-account-proxy.mjs --account=ACCOUNT_NAME --out=./scripts/himalaya-account-proxy.mjs
```

Danach kann der erzeugte Wrapper als `HIMALAYA_COMMAND` verwendet werden.


