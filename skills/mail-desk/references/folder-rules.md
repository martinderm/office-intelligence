# mail-desk Routing Rules

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
| Spam quarantine notification, no legit signal visible in listed quarantined mail | `Junk` |
| Spam quarantine notification, plausible legit signal visible in listed quarantined mail | leave in `INBOX` + review |

## Backend mapping

The catalog names above describe the intended business target. The selected backend maps it to a concrete mailbox state:

- Gmail: an agreed Gmail label and, if applicable, an explicit Inbox state.
- Himalaya / IMAP: a target folder.

Read the selected adapter under `references/backends/` for command syntax, safety constraints, and final-location verification.

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
