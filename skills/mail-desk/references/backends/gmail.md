# Gmail Backend

Use this adapter only when the workspace uses the connected Gmail integration.

## Access and minimal reading

- Use the `gmail` skill for Gmail queries, message summaries, thread reads, drafts, and explicitly approved mailbox actions.
- Use `gmail-inbox-triage` for broad Inbox Zero-style triage; use `mail-desk` only after a mail or thread has been shortlisted for durable workspace processing.
- Begin with the minimal access required by the core flow: header, subject, sender, date, and a short preview. Search with Gmail query syntax; read the complete thread whenever surrounding conversation can change the classification.

## Routing and target verification

- Map catalog `mailbox_folder` values to Gmail labels according to the workspace's label convention.
- Map `<mailbox_folder>/_Needs-Reply` to a dedicated Gmail label only when that label convention exists; otherwise retain the project/topic label and record `needs_reply` in `data/mail-desk/`.
- Verify a routing action by reading the affected message or thread and confirming the intended label state.
- Do not assume that applying a label archives, moves, or removes `INBOX`; make each intended Gmail state explicit.

## Writes, final location, and identities

- Sending, archiving, deleting, moving, or applying labels requires explicit user intent, as required by the Gmail integration.
- Record Gmail routing in the final-location index with `backend: "gmail"`, `final_label`, `gmail_message_id`, and, when available, `gmail_thread_id`.
- Use `mail_desk_final_location_index.py` for the index write; do not edit the index manually.
- Gmail message IDs and thread IDs are backend locators. Keep the normalized RFC `Message-ID` as the durable cross-backend identity whenever present; a Gmail locator never replaces it.

## Sent Items

- Search `SENT` and use thread context before leaving an old `needs_reply` case open.
- For a matching sent reply, retain the Gmail-specific locator only as verification metadata; close or correlate the case through the normalized RFC `Message-ID`, `in_reply_to`, `references`, or the documented fallback key.

## Hard adapter boundary

- Do not invoke Himalaya CLI commands or folder-preflight scripts for Gmail.
- Do not apply Envelope-ID rules to Gmail; Gmail message and thread IDs remain Gmail-only backend locators.
