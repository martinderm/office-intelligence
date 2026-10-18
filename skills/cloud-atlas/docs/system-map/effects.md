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

* **Workspace-Lock Guard:** Jeder Lauf von `sync_project_cloud.py` oder `convert_cloud_docs.py` validiert vor dem ersten Schreibvorgang in `memory/cloud/` die aktive Lease des aufrufenden Harnesses via `workspace-lock`.
* **Idempotente Atomarität:** Das Schreiben von `filemap.json` und aller `.md`-Mirrors erfolgt über `tempfile.NamedTemporaryFile` gefolgt von `os.replace`. Ein vorzeitiger Abbruch hinterlässt weder leere noch halb konvertierte Dateien.

## 5. Ausschluss- und Cleanup-Grenze (Issue #2)

`exclude_dirs` wirkt vor Hashing/Konvertierung und verändert keine Cloud-Quelle.
Neu ausgeschlossene Quellen fehlen im Inventar, ihre bisherigen Mirrors und
Derivate bleiben erhalten. Bei aktiven Ausschlüssen darf Cleanup nur zuvor
inventarisierte Artefakte von Quellen im verbleibenden Scan-Scope entfernen;
unbekannte Artefakte und leere Verzeichnisse bleiben bestehen. Das schützt auch
Custom-Derivate über wiederholte Läufe, nachdem ihr Filemap-Eintrag entfernt ist.
Die Zielplanung reserviert vorhandene Artefakte ohne positive In-Scope-Ownership
bei jedem Lauf erneut; Kollisionen stoppen fail-closed vor Überschreiben oder
Inventarübernahme (`mirror-plan`). Cleanup-Schutz allein wäre nicht ausreichend.
Ohne Ausschlüsse gilt das bisherige Cleanup. Überlappende Quellwurzeln erfordern
getrennte Output-Wurzeln und Filemaps je Storage. Keine automatische Übernahme
von Ownership oder Entfernung zurückbehaltener Artefakte.
