# Himalaya / IMAP Backend

Use this adapter only for workspaces that access mail through a mailbox-specific Himalaya or IMAP skill.

## Preconditions and minimal reading

- Read the local `HIMALAYA.md`, if present, before concrete commands; it owns command syntax and installation-specific constraints. The machine-enforced account binding for batch operations is instead the credentials-free workspace file `.agents/mail-desk-backend.json`; it contains exactly `{"schema_version": 1, "backend": "himalaya", "account": "<configured-name-or-null>"}`. `null` explicitly selects the local Himalaya default account. It contains no credentials.
- The batch runner never derives its backend from available apps/connectors and does not accept `--account` as an override for mailbox modes. A manifest account may be review evidence, but must exactly equal the workspace-bound account.
- Before `execute` and explicit autonomous `pipeline`, the runner performs one bounded, read-only `envelope list -s 1` on the configured source folder (10 seconds). A timeout, unavailable adapter, account mismatch, malformed configuration, or non-list JSON response is a canonical `mailbox_readiness` failure and occurs before any local or mailbox mutation.
- Use the mailbox-specific skill for listing, reading, copying, and verifying messages.
- Begin with the smallest suitable folder listing or preview, then read only the message material required by the core flow.
- Run `python3 scripts/mailbox_preflight.py` before routing when the catalog changed; use `--always` or `--force` when required. It validates catalog target folders.

## Writes, routing, and target verification

- Copy, move, and delete are mailbox writes and require the core flow's explicit approval before execution.
- For GroupWise-like backends, treat `message copy` as a de-facto move and use exactly one target per mail.
- `scripts/mail_desk_move_and_patch.py` locates the source by normalized `Message-ID` when necessary, copies it to the target, verifies the message in that target, attempts source deletion after verification, and writes the final-location index.
- If routing is performed with the client, locate the message in the destination folder after copy or move before recording its final backend location. Do not index a source or intermediate location.

## Himalaya JSON client

### Non-interactive bootstrap

- The adapter resolves only the `himalaya` executable and the config file. Set
  `HIMALAYA_CONFIG` to an absolute, readable `config.toml` path to override the
  platform default (`%APPDATA%\\himalaya\\config.toml` on Windows,
  `~/.config/himalaya/config.toml` elsewhere). The adapter supplies it as
  `-c <path>`. The explicit override and the platform default both pass through the
  same `normalize_himalaya_config_path()` step before existence validation and
  before the `-c` token is built.
  Himalaya 1.2.0 interpretiert Windows-Drive-Doppelpunkte in `-c` als Pfadlistentrenner;
  der Adapter konvertiert deshalb lokale Windows-Drive-Pfade deterministisch auf
  `\\localhost\<drive>$\...`.
- UNC conversion depends on the local administrative share `\\localhost\<drive>$`
  being reachable. If it is disabled or unreachable, the existence validation fails
  and the adapter stops before any process start as a missing configuration
  (`himalaya_config_missing`); no end-to-end support is claimed without a reachable
  admin share.
- `HIMALAYA_COMMAND` is deliberately unsupported. Command strings, shells and
  interactive setup are never configuration interfaces for this adapter.
- Before starting a mailbox process, the adapter verifies executable and config.
  Missing config is `himalaya_config_missing`, an invalid override is
  `himalaya_config_invalid`, and a missing executable is `himalaya_unavailable`.
  These stop immediately and cannot launch Himalaya's setup wizard.
- A timeout stops immediately. Only bounded transport/TLS failures are retried.
  Config, account, authentication, syntax and other command failures are returned
  immediately.

- `scripts/mail_desk_himalaya_client.py` performs Himalaya/IMAP listing, reading, read-only RFC-822 MIME attachment inspection, copying, moving, deleting, searching, and folder checks with structured JSON envelopes and automatic socket/TLS-10054 error handling.
- Use its search operation with a normalized `Message-ID` and the relevant folders when a backend lookup is needed; that search result remains locator evidence, not durable identity.
- **Operational rule: invoke the client only through a JSON input file using `--input`, including single-message and inspection operations.** Direct ad-hoc subcommands with changing arguments are not permitted in the operational agent workflow.

  ```bash
  python3 scripts/mail_desk_himalaya_client.py --input data/mail-desk/himalaya-op.json
  ```

### Message reads and body preview

The client's `read` operation (subcommand `read`, manifest action `read`) fetches
headers plus a bounded body preview. The preview length is the manifest field
`preview_lines` and the CLI flag `--preview-lines`; **both default to `50`
lines**. The result reports `read_level`:

- `"preview"` — the body is truncated to `preview_lines` lines. A high value
  (for example `400`) returns effectively the full message text while
  `read_level` stays `"preview"`.
- `"full_body"` — produced only by the internal full-body read path
  (`full_body=True`, for example the classifier's escalation read), which
  bypasses line truncation and is **not** reachable through the `read` CLI
  subcommand or manifest action.

```bash
python3 scripts/mail_desk_himalaya_client.py read -f INBOX 7195 --json
python3 scripts/mail_desk_himalaya_client.py read -f INBOX 7195 --preview-lines 400 --json
```

```json
{"account": "…", "operations": [{"action": "read", "folder": "INBOX", "envelope_id": "7195", "preview_lines": 400}]}
```

### Unsent draft operations (`save-draft` / `delete-draft` / `replace-draft`)

The three draft operations create, replace and remove **unsent** drafts. They are
standalone client operations with **no approval gate** and no SMTP path: sending is
out of scope, the batch runner has no draft mode, and the operations touch no batch
journal.

- **Target folder (dynamic, fail-loud):** [`scripts/core/drafts.py`](../scripts/core/drafts.py)
  (`resolve_drafts_folder`) resolves the folder from the live catalog — first the
  RFC-6154 `\Drafts` special-use attribute (forward-compatible: Himalaya 1.2.0's
  `folder list` exposes no attribute columns, only `-a`/`-w`/`-o`), then the
  conventional name `Drafts`, then the GroupWise convention `Work In Progress`. If
  none resolves, the operation fails loud with a bounded
  `drafts_folder_unresolved` error instead of hard-coding a folder name. An explicit
  `-f/--folder` skips the resolution.
- **Append (UTF-8 via stdin, Issue #11):** `save-draft` builds the raw RFC-822
  message with the SMTPUTF8 policy (CRLF) and writes its exact UTF-8 bytes to the
  **binary stdin pipe** of `message save` (`message save -o plain -f <folder>`,
  no raw argv literal). Himalaya 1.2.0 (rev
  `1b70c4e0eaa72dee48353f0211e6cc0f0776fe98`,
  `src/email/message/command/save.rs`) ignores the `MESSAGE` argument whenever
  stdin is not a TTY and the output format is not JSON, and instead reads stdin
  with Rust `lines()`, joins the lines with CRLF and drops the final line
  terminator. Every draft size therefore uses stdin (threshold 0, no size
  split); an argv payload is silently ineffective and, above roughly 32k
  characters, also fails with `WinError 206` on Windows. The explicit `-o plain`
  keeps the JSON branch from bypassing stdin, and one extra transport CRLF
  compensates the `lines()`/join normalization so the appended message equals
  the canonical builder raw byte for byte. The payload is validated as strict
  UTF-8 before any executable/config resolution or subprocess (an invalid byte
  would otherwise be silently truncated by upstream `lines()`); a bounded
  `ValueError` without payload content is raised. Non-ASCII subject, recipients
  and body survive without a 7-bit downgrade. Only a
  `HimalayaInvocationError` becomes `draft_save_failed` (no verification, no
  journal item, no argv fallback, no retry `max_retries=1`); other errors
  propagate. An `OSError` (launch or communication, payload and non-payload
  path) becomes `himalaya_launch_failed` with an unknown outcome and is not
  retried. The result carries `{envelope_id, folder, subject, to, date}`.
- **Delete without UID MOVE:** `delete-draft` runs
  `flag add deleted <envelope_id> -f <folder>` and then
  `folder expunge <folder>`. `message delete` is deliberately not used: Himalaya
  documents that it "moves it to the trash folder" for a non-trash folder, which is
  exactly the forbidden UID MOVE.
- **Replace (new-then-delete):** `replace-draft` appends the replacement draft first
  and deletes the old one afterwards. A crash between the two steps leaves both
  drafts observable (documented crash window); the draft journal records both
  envelope ids.
- **Journal:** every operation appends to `data/mail-desk/draft-journal.json`
  (schema 1: `envelope_id`, `folder`, `subject`, `to`, `date`, `action`, plus
  `old_envelope_id`/`crash_window` on replace). The batch
  `batch-recovery-journal.json` contract stays untouched.

### Temporary host execution boundary

If the sandbox cannot read the configured Himalaya config but the same bounded,
read-only probe succeeds on the host, execute only the JSON-input client above
outside the sandbox. Build and review the exact manifest inside the sandbox, pass
the trusted host `HIMALAYA_CONFIG`, and return the structured JSON envelope for
normal processing. Do not approve arbitrary Himalaya commands or a blanket
`himalaya`/`python` command prefix. Read operations may cross this boundary;
copy, move, delete and send still require their existing operation-specific human
approval, workspace lock, identity/location preconditions and verification. This
is a temporary compatibility path pending a least-privilege gateway.

- The existing `himalaya-op.json` manifest shape is documented in [`references/cli-operations.md`](../cli-operations.md); its batch lifecycle is documented in [`references/batch-runner.md`](../batch-runner.md).
- `inspect_attachments` requires the manifest-bound account and a verified RFC
  `Message-ID`. An optional operation-level `account` is evidence only and must
  exactly match the manifest-bound account. Inventory or drift failures are
  errors, never an empty successful attachment result.

### Attachment evaluation boundary

The attachment-evaluation flow reuses the client's read-only RFC-822 inspection and the
canonical fetch/extract/handoff seams; it never issues a mailbox write.

- **Raw-MIME / fetch boundary:** `inspect_attachments` exports the message read-only as an
  RFC-822 source, and only the canonically revalidated, policy-allowed MIME parts
  (`fetch_status: "available"`, `policy_status: "allowed"`) are fetched into
  `data/mail-desk/attachments/<run-id>/`. Mail and attachment content stay
  `untrusted_external`; caller-claimed candidates, policy/fetch statuses or receipts are not
  authority.
- **Lock / preflight / policy controls:** fetch runs behind the shared workspace-lock guard
  with `allow_legacy=False` (no env/parameter/manifest legacy bypass), behind the bounded
  read-only tracked-quarantine preflight (`git ls-files`, no shell, with timeout), and behind
  active-content blocking, extension/MIME consistency and the 15 MB per-file / 25 MB per-mail
  / max-5-files quotas. A missing or foreign lock stops fail-closed before the first write.
- **Extraction timeout:** extraction is bounded and reuses the canonical extractor; the
  terminal extraction status `extraction_failed` (including a timeout) maps to the bounded
  staged `failed` / `extraction_failed` envelope. There is **no automatic retry**.
- **No automatic write / reclassification:** The attachment evaluation performs no mailbox write, no promotion,
  no export, no filing, no disposition and no cleanup, and it does **not** reclassify the mail
  or install the staged result into a `DraftManifest`.
- **Draft continuation:** the default-on `draft` path fetches raw
  MIME only for an ambiguous item, runs `attachment_evaluate`, consumes a validated `ready`
  handoff exactly once as a distinct `untrusted_external` classifier input, and installs the
  additive final `attachment_evaluation`. The mutually exclusive
  `--evaluate-attachments`/`--no-evaluate-attachments` options are valid only for direct
  `draft`/`inspect` (`draft` defaults on, `inspect` defaults off). The
  ready handoff is revalidated canonically before any classification, every bounded
  failure stays item-local in Review/`INBOX`, a deterministic PII-free per-message
  run-id reaches `already_fetched` on a repeated default invocation (no duplicate
  attachment fetch, no cross-run cache), and unexpected backend contract errors fail loud
  instead of being relabelled. The opt-in `inspect` proposal is
  wired: plain `inspect` stays read-only, and only `--evaluate-attachments` (default off)
  adds a top-level, non-executable `manifest_proposal` through the same item flow as
  `draft`; an executable batch manifest is still written only to an explicitly configured
  `manifest_file`. The package acceptance is complete: a hermetic
  package-acceptance test (`tests/test_batch_runner_mde2_acceptance.py`) proves the single
  real path Body/Full Read -> ambiguous -> real `text/plain` attachment -> one
  `untrusted_external` reclassification -> persisted project `DraftManifest` with a bounded
  `attachment_evaluation` (`used_for_classification: true`, 64-hex `classifier_revision`
  bound to the classifier rules plus the consumed attachment hash) at zero mailbox/network
  and zero execute/promote/export/filing/disposition/catalog/cloud side effects.

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

Vertragshistorie: System Map L2 (docs/system-map) und docs/features/_archive.md; Ticket-IDs sind dort kanonisch.
