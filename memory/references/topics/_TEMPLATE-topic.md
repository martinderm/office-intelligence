# Template — Topic Folder Layout

Für jedes Topic wird ein eigener Ordner angelegt:

- `memory/references/topics/<id>/index.md`
- `memory/references/topics/<id>/contacts.md`
- `memory/references/topics/<id>/signals.md`
- `memory/references/topics/<id>/subtopics/`
- optional: `memory/references/topics/<id>/evidence/`

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

## Referenzen

- Kontakte: ./contacts.md
- Signale: ./signals.md
- Subtopics: ./subtopics/
- Evidenz-Log: ./evidence/
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

- 

## Stand (Managed)

<!-- BEGIN:managed-subtopic-summary -->
- 
<!-- END:managed-subtopic-summary -->
```
