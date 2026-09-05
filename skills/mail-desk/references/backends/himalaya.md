# Himalaya / IMAP Backend

Use this adapter only for workspaces that access mail through a mailbox-specific Himalaya or IMAP skill.

## Preconditions and minimal reading

- Read the local `HIMALAYA.md`, if present, before concrete commands; it owns account selection, command syntax, and installation-specific constraints.
- Use the mailbox-specific skill for listing, reading, copying, and verifying messages.
- Begin with the smallest suitable folder listing or preview, then read only the message material required by the core flow.
- Run `python3 scripts/mailbox_preflight.py` before routing when the catalog changed; use `--always` or `--force` when required. It validates catalog target folders.

## Writes, routing, and target verification

- Copy, move, and delete are mailbox writes and require the core flow's explicit approval before execution.
- For GroupWise-like backends, treat `message copy` as a de-facto move and use exactly one target per mail.
- `scripts/mail_desk_move_and_patch.py` locates the source by normalized `Message-ID` when necessary, copies it to the target, verifies the message in that target, attempts source deletion after verification, and writes the final-location index.
- If routing is performed with the client, locate the message in the destination folder after copy or move before recording its final backend location. Do not index a source or intermediate location.

## Himalaya JSON client

- `scripts/mail_desk_himalaya_client.py` performs Himalaya/IMAP listing, reading, copying, moving, deleting, searching, and folder checks with structured JSON envelopes and automatic socket/TLS-10054 error handling.
- Use its search operation with a normalized `Message-ID` and the relevant folders when a backend lookup is needed; that search result remains locator evidence, not durable identity.
- **Operational rule: invoke the client only through a JSON input file using `--input`, including single-message and inspection operations.** Direct ad-hoc subcommands with changing arguments are not permitted in the operational agent workflow.

  ```bash
  python3 scripts/mail_desk_himalaya_client.py --input data/mail-desk/himalaya-op.json
  ```

- The existing `himalaya-op.json` manifest shape is documented in [`references/cli-operations.md`](../cli-operations.md); its batch lifecycle is documented in [`references/batch-runner.md`](../batch-runner.md).

## Envelope IDs

- Envelope IDs are transient operational locators, never durable identifiers.
- After copy or move, record the verified destination Envelope-ID only as `envelope_id`.
- Keep the normalized RFC `Message-ID` as the primary, idempotency, close, and reference key; an Envelope-ID cannot replace it.

## Sent Items

- List and read the applicable IMAP Sent folder through the mailbox-specific skill or the JSON client, with the same minimal-access rule.
- Keep any Sent-folder Envelope-ID only as verification metadata. Correlate or close reply cases through the normalized RFC `Message-ID`, `in_reply_to`, `references`, or the documented fallback key.

## Backend scripts

- `scripts/mailbox_preflight.py` validates catalog target folders before routing.
- `scripts/mail_desk_move_and_patch.py` performs the verified routing and final-location-index update described above.
- `scripts/mail_desk_himalaya_client.py` is the JSON-input-only client for concrete Himalaya/IMAP operations.
