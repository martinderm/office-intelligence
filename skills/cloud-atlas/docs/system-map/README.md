# Cloud-Atlas - Subsystem System Map

**Standalone-Guard (#38):** `promotion_refresh.py` wählt den externen Guard über
`workspace_lock_root` (vertrauenswürdiger API-Aufrufer), `WORKSPACE_LOCK_ROOT` oder
den Installations-Sibling `../workspace-lock` relativ zum Bundle. Ungültige Auswahl
stoppt ohne CWD-/Parent-Scan oder Rückfall auf einen fremden Modulcache. Der lokale
Guard-Cache ist pfadgebunden; `allow_legacy=False` und Lease-Prüfung bleiben gleich.
Ein fehlender Guard ergibt vor dem Refresh `refresh_denied` / `lock_unavailable`
mit optionalem `installation_guidance`, das in `result_hash` eingeht; fremde Leases
erhalten weiterhin nur den bisherigen Lock-Denial. Anleitung:
[Bundle-README](../../../../README.md#workspace-lock-voraussetzung).

> **Typ**: ICM Form 6 (`system-map`), Sub-Skill-Ebene (L2)  
> **Subsystem**: [`skills/cloud-atlas`](../SKILL.md)  
> **Ziel**: Kompakte, zitierbare Architekturkarte der Filemap-, Konvertierungs- und Synchronisations-Engine zur Vermeidung von Context-Bloat bei Refactorings und Konvertierungs-Erweiterungen. Letzte vollständige Metrik (PR #51, 2026-10-23): 33 getrackte Dateien (`git ls-files skills/cloud-atlas/`); finale vollständige pytest-Suite: 244 bestanden, 134 Subtests bestanden, 0 Skips, Exit 0 (Parent-Finalframe im Feature-Ledger). Issue #2 (2026-10-16) führte 32 Dateien / 182/182 (15 Ausschluss-Regressionstests); Issue #20 ergänzt den optionalen AcroForm-Formularfelder-Vertrag, seine Status-/Filemap-Persistenz, Besitz-/Retention-Semantik und die literale, einzeilige Zellendarstellung. Issue #97 ergänzt `scripts/core/retention.py`, `retention_audit.py` und `tests/test_retention_policy.py`; die PR-#51-Zahl bleibt als historischer Messpunkt stehen.
> **Gültig für**: `skills/cloud-atlas/` relativ zum Repository-Root

**Issue #22 (Einzeldatei-Modus `--only-file`, bewusste Scope-Reduktion):** ergänzt
`core/filemap_merge.py`, `core/publish_journal.py`, `core/artifact_refs.py` und
`tests/test_convert_cloud_docs_only_file.py` (113 Tests, 1 davon nur ohne
Symlink-/Junction-Recht übersprungen; Laufzeit 363,7 s). Gesamtsuite
`skills/cloud-atlas/tests`: **372 Tests, 1 übersprungen, 575,7 s** (vor #22: 259 bestanden).
Die frühere, größere Ausbaustufe (415 Tests) ist mit der Scope-Reduktion entfallen.
Die Metrik der Kopfzeile ist davon unberührt (Stand PR #51).

**Issue #97 (Retention-/Quarantäne-Policy, rein lesend):** ergänzt
`scripts/core/retention.py`, `scripts/retention_audit.py` und
`tests/test_retention_policy.py`. Der Audit klassifiziert den bestehenden
Version-Root (`active` = Ziel-Filemap-Referenz, auf Windows alias-/case-insensitiv;
auf case-sensitiven Plattformen exakt; `retained` = neueste 2 inaktive Versionen je
Quell-Key oder ≤ 90 Tage; `quarantine_candidate` = > 365 Tage oder außerhalb des
Counts und > 90 Tage) und meldet unerwartete Layouts/Links. **Die 365-Tage-Regel
hat Vorrang:** eine >365-Tage-Version ist Kandidat, auch wenn sie unter den neuesten
zwei liegt oder ≤ 90 Tage alt wäre. Alter wird ungerundet aus der mtime berechnet.
Referenzierte Link-/Junction-Runs bleiben `active`, werden aber nie traversiert;
Cloud-Platzhalter in Quell-Key-, Run- oder Inhaltsverzeichnissen werden nicht
traversiert. Ein unlesbarer Quell-Key mit aktiver Referenz wird `unresolved`
(`potentially_active: true`) statt Kandidat; `references.unresolved` und die
menschliche Zusammenfassung nennen ihn. Eine Filemap ohne `output_dir` wird
fail-closed abgewiesen; die Identität wird über die nur gelesenen
`projects.json`/`topics.json` (`output_json` + `output_dir`, inkl. Aliasfelder und
synthetisierter Defaults) gebunden. Ohne genau einen Katalogtreffer bleibt die
Klassifikation `classification_authoritative: false` (unverifiziert/mehrdeutig) und
Kandidaten sind nur nicht-autoritative Prüfhinweise. Der komponentenweise Guard
bricht bei Links/Junctions/Platzhaltern in Output-/Filemap-Vorfahren den Audit ab;
bei Katalog-Vorfahren verhindert er nur das Lesen und damit die Verifikation, nicht
den Audit. Er schreibt, verschiebt, löscht
und archiviert nichts; Journale bleiben `journal_not_managed`. `--only-file` und der
#22-Vertrag bleiben unverändert. Die Schwellen sind Empfehlungen, keine harte
Speicherobergrenze; Fremdreferenzen anderer Filemaps/Konsumenten sind unbekannt,
daher ist kein Eintrag `safe_to_delete`/`safe_to_move`.

Die genannte Cloud-Metrik ist der PDF-Fix-Frame vor Integration von #14/#11.
PR #23 integriert Main `920ac3a` ohne Änderung an Cloud-Code/Tests; die neue
Vier-Suiten-Gesamtprüfung und ihre Head-/Tree-Bindung stehen in der Run-Evidenz.

---

## 1. Systemübersicht

`cloud-atlas` ist das zentrale Subsystem für die Kartierung, Spiegelung und Konvertierung externer Cloud-Speicher (Nextcloud, Google Drive, OneDrive, Netzlaufwerke) in agentenlesbare lokale Markdown-Strukturen unter Wahrung von Provenienz und Integrität.

```
┌────────────────────────────────────────────────────────────────────────┐
│                   Externe Cloud-Speicher (Originale)                   │
│         (DOCX, PPTX, XLSX, PDF, Bilddaten, unstrukturierte Bäume)      │
└───────────────────────────────────┬────────────────────────────────────┘
                                    │ Read-Only Scan & Hashing
                                    ▼
┌────────────────────────────────────────────────────────────────────────┐
│                          cloud-atlas Engine                            │
│  ├─ sync_project_cloud.py (Orchestrierter Sync-Runner)                 │
│  ├─ gen_filemap.py (Filemap-Generator, Schema 1, 53 KB)                │
│  ├─ convert_cloud_docs.py (Pandoc-, LibreOffice- & OCR-Pipeline, 103 KB)│
│  └─ core/ (Hilfsmodule)                                                │
│      ├─ metadata.py (Frontmatter- & Metadaten-Schemas)                 │
│      ├─ curation.py (Curation-Overlays & manuelle Overrides)           │
│      ├─ mirror_paths.py (Pfadberechnung & Kollisionsvermeidung)        │
│      ├─ publish_journal.py (Intent-Journal & Recovery, --only-file)    │
│      ├─ artifact_refs.py (Konfigurierte Referenzermittlung)            │
│      └─ scan_scope.py (Storage-lokale Verzeichnisausschlüsse)          │
└───────────────────────────────────┬────────────────────────────────────┘
                                    │ Atomare Disk-Writes (Lock-geschützt)
                                    ▼
┌────────────────────────────────────────────────────────────────────────┐
│             Ziel-Workspace Cloud-Zone (memory/cloud/)                  │
│  ├─ projects/<id>/filemap.json (Maschinenlesbare Bestandsaufnahme)     │
│  ├─ projects/<id>/filemap-curation.json (Manuelle Kuratierungsregeln)  │
│  └─ projects/<id>/mirrors/ (Generierte Markdown-Derivate mit Metadaten)│
└────────────────────────────────────────────────────────────────────────┘
```

---

## 2. Modul-Topographie (`scripts/`)

| Komponente | Dateipfade | Primäre Verantwortlichkeit |
| :--- | :--- | :--- |
| **Sync-Orchestrierung** | [`scripts/sync_project_cloud.py`](../scripts/sync_project_cloud.py) | Geordneter 2-Phasen-Lauf: Konvertierung gefolgt von Filemap-Aktualisierung für Projekte oder Topics. |
| **Filemap-Engine** | [`scripts/gen_filemap.py`](../scripts/gen_filemap.py) | Rekursiver Dateisystemscan, SHA-256 Hashing, Schema-1-Generierung, Validierung gegen `filemap.schema.json`. |
| **Konvertierungs-Engine** | [`scripts/convert_cloud_docs.py`](../scripts/convert_cloud_docs.py) | Pandoc-/LibreOffice-Bridge, PDF-OCR-Pipeline (`local_derivative` vs. `enrich_source`), optionale AcroForm-Formularfelder-Extraktion (PyMuPDF, Status/Warnungen → Filemap), Frontmatter-Injektion. |
| **Metadaten & Frontmatter** | [`scripts/core/metadata.py`](../scripts/core/metadata.py) | Validierung der 12 Pflichtschlüssel und kanonischen Frontmatter-Eigenschaften. |
| **Kuratierungs-Overlay** | [`scripts/core/curation.py`](../scripts/core/curation.py) | Laden und Mergen manueller Kuratierungsentscheidungen aus `filemap-curation.json`. |
| **Spiegelpfad-Berechnung** | [`scripts/core/mirror_paths.py`](../scripts/core/mirror_paths.py) | Deterministische Transformation von Cloud-Pfaden in lokale Mirror-Pfade, Vermeidung von Namenskollisionen; kollidierende Quellen erhalten `original.ext.md`, nicht auflösbare case-only Kollisionen stoppen fail-closed. |
| **Scan-Scope (Issue #2)** | `scripts/core/scan_scope.py` | Gemeinsame Normalisierung, case-insensitiver Komponentenvergleich und Pre-Traversal-Pruning von `exclude_dirs`; Inventar und Rename-Matching bleiben im selben Scope. |
| **Lokale Zugriffsprüfung (Issue #22)** | `scripts/core/artifact_refs.py` | `is_cloud_placeholder` prüft `OFFLINE`/`RECALL_ON_OPEN`/`RECALL_ON_DATA_ACCESS` und behandelt einen `stat`-Fehler auf existierendem Pfad fail-closed; nur für die Quelle und die Ziel-Filemap des `--only-file`-Laufs. Keine Referenz-Ermittlung, kein Walk, keine Symlink-Verfolgung. |
| **Einzeldatei-Merge (Issue #22)** | `scripts/core/filemap_merge.py`, `scripts/core/publish_journal.py`, `convert_cloud_docs.py --only-file` | Roh-Splice genau eines Filemap-Eintrags (alle anderen Bytes identisch), strikte Parse-/Schema-Prüfung vor Mutation, unveränderliche versionierte Ziele unter `<output_dir>/_versions/` (direkt geschrieben, kein Verzeichnis-Rename, ohne Aufräumlogik), atomarer Filemap-Replace als einziger Commit-Punkt, minimales Journal `planned → staged → published → committed` (`staged` unmittelbar vor dem Replace), das nie archiviert oder gelöscht wird und dessen Pfad vor jeder Mutation gegen **alle** Einträge der Ziel-Filemap (auch den der Quelle, inkl. Windows-Aliase) geprüft wird (Claim → fail-closed); Recovery meldet nur, setzt nach einem nachweislichen Commit fort und endet sonst fail-closed ohne Writes; kein Scan, kein Cleanup, keine Fremdreferenz-Ermittlung ([`processes.md`](processes.md) Phase 5, [`effects.md`](effects.md) §6). |
| **Retention-Audit (Issue #97)** | `scripts/core/retention.py`, `scripts/retention_audit.py` | Rein lesende Klassifikation des Version-Roots als `active` (von der Ziel-Filemap referenziert; Windows-Alias nur auf Windows, sonst exakt), `retained` (neueste 2 inaktive Versionen je Quell-Key oder ≤ 90 Tage) oder `quarantine_candidate` (> 365 Tage oder außerhalb des Counts und > 90 Tage; **>365 Tage hat Vorrang vor den neuesten zwei**). Unerwartete Layouts/Links sind Kandidaten; referenzierte Link-Runs bleiben `active` und werden nie traversiert, Cloud-Platzhalter in Quell-Key-/Run-/Inhaltsverzeichnissen ebenfalls nicht. Ein unlesbarer Quell-Key mit aktiver Referenz ist `unresolved`/`potentially_active` statt Kandidat. Identität wird über Katalog-`output_json`+`output_dir` (inkl. Aliasfelder/Defaults) gebunden; jeder Katalog-Lese-/Parsefehler verhindert `verified`, ohne genau einen fehlerfreien Treffer ist die Klassifikation nicht autoritativ. Output-/Filemap-Guardfehler brechen den Audit ab; Katalog-Guardfehler blockieren nur die Verifikation. Filemaps ohne `output_dir` sind fail-closed. Keine Writes, keine physische Quarantäne, kein Move/Delete, keine Journal-Archivierung; alle Einträge `action: "none"`, `safe_to_delete: false`, `safe_to_move: false`, `foreign_reference_risk: "unknown"`. Schwellen sind Empfehlungen, keine Speicherobergrenze ([`processes.md`](processes.md) Phase 6, [`effects.md`](effects.md) §6.1, [`objects.md`](objects.md) §5). |

---

## 3. Navigationsmatrix

| Dimension | Dokument | Inhalt |
| :--- | :--- | :--- |
| **Nomen** (Struktur & Zustand) | [`objects.md`](objects.md) | `filemap.json` (Schema 1), `filemap-curation.json`, Frontmatter-Schemas (12 Pflichtfelder), Mirror-Payloads. |
| **Verben** (Ablauf & Transformation) | [`processes.md`](processes.md) | Filemap-Generierung, Dokumentenkonvertierung, OCR-Verzweigung, Sync-Lifecycle. |
| **Seiteneffekte** (Umwelt & Grenzen) | [`effects.md`](effects.md) | Pandoc-/LibreOffice-Isolation, Tesseract-OCR, 240-Zeichen Pfadlimit (Windows), Lock-Schutz. |

---

## 4. Die 6 fundamentalen Cloud-Atlas-Invarianten

1. **Unveränderlichkeit externer Originale:** Cloud-Speicher bleiben standardmäßig read-only. Modifikationen finden als lokale Markdown-Spiegel in der Workspace-Zone `memory/cloud/` statt.
2. **OCR-Policy Disziplin:** Standard ist `local_derivative` (lokales Textderivat). `enrich_source` (In-Place OCR im Cloud-PDF) ist nur für un-signierte, beschreibbare PDFs mit voller Provenienz zulässig.
3. **Zirkelfreies Payload-Hashing:** `artifact_sha256` im Frontmatter repräsentiert exakt die Bytes des reinen Markdown-Bodys, niemals der Datei inklusive Frontmatter (Ausschluss selbstreferenzieller Hash-Drifts).
4. **Strikte Frontmatter-Konformität:** Erzeugte Markdown-Dateien erzwingen exakt die 12 kanonischen Pflichtfelder (`REQUIRED_CLOUD_FRONTMATTER_KEYS`).
5. **Einzeldatei-Isolation (Issue #22):** `--only-file` verändert genau einen Filemap-Eintrag per Roh-Splice und scannt und löscht nichts. Jede Konvertierung legt ein neues, eindeutiges Versionsverzeichnis unter `<output_dir>/_versions/` an und schreibt Mirror und Derivat **direkt** hinein (kein Staging-Verzeichnis, kein Verzeichnis-Rename, Datei-/Verzeichnis-fsync, nie in-place überschrieben); der atomare Filemap-Replace ist der **einzige Commit-Punkt**. **Es wird nichts gelöscht, verschoben, umbenannt, archiviert oder aufgeräumt**: Journale, Versionen, Reste und Commit-Temps bleiben liegen; der Version-Root hat keine Aufräumlogik; auch der Worker löscht keine Artefakte (OCR schreibt direkt — kein `os.replace(x, x)`, Pfadgleichheit nach Entfernen des Windows-Extended-Prefix; DOC-Short-Path-Staging und LibreOffice-Profil liegen im kurzen Data-Zone-Laufverzeichnis `data/ca-doc/<run-id>`). Einzige zulässige `os.replace`-Aufrufe sind Filemap und Journal. **Die Recovery meldet nur**: ein nachweislich committeter Lauf (`published`/`committed` oder Live-Eintrag == aufgezeichneter neuer Digest) wird fortgesetzt, jeder andere Zustand endet fail-closed (`phase: journal`) ohne jede Mutation, Journal und Reste bleiben liegen. **Vor jeder Journalmutation** prüft der Lauf **alle** Einträge derselben Ziel-Filemap — auch den der Quelle, inklusive Windows-Aliasen (case-insensitiv, abschließende Punkte/Leerzeichen je Komponente) — auf den Journalpfad (Claim → fail-closed, kein Scan). Quelle und Ziel-Filemap werden vor jedem Öffnen lokal geprüft (nie hydratisiert). Der Lauf hält OS-Locks pro Storage (Output exklusiv, Eltern shared, Filemap exklusiv; im Projekt-Datenbereich `data/.locks/cloud-atlas/`, sortiert akquiriert) und prüft den Base-Hash unmittelbar vor dem Replace; kooperierende Läufe sind geschützt, Writer ohne Harness-Lease sind außerhalb des Vertrags. **Bewusste Scope-Reduktion:** kein Vollscan-Anteil (ein regulärer Vollscan kennt den Version-Root nicht und kann versionierte Mirror als Waisen entfernen), keine Fremdreferenz-Ermittlung (Schutz nur gegen die eigene Ziel-Filemap), keine Quarantäne, keine Journal-Archivierung, keine Sibling-Koordination. `published`/`committed` erreichte Versionen werden nie automatisch gelöscht; nie ist ein gemischter Versionszustand sichtbar.
6. **Kuratierungs-Stabilität:** Manuelle Overrides in `filemap-curation.json` übersteuern automatisierte Heuristiken deterministisch.
