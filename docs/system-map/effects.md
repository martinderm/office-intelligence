# Office Intelligence — Paket System Map: Seiteneffekte (Effects)

> **Typ**: ICM Form 6 (`system-map`), Dimension: Seiteneffekte  
> **Ziel**: Vollständige Dokumentation aller Systemgrenzen, Lock-Bedingungen, Dateisystem-Garantien, Fail-Closed-Verhalten und Umwelt-Interaktionen.  
> **Gültig für**: Repository Root & konsumierende Workspaces (relativ)

---

## 1. Zero-Mutation-Garantie für das Bundle

Das Repository `office-intelligence` (einschließlich aller Unterordner in `skills/`) ist ein **statisches Code- und Dokumentations-Paket**.

* **Invariant:** Kein Skript aus dem Bundle modifiziert zur Laufzeit Dateien innerhalb von `skills/office-intelligence/`.
* **Keine Bundle-Control-Plane:** Es werden keine `.agents/session.lock`-Dateien oder temporäre Cache-Dateien im Bundle-Root angelegt oder committed.
* **Zielort aller Mutationen:** Schreiboperationen finden ausnahmslos in den explizit deklarierten Verzeichnissen des Ziel-Workspaces statt (`memory/`, `data/`).

---

## 2. Workspace-Lock & Concurrency-Regeln

Um Race Conditions zwischen parallelen Agenten-Sitzungen (z. B. Codex, Antigravity, Claude Code) zu verhindern, unterliegen alle mutierenden Operationen dem Lock-Guard:

* **Lock-Erwerb:** Vor Beginn einer Mutation muss der ausführende Harness die Lock-Lease im Ziel-Workspace über den Nachbar-Skill `workspace-lock` besitzen.
* **Fail-Closed Guard:** Skripte rufen `require_workspace_lock()` auf:
  * Fehlt die Lease oder ist sie abgelaufen → Sofortiger Abbruch (`WorkspaceLockRequiredError`, Stopcode `WORKSPACE_LOCK_REQUIRED`).
  * Aktiver fremder Lock vorhanden → Sofortiger Abbruch; kein stilles Überschreiben.
* **Kein Legacy-Bypass (normativ, geschlossen):** `--allow-legacy` (`allow_legacy=True`) und Umgebungs-Overrides wie `WORKSPACE_LOCK_ALLOW_LEGACY` dürfen keine Lease ersetzen. Der zuvor offene Drift in den Attachment-Fetch-/Extract-/Quarantäne-Pfaden des `mail-desk` ist mit **FR-15/MD-E1-T01 geschlossen**: `verify_workspace_lock()` wertet den Env-Override nicht mehr aus und führt keinen `allow_legacy`-Parameter mehr; die Aufrufer im Fetch-/Extract-/Cleanup-Pfad rufen den shared Guard ausnahmslos mit `allow_legacy=False` auf, und weder Env-, Parameter- noch Manifest-Werte können Legacy reaktivieren. Zulässig bleibt nur die vertrauenswürdige Lease-/Conversation-ID der Harness-Control-Plane. Detailstand inklusive Quellbezügen steht in der L2-Karte [`skills/mail-desk/docs/system-map/effects.md`](../../skills/mail-desk/docs/system-map/effects.md) §3. Der **Produktions-Preflight gegen getrackte Quarantäne ist mit FR-15/MD-E1-T02 implementiert** (`quarantine_preflight.py`; bounded read-only `git ls-files` ohne Shell mit Timeout, nach der Ownership-Prüfung und vor dem ersten Quarantäne-/Derivat-Write; jede getrackte Datei unter `data/mail-desk/attachments/` bzw. `**/.quarantine-inventory.json`/`.lock` sowie Non-Zero-Exit, Timeout und unlesbarer Index stoppen fail-closed). Der **kontextgebundene Receipt-Klassen-Guard ist mit FR-15/MD-E1-T03 implementiert** (`skills/mail-desk/scripts/core/attachment_authorization.py`; interne Maschinen-Autorisierungs-Fabrik erzeugt `receipt_class: "machine"`/`receipt_type: "attachment_auto_evaluation"` mit prozessinterner Provenienz, und der Guard weist die Maschinen-Klasse in Filing/Promotion/Export/Disposition/Apply-Discard/direktem Fetch fail-closed ab, während typenlose menschliche FR-08-Receipts unverändert gültig bleiben). Der **policygebundene Evaluierungs-Orchestrator ist mit FR-15/MD-E1-T04 als Skeleton begonnen, mit FR-15/MD-E1-T05 um die positive Auswertungsstrecke erweitert und mit FR-15/MD-E1-T06 um die fail-closed Fehler-/Reason-Matrix geschlossen** (`skills/mail-desk/scripts/core/attachment_evaluation.py`): `attachment_evaluate` revalidiert echte MIME-Kandidaten, prüft **vorab** den kanonischen Workspace-Lock (`attachment_fetch.verify_workspace_lock`, kein I/O davor), mintet und prüft die interne Maschinen-Autorisierung im `evaluation`-Kontext und übergibt erst danach den nicht-autoritativen `capability.to_dict()`-Snapshot als strukturellen `approval_receipt` an das unveränderte `op_attachment_fetch` (eigener Lock-/Preflight-/Drift-Check bleibt bestehen). Unter **einer** `run_id` je Mail laufen `fetched` und `already_fetched` identisch durch `extract_attachment_content`; aus den angereicherten Envelopes und den kanonisch gebundenen Parts entsteht **ein** `build_attachment_analysis_handoff(default_materiality="required_for_decision")`, das vor der Rückgabe mit `validate_attachment_handoff` validiert wird. Der Erfolgs-Envelope ist `{"attachment_evaluation": {…}, "attachment_analysis_handoff": {…}}`: erfolgreich `completed`/`handoff_ready`/`auto_evaluated` mit sicheren `files[]`; ein validierter `blocked_on_required_attachment`-Handoff bleibt `completed`/`still_ambiguous` (niemals `supplementary`). **Mit FR-15/MD-E1-T06 ist die negative Fehler-/Reason-Matrix implementiert:** nur exakt erwartete kanonische Ausnahmen (fehlender/fremder Lock, aktiver Inhalt/disallowed Extension, MIME-/Extension-Drift, getrackte Quarantäne/Preflight, Quote, Identity-/Hash-Drift, Kollision/Inventar, Symlink-Escape) und der terminale Extraktionsstatus `extraction_failed` werden auf bounded `failed`-Envelopes abgebildet (`lock_unavailable`, `policy_blocked`, `quota_exceeded`, `fetch_failed`, `extraction_failed`, `handoff_invalid`) mit `authorization: "not_applicable"`, leerem `files[]`, `used_for_classification: false`, `classifier_revision: null`, ohne Handoff-Geschwister und ohne Exception-Text/Rohinhalt/absoluten Pfad; kanonisch gültige, aber unvollständige erforderliche Evidenz (`corrupt_attachment`/`attachment_conversion_unavailable`) bleibt `required_for_decision` und endet `completed`/`still_ambiguous`. Jede `DraftManifest`-Installation bleibt MD-E2; Promotion-/Export-Laufzeitpfade existieren weiterhin nicht. Mit **FR-15/MD-E1-T07** ist MD-E1 als Paket abgenommen: ein hermetischer End-to-End-Test belegt Inspect → policygebundenen Fetch → begrenzte Extraktion → validierten Handoff bei null Mailbox-/Promotion-/Export-/Dispositions-Writes und blockiert zugleich alle bestehenden Downstream-Mutationsseams; Reklassifikation und `DraftManifest`-Installation bleiben **MD-E2**; MD-E2-T01, MD-E2-T02 und MD-E2-T03 (fail-closed-Härtung, kanonische Handoff-Revalidierung, AST-/Katalog-/Hash-gebundene Revision, deterministische `already_fetched`-Idempotenz und opt-in `inspect`-`manifest_proposal` über denselben Item-Flow mit `manifest_file`-Grenze) sind implementiert, MD-E2-T04 offen.

---

## 3. Dateisystem-Sicherheit & Integrität

### 3.1 Atomare Schreiboperationen (Replace-Semantik)
Keine schreibende Skriptoperation schreibt direkt in eine bestehende Zieldatei:
1. Schreiben der Daten in eine temporäre Datei (`tempfile.NamedTemporaryFile`) im selben Verzeichnis (selbes Dateisystem/Mount-Point).
2. Vollständiges Flush und Sync auf Disk.
3. Atomarer Austausch via `os.replace()`.
4. **Garantie:** Bei einem Prozessabbruch, Stromausfall oder Timeout bleibt die bestehende Datei entweder im Originalzustand erhalten oder wird vollständig ersetzt. Es entstehen keine unvollständigen oder korrupten JSON-/Markdown-Dateien.

### 3.2 Pfad-Disziplin & Relativität
* Generell **keine absoluten Host-Pfade** (`C:\Users\...`, `/home/...`) in generierte JSON-Dateien, Kataloge oder Evidenzprotokolle schreiben.
* Alle Pfadreferenzen innerhalb von Katalogen und Indizes sind **relativ zum Workspace-Root** zu halten (Portabilität und Vermeidung von Datenlecks bei Git-Push).

---

## 4. Externe Subprozesse & Adapter-Grenzen

Das Bundle steuert bei Bedarf externe CLI-Tools an, kapselt diese jedoch strikt:

| Werkzeug | Verwendung | Sicherheits- & Laufzeit-Grenze |
| :--- | :--- | :--- |
| **Himalaya** | E-Mail-Fetch & Search (`mail-desk`) | Strikter Prozess-Timeout; kein interaktiver Konfigurations-Wizard (`himalaya_config_missing` stoppt sofort); keine Shell-Expansion. |
| **Pandoc** | Markdown-Konvertierung (`cloud-atlas`) | Subprozess mit isolierten Argumenten und Timeout; keine Ausführung von eingebetteten Skripten. |
| **LibreOffice** | Headless-Konvertierung (`cloud-atlas`) | Ausführung im Headless-Modus (`--headless --convert-to`); Isolation vor GUI-Abhängigkeiten. |
| **Tesseract OCR** | PDF/Bild-Texterkennung (`cloud-atlas`) | Beschränkt auf lokale Bildanalyse; Fallback auf Originaldatei bei Nichterreichbarkeit. |

---

## 5. Sub-Skill-spezifische Effekte (Verweise)

Vertiefende systemspezifische Schutz- und Isolationseffekte sind in den L2-Effektkarten dokumentiert:

* **Mail-Desk Deep Dive:** [`../../skills/mail-desk/docs/system-map/effects.md`](../../skills/mail-desk/docs/system-map/effects.md)  
  *(Windows Reparse-Point-Blockade 0x400, Symlink-Bann, Filterung verbotener Inhalte in Metadaten/Receipts, Inventory-Lock, Monotone Discard-Recovery-Integrität, Fail-Closed Drift-Abbruch, additive Draft-Integration mit genau einem `attachment_evaluation` je Item und content-addressed `classifier_revision` FR-15/MD-E2-T01, fail-closed-Härtung mit kanonischer Handoff-Revalidierung, `still_ambiguous`-Erhalt und deterministischer `already_fetched`-Idempotenz FR-15/MD-E2-T02, opt-in `inspect`-`manifest_proposal` ohne neuen Schreibpfad und mit `manifest_file`-Grenze FR-15/MD-E2-T03)*
* **Cloud-Atlas Deep Dive:** [`../../skills/cloud-atlas/docs/system-map/effects.md`](../../skills/cloud-atlas/docs/system-map/effects.md)  
  *(Schutz signierter PDFs vor In-Place-Mutation, Differenzierung `enrich_source` vs. `local_derivative`, Tool-Timeouts)*
