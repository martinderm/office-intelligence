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
