# Project Intake Template (schema v3)

## title

## id
<!-- lowercase slug, z. B. usage-ng -->

## mailbox_folder
<!-- z. B. Projekte/USAGE-NG -->

## kuerzel
<!-- z. B. USAGE-NG -->

## project_website
<!-- optional, z. B. https://example.org oder k. A. -->

## project_reference
<!-- optional, z. B. 101243057 oder k. A. -->

## laufzeit
<!-- optional, z. B. 2026-01-01 bis 2028-12-31 oder k. A. -->

## gesamtbudget
<!-- optional, z. B. EUR 490,980 oder k. A. -->

## institution_budget
<!-- optional, z. B. EUR 85,000 oder k. A.; `boku_budget` bleibt als Legacy-Alias zulässig -->

## reference_md
<!-- Default: memory/references/projects/<id>/index.md -->

## description

## aliases

-

## keywords

-

## domains

-

## contacts

- name:
  email:

## typical_subject_patterns

-

## workpackages

```yaml
- id: wp1
  number: 1 # optional positive number
  title: Coordination and quality
  lead: Example University # optional
  boku_role: Contributor # optional
  status: active # active | completed | planned | paused
  aliases: [WP1]
  keywords: [quality]
  contacts:
    - email: wp1@example.eu
      role: lead
  tasks:
    - id: T1.1
      title: Kick-off
      lead: Example University
      keywords: [kick-off]
  deliverables:
    - id: D1.1
      title: Quality plan
      lead: BOKU
      type: Report
      due_month: M6
```

## milestones (project-wide; never WP-owned)

```yaml
- id: MS1
  title: Kick-off held
  lead: Example University
  due_month: M2
  related_wps: [wp1]
  prerequisites: Grant agreement signed
```

## routing_priority

50

## do_not_route_if

- newsletter
- no-reply

## cloud_sync

```yaml
default:
  scan_dir: cloud/projects/<id>
```

## schema_version

3
