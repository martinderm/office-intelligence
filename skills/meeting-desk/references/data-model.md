# Meeting Data Model & Schema

> **Skill:** `office-intelligence/meeting-desk`  
> **Standard:** Dual Evidence 2-Pillar Standard

---

## 1. Canonical register: `meetings.json`

`memory/evidence/meetings/meetings.json` is the SSOT for every known meeting and
all of its artifact paths. The canonical root is an **object**:

```json
{
  "schema": "legacy-root/1",
  "meetings": [
    {
      "meeting_id": "ticnote-synthetic-123",
      "source": {"system": "ticnote", "account_ref": null, "external_id": "synthetic-123"},
      "title": "Synthetic meeting",
      "date": 1792562400000,
      "dateString": "2026-10-21T06:00:00Z",
      "meeting_date": "2026-10-21",
      "start_estimated": false,
      "duration_minutes": 12.5,
      "duration": 12.5,
      "participants": [],
      "summary_path": "memory/evidence/topics/example-topic/events/example-event/meetings/2026-10-21-synthetic-meeting-76b67bbe.summary.md",
      "transcript_path": "memory/evidence/topics/example-topic/events/example-event/meetings/2026-10-21-synthetic-meeting-76b67bbe.transcript.md",
      "transcript_raw_path": "memory/evidence/topics/example-topic/events/example-event/meetings/2026-10-21-synthetic-meeting-76b67bbe.transcript.raw.md",
      "topic_slug": "example-topic",
      "project_slug": null,
      "event_slug": "example-event",
      "classification_status": "classified",
      "classification_basis": ["operator-choice"],
      "review_recommended": true,
      "review_reason": "content-changed; source-requested",
      "content_status": "provisional",
      "first_synced_at": "2026-10-21T06:26:58Z",
      "last_synced_at": "2026-10-21T06:27:31Z",
      "record_schema": "meeting-record/1",
      "source_metadata": {}
    }
  ]
}
```

Rules (normative for the canonical writer):

* **Object root.** Unknown root metadata and non-targeted entries are preserved.
  A naked legacy array is rejected with a diagnosis; it is **never** converted,
  reset or replaced by an empty register. Missing, malformed and legacy-array
  registers are three distinct states.
* **Identity.** Matching uses stable identity only: `meeting_id`, legacy `id`,
  and the source pair `source.system` + `source.external_id`. Documented legacy
  aliases: Fireflies raw external ID / `fireflies_<id>`, TicNote
  `ticnote-<recordId>`, Zoom `id: zoom-<uuid>`. A matched entry keeps its
  original stable ID (`meeting_id`, else legacy `id`) and its registered paths;
  the incoming ID never force-rekeys it, and frontmatter/receipts use that
  effective identity. Existing ID values are preserved. More than one match, a
  `meeting_id` bound to another source system, or a conflicting non-null
  `account_ref` stops before writes. An unsourced entry is only adopted through
  a documented, source-proven prefix/external-ID mapping; a bare unknown ID
  collision is rejected instead of reassigning a source. Title similarity is not
  identity.
* **Targeted updates.** Only the matched entry is rewritten. Unknown and legacy
  entry fields survive, and unknown fields inside `source` are preserved; only
  `system`, `external_id` and a non-null incoming `account_ref` are managed.
  `duration`/`duration_minutes` remain minutes aliases;
  `dateString`/`start_time` are legacy time fields; `first_synced_at` /
  `last_synced_at` / `synced_at` are import provenance. `duration` and
  `participants` are not overwritten with "unknown" when the record has no new
  value. When the register is rewritten it is serialized canonically (2-space
  indent, LF, UTF-8); untouched entries keep their values, not their original
  whitespace or key-spacing.
* **New entries** contain exactly the canonical field set shown above (25 keys,
  absent values `null`). They are not a frozen universal adapter contract with
  adapter-specific extras. `date` is Unix milliseconds only when a time is
  known; otherwise `null`. `dateString` is ISO UTC only with a known time.
  `meeting_date` carries the calendar day independently.
* **Content status.** New or changed generated content is `provisional`.
  `accepted` is only preserved from an unchanged human decision; it is never a
  writer outcome. Legacy status values are preserved as existing values, not
  re-emitted as new enum values.
* **Review.** `review_recommended` is monotonic: an adapter or writer trigger may
  add review, never remove it. Unrouting, estimated time, unknown duration,
  changed content and the adapter's own request are recorded in
  `review_reason`. Human dispositions are preserved for unchanged content. This
  package has no automatic transition to `accepted`.
* **Byte-verbatim artifacts.** Summary/transcript bodies are stored unchanged
  under a generated frontmatter; the raw transcript is stored with no frontmatter
  and is **immutable** after the first publication. LF or CRLF is preserved.
* **Orphans.** Every physical meeting file is registered. Only the writer may
  create or update them; a directly published `*.summary.md` / `*.transcript.md`
  pair outside the writer is refused as an orphan.

---

## 2. Record-v1 (adapter → writer transport)

Record-v1 is the only adapter output contract. It has exactly these root and
block fields; unknown extensions are allowed **only** inside the inert
`source_metadata` object. Example (same values as the conformance fixture):

```json
{
  "schema": "meeting-record/1",
  "source": {"system": "ticnote", "account_ref": null, "external_id": "synthetic-123"},
  "meeting_id": "ticnote-synthetic-123",
  "title": "Synthetic meeting",
  "start": {"utc": null, "calendar_date": "2026-10-21", "estimated": true, "basis": ["date-only"]},
  "duration_minutes": null,
  "participants": [],
  "artifacts": {"summary_md": "Synthetic summary\n", "transcript_md": null, "transcript_raw_md": null},
  "classification_hint": {"topic_slug": null, "project_slug": null, "event_slug": null, "basis": []},
  "review": {"recommended": true, "reason": "asr-name-and-number-check"},
  "source_metadata": {}
}
```

Field contract:

* `source.system` ∈ `fireflies`, `ticnote`, `zoom`, `manual`, `vimeo-webvtt`.
  `external_id` is a bounded (≤512 chars) non-empty opaque Unicode string,
  preserved exactly; control characters and unpaired surrogates are rejected.
  It is never a path component and is never normalized. `account_ref` is `null`
  or a bounded opaque reference, never a secret.
* `meeting_id` uses the same opaque identity contract (non-empty, ≤512 chars,
  no control characters/surrogates, exact bytes/characters, no normalization).
  Characters such as `/`, `\`, `+`, `=`, internal spaces or `..` are **valid
  opaque identity**, not filesystem paths: filenames are derived from a hash of
  the identity, and all path safety is enforced separately at the filesystem
  boundary. New IDs must be unique per source; contradictions stop. Manual IDs
  default to date + slug; a caller may resolve a collision with an explicit ID.
  There is no automatic suffix.
* `start` has exactly `utc`, `calendar_date`, `estimated`, `basis`. `utc` is
  `null` or RFC3339 **with explicit timezone**; it is projected to UTC.
  `calendar_date` is `null` or an ISO day. Without a time, `utc` is `null` and
  `estimated` must be `true`. A known timestamp may carry an observed
  provenance basis (for example `provider-datestring-zone`); only an estimated
  start requires a non-empty basis. Import time and invented noon/midnight are
  never substitutes.
* `duration_minutes` is `null` or a finite non-negative number (booleans are not
  numbers, `0` is a known zero, fractions are valid).
* `participants` is a string list. Speaker/provider details belong inertly in
  `source_metadata`.
* `artifacts` has exactly `summary_md`, `transcript_md`, `transcript_raw_md`
  (`null` = **not supplied**, never "delete"). A new record may omit an artifact
  (no path); an update preserves the existing registered path and bytes for every
  omitted artifact, including immutable raw and human-derived clean transcripts.
  There is no deletion field in Record-v1 and no record-driven disposal. A new
  clean transcript requires the unchanged raw source text. Raw files are
  immutable and carry no frontmatter. At least one of `summary_md` or
  `transcript_md` must be non-empty; raw alone is not a meeting body. An explicit reroute with an incomplete
  artifact snapshot is rejected before mutation; a full artifact snapshot may
  move all supplied artifacts and clean the old location only after the new
  location/register publication.
* `classification_hint` carries nullable slugs and a string basis. It is
  **untrusted input and never a routing decision**; `manual-user` is a claim,
  not human authority.
* `review.recommended` is strictly boolean, `review.reason` nullable text. It can
  request review but never downgrade a writer-required review.
* `source_metadata` is inert JSON, bounded by total size and depth. It is copied
  to the register entry and never parsed for routing, paths or lock decisions.

### Bounded JSON and validation limits

The writer rejects: invalid UTF-8, duplicate keys, non-finite or overflowing
numbers, lone surrogates, NUL, nesting deeper than 32, more than 200 000 JSON
nodes, records over 1 MiB, `source_metadata` over 64 KiB, artifact strings over
512 KiB, more than 20 000 register entries, and unknown root/block fields.
Artifacts are UTF-8 text; no lossy decoding is offered. These are input-safety
limits, not a general-purpose parser guarantee.

### Local-file entry

A second CLI entry (`--local`) builds the same Record-v1 and calls the same
writer; there is no second publisher.

```bash
python skills/meeting-desk/scripts/ingest_meeting.py --local \
  --summary memory/evidence/inbox/<name>.summary.md \
  [--transcript <path> [--raw <path>]] [--date YYYY-MM-DD] [--start-utc RFC3339] \
  [--duration MINUTES] [--participants NAME[,NAME]] [--title TEXT] \
  [--meeting-id ID] [--topic SLUG|--project SLUG] [--event SLUG] \
  [--rewrite-frontmatter] --workspace <workspace> [--lease-id <lease>] \
  [--apply-plan SHA256] --json
```

* `--summary` and/or `--transcript` are required; a transcript without `--raw`
  uses the unchanged transcript bytes as the raw artifact.
* Sources must resolve inside a canonical workspace zone and be regular,
  link-free files (the workspace root itself is the trusted anchor). Lexical
  child symlinks, junctions/reparse points and hardlinks are rejected before
  resolution, so no alias can be deleted instead of a real file. Writer state
  and catalog inputs (`meetings.json`, ownership, journal namespace,
  topics/projects/mail-desk catalogs) are rejected before planning/staging.
  Summary/transcript/raw bytes are preserved exactly; without
  `--rewrite-frontmatter` existing source frontmatter stays part of the body,
  with the flag a leading frontmatter block is stripped before storage. Raw is
  never text-rewritten and may be imported with a summary even when no clean
  transcript exists.
* Plan/dry-run is the default; apply needs the matching plan hash and an owned
  lease. The meeting ID defaults to `manual-<date|undated>-<slug>`; collisions
  or a different source under one ID reject without an automatic suffix; an
  explicit `--meeting-id` resolves a caller-side conflict.
* Sources are moved, not mirrored: hash-bound delete steps run only after the
  register points at the published artifacts, and are journalled/recoverable.
  There is no automatic `--upgrade` (unknown options are errors).



### Conformance fixture pack

`tests/fixtures/meeting-record-v1.json` (schema `meeting-record-fixtures/1`) is
the shared SSOT: 11 `valid_records` (including observed provider basis, opaque
Zoom base64 identities and internal-space/`../escape` opaque IDs),
19 `invalid_records`, 5 `raw_invalid_json`, plus a
Synthetic provenance block and a SHA-256 manifest over the canonical JSON of the
three arrays. Verify it with
`scripts/verify_meeting_fixtures.py --json`. Adapter copies must stay
byte-identical; expected values are fixed independently of production code.

---

## 3. Frontmatter of generated meeting files

The writer generates the frontmatter; bodies stay byte-exact.

```markdown
---
document_type: meeting-summary
evidence_level: observed
status: provisional
meeting_id: "ticnote-synthetic-123"
title: "Synthetic meeting"
date: "2026-10-21"
topic: "example-topic"
project: null
event: "example-event"
participants:
  - "alex@example.invalid"
summary_path: "memory/evidence/topics/example-topic/events/example-event/meetings/....summary.md"
---
```

Transcript files use `document_type: meeting-transcript`, `transcript_path` and
`source_raw`. The raw transcript is stored as evidence text only (no
frontmatter). Machine imports are `evidence_level: observed` and
`status: provisional`; this is not a content approval.

---

## 4. Reproducible transcript cleaning

`*.transcript.raw.md` remains unchanged. `*.corrections.json` contains the
reviewed configuration `meeting-corrections/1` with `pairs`, `regex_pairs`,
`rules` and `unklar`; required fields and error behaviour are in the
[builder contract](../scripts/README.md). The cleaned output contains
`source_raw` (workspace-relative path), `corrections_count`, `unklar_count` and
separate rule counters in the header. Silent normalizations do not count as
inline pairs.

Publication is delegated to the canonical writer: only the registered
`transcript_path` of exactly one meeting may be updated, and only while the file
is provably writer-owned (recorded hash). A byte-identical rebuild is a no-op.
`--check` stays read-only. For a byte-identical rebuild the same metadata options
belong to the documented call.

---

## 5. Writer journal, ownership and recovery

* **Ownership** is recorded in `data/meeting-desk/ownership.json` (schema
  `meeting-desk-ownership/1`): workspace-relative path, meeting ID, SHA-256,
  size, artifact kind, writer version and write time. A generated update is only
  allowed against exactly this recorded hash; a file without a record is
  treated as human/non-writer content and is not overwritten (identical bytes
  are still a no-op).
* **Journal** files live under `data/meeting-desk/journal/` with staged bytes in
  a per-operation subdirectory. Each journal carries an integrity digest over
  its body, pre/post hashes per step and the register/ownership prestate.
  Publication order: artifacts (no-clobber create / hash-bound replace), new
  ownership, register, then old-location and local-source cleanup. Only
  journal-bound, hash-verified, single-level own stage files are removed after
  success. Any other entry or stage directory in the journal directory is
  unverifiable foreign state and blocks mutation with `recovery_required` (exit
  3) while preserving every byte; nothing is recursively traversed or deleted.
* **No multi-file transaction.** There is no ACID or power-loss guarantee across
  several `os.replace` calls. An open journal blocks every mutating writer
  operation, including builder publication, rerouting and manual intake; the
  envelope reports `recovery_required` (exit 3), never success. Every mutating
  step re-checks the owned lease before the next mkdir/stage/write/rename/delete.
* **UTF-8 CLI.** stdout is written as UTF-8 bytes (one six-field envelope line),
  independent of the console codepage; a cp1252 console cannot turn a successful
  apply into a `UnicodeEncodeError`. Text-only library streams still work. Failures
  never print a traceback.
* **Recovery** is explicit only: `--inspect` is read-only;
  `--recover --lease-id` requires an owned lease and unchanged journal/stage
  hashes, and resumes forward only when each target is in its recorded pre- or
  post-state. Foreign bytes, missing targets, a corrupt journal or a second open
  journal stop without mutation. Recovery never changes raw evidence and is not
  a data migration.
* Readers that are not journal-aware see no guaranteed global snapshot
  isolation. No race-freedom is claimed against a malicious concurrent
  filesystem actor, and no Power-Loss/adversarial guarantee beyond the tested
  cooperative lock boundary.
