---
type: Project
schema_version: 3
---

# Template — Project Folder Layout

Für jedes Projekt wird eine Arbeitsstruktur in beiden Säulen des Dual-Evidence-Standards angelegt:

- **Säule 1 (Referenzen & Struktur):** `index.md`, `signals.md`, `contacts.md`, `workpackages/`
- **Säule 2 (Evidenzen & Logs):** `memory/evidence/projects/<id>/` mit monatlichen `YYYY-MM.md`-Logs

## `index.md` (Template)

```md
---
id: <id>
title: <title>
kuerzel: <KÜRZEL>
mailbox_folder: <Projekte/...>
project_website: <https://... | k. A.>
project_reference: <... | k. A.>
laufzeit: <... | k. A.>
gesamtbudget: <... | k. A.>
institution_budget: <... | k. A.>
boku_budget: <... | optional legacy alias>
schema_version: 3
---

# <id> — <title>

Kurzbeschreibung (1–3 Sätze): Worum geht’s, wer ist beteiligt, was ist das Ziel?

## Überblick

- Status: aktiv | pausiert | abgeschlossen | unklar
- Owner:
- Mailbox-Ordner:
- Letzte Aktivität: YYYY-MM-DD
- Aktualisiert am: YYYY-MM-DD

## Aktuelle Lage

<!-- BEGIN:managed-summary -->
- Kurzstatus folgt aus klassifizierten Mails.
<!-- END:managed-summary -->

## Referenzen & Evidenz

- Signale: ./signals.md
- Workpackages: ./workpackages/
- Evidenz-Log: ../../../evidence/projects/<id>/
```

## `signals.md` (Template)

```md
# Signals — <id>

## Routing-Signale

- Primäre Domains:
- Schlüssel-Kontakte (Name <mail>):
- Typische Betreffmuster:
- Typische Begriffe / Abkürzungen:

## Do-not-route / Ausschlüsse

- newsletter
- no-reply
- autoreply

## Konsolidierte Signale (Managed)

<!-- BEGIN:managed-signals -->
- Letzter relevanter Mailkontakt:
- Relevante Teilnehmer:innen:
- Häufige Themen/Cluster:
- Aktuelle nächste Schritte:
- Risiken/Blocker:
<!-- END:managed-signals -->
```

## `memory/evidence/projects/<id>/YYYY-MM.md` (Template)

```md
---
document_type: evidence-log
evidence_level: observed
project: <id>
timeframe: YYYY-MM
---

# Evidence — <id> — YYYY-MM

<!-- BEGIN:managed-evidence -->
- YYYY-MM-DD — <subject/kurzer Titel> — messageId: <...>
<!-- END:managed-evidence -->
```

## `workpackages/<wp-id>-<slug>.md` (Template)

```md
# <WP-ID> — <Title>

## Scope

- Lead: <Institution | optional>
- BOKU-Rolle: <Role | optional>
- Status: active | completed | planned | paused
- WP-Nummer: <positive number | optional>

## Tasks

- <T1.1> — <Title> — Lead: <optional>

## Deliverables

- <D1.1> — <Title> — Type: <optional> — Due: <optional> — Lead: <optional>

## Kontakte und Routing-Signale

- Kontakte:
- Aliases:
- Keywords:

## Projektweite Milestones

Milestones werden ausschließlich auf Projektebene im Katalog geführt. Hier nur auf relevante IDs verweisen, beispielsweise `MS1 — Kick-off held`.

## Aktueller Stand (Managed)

<!-- BEGIN:managed-workpackage-summary -->
-
<!-- END:managed-workpackage-summary -->

## Offene Punkte (Managed)

<!-- BEGIN:managed-workpackage-open-items -->
-
<!-- END:managed-workpackage-open-items -->

## Evidenz (Managed)

<!-- BEGIN:managed-workpackage-evidence -->
-
<!-- END:managed-workpackage-evidence -->
```
