# Konformitäts- und Umsetzungsbericht: `office-intelligence` → `agent-architecture`

> **Dokument-ID:** `OI-ARCH-COMPLIANCE-2026-09`  
> **Zielobjekt:** `skills/office-intelligence` als **Shared Skill Bundle**, nicht als Agent-Workspace
>
> **Referenz-Architektur:** `Shared-Memory/agent-architecture` und `skills/authoring-guide.md`
>
> **Prüfdatum:** 2026-09-01  
> **Geprüfter Git-Ausgangspunkt:** `fb9ba3e5f7a6cc239c51ac298453159e8ddb0cfc`
>
> **Worktree bei Prüfung:** nicht clean; Bericht sowie Änderungen unter `skills/project-catalog-entry/` waren gestaged
>
> **Status:** Review abgeschlossen; Umsetzung ausständig

---

## 1. Urteil

`office-intelligence` ist fachlich stark und setzt Dual Evidence, Message-ID-Bindung, Katalog-Routing und nachvollziehbare Evidenzanker bereits überzeugend um. Der bisherige Bericht hat die richtige Modernisierungsrichtung erkannt, aber drei Punkte falsch eingeordnet:

1. Das Repository ist ein **Skill Bundle und kein Agent-Workspace**. Es benötigt daher weder eine eigene `.agents/workspace-architecture.json` noch eine vollständige Dummy-Struktur mit allen vier Data Zones.
2. Concurrency und sichere Schreiboperationen sind keine optionale P2-Verbesserung. Für mutierende Skill-Werkzeuge sind Lock-Vertrag und atomare Writes sicherheitsrelevant.
3. Die In-place-OCR in `cloud-atlas` ist als fachlich sinnvolle Anreicherung bildbasierter Cloud-PDFs zulässig, aber noch nicht als ausdrückliche Mutations-, Provenienz- und Fallback-Policy beschrieben.

Das Bundle ist deshalb **bedingt konform**. Die fachliche Architektur kann beibehalten werden; vor einer vollständigen Konformität sind die P0- und P1-Pakete dieses Berichts umzusetzen und unabhängig zu validieren.

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

**Soll:** Ein neutraler `storage_id` beziehungsweise konfigurierbarer Mount-Pfad ist der Standard. `/Agent-Share/` bleibt als dokumentiertes Legacy-/Deployment-Mapping erhalten, wenn es in bestehenden BOKU-Workspaces gebraucht wird. Eine blinde globale Ersetzung ist nicht zulässig.

### P2-05 — Paketkonventionen sind nicht vollständig konsistent

Der Authoring Guide fordert `LICENSE.txt`; im Bundle liegt `LICENSE`. Dieser Unterschied ist funktional gering, sollte aber entweder vereinheitlicht oder im Guide als erlaubte Variante dokumentiert werden.

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
| `OI-09a` | ⬜ offen | Envelope für Cloud-Sync-Wrapper | `sync_project_cloud.py`, Contract-Test | Success- und Error-Pfad sind kanonisch | `OI-04a`, `OI-04b` | Luna/Flash |
| `OI-09b` | ⬜ offen | Envelope für Filemap-Generator | `gen_filemap.py`, Contract-Test | Human- und JSON-Modus sauber getrennt | `OI-04b` | Luna/Flash |
| `OI-09c` | ⬜ offen | Envelope für Cloud-Konverter | `convert_cloud_docs.py`, Contract-Test | Fortschritt bleibt außerhalb des JSON-Envelopes | `OI-04a` | Luna/Flash |
| `OI-10` | ⬜ offen | Mail-Desk Envelope-Helper | neuer Helper unter `mail-desk/scripts/core/`, Tests | Success- und Error-Envelope zentral getestet | keine | Luna/Flash |
| `OI-11a` | ⬜ offen | Mail-Desk CLI-Gruppe A migrieren | `inspect_manifest`, `final_location_index`, `mailbox_preflight` | Contract-Tests grün | `OI-10` | Luna/Flash |
| `OI-11b` | ⬜ offen | Mail-Desk CLI-Gruppe B migrieren | `resolve_case`, `move_and_patch`, `himalaya_client` | Contract-Tests grün | `OI-10` | Luna/Flash |
| `OI-11c` | ⬜ offen | Batch-Runner-Envelope migrieren | `mail_desk_batch_runner.py`, Tests | alle Modi und Fehlerpfade kanonisch | `OI-10` | Luna/Flash mit hohem Kontext; sonst Terra |
| `OI-12` | ⬜ offen | Legacy-CLI-Adapter und Deprecation | kleiner Adapter, Referenzdoku, Tests | bekannte `ok`-/`status`-Konsumenten bleiben über Opt-in funktionsfähig | `OI-09a/b/c`, `OI-11*` | Luna/Flash |
| `OI-13a` | ⬜ offen | Bundle-Router und README synchronisieren | Root-`SKILL.md`, `README.md` | sieben Skills werden konsistent geroutet | keine | Luna/Flash |
| `OI-13b` | ⬜ offen | Zentralen Skills-Katalog ergänzen | `../skills-catalog.yaml` im Parent-Repo | Trigger vollständig; Katalogvalidator grün | `OI-13a` | Luna/Flash; eigener Parent-Repo-Lock |
| `OI-14a` | ⬜ offen | Mail-Desk-Inhalte klassifizieren | nur Analyse/Mapping-Dokument | jede Sektion hat Ziel `SKILL.md` oder konkrete Referenzdatei | keine | Luna/Flash |
| `OI-14b` | ⬜ offen | Mail-Backenddetails auslagern | `mail-desk/SKILL.md`, Backend-Referenzen | Backendregeln vollständig verlinkt | `OI-14a` | Luna/Flash |
| `OI-14c` | ⬜ offen | Manifest- und Compliance-Details auslagern | `mail-desk/SKILL.md`, neue Referenzen | Formate und Sicherheitsregeln ohne Verlust verlinkt | `OI-14b` | Luna/Flash |
| `OI-14d` | ⬜ offen | Mail-Desk-Router final kürzen | `mail-desk/SKILL.md`, Linkprüfung | Zielgröße erreicht; progressive Disclosure vollständig | `OI-14c` | Luna/Flash |
| `OI-15` | ⬜ offen | Cloud-Abhängigkeiten isolieren | `requirements.txt`, Cloud-Atlas-Doku, Fehlerpfade | Nicht-Cloud-Desks ohne Pakete nutzbar; fehlende Konverter strukturiert | `OI-09a/b/c` | Luna/Flash |
| `OI-16` | ⬜ offen | Event-Speicher abstrahieren | Event-Skill und Template | neutraler Default plus getestete/dokumentierte Legacy-Abbildung | keine | Luna/Flash |
| `OI-17` | ⬜ offen | Abschlussaudit und Regression | gesamte Testsuite, Katalogvalidator, Diff | keine P0/P1-Findings; Evidence-Matrix vollständig | alle Pflichtpakete | stärkeres Modell oder unabhängiger Reviewer |

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

Parallelität ist nur in isolierten Git-Worktrees und bei vollständig disjunkten Dateien sinnvoll. Für Luna-/Flash-Agenten ist serielle Ausführung meist robuster und günstiger als parallele Koordination. Besonders `OI-01`, `OI-03`, `OI-08` und `OI-17` erhalten ein unabhängiges Review durch ein stärkeres Modell oder einen Menschen.

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
| Cloud-PDF-Anreicherung | teilweise konform | In-place-OCR ist policy-gesteuert, versioniert, provenance-gebunden und gegen digitale/signierte PDFs abgesichert |
| Locking | nicht konform | ownership-gebundener, getesteter Mutationsvertrag |
| Atomare Writes | teilweise konform | alle ersetzenden Writes atomar; Append-Vertrag geschützt |
| Data-Zone-Pfade | weitgehend konform | veralteter Hilfetext entfernt; Legacy-Pfade explizit behandelt |
| Cloud-Metadaten | nicht konform | Canonical-Write und Schema-Tests |
| CLI-Envelopes | nicht konform | kanonische Contract-Tests für alle CLI-Einstiegspunkte |
| Dual Evidence | konform mit Verbesserungen | bestehende Beleganker und Trennung bleiben regressionsfrei |
| Router/Katalog | teilweise konform | sieben Sub-Skills konsistent dokumentiert |
| Token-Footprint | teilweise konform | Mail-Desk modularisiert, Details progressiv geladen |
| Abhängigkeiten | teilweise konform | Cloud-Extras isoliert und Fehlerzustände strukturiert |
| Auditierbarkeit | teilweise konform | Commit, Dirty State, Befehle und Evidence-Matrix dokumentiert |

---

## 9. Abschlussurteil

`office-intelligence` benötigt keine neue Agent-Workspace-Architektur. Es benötigt eine gezielte Härtung als Shared Skill Bundle.

Die Umsetzung soll nicht als große Migration erfolgen, sondern als Folge kleiner, testbarer Pakete. Luna- oder Flash-Klassen sind für den Großteil der mechanisch klaren Pakete geeignet. Sicherheits-, Schema- und Abschlussentscheidungen bleiben reviewpflichtig. Nach `OI-17` kann der Status auf `conformant` gesetzt werden, wenn alle P0-/P1-Abnahmekriterien belegt sind.
