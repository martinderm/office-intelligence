# mail-processor

Standalone Mail-Triage/Routing/Processing pipeline (concept-first).

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

Optional (nur wenn explizit erlaubt):

```bash
npm run run
```

> `run` bricht absichtlich ab, wenn `MAIL_ROUTING_ENABLED` nicht auf `true` gesetzt ist.

## Aktueller Implementierungsstand

- ✅ TypeScript-CLI mit `shadow` / `run`
- ✅ `.env`-Loading + Config Defaults
- ✅ Lockfile (Single-Runner, TTL)
- ✅ `projects.json`-Validation (MVP-Felder + Slug-ID)
- ✅ JSONL-State-Logging (`run_started`, `message_processed`, `message_error`, `run_finished`)
- ✅ Himalaya-Adapter für `envelope list`, `message read`, `message copy`
- ✅ Deterministischer Matcher + needsReply-Heuristik + Debug-Artefakte pro Mail (`data/mail-routing/msgs/*.json`)
- ✅ Mock-Mode (`HIMALAYA_COMMAND=mock`) für lokale Tests ohne echte Mailbox
- ⏳ LLM-Extraktion (striktes JSON-Schema + Retry/Backoff) folgt als nächster Schritt

## Himalaya Command Beispiele

```bash
# generisch
HIMALAYA_COMMAND=himalaya

# Agent-Gate (Beispiel aus boku-martin)
HIMALAYA_COMMAND=skills/himalaya-account-main/scripts/himalaya-account-main-gate.exe

# lokaler Test ohne Mailbox
HIMALAYA_COMMAND=mock
```

