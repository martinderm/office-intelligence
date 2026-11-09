# mail-desk Log Schema

The log files under `data/mail-desk/` are JSONL; `final-location-index.json` and `runner-progress.json` are JSON files. Keep entries small. Active files contain only open/current items; completed items move to `data/mail-desk/archive/YYYY-Www/`.

### Standardpfade

```text
data/mail-desk/
  action-log.jsonl          # nur laufende/heutige Arbeitsnotizen, nicht als Dauerablage missbrauchen
  pending-review.jsonl      # nur offene Review-Fälle
  replies-needed.jsonl      # nur offene Antwortfälle
  sent-index.jsonl          # leichter Index gesendeter Antworten (Header-/Routingmetadaten)
  new-sent-items.json       # Issue #88: begrenzter Transit-Handoff neuer Sent Items (operative Evidence)
  sent-synthesis-ledger.jsonl # Issue #88: append-only Konsum-/Dedupe-Ledger der Synthese
  final-location-index.json # schneller O(1)-Lookup für finale Backend-Location (nur script-basiert)
  runner-progress.json      # atomare Fortschrittsdatei bei Batch-Läufen
  injection-quarantine/     # Issue #62: operative Evidence, kein Referenzwissen
    index.jsonl             # append-only Quarantäne-Index
    <id>/record.json        # Schema 1, write-once
    <id>/fenced.txt         # write-once, gefenceter Mail-Inhalt
    <id>/observations.jsonl # append-only Beobachtungen je Wiederholung
  archive/
    YYYY-Www/
      action-log.jsonl
      pending-review.jsonl
      replies-needed.jsonl
```

Keine großen Mailarchive standardmäßig anlegen. Bei Bedarf kurze Auszüge oder Pfade auf Anhänge notieren, aber nicht die komplette Mail duplizieren.

### Schreibregeln & Kontextsparen

- Schreibzugriffe auf gemeinsame `data/mail-desk/`-Dateien immer **seriell**, nie parallel ausführen; das gilt besonders für `.jsonl`-Logs und `final-location-index.json`.
- Für Schema-, Dedupe- oder Formatprüfungen nur kleine, gezielte Ausschnitte lesen.
- Regeldateien innerhalb derselben Session nicht pro Mail erneut voll laden, wenn sich der Falltyp nicht wesentlich geändert hat.
- Nach erster belastbarer Auswertung bevorzugt mit `message_id` plus Arbeitsverdichtung weiterarbeiten statt mit mehrfach wiederholtem Rohmaterial.
- Script-Zugriffe auf `final-location-index.json` sind in [`cli-operations.md`](cli-operations.md) geregelt; die Datei darf niemals manuell editiert werden.

## Durable mail identity

Backend locators are not durable cross-backend identities. An Envelope-ID may change after copy/move, especially on some IMAP backends; a Gmail message or thread ID is specific to Gmail.

Rules:

- Never use a backend locator as primary key, close key, idempotency key, or reference key.
- Store a Himalaya locator only as `envelope_id`; store Gmail locators as `gmail_message_id` and optional `gmail_thread_id`.
- Durable keys are `message_id` or fallback `message_key` with `key_type="fallback_hash"`.
- When closing or updating an item, match by `message_id`/`message_key`, not Envelope-ID.

If no Message-ID exists, create a deterministic fallback key, e.g. hash of `from|date|subject|body-preview`, and set:

```json
{
  "message_key": "sha256:...",
  "key_type": "fallback_hash"
}
```

## action-log.jsonl

Use for current handling notes. Completed handling records should be moved to the weekly archive after the action is done.

```json
{
  "schema_version": 1,
  "at": "2026-04-24T13:00:00Z",
  "mailbox": "MAIN-MAILBOX",
  "backend": "himalaya|gmail",
  "message_id": "69e789bb020000f1000d1629@mail.example.org",
  "key_type": "message_id",
  "envelope_id": "8871",
  "subject": "Wtrlt: ...",
  "from": "Sender Name <sender@example.org>",
  "decision": {
    "kind": "project|topic|archive|ignore",
    "id": "aixlll",
    "confidence": "high|medium|low",
    "needs_reply": true
  },
  "action": {
    "type": "copy_as_move|move|copy|label|archive|none",
    "target": "Themen/AIxLLL/_Needs-Reply"
  },
  "notes": "Short operational note."
}
```

## pending-review.jsonl

Use when the agent should not decide alone. Active file contains only unresolved review items.

```json
{
  "schema_version": 1,
  "at": "2026-04-24T13:00:00Z",
  "mailbox": "MAIN-MAILBOX",
  "message_id": "...",
  "key_type": "message_id",
  "backend_locator": "8871|gmail-message-id",
  "subject": "...",
  "from": "...",
  "reason": "ambiguous_target|missing_folder|possible_catalog_gap|unclear_reply_need|other",
  "suggested_options": [
    "Themen/AIxLLL/_Needs-Reply",
    "Projekte/EVOLVE/_Needs-Reply"
  ],
  "notes": "Why review is needed."
}
```

## replies-needed.jsonl

Optional helper index for reply work. Use only if `needs_reply=true`. Active file contains only open reply items.

```json
{
  "schema_version": 1,
  "at": "2026-04-24T13:00:00Z",
  "mailbox": "MAIN-MAILBOX",
  "message_id": "...",
  "key_type": "message_id",
  "backend_locator": "8871|gmail-message-id",
  "subject": "...",
  "from": "...",
  "folder": "Themen/AIxLLL/_Needs-Reply",
  "reply_status": "needed|drafted|sent|dismissed",
  "reply_note": "What needs to be answered."
}
```

## sent-index.jsonl

Leichter Header-/Routing-Index für gesendete Mails, um bei alten `needs_reply`-Fällen schnell zu prüfen, ob bereits geantwortet wurde.

Path:

```text
data/mail-desk/sent-index.jsonl
```

Minimalfelder:

```json
{
  "schema_version": 1,
  "at": "2026-04-24T13:00:00Z",
  "updated_at": "2026-04-24T13:05:00Z",
  "mailbox": "MAIN-MAILBOX",
  "message_id": "<sent@id>",
  "in_reply_to": "<source@id>",
  "subject": "Re: ...",
  "from": "user@example.org",
  "to": ["partner@example.org"],
  "folder": "Sent Items",
  "backend_locator": "4711|gmail-message-id",
  "project_id": "meshe",
  "topic_id": "netzwerke",
  "source_message_id": "<inbox@id>",
  "confidence": "high"
}
```

Optionale Zusatzfelder:

- `references` (array)
- `thread_key`
- `keywords_matched` (array)
- `has_attachments` (boolean)
- `note` (kurz)

Regeln:

- Keine Mailinhalte speichern (nur Header-/Routingmetadaten).
- `project_id`/`topic_id` kann einzeln oder gemeinsam gesetzt sein.
- `source_message_id` setzen, wenn die Zuordnung zur beantworteten Inbox-Mail belastbar ist.
- `confidence` setzen, wenn Zuordnung heuristisch erfolgte.
- `updated_at` dokumentiert die letzte Datenaktualisierung des Eintrags.
- `backend_locator` ist die zuletzt verifizierte, backend-spezifische Kennung im Sent-Bereich.
- Der Eintragsschlüsselsatz von `sync-sent` ist unverändert; `cc`, `thread_key` und Anker stehen nur im Handoff (nächster Abschnitt).

## new-sent-items.json (Synthese-Handoff, Issue #88)

Operative Evidence (Transit), kein Referenzwissen. Die Datei ist die bounded, atomar (`tmp` + `fsync` +
`replace`, danach best-effort `fsync` des Verzeichnisses) geschriebene Liste der neu indexierten, noch **nicht
konsumierten** Sent Items. Sie wird nur geschrieben, wenn ein Lauf mindestens ein neues, noch nicht
synthetisiertes Item übergibt **und** nichts Unkonsumiertes pending ist, nie bei 0 neuen Items.

```json
{
  "schema_version": 1,
  "kind": "mail-desk.new-sent-items",
  "generated_at": "2026-11-09T09:00:00Z",
  "max_items": 200,
  "count": 1,
  "items": [{
    "message_id": "<sent@id>", "norm_message_id": "sent@id",
    "date": "Mon, 09 Nov 2026 09:55:00 +0100",
    "subject": "Re: ...", "from": "Owner <owner@example.org>",
    "to": ["Partner <partner@example.org>"], "cc": ["Doe, John <john@example.org>"],
    "in_reply_to": "<mid@id>", "references": ["<root@id>", "<mid@id>"],
    "envelope_id": "4711", "folder": "Sent Items", "mailbox": "MAIN-MAILBOX",
    "thread_key": "root@id", "anchor_candidates": ["mid@id", "root@id"],
    "anchored_to": {"vorgang_ref": "V-7", "matched_message_id": "mid@id", "match_type": "in_reply_to"},
    "headers_complete": true
  }]
}
```

**Validierung (fail-closed `handoff_invalid`, Datei bleibt unangetastet):** Dokumentkopf (`schema_version`,
`kind`, `generated_at` ISO, `max_items` zwischen 1 und der kanonischen Obergrenze 200), `count == len(items)`,
`count ≤ max_items` **und** `count ≤ 200` (ein dateieigenes höheres `max_items` erweitert die Grenze nie), je Item
exakt die dokumentierten Felder in den dokumentierten Typen und Längen, Identität (`norm_message_id` =
normalisierte `message_id`, nicht leer), Konsistenz (`thread_key` und `anchor_candidates` stimmen mit
`in_reply_to`/`references` überein, `mailbox` nicht leer), wohlgeformtes `anchored_to` (`null` oder genau
`vorgang_ref`, `matched_message_id`, `match_type`), keine Dubletten. Unkonsumierte Items eines anderen
Mailkontos als des gebundenen verweigern den Lauf (`mailbox_mismatch`). Nichts davon wird „blind konsumiert“.

**Grenzen (nie still gekappt, beim Schreiben und beim Lesen durchgesetzt):** höchstens 200 Items;
`references` höchstens 50 (Thread-Wurzel plus die 49 nächsten Vorfahren), `to`/`cc` höchstens 100 Adressen,
`subject` höchstens 1000 Zeichen. Überschreitet eine gelesene Datei eine dieser Grenzen, ist sie
`handoff_invalid` (`subject_too_long`/`references_too_long`/`to_too_many`/`cc_too_many`) und wird weder
konsumiert noch umgeschrieben. Wurde ein Feld beim Schreiben begrenzt, nennt `truncated_fields` es; der
Sent-Index behält den vollen Wert.

**Thread-Schlüssel:** `thread_key` = erste `References`-Message-ID (Thread-Wurzel), sonst `In-Reply-To`,
sonst die eigene Message-ID (jeweils normalisiert). `anchor_candidates` = `In-Reply-To`, dann `References`
rückwärts (nächster Vorfahr zuerst), ohne die eigene ID. `anchored_to` ist `null` oder der erste Treffer im
Ledger in der Reihenfolge `in_reply_to` → `references` → `thread_key`; `vorgang_ref` kann `null` sein.
**`anchored_to` im Handoff ist ein Schnappschuss zum Schreibzeitpunkt**; maßgeblich ist die frische Auflösung
(`load_pending_handoff`, `record_synthesized`).

### sent-synthesis-ledger.jsonl (Konsum- und Dedupe-Ledger)

Append-only, ein Objekt je Zeile, Schreiber ist der **Consumer** (nach dauerhaft gesichertem
Synthese-Ergebnis), z. B. über `core.sent_handoff.record_synthesized(data_dir, items, vorgang_ref=...)`
oder direktes Anhängen derselben Zeilen:

```json
{"schema_version": 1, "message_id": "sent@id", "thread_key": "root@id", "vorgang_ref": "V-7",
 "role": "primary|follow_up", "anchored_to": "mid@id|null", "recorded_at": "2026-11-09T09:30:00Z"}
```

**Schema (jede vollständige Zeile wird geprüft, sonst `ledger_invalid`, nichts wird übergeben/indexiert):**
Pflicht sind `schema_version` (= 1), `message_id` (**bereits normalisiert**: ohne Klammern, Kleinschreibung),
`role` (`primary`/`follow_up`) und `recorded_at` (ISO-8601); optional `thread_key`, `vorgang_ref`, `anchored_to`
(String oder `null`); weitere Felder sind unzulässig. `follow_up` verlangt ein `anchored_to`, `primary`
verbietet es. Doppelte `message_id` sind zulässig (die erste gültige Zeile gilt und ist für **alle** Indizes
maßgeblich: eine spätere Dublette mit anderem `thread_key`/`vorgang_ref` führt weder einen Thread-Anker noch
eine Vorgangs-Zuordnung ein). **Verschiedene** Message-IDs desselben Threads dürfen den Thread-Index auf eine
Zeile mit vorhandenem `vorgang_ref` verfeinern (erste Zeile je Message-ID bleibt unangetastet).

- **Konsum-Status:** ein Handoff-Item gilt als konsumiert genau dann, wenn seine normalisierte `message_id`
  im Ledger steht. Das ist abgeleiteter Zustand: die Handoff-Datei wird dafür nicht umgeschrieben
  (`consumed_ignored` zählt konsumierte Reste); sie wird erst beim nächsten Handoff mit neuen Items ersetzt.
- **Dedupe:** Message-IDs im Ledger werden nie erneut übergeben (`already_synthesized`, trotzdem
  indexiert). Eine Antwort im selben Thread wird über `anchored_to` am selben `vorgang_ref` verankert
  (`role: follow_up`), nicht als neuer Vorgang.
- **Abgerissene Zeilen (Prozess-Absturz beim Append, auch mitten in einem UTF-8-Zeichen):** Dateien werden
  zeilenweise als Bytes gelesen und je Zeile dekodiert; ein nicht parsbarer, nicht mit Zeilenumbruch
  abgeschlossener **letzter** Rest wird ignoriert (`ledger_torn_tail`) und zerstört nichts. Vor dem
  nächsten Append wird er in das append-only Sidecar `<datei>.torn` (fsync) verschoben und abgeschnitten;
  ein vollständiger letzter Eintrag ohne Zeilenumbruch bleibt erhalten und bekommt nur den Umbruch. Dasselbe
  gilt für `sent-index.jsonl` (dort bleiben unparsbare Zeilen wie bisher stillschweigend ignoriert).
- Der Consumer (Synthese-Pipeline) liegt außerhalb des Bundles; ledger-Zeile **vor** dem Ergebnis zu
  schreiben verliert Evidence, **nach** dem Ergebnis ist at-least-once (Idempotenz liegt beim Consumer).

### Absturzfenster (`sync-sent`)

Der Vertrag gilt für **Prozess-Absturz/-Abbruch**. Dateiinhalte werden vor dem Ersetzen/Anhängen mit `fsync`
gesichert und das Verzeichnis wird nach dem Handoff-Replace best-effort synchronisiert (nicht überall
möglich, z. B. Windows). Plötzlicher Stromausfall ist darüber hinaus nicht garantiert; die Reihenfolge
Handoff vor Index ist dann plattformabhängig.

| Absturz | Zustand | Wiederholungslauf |
|---|---|---|
| vor dem Handoff-Write | nichts geschrieben | liest alles neu |
| nach Handoff, vor/während Index-Append | Items im Handoff, nicht (voll) im Index; ein Append-Fehler meldet `write_state: unknown` | erkennt dieselben Items wieder, Merge per Message-ID, kein Duplikat |
| nach Index, vor Konsum | Items im Handoff **und** im Index | `new_count: 0`, `handoff.state: pending`; neue Items → `handoff_pending` bis zum Konsum |
| während Konsum | Ledger enthält Teilmenge | nur die ledgerten Items gelten als konsumiert |

Nicht durch das Bundle lösbar: Absturz des Consumers zwischen Synthese-Ergebnis und Ledger-Zeile
(Doppel-Promotion möglich), parallele Schreiber ohne Lease und manuelles Löschen der Handoff-Datei (verwirft
unkonsumierte Items bewusst).

### Konsumvertrag und Überhang (`sync-sent`)

**Konsumvertrag (Consumer):** Items nicht blind aus Envelope oder Datei übernehmen, sondern über
`core.sent_handoff.load_pending_handoff(data_dir)` lesen: validiert Handoff und Ledger vollständig
(fail-closed), liefert nur unkonsumierte Items, löst `anchored_to` frisch gegen das aktuelle Ledger auf und
ordnet Vorfahren aus demselben Handoff zuerst (`waits_for`). Schleife: erstes Item promoten →
`record_synthesized` (löst den Anker erneut auf, auch gegen frühere Items desselben Aufrufs) →
`load_pending_handoff` erneut. Eine Antwort im selben Handoff wird so erst nach der Promotion ihres Vorfahren
verankert (`follow_up`, gleicher `vorgang_ref`).

**Schreibregel:** Der Handoff wird nur ersetzt, wenn der Lauf neue Items übergibt **und** nichts Unkonsumiertes
pending ist; Pending-Items werden nie überschrieben, erweitert oder ausgeräumt. Konsumierte Reste in der Datei
werden nur ignoriert (`consumed_ignored`). `handoff_pending` lässt die Datei byte-gleich.

**Überhang über 200:** Der Lauf wird vollständig verweigert (`handoff_overflow`) **vor jedem Write**: nichts
wird übergeben, zurückgestellt oder indexiert. Details: `candidate_count`, `max_items`,
`candidates_per_day` (nicht parsebare Daten als `unparsed`). **Recovery ausschließlich über die Python-API**
`core.sent_indexer.sync_sent_items(dates=[...])` / `sync_sent_items_by_dates(..., include_next_day=False)`:
älteste Tage zuerst, dazwischen konsumieren. `--sync-sent N`/`count` ist **keine** Recovery (Wasserzeichen-Fenster
endet bei heute, überspringt ältere Tage; keine Datums-CLI). Ein Überhang am selben Tag ist nicht per
Datumsbereich teilbar (harte Betriebsgrenze). Nach der Verweigerung wurde nichts geschrieben; ein reiner
Default-/CLI-Wiederanlauf holt die alten Tage nicht nach, die Items bleiben dann außerhalb von Index und Handoff
(keine stille Lücke durch Writes, aber der Betreiber muss die Python-API nutzen).

**Fehlerzustände:** `handoff.state` ist in Erfolgs- und Fehler-Envelopes `none` (nichts offen), `written`,
`pending` oder `invalid` (Handoff oder Ledger nicht interpretierbar); die Ursache steht in `reason_code`.
`write_state` trennt in Fehler-Envelopes `none` (Ablehnung/Fehler vor jedem Write), `partial` (bestätigte
Teilresultate der erfolgreichen Datumsfenster, Zähler exakt) und `unknown` (ein Schreibversuch schlug fehl —
auch ein Handoff-Write nach möglicher atomarer Publikation, z. B. Verzeichnis-`fsync`/`close` — Plattenstand
unverifiziert; dann `new_count: null` und `new_items_unavailable: true`, ein Wiederholungslauf merged idempotent
per Message-ID). Fehler bedeuten also nie pauschal „null Writes“.

## Closing an item

Do not add a separate closed row next to an open row for the same mail. Update the original item and then archive it.

### Vorgehen beim Schließen / Archivieren

1. Aktive Datei lesen (`pending-review.jsonl` oder `replies-needed.jsonl`).
2. Passenden ursprünglichen Eintrag per `message_id` suchen; falls keine Message-ID vorhanden ist, per stabilem `message_key` mit `key_type="fallback_hash"`. Nie per Backend-Locator schließen.
3. Diesen Eintrag mit Status/Resolution ergänzen (siehe Pflichtfelder unten).
4. Aktualisierten erledigten Eintrag aus der aktiven Datei entfernen.
5. Erledigten Eintrag in `data/mail-desk/archive/YYYY-Www/<dateiname>.jsonl` anhängen.
6. Aktive Datei ohne den erledigten Eintrag zurückschreiben.

*Hinweis:* Dieser Ablauf wird automatisiert durch Aufruf von `python3 scripts/mail_desk_resolve_case.py` ausgeführt (Details siehe [`cli-operations.md`](cli-operations.md)).

Aktive Dateien enthalten nur offene bzw. noch relevante Einträge. Alles Erledigte wandert ins Wochenarchiv nach ISO-Kalenderwoche. Keine Doppelstruktur wie `open` + später separate `closed`-Zeile für dieselbe Mail.

Beim Schließen oder Archivieren nur den für den konkreten Fall nötigen Eintrag und Kontext lesen; keine breiten aktiven Dateistände mitschleppen, wenn ein gezielter Lookup reicht.

Required close fields:

```json
{
  "status": "closed|resolved|dismissed|superseded",
  "closed_at": "2026-04-24T13:00:00Z",
  "resolution": "Why this item is done.",
  "resolved_by_message_id": "optional",
  "resolved_by_key": "optional"
}
```

Archive path uses ISO week:

```text
data/mail-desk/archive/YYYY-Www/<source-file>.jsonl
```

Example:

```text
data/mail-desk/archive/2026-W17/replies-needed.jsonl
```

## final-location-index.json

Optional, aber empfohlen für schnelle Quellauflösung aus Projekt-/Topic-Referenzen.

Path:

```text
data/mail-desk/final-location-index.json
```

Zweck:

- `message_id` schnell auf finale Backend-Location mappen
- zuletzt gesehenen Backend-Locator für die finale Location behalten
- optional Thread-Bezug ohne Mailinhalt über `in_reply_to` und `references`

Minimalstruktur:

```json
{
  "schema_version": 1,
  "updated_at": "2026-04-27T11:14:00Z",
  "items": {
    "normalized-message-id": {
      "message_id": "<id@host>",
      "mailbox": "MAIN-MAILBOX",
      "backend": "gmail",
      "final_folder": "Projekte/XYZ",
      "final_label": "Projekte/XYZ",
      "gmail_message_id": "gmail-message-id",
      "gmail_thread_id": "gmail-thread-id",
      "updated_at": "2026-04-27T11:14:00Z",
      "in_reply_to": "<parent@host>",
      "references": ["<root@host>", "<parent@host>"]
    }
  }
}
```

Regeln:

- Keine Mailinhalte im Index speichern.
- Backend-spezifische Locator-Felder nur zusammen mit der jeweiligen finalen Location interpretieren.
- Schlüssel pro Eintrag ist die normalisierte `message_id`.
- Bei fehlender Message-ID optional analog über `message_key` arbeiten.

CLI-Helfer (`skills/mail-desk/scripts/`):

- `mail_desk_final_location_index.py stats`
- `mail_desk_final_location_index.py lookup --mid '<message_id>'`
- `mail_desk_final_location_index.py query --folder '<folder>' --limit 50`
- `mail_desk_final_location_index.py --input data/mail-desk/index-op.json`

## runner-progress.json

Live-Fortschritts- und ETA-Statusdatei unter `data/mail-desk/runner-progress.json`. Wird vom Batch-Runner (`mail_desk_batch_runner.py`) bei jedem Bearbeitungsschritt atomar aktualisiert und dient der Echtzeit-Überwachung und adaptiven Timer-Steuerung (`30s -> 75% ETA`).

```json
{
  "schema_version": 1,
  "run_id": "execute_20260831_125605",
  "mode": "draft|execute|pipeline|inspect",
  "status": "running|completed|failed|aborted|repaired",
  "started_at": "2026-08-31T12:56:05",
  "updated_at": "2026-08-31T10:56:35Z",
  "progress": {
    "total_items": 20,
    "completed_items": 11,
    "percent": 55.0,
    "current_step": "routed to Projekte/In Ausarbeitung/ATAEL",
    "current_envelope_id": "7081",
    "current_subject": "Antw: Re: ATAEL"
  },
  "timing": {
    "elapsed_seconds": 183.2,
    "avg_seconds_per_item": 16.6,
    "estimated_remaining_seconds": 149.0,
    "eta_timestamp": "2026-08-31 13:02:18"
  },
  "error": null
}
```

## injection-quarantine/ (Issue #62)

Operative Evidence für Mail, die der lokale Injection-Barrier als `medium` oder
höher geflaggt hat. Der Store liegt ausschließlich unter
`data/mail-desk/injection-quarantine/` (Data Zone `data/`, nie
`memory/references/`) und ist kein Task-/Wissensartefakt.

```text
injection-quarantine/
  index.jsonl                       # append-only, eine Zeile je Beobachtung
  <quarantine_id>/
    record.json                     # Schema 1, write-once
    fenced.txt                      # write-once, <untrusted_mail_content>-Fence
    observations.jsonl              # append-only, eine Zeile je Wiederholung
```

- `quarantine_id`: 16 Hex, deterministisch aus `oi62:<policy_version>:<message_key>`.
  Derselbe normalisierte `message_id`/Fallback-Key ergibt dieselbe ID.
- `record.json` wird nie überschrieben. Wiederholungen erhöhen nur
  `observations.jsonl` und `index.jsonl`; `record.json` bewahrt die erste
  Beobachtung. `fenced.txt` wird ebenfalls nur einmal geschrieben.
- `record.json` (Schema 1) trägt `quarantine_id`, `status: "quarantined"`,
  `mode`, begrenzte Identität (`message_key`, `message_id`, `envelope_id`,
  `folder`, `subject`, `from`, `date`), `content` (SHA-256/Bytes,
  Fence-Version, Fenced-Hash, Transformationen, `scan_truncated`), `findings`
  (bounded Rule-Samples), `rule_ids`, `max_severity`, `policy` und
  `first_observed_at`. Kein `preview`-Feld, keine Roh-MIME, keine
  Zugangsdaten, keine absoluten Pfade.
- `observations.jsonl` und `index.jsonl` sind append-only und werden nur unter
  der bestehenden seriellen `data/mail-desk/`-Schreibregel ergänzt.
- `fenced.txt` ist gefencter operativer Inhalt und darf nie in konsolidierte
  Referenz- oder Wissensdateien übernommen werden. Die
  `<untrusted_mail_content fence_version="1" sha256="…">`-Delimiter sind fest;
  Spoofs werden zu `[ESCAPED_UNTRUSTED_TAG]`.

## Idempotency

Before handling a mail, search active and archived JSONL files for the normalized `message_id` or fallback `message_key`:

- active files: `action-log.jsonl`, `pending-review.jsonl`, `replies-needed.jsonl`
- archive files under `archive/YYYY-Www/`

If already present, do not process again unless the user explicitly asks.
