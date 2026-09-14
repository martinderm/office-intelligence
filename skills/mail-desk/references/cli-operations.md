# mail-desk CLI-Operationen

Technische Referenz für Script-Zugriffe, kanonische Envelopes, zugelassene
Werkzeugaufrufe, Final-Index-Zugangsregeln und den verpflichtenden
Compliance-Output. Fachliche Entscheidungslogik und Reihenfolge stehen im
[`SKILL.md`](../SKILL.md); Datenformate und Feldsemantik in
[`log-schema.md`](log-schema.md); Batch-Modi und Manifeste in
[`batch-runner.md`](batch-runner.md).

## Pfadkonvention

Script- und Hilfsdateipfade nach Möglichkeit relativ zur `SKILL.md` bzw. zu
ihrem Verzeichnis lesen und verwenden; keine konkurrierenden Pfadvarianten
parallel pflegen.

Für die Skriptaufrufe sind `python3` **und** `python` erlaubt; verwende die
Variante, die lokal verfügbar ist.

## Kanonische CLI-Envelope-Regel

Alle Werkzeuge unter `scripts/` emittieren im JSON-Modus ein kanonisches
Envelope mit genau diesen Top-Level-Feldern:

```json
{
  "action": "...",
  "success": true,
  "state": "Completed",
  "message": "...",
  "data": {},
  "error": null
}
```

- `action`: Operationsname, nichtleere getrimmte Zeichenfolge.
- `success`: Boolean.
- `state`: nichtleere getrimmte Zeichenfolge (z. B. `Completed`, `Failed`,
  `PartialFailure`).
- `message`: menschenlesbare Zeichenfolge.
- `data`: JSON-Objekt mit Nutzlast.
- `error`: bei Erfolg `null`; bei Fehler ein Objekt mit nichtleerem `type` und
  String `message`.

Historische `ok`-/`status`-Formen sind nicht der aktuelle Vertrag. Den
befristeten [`Legacy-CLI-Adapter`](legacy-cli-adapter.md) nur laden und
verwenden, wenn ein ausdrücklich identifizierter historischer `ok`-/`status`-
Konsument weiterbetrieben werden muss; nie für neue Aufrufer.

## Modularer Kern (`scripts/core/`)

- `core/himalaya.py`: Robuste CLI-Ausführung, Header-/Preview-Extraktion,
  Encoding-Schutz und Parallelsuche.
- `core/index.py`: Atomares Lesen, Schreiben, Filtern und Lookup für
  `final-location-index.json`.
- `core/action_log.py`: Protokollierung in `action-log.jsonl`,
  `replies-needed.jsonl` und Case-Archivierung.
- `core/evidence.py`: Aktualisierung von Markdown-Evidenzen
  (`evidence/YYYY-MM.md`) mit Dublettenerkennung.
- `core/classifier.py`: Offline-Regel- und Katalog-Klassifikation mit
  Projekt-/Topic-Pattern-Matching.
- `core/envelope.py`: Zentraler Envelope-Builder für kanonische JSON-Ausgabe.
- `core/progress.py`: Atomare Fortschritts- und ETA-Statusdatei für
  Batch-Läufe.

## Standardisierte Werkzeugleiste

1. **Batch-Runner (`mail_desk_batch_runner.py`):**
   Zentraler Batch-Prozessor für Entwurf (`draft`), Routing, Verifikation,
   Indexierung, Evidenzfortschreibung und Echtzeit-Fortschrittstelemetrie.
   Modi, Manifeste, Schemas und Beispiele:
   [`batch-runner.md`](batch-runner.md).

2. **Offline-Inspektion & Katalog-Reevaluierung (`mail_desk_inspect_manifest.py`):**
   Prüft, filtert und reklassifiziert erstellte Batch-Manifeste offline gegen
   `projects.json` und `topics.json` vor der eigentlichen IMAP-Ausführung
   (`--reclassify`, `--unindexed`, `--unclassified`).

3. **Himalaya & IMAP JSON-Client (`mail_desk_himalaya_client.py`):**
   Nur beim Himalaya-/IMAP-Backend den vollständigen
   [Himalaya-/IMAP-Adapter](backends/himalaya.md) anwenden.

4. **Final Location Index Client (`mail_desk_final_location_index.py`):**
   Kapselt alle Lese-, Schreib-, Lookup-, Statistik- und Filteroperationen
   auf `final-location-index.json`.

5. **Erledigung und Fall-Archivierung (`mail_desk_resolve_case.py`):**
   Archiviert offene Einträge aus `replies-needed.jsonl` oder
   `pending-review.jsonl` direkt unter dem wochenbasierten Pfad
   `archive/YYYY-Www/` und aktualisiert den Status.

   ```bash
   python3 scripts/mail_desk_resolve_case.py \
       --message-id '...' --status 'resolved' --resolution '...'
   ```

6. **Mailbox-Preflight-Check (`mailbox_preflight.py`):**
   Validiert die Erreichbarkeit und Authentifizierung des konfigurierten
   Mailkontos vor komplexen Operationen.

## Harte Regel: kein manueller Final-Index-Write

`data/mail-desk/final-location-index.json` darf **niemals manuell** editiert
oder direkt im Rohtext gelesen werden. Ausschließlich zulässig ist das
standardisierte Werkzeug `mail_desk_final_location_index.py` bzw. der
Batch-Runner:

```bash
python3 scripts/mail_desk_final_location_index.py stats
python3 scripts/mail_desk_final_location_index.py lookup --mid 'msg-2026-001@example.org'
python3 scripts/mail_desk_final_location_index.py query --folder 'Projekte/EVOLVE' --limit 20
python3 scripts/mail_desk_final_location_index.py --input data/mail-desk/index-op.json
```

Die Datei darf NIEMALS direkt mit Texteditoren, `view_file` oder `grep`
geöffnet, gelesen oder manuell bearbeitet werden (Gefahr von
Context-Window-Overflows, unvollständigem Lesen und Syntax-Korruption).

Zusätzlich erlaubt für die Index-Location:

- Standardpfad: `data/mail-desk/final-location-index.json` (empfohlen)
- optionaler Env-Override via `.env`/Umgebung:
  - `MAIL_DESK_DATA_DIR=/abs/path/to/data/mail-desk`
  - oder `MAIL_DESK_FINAL_INDEX_PATH=/abs/path/to/final-location-index.json`

Hinweis: Message-IDs für Skript-Lookups immer in normalisierter Form **ohne
`< >`** übergeben; Message-IDs mit `$` dabei in **Single Quotes** setzen,
damit die Shell nichts expandiert.

JSON-Struktur und Feldregeln sind verbindlich in
[`log-schema.md`](log-schema.md) definiert (Abschnitt
`final-location-index.json`).

## Himalaya-Operationsmanifest (`himalaya-op.json`)

Der Himalaya-Client wird ausschließlich über ein JSON-Eingabemanifest
aufgerufen; direkte Ad-hoc-Subcommands mit wechselnden Argumenten sind im
operativen Agent-Workflow nicht zulässig:

```bash
python3 scripts/mail_desk_himalaya_client.py --input data/mail-desk/himalaya-op.json
```

Manifest-Shape:

```json
{
  "account": "BOKU-MARTIN",
  "operations": [
    { "action": "list_folders" },
    { "action": "list_envelopes", "folder": "INBOX", "page_size": 20 },
    { "action": "read", "folder": "INBOX", "envelope_id": "7195" },
    { "action": "inspect_attachments", "folder": "INBOX", "envelope_id": "7195", "expected_message_id": "message@example.org" },
    { "action": "copy", "source_folder": "INBOX", "target_folder": "Projekte/USAGE-NG", "envelope_id": "7195" },
    { "action": "move", "source_folder": "INBOX", "target_folder": "Projekte/USAGE-NG", "envelope_id": "7195" },
    { "action": "delete", "folder": "INBOX", "envelope_id": "7195" },
    { "action": "search", "query": "USAGE-NG" }
  ],
  "delete_input_on_success": true
}
```

`inspect_attachments` exportiert die Nachricht read-only als RFC-822-Quelle und
liefert ausschließlich validierte, an Account, Message-ID, Folder, Envelope-ID und
Part-Locator gebundene Kandidaten. Ein optionales `operations[].account` darf nur
den Top-Level-Account wiederholen; Abweichung, fehlende Message-ID oder ungültige
MIME-Metadaten sind fail-closed Fehler. Der Inventarschritt lädt noch keinen
Anhang in eine Arbeits- oder Cloud-Ablage herunter.

`attachment_fetch` ruft einen verifizierten MD-A1-Anhangskandidaten sicher und isoliert
in den temporären Quarantäneordner `data/mail-desk/attachments/<run-id>/` ab.
Erfordert zwingend eine gültige `approval_receipt` mit passendem `request_hash` gegen
den deterministischen `review_hash`. Erzwingt Preflight-Drift-Prüfung, Quoten (15 MB einzeln,
25 MB kumulativ, max. 5 Dateien), Re-Hashing, Re-Typing, aktiven Inhalts-Blocker,
idempotente Retrys und atomare Sibling-Temp-Promotion.

Clientzweck und Aufrufregel stehen im
[Himalaya-/IMAP-Adapter](backends/himalaya.md); Batch-Lebenszyklus in
[`batch-runner.md`](batch-runner.md).

## Pflicht-Output pro verarbeiteter Mail

Am Ende der Bearbeitung einer Mail immer einen kompakten Compliance-Block
ausgeben:

- `routing: ok|fail`
- `metadata: ok|fail`
- `final-index-script: ok|fail`
- `reference-source-id: ok|fail|n/a`

`reference-source-id` ist `n/a`, wenn keine Wissenspflege in
`memory/references/*` nötig war.

Ohne diesen Block gilt die Bearbeitung als unvollständig.

## Operative Abschluss-Checkliste (Tool-/Script-Schritte)

Vor Abschluss eines Mail-Schritts:

1. `action-log.jsonl` aktualisiert.
2. Falls Antwortbedarf: `replies-needed.jsonl` aktualisiert.
3. Falls Review-Fall: `pending-review.jsonl` aktualisiert.
4. Final-Index über `mail_desk_final_location_index.py` oder den Batch-Runner
   aktualisiert.
5. Final-Index über `mail_desk_final_location_index.py lookup` oder
   gezielten Batch-Check gegengeprüft.
6. Compliance-Block (`routing|metadata|final-index-script|reference-source-id`)
   ausgegeben; bei keiner Wissenspflege `reference-source-id: n/a`.

Fachliche Verifikations- und Quellenpflichtschritte stehen in der
Abschluss-Checkliste im [`SKILL.md`](../SKILL.md).
