# Calendar-Desk — L2 System Map (ICM Form 6)

**Standalone-Guard (#38):** Der explizite Aufruf- oder API-Installationspfad geht dem
gemeinsamen Operator-Environment `WORKSPACE_LOCK_ROOT` vor; danach folgt der
kompatible Sibling-Fallback des installierten Bundles (`../workspace-lock`).
Fehlende Installation liefert begrenzte Setup-Hilfe auf `WORKSPACE_LOCK_ROOT` und die
Bundle-README. Kein CWD-/Payload-Fallback; Ownership, Guard und Zwei-Phasen-Recovery
bleiben unverändert fail-closed.

> **Metrik (DOC-M1):** 18 getrackte Dateien (`git ls-files skills/calendar-desk`) ·
> 152 Testmethoden in 6 Testmodulen (`test_ical_codec.py`, `test_injection_fence.py`,
> `test_backend_config.py`, `test_caldav_client.py`, `test_freebusy.py`,
> `test_write_engine.py`) plus Fixture-Satz `calendar-record-v1.json`.
> Diese Kopfzeile ist die kanonische Metrik-SSOT; L1 nennt nur die Referenz auf diese Karte.
>
> **Typ:** ICM Form 6, L2 des Sub-Skills `calendar-desk`  
> **Gültig für:** `skills/calendar-desk/` (Bundle-relativ)

---

## 1. Rolle und Grenze

`calendar-desk` besitzt die Integration mit CalDAV-Servern (RFC 4791) und iCalendar-Objekten (RFC 5545):
Kalender-Discovery, Agenda- und Ereignisabfragen, Free/Busy-Berechnungen, Slot-Suche sowie
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
   ├── memory/evidence/calendar/   (*.plan.json, *.apply-receipt.json, Snapshots)
   └── data/calendar-desk/         (recovery/calendar-journal.json, staging/)
```

### Abgrenzung
- **Kein direkter ungeschützter Write:** Kein Skript führt unkonditionierte Netzwerk-PUTs aus; jede Mutation erzwingt vorherige Planung und ETag-Preconditions.
- **Keine Aufgaben- oder Meeting-Erzeugung:** `calendar-desk` synchronisiert Termine und Freizeiten; inhaltliche Sitzungsprotokolle verbleiben bei `meeting-desk`, Action Items bei `task-desk`.
- **Keine Agent-Control-Plane:** Berechtigungs- und Session-Verwaltung liegt vollständig beim konsumierenden Workspace via `workspace-lock`.

---

## 2. Nomen (Objekte und Schemas)

| Objekt | Ort / Schema | Wesentliche Regeln |
| :--- | :--- | :--- |
| `calendar_object_ref` | Python Dict / Schema | Kanonisches 4-Key-Dict `{"uid", "sequence", "recurrence_id", "content_sha256"}`; bit-identisch zwischen `calendar-desk` und `mail-desk` (`calendar_extract.py`). |
| `backend-config` | `.agents/calendar-desk-backend.json` | Account-Konfiguration mit `caldav_url`, `username`, `auth_type: "basic"`, `default_calendar`, `read_only` (bool) und Kalender-Aliassen. |
| `secrets` | `.agents/secrets.json` | Enthält sensible Zugangsdaten (Passwörter/Tokens), getrennt von der nicht-sensiblen Backend-Konfiguration. |
| `write_plan` | `memory/evidence/calendar/*.plan.json` | Plan-Manifest mit `plan_id`, `action` (`create`, `update`, `delete`, `respond`), `target_url`, `etag_precondition`, `fenced_payload_sha256` und simuliertem Diff. |
| `apply_receipt` | `memory/evidence/calendar/*.apply-receipt.json` | Unveränderliche Ausführungsquittung mit `applied_at`, `status: "applied"`, `new_etag`, `content_sha256` und `journal_digest`. |
| `recovery_journal` | `data/calendar-desk/recovery/calendar-journal.json` | Schema `calendar-desk-journal/1`: Integritätsdigest, per-Step Pre-/Poststate-Hashes, ETag-Tracking und Resumability. |
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
   Write Plan Manifest (memory/evidence/calendar/<id>.plan.json)
   Output Plan Receipt (Exit 0, keine Netzwerkmutation)

Phase 2: Apply (mutierend, netzwerkgebunden)
   Verify Workspace Lock (require_workspace_lock, allow_legacy=False)
                           │
                           ▼
   Verify Plan Hash & Target Config (read_only == False)
                           │
                           ▼
   Open Recovery Journal (data/calendar-desk/recovery/calendar-journal.json)
                           │
                           ▼
   Execute HTTP Mutation via CalDAVClient (PUT / DELETE mit ETag Header)
         │
         ├── HTTP 412 Precondition Failed ──► Rollback Journal ──► PreconditionFailed (Exit 2)
         │                                                        (kein Überschreiben fremder Änderungen)
         └── HTTP 200/201/204 Success ──► Write Apply Receipt (memory/evidence/calendar/<id>.apply-receipt.json)
                                                   │
                                                   ▼
                                          Close & Remove Journal ──► Applied (Exit 0)
```

---

## 4. Seiteneffekte & Invarianten

1. **`read_only`-Schutz:** Ein als `read_only: true` markierter Kalender verweigert Plan- und Apply-Operationen deterministisch mit Stopcode `CALENDAR_READ_ONLY`.
2. **ETag-Preconditions & 412-Konflikt-Handling:**
   - Neuanlage erzwingt `If-None-Match: *`. Existiert bereits eine Ressource, schlägt der Aufruf mit HTTP 412 fehl.
   - Aktualisierung und Löschung erzwingen `If-Match: "<etag>"`. Weicht der Serverzustand ab, schlägt der Aufruf fehl; fremde Modifikationen werden niemals blind überschrieben.
3. **Workspace-Lock Ownership:** Vor jeder mutierenden Phase (`apply`) prüft die Engine zwingend die aktive Lease via `require_workspace_lock(allow_legacy=False)`. Ein lockfreier Bypass existiert nicht.
4. **Untrusted Calendar Content Fencing:** Fremde Texteigenschaften (`SUMMARY`, `DESCRIPTION`, `LOCATION`, `ATTENDEE`) werden mit `<untrusted_calendar_content>` umschlossen, um Prompt-Injection-Angriffe in nachgelagerten Agenten-Kontexten abzuwehren.
5. **Crash- und Recovery-Garantie:** Bei Verbindungsabbruch oder Prozessabsturz während des Writes dokumentiert das Journal den unvollständigen Schritt. Nachfolgende Schreibversuche blockieren fail-closed (`recovery_required`), bis eine explizite Inspektion (`inspect`) oder Wiederherstellung (`recover`) erfolgt ist.

---

## 5. Integration mit Nachbar-Skills

- **`mail-desk` (.ics Bridge, CD-4):**
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
- `test_backend_config.py`: Laden von `.agents/calendar-desk-backend.json`, Zielauflösung, `read_only`.
- `test_caldav_client.py`: HTTP-Mocking, Discovery, Reports, ETag-Preconditions, Statuscode-Handling.
- `test_freebusy.py`: Slot-Suche, Arbeitszeiten, Puffer, Überlappungsauflösung.
- `test_write_engine.py`: Zwei-Phasen-Ablauf (`plan` -> `apply`), Journaling, 412-Handling, Lock-Prüfung.
