# Topic Intake Template

## title

## id
<!-- slug, z. B. rpl-validation -->

## mailbox_folder
<!-- z. B. Topics/RPL -->

## reference_md
<!-- default: memory/references/topics/<slug>/index.md -->

## aliases
- 

## keywords
- 

## domains
- 

## contacts
- name:
  email:
  role:

## subtopics
- id:
  title:
  aliases: []
  keywords: []
  typical_subject_patterns: []
  contacts: []
  reference_md: memory/references/topics/<topic-slug>/subtopics/<subtopic-slug>.md
  operations:
  - id: <operation-slug>
    title: <Operation title>
    aliases: []
    keywords: []
    typical_subject_patterns: []
    reference_md: memory/references/topics/<topic-slug>/subtopics/<subtopic-slug>/operations/<operation-slug>/index.md
    status: active
  status: active

## description

## typical_subject_patterns
- 

## routing_priority
70

## do_not_route_if
- newsletter
- no-reply
