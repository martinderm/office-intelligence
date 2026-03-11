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

## Datenpfad & Retention

- Datenpfad frei konfigurierbar über `MAIL_PROCESSOR_DATA_DIR` (relativ oder absolut)
- Empfehlung in Multi-Agent-Setups: **pro Agent eigener Pfad**, z. B. `<agent-workspace>/data/mail-routing`
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

## Himalaya Command Beispiele

```bash
# generisch
HIMALAYA_COMMAND=himalaya

# Agent-Gate (Beispiel aus boku-martin)
HIMALAYA_COMMAND=skills/himalaya-account-main/scripts/himalaya-account-main-gate.exe

# lokaler Test ohne Mailbox
HIMALAYA_COMMAND=mock
```

