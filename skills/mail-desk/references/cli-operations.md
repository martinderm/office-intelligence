# mail-desk CLI-Operationen

Technische Referenz für Script-Zugriffe, kanonische Envelopes, zugelassene
Werkzeugaufrufe, Final-Index-Zugangsregeln und den verpflichtenden
Compliance-Output. Fachliche Entscheidungslogik und Reihenfolge stehen im
[`SKILL.md`](../SKILL.md); Datenformate und Feldsemantik in
[`log-schema.md`](log-schema.md); Batch-Modi und Manifeste in
[`batch-runner.md`](batch-runner.md).

## Pfadkonvention

Script- und Hilfsdateipfade nach Möglichkeit relativ zur `SKILL.md` bzw. zu
ihrem Verzeichnis lesen und verwenden; keine konkurrierenden Pfadvarianten
parallel pflegen.

Für die Skriptaufrufe sind `python3` **und** `python` erlaubt; verwende die
Variante, die lokal verfügbar ist.

## Kanonische CLI-Envelope-Regel

Alle Werkzeuge unter `scripts/` emittieren im JSON-Modus ein kanonisches
Envelope mit genau diesen Top-Level-Feldern:

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

- `action`: Operationsname, nichtleere getrimmte Zeichenfolge.
- `success`: Boolean.
- `state`: nichtleere getrimmte Zeichenfolge (z. B. `Completed`, `Failed`,
  `PartialFailure`).
- `message`: menschenlesbare Zeichenfolge.
- `data`: JSON-Objekt mit Nutzlast.
- `error`: bei Erfolg `null`; bei Fehler ein Objekt mit nichtleerem `type` und
  String `message`.

Historische `ok`-/`status`-Formen sind nicht der aktuelle Vertrag. Den
befristeten [`Legacy-CLI-Adapter`](legacy-cli-adapter.md) nur laden und
verwenden, wenn ein ausdrücklich identifizierter historischer `ok`-/`status`-
Konsument weiterbetrieben werden muss; nie für neue Aufrufer.

## Modularer Kern (`scripts/core/`)

- `core/himalaya.py`: Robuste CLI-Ausführung, Header-/Preview-Extraktion,
  Encoding-Schutz und Parallelsuche.
- `core/index.py`: Atomares Lesen, Schreiben, Filtern und Lookup für
  `final-location-index.json`.
- `core/action_log.py`: Protokollierung in `action-log.jsonl`,
  `replies-needed.jsonl` und Case-Archivierung.
- `core/evidence.py`: Aktualisierung von Markdown-Evidenzen
  (`evidence/YYYY-MM.md`) mit Dublettenerkennung.
- `core/classifier.py`: Offline-Regel- und Katalog-Klassifikation mit
  Projekt-/Topic-Pattern-Matching; kompatible Facade (≤ 813 Zeilen) mit Katalog-I/O,
  Full-Reader-I/O, Thread-Referenzparsing/-Parent-Lookup, Anhangsbindung und
  Manifest-Drafting. Die kanonischen Domänen-Owner liegen unter `core/matching/`
  (`date_parser.py`, `ambiguity.py`, `project_matching.py`, `topic_matching.py`) und sind
  per Objektidentität gebunden (FR-13/MD-M1-T01–T04; die einmalige MD-M1-Revision rotiert
  vorhandene `classifier_revision`-Werte genau einmal).
- `core/envelope.py`: Zentraler Envelope-Builder für kanonische JSON-Ausgabe.
- `core/progress.py`: Atomare Fortschritts- und ETA-Statusdatei für
  Batch-Läufe.

## Standardisierte Werkzeugleiste

1. **Batch-Runner (`mail_desk_batch_runner.py`):**
   Zentraler Batch-Prozessor für Entwurf (`draft`), Routing, Verifikation,
   Indexierung, Evidenzfortschreibung und Echtzeit-Fortschrittstelemetrie.
   Modi, Manifeste, Schemas und Beispiele:
   [`batch-runner.md`](batch-runner.md).

2. **Offline-Inspektion & Katalog-Reevaluierung (`mail_desk_inspect_manifest.py`):**
   Prüft, filtert und reklassifiziert erstellte Batch-Manifeste offline gegen
   `projects.json` und `topics.json` vor der eigentlichen IMAP-Ausführung
   (`--reclassify`, `--unindexed`, `--unclassified`).

3. **Himalaya & IMAP JSON-Client (`mail_desk_himalaya_client.py`):**
   Nur beim Himalaya-/IMAP-Backend den vollständigen
   [Himalaya-/IMAP-Adapter](backends/himalaya.md) anwenden.

4. **Final Location Index Client (`mail_desk_final_location_index.py`):**
   Kapselt alle Lese-, Schreib-, Lookup-, Statistik- und Filteroperationen
   auf `final-location-index.json`.

5. **Erledigung und Fall-Archivierung (`mail_desk_resolve_case.py`):**
   Archiviert offene Einträge aus `replies-needed.jsonl` oder
   `pending-review.jsonl` direkt unter dem wochenbasierten Pfad
   `archive/YYYY-Www/` und aktualisiert den Status.

   ```bash
   python3 scripts/mail_desk_resolve_case.py \
       --message-id '...' --status 'resolved' --resolution '...'
   ```

6. **Mailbox-Preflight-Check (`mailbox_preflight.py`):**
   Validiert die Erreichbarkeit und Authentifizierung des konfigurierten
   Mailkontos vor komplexen Operationen.

7. **Batch-CLI-Wrapper (`mail_desk_batch_cli.py`):**
   Harness-freundlicher Wrapper um den Batch-Runner: persistiert Runner-Envelope
   (stdout) und Progress-Log (stderr) unter `<workspace>/tmp/mail-batch/` und
   emittiert stattdessen einen kompakten kanonischen Summary-Envelope. Für
   Harnesses mit Output-Fenster-Limits (z. B. `draft` mit großen Anhängen).
   Modi und Parameter wie beim Batch-Runner (`draft`/`execute`/`reconcile`).

   `reconcile` liest ausschließlich das Recovery-Journal des unterbrochenen
   `execute`-Laufs und erwartet **kein** `--input`-Manifest. `--account` ist
   optional; ohne Angabe bindet der Runner den Account automatisch aus der
   Workspace-Backend-Datei. `--keep N` (Default 10, Minimum 1) steuert die
   Persistenz-Hygiene: pro Dateimuster bleiben nur die neuesten N
   Envelope-/Progress-Dateien in `tmp/mail-batch/` erhalten.

   ```bash
   python3 scripts/mail_desk_batch_cli.py draft --count 10
   python3 scripts/mail_desk_batch_cli.py execute
   python3 scripts/mail_desk_batch_cli.py reconcile
   ```

8. **Katalog-Inspector (`catalog_inspect.py`):**
   Read-seitiges Gegenstück zum `catalog_validator.py` (MD-S5): liest
   `topics.json`, `projects.json` oder `mail-desk.json` aus dem Workspace und
   gibt ausgewählte Felder als kanonischen Envelope zurück. Rein lesend; die
   Pflege der Kataloge bleibt bei `topic-catalog-entry`/`project-catalog-entry`.
   `--fields` begrenzt die Ausgabe sowohl in der Listen-/Summary-Ansicht als
   auch im Einzeleintrag-Modus (`--id`): emittiert werden nur `id` plus die im
   Eintrag vorhandenen ausgewählten Felder (z. B. ohne `workpackages`).

   ```bash
   python3 scripts/catalog_inspect.py topics --id lifelong-learning
   python3 scripts/catalog_inspect.py projects --id meshe --fields id,mailbox_folder,typical_subject_patterns
   python3 scripts/catalog_inspect.py mail-desk
   ```

## Harte Regel: kein manueller Final-Index-Write

`data/mail-desk/final-location-index.json` darf **niemals manuell** editiert
oder direkt im Rohtext gelesen werden. Ausschließlich zulässig ist das
standardisierte Werkzeug `mail_desk_final_location_index.py` bzw. der
Batch-Runner:

```bash
python3 scripts/mail_desk_final_location_index.py stats
python3 scripts/mail_desk_final_location_index.py lookup --mid 'msg-2026-001@example.org'
python3 scripts/mail_desk_final_location_index.py query --folder 'Projekte/EVOLVE' --limit 20
python3 scripts/mail_desk_final_location_index.py --input data/mail-desk/index-op.json
```

Die Datei darf NIEMALS direkt mit Texteditoren, `view_file` oder `grep`
geöffnet, gelesen oder manuell bearbeitet werden (Gefahr von
Context-Window-Overflows, unvollständigem Lesen und Syntax-Korruption).

Zusätzlich erlaubt für die Index-Location:

- Standardpfad: `data/mail-desk/final-location-index.json` (empfohlen)
- optionaler Env-Override via `.env`/Umgebung:
  - `MAIL_DESK_DATA_DIR=/abs/path/to/data/mail-desk`
  - oder `MAIL_DESK_FINAL_INDEX_PATH=/abs/path/to/final-location-index.json`

Hinweis: Message-IDs für Skript-Lookups immer in normalisierter Form **ohne
`< >`** übergeben; Message-IDs mit `$` dabei in **Single Quotes** setzen,
damit die Shell nichts expandiert.

JSON-Struktur und Feldregeln sind verbindlich in
[`log-schema.md`](log-schema.md) definiert (Abschnitt
`final-location-index.json`).

## Himalaya-Operationsmanifest (`himalaya-op.json`)

Der Himalaya-Client wird ausschließlich über ein JSON-Eingabemanifest
aufgerufen; direkte Ad-hoc-Subcommands mit wechselnden Argumenten sind im
operativen Agent-Workflow nicht zulässig:

```bash
python3 scripts/mail_desk_himalaya_client.py --input data/mail-desk/himalaya-op.json
```

Manifest-Shape:

```json
{
  "account": "BOKU-MARTIN",
  "operations": [
    { "action": "list_folders" },
    { "action": "list_envelopes", "folder": "INBOX", "page_size": 20 },
    { "action": "read", "folder": "INBOX", "envelope_id": "7195" },
    { "action": "inspect_attachments", "folder": "INBOX", "envelope_id": "7195", "expected_message_id": "message@example.org" },
    { "action": "copy", "source_folder": "INBOX", "target_folder": "Projekte/USAGE-NG", "envelope_id": "7195" },
    { "action": "move", "source_folder": "INBOX", "target_folder": "Projekte/USAGE-NG", "envelope_id": "7195" },
    { "action": "delete", "folder": "INBOX", "envelope_id": "7195" },
    { "action": "search", "query": "USAGE-NG" }
  ],
  "delete_input_on_success": true
}
```

`inspect_attachments` exportiert die Nachricht read-only als RFC-822-Quelle und
liefert ausschließlich validierte, an Account, Message-ID, Folder, Envelope-ID und
Part-Locator gebundene Kandidaten. Ein optionales `operations[].account` darf nur
den Top-Level-Account wiederholen; Abweichung, fehlende Message-ID oder ungültige
MIME-Metadaten sind fail-closed Fehler. Der Inventarschritt lädt noch keinen
Anhang in eine Arbeits- oder Cloud-Ablage herunter.

`attachment_fetch` ruft einen verifizierten MD-A1-Anhangskandidaten sicher und isoliert
in den temporären Quarantäneordner `data/mail-desk/attachments/<run-id>/` ab.
Erfordert zwingend eine gültige `approval_receipt` mit passendem `request_hash` gegen
den deterministischen `review_hash`. Erzwingt Preflight-Drift-Prüfung, Quoten (15 MB einzeln,
25 MB kumulativ, max. 5 Dateien), Re-Hashing, Re-Typing, aktiven Inhalts-Blocker,
idempotente Retrys und atomare Sibling-Temp-Promotion. Nach der Lock-Prüfung und vor
dem ersten Quarantäne-Write stoppt ein bounded read-only Git-Preflight fail-closed,
falls unter `data/mail-desk/attachments/` bereits etwas getrackt ist (siehe
Stop-Bedingung unten).

## FR-15 / MD-E1: Policygebundene Anhang-Auswertung (Inspect → Fetch → Extract → Handoff)

Der `attachment_evaluate`-Orchestrator (`scripts/core/attachment_evaluation.py`) ist der
separat aufrufbare, abgenommene MD-E1-Seam (FR-15/MD-E1-T01–T07). Er verarbeitet eine
bereits als unklar klassifizierte Einzelmail und führt in einem Lauf aus:

1. **Inspect:** `inspect_attachments` (`scripts/mail_desk_himalaya_client.py`) exportiert
   die Nachricht read-only als RFC-822-Quelle und liefert an Account, Message-ID, Folder,
   Envelope-ID und Part-Locator gebundene Anhangskandidaten. Caller-seitig behauptete
   Kandidaten-, Policy- oder Fetch-Status-Werte sind keine Autorität.
2. **Fetch:** `attachment_fetch` ruft ausschließlich kanonisch revalidierte, policykonforme
   Parts (`fetch_status: "available"`, `policy_status: "allowed"`) in die Quarantäne
   `data/mail-desk/attachments/<run-id>/` ab. Autorisierung ist die intern erzeugte,
   kontextgebundene Maschinen-Autorisierung (`receipt_class: "machine"`,
   `receipt_type: "attachment_auto_evaluation"`); sie gilt nur für den MD-E1-Flow und wird
   von Human-Approval-Pfaden fail-closed abgewiesen. Lock-Ownership (`allow_legacy=False`),
   der bounded read-only Tracked-Quarantäne-Preflight, aktiver Inhalts-Blocker,
   Extension-/MIME-Konsistenz und die Quoten (15 MB einzeln, 25 MB kumulativ, max. 5
   Dateien) bleiben bindend.
3. **Extract:** `extract_attachment_content` extrahiert begrenzt (max. 15.000 Zeichen je
   Anhang); Office-/PDF-Formate nutzen ausschließlich diesen kanonischen Extraktor. Alle
   Anhänge einer Mail teilen eine `run_id`.
4. **Handoff:** Der validierte, gekapselte `attachment_analysis_handoff` entsteht mit
   `default_materiality: "required_for_decision"` (max. 30.000 Zeichen je Mail inklusive
   sichtbarer Truncation-Marker).

**Staged Ergebnis:** `attachment_evaluate` gibt ausschließlich das kanonische staged
`attachment_evaluation` zurück (`status ∈ {completed, not_needed, skipped, failed}`, bounded
`reason`, `authorization ∈ {auto_evaluated, not_applicable}`, begrenzte sichere `files[]`,
`used_for_classification` **immer** `false`, `classifier_revision` **immer** `null`) plus den
validierten `attachment_analysis_handoff`.

**Erfolg und bounded Fehler:**

- Erfolgspfad: `completed` / `handoff_ready` / `auto_evaluated` mit sicheren `files[]`.
- Validierter `blocked_on_required_attachment`-Handoff (kanonisch gültige, aber unvollständige
  erforderliche Evidenz): `completed` / `still_ambiguous` (nie `supplementary`).
- Fail-closed `failed`-Envelopes mit `authorization: "not_applicable"` und leerem `files[]`:
  `lock_unavailable`, `policy_blocked`, `quota_exceeded`, `fetch_failed`, `extraction_failed`
  (inkl. Extraktions-Timeout) und `handoff_invalid`. Langlebige Outputs enthalten nie
  Exception-Text, Rohinhalt oder absolute Pfade.

**Zero-Write-Garantie:** MD-E1 führt keine Mailbox-Schreiboperation (`copy`/`move`/`delete`),
keine Promotion, Export, Filing, Disposition, Evidence-, Katalog-, Cloud-, Classifier- oder
Cleanup-/GC-Mutation aus. Verifizierte Quarantäne-Artefakte und Inventar bleiben für MD-E2
erhalten.

**MD-E2-Fortsetzung:** Mit **FR-15/MD-E2-T01** ist die standardmäßig aktive
`draft`-Aufrufverdrahtung umgesetzt (`core/attachment_reclassification.py`): nur ein unklares
Item durchläuft `attachment_evaluate`, ein validierter `ready`-Handoff wird genau einmal als
getrenntes `untrusted_external` reklassifiziert, und jedes Draft-Item erhält genau ein
additives finales `attachment_evaluation` (`completed/classification_clear` mit
`used_for_classification: true` und 64-Hex-`classifier_revision` nur bei eindeutigem Erfolg).
Die direkten, gegenseitig exklusiven Optionen `--evaluate-attachments`/`--no-evaluate-attachments`
sind nur für `draft`/`inspect` gültig (`draft` default an, `inspect` default aus). Mit
**FR-15/MD-E2-T02** ist die Grenze fail-closed gehärtet: jede bounded MD-E1-Fehler-/No-Op-Ursache
bleibt item-lokal in Review/`INBOX` (`lock_unavailable`, `policy_blocked`, `quota_exceeded`,
`fetch_failed`, `extraction_failed`, `handoff_invalid`), Identitäts-/Quellen-Pairing-Fehler sind
Bindungsfehler (`handoff_invalid`), ein `ready`-Handoff wird vor der Klassifikation kanonisch mit
`validate_attachment_handoff` revalidiert, fortbestehende Mehrdeutigkeit erhält
`completed`/`still_ambiguous` mit `auto_evaluated` und sicheren `files[]`, `classifier_revision`
bindet den normalisierten AST des Classifier-Regelmoduls plus Kataloge und konsumierte Hashes,
unerwartete Backend-/Programmiervertragsfehler schlagen über
`AttachmentReclassificationContractError` fail-loud fehl, und ein deterministischer, PII-freier
Run-ID je Nachricht erreicht im zweiten Default-Lauf MD-E1 `already_fetched` ohne Doppel-Fetch.
Mit **FR-15/MD-E2-T03** ist der opt-in `inspect`-Vorschlag verdrahtet: `inspect` bleibt
standardmäßig rein lesend; nur `--evaluate-attachments` (`evaluate_attachments: true`) ergänzt
einen top-level, **nicht ausführbaren** `manifest_proposal` über denselben
`draft_manifest`- + `install_draft_attachment_evaluations`-Flow wie `draft`, und eine
ausführbare Batch-Manifest-Datei entsteht weiterhin nur bei explizit konfiguriertem
`manifest_file`. Ein gemischter Batch (klar/geklärt/weiterhin mehrdeutig/bounded failed)
bleibt geordnet und item-lokal. Die Paketabnahme (**FR-15/MD-E2-T04**) ist abgeschlossen:
`tests/test_batch_runner_mde2_acceptance.py` beweist in einem einzigen hermetischen realen
Pfad Body/Full-Read → mehrdeutig → realer `text/plain`-Anhang → genau eine
`untrusted_external`-Neuklassifikation → persistiertes Projekt-`DraftManifest` mit
bounded `attachment_evaluation` (`used_for_classification: true`, 64-Hex-`classifier_revision`
gebunden an Classifier-Regeln plus konsumierten Anhangs-Hash) bei null Mailbox-/Netzwerk-
und null Execute-/Promote-/Export-/Filing-/Dispositions-/Katalog-/Cloud-Seiteneffekten.
**FR-15 ist damit geschlossen**; MD-E1 endet weiterhin am validierten Handoff.

> **Nicht verwechseln:** Das vorbestehende, unabhängige Offline-Flag
> `mail_desk_inspect_manifest.py --reclassify` (siehe oben) reklassifiziert erstellte
> Batch-Manifeste offline gegen `projects.json`/`topics.json`. Es ist **nicht** die
> MD-E2-Neuklassifikation und hat mit FR-15/MD-E1 nichts zu tun.

## Workspace-Integration: Attachment-Quarantäne (Integrationsempfehlung)

Der Mail-Desk legt abgerufene Anhänge isoliert unter `data/mail-desk/attachments/<run_id>/` ab. Das Skill-Bundle selbst ist kein Mail-Desk-Laufzeitworkspace und verändert Consumer-`.gitignore`-Dateien nicht autonom. Die folgenden Integrationsregeln sind Empfehlungen für nutzende Consumer-Workspaces; vor einer Übernahme sind bestehende Workspace-Regeln sorgfältig zu prüfen, und vorhandene Regeln dürfen nicht unbesehen ersetzt werden.

### Empfohlener Integrationsblock für neue Workspaces

Für neu eingerichtete Consumer-Workspaces ohne bestehende Mail-Desk-Ignoreregeln wird folgender vollständiger `.gitignore`-Block empfohlen:

```gitignore
data/*
!data/mail-desk/
!data/mail-desk/**
/data/mail-desk/attachments/
```

### Empfehlung für bestehende Workspaces mit etablierter Mail-Desk-Negation

Besitzt ein bestehender Consumer-Workspace bereits die etablierte Negationskette
```gitignore
data/*
!data/mail-desk/
!data/mail-desk/**
```
wird ausdrücklich empfohlen, **ausschließlich** die engere Root-Regel
```gitignore
/data/mail-desk/attachments/
```
direkt nach der bestehenden Negation `!data/mail-desk/**` anzufügen, statt bestehende Regeln zu ersetzen oder zu duplizieren.

### Hygiene- und Versionierungsregeln

- **Empfehlung für Consumer-Workspaces:** Die obigen Blöcke sind Integrationsvorschläge zur Übernahme in den jeweiligen Consumer-Workspace. Der Skill verändert Consumer-`.gitignore`-Dateien nie autonom. Vor Übernahme bestehende Regeln prüfen; keine vorhandenen Regeln ersetzen.
- **Flüchtige Laufzeitdaten:** Der gesamte Unterbaum `data/mail-desk/attachments/<run_id>/` inklusive Binärdateien (z. B. PDF, Bilder, Office-Dokumente), run-lokalen Inventaren (`.quarantine-inventory.json`), Lock-Dateien (`.quarantine-inventory.lock`), temporären Sibling-Dateien (`.*.tmp`) und Extraktions-Derivaten (`derivatives/`) sind rein lokale Laufzeitdaten und bleiben strikt ignoriert. Sie werden weder als Evidence noch als Final-Index-Inhalt behandelt.
- **Versionierbare Metadaten:** Sämtliche Mail-Desk-Metadaten außerhalb des `attachments/`-Unterbaums — insbesondere `action-log.jsonl`, `replies-needed.jsonl`, `pending-review.jsonl`, `final-location-index.json`, Batch-Manifeste, `batch-recovery-journal.json`, `runner-progress.json` sowie der geplante versionierte `data/mail-desk/attachment-quarantine-index.json` (MD-Q2) — bleiben versionierbar und trackbar.
- **Stop-Bedingung bei getrackten Quarantänedateien:** Bereits versehentlich getrackte Quarantänedateien im Git-Index sind eine strikte Fail-Closed-Stop-Bedingung. Ein bounded, strikt read-only Produktions-Preflight (`quarantine_preflight.py`: `git ls-files` ohne Shell, mit Timeout) prüft dies am vertrauenswürdigen `workspace_root` nach der Lock-Ownership-Prüfung und vor dem ersten Quarantäne-Write bzw. vor dem ersten mutierenden OCR-Derivat-Write; jede getrackte Datei unter `data/mail-desk/attachments/` (inklusive `**/.quarantine-inventory.json`/`.lock`) sowie Non-Zero-Exit, Timeout oder unlesbarer Index stoppen fail-closed. Der Skill entfernt solche Dateien nie autonom aus dem Git-Index und verändert `.gitignore` nie autonom; sie erfordern manuelle Klärung vor der weiteren Ausführung.

Clientzweck und Aufrufregel stehen im
[Himalaya-/IMAP-Adapter](backends/himalaya.md); Batch-Lebenszyklus in
[`batch-runner.md`](batch-runner.md).

## Pflicht-Output pro verarbeiteter Mail

Am Ende der Bearbeitung einer Mail immer einen kompakten Compliance-Block
ausgeben:

- `routing: ok|fail`
- `metadata: ok|fail`
- `final-index-script: ok|fail`
- `reference-source-id: ok|fail|n/a`

`reference-source-id` ist `n/a`, wenn keine Wissenspflege in
`memory/references/*` nötig war.

Ohne diesen Block gilt die Bearbeitung als unvollständig.

## Operative Abschluss-Checkliste (Tool-/Script-Schritte)

Vor Abschluss eines Mail-Schritts:

1. `action-log.jsonl` aktualisiert.
2. Falls Antwortbedarf: `replies-needed.jsonl` aktualisiert.
3. Falls Review-Fall: `pending-review.jsonl` aktualisiert.
4. Final-Index über `mail_desk_final_location_index.py` oder den Batch-Runner
   aktualisiert.
5. Final-Index über `mail_desk_final_location_index.py lookup` oder
   gezielten Batch-Check gegengeprüft.
6. Compliance-Block (`routing|metadata|final-index-script|reference-source-id`)
   ausgegeben; bei keiner Wissenspflege `reference-source-id: n/a`.

Fachliche Verifikations- und Quellenpflichtschritte stehen in der
Abschluss-Checkliste im [`SKILL.md`](../SKILL.md).
