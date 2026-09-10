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
| Token-Footprint | konform | Mail-Desk-Router aktuell 146 Zeilen einschließlich Guard-Vertrag; Details progressiv in Referenzen |
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

Der erste produktive WEEK-Regenerationsversuch wurde nach rund neun Minuten
kontrolliert beendet. Die nachträgliche Codeanalyse zeigte, dass die damals
fehlende Parent-Ausgabe und noch nicht geschriebenen Mirrors keinen Stall
belegten: Der JSON-Orchestrator pufferte `stderr`, während der Konverter die
Mirrors erst nach Abschluss des gesamten Batches schreibt. B2d5 behebt diese
Beobachtbarkeitslücke; die Konvertierungs- und OCR-Semantik bleibt unverändert.

Abnahme am 10.09.2026: Der historische B2c-Prüfstand von 120/120 Cloud-Atlas-Tests
bleibt dokumentiert. B2d7 ergänzt pure Planungs-, Konverter-/Generator-,
Validator- und Stand-alone-Import-Regressionen; der aktuelle Suite-Stand beträgt
132/132 grüne Tests. Das Abschlussurteil `conformant` bleibt bestehen;
eine erfolgreiche produktive WEEK-Regeneration ist damit noch nicht behauptet.
