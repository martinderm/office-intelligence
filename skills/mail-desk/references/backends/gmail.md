# Gmail Backend

Use this adapter when the workspace uses the connected Gmail integration.

## Access and reading

- Use the `gmail` skill for Gmail queries, message summaries, thread reads, drafts, and explicitly approved mailbox actions.
- Use `gmail-inbox-triage` for broad Inbox Zero-style triage; use `mail-desk` only after a mail or thread has been shortlisted for durable workspace processing.
- Search with Gmail query syntax and read a complete thread whenever surrounding conversation can change the classification.
- Treat Gmail message IDs and thread IDs as backend locators. Keep the normalized RFC `Message-ID` as the durable cross-backend identity whenever present.

## Routing

- Map catalog `mailbox_folder` values to Gmail labels according to the workspace's label convention.
- Map `<mailbox_folder>/_Needs-Reply` to a dedicated Gmail label only when that label convention exists; otherwise retain the project/topic label and record `needs_reply` in `data/mail-desk/`.
- Verify a routing action by reading the affected message or thread and confirming the intended label state.
- Do not assume that applying a label archives, moves, or removes `INBOX`; make each intended Gmail state explicit.

## Writes and evidence

- Sending, archiving, deleting, moving, or applying labels requires explicit user intent, as required by the Gmail integration.
- Record Gmail routing in the final-location index with `backend: "gmail"`, `final_label`, `gmail_message_id`, and, when available, `gmail_thread_id`.
- Use `mail_desk_final_location_index.py` for the index write; do not edit the index manually.
- Search `SENT` and use thread context before leaving an old `needs_reply` case open.

## Do not use

- Do not invoke Himalaya CLI commands, folder preflight scripts, or Envelope-ID rules for Gmail.
- Do not treat a Gmail message ID as a replacement for the durable RFC `Message-ID`; it is a backend locator.
