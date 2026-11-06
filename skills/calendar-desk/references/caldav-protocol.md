# Protokoll-Spezifikation: CalDAV (RFC 4791) & iCalendar (RFC 5545)

Dieses Dokument spezifiziert die Protokollanforderungen, Concurrency-Regeln und Sicherheitspraktiken für die CalDAV-Integration von `calendar-desk`.

---

## 1. Discovery und Bootstrap (RFC 4791 §5)

CalDAV basiert auf WebDAV (RFC 4918) und nutzt HTTP-Methoden zur Navigation in Kalenderressourcen.

### 1.1 Discovery-Kette
1. **Principal-Discovery**:
   - `PROPFIND` auf die Basis-URL mit Header `Depth: 0`.
   - Abfrage von `<D:current-user-principal/>`.
2. **Calendar-Home-Set**:
   - `PROPFIND` auf den Principal-URL mit Header `Depth: 0`.
   - Abfrage der Eigenschaft `<C:calendar-home-set xmlns:C="urn:ietf:params:xml:ns:caldav"/>`.
3. **Kalender-Katalog**:
   - `PROPFIND` auf den Home-Set-URL mit Header `Depth: 1`.
   - Filterung nach Sammlungen mit Ressourcentyp `<D:resourcetype><D:collection/><C:calendar/></D:resourcetype>`.

### 1.2 XML-Namespaces
- Standard-WebDAV: `xmlns:D="DAV:"`
- CalDAV: `xmlns:C="urn:ietf:params:xml:ns:caldav"`

---

## 2. Abfragen via `REPORT`

### 2.1 Zeitbereichsabfragen (`calendar-query`)
Zur gezielten Synchronisation eines Fensters wird ein `REPORT`-Aufruf mit `<C:calendar-query>` gesendet:

```xml
<C:calendar-query xmlns:D="DAV:" xmlns:C="urn:ietf:params:xml:ns:caldav">
  <D:prop>
    <D:getetag/>
    <C:calendar-data/>
  </D:prop>
  <C:filter>
    <C:comp-filter name="VCALENDAR">
      <C:comp-filter name="VEVENT">
        <C:time-range start="20261101T000000Z" end="20261201T000000Z"/>
      </C:comp-filter>
    </C:comp-filter>
  </C:filter>
</C:calendar-query>
```

### 2.2 Stapelabruf (`calendar-multiget`)
Wenn mehrere UIDs oder Pfade bekannt sind, erfolgt der koordinierte Abruf ohne Einzel-GETs über `<C:calendar-multiget>`:

```xml
<C:calendar-multiget xmlns:D="DAV:" xmlns:C="urn:ietf:params:xml:ns:caldav">
  <D:prop>
    <D:getetag/>
    <C:calendar-data/>
  </D:prop>
  <D:href>/calendars/user/primary/event-1.ics</D:href>
  <D:href>/calendars/user/primary/event-2.ics</D:href>
</C:calendar-multiget>
```

---

## 3. Schreibabsicherung & Concurrency Control

Um konkurrierende Schreibkonflikte („Lost Updates“) zwischen parallelen Clients auszuschließen, sind HTTP-Vorbedingungen (`If-Match` / `If-None-Match`) zwingend vorgeschrieben:

### 3.1 Neuanlage von Terminen (`PUT`)
- Header: `If-None-Match: *`
- Verhindert das Überschreiben eines bereits existierenden Termins mit derselben UID.
- Antwort bei Kollision: `412 Precondition Failed`.

### 3.2 Aktualisierung existierender Termine (`PUT`)
- Header: `If-Match: "<etag>"`
- Vor dem Senden muss `SEQUENCE` im VEVENT-Objekt inkrementiert werden (`update_vevent_ics`).
- Liegt serverseitig bereits eine neuere ETag-Version vor, antwortet der Server mit `412 Precondition Failed`.
- Das System fängt diesen Status deterministisch als Fehlercode `CALDAV_ETAG_CONFLICT` ab. Der Client muss den aktuellen Serverstand neu laden, Zusammenführungen durchführen und erneut versuchen.

### 3.3 Löschen von Terminen (`DELETE`)
- Header: `If-Match: "<etag>"`
- Stellt sicher, dass nicht versehentlich ein Termin gelöscht wird, der zwischenzeitlich von einem anderen Client verändert wurde.

---

## 4. Sicherheit & Parser-Hygiene

### 4.1 Schutz gegen XML External Entity (XXE) Injections
Bevor XML-Antworten vom Server an den XML-Parser übergeben werden:
- Vorprüfung: Jede Antwort wird auf das Vorkommen von `<!DOCTYPE` oder `<!ENTITY` geprüft.
- Wird eine DTD-Deklaration oder Entity-Referenz erkannt, bricht der Parser fail-closed ab (`CALDAV_SECURITY_ERROR`).
- In Python wird `defusedxml` bevorzugt oder standardmäßig mit deaktivierter Entity-Auflösung geparst.

### 4.2 Antwort-Größenbegrenzung
- Maximale Größe einer einzelnen Server-Antwort: **4 MiB (4.194.304 Bytes)**.
- Übersteigt eine Antwort diese Grenze, bricht der Request ab (`CALDAV_OVERSIZED_RESPONSE`), um Memory-Exhaustion-Angriffe abzuwehren.

### 4.3 Zensur von Zugangsdaten
- Autorisierungs-Header (`Authorization: Basic ...` oder `Bearer ...`), Passwörter und private Tokens dürfen unter keinen Umständen in Logdateien, Tracebacks oder Ausnahme-Objekten auftauchen.
- Fehlermeldungen zensieren URLs mit eingebetteten Benutzerdaten deterministisch: `http://user:pass@host` -> `http://user:[REDACTED]@host`.

---

## 5. iTIP-Integration (RFC 5546) & Seam-Vorgaben

### 5.1 Rollen und Statuswerte
Termineinladungen über das iTIP-Protokoll transportieren Einladungs- und Rückmeldungszustände:
- `METHOD:REQUEST`: Einladung oder Terminänderung durch den Organisator.
- `METHOD:REPLY`: Rückmeldung eines Teilnehmers über seinen Teilnahmestatus (`PARTSTAT`).
- `METHOD:CANCEL`: Absage des Termins oder einer einzelnen Ausnahmeinstanz.

### 5.2 Seam für Phase 2
In Phase 1 (`CD-1`) werden alle iTIP-Felder (`method`, `partstat`, `rsvp`, `role`, `cutype`) im Datenmodell `calendar-record/1` vollständig geparst und roundtrip-fähig erhalten. Die automatische Erzeugung von `REPLY`-Antworten und Mailversand erfolgt in Phase 2 in Kooperation mit `mail-desk`.
