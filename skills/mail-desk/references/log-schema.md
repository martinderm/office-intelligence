# mail-desk Log Schema

All files are JSONL under `data/mail-desk/`. Keep entries small. Active files contain only open/current items; completed items move to `data/mail-desk/archive/YYYY-Www/`.

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
  "status": "running|completed|failed",
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

## Idempotency

Before handling a mail, search active and archived JSONL files for the normalized `message_id` or fallback `message_key`:

- active files: `action-log.jsonl`, `pending-review.jsonl`, `replies-needed.jsonl`
- archive files under `archive/YYYY-Www/`

If already present, do not process again unless the user explicitly asks.
