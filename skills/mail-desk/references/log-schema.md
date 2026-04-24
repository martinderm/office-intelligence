# mail-desk Log Schema

All files are JSONL under `data/mail-desk/`. Append one JSON object per line. Keep entries small.

## action-log.jsonl

Use after a mail was confidently handled.

```json
{
  "schema_version": 1,
  "at": "2026-04-24T13:00:00Z",
  "mailbox": "MAIN-MAILBOX",
  "envelope_id": "8871",
  "message_id": "69e789bb020000f1000d1629@mail.example.org",
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

Use when the agent should not decide alone.

```json
{
  "schema_version": 1,
  "at": "2026-04-24T13:00:00Z",
  "mailbox": "MAIN-MAILBOX",
  "envelope_id": "8871",
  "message_id": "...",
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

Optional helper index for reply work. Use only if `needs_reply=true`.

```json
{
  "schema_version": 1,
  "at": "2026-04-24T13:00:00Z",
  "mailbox": "MAIN-MAILBOX",
  "message_id": "...",
  "subject": "...",
  "from": "...",
  "folder": "Themen/AIxLLL/_Needs-Reply",
  "reply_status": "needed|drafted|sent|dismissed",
  "reply_note": "What needs to be answered."
}
```

## Idempotency

Before handling a mail, search these JSONL files for the normalized `message_id`:

- `action-log.jsonl`
- `pending-review.jsonl`
- `replies-needed.jsonl`

If already present, do not process again unless the user explicitly asks.
