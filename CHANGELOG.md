# Changelog

Alle nennenswerten Änderungen am `office-intelligence`-Bundle. Format orientiert
an [Keep a Changelog](https://keepachangelog.com/de/1.1.0/); jedes Release fasst
die abgeschlossenen Feature Requests (FR) zusammen. Details: Feature-Records im
Archiv [`docs/features/_archive.md`](docs/features/_archive.md) und System Map
([L1](docs/system-map/README.md), [L2](skills/mail-desk/docs/system-map/README.md)).

## [Unreleased]

### 2026-11-09 — Issue #88: Sent-Items-Handoff für die Synthese (Teillieferung)

#### Added

- **mail-desk `sync-sent`:** Envelope-Felder `new_count`/`new_items[]` (inkl. `Cc`,
  `In-Reply-To`, `References`, Envelope-ID, Thread-Schlüssel) und `handoff`; begrenzter (200),
  atomar geschriebener, idempotenter Handoff `data/mail-desk/new-sent-items.json`, der **vor**
  dem Sent-Index fortgeschrieben wird. Konsum-/Dedupe-Ledger
  `sent-synthesis-ledger.jsonl` mit Thread-Ankern (`core/sent_handoff.py`).
  `get_single_email_details` liefert zusätzlich `cc` und `headers_complete`.
- **Fail-closed:** `handoff_pending` (unkonsumierter Handoff + weitere neue Items, nichts
  indexiert), `handoff_invalid`, `sent_window_truncated`, `sync_window_failed`; Exit ≠ 0. >200 neue Items
  werden in 200er-Batches committet (Rest nach Konsum).
- Nicht enthalten: Synthese/LLM-Logik und die Consumer-Pipeline (extern). Issue #88 bleibt
  offen. Vertrag: [L2 §43](skills/mail-desk/docs/system-map/README.md).

#### Fixed (Review-/Audit-Findings zum Erstcommit)

- `sync_sent_items_by_dates` committet einmal statt je Tag (Selbstblockade durch `handoff_pending`).
- Überhang > 200: in der dritten Runde durch deterministische 200er-Batches ersetzt (siehe unten); kein
  Prune-Write (konsumierte Reste werden abgeleitet ignoriert).
- `handoff.state` ist überall `none|written|pending|invalid`; `new_count`/`new_items`/`handoff` auch im
  Legacy-Tupel und in allen Fehler-Envelopes (`sync_failed` statt Erfolg/ungeformtem Fehler).
- Handoff voll schema- und identitätskonsistent validiert, Ledger strikt gegen das Schema (`ledger_invalid`),
  abgerissene (auch UTF-8-)Zeilen blockieren nichts und gehen nicht verloren (Sidecar `.torn`).
- Konsumvertrag: `load_pending_handoff` (frisch verankert, Vorfahren zuerst) und `record_synthesized` mit
  Neuauflösung des Ankers; Verzeichnis-`fsync` best-effort, Crash-Vertrag = Prozess-Absturz.

#### Fixed (zweite Review-/Audit-Runde)

- Überhang > 200 wird in 200er-Batches committet (älteste zuerst, Rest nach Ledger-Konsum, kein ID-Skip).
  Recovery eines Tages nur über `sync_sent_items_by_dates_report(..., include_next_day=False)` (nicht
  `sync_sent_items(dates=[D])`, das D+1 mitliest) und nicht über `--sync-sent N`/`count`.
- Fehler-/Legacy-Envelope vollständig: `reason_code`, `handoff` (inkl. `written_count`/`pending_count`/
  `deferred_count`/`file`) und `write_state` auch bei Bindungs- und Legacy-Fällen; `sync_invalid_count` für
  ungültige Manifest-`count`-Werte.
- Handoff-Grenzen werden auch beim Lesen durchgesetzt (kanonisch 200 Items, 50 References, 100 To/Cc,
  1000 Betreffzeichen); ein dateieigenes höheres `max_items` erweitert nichts.
- Ledger-Dubletten: die erste gültige Zeile je `message_id` gilt für alle Indizes und führt keinen
  widersprüchlichen Thread-Anker ein.
- Fehlervertrag trennt `write_state` `none`/`unknown` (`partial` reserviert); ein fehlgeschlagener
  Handoff-Write nach möglicher Publikation oder Append meldet `new_count: null` statt fälschlich null Writes,
  der Wiederanlauf bleibt idempotent.
- Datumsfenster arbeiten transaktional auf ID-Kopien; ein Lesefehler verwirft Kandidaten und ID-Marken, sodass
  eine später erneut gelistete Mail nicht still übersprungen wird. Ein scheiterndes Fenster bricht den ganzen
  Lauf vor jedem Write ab (`sync_window_failed`), damit das Wasserzeichen keine ältere Lücke überspringt.
- Envelope-Seiten werden bis zur kurzen/leeren Seite paginiert (Dedupe per ID); erst nach `max_pages` volle
  Seiten ergeben `sent_window_truncated` (kein Watermark-Advance, höheres-`max_pages`-Recovery). Das
  Wasserzeichen-Alter blockiert nie: Coverage ab Watermark-Tag (inklusive), leere Tage zählen; Erstlauf mit
  eingefrorenem Laufdatum und 60-Tage-Backfill bzw. `since`. `count` ist ein Item-Budget; bei Erreichen
  `truncated`/`coverage_*` plus Recovery-Aufruf.
- Handoff-/Ledger-`schema_version` und `count` müssen exakt `int` sein (nicht `true`/`1.0`); tief
  verschachteltes JSON (`RecursionError`) ergibt `handoff_invalid`/`ledger_invalid` statt eines Absturzes.
- Untrusted Ledger-/Handoff-Typen (`role`, `truncated_fields`, `match_type`) werden vor Mengenoperationen
  validiert und fail-closed (`ledger_invalid`/`handoff_invalid`) statt `TypeError`.
- Thread-Index verfeinert den Vorgang innerhalb eines Threads über verschiedene Message-IDs, ohne die
  Dubletten-Regel (erste Zeile je Message-ID) zu verletzen.
- L1/L2-Maps, Vertrag, Log-Schema, CLI-Referenz und SKILL synchron aktualisiert.

#### Fixed (dritte Review-/Audit-Runde)

- Persistente, atomare Coverage-Schranke `sent-sync-state.json` (`initial_start`, `covered_through`): jede
  Rückspulung (explizite Daten, älterer `since`, Legacy-Marker) wird **vor** Handoff/Index geschrieben; nach dem
  Append rückt sie nur über einen zusammenhängend angeforderten Prefix, nie über heute/Punktabfragen vor.
  Der heutige Tag wird nie vollständig markiert; Multi-Day-Deferrals und später konsumierte Items können den
  Watermark nicht mehr über unvollständige Tage springen.
- Source-Fenster (`window_date`) statt Mail-Header-Datum bestimmt Sortierung, Barrier und Recovery; fehlende,
  widersprüchliche oder unparsebare Header verschieben kein Item.
- Singular-/plural-int-Datums-API melden `handoff_deferred` und laufen über den Report/State-Pfad; Account-
  Bindung wird vor jedem State-Write geprüft, leere Punktfenster nennen `next_start`/Gap-Recovery. 200er-Batches
  (201/250, Crash/Retry) erzeugen alle IDs eventual. Explizite Datumsaufrufe erzeugen ohne Schranke keine
  jüngere `covered_through` und überspringen keinen Altbestand.
- Legacy `sent-sync-initial-start.json` wird nur gelesen und non-destruktiv migriert; ein älterer `initial_start`
  bleibt erhalten und setzt die Schranke davor zurück.
- `count`-Item-Budget blockiert schon den ersten nichtleeren Tag über Budget (kein Überschreiten); nur
  tatsächlich gelesene/akzeptierte Tage rücken die Schranke vor, der blockierte Tag bleibt `next_start` mit
  erreichbarer Recovery.
- `since`/Initialmarker springen nie über offene Coverage; future `since` wird vor jeder Mutation abgewiesen,
  der Erstlauf mit gültigem `since` persistiert den Marker. State-Schema ist exakt (drei Felder, nicht-null
  `initial_start`); malformed/null/future State und ungültige Kalenderdaten werden vor Read/Write abgewiesen,
  nur autorisierte Reparatur/Quarantäne, kein `since`-Bypass eines malformed State.
- Pagination beendet nur bei klassifiziertem `HimalayaInvocationError`/`himalaya_command_failed` mit exaktem
  „envelope page out of bound“ ab Seite 2 (offline nicht verifizierte **Annahme**); Transport-„buffer index out
  of bounds“ bleibt fail-closed.
- `write_state` unterscheidet `none`/`state`/`unknown` inklusive State-Publish-Fehler und `sync_no_progress`;
  Doku/L1/L2/objects bereinigt.

#### Fixed (vierte Review-/Audit-Runde)

- State-`initial_start`/`covered_through` werden gegen das eingefrorene Laufdatum geprüft; future Werte,
  malformed/null-both State und future `since`/`since` mit Zeitanteil fail-closed vor Read/Write. Altbestand
  `initial_start:null` mit `covered_through` ist nicht sicher migrierbar (auch der Wasserzeichen beweist keine
  lückenlose Untergrenze) und fail-closed; fehlt bei nichtleerem Index jede State-Datei, ebenfalls
  `sent_initial_state_required` (keine Wasserzeichen-Autoinitialisierung). Eine bloße Quarantäne reicht nicht:
  vor Resync muss ein autorisierter Operator einen verifizierten konservativen State/`since` anlegen. Jeder
  originale State/Marker wird vor Merge auf Future geprüft, `covered_through` gleich heute vor dem Read auf
  gestern zurückgesetzt. Nur autorisierte Reparatur/Quarantäne mit validem Schema-JSON, keine autonome Löschung.
- Validierungsfehler setzen keinen synthetischen Handoff-State mehr; der echte Peek-Zustand/pending_count bleibt.
- Count-Break rückt nur tatsächlich akzeptierte Tage vor; ungültige Kalenderdaten und Account-Bindung werden vor
  State-Writes geprüft. Der optionale Draft-Sent-Sync akzeptiert nur echt parsebare ISO-Datum/ISO-Datetime/
  RFC-2822-Header (kein Fallback 2026-01-01 bei „not a date“/unbekanntem Monat) und bricht keinen vollständigen
  Sync ab; der Draft-Pfad schluckt Sync-Ausnahmen wie bisher (Inbound-Semantik unverändert).

#### Fixed (fünfte Review-/Audit-Runde)

- Ein fehlender Coverage-State bei **irgendeinem** bestehenden Sent-Index-Eintrag (auch mit unparsebarem `at`)
  fail-closed `sent_initial_state_required` in allen Einstiegen: Default, `dates`, Report, Singular- und
  Plural-API. Punktdaten, der neueste Watermark und CLI-Defaults initialisieren nie mehr; Handoff/Index bleiben
  unangetastet.
- Datierte APIs reichen ein explizites verifiziertes `since` durch; es ist der exakte Start (kein stilles
  `min(Watermark, since)`), wird vor jedem Read/Write validiert und erlaubt den Resync. Angeforderte Tage vor
  `since` und ein `since` mit Zeitanteil/future sind fail-closed. Crash/Retry wiederholt dieselbe Ablehnung ohne
  Writes; ein pending Handoff bleibt byte-gleich.
- Tests decken alle Einstiege, unparsebares `at`, `since` älter/jünger als der Watermark sowie den
  Repro-Fall (Watermark 2026-01-04, `dates=[2026-01-05]`, Start 2026-01-03) ab; Log-Schema, L1/L2, objects,
  effects, processes, SKILL und Batch-Runner-Vertrag sind synchron.

#### Fixed (sechste Review-/Audit-Runde)

- Recovery bei `sent_initial_state_required` ist exakt dokumentiert und geprüft: Runner ohne `--sync-sent` mit
  `--input`/`--stdin`-Manifest `{"mode":"sync_sent","since":"YYYY-MM-DD","count":N}`, `--keep-input`, Pfade
  unter `data/mail-desk/`. Der direkte `--sync-sent N` setzt den direkten Modus und ignoriert `--input`/`--stdin`;
  der Wrapper `sync-sent --count N` kann kein `since` führen. Ein direkter Runner-Lauf setzt dieselbe gehaltene/
  verifizierte Workspace-Lease voraus wie der Wrapper.
- Ein jüngeres `since` bei bestehendem State wird nie angewendet (kein Coverage-Skip); der Erfolgs-Envelope und
  die CLI-Summary melden additiv `since_requested`, `since_ignored: true`,
  `since_ignored_reason: "coverage_starts_before_requested_since"` und `effective_coverage_start`. Älteres und
  gleiches `since` wenden den Start an (`since_ignored: false`).
- Tests prüfen den Manifest-/stdin-Aufruf offline über gemockte Konfiguration/Dispatch und den Ignore-Hinweis
  (älter/gleich/jünger); `cli-operations.md`, Log-Schema, L1/L2 und SKILL sind synchron.

#### Fixed (siebte Review-/Audit-Runde)

- Der bestehende Wrapper kann jetzt `sync-sent --since YYYY-MM-DD --account <verifiziert>`; er leitet ein
  stdin-Manifest an den Runner und führt die vorhandene maschinelle `verify_workspace_lock`-Prüfung **vor**
  jedem Read/Write aus. Fehlende/fremde Lease stoppt ohne lokale Writes oder Kindprozess. Der direkte
  Runner-Fallback verlangt expliziten Account und dieselbe gehaltene/verifizierte Lease; `--sync-sent`
  bleibt dabei ungesetzt.
- Dokumentation ist portabel (`$MAIL_DESK_SKILL/scripts/...`, kein Workspace-Root-Pfad) und trennt das
  optionale Eingabe-Manifest `batch-sync-sent.json` vom Wrapper-Envelope/Progress unter `tmp/mail-batch/`.
- `sync_sent_items(dates=[...], since=...)` wendet ein älteres `since` nun mit derselben konservativen
  Rewind-Logik wie der Report vor Reads/Commit an; älter/gleich/jünger werden korrekt als
  `since_ignored: false`/`true` samt `effective_coverage_start` gemeldet. Tests decken Barrier-Rewind,
  Retry nach fehlgeschlagenem Fenster, alle drei Formen und die echte Account-Binding-Seam mit
  Offline-Workspace-Fixture ab.

#### Fixed (achte Review-/Audit-Runde)

- Der Wrapper bindet `--data-dir` jetzt kanonisch an den lease-geprüften `--workspace`: relativ oder
  absolut muss der aufgelöste Pfad exakt `<workspace>/data/mail-desk` sein und der Runner-Root-Inferenz
  (`workspace_root_from_data_dir`) entsprechen. Symlinks/Junctions werden aufgelöst; externe Consumer,
  Escapes und fehlende Workspaces stoppen mit `WorkspacePathError` vor Child/Read/Write. Ein fehlendes
  `data/mail-desk` im geprüften Workspace bleibt zulässig; nicht vom Wrapper verwendete Pfade bleiben
  unberührt.
- Tests decken relative/absolute Pfade, externes `--data-dir`, fehlenden Workspace, reale Verzeichnis-Links
  (Symlink oder Windows-Junction, mit Skip wenn nicht verfügbar), fehlende/fremde Lease sowie `--since` und
  Normalpfad ab. CLI-Operations, L1/L2, SKILL und Batch-Runner-Vertrag sind synchron.

#### Fixed (neunte Review-/Audit-Runde)

- Auch das Wrapper-Ausgabeverzeichnis `<workspace>/tmp/mail-batch` wird vor jedem `mkdir`, Prune und Child
  kanonisch an denselben lease-geprüften Workspace gebunden. Zeigen `tmp` oder `mail-batch` per
  Symlink/Junction aus dem Workspace, stoppt der Wrapper mit `WorkspacePathError`, ohne fremde Dateien
  anzulegen, zu überschreiben oder zu löschen; fehlende interne Pfade bleiben zulässig.
- Tests reproduzieren die Lock-Lücke mit realen Links auf beiden Ebenen (`tmp` und `mail-batch`), bestehenden
  externen reconcile-Dateien und `--keep 1`: Dateien bleiben byte-gleich, Guard/Child/Prune werden nicht
  gerufen. Normale Pfade schreiben weiterhin unter `<workspace>/tmp/mail-batch`; CLI-Operations, L1/L2,
  SKILL und Batch-Runner-Vertrag sind aktualisiert.

#### Statische Härtung & akzeptierte Trust-Grenze (zehnte Review-/Audit-Runde)

- Statische Härtung: Jede vorhandene Symlink-/Junction-/Reparse-Point-Komponente in `data`, `data/mail-desk`,
  `tmp` oder `tmp/mail-batch` wird vor mkdir/Prune/Child abgewiesen, auch intern, gebrochen oder
  selbstreferenziell. `durable_append_jsonl` lehnt statische Symlink-/Hardlink-Aliase der Index-/Ledger-Datei
  ab, bevor sie geöffnet wird.
- **Scope-Grenze (akzeptiert, Follow-up Issue #91):** Ein konkurrierender Same-User-Prozess mit
  Schreibrechten im selben Workspace kann Pfade zwischen statischer Prüfung und Zugriff austauschen (TOCTOU)
  oder nach der Prüfung Hardlink-/Symlink-Aliase anlegen. Das liegt ausdrücklich außerhalb des kooperativen
  Lease-Scope; die Lease ist keine OS-Sicherheitsgrenze gegen denselben Benutzer. Vollständige Isolation
  erfordert OS-Mittel (separater Benutzer/Namespace/Container/ACL) oder eine fd-/handle-basierte
  Neuarchitektur und ist als Issue #91 verfolgt. Die Dokumentation nennt diese Grenze ausdrücklich, statt
  Race-Sicherheit zu behaupten.
- Tests: reale interne/externe Junctions, gebrochene und selbstreferenzielle Symlinks, Hardlink-/Symlink-Alias
  der Sent-Index-Datei mit byte-gleicher externer Datei; Race-Injection wird bewusst nicht als „sicher“
  getestet, solange keine belastbare handle-basierte Verankerung existiert.

#### Fixed (elfte Review-/Audit-Runde)

- `<datei>.torn`-Sidecars werden jetzt vor **jeder** Mutation statisch auf Symlink-/Hardlink-Aliase geprüft.
  Ein aliaster Sidecar führt zu `sent_index_alias_invalid`/`ledger_alias_invalid`, ohne Hauptdatei oder externes
  Ziel zu verändern.
- Der statische Alias-Preflight für `sent-index.jsonl` und das Ledger (inkl. Sidecars) läuft jetzt **vor**
  Handoff-Publikation und Index-Append. Direktes `record_synthesized` prüft das Ledger samt Sidecar vor dem
  Lesen/Append. Die Fehler sind keine transienten `sync_failed`/`write_state: unknown`-Fälle: `write_state`
  bleibt `none`/`state`, Handoff/Index bleiben unangetastet; Recovery ist ausschließlich manuelle
  Reparatur/Quarantäne, ein Rerun ohne Korrektur scheitert identisch (keine automatische Löschung).
- Reason-Codes, Sidecar-/Recovery-Semantik und `write_state` sind in Log-Schema, Effects, L1/L2, SKILL,
  CLI-Operations und Batch-Runner-Vertrag synchronisiert; Tests decken Index- und Ledger-Aliase als Hauptdatei
  und Sidecar ab (Hardlink immer, Symlink wo verfügbar).

#### Fixed (zwölfte Review-/Audit-Runde)

- Der Alias-Preflight läuft jetzt in **allen** Einstiegen (Default, `dates`, Report, Singular/Plural und direkter
  Append-API) **vor** Index-/State-Nutzung, Mailbox-Read und jedem Coverage-/Handoff-/Index-Write. Damit kann
  der Report-Pfad bei leerer/dedupten Kandidatenliste keine Coverage mehr fortschreiben, bevor der Alias
  erkannt wird; Index, externes Ziel, State und Handoff bleiben bytegleich, der Collector wird nicht gerufen.
- Alias-Fehler-Details heißen `alias_file`/`alias_kind`/`alias_label`; sie überschreiben nicht mehr
  `handoff.file`. Envelope- und Summary-Tests prüfen Index-/Ledger-/Sidecar-Aliase bei `handoff.state`
  `pending` und `none`, inklusive unverändertem Handoff-Pfad.
- Der Fehlertext behauptet nur noch, dass diese Operation keine Mailbox-, Index-/Ledger-, State-, Handoff- oder
  Append-Ziel-Inhalte gelesen/geschrieben hat (bereits erfolgte Konfig-/Lease-Reads bleiben außen vor).
  Die akzeptierte Same-User-Race-Grenze (#91) wird weiterhin nicht als gelöst dargestellt.

#### Fixed (dreizehnte Review-/Audit-Runde)

- Statische Alias-Fehler lesen den abgewiesenen Pfad nicht mehr für Diagnosen: `failure_envelope` überspringt den
  Handoff-Peek und meldet `handoff.state: unknown` mit `pending_count: null`; auch `peek_handoff_state` prüft das
  Ledger statisch und liest einen Alias nicht. Ein fremdes Hardlink-Ledger kann damit kein falsches `none`/`0`
  mehr vortäuschen.
- `append_sent_index_entries` prüft als direkte API jetzt alle vier Ziele (Index, Ledger und beide
  `.torn`-Sidecars) vor Index-Read/Write; ebenso die private Einzel-Datum-API vor `_known_index_ids`.
  Tests decken Ledger-/Sidecar-Hardlinks für den Direkt-Append und die private API ab.
- `handoff.state` erlaubt additiv `unknown`; Log-Schema, Effects, Objects, L1/L2, SKILL, CLI und Batch-Runner
  sind synchronisiert. Die Same-User-Race-Grenze (#91) bleibt unverändert außerhalb des kooperativen Scope.

#### Fixed (vierzehnte Review-/Audit-Runde)

- `_file_alias_kind` behandelt `PermissionError`/andere `OSError` als `unverifiable` statt sie entweichen zu
  lassen. `peek_handoff_state` wirft dadurch nie und meldet bei unverifizierbarem Ledger `unknown`/
  `pending_count: null`; auch `failure_envelope` bleibt bei invalid-count/sync-Fehlern vollständig geformt.
- Consumer-Reads sind fail-closed: `load_ledger`, `load_pending_handoff` und `load_sent_index` prüfen ihre
  Zieldateien (inklusive `.torn`) statisch vor dem Lesen. Ein fremdes Hardlink-Ledger kann keine
  Konsumentscheidung mehr verfälschen; Tests decken pending Handoff, fremde bereits synthetisierte IDs,
  keine Lese-/Schreib-/Promotion-Aufrufe und bytegleiche externe Dateien ab.
- SOP-Trigger ist maschinenlesbar: `handoff_allows_noop` erlaubt nur `pending_count == 0` bei `state`
  `none`/`written`; `unknown`/`invalid` oder `pending_count: null` blockieren Synthese **und** No-op bis zur
  manuellen Behebung. SKILL, Batch-Runner, L1/L2, Log-Schema und Effects sind synchronisiert.

- Root-Katalog und Progress sind historische Linkstubs; unveränderte Snapshots
  samt Provenienz stehen unter [docs/archive/feature-ledgers](docs/archive/feature-ledgers/README.md).
  Einziger aktiver Backlog: [GitHub Issues](https://github.com/martinderm/office-intelligence/issues).
  Aktuelle Vertrags-/Map-Pflege bleibt erhalten; Workstatus und Run-Abnahme im
  Issue/PR/Run. Frühere Ledger-/Record-Anweisungen in datierten Einträgen unten
  beschreiben ausschließlich den damaligen Prozess, keine heutige Pflicht.

### 2026-09-27

#### Added

- **FR-26/MD-V1 — Verify-Evidenz-Scope:** keep_in_folder/unknown-Items sind von
  der Evidenzpflicht ausgenommen (`in_evidence=null`, Scan geskippt) — Batches
  mit keep-Items schließen regelmäßig über `verify` (Handoff released), statt
  fail-closed zu enden; moved-Items behalten den Evidenz-Check. `reconcile`
  bleibt Recovery-Pfad.
- **FR-26/MD-SE1+MD-SE2 — Sent-Sync-Coverage & fail-closed:** das No-Dates-
  Fenster leitet sich aus dem Index-Watermark ab (max `at` → heute, Cap 60
  Tage mit `follow_up_hint`); `--sync-sent N` wird respektiert (N neueste
  Tage); Per-Datum-Himalaya-Fehler propagieren fail-closed (nie still
  `ok: true`); Telemetrie getrennt (`date_windows_synced` vs.
  `envelopes_examined`); page-full-Tage als `truncated_days` sichtbar.
- Guard-Tests: `test_verify_keep_evidence_scope.py` (3),
  `test_sent_sync_watermark.py` (4), `test_review_reason_read_failure.py` (2).
  **Suite 1073/1073 grün.**

#### Fixed

- **FR-26/MD-A5:** `_full_read_failure` trägt
  `review_reason: "read_escalation_failed"` (maschinenlesbare Triage).

### 2026-09-24

#### Added

- **FR-20/MD-A3 — Anhang-Quota Inline vs. Datei:** getrennte Quoten
  (`max_inline_per_message: 3` für Inline-Teile, `max_attachments_per_message: 5`
  für Datei-Anhänge); neuer transparenter Policy-Reason `skipped_inline_limit`;
  Inline-Signaturbilder verdrängen echte Anhänge nicht mehr
  (Befund Env 9438). Nicht-boolsche `is_inline`-Angaben werden fail-closed
  abgewiesen.
- **FR-19/MD-RC1 — Reconcile-Repair-Härtung:** `apply_local_repairs` korrigiert
  neben fehlenden jetzt auch **stale** Index-/Log-Records nach frischer
  Ziel-Verifikation (append-only `reconciled: true`-Log-Eintrag, idempotent);
  `runner-progress.json` wird im Repair-Pfad deterministisch nachgeführt
  (`completed`/`repaired`) statt auf `failed` stehen zu bleiben
  (Befund Env 9428).
- **FR-23/MD-L1 — Workspace-Lock-Delegation:** Batch-Runner akzeptiert
  `--workspace-lease-id`/`--workspace-conversation-id` (nur mit
  `--draft`/`--inspect`) und reicht die Agent-Session-Lease an die
  Anhang-Bewertung weiter; fremde/abgelaufene Leases bleiben fail-closed
  `lock_unavailable` (Befund Env 9451).
- **Batch-CLI-Werkzeuge adoptiert und gehärtet:** `mail_desk_batch_cli.py`
  (Runner-Wrapper: stdout/stderr nach `tmp/mail-batch/`, kompakter Summary-
  Envelope, `--keep N`-Prune, korrektes `--reconcile`-Threading) und
  `catalog_inspect.py` (read-only Katalog-Feldinspektion, `--fields` auch im
  `--id`-Modus, kanonische Envelope-Fehlerpfade).
- `tests/test_skill_routing_contract.py`, `test_runner_lease_delegation.py`,
  `test_reconcile_stale_repair.py`, `test_attachment_inline_quota.py`,
  `test_batch_cli_wrapper.py`, `test_catalog_inspect.py` — Dokument- und
  Verhaltensverträge für die obigen Änderungen.

#### Changed

- **FR-24/MD-R9 — Pflicht-Skill-Routing:** die mail-desk-`SKILL.md`-Description
  routet Batch-Work nicht mehr weg (die Phrase „führt keine Massenpipeline aus"
  ist entfernt); Batch-/Stapelverarbeitung (draft→execute→verify) läuft fachlich
  durch den Skill. Kanonischer Pflicht-Ladeblock (SKILL.md → Adapter-Referenz →
  cli-operations.md → bei Bedarf batch-runner/folder-rules/log-schema) und
  Consumer-Migrationsbaustein (Phase 0 in der Pipeline-SOP; „Pipeline **und**
  Fachvertrag") sind als Migrationsartefakte dokumentiert
  (Befund Batch 2026-W39/4: drei Werkzeugregel-Verletzungen durch ungeladene
  Fachverträge).
- **FR-System nach ICM restrukturiert:** FR-Records mit YAML-Frontmatter liegen
  unter `docs/features/` (Record library); `FEATURE-REQUESTS.md` ist ein schlanker
  Routing-Katalog; Archiv nach `docs/features/_archive.md` verschoben.
- Doku: `cli-operations.md` (Reconcile-Beispiele, Lease-Delegation, `--keep`,
  `--fields`), `batch-runner.md` (getrennte Quoten, Repair-Nachführung,
  Lease-Delegation, Pflicht-Ladeblock-Kontext), `log-schema.md`
  (runner-progress-Status um `aborted`/`repaired` erweitert).
- System Map: L2-Abschnitte 19–23 (MD-BC, MD-R9, MD-L1, MD-RC1, MD-A3);
  Referenzkorrektur in `processes.md` §5 (Quarantäne-Reconciliation →
  `quarantine_index.py`); Metriken 159 Dateien / 71 Testmodule / 1056 Tests.

#### Archived

- FR-13/15/16/17/18/21/22 (Katalog-Ausdünnung) sowie FR-19/20/23/24 nach
  Abschluss — Records im Langzeit-Archiv, aktiver Backlog: FR-09, FR-10, FR-12.
- **`skills/mail-desk/TODO.md` entfernt, historisiert und disponiert:** das frühere
  gemischte Backlog/Changelog-File (operative boku-user-Aufgaben, technische
  Kandidaten Bulk-Copy/Multi-ID-Move, Multi-Batch-Pipelining, persistenter
  IMAP-Session-Pool, Runner-Modularisierungs-Trigger und die Meilensteine
  Januar–September 2026) wurde in
  [`docs/features/_archive.md`](docs/features/_archive.md) überführt (Sektion
  „Mail-Desk-Historik"); Backlog-Pflege läuft jetzt ausschließlich über das
  FR-System (Record library + Katalog).
- **Disposition der technischen Kandidaten (2026-09-24):** Bulk-Copy
  überholt (GroupWise-POA-Sperrverhalten beweist den sequentiellen Flow als
  stabilen Pfad); persistenter IMAP-Pool entwertet (Himalaya bleibt
  Backend-Architektur); Runner-Modularisierung realisiert (Trigger-Kriterium
  1.147 < 1.500 Zeilen). Konsumerspezifische Betriebsaufgaben entfernt
  (Workspace-Sache von boku-user). Verbleibender Restwert als **FR-25
  (low priority)** formuliert: Multi-Batch-Pipelining/Chunk-Modus für
  historische Großaufarbeitung.
- **Verteilte Notiz-Files aufgeräumt:** `skills/project-catalog-entry/TODO.md`
  (reines FR-01a/b1/b2-Nachweisdokument, kein offenes Backlog) in das Archiv
  überführt (Sektion „Project-Catalog-Entry-Historik") und gelöscht; der
  `project-catalog-entry`-SKILL-Verweis zeigt auf die Archiv-Sektion. Das
  run-scoped Bundle-Root-`TODO.md` (Batch-CLI-Adoptions-Abrechnung) ist mit
  dem Run-Manifest abgerechnet und entfernt. Stale refactor-map-Zelle
  („Backlog & Anstehende Optimierungen") als erledigt markiert.
- **Batch-Runner-Referenz Slimming (DOC-S1):** `references/batch-runner.md`
  von 8.754 auf 7.088 Wörter (−19%, 1.643 → 1.401 Zeilen) — Prosa-Dedup und
  repräsentative JSON-Beispiele; **alle 5 JSON-Schemata byte-identisch**, 21
  Sektionen, Quoten/Limits, Lock-/Lease-Verträge und fail-closed-Regeln
  vollständig erhalten; Size-Gate-Test (`test_batch_runner_slim.py`) pinnt das
  Fenster 5.300–7.100 Wörter dauerhaft.
- **FR-Records mit `sub_skills:`-Pflichtfeld:** alle aktiven FR-Records führen
  jetzt das Frontmatter-Feld `sub_skills:` (betroffene Sub-Skills je Paket);
  die Record-Regel ist in `docs/features/CONTEXT.md` verankert.

### 2026-09-23

#### Added

- **FR-22/MD-ID1–ID4 — Identity-freier Desk-Signals-Fallback:** neutraler
  leerer Fallback ohne `mail-desk.json` (keine „martin"-Trigger im Bundle),
  owner-generierte Anrede-Trigger (Schema 2 mit Schema-1-Legacy),
  sent_indexer/internal-domain/Spam-Gegenindikatoren katalogisiert bzw.
  entfernt, `spam_sender_allowlist`-Gate.
- **FR-21/MD-S4+MD-S5 — Desk-Signals-Doku + Katalog-Validator:**
  Dokumentationsverträge (SKILL.md/batch-runner.md/topic-catalog-entry) und
  ausführbarer Workspace-Katalog-Validator `catalog_validator.py` (Exit 0/1/2).
- **FR-18/MD-S1–S3 — Workspace-Agnostizismus:** Desk-Signals-Katalog
  `mail-desk.json` (Schema 1, fail-loud), sent_indexer-Account-Bindung,
  Zoom-Routing im Topic-Katalog.
- **FR-17/MD-R8 — Reply-Heuristik Closing-Mails:** quote-aware
  Downgrade-Verhalten (B-1–B-11), Live-Verifikation an Env 9412.
- Edge-Pins für `ensure_sentence_end` (satz-sichere Evidence-Betreffs).

### 2026-09-22

#### Added

- **FR-17 — Routing-Katalogtreue und Batch-Determinismus:** routing_priority,
  DNR-/Newsletter-Mapping, MIME-Inventarkette, Inline-Bild-Policy,
  MD-R8-Reply-Heuristik.
- **FR-16 — Dokumentations-/Metrik-Hygiene:** Metrik-SSOT (L2-Kopfzeile kanonisch,
  §3.1-Checkliste), Zellen-Splitting, Daedalus-Referenzkorrektur.

### Frühere Meilensteine (Kurzfassung)

- FR-11 — Attachment-Quarantäne mit Retain-/Promote-/Discard-Lebenszyklus.
- FR-08 — Manifestgebundener Anhangsfluss (MD-A1–MD-A5).
- FR-07 — Kontrollierter Batch-Einstieg, Readiness, Recovery, Completion-Gate.
- FR-05/FR-06 — Batch-Runner-Modularisierung und Post-Batch-Synthese.
- FR-01–FR-04 — Projektkatalog Schema v3, Artefakt-Matching, Subtopics/
  Cloud-Sync/Operations/Events, Dossier-/Apply-/Synthese-/Handoff-Pfad.
