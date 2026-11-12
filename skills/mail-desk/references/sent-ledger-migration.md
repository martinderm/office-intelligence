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
- Löst die Zeile einen Thread-Anker auf, dessen `vorgang_ref` nicht leer und **anders** als der
  angefragte Triad-Vorgang ist, stoppt `promotion_anchor_conflict`; das gilt für den
  `in_reply_to`/`references`-Pfad und den `thread_key`-Fallback. Ein Anker ohne Vorgang ist kein
  Widerspruch. Eine `follow_up`-Zeile darf nie auf einen anderen Vorgang zeigen.
- Nur bei eindeutigem Treffer und fehlender Ledger-Zeile wird **höchstens eine** Zeile mit
  `source: synthesis`, `outcome: synthesized` und dem Tripel angehängt. Optionale Thread-Metadaten
  (`in_reply_to`, `references`, `thread_key`) aus demselben Evidence-Datensatz dienen nur der
  Thread-Verknüpfung, nie dem Tripel-Matching.
- Die Lease wird vor dem Write verifiziert. Das Verfahren ist at-least-once mit idempotentem Consumer,
  nicht exactly-once; es macht das Crash-Fenster prüfbar, liest aber keine Consumer-Dokumente selbst.

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

## 6. CLI

```text
python3 "$MAIL_DESK_SKILL/scripts/mail_desk_sent_ledger.py" plan \
  --workspace <consumer> --data-dir data/mail-desk --inventory <legacy-inventory.jsonl>

python3 "$MAIL_DESK_SKILL/scripts/mail_desk_sent_ledger.py" apply \
  --workspace <consumer> --data-dir data/mail-desk --inventory <legacy-inventory.jsonl> \
  --approval sha256:<plan_id> --workspace-lease-id <lease>

python3 "$MAIL_DESK_SKILL/scripts/mail_desk_sent_ledger.py" recover-promotion \
  --workspace <consumer> --data-dir data/mail-desk \
  --message-id '<sent@id>' --vorgang-ref V-7 --evid-id EVID-2026-0042 \
  --evidence-file <durable-promotions.json> --workspace-lease-id <lease>
```

`plan` ist lesend und braucht keine Lease. `apply` und `recover-promotion` sind mutierend, verlangen eine
verifizierte Lease **und** die kanonische Bindung des Ziels an `<workspace>/data/mail-desk`;
Exit `0` = Erfolg/No-op, `2` = fail-closed (Konflikt, Approval, Lease, Workspace-Bindung, Alias),
`1` = unerwarteter Fehler. Die Evidence-Datei ist ein JSON-Array von Objekten oder
`{"promotions": [...]}`; sie ist Eingabe, kein Bundle-Report.
