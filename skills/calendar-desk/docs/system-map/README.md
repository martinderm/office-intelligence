# Calendar-Desk — L2 System Map (ICM Form 6)

**Standalone-Guard (#38):** Der explizite Aufruf- oder API-Installationspfad geht dem
gemeinsamen Operator-Environment `WORKSPACE_LOCK_ROOT` vor; danach folgt der
kompatible Sibling-Fallback des installierten Bundles (`../workspace-lock`).
Fehlende Installation liefert begrenzte Setup-Hilfe auf `WORKSPACE_LOCK_ROOT` und die
Bundle-README. Kein CWD-/Payload-Fallback; Ownership, Guard und Zwei-Phasen-Recovery
bleiben unverändert fail-closed.

> **Metrik (DOC-M1):** 19 getrackte Dateien (8 Core-Module unter scripts/core/, 1 CLI-Entrypoint scripts/calendar_desk_client.py, 6 Testmodule mit 237 Testmethoden, 4 Dokumentations-/Referenzdateien) ·
> 6 Testmodule mit 237 Testmethoden (`test_ical_codec.py`, `test_injection_fence.py`,
> `test_backend_config.py`, `test_caldav_client.py`, `test_freebusy.py`,
> `test_write_engine.py`) plus Fixture-Satz `calendar-record-v1.json`.
> Diese Kopfzeile ist die kanonische Metrik-SSOT; L1 nennt nur die Referenz auf diese Karte.
>
> **Typ:** ICM Form 6, L2 des Sub-Skills `calendar-desk`  
> **Gültig für:** `skills/calendar-desk/` (Bundle-relativ)

---

## 1. Rolle und Grenze

`calendar-desk` besitzt die Integration mit CalDAV-Servern (RFC 4791) und iCalendar-Objekten (RFC 5545):
Kalender-Discovery via `PROPFIND Depth: 1` auf der konfigurierten `principal_url`, Agenda- und Ereignisabfragen, Free/Busy-Berechnungen, Slot-Suche sowie
den zustandsbehafteten Zwei-Phasen-Schreibpfad (`plan` ──► `apply`) mit ETag-Preconditions und
Recovery-Journal. **Eine** primäre CLI-Fassade: `scripts/calendar_desk_client.py`.

```text
CalDAV Server (RFC 4791 / RFC 5545)
   │  PROPFIND / REPORT / PUT / DELETE (HTTPS)
   ▼
calendar_desk_client.py ── validate / plan ── two-phase write engine
   │
   ├── core/backend_config.py      (.agents/calendar-desk-backend.json & secrets)
   ├── core/caldav_client.py       (Stdlib CalDAV HTTP Client)
   ├── core/ical_codec.py          (RFC-5545 Parser, Serializer, Unfolding, Hashes)
   ├── core/freebusy.py            (Free/Busy Aggregation, find-slots)
   ├── core/write_engine.py        (Zwei-Phasen-Engine, Recovery-Journal, Lock Guard)
   └── core/injection_fence.py     (<untrusted_calendar_content> Fencing)
   │
   ├── memory/evidence/calendar/   (calendars.json, raw/<account>/<calendar_alias>/<uid>.ics)
   └── data/calendar-desk/         (proposals/<plan_id>.json, journal/<plan_id>.json)
```

### Abgrenzung
- **Kein direkter ungeschützter Write:** Kein Skript führt unkonditionierte Netzwerk-PUTs aus; jede Mutation erzwingt vorherige Planung und ETag-Preconditions.
- **Keine Aufgaben- oder Meeting-Erzeugung:** `calendar-desk` synchronisiert Termine und Freizeiten; inhaltliche Sitzungsprotokolle verbleiben bei `meeting-desk`, Action Items bei `task-desk`.
- **Keine Agent-Control-Plane:** Berechtigungs- und Session-Verwaltung liegt vollständig beim konsumierenden Workspace via `workspace-lock`.

---

## 2. Nomen (Objekte und Schemas)

| Objekt | Ort / Schema | Wesentliche Regeln |
| :--- | :--- | :--- |
| `calendar_object_ref` | Python Dict / Schema | Kanonisches 4-Key-Dict `{"uid", "sequence", "recurrence_id", "content_sha256"}`; bit-identisch zwischen `calendar-desk` und `mail-desk` (`calendar_extract.py`). Vorbereitet für CD-4. |
| `backend-config` | `.agents/calendar-desk-backend.json` | Konfiguration mit `default_account`, `default_timezone`, `accounts.<id>.{principal_url, allow_loopback_http, timeout_seconds, calendars.<alias>.{collection_path, collection_url, display_name, role, write_policy}}`, wobei `write_policy` als `"read_only"` oder `"dry_run_and_approval"` definiert ist. Zugangsdaten stammen aus Umgebungsvariablen (`CALDAV_USERNAME_<ACCOUNT>`, `CALDAV_PASSWORD_<ACCOUNT>`, `CALDAV_PRINCIPAL_URL_<ACCOUNT>`) oder `secrets.json`. |
| `secrets` | `.agents/secrets.json` | Enthält sensible Zugangsdaten (Passwörter/Tokens), getrennt von der nicht-sensiblen Backend-Konfiguration. |
| `calendar_evidence` | `memory/evidence/calendar/calendars.json` | Evidenzregister nach Schema `calendar-desk-register/1` (`schema_version`, `updated_at`, `events[]` Register von angewandten und wiederhergestellten Terminen dedupliziert nach `(account, calendar_alias, uid)`). |
| `raw_calendar_evidence` | `memory/evidence/calendar/raw/<account>/<calendar_alias>/<uid>.ics` | Auditsichere Rohdaten der Kalenderereignisse. |
| `write_proposal` | `data/calendar-desk/proposals/<plan_id>.json` | Persistierter Plan mit `plan_id`, `action` (`create-event`, `update-event`, `cancel-event`, `delete-event`), `collection_url`, `etag_precondition`, `canonical_ics_sha256`, diff und `approval_template`. Planerstellung ist rein lesend (`--lease-id` optional). |
| `recovery_journal` | `data/calendar-desk/journal/<plan_id>.json` | Schema `calendar-desk-journal/1`: Integritätsdigest, per-Step Pre-/Poststate-Hashes, ETag-Tracking und Resumability. Phasen: `intent` -> `in_flight` -> `completed` (bleibt persistiert, wird nicht gelöscht). |
| `freebusy_slot` | JSON-Envelope `data.slots[]` | Verfügbares Zeitfenster mit `start`, `end`, `duration_minutes` (normalisiert in UTC). |

---

## 3. Verben & Zustandsmaschine (Zwei-Phasen-Write)

```text
Phase 1: Plan (read-only / evidence-only)
   Input Payload ──► Injection Fencing ──► RFC-5545 Serialization ──► Content-SHA256
                           │
                           ▼
   Resolve Target URL & ETag Precondition (If-None-Match: * oder If-Match: "<etag>")
                           │
                           ▼
   Write Plan Manifest (data/calendar-desk/proposals/<plan_id>.json)
   Output Plan Receipt (Exit 0, keine Netzwerkmutation, kein Lock erforderlich)

Phase 2: Apply (mutierend, netzwerkgebunden)
   Verify Workspace Lock (require_workspace_lock, allow_legacy=False)
                           │
                           ▼
   Verify Plan Hash & Target Config (write_policy == "dry_run_and_approval")
                           │
                           ▼
   Write Atomic Journal (intent -> in_flight) (data/calendar-desk/journal/<plan_id>.json)
                           │
                           ▼
   Execute HTTP Mutation via CalDAVClient (PUT / DELETE mit ETag Header)
         │
         ├── HTTP 412 Precondition Failed ──► Rollback Journal ──► PreconditionFailed (Exit 2)
         │                                                        (kein Überschreiben fremder Änderungen)
         ├── HTTP 5xx / 408 / 429 / CALDAV_NETWORK_ERROR ──► unknown_remote_outcome ──► RECOVERY_REQUIRED (Exit 3)
         │
         └── HTTP 200/201/204 Success ──► Update Local Evidence (calendars.json & raw/)
                                                   │
                                                   ▼
                                          Persist Completed Journal ──► Completed (Exit 0)
```

### Exit-Codes (`calendar_desk_client.py`)
- **Exit 0 (`Completed`):** Erfolgreich ausgeführte Operation.
- **Exit 1 (`Failed` / Error):** Allgemeine Fehler (`CalDAVConfigError`, `CalDAVProtocolError` sofern nicht 412, `ValueError`, unhandled `Exception`). Zudem scheitern `compute_freebusy` und `find_free_slots` fail-closed mit `CALDAV_INCOMPLETE_FREEBUSY` (Exit 1), wenn Termine übersprungen wurden (`skipped[]` nicht leer) oder die Abfrage trunkiert ist (`truncated: true`).
- **Exit 2 (`Failed` / PreconditionFailed):** Precondition- / Validierungs- / Policy-Fehler:
  - `CalendarWriteError` mit `code` in `("CALDAV_ETAG_CONFLICT", "CALENDAR_WRITE_FORBIDDEN", "WRITE_POLICY_VIOLATION")`
  - `CalDAVProtocolError` mit `status_code == 412`
  - Kalender mit `write_policy: "read_only"` lösen deterministisch `CALENDAR_WRITE_FORBIDDEN` aus.
- **Exit 3 (`Failed` / RecoveryRequired):** Wiederherstellung erforderlich:
  - `CalendarWriteError` mit `code == "RECOVERY_REQUIRED"` (unterbrochener Schreibvorgang / offenes Journal / `unknown_remote_outcome` bei Netzwerk- oder Serverfehlern).

---

## 4. Seiteneffekte & Invarianten

1. **`write_policy`-Schutz:** Ein als `write_policy: "read_only"` konfigurierter Kalender verweigert Plan- und Apply-Operationen deterministisch mit Stopcode `CALENDAR_WRITE_FORBIDDEN` (Exit 2). Nur `write_policy == "dry_run_and_approval"` erlaubt die Vorbereitung und Ausführung von Schreiboperationen.
2. **ETag-Preconditions & 412-Konflikt-Handling:**
   - Neuanlage erzwingt `If-None-Match: *`. Existiert bereits eine Ressource, schlägt der Aufruf mit HTTP 412 fehl (Exit 2).
   - Aktualisierung und Löschung erzwingen `If-Match: "<etag>"`. Weicht der Serverzustand ab, schlägt der Aufruf fehl (Exit 2); fremde Modifikationen werden niemals blind überschrieben.
3. **Workspace-Lock Ownership:** Vor jeder mutierenden Phase (`apply`, `recover`) prüft die Engine zwingend die aktive Lease via `require_workspace_lock(allow_legacy=False)`. Die Planphase (`plan`) schreibt lediglich einen unverbindlichen Vorschlag unter `data/calendar-desk/proposals/<plan_id>.json` und erfordert keinen Lock (`--lease-id` optional).
4. **Untrusted Calendar Content Fencing:** Fremde Texteigenschaften (`SUMMARY`, `DESCRIPTION`, `LOCATION`, `ATTENDEE`) werden mit `<untrusted_calendar_content>` umschlossen, um Prompt-Injection-Angriffe in nachgelagerten Agenten-Kontexten abzuwehren.
5. **Crash- und Recovery-Garantie:** Bei Verbindungsabbruch, HTTP 5xx/408/429 oder Netzwerkfehlern wendet die Engine `unknown_remote_outcome` an; das Journal unter `data/calendar-desk/journal/<plan_id>.json` dokumentiert den unvollständigen Schritt. Nachfolgende Schreibversuche blockieren fail-closed (`recovery_required`, Exit 3), bis eine explizite Inspektion (`inspect`) oder Wiederherstellung (`recover`) erfolgt ist. Erfolgreich ausgeführte Operationen aktualisieren die lokale Evidenz (`data/calendar-desk/calendars.json` nach Schema `calendar-desk-register/1` und `data/calendar-desk/raw/<account>/<calendar_alias>/<uid>.ics`) und setzen das Journal auf Zustand `completed` (das Journal bleibt für Revisionszwecke persistiert und wird nicht gelöscht).
6. **Incomplete Free/Busy-Garantie:** `query_events` liefert `skipped[]` und `truncated`. `compute_freebusy` und `find_free_slots` brechen fail-closed mit `CALDAV_INCOMPLETE_FREEBUSY` (Exit 1) ab, falls Termine übersprungen wurden oder das Abfragefenster unvollständig ist.

---

## 5. Integration mit Nachbar-Skills & Scope-Grenzen (#14)

- **Scope-Grenze (#14):**
  - Pakete CD-1 bis CD-3 sind vollständig implementiert: RFC-5545 Codec, CalDAV-Client, Free/Busy-Engine und Zwei-Phasen-Write-Engine inklusive kanonischem 6-Feld-Envelope und Exit-Codes 0/1/2/3.
  - CD-4 (Desk Bridges `event_dossier.py`, `ingest-ics`, `match-meeting`) und CD-5 (Batch-Scheduler / Background-Sync / iTIP-Automatisierung `respond-invitation`) sind als Restumfang in Issue #81 deklariert.
- **`mail-desk` (.ics Bridge, CD-4 Vorbereitung):**
  - Eingehende Kalenderanhänge (`.ics`, `text/calendar`, `application/ics`) werden über `skills/mail-desk/scripts/core/calendar_extract.py` extrahiert.
  - Das Modul ist stdlib-only und importiert kein `calendar-desk`.
  - Bit-identische Parität für `uid`, `sequence`, `recurrence_id` und `content_sha256` wird über die Testsuite `skills/mail-desk/tests/test_calendar_attachment_ingest.py` abgesichert.
- **`workspace-lock` (Integration, CD-4a):**
  - Exponiert `load_guard(trusted_root)` und `require_owned_workspace_lock` in `calendar_desk_client.py`.
  - Vollständige Kompatibilität mit dem isolierten Bundle-Testrunner `tests/test_standalone_workspace_lock.py`.

---

## 6. Verifikation & Testsuite

Die Testsuite unter `skills/calendar-desk/tests/` arbeitet hermetisch, stdlib-only und ohne externe Netzwerkverbindungen:
- `test_ical_codec.py`: RFC-5545 Parsing, Zeilenfaltung, Serialisierung, Zeitzonen, VEVENT/VALARM.
- `test_injection_fence.py`: Erkennung verdächtiger Muster, Fencing, Escaping.
- `test_backend_config.py`: Laden von `.agents/calendar-desk-backend.json`, Zielauflösung, `write_policy`.
- `test_caldav_client.py`: HTTP-Mocking, Discovery via `principal_url`, Reports, ETag-Preconditions, Statuscode-Handling.
- `test_freebusy.py`: Slot-Suche, Arbeitszeiten, Puffer, Überlappungsauflösung, `CALDAV_INCOMPLETE_FREEBUSY`.
- `test_write_engine.py`: Zwei-Phasen-Ablauf (`plan` -> `apply`), Journaling, 412-Handling, Lock-Prüfung.
