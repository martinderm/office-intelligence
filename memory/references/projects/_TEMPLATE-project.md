# Template — Project Folder Layout

Für jedes Projekt wird ein eigener Ordner angelegt:

- `memory/references/projects/<id>/index.md`
- `memory/references/projects/<id>/signals.md`
- `memory/references/projects/<id>/evidence/` (monatliche Logs)
- `memory/references/projects/<id>/workpackages/` (Workpackages)

---

## `index.md` (Template)

```md
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

## Referenzen

- Signale: ./signals.md
- Evidenz-Log: ./evidence/
- Workpackages: ./workpackages/
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

## Konsolidierte Signale (Managed)

<!-- BEGIN:managed-signals -->
- Letzter relevanter Mailkontakt:
- Relevante Teilnehmer:innen:
  -
- Häufige Themen/Cluster:
  -
- Aktuelle nächste Schritte:
  -
- Risiken/Blocker:
  -
<!-- END:managed-signals -->
```

## `evidence/YYYY-MM.md` (Template)

```md
# Evidence — <id> — YYYY-MM

<!-- BEGIN:managed-evidence -->
- YYYY-MM-DD — <subject/kurzer Titel> — messageId: <...>
<!-- END:managed-evidence -->
```

## `workpackages/<wp-id>-<slug>.md` (Template)

```md
# <WP-ID> — <Title>

## Scope

- 

## Kontakte

- 

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
