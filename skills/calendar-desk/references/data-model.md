# Datenmodell-Spezifikation: `calendar-record/1`

Dieses Dokument spezifiziert das kanonische Datenmodell `calendar-record/1` für `calendar-desk` innerhalb des `office-intelligence`-Skill-Bundles.

---

## 1. Schema-Übersicht

Das Schema `calendar-record/1` normalisiert rohe RFC-5545-iCalendar-Komponenten (`VEVENT`) in ein deterministisches, serialisierbares JSON-Format.

```json
{
  "schema": "calendar-record/1",
  "account": "infomaniak-work",
  "calendar_alias": "primary",
  "uid": "timed-20261110-001@office-intelligence.test",
  "recurrence_id": null,
  "sequence": 0,
  "etag": "\"2026-11-06T10:00:00Z-442\"",
  "href": "/calendars/user/primary/timed-20261110-001.ics",
  "method": null,
  "status": "CONFIRMED",
  "transp": "OPAQUE",
  "summary": "Team Strategieabstimmung Q4",
  "description": "Quartalsabstimmung für Office Intelligence.",
  "location": "Besprechungsraum Donau / Virtual",
  "start": {
    "utc": "2026-11-10T09:00:00Z",
    "local": "2026-11-10T10:00:00+01:00",
    "tzid": "Europe/Vienna",
    "all_day": false,
    "date": "2026-11-10"
  },
  "end": {
    "utc": "2026-11-10T10:30:00Z",
    "local": "2026-11-10T11:30:00+01:00",
    "tzid": "Europe/Vienna",
    "all_day": false,
    "date": "2026-11-10"
  },
  "duration_minutes": 90,
  "rrule": null,
  "exdates": [],
  "organizer": null,
  "attendees": [],
  "classification": "PUBLIC",
  "source_provenance": {
    "origin": "ics_import",
    "raw_ics_relpath": null,
    "ingested_at": null
  },
  "content_sha256": "518e065d4003bed87fd66b8d104945540b7b19f74e3bc1d643b35a9ef7c12625"
}
```

---

## 2. Feld-Definitionen

| Feld | Typ | Pflicht | Beschreibung |
| :--- | :--- | :--- | :--- |
| `schema` | `string` | Ja | Fest `"calendar-record/1"`. |
| `account` | `string` | Ja | Identifier des Quell-Accounts (z. B. `"infomaniak-work"`). |
| `calendar_alias` | `string` | Ja | Identifier des Kalenders (z. B. `"primary"`, `"editorial"`). |
| `uid` | `string` | Ja | Eindeutige RFC-5545-`UID` des Termins. |
| `recurrence_id` | `string \| null` | Nein | ISO-8601-UTC-String oder Datum der Ausnahmeinstanz (`RECURRENCE-ID`), sonst `null`. |
| `sequence` | `integer` | Ja | Revisionsnummer gemäß RFC 5545 (`>= 0`, Standard `0`). |
| `etag` | `string \| null` | Nein | CalDAV-ETag zur Concurrency-Steuerung. |
| `href` | `string \| null` | Nein | CalDAV-Ressourcenpfad. |
| `method` | `string \| null` | Nein | iTIP-Methode (`"PUBLISH"`, `"REQUEST"`, `"REPLY"`, `"CANCEL"`, oder `null`). |
| `status` | `string` | Ja | Termin-Status (`"CONFIRMED"`, `"TENTATIVE"`, `"CANCELLED"`). |
| `transp` | `string` | Ja | Zeitverfügbarkeit (`"OPAQUE"` = belegt, `"TRANSPARENT"` = frei). |
| `summary` | `string` | Ja | Titel / Betreff des Termins (Standard `""`). |
| `description` | `string` | Ja | Beschreibung / Notizen (Standard `""`). |
| `location` | `string` | Ja | Ort oder Meeting-Link (Standard `""`). |
| `start` | `object` | Ja | Startzeitpunkt-Endpunkt (siehe Abschnitt 3). |
| `end` | `object` | Ja | Endzeitpunkt-Endpunkt (siehe Abschnitt 3). |
| `duration_minutes` | `integer` | Ja | Dauer des Termins in vollen Minuten (`>= 0`). |
| `rrule` | `string \| null` | Nein | Roher RFC-5545-`RRULE`-Wert (z. B. `"FREQ=WEEKLY;BYDAY=MO,WE"`). |
| `exdates` | `list[string]` | Ja | Liste von ISO-8601-UTC-Strings oder Datumsangaben für Ausschlussdaten (`EXDATE`). |
| `organizer` | `object \| null` | Nein | Organisator des Termins (siehe Abschnitt 4). |
| `attendees` | `list[object]` | Ja | Liste der Teilnehmerobjekte (siehe Abschnitt 4). |
| `classification` | `string` | Ja | Vertraulichkeit (`"PUBLIC"`, `"PRIVATE"`, `"CONFIDENTIAL"`). |
| `source_provenance` | `object` | Ja | Herkunftsnachweis (Herkunftsdesk, Dateipfad, Zeitstempel). |
| `content_sha256` | `string` | Ja | 64-stelliger Kleinbuchstaben-Hex-SHA-256 des CRLF-normalisierten rohen `.ics`. |

---

## 3. Endpunkt-Objekt (`start` und `end`)

Sowohl `start` als auch `end` verwenden exakt dieselbe Struktur:

```json
{
  "utc": "2026-11-10T09:00:00Z",
  "local": "2026-11-10T10:00:00+01:00",
  "tzid": "Europe/Vienna",
  "all_day": false,
  "date": "2026-11-10"
}
```

### 3.1 Zeitgebundene Termine (`all_day: false`)
- `utc`: Kanonischer ISO-8601-UTC-String mit `Z`-Suffix (`YYYY-MM-DDTHH:MM:SSZ`).
- `local`: Lokaler ISO-8601-String inklusive Offset (`YYYY-MM-DDTHH:MM:SS+HH:MM`).
- `tzid`: IANA-Zeitzonenname (z. B. `"Europe/Vienna"`).
- `all_day`: `false`.
- `date`: Kalendertag des lokalen Beginns (`YYYY-MM-DD`).

### 3.2 Ganztägige Termine (`all_day: true`)
Ganztägige Termine basieren auf dem RFC-5545-Typ `VALUE=DATE`.
- `utc`: `null`.
- `local`: `null`.
- `tzid`: `null`.
- `all_day`: `true`.
- `date`: Datum im Format `YYYY-MM-DD`.
- **Exklusives Ende**: Gemäß RFC 5545 ist `DTEND` bei ganztägigen Terminen exklusiv. Ein 3-Tage-Termin vom 24. bis inklusive 26. Dezember hat `start.date = "2026-12-24"`, `end.date = "2026-12-27"` und `duration_minutes = 4320` (3 * 1440).

---

## 4. Teilnehmer & Organisator

### 4.1 `organizer`
```json
{
  "cn": "Dr. Martin Derm",
  "email": "martin@example.test",
  "cal_address": "mailto:martin@example.test"
}
```

### 4.2 `attendees`
```json
[
  {
    "cn": "Alice Berger",
    "email": "alice@example.test",
    "cal_address": "mailto:alice@example.test",
    "partstat": "ACCEPTED",
    "role": "REQ-PARTICIPANT",
    "rsvp": true,
    "cutype": "INDIVIDUAL"
  }
]
```
- `partstat`: Status der Teilnahme (`"ACCEPTED"`, `"DECLINED"`, `"TENTATIVE"`, `"NEEDS-ACTION"`).
- `role`: Rolle (`"REQ-PARTICIPANT"`, `"OPT-PARTICIPANT"`, `"NON-PARTICIPANT"`, `"CHAIR"`).
- `rsvp`: Boolean, ob eine Antwort angefordert wurde.
- `cutype`: Kalender-Benutzertyp (`"INDIVIDUAL"`, `"GROUP"`, `"RESOURCE"`, `"ROOM"`).

---

## 5. Angereicherte Sicherheits-Blöcke

Beim Durchlaufen des Fencing-Scanners (`skills/calendar-desk/scripts/core/injection_fence.py`) wird der Record um zwei Blöcke angereichert:

```json
{
  "fenced_content": {
    "summary": {
      "fenced": "<untrusted_calendar_content fence_version=\"1\" sha256=\"...\">\n...\n</untrusted_calendar_content>",
      "sha256": "4a7d...64hex",
      "escaped_tags": false
    },
    "description": { ... },
    "location": { ... }
  },
  "injection_scan": {
    "flagged": false,
    "findings": []
  }
}
```

Wenn Anomalien oder Injections gefunden werden, enthält `findings`:
```json
[
  {
    "field": "description",
    "category": "prompt_injection",
    "detail": "Matched prompt injection indicator: ignore previous instructions"
  }
]
```

---

## 6. Handoff-Objekt: `calendar_objects`

Für Querverweise zwischen Skills (`mail-desk`, `event-documentation`, `project-catalog-entry`, `topic-catalog-entry`) wird ein minimaler Referenz-Deskriptor verwendet:

```json
{
  "uid": "timed-20261110-001@office-intelligence.test",
  "sequence": 0,
  "recurrence_id": null,
  "content_sha256": "518e065d4003bed87fd66b8d104945540b7b19f74e3bc1d643b35a9ef7c12625"
}
```
Dieser Deskriptor erlaubt es nachfolgenden Prozessen, die unveränderte Byte-Identität der Quelldaten nachzuvollziehen.
