# Cloud-Atlas — Subsystem System Map: Seiteneffekte (Effects)

> **Typ**: ICM Form 6 (`system-map`), Dimension: Seiteneffekte  
> **Subsystem**: [`skills/cloud-atlas`](../SKILL.md)  
> **Ziel**: Vollständige Dokumentation aller Subprozess-Grenzen, Tool-Abhängigkeiten, Sicherheitsbarrieren und Plattformspezifika.  
> **Gültig für**: `skills/cloud-atlas/` relativ zum Repository-Root

---

## 1. Externe Konvertierungs-Werkzeuge & Subprozess-Isolation

Cloud-Atlas steuert mehrere externe Konverter an, bindet diese jedoch hermetisch:

| Werkzeug | Aufruf-Modus | Isolation & Schutzgrenzen |
| :--- | :--- | :--- |
| **Pandoc** | CLI Subprozess | Striktes Time-out pro Datei (Standard: 60 Sekunden via `--file-timeout`). Keine Ausführung eingebetteter Lua-Filter oder Shell-Hooks. |
| **LibreOffice** | CLI Headless | Startet mit `--headless --convert-to pdf --outdir <temp>`. Erzeugt isoliertes Benutzerprofil im Temp-Verzeichnis, um Sperrkonflikte mit laufenden Instanzen zu verhindern. |
| **Tesseract** | CLI Subprozess | Bild-OCR mit begrenzter Laufzeit. Fehlt Tesseract oder das geforderte Sprachpaket, fällt der Konverter fail-closed auf `no_ocr` zurück. |

---

## 1a. PDF-Formularfelder-Extraktion (AcroForm, optional)

Bei der PDF-Konvertierung (`convert_cloud_docs.py`, Worker-Pfad `is_pdf`) werden **gefüllte PDF-Formularfelder (AcroForm) via PyMuPDF** (`pymupdf`/Fallback `fitz`, rein lesend) extrahiert und als Abschnitt „Formularfelder (ausgefüllt)“ (Tabelle Seite/Feld/Typ/Wert) an den Markdown-Mirror angehängt, weil MarkItDown Widget-Werte nicht liest. Keine Mutationspfade; PyMuPDF fehlt → Spiegel bleibt unverändert (kein `conversion_required`). Fehl-/Kodierungsartefakte (z. B. „¬“ statt „€“) werden **getreu zum Original-PDF** übernommen, nicht repariert.

**Abgrenzungen (Issue #20):**

* **Expliziter, begrenzter Status.** `extract_pdf_form_fields()` liefert `complete`/`no_fields`/`partial`/`dependency_missing`/`error` plus `tool`/`tool_version` und begrenzte `warnings`. **Alle** Widget-Metadaten (`field_value`/`field_name`/`field_type_string`), die **lazy Widget-Iteration** (`page.widgets()`) und die **lazy Dokument-Iteration** (statt eager `list(doc)`) sind einzeln guardiert; Öffnungs-, Parser- und Iterationsfehler werden nicht mehr als leere bzw. scheinbar erfolgreiche Teilliste verschluckt. Grenzlogik: Lesefehler mit bereits gelesenen Feldern ⇒ `partial`; Dokument-Iterationsfehler **ohne** Felder ⇒ `error`; das Dokument-Handle wird in jedem Pfad geschlossen.
* **Literal & einzeilig.** Feldname/Typ/Wert werden über `literal_markdown_cell()` einzeilig und literal gerendert (Whitespace-Kollaps inkl. U+2028/U+2029, HTML entity-escaped, Markdown-Metazeichen escaped); NBSP bleibt erhalten, `None` wird zur leeren Zelle. Zeilenumbrüche/HTML-Bilder können den Mirror nicht umstrukturieren.
* **Typabhängiges `Off` (exakt).** Nur Button-Familien (Button/Checkbox/Radio) verwerfen die **exakten** Marker `Off`/`/Off`; Text-/Choice-Werte und benutzerdefinierte Checked-Werte wie `OFF`/`off` bleiben erhalten (kein `casefold`).
* **Transport, Persistenz & Besitz.** Das `form_fields`-Objekt (Status/Count/Tool/Version/Warnungen/`source_sha256`/`extracted_before_ocr`) erreicht über Worker und Scheduler den **Filemap-Eintrag** (`files[<quelle>].form_fields`). Es ist ein **reserviertes generated Feld** (`GENERATED_FILEMAP_FIELDS`): Curation-Overlays dürfen es weder setzen noch überschreiben. Für eine **unveränderte** Quelle (gleicher Inhalt-Hash) wird es im Skip-Pfad und im Generator explizit weitergetragen; für eine geänderte/neue Quelle wird kein veralteter Vertrag übernommen. Das kanonische Frontmatter wächst **nicht**; `converter` bleibt die Basis-Konvertierungsmethode des zusammengesetzten Mirrors.
* **OCR-Reihenfolge.** Bei `enrich_source` werden die Felder vor der destruktiven In-Place-OCR gelesen (`extracted_before_ocr: true`, Pre-OCR-`source_sha256`); `local_derivative` liest weiter aus dem unveränderten Original.
* **Optionalität/Härtung.** `_load_pymupdf` behandelt **jeden** Importfehler als fehlende Capability (nicht nur `ImportError`; Legacy-`fitz`-Fallback), ohne die Basis-Konvertierung zu killen.
* **Bounds.** `MAX_PDF_FORM_FIELDS`/`MAX_PDF_FORM_FIELD_TEXT`/`MAX_PDF_FORM_FIELD_NAME`/`MAX_PDF_FORM_FIELD_TYPE`/`MAX_PDF_FORM_FIELD_WARNINGS` begrenzen Feldanzahl, Wert-, Namens- und Typlänge sowie Warnungen; Überschreitung ⇒ `partial` + `*_truncated`-Warnung — sichtbar auch bei ausgeschöpftem Warnungsbudget.
* **Rebuild.** Bestehende Mirrors unveränderter Quellen werden nicht automatisch nachgerüstet; nur ein gezielter `--force`-Lauf erzeugt sie neu (kein Live-Rebuild).

Testabdeckung unter `tests/test_pdf_form_field_extraction.py` (inkl. Non-`ImportError`-Importfehler, Legacy-`fitz`-Fallback und fehlende Option ohne modulweiten Skip) und `tests/test_filemap_curation_overlay.py` (Overlay-Reservierung + Retention bei unverändertem/geändertem Quell-Hash).

---

## 2. Windows Pfadlängen-Beschränkung (`EXTERNAL_CONVERTER_PATH_LIMIT`)

Externe Windows-Binaries (insbesondere LibreOffice und ältere Pandoc-Builds) scheitern trotz aktivierter Windows-10-Long-Paths-Option häufig an Pfaden über ~260 Zeichen.

* **Konstante:** `EXTERNAL_CONVERTER_PATH_LIMIT = 240` in [`scripts/convert_cloud_docs.py`](../scripts/convert_cloud_docs.py#L36).
* **Effekt:** Liegt ein Zieldateipfad inklusive temporärer Arbeitsnamen über 240 Zeichen, nutzt der Konverter deterministisch verkürzte temporäre Hash-Pfade für den externen Tool-Aufruf und verschiebt das fertige Markdown-Derivat erst nach Abschluss an den Zielort.

---

## 3. Schutz digitaler Signaturen & PDF-Integrität

Die Modifikation von Originaldateien im Modus `enrich_source` unterliegt strengsten Sicherheitsbarrieren:

* **Signaturprüfung:** Enthält ein PDF eine digitale Signatur, ist jede In-Place-Bearbeitung **strikt untersagt** (jede Änderung würde die Signatur ungültig machen).
* **Fail-Closed Fallback:** Kann die Signaturfreiheit nicht zweifelsfrei bewiesen werden oder ist das Medium schreibgeschützt, verweigert der Konverter `enrich_source` und erzwingt automatisch `local_derivative`.

---

## 4. Nebenläufigkeit & Locking

* **Workspace-Lock Guard:** Der aufrufende Harness validiert vor dem ersten Schreibvorgang in `memory/cloud/` die aktive Lease via `workspace-lock`; `convert_cloud_docs.py` erzwingt die Lease nicht selbst (auch nicht mit `--only-file`). Ohne Harness-Guard gilt die Lock-Vorbedingung aus `SKILL.md`.
* **Idempotente Atomarität:** Das Schreiben von `filemap.json` und aller `.md`-Mirrors erfolgt über `tempfile.NamedTemporaryFile` gefolgt von `os.replace`. Ein vorzeitiger Abbruch hinterlässt weder leere noch halb konvertierte Dateien.

## 5. Ausschluss- und Cleanup-Grenze (Issue #2)

`exclude_dirs` wirkt vor Hashing/Konvertierung und verändert keine Cloud-Quelle.
Neu ausgeschlossene Quellen fehlen im Inventar, ihre bisherigen Mirrors und
Derivate bleiben erhalten. Bei aktiven Ausschlüssen darf Cleanup nur zuvor
inventarisierte Artefakte von Quellen im verbleibenden Scan-Scope entfernen;
unbekannte Artefakte und leere Verzeichnisse bleiben bestehen. Das schützt auch
Custom-Derivate über wiederholte Läufe, nachdem ihr Filemap-Eintrag entfernt ist.
Die Zielplanung reserviert vorhandene Artefakte ohne positive In-Scope-Ownership
bei jedem Lauf erneut; nicht auflösbare Kollisionen stoppen fail-closed vor Überschreiben oder
Inventarübernahme (`mirror-plan`); auflösbare Kreuzgruppen-Kollisionen werden auf `original.ext.md` disambiguiert. Cleanup-Schutz allein wäre nicht ausreichend.
Ohne Ausschlüsse gilt das bisherige Cleanup. Überlappende Quellwurzeln erfordern
getrennte Output-Wurzeln und Filemaps je Storage. Keine automatische Übernahme
von Ownership oder Entfernung zurückbehaltener Artefakte.

**Der Vollscan kennt den Version-Root nicht (bewusste Grenze der Scope-Reduktion):**
`_versions/` und `.only-file-journal/` werden vom Vollscan weder reserviert noch
ausgenommen. Ein regulärer Vollscan kann versionierte Quellen erneut konvertieren
und die versionierten Mirror-Dateien als Waisen entfernen; Journale werden von ihm
nicht gelesen. Der Version-Root bleibt ausschließlich Sache von `--only-file` und
hat dort keine Aufräumlogik.

## 6. Einzeldatei-Modus: versionierte Ziele und Commit (Issue #22)

`convert_cloud_docs.py --only-file` schreibt ausschließlich:

| Ziel | Wann | Mechanismus |
| :--- | :--- | :--- |
| `<output_dir>/.only-file-journal/<key>.json` | jeder Launch mit Konvertierung oder Eintragsänderung | atomar (Datei-`fsync` + `os.replace`), `planned → staged → published → committed`; `staged` unmittelbar vor dem Replace; wird **nie** umbenannt, archiviert oder gelöscht |
| `<output_dir>/_versions/<quell-key>/<zeit-pid-id>/` | jede Konvertierung | neues, eindeutiges Versionsverzeichnis (nie überschrieben); Mirror und Derivat werden **direkt hineingeschrieben** (kein Staging-Verzeichnis, kein Verzeichnis-Rename, auch der Mirror ohne Temp/Replace), Dateien und Verzeichnis gefsynct; keine Aufräumlogik |
| `filemap.json` | nur wenn sich der eine Eintrag ändert | Roh-Splice in die **aktuelle** Datei unter OS-Lock + Base-Hash-Prüfung vor dem Temp-Write und unmittelbar vor dem atomaren `os.replace` — **einziger Commit-Punkt** |

Alles andere bleibt unangetastet: kein Walk, kein Löschen verwaister Mirrors/Derivate,
keine Quarantäne, keine Archivierung. **Bewusste Scope-Reduktion:** es gibt keine
Fremdreferenz-Ermittlung (Schutz nur gegen die **eigene** Ziel-Filemap), keine
Sibling-Koordination und keinen Vollscan-Anteil.

**Fehlermodell:** Der Filemap-Replace ist der **einzige** Commit-Punkt. Jeder Fehler davor
lässt die alte Version aktiv; die neue Version bleibt unreferenziert liegen und wird als
`retained_version` gemeldet. **Es wird nichts gelöscht, verschoben, umbenannt oder
archiviert** — auch nicht im Worker: OCR schreibt direkt in das endgültige Derivat, ein
Short-Path-Staging und LibreOffice-Profil bleiben — im Short-Path- wie im direkten Pfad —
im kurzen, eindeutigen Laufverzeichnis `<workspace>/data/ca-doc/<run-id>` (kanonische Data
Zone) liegen, Derivate werden per Kopie übernommen. Das
Profil wird dem Konverter als reiner `file://`-URI übergeben (ohne Extended-Prefix,
Leerzeichen percent-kodiert, UNC als `file://server/share/…`); ein `file://///?/…`-URI
würde LibreOffice bis zum Timeout hängen lassen. Die URI bleibt unter 120 Zeichen; tiefere
Workspace-Pfade enden fail-closed als `conversion_required` (kein Konverter-Absturz). **Einzige zulässige
Ausnahmen sind** der atomare `os.replace` der Ziel-Filemap und des Journals
(Temp-Datei im selben Verzeichnis; notwendig für Commit bzw. Journal-Record). Das OCR-
Derivat wird direkt geschrieben; die Pfadgleichheit wird nach Entfernen des Windows-
Extended-Prefix geprüft, sodass kein `os.replace(x, x)` entsteht. Eine bereits committete, später überholte Version ist historisch und wird
nicht gemeldet. Kaputtes JSON der Ziel-Filemap verhindert den Lauf (`filemap`); ein
unlesbares Journal, ein unklarer Journalzustand oder eine beschädigte aktive Version
beenden ihn fail-closed (`journal`) ohne jede Mutation.

**Journal-Pfad ist geschützt:** Vor jeder Journalmutation prüft der Lauf **alle** Einträge
derselben Ziel-Filemap (auch den Eintrag der gewählten Quelle); beansprucht einer davon
den Journalpfad (`markdown_mirror` oder `derivative.path`), endet er fail-closed
(`phase: journal`) ohne Journal-Write und ohne Filemap-Änderung (kein Scan, keine andere
Filemap). Der Vergleich folgt dem Windows-Aliasing (case-insensitiv, abschließende
Punkte/Leerzeichen je Komponente ignoriert).

**Recovery meldet nur:** Der nächste Lauf derselben Quelle liest genau den kanonischen
Journalpfad (kein Scan, keine Suche nach weiteren Journalen, keine Fremd-Filemap) und
entscheidet **nichts**: Ist der Lauf nachweislich committet (`published`/`committed` oder
Live-Eintrag == aufgezeichneter neuer Digest), fährt er fort; jeder andere Zustand ist
unklar und blockiert. Journal, Versionen und Stages bleiben exakt liegen; die manuelle
Abhilfe ist die Inspektion und das Entfernen des Journals durch den Operator.

**Zusage:** F1 gilt ohne Restore: Vor dem Commit ist die alte Version vollständig aktiv,
nach dem Commit die neue; es gibt keinen mehrstufigen In-Place-Rollback und keinen
gemischten Zustand. Getestet sind Crashs nach jedem Schritt (`planned`, `writing`, `temp`,
`filemap_tmp`, `staged`, `filemap`, `published`, `committed`), fail-closed ohne Writes vor
dem Commit, Fortsetzen nach dem Commit, kein `unlink`/`remove`/`rename`/`replace`/`rmtree`
im Recoverypfad, Platzhalter-Quelle/-Ziel-Filemap (fail-closed), doppelte JSON-Schlüssel,
fremde Einträge der Ziel-Filemap und Checksummen-Abweichungen. **Nicht** getestet und
ausdrücklich außerhalb des Vertrags: Vollscan-Verträglichkeit des Version-Roots,
Fremdreferenzen anderer Storages, Quarantäne, Journal-Archivierung und Aufräumlogik.

**Prozessgrenze:** `--file-timeout` beendet den Worker samt Kindprozessen
(`terminate_process_tree`: Windows `taskkill /T /F`, POSIX eigene Prozessgruppe via
`setsid` und `killpg`), damit kein externer Konverter weiterarbeitet. Der Windows-Pfad
bleibt über `get_safe_path` abgedeckt.
