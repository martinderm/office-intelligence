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
| Newsletter-suppressed (topic/project `do_not_route_if` matches `newsletter` in subject/headers) | `Newsletter` (`copy_as_move`); review only if another strong candidate exists |
| Other `do_not_route_if` suppression (`no-reply`, `mailing list`, automatic replies) | leave in `INBOX` + review |

## Suppression (do_not_route_if)

Catalog entries can declare `do_not_route_if` predicates (project and topic level). A
declared predicate that matches the mail's subject or header signals (subject, from, to,
cc — never body text) suppresses that candidate; suppressed candidates are surfaced as
`decision.suppressed_candidates[]` (catalog data: `kind`, `id`, `suppression_reason`) and
never silently dropped. A `mailing list` declaration intentionally still suppresses on a
subject that literally contains the phrase "mailing list"; the bare `list.` token matches
only from/to headers, never the subject. Newsletter-suppressed mails map deterministically
to the `Newsletter` folder (`copy_as_move`, `review_required: false`) unless a strong
non-suppressed candidate wins; all other suppressions keep the mail in `INBOX` with a
review reason. Thread (parent-folder) inheritance never consults `do_not_route_if`.

`keep_in_folder` (with `target_folder: INBOX`) is the retention action behind the
`leave in INBOX` rows above: the mail stays in its source folder and no mailbox write
occurs.

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
