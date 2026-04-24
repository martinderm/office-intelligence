# Pending Actions Architecture

## Status

Implementation layer for post-classification mail processing in `office-intelligence`.

## Purpose

`pending-actions.json` is intentionally small. It is not a knowledge store and not a decision queue. Its job is to point the next processing step to classified mail artifacts that still need contextual post-processing.

The post-processing step can then load the mail artifact plus the relevant project/topic context and decide whether to update references, create reply tasks, or create a real pending decision.

## Files

Default paths are relative to `MAIL_PROCESSOR_DATA_DIR`:

- `pending-actions.json` — current open mail post-processing queue
- `logs/actions/YYYY-Www.json` — weekly action logs for completed/dismissed/failed actions
- existing `pending-decisions.json` — human decisions only, not duplicated here
- existing `state.jsonl` — operational run/event log

No separate action `state.json` is introduced for now. The queue state is `pending-actions.json`; durable history is the weekly action log; technical runtime state remains in `state.jsonl`.

## Boundaries

### `pending-actions.json`

Contains only enough information to find and post-process a classified mail:

- stable/action identity
- source mailbox/folder/envelope
- `stable_id` and `file_id`
- JSON mail artifact path
- primary target from classification
- optional confidence
- `needs_reply`
- classification source

It must not contain:

- full mail content
- extracted facts
- summaries
- suggested reference edits
- decisions requiring Martin

### `pending-decisions.json`

Contains decisions that require human input, e.g. missing mailbox folders or later explicit reference/update choices.

A weak or missing classification target in `pending-actions.json` is not itself a decision. The post-processing step may turn it into a pending decision if human judgment is actually needed.

### `logs/actions/YYYY-Www.json`

Contains completed/failed/dismissed/superseded action entries. Processed actions are removed from `pending-actions.json` and appended to the current ISO-week log.

## Target Semantics

`target.kind` is one of:

- `project`
- `topic`
- `none`

There is deliberately no `review` target kind. Review is a processing outcome or possibly a pending decision, not a target. This avoids duplicating `pending-decisions.json`.

## Creation Rule

For every processed/classified mail artifact, `mail-processor` upserts one `mail_postprocess` item keyed by:

```text
mail_postprocess:<stableId>:<fileId>
```

`message_id` is represented by `stable_id`; for normal messages they are identical. `file_id` is retained because it points to the local artifact naming scheme.

## Routing Rule

For BOKU GroupWise, Himalaya `message copy` has been observed to behave effectively like a move. Therefore `MAIL_COPY_SEMANTICS=acts_like_move` must be treated as a single-target operation.

Needs-reply handling is consequently a needs-reply move in that environment:

- project match + `needs_reply=true` -> `<project.mailbox_folder>/_Needs-Reply`
- topic match without project + `needs_reply=true` -> `<topic.mailbox_folder>/_Needs-Reply`
- no project/topic match + `needs_reply=true` -> `Inbox/_Needs-Reply`
- project/topic match + `needs_reply=false` -> project/topic folder

With normal copy semantics, project/topic mails that need a reply may still be copied to both the parent folder and its `_Needs-Reply` child folder.

Final archival/routing after contextual post-processing should be implemented as a later explicit processing step, not hidden inside classification.

## Processing Rule

A later processor should:

1. load `pending-actions.json`
2. select an item
3. load the referenced JSON mail artifact
4. load the relevant project/topic context if `target.kind` is known
5. perform or propose the actual post-processing
6. remove the item from `pending-actions.json`
7. append the result to `logs/actions/YYYY-Www.json`
8. create/update `pending-decisions.json` only if Martin needs to decide something
