# Himalaya / IMAP Backend

Use this adapter for workspaces that access mail through a mailbox-specific Himalaya or IMAP skill.

## Access and routing

- Read the local `HIMALAYA.md`, if present, before concrete commands; it owns account selection, command syntax, and installation-specific constraints.
- Use the mailbox-specific skill for listing, reading, copying, and verifying messages.
- Run `python3 scripts/mailbox_preflight.py` before routing when the catalog changed, or with `--always` / `--force` when required. The script validates catalog target folders.
- For GroupWise-like backends, treat `message copy` as a de-facto move and use exactly one target per mail.

## Envelope IDs

- Envelope IDs are transient operational locators, never durable identifiers.
- After copy or move, locate the message in the destination folder and record that final destination Envelope-ID only as `envelope_id`.
- Keep the normalized RFC `Message-ID` as the primary, idempotency, close, and reference key.

## Backend scripts

- `scripts/mailbox_search_by_id.py` searches a Message-ID through Himalaya folders.
- `scripts/mail_desk_move_and_patch.py` performs the Himalaya routing action, verifies the final Envelope-ID, and writes the final-location index.
- `scripts/mailbox_preflight.py` checks catalog target folders.

The generic index and case-resolution scripts remain shared across backends.
