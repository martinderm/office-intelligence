# mail-desk Log Schema

All files are JSONL under `data/mail-desk/`. Keep entries small. Active files contain only open/current items; completed items move to `data/mail-desk/archive/YYYY-Www/`.

## Durable mail identity

Envelope-ID is not durable. It may change after copy/move, especially on GroupWise.

Rules:

- Never use Envelope-ID as primary key, close key, idempotency key, or reference key.
- Store it only as `last_seen_envelope_id` for operational traceability.
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
  "last_seen_envelope_id": "8871",
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
  "last_seen_envelope_id": "8871",
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
  "last_seen_envelope_id": "8871",
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

## Idempotency

Before handling a mail, search active and archived JSONL files for the normalized `message_id` or fallback `message_key`:

- active files: `action-log.jsonl`, `pending-review.jsonl`, `replies-needed.jsonl`
- archive files under `archive/YYYY-Www/`

If already present, do not process again unless the user explicitly asks.
