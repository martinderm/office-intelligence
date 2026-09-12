# Konformitäts- und Umsetzungsbericht: `office-intelligence` → `agent-architecture`

> **Dokument-ID:** `OI-ARCH-COMPLIANCE-2026-09`
> **Zielobjekt:** `skills/office-intelligence` als **Shared Skill Bundle**, nicht als Agent-Workspace
>
> **Referenz-Architektur:** `Shared-Memory/agent-architecture` und `skills/authoring-guide.md`
>
> **Prüfdatum:** 2026-09-01; Abschlussaudit: 2026-09-07
> **Geprüfter Git-Ausgangspunkt:** `fb9ba3e5f7a6cc239c51ac298453159e8ddb0cfc`
>
> **Geprüfter Implementierungsstand:** `4b4d620687b495ad0972c6e33808f094c7dc4435`
>
> **Worktree beim Abschlussaudit:** Implementierungsstand clean; `.agents/session.lock` ignoriert und nicht versioniert; anschließend nur dieser Abschlussbericht geändert
>
> **Status:** `conformant` im dokumentierten Scope; keine offenen P0-/P1-Findings

---

## 1. Urteil

`office-intelligence` ist nach Umsetzung von `OI-01` bis `OI-16` und dem Abschlussaudit `OI-17` als Shared Skill Bundle im dokumentierten Scope **konform**. Dual Evidence, Message-ID-Bindung, Katalog-Routing und nachvollziehbare Evidenzanker blieben erhalten; Sicherheits-, Metadaten-, CLI- und Portabilitätsverträge sind nun getestet und dokumentiert.

Die drei tragenden Architekturentscheidungen sind:

1. Das Repository bleibt ein **Skill Bundle und kein Agent-Workspace**; es erhält keine konkurrierende Control Plane oder Dummy-Data-Zones.
2. Mutationen werden am ausführenden Harness über den gemeinsamen ownership-gebundenen `workspace-lock`-Guard autorisiert; ersetzende Writes sind atomar, Append-only bleibt lockgebunden.
3. PDF-OCR verwendet `local_derivative` als sicheren Default. `enrich_source` bleibt die bewusst erlaubte Sonderlösung für beschreibbare, sicher unsignierte und versionierte Cloud-PDFs mit vollständiger Provenienz; signierte oder nicht sicher beurteilbare PDFs werden nicht in-place verändert.

Das Abschlussaudit fand vier eng begrenzte Restabweichungen (Mail-Desk-Ersatzwrites, veralteter Guard-Hinweis, benutzerspezifischer Tesseract-Suchpfad und Paket-/Frontmatter-Konventionen). Sie wurden im geprüften Implementierungsstand `4b4d620` geschlossen und vollständig regressionsgetestet.

---

## 2. Prüfgrenze

### 2.1 Im Scope

- Qualität und Routing des Root-`SKILL.md`
- Konsistenz der sieben Sub-Skills
- sichere und portable Skripte gemäß Shared Skill Authoring Guide
- Data-Zone-Konformität der Pfade und Artefakte, die das Bundle in **konsumierenden Workspaces** erzeugt
- Structured CLI Envelopes
- Lock-, Mutations- und Abwärtskompatibilitätsverträge
- Tests, Reproduzierbarkeit und kleine umsetzbare Arbeitspakete

### 2.2 Nicht im Scope und kein Finding

- kein eigenes `AGENTS.md` für das Bundle erforderlich
- kein `.agents/`-Verzeichnis oder Workspace-Architekturprofil erforderlich
- keine leeren `memory/cloud/`- oder `memory/operations/`-Dummy-Verzeichnisse erforderlich
- keine ICM-, Code-Graph- oder andere Control Plane für das Bundle erforderlich

Die vier Data Zones gelten dort, wo das Bundle Daten in einem Agent-Workspace erzeugt oder verwaltet. Das im Bundle enthaltene `memory/` ist Beispiel- beziehungsweise Template-Inhalt und kein Beleg dafür, dass das Bundle selbst ein vollständiger Agent-Workspace ist.

---

## 3. Verifizierte Stärken

- Physisch vorhanden sind sieben Sub-Skills: `cloud-atlas`, `event-documentation`, `mail-desk`, `meeting-desk`, `project-catalog-entry`, `task-desk` und `topic-catalog-entry`.
- `cloud-atlas` verwendet für neue Standardkonfigurationen bereits `memory/cloud/projects/...` beziehungsweise `memory/cloud/topics/...`.
- Cloud-Originale werden in den regulären Konvertierungs- und `.doc`-Derivatpfaden von lokalen Mirrors getrennt; die In-place-OCR ist eine bewusst zu deklarierende Ausnahme für bildbasierte PDFs.
- Die vorhandenen `cloud-atlas`-Tests liefen beim Review mit Exit-Code 0 durch.
- Dual Evidence ist in Mail-, Meeting-, Event-, Task-, Projekt- und Themen-Workflows fachlich gut verankert.
- Der zentrale `skills-catalog.yaml` ist syntaktisch valide; der Katalogvalidator meldete Erfolg.
- `mail-desk` nutzt für einzelne Zustandsdateien bereits atomare Schreibmuster mit `tempfile` und `os.replace`.

Diese Stärken dürfen bei der Modernisierung nicht durch großflächige Neuarchitektur oder unnötige Framework-Einführung verloren gehen.

---

## 4. Findings nach Priorität

Die folgenden Abschnitte dokumentieren die historischen Ausgangsbefunde. Ihr
Abschlussstatus und die aktuelle Evidenz stehen in Abschnitt 10; keiner der P0-/P1-
Befunde ist am geprüften Implementierungsstand offen.

### P1-00 — In-place-OCR benötigt einen ausdrücklichen Anreicherungsvertrag

**Beleg:** `skills/cloud-atlas/scripts/convert_cloud_docs.py`, Funktion `run_ocr_on_pdf()`:

```python
["ocrmypdf", "-l", "deu", "--redo-ocr", safe_src, safe_src]
```

**Einordnung:** Für gescannte oder bildbasierte PDFs ist eine dauerhafte OCR-Textebene im Cloud-Bestand fachlich nützlich: Das Dokument wird dadurch auch außerhalb des Agent-Workspaces durchsuchbar. OCRmyPDF unterstützt identische Ein- und Ausgabepfade ausdrücklich und überschreibt die Datei erst nach erfolgreicher Verarbeitung. Siehe [OCRmyPDF Cookbook — Modify a file in place](https://ocrmypdf.readthedocs.io/en/stable/cookbook.html#modify-a-file-in-place).

Die aktuelle Implementierung ist daher nicht wegen der In-place-Mutation selbst fehlerhaft. Nachzuschärfen sind Auslösung und Nachweis:

- `len(extracted_text) < 30` kann auch sehr kurze digitale PDFs erfassen und reicht als alleinige Scan-Erkennung nicht aus.
- `--redo-ocr` ist für das Ersetzen einer bestehenden OCR-Schicht vorgesehen. Für PDFs ohne Textschicht reicht der normale OCR-Modus; `redo` wird nur bei bewusst gewünschter Erneuerung bestehender OCR verwendet. Siehe [OCRmyPDF OCR processing modes](https://ocrmypdf.readthedocs.io/en/stable/advanced.html#ocr-processing-mode).
- Der Default `--output-type auto` und die Optimierung können neben der Textebene weitere PDF-Transformationen auslösen.
- Signierte PDFs dürfen nicht automatisch verändert werden.
- Vorher-/Nachher-Provenienz und Cloud-Versionierung werden noch nicht vollständig dokumentiert.

**Soll:**

- Eine Konfiguration `ocr_policy` unterscheidet mindestens `enrich_source`, `local_derivative` und `disabled`.
- `enrich_source` ist nur bei bildbasierten PDFs, beschreibbarem Cloud-Speicher und vorhandener Versionshistorie oder Backup zulässig.
- Für unbekannte Deployments bleibt `local_derivative` der sichere Default; bestehende Deployments dürfen `enrich_source` ausdrücklich setzen.
- Wenn ausschließlich eine Textebene ergänzt werden soll, verwendet der Lauf `--output-type pdf --optimize 0`. `--redo-ocr` wird nur bei erkannter bestehender OCR und passender Policy eingesetzt.
- Signierte PDFs führen zu einem strukturierten Stopp ohne Mutation.
- Filemap beziehungsweise Provenienzmetadaten erfassen Vorher-Hash, Nachher-Hash, Zeitpunkt, OCRmyPDF-/Tesseract-Version, gewählte Policy und – soweit verfügbar – Cloud-Versions-ID.
- Regressionstests decken erfolgreiche In-place-Anreicherung, kurze digitale PDFs, signierte PDFs und einen fehlgeschlagenen OCR-Lauf ohne Überschreiben ab.

### P0-02 — Mutations- und Lock-Vertrag ist nicht ausreichend definiert

**Beleg:** In `cloud-atlas` und `mail-desk` gibt es keine Prüfung oder verbindliche Dokumentation eines ownership-gebundenen Ziel-Workspace-Locks. Der bisher vorgeschlagene Soft-Check „kein Lockfile vorhanden → weitermachen“ verhindert keine Race Condition.

**Risiko:** Zwei Harnesses können gleichzeitig Filemaps, Kataloge, Evidenzdateien oder Mail-Desk-State verändern.

**Soll:**

- Der konsumierende Workspace muss vor jeder lokalen Mutation durch `workspace-lock` gesperrt sein.
- Der Root-Skill und jeder mutierende Sub-Skill dokumentieren diese Vorbedingung.
- Skripte erhalten entweder eine verifizierbare Lease-/Conversation-ID oder verwenden einen gemeinsamen Guard, der fremde beziehungsweise fehlende Ownership fail-closed behandelt.
- Ein fehlendes Lockfile ist nur bei ausdrücklich dokumentiertem Single-Session-Legacy-Modus zulässig; dieser Modus muss opt-in sein und eine Warnung erzeugen.

### P1-01 — Schreiboperationen sind nicht durchgehend atomar

**Belege:**

- `cloud-atlas/scripts/gen_filemap.py` schreibt Filemaps und Katalogänderungen direkt.
- `cloud-atlas/scripts/convert_cloud_docs.py` schreibt Mirrors und `filemap.json` direkt.
- `mail-desk/scripts/core/evidence.py` verwendet `Path.write_text()` direkt.
- JSONL-Anhänge sind teilweise append-basiert, aber nicht gegen parallele Writer geschützt.

**Soll:** Ersetzende Schreiboperationen verwenden temporäre Dateien im Zielverzeichnis, Flush/Close und `os.replace`. Logisches Append-only benötigt Lock-Ownership oder ein explizites transaktionales Append-Verfahren.

### P1-02 — Cloud-Artefaktmetadaten sind nicht kanonisch

**Ist:** Markdown-Mirrors verwenden unter anderem `original_file`, `original_sha256`, `conversion_date` und `last_verified_date`.

**Soll für neu erzeugte oder aktualisierte Mirrors:**

```yaml
zone: cloud
trust_level: untrusted_external
status: active
instructions_are_data: true
source_uri: data/cloud/<storage_id>/<path>
source_sha256: <sha256>
artifact_sha256: <sha256>
synced_at: <RFC-3339 timestamp>
converter: <converter-id>
data_classification: internal
retention_class: project-lifecycle
owner: <declared owner>
```

Legacy-Felder werden weiterhin gelesen, aber nicht mehr in neuem kanonischem Frontmatter geschrieben. Da das normative Schema `additionalProperties: false` verwendet, gehören zusätzliche technische Konvertierungsdetails in ein separates, klar benanntes Metadatenobjekt oder in die Filemap.

### P1-03 — Für `filemap.json` fehlt ein eindeutiger Schemascope

Die gesamte Filemap kann nicht ungeprüft wie ein einzelnes `data-zone-artifact` behandelt werden. Es ist festzulegen, ob:

1. die Filemap selbst Artifact-Metadaten plus ein `files`-Objekt nach einem eigenen Schema erhält, oder
2. jedes `files[]`-Element einen kanonischen Metadatensatz enthält.

**Soll:** Ein eigenes `filemap.schema.json` definieren oder die bestehende Containerstruktur dokumentieren und gezielt nur die Artefaktmetadaten gegen `data-zone-artifact.schema.json` validieren.

### P1-04 — Structured CLI Envelopes sind uneinheitlich

**Ist:** Die Werkzeuge verwenden Mischformen aus `ok`, `status`, `resolved`, reinem Text und teilweise bereits strukturierten Envelopes.

**Kanonischer Vertrag:**

```json
{
  "action": "...",
  "success": true,
  "state": "Completed",
  "message": "...",
  "data": {},
  "error": null
}
```

Legacy-Kompatibilität soll nicht dauerhaft drei konkurrierende Wahrheiten (`success`, `ok`, `status`) im selben Objekt etablieren. Besser ist:

- `--json`: ausschließlich kanonischer Envelope
- temporärer `--legacy-json`-Modus oder kleiner Adapter für bekannte Altkonsumenten
- dokumentierte Deprecation und Contract-Tests

### P1-05 — Audit und Scoring sind nicht reproduzierbar genug

Der ursprüngliche Bericht enthielt Prozentwerte ohne Scoring-Rubrik und keinen exakten Git-/Dirty-Worktree-Bezug. Aussagen wie „95 % konform“ oder „Zero-Breaking-Changes-Garantie“ sind daher nicht belastbar.

**Soll:** Künftige Audits dokumentieren Commit, Dirty State, Prüfbefehle, Finding-ID, Belegpfad, Schweregrad und Abnahmetest. Prozentwerte werden nur mit veröffentlichter Gewichtung verwendet; ansonsten reicht `conformant`, `partially_conformant`, `non_conformant` oder `not_applicable`.

### P2-01 — Router, README und Katalog sind inkonsistent

- Root-`SKILL.md` nennt sechs Desks und lässt `cloud-atlas` aus.
- `README.md` lässt `meeting-desk` und `task-desk` in der Hauptübersicht aus.
- Der zentrale Katalog enthält nicht alle präzisen Sub-Skill-Trigger.

**Soll:** Alle drei Quellen nennen dieselben sieben Sub-Skills. Der Root-Router bleibt kurz und verweist nur auf den passenden Desk.

### P2-02 — `mail-desk/SKILL.md` ist zu groß

Mit 622 Zeilen und rund 38 KB lädt der Skill zu viele Details in jede Mail-Desk-Session.

**Soll:** Workflow-Kern, Sicherheitsregeln und Routing verbleiben im `SKILL.md`; Backenddetails, Manifestformate, lange Beispiele und Sonderfälle wandern in gezielt geladene Dateien unter `references/`. Zielgröße: ungefähr 100–150 Zeilen, soweit ohne Informationsverlust möglich.

### P2-03 — Optionale Konvertierungsabhängigkeiten sind nicht sauber isoliert

Das Root-`requirements.txt` enthält `markitdown` und `ocrmypdf`, obwohl nur `cloud-atlas` diese benötigt. Dadurch wirkt das gesamte Bundle abhängig von schweren optionalen Paketen.

**Soll:** Abhängigkeiten bei `cloud-atlas` dokumentieren und als optionale Funktion isolieren. Die übrigen Desks müssen ohne diese Pakete vollständig funktionieren. Fehlende Konverter liefern einen strukturierten Zustand wie `ConversionRequired`, keine unstrukturierte Installationsaufforderung.

### P2-04 — Event-Speicherpfade sind institutionsspezifisch

`event-documentation` verwendet `/Agent-Share/...` als festen BokuDrive-Pfad.

**Soll:** Events besitzen und benötigen keinen eigenen Cloud-Speicher. Falls Event-Assets über Cloud Atlas archiviert oder erschlossen werden, verwenden sie ausschließlich einen bestehenden, vom zugehörigen Projekt beziehungsweise Parent-Topic/Subtopic geerbten `cloud_sync.<storage_id>`. Bei mehreren geeigneten Konfigurationen wählt ein optionaler Event-Selektor exakt eine davon; ohne geeignete Parent-Konfiguration bleibt das Event gültig, nur die Cloud-Verarbeitung entfällt. Mount, Mirrors und Filemaps werden aus den workspace-relativen Pfaden der geerbten Konfiguration aufgelöst; institutionsspezifische Speicherpfade sind kein zulässiger Default. Bestehende `/Agent-Share/`-Links gelten nur als Migrationsaltbestand, werden weder neu erzeugt noch blind global ersetzt und dürfen erst nach eindeutiger Zuordnung zu einer konkreten Cloud-Atlas-Storage-ID kontrolliert migriert werden.

### P2-05 — Paketkonventionen sind nicht vollständig konsistent

Der Authoring Guide fordert `LICENSE.txt`; im Ausgangsstand lag die Datei als `LICENSE` vor. `OI-17` hat sie ohne Inhaltsänderung in `LICENSE.txt` umbenannt. Zusätzlich wurden die zuvor vom Skill-Validator abgelehnten Winkelklammern aus den Frontmatter-Beschreibungen der Projekt- und Topic-Katalog-Skills entfernt.

### Verbesserung, aber kein Compliance-Blocker

- optionale Validatoren für Projekt- und Themenkataloge
- optionale deterministische Helper für Meeting-Registrierung und Task-Deduplizierung
- zusätzliche Scaffolding-Skripte für Events

Diese Punkte dürfen erst nach den P0-/P1-Paketen umgesetzt werden und nicht als bestehender Normverstoß dargestellt werden.

---

## 5. Abwärtskompatibilitätsstrategie

### Pfade

- Neue Konfigurationen verwenden `memory/cloud/<scope>/<slug>/<storage_id>`.
- Explizit konfigurierte Legacy-Pfade werden zunächst gelesen und respektiert.
- Eine Pfadmigration erfolgt nie still, sondern als eigenes Paket mit Inventar, Kollisionsprüfung, Linkprüfung und Restore-Plan.

### Frontmatter

- Parser lesen kanonische und alte Feldnamen.
- Writer erzeugen ausschließlich kanonisches Frontmatter.
- Bestehende Mirrors werden erst bei tatsächlicher Aktualisierung oder expliziter Migration angehoben.

### CLI

- Der kanonische Envelope erhält Contract-Tests.
- Bekannte Legacy-Konsumenten werden inventarisiert.
- Kompatibilität läuft über einen befristeten Adapter oder Modus, nicht über dauerhaft widersprüchliche Top-Level-Felder.

### Locking

- Bestehende Workspaces ohne Lock-Infrastruktur benötigen eine bewusste Einführungs- oder Legacy-Entscheidung.
- Sicherheit wird nicht durch stilles Weiterlaufen bei fehlender Ownership ersetzt.

---

## 6. Kleine Umsetzungspakete

Jedes Paket ist so geschnitten, dass ein kleiner Coding Agent nur wenige Dateien und einen klaren Abnahmetest laden muss. Ein Paket soll typischerweise 30–90 Minuten dauern und höchstens einen fachlichen Zweck verändern.

| ID | Status | Paket | Dateien im Hauptscope | Abnahme | Abhängigkeit | Modell-Eignung |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| `OI-01` | ✅ abgeschlossen | Kontrollierte In-place-OCR absichern | `convert_cloud_docs.py`, `cloud-atlas/SKILL.md`, `test_doc_conversion.py` | `ocr_policy` umgesetzt; Scan wird nachvollziehbar angereichert; kurze digitale und signierte PDFs bleiben unverändert; Fehler überschreibt nichts | keine | Luna/Flash implementiert; stärkeres Review wegen Cloud-Mutation |
| `OI-02` | ✅ abgeschlossen | Mutations- und Lock-Vertrag dokumentieren | Root-`SKILL.md`, `cloud-atlas/SKILL.md`, `mail-desk/SKILL.md` | Jede mutierende Operation benennt Lock-Vorbedingung und Legacy-Grenze | keine | Luna/Flash |
| `OI-03` | ✅ abgeschlossen | Gemeinsamen Lock-Guard entwerfen und testen | neuer kleiner Helper plus Tests | fremder/fehlender/eigener Lock deterministisch getestet; kein Force | `OI-02` | Luna/Flash implementiert; stärkeres Review |
| `OI-04a` | ✅ abgeschlossen | Atomare Writes im Cloud-Konverter | `convert_cloud_docs.py`, direkte Tests | simulierte Unterbrechung beschädigt Mirror und Filemap nicht | `OI-03` | Luna/Flash |
| `OI-04b` | ✅ abgeschlossen | Atomare Writes im Filemap-Generator | `gen_filemap.py`, direkte Tests | simulierte Unterbrechung beschädigt Filemap und Katalog nicht | `OI-03` | Luna/Flash |
| `OI-05` | ✅ abgeschlossen | Atomare Writes in `mail-desk` | `core/evidence.py` und direkte Tests | bestehende Daten bleiben bei Write-Fehler intakt | `OI-03` | Luna/Flash |
| `OI-06` | ✅ abgeschlossen | Kanonischen Metadaten-Builder ergänzen | kleiner Helper in `cloud-atlas`, Tests | erzeugte Cloud-Metadaten validieren gegen kanonisches Schema | `OI-01` | Luna/Flash |
| `OI-07` | ✅ abgeschlossen | Dual-Read/Canonical-Write migrieren | `convert_cloud_docs.py`, Tests | alte Mirrors werden erkannt; neue Ausgabe enthält keine Legacy-Felder | `OI-06` | Terra-high implementiert; stärkeres Parent-Review |
| `OI-08` | ✅ abgeschlossen | Filemap-Schema entscheiden und implementieren | neues Schema/Referenz, `gen_filemap.py`, Tests | Container und Artefaktmetadaten deterministisch validiert | `OI-06` | Luna-xhigh implementiert; stärkeres Parent-Review mit zwei Korrekturrunden (Portabilität sowie Schema-URI/Index-Hygiene) |
| `OI-09a` | ✅ abgeschlossen | Envelope für Cloud-Sync-Wrapper | `sync_project_cloud.py`, Contract-Test | Success- und Error-Pfad sind kanonisch | `OI-04a`, `OI-04b` | Terra-medium implementiert; stärkeres Parent-Review ohne Korrekturrunde |
| `OI-09b` | ✅ abgeschlossen | Envelope für Filemap-Generator | `gen_filemap.py`, Contract-Test | Human- und JSON-Modus sauber getrennt | `OI-04b` | Terra-medium implementiert; stärkeres Parent-Review mit drei gezielten Korrekturrunden (Modussemantik, Teilmutationen, Diagnosegrenzen) |
| `OI-09c` | ✅ abgeschlossen | Envelope für Cloud-Konverter | `convert_cloud_docs.py`, Contract-Test | Fortschritt bleibt außerhalb des JSON-Envelopes | `OI-04a` | Terra-high implementiert; stärkeres Parent-Review mit zwei gezielten Korrekturrunden (Multi-Storage-Fortsetzung und echte Konvertierungsfehler) |
| `OI-10` | ✅ abgeschlossen | Mail-Desk Envelope-Helper | neuer Helper unter `mail-desk/scripts/core/`, Tests | Success- und Error-Envelope zentral getestet | keine | Terra-high implementiert; stärkeres Parent-Review ohne Korrekturrunde |
| `OI-11a` | ✅ abgeschlossen | Mail-Desk CLI-Gruppe A migrieren | `inspect_manifest`, `final_location_index`, `mailbox_preflight` | Contract-Tests grün | `OI-10` | Terra-high implementiert; stärkeres Parent-Review mit einer gezielten Korrekturrunde (verschachtelter Stats-Erfolgsstatus) |
| `OI-11b` | ✅ abgeschlossen | Mail-Desk CLI-Gruppe B migrieren | `resolve_case`, `move_and_patch`, `himalaya_client` | Contract-Tests grün | `OI-10` | Terra-high implementiert; stärkeres Parent-Review mit einer gezielten Korrekturrunde (Fehlerphasentreue und Maschinenargument-Erkennung) |
| `OI-11c` | ✅ abgeschlossen | Batch-Runner-Envelope migrieren | `mail_desk_batch_runner.py`, Tests | alle Modi und Fehlerpfade kanonisch | `OI-10` | Terra-high implementiert; stärkeres Parent-Review mit zwei gezielten Korrekturrunden (Serialisierung/Manifest-Sicherheit und Pipeline-PartialFailure-Semantik) |
| `OI-12` | ✅ abgeschlossen | Legacy-CLI-Adapter und Deprecation | kleiner Adapter, Referenzdoku, Tests | bekannte `ok`-/`status`-Konsumenten bleiben über Opt-in funktionsfähig | `OI-09a/b/c`, `OI-11*` | Terra-high implementiert; stärkeres Parent-Review mit vier gezielten Korrekturrunden (historische Payload-Shapes, Kollisionsschutz/OI-10-Helper, Final-Index-Manifeste und Skill-Routing) |
| `OI-13a` | ✅ abgeschlossen | Bundle-Router und README synchronisieren | Root-`SKILL.md`, `README.md` | sieben Skills werden konsistent geroutet | keine | Terra-high implementiert; stärkeres Parent-Review ohne Korrekturrunde; Root- und fünf Sub-Skill-Validatoren grün, zwei unveränderte Altfehler in Katalog-Skill-Descriptions dokumentiert |
| `OI-13b` | ✅ abgeschlossen | Zentralen Skills-Katalog ergänzen | `../skills-catalog.yaml` im Parent-Repo | Trigger vollständig; Katalogvalidator grün | `OI-13a` | Terra-high implementiert; stärkeres Parent-Review ohne Korrekturrunde; eigener Parent-Repo-Lock und separater Katalog-Commit |
| `OI-14a` | ✅ abgeschlossen | Mail-Desk-Inhalte klassifizieren | nur Analyse/Mapping-Dokument | jede Sektion hat Ziel `SKILL.md` oder konkrete Referenzdatei | keine | Terra-high implementiert; stärkeres Parent-Review mit einer gezielten Korrekturrunde zur sauberen OI-14b/c/d-Paketgrenze; 28/28 H2-/H3-Abschnitte gemappt |
| `OI-14b` | ✅ abgeschlossen | Mail-Backenddetails auslagern | `mail-desk/SKILL.md`, Backend-Referenzen | Backendregeln vollständig verlinkt | `OI-14a` | Terra-high implementiert; stärkeres Parent-Review mit einer gezielten Korrekturrunde (Gmail-Adaptergrenze und verlustfreier OI-14c-Manifest-Handoff); 52 Mail-Desk-Tests grün |
| `OI-14c` | ✅ abgeschlossen | Manifest- und Compliance-Details auslagern | `mail-desk/SKILL.md`, neue Referenzen | Formate und Sicherheitsregeln ohne Verlust verlinkt | `OI-14b` | Terra-high korrigierte nach Parent-Finding die fünf kanonischen Batch-Runner-Beispiele und die JSON/JSONL-Abgrenzung; Parent-Review, 52 Mail-Desk-Tests, Linkprüfung und `quick_validate` grün |
| `OI-14d` | ✅ abgeschlossen | Mail-Desk-Router final kürzen | `mail-desk/SKILL.md`, Linkprüfung | Zielgröße erreicht; progressive Disclosure vollständig | `OI-14c` | Terra-high implementiert; Parent-Review mit gezielter Korrekturrunde zu Lock-Richtung, Todo-/Reply-Prüfpflicht und Spam-Fast-Path; Router auf 141 Zeilen verdichtet, 52 Mail-Desk-Tests, Linkprüfung und `quick_validate` grün |
| `OI-15` | ✅ abgeschlossen | Cloud-Abhängigkeiten isolieren | `requirements.txt`, Cloud-Atlas-Doku, Fehlerpfade | Nicht-Cloud-Desks ohne Pakete nutzbar; fehlende Konverter strukturiert | `OI-09a/b/c` | Terra-high implementiert; Parent-Review mit zwei gezielten Korrekturrunden zu Signaturschutz, Filemap-Fortsetzung, Capability-Klassifikation und einheitlichem `ConversionRequired`-Vertrag; 93 Cloud-Atlas-Tests, 52 Mail-Desk-Regressionstests, Bundle-/Skill-Validierung und `git diff --check` grün |
| `OI-16` | ✅ abgeschlossen, fachlich nachgeschärft | Optionale Event-Cloud-Nutzung über Cloud Atlas abstrahieren | Event-Skill, Template, Mail-Classifier und Contract-Tests | Events ohne Cloud gültig; Cloud-Assets nur über geerbtes `cloud_sync.<storage_id>`; explizite Selektoren streng; `/Agent-Share/` nur kontrollierter Migrationsinput | keine | Terra-high implementierte die ursprüngliche Abstraktion; Nutzerkorrektur anschließend im Parent umgesetzt: kein Event-eigener oder verpflichtender Speicher, optionale Parent-/Subtopic-Vererbung; 6 Event-Contract- und 152 Mail-Desk-Tests, Bundle-/Skill-Validierung und `git diff --check` grün |
| `OI-17` | ✅ abgeschlossen | Abschlussaudit und Regression | gesamte Testsuite, Katalogvalidator, Diff und gezielte Restkorrekturen | keine P0/P1-Findings; Evidence-Matrix vollständig | alle Pflichtpakete | Parent-Audit auf neu eingelesener Governance-/Reportbasis; Restbefunde in `4b4d620` geschlossen; 153 Bundle-Tests und 12 Guard-Tests grün, acht Skill-Entrypoints valide |

### Paketvorlage für kleine Coding Agents

Jede neue Session erhält nur:

```markdown
Ziel: <eine konkrete Verhaltensänderung>
Scope: <maximal wenige Dateien>
Nicht ändern: <explizite Grenzen>
Zu laden: Root-SKILL, betroffener Sub-Skill, relevante Tests, eine direkte Normreferenz
Akzeptanz: <deterministischer Befehl und erwartetes Ergebnis>
Git-Modus: Review; nichts stagen, committen oder pushen
Lock: Ziel-Workspace vor Mutation erwerben und danach freigeben
Handoff: Diff, Tests, Restunsicherheit und Restore-Hinweis berichten
```

---

## 7. Session- und Ausführungsstrategie

### Empfehlung: linear geordnet, aber pro Paket eine frische Session

Eine einzige lange Umsetzungssession ist nicht empfehlenswert. `office-intelligence` umfasst sehr unterschiedliche Domänen, und kleine Modelle verlieren bei einem durchgehenden Kontext leicht Scope-Grenzen, Legacy-Verträge und bereits getroffene Entscheidungen aus dem Fokus.

Empfohlen wird daher:

1. Pakete gemäß Abhängigkeiten **linear** ausführen.
2. Für jedes Paket eine **frische Session** starten.
3. Pro Session nur die in der Paketkarte genannten Dateien und direkten Referenzen laden.
4. Nach jedem Paket Tests, Diff und Handoff festhalten.
5. Erst nach bestandenem Paket mit dem abhängigen Paket fortfahren.

Parallelität ist nur in isolierten Git-Worktrees und bei vollständig disjunkten Dateien sinnvoll. Für Luna-/Flash-Agenten ist serielle Ausführung meist robuster und günstiger als parallele Koordination. Besonders `OI-01`, `OI-03` und `OI-08` erhalten ein unabhängiges Review durch ein stärkeres Modell oder einen Menschen. `OI-17` wird durch einen kontexttragenden starken Parent nach vollständigem Neueinlesen der Governance- und Reportbasis ausgeführt; bei zweifelhaften Befunden ist ein unabhängiger Reviewer hinzuzuziehen.

### Empfohlene Reihenfolge

```text
Sicherheitsstrang: OI-01 → OI-02 → OI-03 → OI-04a/OI-04b/OI-05
Metadatenstrang:   OI-06 → OI-07 → OI-08
CLI-Strang:        OI-09a/b/c + OI-10 → OI-11a/b/c → OI-12
Dokustrang:        OI-13a → OI-13b; OI-14a → OI-14b → OI-14c → OI-14d; OI-15; OI-16
Abnahme:           OI-17
```

Die Stränge sind logisch teilweise unabhängig, sollen im selben physischen Workspace aber wegen Single-Harness Execution nicht gleichzeitig mutieren.

---

## 8. Abnahmematrix

| Bereich | Status jetzt | Abnahmebedingung |
| :--- | :--- | :--- |
| Cloud-PDF-Anreicherung | konform | Policy, Signaturschutz, atomare Mutation und Provenienz sind getestet |
| Locking | konform | gemeinsamer ownership-gebundener Guard am Harness-Gate; kein autonomes Force; Legacy nur explizit |
| Atomare Writes | konform | ersetzende Cloud- und Mail-Desk-Writes verwenden sibling-temp + replace; Append-only ist lockgebunden |
| Data-Zone-Pfade | konform | kanonische Cloud-Pfade; Legacy-Pfade nur kontrolliert gelesen/migriert; optionale Event-Cloud-Assets nur über geerbte Cloud-Atlas-Konfigurationen |
| Cloud-Metadaten | konform | Canonical-Write, Dual-Read und Schema-Tests vorhanden |
| CLI-Envelopes | konform | kanonische Contract-Tests für Cloud- und Mail-Desk-Einstiegspunkte; Legacy nur über Opt-in-Adapter |
| Dual Evidence | konform | Beleganker und normative/empirische Trennung regressionsfrei |
| Router/Katalog | konform | sieben Sub-Skills in Root, README und zentralem Katalog konsistent |
| Token-Footprint | konform, beobachten | Der Mail-Desk-Router umfasst nach den post-audit ergänzten FR-03/04/06-Verträgen aktuell 285 physische Zeilen; Backend-, Manifest- und Detailverträge bleiben progressiv in Referenzen. Bei weiteren Fachflüssen ist erneut auszulagern. |
| Abhängigkeiten | konform | Cloud-Extras isoliert; fehlende Konverter liefern `ConversionRequired` |
| Paketkonventionen | konform | `LICENSE.txt`, valide Frontmatter und portable Pfade |
| Auditierbarkeit | konform | Ausgangs- und Implementierungscommit, Dirty State, Befehle und Evidence-Matrix dokumentiert |

---

## 9. Abschlussurteil

`office-intelligence` benötigt und besitzt keine eigene Agent-Workspace-Architektur. Die gezielte Härtung als Shared Skill Bundle ist abgeschlossen.

Die lineare Umsetzung in kleinen, separat geprüften Paketen hat sich bewährt. Kleinere Modelle waren für eng mechanische Pakete teilweise geeignet; sicherheits-, schema- und kontextreiche Pakete benötigten Terra beziehungsweise stärkeres Parent-Review. Am geprüften Stand sind alle P0-/P1-Abnahmekriterien belegt. Der Status lautet daher `conformant`.

---

## 10. Abschlussaudit-Evidenz (`OI-17`)

### 10.1 Reproduzierbarer Prüfstand

- Ausgangscommit des ursprünglichen Audits: `fb9ba3e5f7a6cc239c51ac298453159e8ddb0cfc`
- Letzter Paketstand vor Abschlusskorrekturen: `977aa4ade77da344ef5cafe596ed04cab336d897`
- Geprüfter Implementierungsstand nach Abschlusskorrekturen: `4b4d620687b495ad0972c6e33808f094c7dc4435`
- Externer `workspace-lock`-Stand: `484b28fb58be17164274b0169dcfdc15cf5ac11d`
- Git-Hygiene: Implementierungsstand clean; `.agents/session.lock` durch `.gitignore` erfasst, weder getrackt noch gestaged; kein Push im Abschlussaudit

### 10.2 Prüfungen

| Prüfung | Ergebnis |
| :--- | :--- |
| `python -m unittest discover -s skills/cloud-atlas/tests -p "test_*.py"` | 94/94 grün |
| `python -m unittest discover -s skills/mail-desk/tests -p "test_*.py"` | 54/54 grün |
| `python -m unittest discover -s skills/event-documentation/tests -p "test_*.py"` | 5/5 grün |
| `python -m unittest discover -s tests -p "test_*.py" -v` im `workspace-lock`-Repo | 12/12 grün |
| `python -m compileall -q skills` | grün |
| `quick_validate.py` mit UTF-8-Modus für Root und alle sieben Sub-Skills | 8/8 grün |
| `python scripts/validate-skills-catalog.py` im zentralen Skills-Repo | grün |
| Lokale Markdown-Linkauflösung | alle nicht externen, nicht templatisierten Links auflösbar |
| Portabilitäts- und Write-Scan | keine benutzerspezifischen Windows-Home-Pfade; keine direkten ersetzenden Textwrites außerhalb atomarer Helper |
| `git diff --check` | grün |

Der UTF-8-Modus beim Skill-Validator ist auf Windows erforderlich, weil dessen
`read_text()` sonst die lokale Codepage verwendet. Dies ist ein Validator-
Ausführungsdetail, kein Encoding-Fehler der UTF-8-Skilldateien.

### 10.3 Finding-zu-Evidenz-Matrix

| Finding | Status | Primäre Evidenz |
| :--- | :--- | :--- |
| `P1-00` OCR-Anreicherungsvertrag | geschlossen | `cloud-atlas/SKILL.md`, OCR-/Signatur-/Failure-Mode-Tests in `test_doc_conversion.py` |
| `P0-02` Lock-Vertrag | geschlossen | Root-, Cloud- und Mail-Skill; shared `workspace_lock_guard.py`; 12 Guard-Tests |
| `P1-01` atomare Writes | geschlossen | atomare Cloud-Writer; Mail-Desk `core/common.py`, `core/evidence.py`, `core/index.py`; Failure-Mode-Tests |
| `P1-02` Cloud-Metadaten | geschlossen | `core/metadata.py`, Canonical-Write-/Dual-Read-Tests |
| `P1-03` Filemap-Schemascope | geschlossen | `references/filemap.schema.json`, `references/filemap-schema.md`, Generator-/Schema-Tests |
| `P1-04` CLI-Envelopes | geschlossen | Cloud- und Mail-Desk-Contract-Tests sowie expliziter Legacy-Adapter |
| `P1-05` reproduzierbares Audit | geschlossen | Commit-/Dirty-State-Angaben und diese Prüfmatrix |
| `P2-01` Router/Katalog | geschlossen | sieben konsistente Routen in `SKILL.md`, `README.md` und `skills-catalog.yaml` |
| `P2-02` Mail-Desk-Größe | geschlossen | aktuell 146-zeiliger Router plus gezielt geladene Referenzen |
| `P2-03` optionale Cloud-Abhängigkeiten | geschlossen | `cloud-atlas/requirements-conversion.txt`, `ConversionRequired`-Tests |
| `P2-04` Event-Speicher | geschlossen und nachgeschärft | Events ohne Cloud gültig; optionale Assets über geerbte Cloud-Atlas-Konfiguration; sechs Event-Storage-Tests |
| `P2-05` Paketkonventionen | geschlossen | `LICENSE.txt`; acht erfolgreiche Skill-Validierungen |

---

## 11. Post-Audit-Wartung

Der historische Prüfstand in Abschnitt 10 bleibt unverändert. Nach `OI-17`
wurden aus der kontrollierten BOKU-Migration drei eng begrenzte Cloud-Atlas-
Wartungspakete abgeleitet:

| Paket | Ergebnis | Evidenz |
| :--- | :--- | :--- |
| B2b Konvertierungshärtung | Fehlende lokale Bildassets werden im Markdown-Mirror neutralisiert, ohne normale, externe oder andersartige Links umzudeuten. | Commit `33f923b`; fokussierte Konvertertests |
| B2c Filemap-Curation | Versionierte Curation-Overlays sind gemeinsame SSOT für Konverter und Generator; entfernte Overlay-Felder werden nicht aus generiertem Altstand wiederhergestellt, technische OCR-Metadaten bleiben erhalten. | Commit `31843be`; Schema-, Generator- und Konvertertests |
| B2d5 JSON-Fortschritt | Der Orchestrator hält `stdout` als kanonischen Abschluss-Envelope rein, puffert aber den Child-Fortschritt auf `stderr` nicht mehr bis zum Prozessende. | `sync_project_cloud.py`, Contract-Tests und Cloud-Atlas-Skillvertrag |
| B2d7 Mirror-Zielkollisionen | Reale WEEK-Evidenz: 112 unterstützte Quellen, aber 108 bisherige Stem-Mirrors; vier Gruppen mit verschiedenen Endungen und unterschiedlichen Source-SHAs konnten Provenienz überschreiben. Gemeinsame, case-insensitive Pfadplanung disambiguiert jede Gruppe zu `original.ext.md`, prüft vor Writes auf Eindeutigkeit/Output-Zone und lässt den Generator dieselbe Policy verwenden. Derivate und OCR-Pfade bleiben mangels analoger Evidenz unverändert. | `scripts/core/mirror_paths.py`, Konverter-/Generator-/Validator-Regressionen, Cloud-Atlas-Suite |
| B2e Windows-Konverter- und OCR-Runtime-Härtung | Reale Lifelong-Learning-Evidenz: Zwei Legacy-DOC-Dateien auf >320-Zeichen-Pfaden benötigen für LibreOffice ein isoliertes Kurzpfad-Staging; ein OCR-Lauf erkannte einen unvollständigen konfigurierten tessdata-Baum (`deu` vorhanden, hOCR-Konfiguration fehlte) zu spät. Cloud Atlas staged lange DOC-Wege automatisch und prüft vor OCR-Kandidaten einmalig OCRmyPDF, `deu` und hOCR fail-closed als `ConversionRequired`; digitale und No-OCR-Läufe bleiben ausgenommen. | `convert_cloud_docs.py`, DOC-/OCR-Contract-Regressionen, Cloud-Atlas-Suite |

Der erste produktive WEEK-Regenerationsversuch wurde nach rund neun Minuten
kontrolliert beendet. Die nachträgliche Codeanalyse zeigte, dass die damals
fehlende Parent-Ausgabe und noch nicht geschriebenen Mirrors keinen Stall
belegten: Der JSON-Orchestrator pufferte `stderr`, während der Konverter die
Mirrors erst nach Abschluss des gesamten Batches schreibt. B2d5 behebt diese
Beobachtbarkeitslücke; die Konvertierungs- und OCR-Semantik bleibt unverändert.

Abnahme am 10.09.2026: Der historische B2c-Prüfstand von 120/120 Cloud-Atlas-Tests
bleibt dokumentiert. B2d7 ergänzt pure Planungs-, Konverter-/Generator-,
Validator- und Stand-alone-Import-Regressionen; Commit
`198e284d8f459f254cfb81b8449db22168232c2b` belegt 132/132 grüne Tests. Das
Abschlussurteil `conformant` bleibt bestehen. Die anschließenden produktiven
Regenerationen und ihre Abnahmegrenzen sind nachstehend separat belegt.

### 11.1 Produktive Validierung in `boku-user` (10.–11.09.2026)

Die folgenden Ergebnisse betreffen ausschließlich die jeweils benannte
`cloud_sync`-Storage-Konfiguration im BOKU-Workspace. Die Läufe vom 10.09.2026
verwendeten `--no-ocr` (`enrich_source: false`). Die produktiven BOKUdrive-Läufe
vom 11.09.2026 verwendeten die reguläre `enrich_source`-Policy. Nur im großen
Lifelong-Learning-Storage war eine selektive Quellenanreicherung nötig; die
übrigen abgenommenen BOKUdrive- und ATAEL-Archivläufe benötigten kein OCR. Lokale
Mirrors und Filemaps unter `memory/cloud/` sind abgeleiteter, ignorierter Zustand.
Diese Storage-Ergebnisse erweitern weder den Architektur- noch den Conformance-
Scope dieses Shared-Skill-Berichts.

| Storage | Abnahmestatus und dauerhafte Kennzahlen | Beleg |
| :--- | :--- | :--- |
| `week.onedrive-legacy` | **accepted**: 128 Filemap-Einträge, davon 112 unterstützte Quellen mit 112 vorhandenen, case-insensitiv eindeutigen Mirrors; Filemap-/Curation-Vertrag gültig. | Commit `deb56124281b588666a29a82c300b8348ab859dd`; Receipt `boku-user/runs/20260910-091116-cloud-filemap-regeneration-b2d8-week-onedrive-retry/receipt.json` |
| `meshe.meshe-teams` | **accepted with external integrity alert**: 130/101 Quellen/Mirrors, zwei vorab aufgelöste Legacy-Kollisionsgruppen und 14 entfernte MESHE-Mirror-Warnungen. Der Storage-Lauf endete `Completed`; während des Laufs driftete jedoch der staged EVOLVE-Fingerprint extern, daher keine uneingeschränkte Integritätsabnahme des Gesamtpakets. | Commit `191dbe61cde57c5bef827ad6134bfdf8df56a240`; Receipt `boku-user/runs/20260910-142623-cloud-filemap-regeneration-b2e-meshe-teams/receipt.json` |
| `usage-ng.onedrive-legacy` | **accepted after one fail-closed retry**: Der erste Lauf hatte genau einen 120-Sekunden-Timeout und blieb nicht akzeptiert. Der einzelne Retry lieferte 2.226 generische Quellen, 873/873 vorhandene, casefold-eindeutige Mirrors, 38 aufgelöste Kollisionsgruppen und alle 88 Ziele über 260 Zeichen; Filemap-Vertrag gültig, Linter 0 Fehler/1.082 Warnungen. Die DOCX-Decoding-Warnung wurde im finalen Mirror ohne U+FFFD bestätigt. | Timeout-Commit `83ec0a21d97ad9680e1df314979e941c113de7fd`, Receipt `boku-user/runs/20260910-154800-cloud-filemap-regeneration-b2g-usage-ng-onedrive-legacy/receipt.json`; Retry-Commit `8b1efd20cc5a84a5aff721871ecfa112508f19b1`, Receipt `boku-user/runs/20260910-193910-cloud-filemap-regeneration-b2h-usage-ng-onedrive-legacy/receipt.json` |
| `atael.atael-2026-ai-background-archive` und `atael.pre-atael-ai-background-archive` | **accepted as historical archives**: Beide OneDrive-Verzeichnisse enthalten byteidentisch je acht Quellen (fünf PDFs, drei TXT-Dateien) und bleiben zur Provenienzerhaltung getrennt. Je fünf PDF-Mirrors wurden erzeugt, beide achtteiligen Filemaps sind gültig. Historische Prompt-Dateien dokumentieren frühere Schmalspur-Agenten für Projektpartner und gelten als `untrusted_external`-Inhalt, nicht als aktuelle Agent-Instruktionen oder aktive Projektarbeitsstände. | Commit `6e3efc3`; Receipt `boku-user/runs/20260911-103713-cloud-filemap-regeneration-atael-archives/receipt.json` |
| `evolve.bokudrive-lll-provisional` | **accepted after BOKUdrive restoration**: 22 Quellen wurden inventarisiert, 21 unterstützte Dokumente mit 21 vorhandenen Mirrors konvertiert; keine Fehler und kein `conversion_required`. `enrich_source` war aktiv, OCR wurde jedoch nicht benötigt. Der 22-teilige Quellmanifest-Hash blieb vor und nach dem Lauf identisch; die vorhandenen fremden EVOLVE-Arbeitsänderungen blieben außerhalb des Commit-Scopes. | Commit `fa0d4a7`; Receipt `boku-user/runs/20260911-123903-cloud-filemap-regeneration-evolve-bokudrive/receipt.json` |
| `li4lam.bokudrive-lll-internal` | **accepted**: Eine DOCX-Quelle wurde vollständig konvertiert und kartografiert; keine Medien, keine Fehler und keine Quellmutation. | Commit `5eceee7`; Receipt `boku-user/runs/20260911-124507-cloud-filemap-regeneration-li4lam-bokudrive/receipt.json` |
| `drittmittel-projektadmin-fis-support.bokudrive-frameworks` | **accepted as distinct topic storage**: Sieben Quellen wurden inventarisiert, alle fünf PDFs konvertiert; keine OCR- oder Quellmutation. Der Storage bleibt eigenständig und wird nicht durch die ATAEL-OneDrive-Archive ersetzt. | Commit `e7d5b18`; Receipt `boku-user/runs/20260911-124658-cloud-filemap-regeneration-frameworks-bokudrive/receipt.json` |
| `week.bokudrive` | **accepted**: 20 Quellen mit 20 vorhandenen Mirrors; 18 neu konvertiert und zwei aktuell übersprungen. Keine Fehler, kein `conversion_required`, keine OCR- oder Quellmutation. Nicht fatale XLSX-Metadaten-/Formatwarnungen beeinträchtigten die erzeugten Mirrors nicht. | Commit `c61ce1a`; Receipt `boku-user/runs/20260911-124931-cloud-filemap-regeneration-week-bokudrive/receipt.json` |
| `lifelong-learning.bokudrive-lll-allgemein` | **accepted after controlled recovery**: 2.092 Quellen, davon 2.089 katalogisierte Nutzdateien und 1.461/1.461 gültige Markdown-Mirrors. 76 bildbasierte PDFs wurden unter der expliziten `enrich_source`-Policy in place angereichert. Nach einem fail-closed abgebrochenen Tesseract-Konfigurationsversuch löste ein isolierter OCR-Lauf 76 Scans; 41 OCR-inkompatible PDFs wurden im No-OCR-Nachlauf gespiegelt und zwei Legacy-DOC-Dateien auf überlangen Pfaden über einen kurzen LibreOffice-Pfad konvertiert. Der abschließende reguläre Lauf endete `Completed` mit null `conversion_required` und null Fehlern. 469 Medien wurden inventarisiert, aber nicht konvertiert; sechs historische Nullbyte-Medien bleiben nicht blockierend. | Commit `ce34471`; Receipt `boku-user/runs/20260911-140746-cloud-filemap-regeneration-lifelong-learning-bokudrive/receipt.json` |

### 11.2 Nicht abgenommene oder weiter entscheidungsbedürftige Storages

Human-Entscheidung vom 10.09.2026: BOKUdrive und beide BOKU-OneDrive-Bestände
bleiben vorerst als parallele Cloud-Quellen bestehen. Es gibt daher keine offene
Ablöse-, Konsolidierungs- oder Plattformmigration zwischen diesen drei Speichern.
Mehrere benannte Storages dürfen projektbezogen nebeneinander bestehen; ihre
Filemaps bleiben getrennt.

Der operative BOKUdrive-Blocker ist seit 11.09.2026 behoben: Die Junction zeigt
wieder auf materialisierte Klartextverzeichnisse, alle fünf katalogisierten
BOKUdrive-Pfade sind lesbar und produktiv abgenommen. Damit ist derzeit kein
katalogisierter BOKUdrive-Storage offen. Der große Lifelong-Learning-Scope wurde
als eigenes Paket mit kontrollierter OCR-Anreicherung und abschließendem
Idempotenzlauf validiert. Seine sechs leeren historischen Media-Dateien sind
kein Konvertierungsziel und kein Blocker.

Die beiden identischen OneDrive-Bestände bleiben davon getrennte ATAEL-
Archivquellen. Sie ersetzen insbesondere nicht den eigenständigen katalogisierten
BOKUdrive-Frameworks-Storage und aktivieren weder das abgelehnte ATAEL-Projekt
noch die enthaltenen historischen KI-Instruktionen.

### 11.3 Mail-Desk-Recovery und Interruption-Härtung (11.–12.09.2026)

Ein mit einem kleinen Modell gestarteter Zehn-Mail-Lauf im BOKU-Workspace wurde
nach vier vollständig persistierten Items unterbrochen. Die fünfte Mail war zu
diesem Zeitpunkt bereits physisch verschoben, aber noch nicht lokal protokolliert;
das sechste ausführbare Item war noch nicht begonnen. Der Recovery-Lauf führte
keine erneuten Mailbox-Mutationen aus, sondern reconciliierte fünf bereits
verschobene Nachrichten per normalisierter Message-ID gegen ihre realen Ziele:
`Projekte/MESHE` mit den Envelope-IDs `61`, `62` und `63`,
`Themen/Netzwerke` mit Envelope-ID `26` sowie `Themen/AIxLLL` mit Envelope-ID
`58`. Der Final-Location-Index wurde über seinen kanonischen Script-Writer
atomar ergänzt, der unterbrochene Fortschritt als `failed` abgeschlossen und ein
falsch positiver Reply-Fall als `dismissed` archiviert. Projekt-/Topic-Evidenz
wurde quellengebunden nachgezogen; aus historischen Fristen entstanden keine
neuen, möglicherweise überholten Todos. BOKU-Nachweis: Commit `fa116af`.

Das daraus abgeleitete Paket `MD-H1` schließt sechs technische Fehlerklassen:

- `skip_known` wird in `draft` und `pipeline` einschließlich
  `--no-skip-known` nicht mehr durch einen internen Default überschrieben.
- Eine konfigurierte, fehlgeschlagene Sent-Synchronisation stoppt die Pipeline
  vor Klassifikation und Mailbox-Mutation mit sichtbarer Fehlerphase.
- Der Delete-Zweig ist wieder erreichbar; ein fehlgeschlagenes Löschen wird
  weder als Routing-Erfolg protokolliert noch indiziert.
- Teilweise fehlgeschlagene Execute-Läufe schreiben den Fortschrittsstatus
  `failed` statt `completed`.
- Nullable Himalaya-Absendernamen und -Betreffe brechen die Suche nicht mehr ab.
- Reads ohne geparste Message-Header werden als Fehler gemeldet und nicht als
  erfolgreich gelesene leere Mail weitergereicht.

Implementierungsnachweis: Commit `bc00898`; 192/192 Mail-Desk-Tests,
`compileall`, Mail-Desk-`quick_validate` und `git diff --check` grün. Für kleine
Modelle bleibt die empfohlene Betriebsform ein begrenzter `draft`-Lauf mit
anschließender Review und separatem `execute`, vorzugsweise in kleinen Paketen
von drei bis fünf Mails. Eine autonome Zehn-Mail-Pipeline ist trotz der neuen
Fail-Closed-Grenzen kein geeigneter Erstauftrag für Luna.

`MD-H2` ergänzt den kontrollierten Standard-Batch-Einstieg. Ein gewöhnlicher
Auftrag „verarbeite N Mails“ ist im Skill jetzt verbindlich `draft` → sichtbare
Manifest-Review → `execute` → `verify`; `pipeline` bleibt ausschließlich eine
ausdrücklich beauftragte Ausnahme. Jeder Draft trägt `expected_count`, aktuelle
`candidate_count`, `allow_fewer`, `source_folder`, Account und `skip_known` sowie
einen Pending-SHA-256 über den kanonischen Execute-Request. Vor jeder Execute-
Seitenwirkung verlangt der Runner eine explizite, hash-gebundene Approval-Receipt
und prüft Account, Quellordner und Kandidatenzahl. Weniger Kandidaten stoppen
standardmäßig; `allow_fewer: true` kann ausschließlich diesen Minderbestand nach
Review erlauben, nie einen Mehrbestand. Gate-Fehler erzeugen weder Progress-,
Index-, Log-, Evidence- noch Mailbox-Mutationen. Die bestehenden bewussten
Autonomous-Pipeline- und FR-04-Dossier-Verträge bleiben getrennt kompatibel.

MD-H2-Nachweis: `test_batch_runner_h2_contract.py` deckt Exact Match, Default-
Stop bei weniger, explizites `allow_fewer`, Stop bei mehr und die vollständige
Mutationsfreiheit von Gate-Fehlern ab; die vollständige Mail-Desk-Suite,
`compileall`, `quick_validate` und `git diff --check` sind Bestandteil der
Abnahme dieses Pakets.

`MD-H3` bindet den Transport jetzt in jeder mailbox-zugreifenden Runner-Fassade
(`inspect`, `draft`, `search`, `sync_sent`, `verify`, `execute`, `pipeline`) an die
credentials-freie Workspace-Control-Plane `.agents/mail-desk-backend.json`.
Die Datei enthält exakt Schema-Version, den unterstützten Adapter `himalaya` und
den Account (Name oder explizites `null` für den lokalen Standardaccount); sie
enthält keine Credentials. Apps, Connectorlisten und Manifeste dürfen diese
Bindung nicht wählen oder übersteuern; ein optionaler `--account`-Wert ist nur
zulässig, wenn er ihr exakt entspricht. Ein MD-H2- oder FR-04-Account
bleibt lediglich Review-Evidenz und muss exakt mit dem Workspacewert
übereinstimmen.

Vor jedem `execute` und jeder ausdrücklich autonomen `pipeline` läuft zusätzlich
ein bounded, read-only `envelope list -s 1` mit zehn Sekunden Timeout und genau
einem Versuch ohne nachgelagerten Backoff.
Der resultierende `mailbox_readiness`-Envelope ist kanonisch. Fehlende oder
ungültige Konfiguration, Accountdrift, fehlender Adapter, Timeout, Connectivity-
Fehler oder eine nicht als JSON-Liste parsebare Minimalantwort stoppen vor
Progress-, Index-, Log-, Evidence-, Handler- und Mailbox-Mutation. Die H3-
Regressionen prüfen jeden dieser Stops einschließlich der Mutationsfreiheit und
des Envelope-Shapes; sie laufen zusätzlich zur vollständigen Mail-Desk-Suite.

`MD-H4` macht den damaligen manuellen Recovery-Fall zum regulären, überprüfbaren
Runner-Vertrag. `batch-recovery-journal.json` führt pro deterministischem Batch
und normalisierter Message-ID die Phasen von Auswahl über Copy, Zielverifikation
und Source-Delete bis Index, Log und Evidence. Jede Phasenänderung ist atomar
gesichert, bevor die nächste externe oder lokale Seitewirkung beginnt. SIGINT und
Timeout führen in Journal und `runner-progress.json` zu `aborted`; sie erzeugen
weder einen Abschluss noch einen dauerhaft `running` verbleibenden Lauf.

Der neue first-class-Modus `reconcile` ist standardmäßig read-only. Er berichtet
für jede betroffene Message-ID Journalphase, erneute Zielverifikation,
Index-/Log-/Evidence-Stand und den notwendigen Recovery-Schritt. Eine lokale
Nachreparatur erfordert eine explizite freigegebene Receipt und eine frische
Zielverifikation; sie ergänzt ausschließlich fehlende lokale Index-, Log- oder
Evidence-Daten und führt niemals einen Mailbox-Copy oder -Delete aus. Ein
Execute-Wiederanlauf verifiziert ein bereits journalisiertes Ziel vor jeder
Copy-Entscheidung und kann deshalb keinen Doppel-Move erzeugen. Die
Fault-Injection-Regressionen decken Unterbrechungen nach Copy, Verify, Delete,
Index, Log und Reply-Append ab. Der jeweilige Execute-Resume darf keinen zweiten
Copy oder Delete und keine doppelte Log-, Reply- oder Evidence-Zeile erzeugen.
Der Nachweis liegt in `test_maildesk_h4_recovery.py`; vollständige Suite,
`compileall`, `quick_validate` und `git diff --check` sind Teil der Abnahme.

`MD-H5` schließt die technische Abschlussgrenze: `execute` erzeugt lediglich
einen quellengebundenen, unreleased `synthesis_candidate`. Ein
`synthesis_handoff` wird nur nach vollständig erfolgreichem quellengebundenem
Verify (direkt oder in der Pipeline) oder nach abgeschlossenem Reconcile
freigegeben. Beide Pfade liefern zusätzlich einen
versionierten `completion_report` mit den verifizierten Message-IDs. Partial,
Abort und fehlgeschlagener Verify führen sichtbar zu `recovery_required` und dem
kanonisch leeren Handoff; es gibt damit keine vorzeitige Synthese- oder
Abschlussbehauptung. `test_synthesis_handoff.py` und
`test_maildesk_h5_completion.py` decken die Fehl- und Erfolgsgrenzen ab.
Der Standalone-Verify übernimmt einen Candidate ausschließlich aus einem
strukturell vollständigen erfolgreichen Execute-Summary mit exakt passenden
Result-, Verify- und Candidate-Message-IDs. Freie Top-Level-Candidates,
partielle/abgebrochene Summaries und beliebige Envelope-`data` werden nicht als
Provenienz akzeptiert.

Das dokumentierte Luna-Betriebsprofil begrenzt neue Aufträge auf drei bis fünf
Mails und verlangt Draft → sichtbare Human-/starke-Modell-Review → Execute →
Verify → Synthese in einer linearen Session. Frische Sessions sind zwischen
vollständig abgeschlossenen, unabhängigen Batches sinnvoll, nicht innerhalb einer
Mailbox-Transaktion. Eine autonome Pipeline ist kein Luna-Erstauftrag; Count-,
Receipt-, Readiness-, Review-, Verify- und Reconcile-Fehler sind Stopbedingungen.
