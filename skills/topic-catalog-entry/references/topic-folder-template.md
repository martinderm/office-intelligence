---
type: Topic
---

# Template — Topic Folder Layout

Für jedes Topic wird eine Arbeitsstruktur in beiden Säulen des Dual-Evidence-Standards angelegt:

- **Säule 1 (Referenzen & Struktur):**
  - `memory/references/topics/<id>/index.md`
  - `memory/references/topics/<id>/contacts.md`
  - `memory/references/topics/<id>/signals.md`
  - `memory/references/topics/<id>/subtopics/`
  - `memory/references/topics/<id>/subtopics/<subtopic-id>/operations/<operation-id>/index.md` (optional, nur für Dauerprozesse)
  - `memory/references/topics/<id>/subtopics/<subtopic-id>/events/<event-id>/index.md` (optional, nur für terminierte Events)
  - `memory/operations/topics/<id>/subtopics/<subtopic-id>/<operation-id>/` (aktiver Arbeitsstand)
- **Säule 2 (Evidenzen & Logs):**
  - `memory/evidence/topics/<id>/` (chronologische Logs `YYYY-MM.md`)

---

## `index.md` (Template)

```md
# <id> — <title>

Kurzbeschreibung (1–3 Sätze): Worum geht’s, warum ist das Topic routingrelevant?

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

- Kontakte: ./contacts.md
- Signale: ./signals.md
- Subtopics: ./subtopics/
- Evidenz-Log: ../../../evidence/topics/<id>/
```

## `contacts.md` (Template)

```md
# Contacts — <id>

- Name:
  - Rolle:
  - E-Mail:
  - Notiz:
```

## `signals.md` (Template)

```md
# Signals — <id>

## Routing-Signale

- Primäre Domains:
  -
- Schlüssel-Kontakte (Name <mail>):
  -
- Typische Betreffmuster:
  -
- Typische Begriffe / Abkürzungen:
  -

## Do-not-route / Ausschlüsse

- newsletter
- no-reply
- autoreply
```

## `subtopics/<subtopic-id>.md` (Template)

```md
# <subtopic-id> — <Title>

## Scope

- 

## Kontakte

- 

## Signale

- Aliases:
  -
- Keywords:
  -
- Typical subject patterns:
  -

## Stand (Managed)

<!-- BEGIN:managed-subtopic-summary -->
- 
<!-- END:managed-subtopic-summary -->
```

## `subtopics/<subtopic-id>/operations/<operation-id>/index.md` (Template)

```md
# <operation-id> — <Title>

## Scope

- Wiederkehrender/laufender Dauerprozess innerhalb von `<subtopic-id>`.

## Signale

- Aliases:
  -
- Keywords:
  -
- Typical subject patterns:
  -

## Referenzen & Evidenz

- Aktiver Arbeitsstand: `memory/operations/topics/<topic-id>/subtopics/<subtopic-id>/<operation-id>/`
- Mail-Evidence: `memory/evidence/topics/<topic-id>/subtopics/<subtopic-id>/operations/<operation-id>/`

## Stand (Managed)

<!-- BEGIN:managed-operation-summary -->
-
<!-- END:managed-operation-summary -->
```

## `subtopics/<subtopic-id>/events/<event-id>/index.md` (Template)

```md
# <event-id> — <Title>

## Eckdaten

- Start: YYYY-MM-DD
- Ende: YYYY-MM-DD (optional)
- Routingstatus: active | inactive
- Phase: planned | live | completed | cancelled
- Cloud-Storage: `<scope>:<storage-id>` (bestehender Katalogeintrag)

## Referenzen & Evidenz

- Event-Evidence: `memory/evidence/topics/<topic-id>/events/<event-id>/`
- Aufzeichnungen: `../../../../../../../evidence/topics/<topic-id>/events/<event-id>/recordings/`
- Notizen: `../../../../../../../evidence/topics/<topic-id>/events/<event-id>/notes/`
- Action Items: `../../../../../../../evidence/topics/<topic-id>/events/<event-id>/action-items.md`

## Stand (Managed)

<!-- BEGIN:managed-event-summary -->
-
<!-- END:managed-event-summary -->
```
