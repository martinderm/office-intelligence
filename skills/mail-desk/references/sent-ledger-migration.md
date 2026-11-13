# Sent-Ledger: Outcome, Legacy-Import und Crash-Idempotenz (Issue #107)

Gilt für `data/mail-desk/sent-synthesis-ledger.jsonl` im Consumer. Das Ledger bleibt die einzige
Konsum-/Dedupe-Wahrheit; es gibt keine zweite Index- oder Reportdatei. Kein Mailbox-, Netzwerk- oder
Dokumentzugriff, keine LLM-Logik, keine realen Daten in den Tests.

## 1. Outcome und Provenienz

| Feld | Werte | Bedeutung |
|---|---|---|
| `outcome` | `synthesized` \| `no_evidence` | Ergebnis der Konsumentscheidung. Fehlt das Feld, gilt die Zeile als `synthesized` (Schema-1-Altbestand). |
| `source` | `synthesis` \| `legacy_import` | Provenienz, **kein drittes Outcome**. Fehlt das Feld, gilt `synthesis`. |
| `reason_code` | `null` oder `^[a-z][a-z0-9_]{1,63}$` | Pflicht bei `no_evidence`, verboten bei `synthesized`. |
| `evid_id` | `null` oder begrenzter exakter Token | Optional bei `synthesized`; bei `no_evidence` verboten. |
| `vorgang_ref` | `null` oder begrenzter exakter Token | Bei `no_evidence` verboten; bei importierter Synthese Pflicht. |
| `anchored_to` | `null` oder Message-ID | Bei `no_evidence` verboten. |

- **Konsum:** Jede gültige Ledger-Zeile konsumiert die normalisierte `message_id`, unabhängig vom Outcome.
  `synthesized` und `no_evidence` lösen das Item daher aus dem Handoff und verhindern eine erneute Promotion.
- **`no_evidence` erfindet keine Evidenz:** kein Vorgang, kein EVID-Anker, kein Thread-Anker, `role: primary`.
  Die Zeile dokumentiert nur die begründete No-op-Entscheidung. Ein späterer Reply im selben Thread wird
  nicht an diese Zeile verankert; ein vorhandener echter Synthese-Anker im selben Thread bleibt maßgeblich.
- **Kanonische Begründungscodes:** `no_matching_vorgang`, `no_relevant_evidence`, `thread_unresolved`,
  `source_unavailable`, `headers_incomplete`, `conflicting_evidence`, `legacy_status_no_evidence`.
  Der letzte Code ist nur für die ehrliche Herkunft „Legacy-Index sagte `no_evidence`, ohne
  maschinenlesbaren Grund“ vorgesehen und ersetzt keine fachliche Prüfung.
- **Konflikte:** Die Leseindizes bleiben first-line-wins (Altverhalten). Neue Appends, der Import und die
  Recovery prüfen dagegen **alle** Zeilen einer Message-ID (`all_by_message_id`). Abweichendes
  Outcome/Vorgang/EVID/Grund ⇒ `ledger_conflict`, nichts wird angehängt. Identische Dubletten sind ein
  idempotenter Skip. So verschwindet kein Konflikt in der Lesekompatibilität.
- **Idempotenz alter Aufrufe:** Ein wiederholter `record_synthesized`-Aufruf darf die optionalen
  `vorgang_ref`/`evid_id` weglassen. Ein **weggelassenes** optionales Feld wirkt als Wildcard gegen die
  bereits dauerhafte Zeile; ein **explizit** übergebener, abweichender Wert (auch gegen einen fehlenden
  Altbestandswert) bleibt `ledger_conflict`.

## 2. Legacy-Import (einmalig, idempotent, gestuft)

### 2.1 Inventar

Der Operator erzeugt offline ein JSONL-Inventar; das Bundle liest es nur. Eine Zeile:

```json
{"schema_version": 1, "message_id": "<sent@id>", "status": "synthesized",
 "vorgang_ref": "V-7", "evid_id": "EVID-2026-0042", "reason_code": null,
 "recorded_at": "2026-10-01T10:00:00Z", "thread_key": "root@id"}
```

- `status` ist die Legacy-Vokabel; sie wird als `outcome` ins Ledger übernommen.
- `synthesized` verlangt `vorgang_ref` **und** `evid_id`. Fehlt eines, ist das ein Konflikt
  (`missing_vorgang_ref`/`missing_evid_id`), kein erfundener Anker.
- `no_evidence` verlangt `reason_code` und verbietet `vorgang_ref`/`evid_id`.
- `message_id` darf roh (`<...>`) geliefert werden und wird normalisiert; `vorgang_ref`/`evid_id` werden
  **exakt** verglichen (kein Case-Folding, kein Trimmen).
- `recorded_at` ist Pflicht; `thread_key` ist optional und wird normalisiert. Es gibt kein `note`- oder
  Inhaltsfeld.
- Unbekannte Felder, ungültiges JSON, fehlende `message_id`, ungültiger `status` oder ungültiges
  `recorded_at` ⇒ `legacy_inventory_invalid` (nichts geplant/geschrieben). Semantische Lücken
  (fehlende Triade, `no_evidence` mit EVID) erscheinen dagegen im Dry-run-Konfliktbericht.
- Zwei Inventarzeilen mit derselben Message-ID und abweichender Identität ⇒
  `duplicate_inventory_conflict`; identische Dubletten werden dedupliziert.

### 2.2 Dry-run und Freigabe

`core.sent_ledger.build_legacy_import_plan(data_dir, inventory_path)` ist rein lesend. Er liefert:

- `entries[]` mit `action` `import`/`already_present`, Outcome, Vorgang, EVID, Grund, `recorded_at`,
  optionalem `thread_key` und `pending_handoff` (die Zeile konsumiert ein aktuell offenes Handoff-Item).
- `conflicts[]` mit `message_id` und `code` (u. a. `ledger_conflict`, `existing_ledger_conflict`,
  `duplicate_inventory_conflict`, `missing_evid_id`, `missing_vorgang_ref`, `missing_reason_code`,
  `no_evidence_with_evid_id`, `evid_shared_conflict`). Ein `evid_id` darf nur zu genau einer Message-ID
  gehören; dieselbe EVID-ID für zwei Inventar- oder Ledger-Message-IDs blockiert den Plan.
- `summary` mit `import`/`already_present`/`conflict`/`inventory_duplicates`/`pending_handoff`.
- `target_workspace_id` = Target-Fingerprint des verifizierten Workspace (Hash, kein Pfad).
- `plan_id` = `sha256:` + kanonischer Hash aus **Target-Fingerprint**, normalisiertem Inventar,
  **aktuellen Ledger-Bytes**, **aktuellen Handoff-Bytes** und den geplanten Einträgen.

`blocked: true` bedeutet: kein Approval-Token, kein Write. Der Report wird nur zurückgegeben, **nicht**
als Datei unter `data/mail-desk/` abgelegt und enthält keine absoluten Pfade (Inventar nur mit Name/Digest).
Derselbe Dry-run auf identischen Daten in einem anderen Workspace erzeugt einen anderen `plan_id`; ein
Token von Workspace A ist für Workspace B stale.

### 2.3 Apply

`core.sent_ledger.apply_legacy_import(data_dir, inventory_path, approval=<plan_id>, workspace_root=...,
lease_id=..., conversation_id=...)`:

1. verifiziert die invocation-eigene `workspace-lock`-Lease **vor** dem ersten Write (fehlend/fremd ⇒
   `workspace_lease_required`, nichts geschrieben);
2. bindet das aufgelöste Ziel an genau `<workspace>/data/mail-desk` des verifizierten Workspace; ein
   `--data-dir` in einem anderen Workspace, Traversal oder eine Symlink-/Junction-/Reparse-Komponente in
   `data`/`data/mail-desk` stoppt mit `workspace_binding_invalid` **vor** jedem Read/Write;
3. baut den Plan gegen den **aktuellen** Stand neu; der `plan_id` bindet den Target-Fingerprint, daher ist
   ein für Workspace A freigegebener Token in B stale;
4. stoppt bei Konflikten (`legacy_import_conflict`) oder abweichendem Token (`legacy_import_approval_stale`);
5. hängt nur `import`-Einträge an, mit `source: legacy_import` und unverändertem `recorded_at`;
6. schreibt keine Sonderdatei.

**Wiederholung nach Teilimport:** Ein abgebrochener Append hinterlässt eine Teilmenge. Ein erneuter Dry-run
meldet diese als `already_present`, nur der Rest ist `import`; der alte Token ist stale, der neue Token
hängt ausschließlich den Rest an. **Nach vollständigem Import** ist ein erneutes Apply ohne Import ein
sicherer No-op (`already_complete`, `applied: 0`). Ein Schreibfehler wird als
`legacy_import_write_failed`/`state: unknown` gemeldet; kein blinder Retry, sondern neuer Dry-run.

## 3. Promotion-Recovery nach Crash

`core.sent_ledger.recover_promotion(data_dir, message_id=..., vorgang_ref=..., evid_id=...,
promotion_evidence=[...], ...)` prüft vor einem erneuten Promoten das **exakte Tripel**:

- `message_id` wird normalisiert; `vorgang_ref` und `evid_id` werden **byte-/case-exakt** verglichen.
  Unvollständige Triade ⇒ `promotion_recovery_incomplete` (vor Lease/Write).
- Es muss **genau ein** Evidence-Datensatz mit dieser Message-ID existieren. Kein Treffer ⇒
  `promotion_not_found`; mehrere Treffer (auch identische) ⇒ `promotion_ambiguous`.
- Konkurrierende Evidence mit derselben `evid_id` aber **fehlender** `message_id` ist nicht beweisbar
  eindeutig ⇒ `promotion_recovery_incomplete` (`competing_evidence_missing_message_id`); eine fremde
  Message-ID mit derselben EVID-ID ⇒ `evid_shared_conflict`. Beides stoppt vor jedem Write.
- Der eine Treffer muss exakt das angefragte `vorgang_ref`/`evid_id`-Paar tragen; sonst
  `promotion_conflict` (Teiltreffer werden nicht stillschweigend akzeptiert).
- Im Ledger darf höchstens **eine** Zeile für die Message-ID stehen:
  - exakt passende `synthesized`-Zeile ⇒ `already_consumed`, kein Append;
  - abweichende Zeile ⇒ `ledger_conflict`;
  - mehrere Zeilen ⇒ `ledger_ambiguous`.
- Dieselbe `evid_id` darf nicht bereits an eine andere Message-ID gebunden sein ⇒ `evid_shared_conflict`;
  diese globale Prüfung läuft **vor** dem `already_consumed`-No-op, damit eine passende eigene Ledger-Zeile
  einen fremden EVID-Besitzer nicht verdeckt.
- Der Thread-Anker wird **konservativ** gegen das aktuelle Ledger geprüft: alle Ledger-Zeilen jedes
  `anchor_candidates`-/`thread_key`-Ankers, nicht first-line-wins. Abweichende Doppelzeilen ⇒
  `promotion_anchor_ambiguous`; ein anderer nichtleerer Vorgang ⇒ `promotion_anchor_conflict`; ein
  aufgelöster Anker **ohne** Vorgang ⇒ `promotion_anchor_unresolved` (kein erfundener `follow_up`).
  Das Handoff-Item liefert die Thread-Metadaten; `headers_complete: false` ⇒
  `handoff_metadata_incomplete`, Widerspruch zwischen Handoff- und Evidence-Metadaten ⇒
  `promotion_metadata_conflict`. Die optionalen Evidence-Felder werden strikt typgeprüft:
  `in_reply_to`/`thread_key` nur als Strings (oder fehlend/leer), `references` nur als Liste
  nichtleerer Strings; jede andere Form ⇒ `promotion_metadata_invalid` (keine implizite
  `norm_id`-Coercion, kein zeichenweises Iterieren eines Strings). Fehlt ein Handoff-Item **und**
  liefert auch die Evidence keine Thread-Metadaten, gilt die Nachricht als primär/root (kein
  Anker); es wird kein fremder Anker behauptet.
- Ein noch offener Vorfahre blockiert wie bei `record` mit `ancestor_pending` (nichts
  geschrieben): sowohl über das offene Handoff-Item als auch über die Evidence-Anker, wenn das Ziel
  selbst nicht (mehr) im Handoff steht. Erst nach der Ledger-Zeile des Vorfahren ist der frische
  Thread-Anker stabil. Ein bereits konsumierter Treffer bleibt der `already_consumed`-No-op.
- Nur bei eindeutigem Treffer und fehlender Ledger-Zeile wird **höchstens eine** Zeile mit
  `source: synthesis`, `outcome: synthesized` und dem Tripel angehängt. Optionale Thread-Metadaten
  (`in_reply_to`, `references`, `thread_key`) aus demselben Evidence-Datensatz und dem Handoff-Item
  dienen nur der Thread-Verknüpfung, nie dem Tripel-Matching.
- Die Lease wird vor dem Write verifiziert. Das Verfahren ist at-least-once mit idempotentem Consumer,
  nicht exactly-once; es macht das Crash-Fenster prüfbar, liest das Handoff-Item (Metadaten, keine
  Inhalte), aber keine Consumer-Fachdokumente selbst.

## 4. Datei-, Alias- und Absturzschutz

- **Workspace-Bindung:** Vor jedem mutierenden Import-/Recovery-Pfad wird das aufgelöste Ziel an den
  verifizierten Workspace gebunden. Es muss exakt `<workspace>/data/mail-desk` sein; ein `--data-dir` in
  einem anderen Workspace, Traversal, ein Symlink/Junction/Reparse in `data`/`data/mail-desk` oder ein
  nicht verifizierbarer `lstat` stoppt mit `workspace_binding_invalid` **vor** jedem Read/Write. Eine
  gültige Lease für Workspace A autorisiert nie Workspace B.
- Index, Ledger und Handoff (jeweils inklusive `<datei>.torn`) werden statisch auf Symlink-/Hardlink-Aliase
  geprüft, bevor gelesen oder geschrieben wird; `alias_kind: unverifiable` ist ebenfalls fail-closed.
  Keine automatische Löschung/Quarantäne.
- Eine abgerissene letzte Ledger-Zeile (auch mitten in UTF-8) wird ignoriert, bleibt auf der Platte und
  wandert vor dem nächsten Append ins append-only Sidecar `<datei>.torn`; ein vollständiger Eintrag ohne
  Zeilenumbruch bleibt erhalten. Das gilt für Import- und Recovery-Appends gleichermaßen.
- Der Import verändert die Handoff-Datei **nicht**; er kann ein offenes Item nur über die Ledger-Zeile
  konsumieren (`consumed_ignored` beim nächsten Sync). Ein offenes Handoff bleibt bis dahin byte-gleich.

## 5. Versionsskew

`schema_version` bleibt **1**. Die Felder `outcome`, `source`, `reason_code` und `evid_id` sind additiv und
optional; alte Zeilen bleiben gültig. Ein **alter** Bundle-Reader kennt die Felder nicht und lehnt neue
Zeilen als `ledger_invalid` ab. Das ist fail-closed, aber bedeutet: vor dem ersten erweiterten Schreibvorgang
muss der installierte Bundle-Pin Issue #107 enthalten. Kein stiller Fallback, kein Schema-Bump.

## 6. Konsum-Adapter für den Consumer (Issue #88)

`pending`, `record`, `consume` und `crash-check` sind die geprüften, offline-only
Einstiege des Bundles für den Pending-/Ledger-Konsum. Sie ersetzen keinen zweiten
Index und keine zweite CLI: `scripts/mail_desk_sent_ledger.py` bleibt die einzige
Sent-Ledger-CLI. Rein lesend sind `pending`, `crash-check` und `consume --dry-run`
(keine Lease, keine Schreibzugriffe); mutierend sind `record` und `consume`.

Alle vier binden ein explizites `--workspace` (oder ein kanonisches absolutes
`--data-dir`) an genau `<workspace>/data/mail-desk`; ein fremder Workspace,
Traversal oder eine Symlink-/Junction-/Reparse-Komponente stoppt mit
`workspace_binding_invalid` vor Lesen/Schreiben. Statische Ledger-/Handoff-Aliase
bleiben fail-closed.

### 6.1 `pending` (read-only)

```bash
python3 scripts/mail_desk_sent_ledger.py pending \
  --workspace <consumer> --data-dir data/mail-desk --account <ACCOUNT> --json
```

- Liefert die offenen Handoff-Items als **stabile JSON-Envelope** (kanonische sechs
  Schlüssel; `data.kind: mail-desk.sent-ledger.pending-view`, `view_version: 1`).
  `--json` ist für den dokumentierten Consumer-Aufruf akzeptiert; JSON ist der
  einzige Ausgabemodus.
- **Keine Rohinhalte:** pro Item nur `item_ref`/`thread_ref`/`waits_for` als
  gekürzte SHA-256-Referenzen (16 Hex-Zeichen über die normalisierte Message-ID),
  `headers_complete`, `truncated_fields` (Feldnamen), `anchored`, `anchor`
  (`vorgang_ref`, gehashte `matched_ref`, `match_type`) sowie Zähler. Betreff,
  Adressen, Datum, Envelope-ID und rohe/normalisierte Message-IDs werden nicht
  ausgegeben. Die Referenz ist **keine Anonymisierung** und nicht als
  „unumkehrbar“ zugesichert; sie ist personenbezogen zu behandeln.
- **Lesegate:** Die Pending-Sicht liefert bewusst weder Envelope-ID noch Message-ID.
  Der Körperleseweg ist ein separater, noch nicht belegter Gate (§6.5); bis dahin
  bleibt ein Item ohne Textlesen offen.
- `handoff_state` ist `none` (kein offenes Item) oder `pending`; `written` behauptet
  ein reiner Pending-Aufruf nie. `handoff_present` und `handoff_item_count`
  unterscheiden eine **fehlende** von einer **leeren** Handoff-Datei (beide
  `handoff_state: none`, `pending_count: 0`). `handoff_item_count > 0` bei
  `pending_count: 0` bedeutet: die Datei listet ausschließlich bereits konsumierte
  Reste (`consumed_ignored == handoff_item_count`); das ist **kein** offenes Pending
  und blockiert den Sync nicht. Eine fehlende Datei bleibt dagegen **kein**
  Leerbeweis.
- `consumed_ignored` zählt Handoff-Reste mit Ledger-Zeile; `consumed_outcomes`
  (`synthesized`/`no_evidence`) und `consumed_sources` (`synthesis`/`legacy_import`)
  weisen die Zusammensetzung aus. `ledger` enthält Zeilen-/Message-ID-Zahl,
  Dublettenzahl, Outcome-/Source-Histogramm, `torn_tail` und den Ledger-Hash.
- Eine `no_evidence`-Zeile konsumiert das Item, ist aber **kein** Thread-Anker; eine
  importierte `legacy_import`-Zeile konsumiert und verankert je nach Outcome wie eine
  Laufzeitzeile. `items[]` ist Vorfahren-zuerst geordnet; `waits_for` nennt offene
  Vorfahren aus demselben Handoff.
- Mit `--account` prüft der Aufruf **jedes** offene Item exakt gegen den aus
  `.agents/mail-desk-backend.json` gebundenen Account. Ein Abweicher stoppt die
  gesamte Runde (`mailbox_mismatch`, `mismatch_count`, gehashte `mismatch_refs`);
  eine fehlende/ungültige Bindung stoppt `workspace_config_invalid`, ein explizit
  abweichendes `--account` `account_mismatch`. Ohne `--account` findet kein
  Account-Gate statt (der Sync prüft sein eigenes Schreibziel).
- Fehler (kaputtes JSON, Schemaverstoß, Alias) enden als Fehler-Envelope mit
  `data.reason_code` und Exit `2`; es wird nichts geschrieben.

### 6.2 `record` (append = Konsum)

```bash
python3 scripts/mail_desk_sent_ledger.py record \
  --workspace <consumer> --data-dir data/mail-desk \
  (--item-ref sha256:<ref> | --message-id '<sent@id>') \
  --outcome synthesized --vorgang-ref <V> [--evid-id <EVID>] \
  [--account <ACCOUNT>] --workspace-lease-id <lease>

python3 scripts/mail_desk_sent_ledger.py record ... \
  --outcome no_evidence --reason-code <code> --workspace-lease-id <lease>
```

- Genau **ein** Selektor: die gekürzte Hash-Referenz `item_ref` aus `pending`
  (keine Anonymisierung) oder die rohe `--message-id`. `item_ref` hält die rohe ID
  aus dem Routine-Report; nach dem Konsum ist die Referenz nicht mehr auflösbar,
  dann ist `--message-id` der Idempotenz-Pfad. `norm_message_id` steht in der
  Antwort **nur** bei expliziter `--message-id`-Auswahl; bei `--item-ref` bleibt die
  rohe ID aus der Ausgabe.
- **Lease und Zielbindung vor dem ersten Append:** `record_outcome` selbst hat
  keinen Lease-/Workspace-Guard und prüft eine explizit übergebene `vorgang_ref`
  nicht gegen den Thread-Anker. Der Adapter verifiziert deshalb invocation-eigene
  Lease **und** kanonisches Ziel, bevor er liest/schreibt; fehlt/fremd ⇒
  `workspace_lease_required`, nichts geschrieben.
- **Account-Gate:** `--account` ist bei `record` **optional**. Ohne den Parameter
  findet **kein** Account-/Mailbox-Gate statt; die SOP übergibt ihn immer und prüft
  nach jedem Reload erneut. Mit `--account` gilt dieselbe Runde-weite Regel wie bei
  `pending` (`mailbox_mismatch`, `account_mismatch`,
  `workspace_config_invalid`).
- `synthesized` verlangt eine exakte, nichtleere `vorgang_ref`, verbietet
  `reason_code` und erlaubt optional eine exakte `evid_id`. Eine EVID-ID, die schon
  zu einer anderen Message-ID gehört, stoppt `evid_shared_conflict`.
- **Frischer Anker, konservativ geprüft:** unmittelbar vor dem Append wird
  `resolve_thread_anchor` gegen das aktuelle Ledger neu ausgeführt. Geprüft werden
  **alle** Ledger-Zeilen jedes `anchor_candidates`-/`thread_key`-Ankers, nicht die
  first-line-wins-Sicht: abweichende Doppelzeilen eines Vorfahren stoppen
  `anchor_ambiguous`, jede nichtleere Vorgangsreferenz muss exakt passen
  (`anchor_conflict`), ein aufgelöster Synthese-Anker ohne Vorgang stoppt
  `reference_anchor_unresolved` und ein Item mit `headers_complete: false` stoppt
  bei `synthesized` `handoff_metadata_incomplete`. Der Anker-Gate läuft nur für
  `synthesized`; eine `no_evidence`-Zeile trägt keinen Anker und wird davon nicht
  erzwungen (die offene No-Evidence-Politik bleibt davon unberührt, siehe unten). Es wird keine
  Referenz erfunden (die Entscheidung zum referenzlosen Anker bleibt offen).
- `no_evidence` verlangt einen schema-gültigen Begründungscode
  (`^[a-z][a-z0-9_]{1,63}$`) und verbietet `vorgang_ref`/`evid_id`. Die dokumentierten
  sieben Codes sind Empfehlungen, keine geschlossene Liste; eine strengere fachliche
  Politik ist eine Consumer-/Nutzerentscheidung (offene No-Evidence-Politik) und nicht im Bundle erzwungen.
  Solange diese Politik offen ist, bleiben unklare Zuordnung, unvollständige Header,
  widersprüchliche Evidence und fehlende Vorgangsbindung **offen**; die SOP schreibt
  dann keine `no_evidence`-Zeile und empfiehlt keine konkreten Codes.
- **Konsum:** Jede angehängte Zeile konsumiert die normalisierte Message-ID,
  unabhängig vom Outcome. Ein offenes Item mit offenem Vorfahren wird mit
  `ancestor_pending` abgelehnt. Das Handoff wird **nie** umgeschrieben
  (`handoff_unchanged`); `pending_after` zählt die verbleibenden offenen Items.
- **Idempotenz:** Ist die normalisierte Message-ID bereits mit passender Identität
  (Outcome, expliziter Vorgang/EVID, Grund) im Ledger, ist der Aufruf ein No-op
  (`already_consumed`, `appended: 0`). Eine abweichende Identität ist
  `ledger_conflict`; identische Zeilen werden nie doppelt angehängt.
- Ein Schreibfehler meldet `sent_ledger_write_failed`/`state: unknown`; kein blinder
  Retry, zuerst `pending`/`crash-check` erneut lesen.

### 6.3 `consume` (Batch-Konsum bereits ausgewerteter Items)

```bash
python3 scripts/mail_desk_sent_ledger.py consume \
  --workspace <consumer> --data-dir data/mail-desk \
  --evaluations data/mail-desk/sent-evaluation.json \
  --account <ACCOUNT> --workspace-lease-id <lease>
```

`consume` ist der dünne Delegationspfad: Der Consumer bleibt Eigentümer der
inhaltlichen Auswertung (Körperlesen, Projekt-/Themenzuordnung, EVID-Pflege) und
übergibt deren Ergebnis als **Evaluationsdokument**; das Bundle schreibt keine
zweite Konsumlogik und keine Ledger-Zeile im Consumer. Der Consumer kann damit
von einer eigenen Ledger-Schleife auf einen Aufruf zurückgebaut werden.

```json
{
  "schema_version": 1,
  "kind": "mail-desk.sent-evaluation",
  "items": [
    {"item_ref": "sha256:<ref>", "outcome": "synthesized",
     "vorgang_ref": "V-7", "evid_id": "EVID-2026-0042"},
    {"message_id": "<sent@id>", "outcome": "no_evidence",
     "reason_code": "legacy_status_no_evidence"},
    {"item_ref": "sha256:<ref>", "outcome": "not_evaluated"}
  ]
}
```

Das Dokument ist Consumer-Transit, kann rohe `message_id`-Selektoren enthalten und ist
damit personenbezogen: Das Bundle schreibt es nie und es wird **nie gestaged**. Die
Beispielcodes im Dokument sind Platzhalter einer bereits getroffenen
Consumer-Entscheidung, **keine Empfehlung**; solange die No-Evidence-Politik offen ist,
wird keine `no_evidence`-Zeile geschrieben.

- **Genau ein Selektor** je Eintrag: `item_ref` aus `pending` oder die rohe
  `message_id`. Beide Selektoren für dasselbe Ziel in zwei Einträgen sind
  `evaluation_duplicate`; doppelte Selektoren im Dokument `evaluation_invalid`.
- **Nur drei Outcomes:** `synthesized` (exakte `vorgang_ref`, optional `evid_id`,
  kein `reason_code`), `no_evidence` (schema-gültiger `reason_code`, kein Anker)
  oder `not_evaluated` (keine weiteren Felder). Jede andere Form, ein unbekanntes
  Feld oder ein fehlender Anker stoppt `evaluation_invalid`; **vor** dem ersten
  Append wird das gesamte Dokument validiert.
- **Fail-closed:** Ein offenes Item ohne Eintrag oder mit `not_evaluated` bleibt
  offen und wird als `not_evaluated` (nur gehashte `item_ref`) gemeldet. Es wird
  kein Outcome erfunden; `pending_after` bleibt entsprechend > 0.
- **Vorprüfung vor jedem Write:** doppelte Ziele (`evaluation_duplicate`), ein
  bereits dauerhaft gebundenes, aber widersprüchliches Ziel (`ledger_conflict`),
  zwei Einträge mit derselben `evid_id` (`evid_shared_conflict`) und ein fremder
  EVID-Besitzer im Ledger stoppen die ganze Runde, bevor eine Zeile entsteht.
- **Ein Append pro Item, dieselben Gates wie `record`:** Lease und kanonische
  `<workspace>/data/mail-desk`-Bindung werden vor der Runde und vor jedem Append
  geprüft; der gemeinsame `workspace-lock`-Guard wird ausnahmslos mit
  `allow_legacy=False` aufgerufen (kein Env-/Manifest-Bypass). Jeder Append läuft über `record_pending_outcome` → `record_outcome`
  (die gemeinsame Implementierung von `record_synthesized`/`record_no_evidence`;
  `synthesized` mit Vorgang/EVID, `no_evidence` mit Begründungscode) und damit über
  alle Gates (frischer Anker, exakte Vorgangs-/EVID-Prüfung, offene Vorfahren,
  Account-Gate). Ein Item mit
  offenem Vorfahren wird zunächst zurückgestellt (`deferred`,
  `ancestor_pending`) und nach dem Konsum des Vorfahren in derselben Runde erneut
  versucht. Ein fachlicher Konflikt eines Items (`anchor_conflict`,
  `anchor_ambiguous`, `reference_anchor_unresolved`,
  `handoff_metadata_incomplete`, `evid_shared_conflict`, `ledger_conflict`,
  `not_pending`) lässt nur dieses Item offen und endet als
  `consume_blocked` (Exit `2`); bereits geschriebene Zeilen bleiben dauerhaft, ein
  erneuter Lauf ist idempotent.
- **Idempotenz:** Ein zweiter Lauf mit demselben Dokument schreibt nichts;
  bereits dauerhafte Ziele erscheinen als `already_consumed` (nur gehashte
  `item_ref`), `pending_before`/`pending_after` sind `0`. Ein Eintrag, der einem
  dauerhaften Ziel mit anderer Identität widerspricht, ist `ledger_conflict` und
  schreibt nichts. Einträge ohne offenes und ohne dauerhaftes Ziel sind
  `unmatched_evaluations` (z. B. Altbestand des Consumer-Index) und werden nicht
  als Erfolg gezählt.
- **`--dry-run`:** validiert Dokument, Bindung und Konsumkandidaten, nimmt **keine
  Lease** und schreibt nichts. `pending_after` bleibt der Ist-Stand;
  `would_consume`, `would_consume_outcomes` und `projected_pending_after` zeigen
  die Prognose. `not_evaluated`/`deferred` werden wie im echten Lauf gemeldet.
- **Envelope:** `data.kind: mail-desk.sent-ledger.consume`, `view_version: 1`,
  `dry_run`, `pending_before`, `pending_after`, `consumed`/`appended`,
  `already_consumed`, `not_evaluated`, `deferred`, `blocked`, `evaluation_entries`,
  `unmatched_evaluations`/`unmatched_refs`, `ledger_sha256_before/after`,
  `handoff_sha256_before/after`, `handoff_unchanged`. Das Handoff wird nie
  umgeschrieben. Die Antwort enthält **keine** rohen/normalisierten Message-IDs,
  nur gehashte `item_ref`/`waits_for` (keine Anonymisierung).
- **`status`:** `empty` (kein offenes Item), `consumed` (alle offen gewesenen
  Items konsumiert), `partial` (ein Teil konsumiert oder als `deferred`
  zurückgestellt, Rest offen), `not_evaluated` (nichts konsumiert und nichts
  zurückgestellt, weil nichts ausgewertet war) oder bei `--dry-run` `planned`.
  `success: true` allein bedeutet **nicht** „alles
  konsumiert“; `pending_after`, `not_evaluated_count`, `deferred_count` und
  `blocked_count` sind maßgeblich.

### 6.4 `crash-check` (read-only)

```bash
python3 scripts/mail_desk_sent_ledger.py crash-check \
  --workspace <consumer> --data-dir data/mail-desk \
  --message-id '<sent@id>' --vorgang-ref <V> --evid-id <EVID> \
  [--evidence-file <durable-promotions.json>]
```

- Rein lesend (keine Lease, keine Schreibzugriffe); die normalisierte Message-ID und
  die **exakten** Token `vorgang_ref`/`evid_id` werden verglichen, nie Substrings.
  Leerraum-/Case-Abweichungen sind keine Treffer; ein ungültiger Token stoppt
  `promotion_recovery_incomplete`.
- Ohne `--evidence-file` klassifiziert der Aufruf das **Ledger**: `status: unique`
  (genau eine exakt passende `synthesized`-Zeile), `absent` (keine Zeile) oder
  `conflict` (abweichende Zeile, doppelte Zeilen, fremd gebundene EVID-ID).
- Mit `--evidence-file` wird zusätzlich die vollständige dauerhafte
  Promotion-Evidence klassifiziert (`evidence_state: not_provided|unique|absent|
  conflict`). Nur eindeutige Evidence **und** fehlende Ledger-Zeile ergeben
  `status: unique` mit `append_required`/`safe_to_append: true`; fehlende Evidence
  bei bereits konsumierter Zeile ist `evidence_incomplete`. Eine fremd gebundene
  EVID-ID ist auch dann `evid_shared_conflict` (**nicht** `absent`), wenn kein
  Datensatz zur Ziel-Message-ID existiert.
- **Thread-Anker (konservativ):** Ist ein Handoff-Item vorhanden, sind seine Header
  maßgeblich; `headers_complete: false` ⇒ `handoff_metadata_incomplete`. Handoff- und
  Evidence-Metadaten dürfen sich nicht widersprechen
  (`promotion_metadata_conflict`); malformed Evidence-Metadaten (`in_reply_to`/
  `thread_key` kein String, `references` keine Liste nichtleerer Strings) ⇒
  `promotion_metadata_invalid`, ohne `norm_id`-Coercion. Geprüft werden **alle**
  Ledger-Zeilen jedes Anker-Kandidaten und des aufgelösten Ankers: widersprüchliche
  Doppelzeilen ⇒ `promotion_anchor_ambiguous`, ein anderer nichtleerer Vorgang ⇒
  `promotion_anchor_conflict`, ein referenzloser Anker ⇒
  `promotion_anchor_unresolved`. Das gilt auch ohne `--evidence-file`, wenn das
  Handoff-Item die Thread-Metadaten liefert. Ein offener Vorfahre ist
  `conflict`/`ancestor_pending` (auch wenn ihn nur die Evidence-Anker nennen und das
  Ziel nicht mehr im Handoff steht); `safe_to_append` ist dann **immer** `false`, wie
  bei `record`. Fehlt ein Handoff-Item **und** liefert die Evidence keine
  Thread-Metadaten, gilt die Nachricht als primär/root (kein Anker); es wird kein
  fremder Anker behauptet.
- `conflict` endet als **Fehler-Envelope** (Exit `2`), nie als `success: true`; das
  ist keine Ausnahme wie beim blockierten `plan`. `unique`/`absent` liefern Exit `0`.
  `absent` autorisiert **keine** Promotion und keinen Append; es belegt nur, dass für
  diese Message-ID keine dauerhafte Triade sichtbar ist.
- Der Aufruf ersetzt `recover-promotion` nicht: `recover-promotion` bleibt der
  mutierende, lease-gebundene Pfad mit vollständiger Evidence und derselben
  konservativen Ankerprüfung.

### 6.5 Offener Körperlese-Gate

Die Pending-Sicht liefert absichtlich keine Envelope-ID und keine rohe Message-ID.
Der SOP-Pfad darf deshalb **kein** Textlesen behaupten, solange kein deklariertes,
geprüftes Werkzeug einen JSON-Manifest-Leseweg mit **Account-, Ordner-,
Envelope-ID- und normalisierter Message-ID-Bindung** bereitstellt und die
Datenschutz-/Manifestentscheidung vorliegt. Bis dahin gilt: Item bleibt offen, keine
Promotion, keine Konsumzeile, kein Ad-hoc-/Inline-Ersatz. Ein direkter
`read`-Subcommand ist technische Fähigkeit, aber keine freigegebene SOP-Option.

## 7. CLI

```text
python3 "$MAIL_DESK_SKILL/scripts/mail_desk_sent_ledger.py" plan \
  --workspace <consumer> --data-dir data/mail-desk --inventory <legacy-inventory.jsonl>

python3 "$MAIL_DESK_SKILL/scripts/mail_desk_sent_ledger.py" apply \
  --workspace <consumer> --data-dir data/mail-desk --inventory <legacy-inventory.jsonl> \
  --approval sha256:<plan_id> --workspace-lease-id <lease>

python3 "$MAIL_DESK_SKILL/scripts/mail_desk_sent_ledger.py" pending \
  --workspace <consumer> --data-dir data/mail-desk --account <ACCOUNT> --json

python3 "$MAIL_DESK_SKILL/scripts/mail_desk_sent_ledger.py" consume \
  --workspace <consumer> --data-dir data/mail-desk \
  --evaluations data/mail-desk/sent-evaluation.json \
  --account <ACCOUNT> --workspace-lease-id <lease> [--dry-run]

python3 "$MAIL_DESK_SKILL/scripts/mail_desk_sent_ledger.py" record \
  --workspace <consumer> --data-dir data/mail-desk --message-id '<sent@id>' \
  --outcome synthesized --vorgang-ref V-7 --evid-id EVID-2026-0042 \
  --account <ACCOUNT> --workspace-lease-id <lease>

python3 "$MAIL_DESK_SKILL/scripts/mail_desk_sent_ledger.py" crash-check \
  --workspace <consumer> --data-dir data/mail-desk \
  --message-id '<sent@id>' --vorgang-ref V-7 --evid-id EVID-2026-0042 \
  [--evidence-file <durable-promotions.json>]

python3 "$MAIL_DESK_SKILL/scripts/mail_desk_sent_ledger.py" recover-promotion \
  --workspace <consumer> --data-dir data/mail-desk \
  --message-id '<sent@id>' --vorgang-ref V-7 --evid-id EVID-2026-0042 \
  --evidence-file <durable-promotions.json> --workspace-lease-id <lease>
```

`plan`, `pending`, `crash-check` und `consume --dry-run` sind lesend; `pending`,
`crash-check` und `consume --dry-run` brauchen keine Lease.
`apply`, `record`, `consume` und `recover-promotion` sind mutierend, verlangen eine verifizierte Lease **und** die
kanonische Bindung des Ziels an `<workspace>/data/mail-desk`; Exit `0` = Erfolg/No-op, `2` = fail-closed
(Konflikt, Approval, Lease, Workspace-Bindung, Alias, crash-check-Konflikt, `consume_blocked`), `1` = unerwarteter Fehler.
Die Evidence-Datei ist ein JSON-Array von Objekten oder `{"promotions": [...]}`; sie ist Eingabe, kein
Bundle-Report.
