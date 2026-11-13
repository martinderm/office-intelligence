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
  sent-synthesis-ledger.jsonl # Issue #88/#107: append-only Konsum-/Dedupe-Ledger der Synthese (Outcome/Provenienz)
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

**Validierung (fail-closed `handoff_invalid`, Datei bleibt unangetastet):** Dokumentkopf (`schema_version`
**exakt** int 1 — nicht `true`/`1.0`, `kind`, `generated_at` ISO, `max_items` int zwischen 1 und der kanonischen
Obergrenze 200), `count` **exakt** int, `count == len(items)`, `count ≤ max_items` **und** `count ≤ 200` (ein
dateieigenes höheres `max_items` erweitert die Grenze nie), je Item
exakt die dokumentierten Felder in den dokumentierten Typen und Längen, Identität (`norm_message_id` =
normalisierte `message_id`, nicht leer), Konsistenz (`thread_key` und `anchor_candidates` stimmen mit
`in_reply_to`/`references` überein, `mailbox` nicht leer), wohlgeformtes `anchored_to` (`null` oder genau
`vorgang_ref`, `matched_message_id`, `match_type`), keine Dubletten. Unkonsumierte Items eines anderen
Mailkontos als des gebundenen verweigern den Lauf (`mailbox_mismatch`). Nichts davon wird „blind konsumiert“.
Tief verschachteltes JSON, das `json.loads` mit `RecursionError` abbricht, ist ebenfalls `handoff_invalid`
(`json_too_deep`) und wird nicht umgeschrieben.

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
Synthese-Ergebnis), z. B. über `core.sent_handoff.record_synthesized(data_dir, items, vorgang_ref=..., evid_id=...)`,
`core.sent_handoff.record_no_evidence(data_dir, items, reason_code=...)` oder direktes Anhängen derselben Zeilen:

```json
{"schema_version": 1, "message_id": "sent@id", "thread_key": "root@id", "vorgang_ref": "V-7",
 "role": "primary|follow_up", "anchored_to": "mid@id|null", "recorded_at": "2026-11-09T09:30:00Z",
 "outcome": "synthesized|no_evidence", "source": "synthesis|legacy_import",
 "reason_code": "no_matching_vorgang|null", "evid_id": "EVID-2026-0042|null"}
```

**Schema (jede vollständige Zeile wird geprüft, sonst `ledger_invalid`, nichts wird übergeben/indexiert):**
Pflicht sind `schema_version` (**exakt** int 1, nicht `true`/`1.0`), `message_id` (**bereits normalisiert**: ohne
Klammern, Kleinschreibung), `role` (`primary`/`follow_up`) und `recorded_at` (ISO-8601). Optional sind
`thread_key`, `vorgang_ref` (opaker String), `anchored_to` (String oder `null`) sowie seit Issue #107
`outcome` (`synthesized`/`no_evidence`), `source` (`synthesis`/`legacy_import`), `reason_code`
(`null` oder begrenzter Token `^[a-z][a-z0-9_]{1,63}$`) und `evid_id` (`null` oder begrenzter exakter
opaker Token). Weitere Felder sind unzulässig. `follow_up` verlangt ein `anchored_to`, `primary` verbietet es.

- **Abwärtskompatibilität (Schema 1):** Eine Zeile **ohne** `outcome` gilt als `synthesized`, ohne `source` als
  `synthesis` (`ledger_outcome`/`ledger_source`). Alte Schema-1-Ledger bleiben unverändert gültig. Neue Writer
  schreiben `outcome` und `source` explizit; ein alter Reader lehnt die zusätzlichen Felder als `ledger_invalid`
  ab (fail-closed, keine stille Fehlinterpretation). Deshalb muss der installierte Bundle-Pin vor dem ersten
  erweiterten Schreibvorgang auf eine Version mit Issue #107 aktualisiert werden (**Versionsskew**).
- **`no_evidence` (Issue #107):** verlangt einen Begründungscode in `reason_code` und darf **kein** `vorgang_ref`,
  `anchored_to` oder `evid_id` tragen; `role` ist `primary`. Die Zeile **konsumiert** das Item (Ledger-Präsenz),
  erzeugt aber weder Vorgang noch EVID- noch Thread-Anker. Ein späterer Reply im selben Thread wird daher nicht
  an eine `no_evidence`-Zeile verankert. Kanonische Codes: `no_matching_vorgang`, `no_relevant_evidence`,
  `thread_unresolved`, `source_unavailable`, `headers_incomplete`, `conflicting_evidence`,
  `legacy_status_no_evidence` (letzterer nur für die ehrliche Herkunft „Legacy-Index sagte `no_evidence` ohne
  maschinenlesbaren Grund“). Kein Code ersetzt fehlende fachliche Evidenz. Das Schema erzwingt nur einen
  begrenzten Token; **welche** Codes konsumieren dürfen, ist eine offene Rollout-Entscheidung (No-Evidence-Politik) und
  keine Schema-Whitelist. Solange sie offen ist, bleiben unklare Fälle `pending`.
- **`synthesized`:** darf keinen `reason_code` tragen; `evid_id` ist optional (Altzeilen ohne EVID bleiben gültig).
  Importierte Synthese-Zeilen (`source: legacy_import`) verlangen dagegen **Vorgang + EVID-ID**; importierte
  `no_evidence`-Zeilen verlangen den Begründungscode. So erfindet der Import weder Vorgang noch Evidenz.
- **Konflikte:** Doppelte `message_id`s bleiben lesekompatibel (die erste gültige Zeile gilt für die
  Leseindizes). **Neue** Operationen (Append, Import, Recovery) prüfen dagegen *alle* Zeilen der Message-ID
  (`load_ledger()["all_by_message_id"]`) und stoppen bei abweichendem Outcome/Vorgang/EVID/Grund mit
  `ledger_conflict`; First-line-wins verdeckt keinen neuen Konflikt. Identische Dubletten sind ein idempotenter
  Skip. **Verschiedene** Message-IDs desselben Threads dürfen den Thread-Index auf eine Zeile mit vorhandenem
  `vorgang_ref` verfeinern; `no_evidence`-Zeilen sind davon ausgeschlossen.
- **Idempotenz alter Aufrufe:** Ein wiederholter `record_synthesized`-Aufruf darf `vorgang_ref`/`evid_id`
  weglassen; ein weggelassenes optionales Feld ist ein Wildcard gegen die bereits dauerhafte Zeile. Ein
  explizit übergebener, abweichender Wert (auch gegen einen fehlenden Altbestandswert) bleibt
  `ledger_conflict`. Ebenso muss eine `EVID-ID` zu genau einer Message-ID gehören; zwei verschiedene IDs mit
  derselben EVID-ID blockieren Import/Recovery (`evid_shared_conflict`).
- **Legacy-Import und Crash-Recovery:** Dry-run, Approval-Bindung (Target-Fingerprint + Inventar +
  Ledger-/Handoff-Bytes; keine absoluten Pfade im Report), Konfliktmatrix und das exakte
  `message_id` + `vorgang_ref` + `EVID-ID`-Tripel stehen in
  [`sent-ledger-migration.md`](sent-ledger-migration.md). Der Import schreibt `source: legacy_import`; eine
  Promotion-Recovery schreibt `source: synthesis` mit derselben Zeilenform. Beide Wege sind idempotent und
  schreiben keine Sonderdatei. Löst die Recovery einen Thread-Anker mit einem anderen nichtleeren Vorgang
  auf, stoppt `promotion_anchor_conflict` (kein `follow_up` auf einen fremden Vorgang); der Import blockiert
  eine EVID-ID, die bereits zu einer anderen Message-ID gehört. Konkurrierende Evidence mit derselben
  EVID-ID aber fehlender `message_id` stoppt als `promotion_recovery_incomplete`; die globale
  EVID-Besitzerprüfung läuft vor dem `already_consumed`-No-op.

- **Konsum-Status:** ein Handoff-Item gilt als konsumiert genau dann, wenn seine normalisierte `message_id`
  im Ledger steht (unabhängig von `outcome`). Das ist abgeleiteter Zustand: die Handoff-Datei wird dafür nicht
  umgeschrieben (`consumed_ignored` zählt konsumierte Reste); sie wird erst beim nächsten Handoff mit neuen
  Items ersetzt.
- **Dedupe:** Message-IDs im Ledger werden nie erneut übergeben (`already_synthesized`, trotzdem indexiert).
  Eine Antwort im selben Thread wird über `anchored_to` am selben `vorgang_ref` verankert (`role: follow_up`),
  nicht als neuer Vorgang; `no_evidence` ist davon ausgenommen.
- **Abgerissene Zeilen (Prozess-Absturz beim Append, auch mitten in einem UTF-8-Zeichen):** Dateien werden
  zeilenweise als Bytes gelesen und je Zeile dekodiert; ein nicht parsbarer, nicht mit Zeilenumbruch
  abgeschlossener **letzter** Rest wird ignoriert (`ledger_torn_tail`) und zerstört nichts. Vor dem
  nächsten Append wird er in das append-only Sidecar `<datei>.torn` (fsync) verschoben und abgeschnitten;
  ein vollständiger letzter Eintrag ohne Zeilenumbruch bleibt erhalten und bekommt nur den Umbruch. Dasselbe
  gilt für `sent-index.jsonl` (dort bleiben unparsbare Zeilen wie bisher stillschweigend ignoriert).
- **Consumer-Trigger:** `handoff.pending_count > 0` startet die Synthese. `handoff.state` `unknown`/`invalid`
  oder `pending_count: null` **blockiert** Synthese **und** den `inbox_zero`-No-op bis zur manuellen
  Ursachenbehebung; nur `pending_count == 0` bei `state` `none`/`written` ist ein bekannt leeres Handoff
  (`handoff_allows_noop`). Der Consumer (Synthese-Pipeline) liegt außerhalb des Bundles; ledger-Zeile **vor**
  dem Ergebnis zu schreiben verliert Evidence, **nach** dem Ergebnis ist at-least-once (Idempotenz beim Consumer).
  Nach einem Absturz zwischen dauerhafter Promotion und Ledger-Zeile zuerst
  `core.sent_ledger.recover_promotion(...)` mit dem vollständigen Tripel prüfen; kein blindes erneutes Promoten.

**Statische Alias-Fehlkonfiguration (`sent_index_alias_invalid`/`ledger_alias_invalid`/`handoff_alias_invalid`):**
In **allen** Einstiegen (Default, `dates`, Report, Singular/Plural und direkte Append-API) prüft der Sync den
Index, das Ledger, beide `<datei>.torn`-Sidecars sowie das Handoff-Dokument samt `.torn`-Sidecar statisch auf
Symlink-/Hardlink-Aliase,
**bevor** Index-/State-Inhalte genutzt, die Mailbox gelesen oder Handoff/Index/Coverage geschrieben wird. Ein
Alias ist eine Dateisystem-Fehlkonfiguration, kein transienter Fehler: beim vorgezogenen Preflight bleibt
`write_state` `none`; ein erst später (nur durch Race) erkannter Alias kann `state` melden. Handoff und Index
bleiben unangetastet. Fehler-Details heißen `alias_file`, `alias_kind`, `alias_label`; `handoff.file` bleibt der
Handoff-Pfad. `alias_kind: "unverifiable"` (`reason: alias_unverifiable`) bedeutet, dass `lstat` wegen
Permission/IO nicht prüfen konnte: ebenfalls fail-closed, `unknown`/`null`, nie „kein Alias“. Recovery ist
ausschließlich manuelle Reparatur/Quarantäne der betroffenen Datei(en), keine automatische Löschung; ein bloßer
Rerun ohne Korrektur scheitert identisch. Direktes `record_synthesized` prüft das Ledger samt Sidecar vor dem
Lesen/Append. Auch `load_ledger`, `load_pending_handoff`, `load_sent_index` und `read_handoff` prüfen ihre
Zieldateien statisch **vor** dem Lesen und fail-closed; ein fremdes Hardlink-Ledger/-Handoff kann so keine
Konsumentscheidung verfälschen. Der Classifier propagiert `sent_index_alias_invalid` ausdrücklich, statt den
fehlenden Lookup als „nicht beantwortet“ (`needs_reply: true`) umzudeuten. Der diagnostische Handoff-Peek
prüft Ledger und Handoff statisch und meldet `unknown`/`null` statt eines falschen `none`/`0`; statische
Aliasfehler überspringen den Peek ganz. Der Check ist keine Zusage gegen einen konkurrierenden Same-User-Mutator
(Issue #91).

### Konsum-Adapter (Pending-Sicht, Append, Batch-Konsum, Crash-Check, Issue #88)

Der Consumer konsumiert ausschließlich über die Sent-Ledger-CLI (Vertrag:
[`sent-ledger-migration.md`](sent-ledger-migration.md#6-konsum-adapter-für-den-consumer-issue-88)):

- **`pending` (read-only):** kanonische JSON-Envelope mit
  `data.kind: mail-desk.sent-ledger.pending-view`, `view_version: 1`,
  `handoff_state` (`none`/`pending`), `handoff_present`, `handoff_item_count`,
  `pending_count`, `consumed_ignored`, `consumed_outcomes`, `consumed_sources`,
  `counts` und `ledger`. `items[]` enthält nur `item_ref`/`thread_ref`/`waits_for`
  als gekürzte SHA-256-Referenzen (16 Hex-Zeichen; **keine** Anonymisierung, keine
  Unumkehrbarkeits-Zusage), `headers_complete`, `truncated_fields` (Feldnamen),
  `anchored` und `anchor` (`vorgang_ref`, gehashte `matched_ref`, `match_type`). Keine
  Betreffzeile, Adresse, Datum, Envelope-ID oder rohe/normalisierte Message-ID im
  Report. Die Sicht liefert bewusst keinen Envelope-/Message-ID-Locator; der
  Körperleseweg ist ein offener Gate (siehe `sent-ledger-migration.md` §6.5).
- **Konsumzählung:** `consumed_ignored` umfasst Handoff-Reste mit beliebiger
  Ledger-Zeile; `consumed_outcomes` (`synthesized`/`no_evidence`) und
  `consumed_sources` (`synthesis`/`legacy_import`) weisen die Zusammensetzung aus.
  Eine `no_evidence`-Zeile konsumiert, ist aber kein Thread-Anker; eine
  `legacy_import`-Zeile konsumiert und verankert nur, wenn sie `synthesized` ist.
- **Fehlende vs. leere Handoff-Datei:** beide melden `handoff_state: none` und
  `pending_count: 0`; `handoff_present`/`handoff_item_count` unterscheiden sie.
  `handoff_item_count > 0` bei `pending_count: 0` heißt ausschließlich konsumierte
  Reste (`consumed_ignored == handoff_item_count`), kein offenes Pending; das
  blockiert den Sync nicht. Eine fehlende Datei bleibt kein Leerbeweis.
- **`record` (append = Konsum):** genau ein Selektor (`--item-ref` aus der
  Pending-Sicht oder normalisierte/normalisierbare `--message-id`). Vor dem Append
  werden Lease und kanonische `<workspace>/data/mail-desk`-Bindung verifiziert
  (`record_outcome` selbst hat keinen Guard). `synthesized` verlangt eine exakte
  `vorgang_ref`, verbietet `reason_code`, erlaubt optional `evid_id` und prüft die
  frische Ankerreferenz gegen **alle** Ledger-Zeilen: fremder Vorgang
  (`anchor_conflict`), widersprüchliche Doppelzeilen (`anchor_ambiguous`),
  referenzloser Anker (`reference_anchor_unresolved`), unvollständige Header bei
  `synthesized` (`handoff_metadata_incomplete`; der Anker-Gate läuft nicht für
  `no_evidence`); eine EVID-ID gehört zu genau einer Message-ID
  (`evid_shared_conflict`). `no_evidence` verlangt einen schema-gültigen
  Begründungscode und verbietet jeden Anker. Passende bereits dauerhafte Identität ⇒
  `already_consumed` (No-op); abweichende Identität ⇒ `ledger_conflict`. Das Handoff
  bleibt byte-gleich. `norm_message_id` steht nur bei expliziter
  `--message-id`-Auswahl in der Antwort. `--account` ist optional; ohne ihn findet
  **kein** Account-Gate statt (Consumer-Pflicht, in der SOP immer gesetzt).
- **`consume` (Batch-Konsum):** liest ein consumer-eigenes Evaluationsdokument
  (`schema_version: 1`, `kind: mail-desk.sent-evaluation`, `items[]`) und appendet
  **nur** bereits ausgewertete Items (`synthesized` mit exakter `vorgang_ref` und
  optionaler `evid_id`, `no_evidence` mit schema-gültigem `reason_code`).
  `not_evaluated` und Items ohne Eintrag bleiben offen und werden nur als
  gehashte `item_ref` gemeldet. Selektoren sind `item_ref` aus `pending` oder die
  rohe `message_id`; doppelte Ziele, widersprüchliche dauerhafte Identitäten und
  eine mehrfach gebundene `evid_id` stoppen **vor** dem ersten Append. Jeder
  Append nutzt dieselben Gates wie `record` (Lease/Bindung, frischer Anker,
  offene Vorfahren, Account). `--dry-run` validiert und projiziert ohne Lease und
  ohne Write. Envelope: `data.kind: mail-desk.sent-ledger.consume`,
  `view_version: 1`, `dry_run`, `pending_before`/`pending_after`,
  `consumed`/`appended`, `already_consumed`, `not_evaluated`, `deferred`,
  `blocked`, `evaluation_entries`, `unmatched_evaluations`,
  `projected_pending_after` (Dry-Run), Ledger-/Handoff-Hashes und
  `handoff_unchanged`. Ein fachlicher Item-Konflikt endet als `consume_blocked`
  (Exit `2`), bereits geschriebene Zeilen bleiben dauerhaft; ein zweiter Lauf ist
  idempotent. Die Antwort enthält keine rohen Message-IDs. Das
  Evaluationsdokument ist Consumer-Transit, kann rohe `message_id`-Selektoren
  enthalten, ist personenbezogen und wird **nie gestaged**.
- **`crash-check` (read-only):** exakte Triade aus normalisierter `message_id` und
  byte-/case-exakten Token; `unique`/`absent` sind Exit `0`, `conflict` ist ein
  Fehler-Envelope mit Exit `2`. Doppelte/widersprüchliche Ledger-Zeilen, eine fremd
  gebundene EVID-ID (auch ohne Ziel-Datensatz), Thread-Anker-Konflikte und ein offener
  Vorfahre zählen als Konflikt; mit Handoff-Item werden dessen Header und alle
  Anker-Kandidaten geprüft (`handoff_metadata_incomplete`,
  `promotion_metadata_conflict`, `promotion_metadata_invalid`,
  `promotion_anchor_ambiguous`, `promotion_anchor_conflict`,
  `promotion_anchor_unresolved`, `ancestor_pending`). Evidence-Thread-Metadaten sind
  strikt typgeprüft (`references` nur Liste nichtleerer Strings, `in_reply_to`/
  `thread_key` nur Strings); keine `norm_id`-Coercion. `safe_to_append` ist nur bei
  eindeutiger Evidence, fehlender Ledger-Zeile und konsistentem Anker ohne offenen
  Vorfahren `true`; der Aufruf schreibt nie.

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
| Consumer: Promotion dauerhaft, Ledger-Zeile fehlt | Promotion + EVID existieren, Ledger nicht | zuerst `crash-check` (lesend, exakte Triade) mit der vollständigen Evidence; `unique` + `append_required` ⇒ `core.sent_ledger.recover_promotion(...)`; `absent`/`conflict` ⇒ stoppen |

Nicht durch das Bundle lösbar: parallele Schreiber ohne Lease, manuelles Löschen der Handoff-Datei
(verwirft unkonsumierte Items bewusst) und ein Consumer, der die Promotion-Recovery nicht aufruft. Die
Recovery macht das at-least-once-Fenster **prüfbar**, aber nicht exactly-once: Sie liest keine
Consumer-Dokumente, sondern bewertet ausschließlich die vom Aufrufer vollständig übergebene
Promotion-Evidence und das Ledger. Exaktes Tripel-Matching und die Lease-Pflicht stehen in
[`sent-ledger-migration.md`](sent-ledger-migration.md#3-promotion-recovery-nach-crash).

### Konsumvertrag und Überhang (`sync-sent`)

**Konsumvertrag (Consumer):** Items nicht blind aus Envelope oder Datei übernehmen, sondern über
die Sent-Ledger-CLI lesen: `pending` ist die read-only Sicht (Zähler, gekürzte Hash-Referenz
`item_ref` — keine Anonymisierung —,
`waits_for`; keine Rohinhalte), `consume` appendet in einer lease-gebundenen Runde genau die
bereits ausgewerteten Items (`synthesized`/`no_evidence`) aus einem Evaluationsdokument und lässt
`not_evaluated` offen, `record` appendet genau eine Konsumzeile (Lease- und
Zielbindung vor dem Write, frische Ankerprüfung, Idempotenz über die normalisierte Message-ID).
Der Consumer behält nur die inhaltliche Auswertung; die Ledger-Schreiblogik liegt im Bundle.
Die Python-API `core.sent_handoff.load_pending_handoff(data_dir)` bleibt der darunterliegende
Lesekern: validiert Handoff und Ledger vollständig (fail-closed), liefert nur unkonsumierte Items,
löst `anchored_to` frisch gegen das aktuelle Ledger auf und ordnet Vorfahren aus demselben Handoff
zuerst (`waits_for`). Schleife: erstes Item promoten → `consume`/`record` (löst den Anker erneut
auf, auch gegen frühere Items desselben Aufrufs) → `pending` erneut. Eine Antwort im selben Handoff
wird so erst nach der Promotion ihres Vorfahren verankert (`follow_up`, gleicher `vorgang_ref`).

**Schreibregel:** Der Handoff wird nur ersetzt, wenn der Lauf neue Items übergibt **und** nichts Unkonsumiertes
pending ist; Pending-Items werden nie überschrieben, erweitert oder ausgeräumt. Konsumierte Reste in der Datei
werden nur ignoriert (`consumed_ignored`). `handoff_pending` lässt die Datei byte-gleich.

**Überhang über 200:** Mehr als 200 neue Items werden in **200er-Batches** committet: die ältesten 200
(Reihenfolge nach Datum/Envelope-ID) werden übergeben und indexiert, der Rest bleibt unindexiert; der Lauf
meldet `truncated`, `deferred_count` und einen Recovery-Aufruf. Nach dem Ledger-Konsum wird der Watermark-Tag
erneut gescannt und der nächste Batch erzeugt — kein ID-Skip, kein Datenverlust. Ein vorhandenes Pending-Handoff
bleibt `handoff_pending` (fail-closed); Crash-Vertrag Handoff-vor-Index bleibt. **Recovery**:
`sync_sent_items_by_dates(..., include_next_day=False, max_pages=...)` nach dem Konsum.

**Fehlerzustände:** `handoff.state` ist in Erfolgs- und Fehler-Envelopes `none` (nichts offen), `written`,
`pending`, `invalid` (Handoff oder Ledger nicht interpretierbar) oder `unknown`; die Ursache steht in
`reason_code`. Statische Index-/Ledger-/Sidecar-Aliasfehler setzen bewusst `unknown` und `pending_count: null`,
weil der abgewiesene Ledger-/Handoff-Pfad nicht gelesen werden darf (kein falsches `none`/`0`).
Scheitert ein Datumsfenster, wird der ganze Lauf vor jedem Write verweigert (`sync_window_failed`,
`failed_dates`; nichts committet, damit das Wasserzeichen keine ältere Lücke überspringt). Ein fehlender
State bei bestehendem Index ist in allen Einstiegen fail-closed (`sent_initial_state_required`), unabhängig
von einem parsebaren `at`; ein verifiziertes `since` ist der exakte Start, nie der Watermark. Leere Tage
zählen; der Erstlauf ohne Index nutzt das eingefrorene Laufdatum sowie ein 60-Tage-Backfill bzw. `since`.
`count` ist
ein Item-Budget (kein Tageslimit); bei Erreichen stoppt der Lauf mit `truncated`/`coverage_*` und Recovery-Aufruf,
ohne über ungedeckte Tage vorzurücken. Seiten werden bis zur kurzen/leeren Seite paginiert (Dedupe per
Envelope-ID); nach `max_pages` weiterhin volle Seite ⇒ `sent_window_truncated`, kein Watermark-Advance.
`write_state` ist dann `none` (Ablehnung/Fehler vor jedem Write), `state` (nur
`sent-sync-state.json`/Coverage-Schranke wurde geschrieben, kein Handoff/Index) oder `unknown` (Schreibversuch
schlug fehl — auch ein Handoff-Write nach möglicher atomarer Publikation, z. B. Verzeichnis-`fsync`/`close` —
Plattenstand unverifiziert; dann `new_count: null` und `new_items_unavailable: true`, Wiederholung idempotent
per Message-ID). `partial` ist reserviert und wird nicht erzeugt. Fehler bedeuten also nie pauschal „null Writes“.

### `sent-sync-state.json` (persistente Coverage-Schranke)

`schema_version: 1`, `initial_start: "YYYY-MM-DD"` (Pflicht), `covered_through: "YYYY-MM-DD"|null`;
exakt diese drei Felder. Ein Altbestand mit `initial_start:null` und `covered_through` ist **nicht sicher
migrierbar** (auch der Index-Wasserzeichen beweist keine lückenlose Untergrenze; historische Punktabfragen
können Lücken lassen): fail-closed `unsafe_null_initial_migration`, nichts gelesen/geschrieben, Datei
unangetastet. Jeder originale neue State und jeder originale Legacy-Marker wird **vor** Merge/Overwrite auf
Future-Werte geprüft; ein älterer Merge maskiert keinen Future-Wert. Ein `covered_through` gleich dem
eingefrorenen Laufdatum wird vor dem Read auf gestern zurückgesetzt (heute nie final). Future
`initial_start`/`covered_through`, fehlende Felder, ungültige Kalenderdaten oder ein `since` nach dem
eingefrorenen Laufdatum werden **vor** jedem Read/Write abgewiesen
(`sent_initial_state_invalid`/`sync_invalid_since`/`sync_invalid_date`); ein `since` mit Zeitanteil ist ein
Formatfehler („YYYY-MM-DD“, nicht future). Nur ein autorisierter Operator repariert oder quarantäniert die
Datei, keine autonome Löschung/Überschreibung und kein `since`-Bypass eines malformed State; ein valider State ist
`{"schema_version":1,"initial_start":"YYYY-MM-DD","covered_through":null|"YYYY-MM-DD"}` mit konservativem,
verifiziertem älterem Start (keine erfundene Coverage). Fehlt bei bestehendem Sent-Index jede State-Datei,
fail-closed `sent_initial_state_required` in **allen** Einstiegen (Default, `dates`, Report, Singular/Plural)
und unabhängig von einem parsebaren `at`; eine bloße Quarantäne reicht nicht. Ein vom Operator verifiziertes
`since` ist der exakte Start (nie `min(Watermark, since)`, keine Punktdaten/CLI-Defaults) und erlaubt den
Resync; datierte APIs reichen `since` durch, angeforderte Tage davor sind fail-closed. `initial_start`
wird vor dem ersten Read persistiert (auch bei explizitem `since`); ein Fehlversuch behält ihn über den
Tageswechsel. `covered_through` ist der letzte **vollständig** committete Tag. Jede Rückspulung (explizite
Daten, älterer `since`, Legacy-Marker) wird **vor** Handoff/Index dauerhaft geschrieben; nach dem Append darf
sie nur über einen zusammenhängend angeforderten Prefix vorrücken, nie über eine Punktabfrage und nie über den
heutigen Tag. Das Source-Fenster (`window_date`) statt des Header-Datums bestimmt Grenzen/Recovery; fehlende
oder unparsebare Header verschieben kein Item und brechen einen vollständigen Sync nicht ab. Der Draft-Pfad
schluckt weiterhin **nicht-Alias**-Sync-Ausnahmen wie bisher, propagiert aber statische
`sent_index_alias_invalid`/`ledger_alias_invalid`/`handoff_alias_invalid`; Inbound-Semantik unverändert. Ein Crash vor dem finalen State lässt die
konservative Schranke stehen (sicherer Re-Read). Ungültig/unlesbar ⇒ `sent_initial_state_invalid` (nichts
gelesen oder geschrieben, Datei unangetastet). Das alte `sent-sync-initial-start.json` wird nur gelesen und
non-destruktiv in den State migriert (älterer `initial_start` bleibt erhalten, Schranke wird davor
zurückgesetzt).

**Recovery ohne State (Issue #88):** `sent_initial_state_required` wird nie über Punktdaten, Watermark oder
CLI-Default aufgelöst. Der geprüfte Weg nutzt bevorzugt den Wrapper, der die maschinelle
`verify_workspace_lock`-Prüfung **vor** jedem Read/Write ausführt:
`python3 "$MAIL_DESK_SKILL/scripts/mail_desk_batch_cli.py" sync-sent --count 150 --since YYYY-MM-DD
--account VERIFIED --workspace <consumer> --workspace-lease-id <lease>`.
Der Wrapper sendet dem Runner ein stdin-Manifest
`{"mode":"sync_sent","folder":"Sent Items","since":"YYYY-MM-DD","count":150,"account":"VERIFIED"}`;
der direkte `--sync-sent`-Modus wird dabei nicht gesetzt. Nur ohne Wrapper direkt: Runner mit
`--input data/mail-desk/batch-sync-sent.json --keep-input` (oder `--stdin`), Manifest mit **explizitem**
Account und derselben gehaltenen/verifizierten Lease; `--sync-sent` darf dabei nicht gesetzt sein, weil der
direkte Modus `--input`/`--stdin` ignoriert. `$MAIL_DESK_SKILL` ist das installierte Skill-Verzeichnis, nicht
der Workspace-Root. Keine autonome Löschung.

**`since`-Hinweis:** Ist `since` jünger als die bestehende Coverage, wird es nicht angewendet (kein
Coverage-Skip); Envelope/CLI melden additiv `since_requested`, `since_ignored: true`,
`since_ignored_reason: "coverage_starts_before_requested_since"` und `effective_coverage_start`. Älteres oder
gleiches `since` wird angewendet (`since_ignored: false`).

### Handoff-Batches über 200

Mehr als 200 neue Items werden deterministisch (älteste zuerst) in 200er-Batches committet: Handoff atomar,
dann Index, dann Coverage-Schranke. Der Suffix ab dem ersten deferred Item bleibt unindexiert; die Schranke
steht davor. Nach Ledger-Konsum wird derselbe Tag erneut gescannt und der nächste Batch erzeugt — alle IDs werden
eventual erreichbar, ohne Skip. `sync_sent_items_by_dates_report` zeigt `truncated`/`deferred_count`/`next_start`/
`recovery`; `sync_sent_items_by_dates` und `sync_sent_items_by_date` melden `handoff_deferred` statt still
abzuschneiden. Das dokumentierte Seitenende („envelope page out of bound“) ist eine offline nicht verifizierte
Adapter-Annahme; jede Abweichung bleibt fail-closed.

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
  "pid": 4242,
  "heartbeat_at": "2026-08-31T10:56:35Z",
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

- **`pid`/`heartbeat_at` (additiv, Schema bleibt 1):** `pid` ist der Owner-Prozess des Laufs. `status: "running"` ist nur mit lebendem `pid` gültig. `core.progress.read_runner_progress` liefert die liveness-geprüfte **read-only** Sicht (kein Lease nötig): Ein verwaister `running`-Zustand erscheint dort als `aborted` mit `progress.current_step: "interrupted"`, `error: "interrupted: the runner process is no longer running"`, `interrupted_at` und genullter ETA. Ein direktes Lesen der JSON-Datei ist **kein** Settlement; erst `settle_stale_runner_progress` (mutierend, im Kontext eines lease-gebundenen Laufs) oder der nächste Tracker schreibt den Zustand atomar fest. Der neue Tracker bewahrt die begrenzte Identität des unterbrochenen Laufs unter `interrupted_previous_run` (`run_id`, `mode`, `status: "interrupted"`, `completed_items`, `updated_at`).
- **Legacy-Zustand:** Fehlt `pid`, bleibt der Status unverändert (Eigentümerschaft nicht beweisbar); neue Läufe schreiben `pid` immer.
- **Liveness-Grenzen:** Die Prüfung ist best-effort über den Owner-Pid (Windows `OpenProcess`, POSIX `kill(pid, 0)`). Ein unbekannter oder nicht prüfbarer Zustand gilt konservativ als lebend; PID-Namespaces, Container/WSL-Grenzen und PID-Reuse können einen toten Lauf als lebend erscheinen lassen, nie umgekehrt. `heartbeat_at` ist informativ und kein Liveness-Kriterium; es wird nur bei Zustands-Writes aktualisiert.
- **Terminale Zustände:** `completed`/`failed`/`aborted`/`repaired` werden nie durch die Exit-/Reader-Settlement überschrieben. Beim regulären Prozessende beendet `abort_active_trackers` noch laufende Tracker (`error = "interrupted: batch run ended before completion"` bei einem abgebrochenen Runner); ein OS-Kill wird über den fehlenden Owner-Pid erkannt.

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
