# mail-desk Log Schema

All files are JSONL under `data/mail-desk/`. Keep entries small. Active files contain only open/current items; completed items move to `data/mail-desk/archive/YYYY-Www/`.

## Durable mail identity

Envelope-ID is not durable. It may change after copy/move, especially on GroupWise.

Rules:

- Never use Envelope-ID as primary key, close key, idempotency key, or reference key.
- Store it only as `envelope_id` for operational traceability.
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
  "message_id": "69e789bb020000f1000d1629@mail.example.org",
  "key_type": "message_id",
  "envelope_id": "8871",
  "subject": "Wtrlt: ...",
  "from": "Sender Name <...>",
  "decision": {
    "kind": "project|topic|archive|ignore",
    "id": "aixlll",
    "confidence": "high|medium|low",
    "needs_reply": true
  },
  "action": {
    "type": "copy_as_move|move|copy|none",
    "target_folder": "Themen/AIxLLL/_Needs-Reply"
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
  "envelope_id": "8871",
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
  "envelope_id": "8871",
  "subject": "...",
  "from": "...",
  "folder": "Themen/AIxLLL/_Needs-Reply",
  "reply_status": "needed|drafted|sent|dismissed",
  "reply_note": "What needs to be answered."
}
```

## Closing an item

Do not add a separate closed row next to an open row for the same mail. Update the original item and then archive it.

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

- `message_id` schnell auf finalen Ordner mappen
- zuletzt gesehene Envelope-ID für den Zielordner behalten
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
      "final_folder": "Projekte/XYZ",
      "envelope_id": "4711",
      "updated_at": "2026-04-27T11:14:00Z",
      "in_reply_to": "<parent@host>",
      "references": ["<root@host>", "<parent@host>"]
    }
  }
}
```

Regeln:

- Keine Mailinhalte im Index speichern.
- Envelope-ID nur zusammen mit dem `final_folder` interpretieren.
- Schlüssel pro Eintrag ist die normalisierte `message_id`.
- Bei fehlender Message-ID optional analog über `message_key` arbeiten.

CLI-Helfer (`skills/mail-desk/scripts/`):

- `final_index_lookup.py --message-id ...`
- `final_index_upsert.py --mode upsert-final --stdin`
  - Pflichtfelder im Payload: `message_id`, `final_folder`, `envelope_id`
- `final_index_upsert.py --mode patch --stdin`
  - Pflichtfeld im Payload: `message_id`
  - patcht nur bestehende Einträge

## Idempotency

Before handling a mail, search active and archived JSONL files for the normalized `message_id` or fallback `message_key`:

- active files: `action-log.jsonl`, `pending-review.jsonl`, `replies-needed.jsonl`
- archive files under `archive/YYYY-Www/`

If already present, do not process again unless the user explicitly asks.
