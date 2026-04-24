# mail-desk Folder Rules

## Source of truth

Folder names come from the workspace catalogs:

- `memory/references/projects/projects.json` → `project.mailbox_folder`
- `memory/references/topics/topics.json` → `topic.mailbox_folder`

Do not invent permanent folder names if a catalog entry exists. If a catalog entry is wrong or missing, use review instead of silent correction.

## Routing targets

| Situation | Target |
|---|---|
| Project + needs reply | `<project.mailbox_folder>/_Needs-Reply` |
| Topic + needs reply | `<topic.mailbox_folder>/_Needs-Reply` |
| Project, no reply | `<project.mailbox_folder>` |
| Topic, no reply | `<topic.mailbox_folder>` |
| Unclear + needs reply | `INBOX/_Needs-Reply` or review |
| Unclear, no reply | leave in INBOX + review |

## BOKU/GroupWise convention

The mailbox-specific Himalaya skill owns the exact command syntax and backend caveats. For BOKU GroupWise, the important operational consequence is:

- `message copy <target> <id>` often behaves like a move.
- Use exactly one target per mail.
- Verify when the action is risky or unexpected.
- Keep Message-ID in the log because Envelope-ID changes after moves/copies.

## Missing folders

If a target folder is missing:

1. Do not silently route elsewhere unless the user gave a rule.
2. Add `pending-review.jsonl` entry with `reason="missing_folder"`.
3. If the missing folder is structurally expected, ask whether to create it or update the catalog.

## Needs-Reply child folders

`_Needs-Reply` is derived from the parent `mailbox_folder`. It is not stored as a separate catalog field.

Examples:

- `Projekte/MESHE` → `Projekte/MESHE/_Needs-Reply`
- `Themen/AIxLLL` → `Themen/AIxLLL/_Needs-Reply`
