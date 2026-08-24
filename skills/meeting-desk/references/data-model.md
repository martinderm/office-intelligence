# Meeting Data Model & Schema

> **Skill:** `office-intelligence/meeting-desk`  
> **Standard:** Dual Evidence 2-Pillar Standard

---

## 1. Top-Level Index: `meetings.json`

Die Datei `memory/evidence/meetings/meetings.json` dient als kanonisches Register aller im Workspace bekannten Meetings.

### JSON-Schema Beispiel:

```json
[
  {
    "id": "fireflies_123456789",
    "source": {
      "system": "fireflies",
      "import_mode": "api_sync",
      "external_id": "123456789"
    },
    "title": "Semesterplanung WS2026/27",
    "date": 1787481600000,
    "dateString": "2026-08-24 10:00:00",
    "duration_minutes": 45,
    "channel_slug": "lehre-weiterbildung",
    "summary_path": "memory/evidence/meetings/lehre-weiterbildung/2026-08-24-semesterplanung-ws2026-27.summary.md",
    "transcript_path": "memory/evidence/meetings/lehre-weiterbildung/2026-08-24-semesterplanung-ws2026-27.transcript.md",
    "topic_slug": "aixlll",
    "project_slug": null,
    "participants": [
      "martin.mayr@boku.ac.at",
      "kollege@boku.ac.at"
    ],
    "classification_status": "classified",
    "synced_at": "2026-08-24T10:55:00Z"
  }
]
```

---

## 2. Struktur der Markdown-Summary (`*.summary.md`)

```markdown
---
document_type: meeting-summary
evidence_level: observed
status: accepted
meeting_id: "fireflies_123456789"
title: "Semesterplanung WS2026/27"
date: "2026-08-24"
topic: "aixlll"
participants:
  - "Martin Mayr"
  - "Kollege"
summary_path: "memory/evidence/meetings/lehre-weiterbildung/2026-08-24-semesterplanung-ws2026-27.summary.md"
---

# Meeting: Semesterplanung WS2026/27

## 📌 Kernthemen & Kontext
- Abstimmung der Termine für Blocklehrveranstaltungen im Wintersemester.

## 🤝 Wesentliche Beschlüsse
- Beschluss 1: Vorlesungsstart am 12. Oktober.
- Beschluss 2: Moodle-Kurs bis 15. September aktualisieren.

## ⚡ Action Items
- [ ] **Martin:** Moodle-Kurs duplizieren und freischalten (bis 15.09.2026).
- [ ] **Kollege:** Gastvortragende anfragen.
```
