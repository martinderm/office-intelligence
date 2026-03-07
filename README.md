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
- ✅ JSONL-State-Logging (`run_started`, `message_processed`, `message_skipped`, `message_error`, `run_finished`)
- ✅ Himalaya-Adapter für `envelope list`, `message export --full` (bevorzugt), `message read` (Fallback), `message copy`
- ✅ Deterministischer Matcher + needsReply-Heuristik + Debug-Artefakte pro Mail (`data/mail-routing/msgs/*.json`)
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

## Datenpfad & Retention

- Datenpfad frei konfigurierbar über `MAIL_PROCESSOR_DATA_DIR` (relativ oder absolut)
- Empfehlung in Multi-Agent-Setups: **pro Agent eigener Pfad**, z. B. `<agent-workspace>/data/mail-routing`
- Retention für Debug-Dateien (`msgs/*.json`) über `MAIL_DEBUG_RETENTION_DAYS`
  - Zahl in Tagen (z. B. `30`)
  - `unlimited` = keine automatische Löschung

## Himalaya Command Beispiele

```bash
# generisch
HIMALAYA_COMMAND=himalaya

# Agent-Gate (Beispiel aus boku-martin)
HIMALAYA_COMMAND=skills/himalaya-account-main/scripts/himalaya-account-main-gate.exe

# lokaler Test ohne Mailbox
HIMALAYA_COMMAND=mock
```

