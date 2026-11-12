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
  per Objektidentität gebunden (die einmalige Revision rotiert vorhandene
  `classifier_revision`-Werte genau einmal).
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

7. **Batch-CLI-Wrapper (`mail_desk_batch_cli.py`) — empfohlener Standardweg:**
   **Empfohlener Standard** für `draft`, `execute`, `reconcile` und `sync-sent`.
   Der Wrapper ruft den Batch-Runner auf, persistiert dessen Envelope (stdout) und
   Progress-Log (stderr) unter `<workspace>/tmp/mail-batch/` und emittiert
   stattdessen **nur** einen kompakten kanonischen Summary-Envelope. Damit bleibt
   der Caller-Kontext auch bei `draft`-Läufen mit großen Anhängen und bei langen
   Bekannt-Backlogs klein (OUT-P1, Issues #5/#6).

   **Herkunft der Envelope-Duplikation:** Der Runner selbst emittiert **genau
   einen** kanonischen Envelope auf stdout. Sieht ein Caller die Envelope doppelt,
   stammt die zweite Kopie aus dem Harness/der Shell (Log-Mitschnitt **plus**
   zusätzliche Envelope-Ausgabe), nicht aus dem Runner. Der Wrapper eliminiert
   diese Duplikation, weil er Runner-stdout/-stderr in Dateien umleitet und dem
   Caller ausschließlich den Summary-Envelope übergibt.

   Modi: `draft`/`execute`/`reconcile`/`sync-sent`; kein beliebiger Runner-Passthrough.
   `--count` muss positiv sein (Default: `draft` 10, `sync-sent` 150). Bei `sync-sent` ist `count` ein
   **Item-Budget** (kein Tageslimit). `--since YYYY-MM-DD` ist der verifizierte Recovery-Start und
   erfordert `--account`; ohne `since` läuft `sync-sent` im bestehenden direkten `--sync-sent`-Modus.
   Mailbox lesend, lokal jedoch schreibend (`sent-index.jsonl`, Wrapper-Envelope/Progress unter
   `tmp/mail-batch/`; `batch-sync-sent.json` ist nur ein optionales **Eingabe**-Manifest für die
   Auto-Discovery, kein Ergebnis) sowie den Synthese-Handoff
   `data/mail-desk/new-sent-items.json` (Issue #88, nur bei neuen Items; Vertrag und
   Fail-closed-Codes `sync_invalid_count`/`backend_binding`/`handoff_pending`/`handoff_invalid`/
   `ledger_invalid`/`sent_index_alias_invalid`/`ledger_alias_invalid`/`handoff_alias_invalid`/
   `handoff_deferred`/`sent_window_truncated`/`sync_window_failed`/`sync_failed`, `write_state`
   `none`/`state`/`unknown` (`partial` reserviert). Statische Index-/Ledger-/Handoff-/Sidecar-Aliase werden in
   allen Einstiegen vor Index-/State-Nutzung, Mailbox-Read und Handoff/Index geprüft; `alias_kind:
   unverifiable` ist ebenfalls fail-closed; `handoff.file` bleibt der Handoff-Pfad (`alias_file` separat),
   keine automatische Löschung, ein Rerun ohne manuelle Reparatur scheitert identisch:
   [`batch-runner.md`](batch-runner.md#modus-sync_sent-sent-index-und-synthese-handoff-issue-88)).
   Ein Batch > 200 wird committet, der Rest bleibt für den nächsten Lauf; Recovery über
   `sync_sent_items_by_dates_report(dates=['D'], include_next_day=False, max_pages=...)`, nicht über
   `--count`/`--sync-sent N` (keine Datums-CLI).
   Der Summary-Envelope nennt `new_count`, `reason_code`, `write_state`, `truncated`, `coverage_*`,
   `next_start`, `follow_up_hint` und `handoff` (nur Zähler);
   die Items stehen im persistierten Runner-Envelope und im Handoff. Bei `write_state: unknown`
   fehlt `new_count` (nicht verifiziert).
   Es führt keine Send-/APPEND-Aktion aus.
   Vor jeglicher Wrapper-Persistenz oder Pruning wird der eigene Workspace-Lock
   mit dem bestehenden shared Guard geprüft, ohne Legacy-Bypass. Dafür dienen
   `--workspace-lease-id`/`--workspace-conversation-id` oder die bestehenden
   `WORKSPACE_LOCK_LEASE_ID`/`WORKSPACE_LOCK_CONVERSATION_ID` der Harness-Control-Plane.
   Bei `draft` werden explizite IDs unverändert an den Runner weitergereicht;
   bei anderen Modi schützen sie nur den Wrapper, da der Runner diese Flags dort
   nicht akzeptiert. Bei `execute` prüft der Wrapper zusätzlich Backend-/Account-
   Bindung und den bestehenden H2-Vertrag vor Log-Erstellung/Pruning. Ungültige
   Aufträge stoppen ohne lokale Writes; der geprüfte Snapshot wird per stdin an
   den Runner übergeben, der dieselben Gates erneut prüft. Das Eingabemanifest
   bleibt erhalten; keine automatische Freigabe.
   `--data-dir` (relativ zu `--workspace` oder absolut) muss kanonisch exakt
   `<workspace>/data/mail-desk` des per Lease geprüften Consumer-Workspaces sein.
   Der Wrapper löst Symlinks/Junctions auf, gleicht mit der Runner-Root-Inferenz
   (`workspace_root_from_data_dir`) ab und stoppt mit `WorkspacePathError` vor
   Kindprozess/Read/Write bei externen Consumern, Escapes oder fehlendem Workspace.
   Ein fehlendes `data/mail-desk` im geprüften Workspace bleibt zulässig (der Sync
   legt es an). Der Wrapper bindet auch sein Ausgabeverzeichnis `<workspace>/tmp/mail-batch`
   vor jedem `mkdir`/Prune/Child kanonisch an denselben Workspace. Jede vorhandene
   Symlink-/Junction-/Reparse-Point-Komponente in `data`, `data/mail-desk`, `tmp` oder
   `tmp/mail-batch` wird abgewiesen (auch intern, gebrochen oder selbstreferenziell); der Lauf
   stoppt mit `WorkspacePathError`, bevor fremde Dateien angelegt, überschrieben oder gelöscht
   werden. Nicht vom Wrapper verwendete Pfade bleiben unberührt.

   **Trust-Grenze (bewusst; Same-User-Adversar außerhalb des kooperativen Lease-Scope, Issue #91):**
   Diese Prüfungen sind statisch.
   Ein konkurrierender Prozess mit Schreibrechten im selben Workspace kann Pfade zwischen
   Prüfung und Zugriff austauschen (TOCTOU) oder Hardlink-/Symlink-Aliase für Zieldateien
   anlegen; mit Python-Mitteln ist das plattformübergreifend nicht vollständig verhinderbar.
   Die Workspace-Lease ist eine kooperative Ownership-Grenze, kein Schutz gegen denselben
   Benutzer. Vollständige Isolation erfordert separaten OS-Benutzer, Namespace/Container oder
   ACLs (Issue #91); bis dahin gibt es keine Zusage „vor jeder Mutation sicher“ gegen einen
   feindlichen Mutator.
   Ein fehlgeschlagener oder unlesbarer Runner-Envelope ergibt einen fehlgeschlagenen
   Summary-Envelope und einen Nichtnull-Exit, auch bei Child-Exit 0.
   `reconcile` liest ausschließlich das Recovery-Journal des unterbrochenen
   `execute`-Laufs und erwartet **kein** `--input`-Manifest. `--account` ist
   optional; ohne Angabe bindet der Runner den Account automatisch aus der
   Workspace-Backend-Datei. `--keep N` (Default 10, Minimum 1) steuert die
   Persistenz-Hygiene: pro Dateimuster bleiben nur die neuesten N
   Envelope-/Progress-Dateien in `tmp/mail-batch/` erhalten.
   `reconcile` ist ohne Zusatzflag **read-only**. `--apply` ist nur mit
   `reconcile` zulässig, setzt `apply_local_repairs: true` und erzeugt eine
   explizite CLI-Approval-Receipt (`state: "approved"`, `reviewed_at`,
   `reviewed_by`); `--approved-by <NAME>` überschreibt den Reviewer (Default
   `cli-operator`). `--apply` repariert ausschließlich lokale
   Index-/Log-/Evidence-Records nach frischer Ziel-Verifikation und mutiert
   niemals die Mailbox. `--apply` in anderen Modi sowie `--approved-by` ohne
   `--apply` sind Argumentfehler.

   ```bash
   MAIL_DESK_SKILL=/path/to/installed/mail-desk   # installiertes Skill-Verzeichnis
   python3 "$MAIL_DESK_SKILL/scripts/mail_desk_batch_cli.py" draft --count 10
   python3 "$MAIL_DESK_SKILL/scripts/mail_desk_batch_cli.py" execute
   python3 "$MAIL_DESK_SKILL/scripts/mail_desk_batch_cli.py" reconcile
   python3 "$MAIL_DESK_SKILL/scripts/mail_desk_batch_cli.py" reconcile --apply --approved-by operator
   python3 "$MAIL_DESK_SKILL/scripts/mail_desk_batch_cli.py" sync-sent --count 150
   ```

   Die Beispiele setzen eine eigene Lease über die genannten Umgebungsvariablen
   voraus; alternativ die entsprechende ID explizit übergeben.

   **Sent-Sync-Recovery bei fehlendem Coverage-State (`sent_initial_state_required`,
   Issue #88):** Bevorzugt über den Wrapper, weil dieser die maschinelle
   `verify_workspace_lock`-Prüfung **vor** jedem Read/Write ausführt:

   ```bash
   MAIL_DESK_SKILL=/path/to/installed/mail-desk   # installiertes Skill-Verzeichnis, nicht der Workspace
   python3 "$MAIL_DESK_SKILL/scripts/mail_desk_batch_cli.py" sync-sent \
       --count 150 --since 2026-09-01 --account ACC \
       --workspace "$PWD" --workspace-lease-id "$WORKSPACE_LOCK_LEASE_ID"
   ```

   `--since` erfordert `--account` und übergibt dem Runner ein stdin-Manifest
   `{"mode":"sync_sent","folder":"Sent Items","count":150,"since":"...","account":"..."}`;
   dabei wird bewusst **nicht** der direkte `--sync-sent`-Modus gesetzt. Fehlt
   oder fremd ist die Lease, stoppt der Wrapper mit `WorkspaceLockError` vor
   jedem Read/Write. Normale Läufe bleiben `... sync-sent --count 150` ohne `since`.

   Nur falls der Wrapper nicht verfügbar ist, den Runner direkt starten; dann
   muss der Aufrufer dieselbe Lease bereits halten und verifiziert haben (der
   Runner prüft sie für `sync_sent` nicht selbst). Manifest mit **explizitem**
   Account:

   ```json
   {"mode":"sync_sent","folder":"Sent Items","since":"YYYY-MM-DD","count":150,"account":"VERIFIED-ACCOUNT"}
   ```

   ```bash
   python3 "$MAIL_DESK_SKILL/scripts/mail_desk_batch_runner.py" \
       --input data/mail-desk/batch-sync-sent.json --keep-input
   # oder
   python3 "$MAIL_DESK_SKILL/scripts/mail_desk_batch_runner.py" --stdin < data/mail-desk/batch-sync-sent.json
   ```

   `--sync-sent` darf dabei **nicht** gesetzt sein: Es gewinnt als direkter Modus und
   ignoriert das Manifest stillschweigend. `$MAIL_DESK_SKILL` ist das installierte
   Skill-Verzeichnis (im Bundle `skills/mail-desk`), nicht der Workspace-Root.

   `since` ist der **verifizierte konservative Start** (ISO-Datum, nicht future,
   kein Zeitanteil); er wird exakt übernommen, nicht mit dem Watermark gemischt.
   `count` ist ein positives Item-Budget; Pfade bleiben unter dem Workspace
   (`--data-dir`, Default `data/mail-desk/`): `sent-index.jsonl`,
   `sent-sync-state.json`, `new-sent-items.json`. Der Lauf ist eine bewusste
   Operator-Recovery, keine Live-Automatik; vorher State/Index prüfen und den
   Start verifizieren. Ist `since` jünger als die bestehende Coverage, wird es
   **nicht** angewendet (kein Coverage-Skip); der Envelope meldet
   `since_ignored: true`, `since_ignored_reason:
   "coverage_starts_before_requested_since"` und `effective_coverage_start`.

   Der Wrapper prüft vor dem Kindprozess die Workspace-Lease
   (`verify_workspace_lock`; `--workspace-lease-id`/`--workspace-conversation-id`
   bzw. `WORKSPACE_LOCK_*`). Ein direkter Runner-Aufruf umgeht diese
   Wrapper-Prüfung und akzeptiert für `sync_sent` keine Lease-Flags: Er ist nur
   zulässig, solange der Aufrufer dieselbe Lease hält und verifiziert hat.
   Zulässige Aufrufe sind der Wrapper für normale Läufe und der obige
   `--input`/`--stdin`-Manifest-Lauf ohne `--sync-sent` für die Recovery.

8. **Katalog-Inspector (`catalog_inspect.py`):**
   Read-seitiges Gegenstück zum `catalog_validator.py`: liest
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

   Auflösungsregel: Der Katalogpfad ist das aufgelöste `--workspace` (Default
   `"."`, aufgelöst gegen das Prozess-CWD) plus
   `memory/references/topics/topics.json`,
   `memory/references/projects/projects.json` bzw.
   `memory/references/mail-desk/mail-desk.json`. Aus dem Workspace-Root aufrufen
   oder `--workspace <root>` setzen; bei falschem CWD endet der Aufruf read-only
   mit `Catalog file not found.` (Exit 1).

## Sent-Ledger: Legacy-Import und Promotion-Recovery (Issue #107)

`scripts/mail_desk_sent_ledger.py` ist die schmale CLI für das erweiterte
`sent-synthesis-ledger.jsonl`; sie berührt weder Mailbox noch Netzwerk und schreibt
keine Report-Sonderdatei. Vollständiger Vertrag:
[`sent-ledger-migration.md`](sent-ledger-migration.md).

```bash
# 1) Lesender Dry-run mit Konfliktbericht und deterministischem Approval-Token.
python3 scripts/mail_desk_sent_ledger.py plan \
  --workspace <consumer> --data-dir data/mail-desk --inventory <legacy-inventory.jsonl>

# 2) Nur der exakte plan_id wird akzeptiert; mutierend unter verifizierter Lease.
python3 scripts/mail_desk_sent_ledger.py apply \
  --workspace <consumer> --data-dir data/mail-desk --inventory <legacy-inventory.jsonl> \
  --approval sha256:<plan_id> --workspace-lease-id <lease>

# 3) Crash zwischen dauerhafter Promotion und Ledger-Zeile: exaktes Tripel prüfen.
python3 scripts/mail_desk_sent_ledger.py recover-promotion \
  --workspace <consumer> --data-dir data/mail-desk \
  --message-id '<sent@id>' --vorgang-ref V-7 --evid-id EVID-2026-0042 \
  --evidence-file <durable-promotions.json> --workspace-lease-id <lease>
```

- `plan` ist rein lesend und braucht keine Lease. Es liefert `entries`,
  `conflicts`, `summary`, `plan_id`, `target_workspace_id` (Target-Hash, kein Pfad),
  `blocked` und `approval{required,token,binds}`. Der `plan_id` bindet den
  Target-Fingerprint; identische Daten in einem anderen Workspace ergeben einen
  anderen Token (A-Token ist für B stale). Der Report enthält keine absoluten Pfade.
  `blocked: true` bedeutet: kein Token, kein Write.
- `apply` verifiziert die invocation-eigene `workspace-lock`-Lease vor dem ersten
  Write, bindet das aufgelöste Ziel an genau `<workspace>/data/mail-desk` (fremder
  Workspace, Traversal oder Symlink/Junction/Reparse ⇒ `workspace_binding_invalid`,
  nichts geschrieben), baut den Plan gegen den aktuellen Ledger-/Handoff-Stand neu
  und stoppt bei Konflikt (`legacy_import_conflict`) oder abweichendem Token
  (`legacy_import_approval_stale`). Ein Teilimport wird nach frischem Dry-run
  idempotent fortgesetzt; nach vollständigem Import ist ein erneutes Apply ein
  sicherer No-op (`already_complete`). `legacy_import_write_failed` meldet
  `state: unknown`, nie „nichts geschrieben“.
- `recover-promotion` vergleicht normalisierte `message_id` plus **exakte**
  `vorgang_ref`/`evid_id`-Token. Kein Treffer, Teiltreffer, mehrere Treffer,
  widersprüchliche/doppelte Ledger-Zeilen, eine fremd gebundene EVID-ID, konkurrierende
  Evidence mit derselben EVID-ID aber fehlender `message_id` oder ein aufgelöster
  Thread-Anker mit **anderem nichtleerem** Vorgang stoppen
  (`promotion_not_found`, `promotion_conflict`, `promotion_ambiguous`,
  `promotion_recovery_incomplete`, `ledger_conflict`, `ledger_ambiguous`,
  `evid_shared_conflict`, `promotion_anchor_conflict`). Die globale EVID-Prüfung
  läuft vor dem `already_consumed`-No-op. Nur ein eindeutiger Treffer hängt
  höchstens eine Ledger-Zeile an; `workspace_binding_invalid` gilt wie bei `apply`.
- Exit `0` = Erfolg/No-op, `2` = fail-closed, `1` = unerwarteter Fehler. Statische
  Ledger-/Handoff-/Sidecar-Aliase bleiben fail-closed; ein `.torn`-Rest wird vor
  dem Append ins Sidecar verschoben und nicht zerstört.
- **Versionsskew:** `schema_version` bleibt 1; alte Zeilen ohne `outcome`/`source`
  gelten als `synthesized`/`synthesis`. Ein alter Bundle-Reader lehnt neue Felder
  als `ledger_invalid` ab; vor dem ersten erweiterten Write den installierten
  Bundle-Pin auf Issue #107 aktualisieren.

## Begrenzte Ziel-Verifikationsdiagnose (Issue #12)

`verify_in_target_folder(..., diagnostics=<dict>)` ergänzt `header_reads` für
genau diesen Aufruf: maximal sechs Einträge (ein optionaler Fast-Path plus fünf
Kandidaten/Fallbacks). Jeder Eintrag enthält ausschließlich:

- `phase`: `fast_path`, `candidate` (Subject- oder Datumsfilter) oder `fallback`;
- `elapsed_ms`: nichtnegative gemessene Header-Aufrufdauer einschließlich
  vorhandener Subprozess-/Cleanup-Kosten, kein reines Netzwerk-Latenzmaß;
- `outcome`: `match`, `mismatch`, `missing_message_id`, `timeout` oder `error`.

Die Liste wird pro Aufruf ersetzt, nicht über Wiederholungen angehäuft. Keine
Rohheader, Message-IDs, Envelope-IDs oder Exception-Texte in diesen neuen Feldern.
`timeout` klassifiziert bekannte Timeoutfehler; andere Ausnahmen bleiben `error`.
Ein leeres Array kann auch bedeuten, dass die Listung vor dem ersten Header
scheiterte; es beweist nicht die Abwesenheit der Nachricht.

Reconcile gibt die Liste im bestehenden `verify_diagnostics`-Report zurück.
Bei erschöpftem Execute-Copy-Verify stehen die Headerdaten **nur des letzten
Versuchs** in `partial`-Journal-Details als `verify_last_header_reads`; bestehende
Gesamtdauern pro Versuch bleiben erhalten. Keine zweite Persistenz oder neue CLI.
Headerbudget (10 s, ein Versuch), Listingbudget (30 s, zwei Versuche), vier äußere
Copy-Verify-Versuche, Message-ID-/Zielbindung und Erfolgskriterien bleiben
unverändert. Diagnose ist weder eine neue Trustquelle noch ein Latenzfix und
autorisiert keine zusätzlichen Live-Lese-/Schreibzugriffe.

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
    { "action": "save_draft", "to": "a@example.org", "subject": "Betreff", "body": "Text" },
    { "action": "delete_draft", "envelope_id": "41", "folder": "Drafts" },
    { "action": "replace_draft", "envelope_id": "41", "to": "a@example.org", "subject": "Betreff", "body": "Text" },
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

Bei nichtleeren RFC-822-Quellen validiert `inspect_mime_tree` vor der Inventur
den gesamten MIME-Baum. Defekte vor oder nach der Dekodierung, mehrfache
`Content-Type`-, `Content-Transfer-Encoding`- oder `Content-Disposition`-Header,
nicht dekodierbare Nutzlasten, unbekannte Transferkodierungen und
`message/rfc822` lösen `MimeStructureError`
aus; ein partielles Inventar wird nicht zurückgegeben. Der bisherige Umgang mit
Leerinput und die Prüfung des RFC-822-Headerabschlusses ändern sich nicht.
Die Draft-Reklassifikation behandelt den konkreten Strukturfehler elementlokal
als `handoff_invalid` mit Review/INBOX, ohne den übrigen Lauf abzubrechen.

`attachment_fetch` ruft einen verifizierten Anhangskandidaten sicher und isoliert
in den temporären Quarantäneordner `data/mail-desk/attachments/<run-id>/` ab.
Erfordert zwingend eine gültige `approval_receipt` mit passendem `request_hash` gegen
den deterministischen `review_hash`. Erzwingt Preflight-Drift-Prüfung, Quoten (15 MB einzeln,
25 MB kumulativ, max. 5 Dateien), Re-Hashing, Re-Typing, aktiven Inhalts-Blocker,
idempotente Retrys und atomare Sibling-Temp-Promotion. Nach der Lock-Prüfung und vor
dem ersten Quarantäne-Write stoppt ein bounded read-only Git-Preflight fail-closed,
falls unter `data/mail-desk/attachments/` bereits etwas getrackt ist (siehe
Stop-Bedingung unten).

## Ungesendete Entwürfe (`save-draft`, `delete-draft`, `replace-draft`)

Die drei eigenständigen Entwurfsoperationen sind **kein Batch-Modus** und haben
**kein Approval-Gate**: ein Entwurf liegt ungesendet in der Mailbox, Senden/SMTP
bleiben out of scope. Sie laufen über dieselbe JSON-Manifest-Oberfläche wie die
übrigen Aktionen (Manifest-Aktionen `save_draft`, `delete_draft`, `replace_draft`)
oder direkt als Subcommand:

```bash
python3 scripts/mail_desk_himalaya_client.py save-draft -t a@example.org -s "Betreff" -b "Text" --json
python3 scripts/mail_desk_himalaya_client.py delete-draft 41 -f "Drafts" --json
python3 scripts/mail_desk_himalaya_client.py replace-draft 41 -t a@example.org -s "Betreff" -b "Text" --json
```

- **Drafts-Ordner (deterministisch, fail-loud):** ohne `--folder` löst der Client
  den Zielordner aus der Live-Ordnerliste auf: zuerst das RFC-6154-Attribut
  `\Drafts` (forward-kompatibel; die installierte Himalaya-Fassung listet Ordner
  ohne Attributspalten), dann der konventionelle Name `Drafts`, dann die
  GroupWise-Konvention `Work In Progress`. Ist nichts davon vorhanden, bricht die
  Operation fail-loud mit einem begrenzten `drafts_folder_unresolved`-Fehler ab;
  ein hartkodierter Ordnername ist kein Fallback.
- **UTF-8-sicher über stdin (Issue #11):** Die Nachricht wird als roher
  RFC-822-Text in UTF-8 (SMTPUTF8, CRLF) gebaut und als exakte Bytes über die
  **binäre stdin-Pipe** an `message save` (IMAP APPEND) übergeben — **nie** als
  argv-Literal. Himalaya 1.2.0 (Rev `1b70c4e`) ignoriert das rohe
  `MESSAGE`-Argument im Nicht-TTY-/Nicht-JSON-Modus vollständig und konsumiert
  stdin (Rust `lines()` + CRLF-Join, finaler Zeilenabschluss entfällt). Deshalb
  nutzt **jede** Draft-Größe den stdin-Pfad (Schwelle 0, kein Größen-Split): ein
  argv-Pfad wäre still wirkungslos und würde bei > ~32 k Zeichen zusätzlich am
  Windows-Kommandozeilenlimit (`WinError 206`) scheitern. `-o plain` ist
  explizit gesetzt, damit der JSON-Zweig stdin nicht umgeht; ein zusätzliches
  Transport-CRLF kompensiert die `lines()`-Normalisierung, sodass der
  angehängte Entwurf byte-genau dem kanonischen Builder-Raw entspricht.
  Betreff, Empfänger und Body bleiben byte-erhalten; es entsteht kein
  RFC-2047-Downgrade. Der Payload wird strict als UTF-8 validiert, **bevor**
  Executable-/Config-Auflösung und Subprozess starten (ungültige Bytes →
  begrenzter `ValueError` ohne Payload-Inhalt; Himalaya würde via `lines()`
  sonst still abschneiden). Das Ergebnis trägt das Envelope
  `{envelope_id, folder, subject, to, date}`. Nur ein `HimalayaInvocationError`
  endet klassifiziert (`draft_save_failed`) ohne Verifikation, ohne
  Journal-Eintrag, ohne argv-Fallback und ohne Wiederholung (`max_retries=1`);
  andere Fehler propagieren. `OSError` (Start oder Kommunikation, Payload- und
  Nicht-Payload-Pfad) → `himalaya_launch_failed` mit unbekanntem Ausgang.
- **Löschen ohne UID MOVE:** `delete-draft` setzt `\Deleted` direkt am Envelope
  (`flag add deleted`) und entfernt die markierte Nachricht danach mit
  `folder expunge`. `message delete` wird bewusst nicht verwendet: für einen
  Nicht-Trash-Ordner verschiebt Himalaya die Nachricht in den Trash-Ordner.
- **Ersetzen (neu-erst-dann-löschen):** `replace-draft` hängt zuerst den neuen
  Entwurf an und löscht erst danach den alten (`replace_envelope_id`). Das
  Crash-Fenster zwischen beiden Schritten ist dokumentiert: bricht der Lauf
  dazwischen ab, sind beide Entwürfe sichtbar, weil der neue bereits durabel
  angehängt ist. Das Draft-Journal hält beide Envelope-IDs fest.
- **Draft-Journal:** Jede Operation schreibt einen Eintrag in
  `data/mail-desk/draft-journal.json` (Schema 1) — je Envelope-ID `envelope_id`,
  `folder`, `subject`, `to`, `date` und `action`; beim Ersetzen zusätzlich
  `old_envelope_id` und `crash_window`. Das Journal ist bewusst eine eigene Datei
  und **nicht** das `batch-recovery-journal.json` des Batch-Pfads.

### Entwürfe mit Dateianhängen (Issue #18)

```bash
python3 scripts/mail_desk_himalaya_client.py save-draft -a konto -t a@example.org -s "Betreff" -b "Text" \
  --attach data/angebot.pdf --attach memory/cloud/notiz.txt --lease-id <lease> --json
python3 scripts/mail_desk_himalaya_client.py replace-draft 41 -a konto -t a@example.org -s "Betreff" -b "Text" \
  --attach data/angebot.pdf --old-message-id "<alt@example.org>" --json
python3 scripts/mail_desk_himalaya_client.py replace-draft 41 -a konto -t a@example.org -s "Betreff" -b "Text" \
  --drop-attachments --old-message-id "<alt@example.org>" --json
```

Manifest-Aktionen `save_draft`/`replace_draft` kennen `attachments` (Liste aus
Pfad-Strings oder Objekten `{"path", "filename", "content_type"}`; `[]` = bewusst
keine) und `old_message_id`. Eine Lease im Manifest wird nie berücksichtigt.

- **Grenzen (inklusiv, vor dem APPEND geprüft, nichts wird gekürzt):** höchstens 5
  Anhänge, höchstens 3 MiB (3 × 1.048.576 Byte) je Originaldatei, höchstens
  10 MiB (10 × 1.048.576 Byte) für die **vollständig serialisierte** MIME-Mail. Der
  Base64-Aufschlag (4/3 plus CRLF je 76 Zeichen) ist enthalten; das reine
  Transport-CRLF des stdin-Pfads zählt nicht. Niedrigere Serverlimits bleiben
  wirksam und enden als `draft_save_failed`.
- **Pfade und Namen:** Pfad relativ zur Workspace-Wurzel, `/`-getrennt, beginnt
  exakt mit `memory/references/`, `memory/evidence/`, `memory/cloud/` oder `data/`;
  `data/mail-desk/` (Journale, Quarantäne, Indizes) ist ausgeschlossen. Kein
  absoluter Pfad, `..`, `\`, `:`, Steuerzeichen, Gerätename oder Endpunkt/-leerzeichen.
  Symlinks und Junctions werden auf der ganzen Kette ab der Workspace-Wurzel
  (Zonenwurzel eingeschlossen) abgewiesen; das geöffnete Handle muss vor dem
  ersten gelesenen Byte dieselbe reguläre Datei sein wie die geprüfte Kette
  (`attachment_changed` sonst). Dateiname: höchstens 128 Zeichen / 255 UTF-8-Byte,
  keine Steuer-/Bidi-Zeichen oder Separatoren, eindeutig je Mail. Content-Type aus
  einer festen Endungstabelle, sonst `application/octet-stream`; explizit nur
  klein geschrieben `type/subtype` ohne Parameter, nie `multipart/*`/`message/*`.
- **Header und Message-ID:** Betreff, An, Cc, Von und Datum dürfen keine Steuer-,
  Zeilen- oder Absatztrenner-Zeichen enthalten (`draft_header_invalid`); eine
  vorgegebene Message-ID muss `<lokal@domain>` im einfachen Atom-Format sein
  (`draft_message_id_invalid`). Fehlertexte nennen Anhänge nur per Index und
  enthalten nie Dateiinhalt, Aufruferpfade oder Backend-Stderr.
- **Fehlercodes:** `attachment_*` (Spezifikation, Pfad, Link, Größe, Name, Typ),
  `message_too_large`, `draft_binding_failed`, `draft_lock_missing`,
  `draft_message_id_not_unique`, `draft_journal_write_failed`, `draft_save_failed`
  (eindeutig gescheitert), `draft_append_outcome_unknown` (nie wiederholt),
  `draft_verification_failed`, `draft_replace_would_drop_attachments`,
  `draft_old_unreadable`, `draft_old_identity_mismatch`,
  `draft_replace_old_not_deleted`.
- **Ersetzen:** Ohne `--attach`/`--drop-attachments` wird ein alter Entwurf mit
  Anhängen, mit MIME-Parser-Defekten, mit mehrdeutiger Message-ID oder nicht lesbar nie
  ersetzt (`draft_replace_would_drop_attachments` / `draft_old_unreadable`); ohne Journal
  (`draft_binding_failed`) auch nicht. Nur ein beweisbar reiner Text-Entwurf wird ersetzt,
  und zwar über **denselben** verifizierten Ablauf wie mit Liste (kein Header-only-Verify,
  kein Löschen per veralteter Envelope-ID): Message-ID des alten Entwurfs aus dem
  gelesenen Entwurf (ein angegebenes `--old-message-id` muss passen) → Neu-Message-ID-
  Vorprüfung → genau ein APPEND → eindeutiges semantisches Rücklesen → Journal → alte ID
  per Message-ID neu auflösen → Löschen. Mit expliziter Liste zusätzlich: Bindung/Lease →
  Dateien → Größen, `--old-message-id` Pflicht.
- **Größe immer:** Die 10-MiB-Grenze der vollständig serialisierten Mail gilt für jeden
  Entwurf, auch ohne Anhänge sowie bei `[]`-Save und `[]`-Replace; Überschreitung wird vor
  dem APPEND abgewiesen (`message_too_large`).
- **Eindeutigkeit und Ordnergröße:** Die Message-ID-Suche liest den `Message-ID`-Header
  **jeder** Envelope des Ordners (höchstens 50 Lesungen, vor dem APPEND mit Reserve für die
  neue Mail). Ein Drafts-Ordner mit mehr als 49 Nachrichten ist im Anhangsmodus und beim
  Replace daher fail-closed (`draft_message_id_not_unique`), bis er aufgeräumt ist; mehrere
  `Message-ID`-Header in einer Mail machen die Suche ebenfalls unvollständig.
- **Rücklesen:** Es werden zusätzlich Parser-Defekte, doppelte Identitäts-/Empfänger-Header,
  Date, unbeabsichtigte Cc/From, Bcc/Sender/Reply-To/Resent-* und die flache MIME-Struktur
  geprüft. `From` bleibt im Builder optional; ein gespeichertes, nicht beabsichtigtes `Cc`
  wird nie stillschweigend akzeptiert.
- **Grenzen des Nachweises:** Die Tests sind ein Offline-Modell des Backends, kein
  Live-Nachweis. Unter Windows gibt es kein `O_NOFOLLOW`; ein Austausch zwischen
  Prüfung und `open` wird vor dem Lesen erkannt (Identitätsvergleich), nicht
  verhindert. Hardlinks innerhalb einer Zone sind reguläre Dateien und werden nicht
  erkannt. Der CLI-Export landet vollständig auf der Platte, bevor Python die Datei mit
  harter Grenze (12 MiB + 1 Byte, ein reguläres Handle, Identitäts-/Wachstumsprüfung)
  lädt: die Grenze schützt den Python-Speicher, nicht die vorgelagerte Backend-Übertragung.

### Vorbereitete Rohmail (Raw-EML-Import, Issue #18)

```bash
python3 scripts/mail_desk_himalaya_client.py save-raw-draft -a konto --eml data/eingang/angebot.eml \
  --lease-id <lease> --json
```

Manifest-Aktion `save_raw_draft` mit `{"eml": "data/eingang/angebot.eml", "folder": "Drafts"}`
(`folder` optional). Das Schema ist geschlossen: erlaubt sind nur `action`, `eml`, `folder` und
`account` (`account` muss dem Manifest-Account entsprechen, sonst `Account drift`; die Fehlermeldung
nennt `'eml'`, `'folder'` und `'account'`); `subject`, `to`, `cc`, `body`, `attachments`,
`lease_id` und jeder weitere Schlüssel werden abgewiesen; die Lease kommt nur aus `--lease-id`/
`--conversation-id` bzw. der Umgebung. Die Mail ist die Datei - es gibt keine Kompositions-Flags
und kein `--attach`. Ein `replace` für Rohmails gehört nicht zu diesem Umfang.

- **Kein zweiter Schreibpfad:** Dieselbe Kette wie bei Builder-Entwürfen (`_append_and_verify`):
  Bindung (Workspace-Wurzel aus `data/mail-desk`, Journal, echte Lease mit `allow_legacy=False`,
  gebundener Account) **vor** dem ersten Dateizugriff, Datei lesen und prüfen, Message-ID-Vorprüfung
  (muss im Zielordner fehlen und beweisbar fehlen; Ordner mit mehr als 49 Nachrichten sind
  fail-closed), Journal `append_started`, **genau ein** binärer stdin-APPEND, niemals wiederholt,
  semantisches Zurücklesen genau einer gespeicherten Mail. Ein unklarer APPEND-Ausgang
  (`draft_append_outcome_unknown`) wird journalisiert und nie wiederholt.
- **Quelle:** genau eine reguläre Datei `*.eml` unterhalb von `memory/references/`,
  `memory/evidence/`, `memory/cloud/` oder `data/` (nicht `data/mail-desk/`); gleiche Pfadregeln,
  Link-/Junction-Abweisung und Handle-Identitätsprüfung wie bei Anhängen (ein gemeinsamer begrenzter
  Leser). Höchstens **10 MiB** als Datei **und** nach der Normalisierung (LF→CRLF kann eine Datei
  über die Grenze heben: `raw_eml_message_too_large`); die Prüfung geschieht aus `fstat` vor dem Lesen.
- **Profil (alles andere fail-closed, inhaltsfreie Codes):** gültiges UTF-8
  (`raw_eml_not_utf8` - der stdin-Transport erlaubt nur UTF-8, es wird nichts umkodiert), kein NUL
  (`raw_eml_nul_byte`), kein einzelnes CR (`raw_eml_bare_cr`), Zeilen höchstens 998 Oktette
  (`raw_eml_line_too_long`). Header strikt nach RFC 5322 (`raw_eml_header_invalid`): Feldname aus
  druckbarem US-ASCII, kein Leerraum vor dem Doppelpunkt, Faltung nur durch führendes Leerzeichen/
  Tab (kein leeres Fortsetzungsfeld), keine Steuer-/Zeilen-/Absatztrenner-Zeichen außer Tab, höchstens
  100 Felder und 64 KiB Header, Trennzeile Pflicht. Genau **eine** `Message-ID` der Form
  `<dot-atom@hostname>` (`raw_eml_header_missing` / `raw_eml_header_duplicate` /
  `raw_eml_message_id_invalid`; Kommentare, Mehrfach-IDs, Anführungszeichen, Domain-Literale und
  Nicht-ASCII-Hosts werden abgewiesen); `To` und `Date` nicht leer; `From`, `Sender`, `Reply-To`,
  `To`, `Cc`, `Bcc`, `Subject`, `Date`, `In-Reply-To`, `References`, `MIME-Version` und **jedes**
  `Content-*` (auch `Content-ID`, `Content-Description`, `Content-Language`, `Content-Location`) höchstens
  einmal (Groß-/Kleinschreibung egal). Alle Header, auch `Bcc`/`X-Unsent`, bleiben erhalten; die
  Nachrichten-Header **und** die Header-Blöcke aller MIME-Teile werden im Rücklesen Name für Name
  verglichen (siehe Nachweis). Die Datei wird nie verändert außer durch die Zeilenende-Normalisierung.
- **Adresse und Datum (strikte RFC-5322-Grammatik):** `To` muss eine wohlgeformte Adressliste sein
  (Mailboxen und Gruppen; Anzeigename in UTF-8 oder RFC 2047 erlaubt; Kommentare erlaubt, soweit der
  Standardbibliotheks-Parser sie ohne Defekt annimmt, auch mehrere und verschachtelte; ASCII-`addr-spec`;
  kein überzähliger Text; kein Parser-Defekt): `raw_eml_address_invalid` (z. B. `not-an-address`, `a@`,
  `Doe, John <j@example.org>`, `a@example..org`). Ein eigener Scan trennt Listenelemente nur an Kommas
  **außerhalb** von Anführungszeichen und Kommentaren: `"Doe, , John" <a@example.org>`,
  `a@example.org (Kommentar, , x)` und `"a, , b"@example.org` sind gültig, leere Elemente
  (`a@example.org,`, `,a@example.org`, `a@x,,b@x`, `Group: a@example.org,;`) und offene Anführungszeichen
  oder Kommentare werden abgewiesen. **Whitespace:** Nur ASCII-Leerzeichen und Tab trennen Tokens.
  Unicode-Whitespace (NBSP, EM SPACE, ...) ist **nur innerhalb von Kommentaren und innerhalb von
  Anzeigenamen in Anführungszeichen** erlaubt (RFC-6532-Text); außerhalb davon - zwischen Tokens, am
  Anfang oder Ende, in Domain oder Local-Part, zwischen Adresse und Kommentar (z. B. `a@x,<NBSP>b@x`) - wird
  die Liste abgewiesen. Auch eine Adresse in Anführungszeichen (`"a<NBSP>b"@x`) bleibt wegen der
  ASCII-`addr-spec`-Regel abgewiesen. **Leere Gruppe mit CFWS (dokumentierte Grenze, Python 3.12):**
  `email.headerregistry` der Standardbibliothek wirft einen `AttributeError`, sobald auf eine leere Gruppe
  Leerraum oder ein Kommentar folgt (`Group:; , a@example.org`, `a@example.org, Group:; , b@example.org`,
  `Group:; (c), a@example.org`); der Parser wird nicht gepatcht, die Liste bleibt fail-closed abgewiesen
  (`raw_eml_address_invalid`), obwohl sie nach RFC 5322 gültig wäre. Ohne CFWS (`Group:;`,
  `Group:;, a@example.org`) und bei nichtleeren Gruppen mit CFWS wird sie angenommen; auf einem Interpreter
  ohne diesen Fehler würde sie ebenfalls angenommen (die Tests springen dann). Andere Adress-Header werden erhalten und
  verglichen, aber nicht auf Grammatik geprüft. `Date` muss ein RFC-5322-`date-time` sein: optionaler
  Wochentag (muss zum Datum passen), 1-2-stelliger Tag, Monatsname, 4-stelliges Jahr 1900-9999, reales
  Kalenderdatum, Stunde 00-23, Minute 00-59, Sekunde 00-60 (optional), numerische Zone `±hhmm` (hh 00-23,
  mm 00-59), alles mit **ASCII-Ziffern** `0`-`9` (arabisch-indische oder Vollbreiten-Ziffern werden
  abgewiesen) und ASCII-Leerzeichen zwischen den Feldern; obsolete Formen (zweistelliges Jahr, `GMT`/`EST`)
  und `Sat, 99 Foo 2026 99:99:99 +9999` werden mit `raw_eml_date_invalid` abgewiesen. Erlaubt ist höchstens
  ein Trailing-Kommentar ohne Verschachtelung (`+0100 (CET)`); sein Inhalt darf beliebige Zeichen außer
  Klammern enthalten, auch Unicode-Whitespace (`(CET<NBSP>x)`), während Unicode-Whitespace außerhalb des
  Kommentars (zwischen den Feldern, vor dem Kommentar, am Anfang oder Ende) mit `raw_eml_date_invalid`
  abgewiesen wird.
- **Teil-Header und MIME-Werte (`multipart/mixed`, Nachrichten- und Teil-Ebene):** Der Header-Block
  **jedes** MIME-Teils wird zuerst mit denselben strikten Regeln wie der Nachrichten-Header zerlegt
  (`raw_eml_part_header_invalid`): gültige Feldzeilen und Faltung, keine Steuer-/Trennzeichen außer Tab,
  höchstens 100 Felder / 64 KiB, Trennzeile Pflicht. Doppelte oder widersprüchliche `Content-Disposition`,
  `Content-Type`, `Content-Transfer-Encoding` und jedes weitere `Content-*` sowie `MIME-Version` im selben
  Teil werden abgewiesen. Danach werden `Content-Type`, `Content-Disposition` und
  `Content-Transfer-Encoding` von einem eigenen strengen Parser gelesen (RFC 2045/2231,
  `raw_eml_mime_invalid`) - **nicht** über die toleranten Getter der Standardbibliothek: genau ein
  `type/subtype` (`text/html, text/plain` und jedes nachfolgende Zeichen werden abgewiesen), Parameter als
  Token oder Anführungszeichen-String mit `\`-Escapes, Leerraum nur um `;` (nie um `=`), kein Kommentar,
  kein hängendes `;`; derselbe Parametername (`filename`, `name`, `charset`, `boundary`, ...) höchstens
  einmal, auch nicht gleichzeitig als `name` und `name*`. RFC 2231 wird unterstützt (`name*=utf-8''...`,
  `name*0=`/`name*0*=`-Fortsetzungen, nur Charsets UTF-8/US-ASCII, lückenlose Abschnitte, gültige
  `%XX`), weil die Standardbibliothek selbst lange Dateinamen so schreibt. `boundary` muss genau einmal
  vorkommen und aus 1-70 gültigen Zeichen bestehen. Die Sicht des strengen Parsers muss mit der der
  Standardbibliothek übereinstimmen (Content-Type, Boundary, Dateiname, Anzahl der Teile), sonst
  `raw_eml_mime_invalid`. **Parameterposition:** `name` gilt ausschließlich im `Content-Type`, `filename` ausschließlich in der
  `Content-Disposition` - überall: in einer anderen Position (`Content-Type: ...; filename=...`,
  `Content-Disposition: ...; name=...`, auch als `filename*`/`name*`) werden sie abgewiesen, ob sicher,
  gleich oder pfadartig. Der Text (Nachricht oder erster Teil) darf weder `Content-Disposition: attachment`
  noch einen `name`/`filename` in irgendeiner Position tragen (`raw_eml_structure_unsupported`); `inline`
  ohne Dateinamen ist erlaubt. Ein `multipart/mixed`-Container trägt `name`/`filename` ebenfalls in keiner
  Position (`raw_eml_structure_unsupported`). Ein Anhang braucht `attachment` mit `filename` in der
  `Content-Disposition`; `filename` und, falls vorhanden, das `name` des `Content-Type` müssen die
  Dateinamen-Regeln erfüllen (kein `/`, `\`, `:`, `..`, Gerätename, ...) und gleich sein; ein `filename` im
  `Content-Type` oder ein `name` in der `Content-Disposition` ist ein Verstoß (`raw_eml_attachment_invalid`).
- **Teile-Grenze:** Höchstens **64 MIME-Teile** (Textteil plus Anhangsteile) werden überhaupt gezählt: das
  65. Delimiter stoppt den Scan mit `raw_eml_part_count_exceeded`, **bevor** ein Teil-Header-Block
  geparst oder die Standardbibliothek aufgerufen wird. Das Produktlimit bleibt 5 Anhänge
  (`raw_eml_attachment_count_exceeded`); die 64 begrenzen nur die Arbeit.
- **Struktur:** `text/plain` (Charset UTF-8 oder US-ASCII, Encoding 7bit/8bit/quoted-printable/
  base64; ohne MIME-Header gilt US-ASCII, ein Text mit Umlauten braucht `MIME-Version: 1.0` und
  `Content-Type: text/plain; charset=utf-8`) oder flach `multipart/mixed` mit dem Text als erstem Teil
  und höchstens **5** `attachment`-Teilen, je Base64 (streng, nur das Alphabet), dekodiert 1 Byte bis
  **3 MiB**, gültiger und eindeutiger Dateiname (Regeln der Anhangsliste), streng geparster Content-Type
  (`raw_eml_attachment_invalid` / `raw_eml_attachment_count_exceeded` / `raw_eml_attachment_too_large`).
  Nicht unterstützt (`raw_eml_structure_unsupported` / `raw_eml_mime_invalid`): HTML, `multipart/
  alternative|related`, geschachtelte oder `message/*`-Teile, Preamble/Epilog-Text, ein Anhang mit
  `inline`, jeder MIME-Parser-Defekt, fremde Charsets.
- **CRLF-Normalisierung (Himalaya):** Himalaya 1.2.0 liest die stdin-Nachricht mit Rust `lines()`,
  entfernt jedes `\r` und verbindet mit CRLF, ohne finales Zeilenende. Die Datei wird deshalb **vor**
  dem APPEND zu kanonischen CRLF-Zeilen (LF und CRLF gemischt erlaubt, einzelnes CR nicht), mit genau
  einem abschließenden CRLF falls es fehlt (Leerzeilen am Ende bleiben), und mit dem einen
  Transport-CRLF des bestehenden Pfads gesendet. Die gespeicherte Mail muss der normalisierten
  entsprechen. Ergebnis und Journal führen `source` bzw. `source_path`/`source_sha256`/
  `normalized_sha256`/`source_line_endings` (`crlf`, `lf`, `mixed`).
- **Nachweis:** Erfolg erst nach `message export` genau einer Envelope zur Message-ID und einem
  semantischen Vergleich mit der vorbereiteten Mail: Header-Folge (Name und whitespace-normalisierter
  Wert; zusätzliche, fehlende, geänderte oder umgestellte Header ergeben `header:<name>`/
  `header_order`), dekodierter Text **einschließlich seiner abschließenden Zeilenumbrüche**
  (`Text\r\n\r\n\r\n` und `Text\r\n` sind verschieden, `body:text`; neutral ist nur die Form des
  Zeilenendes LF/CRLF/einzelnes CR, auch nach dem Dekodieren), die **Teil-Header-Werte** jedes MIME-Teils
  (`Content-ID`, `Content-Description`, `Content-Transfer-Encoding`, `Content-Type` samt `charset`,
  `Content-Disposition`, `Content-Language`, `Content-Location`, jeder weitere Teil-Header: Name und
  ASCII-whitespace-normalisierter Wert, geänderte, fehlende, zusätzliche oder umgestellte Header ergeben
  `part[n]:header:<name>` bzw. `part[n]:header_order`, `n` ab 1 mit dem Textteil als erstem Teil),
  Anhangsanzahl und je Anhang Name, Typ, Größe und SHA-256. Unicode-Whitespace bleibt in allen
  Vergleichswerten signifikant (nur ASCII-Leerzeichen/Tab werden zusammengefasst).
  Abweichung, Duplikat oder Nichtauffindbarkeit: `draft_verification_failed` (`appended_unverified`
  im Journal), kein Erfolg. Das ist keine Byte-Gleichheit beliebiger Rohmails.
- **Fehlercodes (neu):** `raw_eml_path_invalid`, `raw_eml_path_forbidden`, `raw_eml_extension_invalid`,
  `raw_eml_unreadable`, `raw_eml_link_refused`, `raw_eml_not_regular_file`, `raw_eml_changed`,
  `raw_eml_empty`, `raw_eml_too_large`, `raw_eml_message_too_large`, `raw_eml_invalid`, `raw_eml_not_utf8`,
  `raw_eml_nul_byte`, `raw_eml_bare_cr`, `raw_eml_line_too_long`, `raw_eml_header_invalid`,
  `raw_eml_header_missing`, `raw_eml_header_duplicate`, `raw_eml_message_id_invalid`,
  `raw_eml_mime_invalid`, `raw_eml_structure_unsupported`, `raw_eml_attachment_invalid`,
  `raw_eml_attachment_count_exceeded`, `raw_eml_attachment_too_large`, `raw_eml_part_header_invalid`,
  `raw_eml_address_invalid`, `raw_eml_date_invalid`, `raw_eml_part_count_exceeded`; dazu die bestehenden
  `draft_binding_failed`, `draft_lock_missing`, `draft_message_id_not_unique`,
  `draft_journal_write_failed`, `draft_save_failed`, `draft_append_outcome_unknown`,
  `draft_verification_failed`. Ein Validierungsfehler führt zu **keinem** Mailbox-Kontakt und keinem
  Journal-Eintrag; Fehlertexte enthalten nie Dateiinhalt, Aufruferpfade oder Backend-Stderr.
- **Grenzen des Nachweises:** Offline-Modell des Backends (synthetische Mails, kein echtes
  Postfach, kein SMTP, keine Cloud), kein Live-Nachweis. Hardlinks innerhalb einer Zone sind
  reguläre Dateien; unter Windows gibt es kein `O_NOFOLLOW` (Rennen werden erkannt, nicht
  verhindert). Der CLI-Export landet vor dem begrenzten Python-Laden vollständig auf der Platte.
  Bytegleiche Übertragung beliebiger Rohmails (8bit-Fremdcharsets, HTML-Alternativen, eingebettete
  Mails) ist ausdrücklich nicht Teil des Profils.

## Gebundene Draft-Diagnose (`diagnose-draft`, opt-in, Default aus)

Zusätzlich zu den drei Entwurfsoperationen existiert ein **rein diagnostischer**
Pfad, der genau **einen** `message save`-APPEND beobachtet, ohne Rohprotokoll,
Zugangsdaten oder Mailinhalte offenzulegen. Er ist **ausdrücklich opt-in und
standardmäßig aus**: `--opt-in` in der CLI *und* `opt_in: true` im Request sind
beide nötig; sonst stoppt der Aufruf vor jedem lokalen Write und jedem Prozess.

```bash
python3 scripts/mail_desk_himalaya_client.py diagnose-draft --request req.json --review rev.json --opt-in --json
```

- **Geschlossener, versionierter Request:** Das Request-Manifest bindet exakt
  `workspace_id`, `account`, `folder`, `operation: "save_draft"` und den
  `draft`-Inhalt. Die Schlüsselmenge ist geschlossen: unbekannte Schlüssel (etwa
  `reviewed: true` im selben Manifest) werden fail-closed abgewiesen, und genau
  **eine** Operation `save_draft` ist erlaubt — kein `replace`/`delete`, keine
  Account-Overrides, keine Mehrfachoperationen. `account` ist **explizit und
  nicht-null** und muss exakt zur Workspace-Backend-Konfiguration passen;
  `workspace_id` muss dem aufgelösten Workspace-Wurzelnamen entsprechen
  (`<root>/data/mail-desk`-Layout; Link-/Junction-Vorfahren werden abgewiesen,
  gleichnamiger Klon bleibt eine dokumentierte Restgrenze). `folder` ist der
  reviewte Zielordner (die explizite Vorgabe ersetzt jede Ordner-Erkennung).
- **Separater, hashgebundener Review-Input:** Die Freigabe steht **nicht** im
  Request, sondern in einer eigenen Operator-Datei (`--review`,
  `DiagnosticReviewReceipt`, exakt `schema_version`, `kind`, `receipt_id`,
  `request_hash`, `approved_at`, `approved_by`; `receipt_id` als festes Token,
  `approved_at` als realer Zeitpunkt). Sie ist an den exakten kanonischen
  Request-Hash gebunden (inkl. Workspace/Account/Folder/Operation); Drift wird
  abgewiesen. Wie die übrigen Receipts ist dies eine Struktur-/Hash-Bindung und
  **keine** kryptografische Authentisierung.
- **Read-only-Preflight vor dem Marker:** Request, Receipt, Timeout, Workspace-/
  Account-Bindung, Draft-Bau (Header-Injektions-Abweisung), strikte
  UTF-8-Validierung des Payloads und die Config-/Executable-Auflösung laufen
  **vollständig vor** dem Einmal-Marker; ein fehlgeschlagener Preflight verbraucht
  keinen Versuch.
- **Einmalig, ein Prozess über `run_himalaya`:** Der begrenzte Lauf nutzt den
  bestehenden `run_himalaya`-Seam mit dem keyword-only `diagnostic_capture`
  (Default `None` → unverändert). Danach wird der Einmal-Marker atomar unter
  `data/mail-desk/diagnostics/attempts/<request_hash>.json` konsumiert; ein Replay
  wird abgewiesen. Es läuft genau ein `message save -o plain -f <reviewter
  Ordner>` — keine Ordner-Erkennung, keine Verifikation, kein Retry, kein
  Fallback. Crash, Timeout, Cap-Überlauf, Reader-Fehler oder unbekannter Exit
  hinterlassen einen **unbekannten** APPEND-Ausgang. Der Workspace-Lock wird über
  den shared Guard mit `allow_legacy=False` geprüft; die Lease/Conversation kommt
  aus den bestehenden `WORKSPACE_LOCK_LEASE_ID`/`WORKSPACE_LOCK_CONVERSATION_ID`.
- **Fester Kindfilter:** nur `off,imap_client::stream=trace,imap_codec::response
  =warn`; `tasks`, `resolver` und `wire` bleiben aus. Die Umgebung des
  Kindprozesses erhält feste Overrides (`RUST_LOG`, `RUST_BACKTRACE=0`,
  `NO_COLOR=1`, …); die globale Umgebung wird nie verändert.
- **Begrenzter Parser:** stderr wird inkrementell gelesen (Cap 64 KiB / 128
  sichere Events); unbekannte Targets, falsche Levels, Spans, ANSI-Escapes,
  Multiline-Fragmente und Oversize-Zeilen werden **verworfen**, nie redigiert.
  Erkannt werden nur die drei festen `imap_client::stream`-Meldungen und die
  Continuation-Warnung **nur** als vollständiges, exakt gepinntes 3-Zeilen-Record
  mit `WARN imap_codec::response:`-Präfix (eingebettetes CR/LF, über
  Chunk-Grenzen); eine bloße Signatur in unbekanntem Inhalt ergibt **keinen**
  Event. Ein sicheres Event trägt nur den festen Kind-Namen und begrenzte
  Zählwerte. Ein Cap-Überlauf tötet/reapt den Prozess.
- **Ausgang & Cleanup:** Nur ein realer Nicht-Null-Exit ist ein definitiver
  Fehler; Timeout, Cap-Überlauf, Reader-Fehler, `cleanup_incomplete` (ein Reader
  lebt nach dem einen begrenzten Cleanup-Budget noch) und `exit None` sind
  **unbekannt**, nie „failed"; ein Resolve-Fehler nach bestandenem Preflight ist
  unbekannter Transport. Geteilte Puffer werden nach dem Cleanup-Budget unter
  Lock eingefroren; `child_reaped` nur bei echtem Reap.
- **Additive Evidence, kein Kind-stdout:** strukturierte Diagnose unter
  `data/mail-desk/diagnostics/evidence/<request_hash>.json` — ohne Mailfelder,
  Zugangsdaten oder Pfade. Der kanonische Erfolgs-Envelope bleibt unverändert; der
  rohe Kind-stdout wird **nicht** durchgereicht. Request/Review werden
  größenbegrenzt (2 MiB/16 KiB) und strikt (Duplicate-Keys/NaN/UTF-8) ohne
  Key-Echo gelesen.

Der reguläre Entwurfs-, Batch- und Anhangspfad bleibt unberührt; dieser Pfad
ergänzt keine Mailbox-Berechtigung und sendet nie.

## Policygebundene Anhang-Auswertung (Inspect → Fetch → Extract → Handoff)

Der `attachment_evaluate`-Orchestrator (`scripts/core/attachment_evaluation.py`) ist der
separat aufrufbare, abgenommene Seam. Er verarbeitet eine
bereits als unklar klassifizierte Einzelmail und führt in einem Lauf aus:

1. **Inspect:** `inspect_attachments` (`scripts/mail_desk_himalaya_client.py`) exportiert
   die Nachricht read-only als RFC-822-Quelle und liefert an Account, Message-ID, Folder,
   Envelope-ID und Part-Locator gebundene Anhangskandidaten. Caller-seitig behauptete
   Kandidaten-, Policy- oder Fetch-Status-Werte sind keine Autorität.
2. **Fetch:** `attachment_fetch` ruft ausschließlich kanonisch revalidierte, policykonforme
   Parts (`fetch_status: "available"`, `policy_status: "allowed"`) in die Quarantäne
   `data/mail-desk/attachments/<run-id>/` ab. Autorisierung ist die intern erzeugte,
   kontextgebundene Maschinen-Autorisierung (`receipt_class: "machine"`,
   `receipt_type: "attachment_auto_evaluation"`); sie gilt nur für den Anhang-Auswertungs-Flow und wird
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

**Zero-Write-Garantie:** Die Anhang-Auswertung führt keine Mailbox-Schreiboperation (`copy`/`move`/`delete`),
keine Promotion, Export, Filing, Disposition, Evidence-, Katalog-, Cloud-, Classifier- oder
Cleanup-/GC-Mutation aus. Verifizierte Quarantäne-Artefakte und Inventar bleiben für den nachgelagerten Draft-Pfad
erhalten.

**Draft-Fortsetzung:** Die standardmäßig aktive
`draft`-Aufrufverdrahtung umgesetzt (`core/attachment_reclassification.py`): nur ein unklares
Item durchläuft `attachment_evaluate`, ein validierter `ready`-Handoff wird genau einmal als
getrenntes `untrusted_external` reklassifiziert, und jedes Draft-Item erhält genau ein
additives finales `attachment_evaluation` (`completed/classification_clear` mit
`used_for_classification: true` und 64-Hex-`classifier_revision` nur bei eindeutigem Erfolg).
Die direkten, gegenseitig exklusiven Optionen `--evaluate-attachments`/`--no-evaluate-attachments`
sind nur für `draft`/`inspect` gültig (`draft` default an, `inspect` default aus). Die
Grenze ist fail-closed gehärtet: jede bounded Fehler-/No-Op-Ursache
bleibt item-lokal in Review/`INBOX` (`lock_unavailable`, `policy_blocked`, `quota_exceeded`,
`fetch_failed`, `extraction_failed`, `handoff_invalid`), Identitäts-/Quellen-Pairing-Fehler sind
Bindungsfehler (`handoff_invalid`), ein `ready`-Handoff wird vor der Klassifikation kanonisch mit
`validate_attachment_handoff` revalidiert, fortbestehende Mehrdeutigkeit erhält
`completed`/`still_ambiguous` mit `auto_evaluated` und sicheren `files[]`, `classifier_revision`
bindet den normalisierten AST des Classifier-Regelmoduls plus Kataloge und konsumierte Hashes,
unerwartete Backend-/Programmiervertragsfehler schlagen über
`AttachmentReclassificationContractError` fail-loud fehl, und ein deterministischer, PII-freier
Run-ID je Nachricht erreicht im zweiten Default-Lauf `already_fetched` ohne Doppel-Fetch.
Der opt-in `inspect`-Vorschlag ist verdrahtet: `inspect` bleibt
standardmäßig rein lesend; nur `--evaluate-attachments` (`evaluate_attachments: true`) ergänzt
einen top-level, **nicht ausführbaren** `manifest_proposal` über denselben
`draft_manifest`- + `install_draft_attachment_evaluations`-Flow wie `draft`, und eine
ausführbare Batch-Manifest-Datei entsteht weiterhin nur bei explizit konfiguriertem
`manifest_file`. Ein gemischter Batch (klar/geklärt/weiterhin mehrdeutig/bounded failed)
bleibt geordnet und item-lokal. Die Paketabnahme ist abgeschlossen:
`tests/test_batch_runner_mde2_acceptance.py` beweist in einem einzigen hermetischen realen
Pfad Body/Full-Read → mehrdeutig → realer `text/plain`-Anhang → genau eine
`untrusted_external`-Neuklassifikation → persistiertes Projekt-`DraftManifest` mit
bounded `attachment_evaluation` (`used_for_classification: true`, 64-Hex-`classifier_revision`
gebunden an Classifier-Regeln plus konsumierten Anhangs-Hash) bei null Mailbox-/Netzwerk-
und null Execute-/Promote-/Export-/Filing-/Dispositions-/Katalog-/Cloud-Seiteneffekten.
Die Anhang-Auswertung endet weiterhin am validierten Handoff.

**Lease-Delegation:** Die optionalen Flags `--workspace-lease-id <ID>` und
`--workspace-conversation-id <ID>` sind nur mit direktem `--draft`/`--inspect` gültig und
reichen eine von der Agent-Session gehaltene Consumer-Workspace-Lease an die Anhang-Bewertung
weiter, damit diese unter Agent-Lock nicht fail-closed mit `lock_unavailable` endet. Sie
setzen die Config-Keys `lease_id`/`conversation_id` ausschließlich, wenn gesetzt; ohne Flag
bleibt die Config-Form byte-identisch und das Verhalten wie bisher. Fremde oder abgelaufene
Leases bleiben fail-closed `lock_unavailable`, und ohne Flag erfolgt weder Lease-Beschaffung
noch Fetch.

> **Nicht verwechseln:** Das vorbestehende, unabhängige Offline-Flag
> `mail_desk_inspect_manifest.py --reclassify` (siehe oben) reklassifiziert erstellte
> Batch-Manifeste offline gegen `projects.json`/`topics.json`. Es ist **nicht** die
> Draft-Neuklassifikation und hat mit der Anhang-Auswertung nichts zu tun.

## Lokale Injection-Barrier (Issue #62)

Vor der Body-Auswertung und vor jeder agentensichtbaren Ausgabe läuft in
`inspect`, `draft` und `pipeline` ein deterministischer, rein lokaler Pre-Filter
([`scripts/core/injection_barrier.py`](../scripts/core/injection_barrier.py)).
Er nutzt ausschließlich Python-Stdlib, hat keinen Netzwerk-/Cloud-/Modellbezug
und ersetzt keine Schreib-, Review- oder Lock-Gates.

**Fencing:** `preview`/Body werden in feste Delimiter
`<untrusted_mail_content fence_version="1" sha256="…">…</untrusted_mail_content>`
gefasst; Mail- und Attachment-Delimiter-Spoofs werden zu
`[ESCAPED_UNTRUSTED_TAG]`. Subject/From/To/Cc bleiben typisierte Datenfelder,
werden aber gescannt. `inspect`-Ausgaben und `batch-inspected.json` enthalten
deshalb gefencete Previews; der Scanner erkennt seinen eigenen Fence und
wertet ihn nicht als Spoof.

**Scan und Quarantäne:** Feste Default-Regeln erkennen instruction-ähnliche
Phrasen (EN/DE), Fake-Rollenmarker, Fence-Spoofs, aktive/verdächtige URLs,
gefährliche Anhangsnamen/-MIME, Steuer-/Bidi-/Zero-Width-Zeichen,
Mixed-Script-Homoglyphen, aktives/hidden HTML und lange Encoded-Blobs. Die
Schwelle ist fix `medium`. Geflaggte Mails werden unter
`data/mail-desk/injection-quarantine/<id>/` als `record.json` (Schema 1,
write-once), `fenced.txt` (write-once) und `observations.jsonl` (append-only)
abgelegt; das globale `index.jsonl` ist ebenfalls append-only. Die ID ist
deterministisch aus dem normalisierten `message_key`; Wiederholung ergänzt nur
Beobachtungen. Keine Roh-MIME, keine Zugangsdaten, keine absoluten Pfade, kein
`preview`-Feld im Record.

**Fail-loud:** Geflaggte Mails werden aus `draft`-/`pipeline`-Manifesten und
`inspect`-Proposals ausgeschlossen. Das Manifest trägt einen hash-gebundenen
top-level `injection_barrier`-Block. `draft`/`pipeline` liefern `ok: false`,
`status: "quarantined"`; die CLI-Hülle meldet `state: "Quarantined"`,
`error.type: "InjectionQuarantine"` und Exit 1. `execute`,
`mail_desk_sign_manifest.py`, der Batch-CLI-Wrapper und der innere
`dossier_apply`-Execute stoppen vor jeder Mutation mit
`injection_quarantine_present`, auch bei `allow_fewer`. Ein geflaggter Lauf ist
kein `inbox_zero`; zuerst Quarantäne auflösen, dann neu draften.

**Read-only-Grenze:** `inspect` bleibt mailbox-read-only und schreibt nur die
Quarantäne-Evidence. Es kopiert, verschiebt oder löscht keine Mail.

**Workspace-Policy:** Optional
`memory/references/mail-desk/injection-barrier.json` (Schema 1) mit
ausschließlich additiven Feldern `extra_instruction_phrases`,
`extra_url_shortener_domains`, `extra_dangerous_extensions`,
`extra_macro_extensions`. Fehlt die Datei, gelten die Bundle-Defaults; jede
Schema-/Typ-/Größenabweichung stoppt fail-loud. Es gibt keinen Off-Schalter und
keine Absenkung der Schwelle.

**Fingerprint:** `classifier.py` und die Matching-Module bleiben unangetastet;
`classifier_revision` rotiert nicht.



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
- **Versionierbare Metadaten:** Sämtliche Mail-Desk-Metadaten außerhalb des `attachments/`-Unterbaums — insbesondere `action-log.jsonl`, `replies-needed.jsonl`, `pending-review.jsonl`, `final-location-index.json`, Batch-Manifeste, `batch-recovery-journal.json`, `runner-progress.json` sowie der geplante versionierte `data/mail-desk/attachment-quarantine-index.json` — bleiben versionierbar und trackbar.
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

Vertragshistorie: System Map L2 (docs/system-map) und docs/features/_archive.md; Ticket-IDs sind dort kanonisch.
