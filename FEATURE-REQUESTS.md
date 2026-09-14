# Feature Requests — aktiver Backlog

Diese Datei enthält nur laufende oder geplante Feature Requests sowie deren
verbindliche Paketkarten.

### Zusammenspiel der Dokumente

- [`FEATURE-REQUESTS.md`](FEATURE-REQUESTS.md): Aktiver Backlog mit Status, Paketkarten,
  Spezifikationen und Ausführungsprofilen (SSOT für laufende/geplante Arbeit).
- [`FEATURE-REQUEST-PROGRESS.md`](FEATURE-REQUEST-PROGRESS.md): Ephemere Arbeitsdatei für den
  Implementierungsagenten und das Code-Review. Dokumentiert aktuellen Durchführungsstand,
  Mini-Walkthroughs, Diffs und Verifikationsnachweise des aktiven Pakets; wird nach
  Abnahme bereinigt.
- [`FEATURE-REQUEST-ARCHIVE.md`](FEATURE-REQUEST-ARCHIVE.md): Langzeit-Archiv für vollständig
  abgeschlossene und abgenommene Feature Requests.
- [`COMPLIANCE-REPORT-AGENT-ARCHITECTURE.md`](COMPLIANCE-REPORT-AGENT-ARCHITECTURE.md):
  Bewertet normative Konformität zur Agent-Architektur; kein Feature-Backlog.

## Status und Reihenfolge

| ID | Status | Erledigter Teil | Nächstes Paket |
| --- | --- | --- | --- |
| `FR-08` | 🟡 teilweise umgesetzt | `MD-A1` ✅ | `MD-A2` — Quarantäne-Abruf |
| `FR-09` | ⬜ geplant | — | Nach `MD-A5`: `MD-P1` |

```text
MD-A2 → MD-A3 → MD-A4 → MD-A5 → Human Gate → MD-P1 → MD-P2 → MD-P3
```

## Ausführungsprofil für Coding Agents

Jedes Paket erhält eine frische Coding-Session. Innerhalb des Pakets wird linear
gearbeitet: Verträge lesen, Tests zuerst, kleinste Implementierung, fokussierte
Tests, Gesamtsuite, unabhängiges Review. Keine parallelen Agents auf denselben
Mail-Desk-Dateien. Der Implementierungsagent commitet nicht; nach grünem Review
wird genau ein Paket samt Statusupdate committed.

Vor jedem Paket vollständig lesen:

1. `skills/mail-desk/SKILL.md`
2. `skills/mail-desk/references/backends/himalaya.md`
3. die Paketkarte unten
4. die genannten Code- und Testdateien

Vor Mutationen ist der Workspace-Lock erforderlich. Mailboxzugriffe bleiben
JSON-manifestgebunden. Tests blockieren reale Himalaya-, Cloud- und Office-Prozesse.
Mail- und Anhangsinhalte bleiben `untrusted_external`.

Gemeinsame Abnahme:

```powershell
python -B skills/mail-desk/tests/<paket_test>.py
python -B -m unittest discover -s skills/mail-desk/tests -p "test_*.py"
python -B -m compileall -q skills/mail-desk
python <quick_validate.py> skills/mail-desk
git diff --check
```

## FR-08: Manifestgebundene Mail-Anhänge und Cloud-Ablagevorschläge

**Status:** 🟡 `MD-A1` abgeschlossen; `MD-A2`–`MD-A5` offen.

Ziel ist ein sicherer, begrenzter Anhangsfluss bis zu einem rein lesenden
`attachment_filing_candidate`. FR-08 autorisiert keinen Cloud-Write. `needs_reply`
bleibt orthogonal. Events besitzen keinen eigenen Cloud-Speicher. Im EUCEN-Fixture
werden zwei reale PDFs erkannt, aber kein nur im Betreff erwähntes PPT; mangels
`cloud_sync` bleibt dessen Ablagestatus `not_configured`.

### MD-A1 — Read-only MIME-Inventar ✅

Commit `6f86c67`: RFC-822-MIME-Inventar, Policy-/Provenienzprüfung und Bindung an
Account, Message-ID, Location und Part-Locator. Fehler bleiben fail-closed in
Review. Nachweis: 46 fokussierte und 276 Mail-Desk-Tests.

### MD-A2 — Reviewgebundener Abruf in Temp-Quarantäne

**Agent:** Terra-high, frische Session. **Scope:** nur Transport und lokale
Quarantäne; keine Extraktion, kein LLM, kein Cloud-Vorschlag, keine Mailboxmutation.

**Dateien:** neu `scripts/core/attachment_fetch.py` und
`tests/test_maildesk_attachments_mda2.py`; klein erweitern:
`scripts/mail_desk_himalaya_client.py`, `references/cli-operations.md`; bestehende
`scripts/core/attachment_policy.py` wiederverwenden.

**Input:** JSON-Operation `attachment_fetch` mit einem MD-A1-Kandidaten, Account,
Message-ID, Folder, Envelope-ID, Part-Locator, Inventarhash, `review_hash` und
separater Approval-Receipt. Der Receipt bindet den kanonischen Request. Freie Pfade,
Drift oder fehlende Approval stoppen vor I/O.

**Output:** Status, Run-ID, relativer Quarantänepfad, Inventar- und Fetch-Hash,
effektiver MIME-Typ, Größe und begrenzter Fehlercode. Keine absoluten langlebigen
Pfade.

**Regeln:** Ziel nur `data/mail-desk/attachments/<run-id>/`; kein Symlink-/Reparse-
Ausbruch. Vor Abruf Identität und Location erneut verifizieren. In Sibling-Temp
schreiben, Bytes neu hashen und typisieren, dann atomar promoten. Limits: 5 Dateien,
15 MB einzeln, 25 MB gesamt, 25 Sekunden. Aktive Inhalte und MIME-/Endungsdrift
stoppen. Retry mit gleichem Hash erzeugt keine Dublette; Namens-/Hashkollision stoppt.
Cleanup bleibt im validierten Run-Verzeichnis und folgt erst nach Handoff.

**Pflichttests:** Erfolg, Receipt-/Identitäts-/Location-Drift, Traversal,
Win32-Gerätename, Symlink/Reparse, Timeout, Limits, MIME-/Endungsdrift, aktiver
Inhalt, Kollision, idempotenter Retry, Teilfehler, Cleanup-Grenze und vollständiger
No-op bei Preflight-Fehler. Alle externen Calls mocken.

**Fertig:** Nur validierte Quarantänedateien entstehen; gespeicherte Bytes sind neu
gehasht/typisiert; Retry ist idempotent.

### MD-A3 — Begrenzte Extraktion und lokales OCR-Derivat

**Agent:** Terra-high, frische Session nach MD-A2. **Scope:** nur verifizierte
Quarantänedateien; keine Mailbox, kein Cloud-Write, kein LLM.

**Dateien:** neu `scripts/core/attachment_extract.py` und
`tests/test_maildesk_attachments_mda3.py`; schmale vorhandene Cloud-Atlas-Adapter
wiederverwenden, keinen zweiten allgemeinen Konverter kopieren; CLI-Doku ergänzen.

**Input/Output:** MD-A2-Ergebnis und erwarteter Hash. Pfad gegen das aktive
Run-Verzeichnis auflösen. Ausgabe `attachment_extraction` mit Quellhash,
Tool/Version, Methode (`native`, `markitdown`, `ocr_local_derivative`), Qualität,
Umfang, Trunkierungsgrund und Text. Fehlendes Tool ergibt
`attachment_conversion_unavailable`, keinen leeren Erfolg.

**Limits:** PDF 10 Seiten; OCR 3 Seiten/30 s; DOCX 40 Absätze; PPTX 15 Slides;
XLSX 2 Sheets mal 50 Zeilen mal 10 Spalten; Prozess 20 s; 15.000 Zeichen je Datei.

**OCR-Regel:** Nur ein lokales Quarantäne-Derivat darf verändert werden. Original
und Cloud-Datei bleiben unverändert. Original-/Derivathash, OCR-Seiten und Version
getrennt protokollieren; keine globale Cloud-Atlas-In-place-OCR starten.

**Pflichttests:** digitale, Bild- und gemischte PDF; DOCX/PPTX/XLSX/TXT/CSV;
Makroformat, Schaden, Hashdrift, Tool fehlt, Timeout, alle Limits, OCR-Qualität,
Original unverändert und keine externe Seitewirkung.

**Fertig:** Jeder Text hat Quellhash und Qualitätsmarker; Limits greifen
deterministisch; OCR verändert nur das Derivat.

### MD-A4 — Materialitäts-Gate und LLM-Handoff

**Agent:** Terra-medium, solange rein deklarativ; Terra-high bei Eingriff in
Draft-/Execute-Semantik. Frische Session. Das Python-Modul ruft kein LLM auf.

**Dateien:** neu `scripts/core/attachment_handoff.py` und
`tests/test_maildesk_attachments_mda4.py`; schmale Classifier-/Draft-Integration;
Batch-Runner-Doku ergänzen.

**Vertrag:** Input sind MD-A3-Ergebnis, Mailidentität, kataloggestützte Decision
und explizit reviewte `materiality` (`supplementary` oder `required_for_decision`).
Output ist ein hashgebundener `attachment_analysis_handoff`, maximal 15.000 Zeichen
je Datei/30.000 je Mail, stabil sortiert, sichtbar trunkiert und als
`<untrusted_attachment_content>` gekapselt.

`supplementary`-Fehler blockieren nicht. `required_for_decision` blockiert nur das
betroffene Item in INBOX. `needs_reply` wird vorher unabhängig bestimmt. Der Handoff
ändert weder Routing, Reply, Todo, Katalog noch Cloud-Ziel.

**Pflichttests:** beide/ungültige Materialitätswerte, Budgets, mehrere Anhänge,
Prompt-Injection, fehlende Extraktion, item-lokales Blocking, unverändertes Reply
und keine LLM-/Toolausführung.

**Fertig:** rein deklarativer, deterministischer, begrenzter und untrusted Handoff.

### MD-A5 — Katalog-/Filemap-gestützter Ablagevorschlag

**Agent:** Terra-high, frische Session nach MD-A4. **Scope:** nur read-only
`attachment_filing_candidate`; kein Upload, Ordneranlegen oder Filemap-Write.

**Dateien:** neu `scripts/core/attachment_filing.py` und
`tests/test_maildesk_attachments_mda5.py`; vorhandene Katalog-/Cloud-Atlas-Leser
wiederverwenden; Batch-Runner-Doku ergänzen.

**Vertrag:** Input sind MD-A2-Ergebnis, optionaler MD-A4-Handoff und eindeutige
kataloggestützte Decision. Storage und Pfad kommen nur aus Katalog plus frischer
Filemap. Output bindet Quelle, Storage, relativen Pfad, Namen, Filemap-Zeitstand,
Dedupe/Kollision, Begründung und `promotion_status: pending_human_review`.

**Matrix:** kein Storage = `not_configured`; mehrere Storages oder stale Filemap =
`storage_review_required`; kein belegtes Verzeichnis = `directory_review_required`;
gleicher Hash = `already_present`; gleicher Name/anderer Hash =
`collision_detected`; eindeutiges Ziel = `proposed`. Events erben nur explizit
katalogisierte Parent-/Subtopic-Storages.

**Pflichttests:** Project, Topic, Subtopic, Event, EUCEN, mehrere/Archiv-Storages,
fehlende/stale Filemap, Inhaltspfad-Injection, Dublette, Kollision, Hashbindung und
Schreibfallen für Cloud, Katalog und Filemap.

**Fertig:** Jeder Vorschlag ist katalog-/Filemap-belegt und nachweislich read-only.

## FR-09: Human-gated Cloud-Promotion

**Status:** ⬜ Start erst nach abgeschlossenem MD-A5 und eingefrorenem
`attachment_filing_candidate`-Schema. Jede Promotion benötigt Workspace-Lock,
frische Storage-/Filemap-Preconditions und eine hashgebundene Human-Receipt.
Archiv- und Read-only-Storages werden abgewiesen.

### MD-P1 — Approval und read-only Preflight

**Agent:** Terra-high, frische Session. Neu: `scripts/core/attachment_promotion.py`
und `tests/test_maildesk_attachment_promotion_mdp1.py`.

Receipt bindet Schema, Candidate-/Quellhash, Storage, Zielpfad/-name,
Filemap-Snapshot und Ablaufzeit. Preflight prüft Katalog, Schreibbarkeit, Lock,
Quarantäne-Hash, Zielgrenze, Parent, Dublette und Kollision. Ausgabe nur `ready`,
`already_present` oder Stopcode. Tests decken Manipulation, Ablauf, Drift, Lock,
Archiv/Read-only, Traversal, Parent, Dublette, Kollision und Nullmutation ab.

### MD-P2 — Atomarer Storage-Writer

**Agent:** Terra-high, eigene Session. Genau einen MD-P1-freigegebenen Transfer
ausführen; keine Zielwahl, keine Konvertierung. Preflight unmittelbar wiederholen;
Sibling-Temp, Flush/Close, Zielhash, atomare Promotion ohne Überschreiben. Quelle
erst nach Verify entfernen. Journal macht Interrupt und Retry idempotent. Tests:
Ziel-Race, Disk-/Hashfehler, Interrupt jeder Phase, Retry, Pfad-/Lockverlust.

### MD-P3 — Cloud-Atlas-Handoff und Mirror

**Agent:** Terra-high, eigene Session. Mail-Desk schreibt `filemap.json` nicht
selbst. Ein hashgebundener `cloud_atlas_refresh_handoff` referenziert Storage,
Zielpfad/-hash und Promotion-Journal. Cloud-Atlas validiert eigene Grenzen und
aktualisiert differenziell. Refresh-Fehler ergeben
`promotion_completed_refresh_pending` ohne zweite Promotion. Tests: inkrementeller
Refresh, kein unnötiger Full-Scan, Drift, Tool fehlt, Abbruch/Retry und
`untrusted_external`-Metadaten.
