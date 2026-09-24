# Feature Requests — aktiver Backlog

Diese Datei enthält nur laufende oder geplante Feature Requests sowie deren
verbindliche Paketkarten.

### Zusammenspiel der Dokumente

- [`FEATURE-REQUESTS.md`](FEATURE-REQUESTS.md): Aktiver Backlog mit Status, Paketkarten,
  Spezifikationen und Ausführungsprofilen (SSOT für laufende/geplante Arbeit).
- [`FEATURE-REQUEST-PROGRESS.md`](FEATURE-REQUEST-PROGRESS.md): Ephemere Arbeitsdatei für den
  Implementierungsagenten und das Code-Review. Dokumentiert aktuellen Durchführungsstand,
  Mini-Walkthroughs, Diffs und Verifikationsnachweise des aktiven Pakets; wird nach
  Abnahme bereinigt.
- [`FEATURE-REQUEST-ARCHIVE.md`](FEATURE-REQUEST-ARCHIVE.md): Langzeit-Archiv für vollständig
  abgeschlossene und abgenommene Feature Requests.
- [`COMPLIANCE-REPORT-AGENT-ARCHITECTURE.md`](COMPLIANCE-REPORT-AGENT-ARCHITECTURE.md):
  Bewertet normative Konformität zur Agent-Architektur; kein Feature-Backlog.

## Status und Reihenfolge

| ID | Status | Erledigter Teil | Nächstes Paket |
| --- | --- | --- | --- |
| `FR-09` | ⬜ geplant; Human Gate offen | FR-08 und `attachment_filing_candidate` Schema 1 abgeschlossen | Nach ausdrücklicher Freigabe: `MD-P1` |
| `FR-10` | ⬜ geplant | Temporäre manifestgebundene Host-Ausführung dokumentiert | `MD-G1` |
| `FR-12` | ⬜ geplant | Identifikation des 2.200-Zeilen-Monolithen `convert_cloud_docs.py` in System Map | `CA-M1` |
| `FR-19` | ⬜ geplant | Befund Batch 2026-W39/3 (Env 9428): `apply_local_repairs` füllt nur fehlende Records; stale Index-/Log-Records nach transienter Ziel-Verifikation bleiben stehen (manuell korrigiert) | `MD-RC1` (Repair-Härtung: stale Records nachverifizieren); Paketkarte unten |
| `FR-20` | ⬜ geplant | Befund Batch 2026-W39/3 (Env 9438): Inline-Signaturbilder verbrauchen das Anhang-Zählquota (5); echte `.docx`-Anhänge werden `skipped_count_limit` und nie policy-geprüft | `MD-A3` (Anhang-Quota: Inline vs. Datei); Paketkarte unten |
| `FR-23` | ⬜ geplant | Befund Batch 2026-W39/4 (Env 9451): Anhang-Bewertung fail-closed `lock_unavailable` — der canonical Lock-Guard (`attachment_evaluation.py:600-608`) verlangt, dass die aufrufende Operation das Workspace-Lock besitzt; der Agent hält es während der Batch-Routine, der Runner-Subprozess hat keinen Delegationspfad | `MD-L1` (Lease-Delegation `--workspace-lease-id`, fail-closed für fremde/abgelaufene Leases bleibt); Paketkarte unten |

Vollständig abgeschlossene FRs (FR-01–08, 11, 13–18, 21, 22) stehen im
[`FEATURE-REQUEST-ARCHIVE.md`](FEATURE-REQUEST-ARCHIVE.md).


```text
Human Gate → MD-P1 → MD-P2 → MD-P3
```

FR-10 ist unabhängig von FR-09 und sollte vor einem breiteren produktiven
Mailbox-Betrieb umgesetzt werden.

## Ausführungsprofil für Coding Agents

Jedes Paket erhält eine frische Coding-Session. Innerhalb des Pakets wird linear
gearbeitet: Verträge lesen, Tests zuerst, kleinste Implementierung, fokussierte
Tests, Gesamtsuite, unabhängiges Review. Keine parallelen Agents auf denselben
Mail-Desk-Dateien. Der Implementierungsagent commitet nicht; nach grünem Review
wird genau ein Paket samt Statusupdate committed.

Vor jedem Paket vollständig lesen:

1. `skills/mail-desk/SKILL.md`
2. `skills/mail-desk/references/backends/himalaya.md`
3. die Paketkarte unten
4. die genannten Code- und Testdateien

Vor Mutationen ist der Workspace-Lock erforderlich. Mailboxzugriffe bleiben
JSON-manifestgebunden. Tests blockieren reale Himalaya-, Cloud- und Office-Prozesse.
Mail- und Anhangsinhalte bleiben `untrusted_external`.

Gemeinsame Abnahme:

```powershell
python -B skills/mail-desk/tests/<paket_test>.py
python -B -m unittest discover -s skills/mail-desk/tests -p "test_*.py"
python -B -m compileall -q skills/mail-desk
python <quick_validate.py> skills/mail-desk
git diff --check
```

## FR-09: Human-gated Cloud-Promotion

**Status:** ⬜ Technische Vorbedingungen erfüllt; ausdrückliches Human Gate für
`MD-P1` noch offen. FR-08 ist abgeschlossen und der
`attachment_filing_candidate` als Schema 1 eingefroren. Jede Promotion benötigt
Workspace-Lock, frische Storage-/Filemap-Preconditions und eine hashgebundene
Human-Receipt. Archiv- und Read-only-Storages werden abgewiesen.

### Produktiver Betrieb ohne FR-09

FR-08 kann ohne FR-09 produktiv verwendet werden, endet aber absichtlich bei einem
read-only `attachment_filing_candidate` mit
`promotion_status: "pending_human_review"`. Mailklassifikation, Anhangsinventar,
reviewgebundener Abruf, begrenzte Extraktion, LLM-Handoff und Ablagevorschlag
funktionieren. Es gibt jedoch keinen autorisierten Codepfad, der den Anhang in den
gemounteten Cloud-Speicher schreibt, die Filemap aktualisiert oder einen
Cloud-Atlas-Refresh auslöst.

Bis FR-09 umgesetzt ist, gilt daher:

- Der Vorschlag wird dem Menschen mit Storage, relativem Zielpfad, Dedupe-/
  Kollisionsstatus und Quarantänebezug angezeigt.
- Eine Ablage erfolgt nur manuell außerhalb des Mail-Desk; der Mail-Desk darf sie
  weder behaupten noch als abgeschlossen protokollieren.
- Quarantänedatei und `.quarantine-inventory.json` bleiben lokal bestehen. Es gibt
  derzeit keine automatische Retention oder Garbage Collection; Bereinigung darf
  nur explizit und unter Workspace-Lock über den validierten Run erfolgen.
- `already_present` ist lediglich ein read-only Befund aus der Filemap und kein
  Nachweis einer in dieser Ausführung erfolgten Promotion.
- Ein späterer FR-09-Lauf muss Kandidat, Quarantänedatei, Katalog und Filemap frisch
  revalidieren; alte Vorschläge oder manuelle Ablagen werden niemals blind
  fortgesetzt.

Das ist funktional eingeschränkt, aber sicher: Es entstehen keine stillen
Cloud-Schreibvorgänge. Operativ bleiben dafür manuelle Ablage, mögliche lokale
Quarantänenreste und eine noch nicht aktualisierte Cloud-Atlas-Sicht offen.

### Gemeinsamer FR-09-Vertrag

FR-09 verarbeitet genau **einen** `attachment_filing_candidate` je Operation. Die
Pakete werden nacheinander in frischen Terra-high-Sessions umgesetzt und jeweils
separat reviewt und committed. Keine Session darf mit dem nächsten Paket beginnen,
bevor das vorherige Paket grün ist. Der Coding-Agent liest vor jedem Paket
vollständig:

1. `skills/mail-desk/SKILL.md`
2. `skills/mail-desk/references/batch-runner.md`
3. `skills/cloud-atlas/SKILL.md` bei MD-P3
4. diese Paketkarte und die Progress-Datei
5. `attachment_filing.py`, `attachment_fetch.py` und die Tests von MD-A2/MD-A5

Alle Manifeste, Receipts, Journale und Handoffs verwenden kanonisches JSON
(`sort_keys=True`, kompakte Separatoren, UTF-8) und SHA-256. Freie absolute Pfade,
Pfadwerte aus Mail-/Anhangsinhalt und caller-seitig behauptete Verifikation sind
untrusted und werden abgewiesen. Der Zielpfad wird ausschließlich als
`workspace_root / catalog.cloud_sync[storage_id].scan_dir /
candidate.destination.target_relative_path` rekonstruiert. `scan_dir`, Storage,
Projekt/Topic/Subtopic und Zielpfad werden nie aus dem Receipt übernommen, sondern
aus Katalog und Kandidat neu aufgelöst und anschließend gegen den Receipt geprüft.

„Promotion“ bezeichnet die verifizierte Dateipromotion in das unter `scan_dir`
gemountete bzw. verlinkte Storage-Verzeichnis. Sie beweist nicht, dass ein externer
OneDrive-/BokuDrive-Sync-Client die Bytes bereits serverseitig bestätigt hat.
Cloud-Atlas arbeitet ebenfalls gegen diesen lokalen Storage-Mount und verifiziert
Filemap und Mirror, nicht den Remote-Providerzustand. Eine echte Remote-Sync-
Bestätigung wäre ein separates, adapterabhängiges Feature und darf in FR-09 nicht
behauptet werden.

Zulässig sind nur Kandidaten mit Schema 1, `candidate_type:
"attachment_filing_candidate"`, `status: "proposed"` und
`promotion_status: "pending_human_review"`. `not_configured`,
`storage_review_required`, `directory_review_required`, `already_present` und
`collision_detected` dürfen keinen Writer starten. Events besitzen keinen eigenen
Storage; eine bereits in MD-A5 aufgelöste Parent-/Subtopic-Bindung wird erneut gegen
den aktuellen Katalog geprüft.

Der Workspace-Lock wird ausschließlich aus der vertrauenswürdigen Harness-Control-
Plane übernommen. Lease-/Conversation-ID, `allow_legacy`, Workspace-Root und
Approval-Entscheidung sind keine Manifestfelder. Fehlender oder fremder Lock stoppt
fail-closed. Tests blockieren reale Cloud-, Mailbox- und Office-Zugriffe.

### MD-P1 — Approval und read-only Preflight

**Ziel:** Eine Human Approval kryptografisch an exakt einen aktuellen Kandidaten
binden und unmittelbar vor einer möglichen Promotion ausschließlich lesend prüfen.
Dieses Paket kopiert, löscht und verändert keine Datei.

**Agent und Scope:** Terra-high, frische Session. Neu:
`skills/mail-desk/scripts/core/attachment_promotion.py` und
`skills/mail-desk/tests/test_maildesk_attachment_promotion_mdp1.py`;
`references/batch-runner.md` und Progress-Datei aktualisieren. Noch keine
Writer-Operation und keine Cloud-Atlas-Änderung.

**Öffentliche Funktionen:** Schmale, testbare Funktionen für
`compute_promotion_review_hash()`, `verify_promotion_approval_receipt()` und
`preflight_attachment_promotion()`. Keine zweite Implementierung der MD-A2-, MD-A5-
oder Cloud-Atlas-Validatoren: bestehende öffentliche Validatoren wiederverwenden.

**Input-Vertrag:**

- extern gebundener Workspace-Root und externe Lock-Ownership;
- vollständiger, unveränderter MD-A5-Kandidat samt `candidate_hash`;
- aktuelle Projekt-/Topic-Kataloge oder deren kanonische Pfade;
- aktuelle, exakt nach `storage_id` gebundene Filemap;
- Human-Receipt Schema 1 mit `receipt_type:
  "attachment_promotion_approval"`, `decision: "approved"`, `review_hash`,
  `approved_at`, `expires_at` und optionalem menschlichem Kommentar.

Der kanonische Review-Payload bindet mindestens Candidate-Schema und
`candidate_hash`, Account, Message-ID, Folder, Envelope-ID, Part-Locator,
Quarantänepfad und Quell-SHA-256, Storage-ID, `scan_dir`, Zielverzeichnis,
Zieldateiname und Zielrelativpfad sowie einen Hash des vollständig validierten
Filemap-Snapshots. `approved_at` und `expires_at` sind timezone-aware RFC 3339;
abgelaufene, zukünftig ausgestellte oder unplausibel lange Receipts werden
fail-closed abgewiesen. Tests verwenden eine injizierbare Uhr.

**Preflight-Reihenfolge:** Vor jeglichem mutierenden I/O zunächst alle Eingaben
strukturell validieren, danach:

1. Candidate-Hash kanonisch neu berechnen und MD-A2-Quarantäneevidenz mit
   `verify_quarantine_attachment_artifact()` einschließlich realem Disk-Hash prüfen.
2. Receipt gegen den neu berechneten Review-Payload validieren.
3. Lock-Ownership über den kanonischen Workspace-Lock-Guard prüfen, ohne IDs aus
   Kandidat, Receipt oder Operation zu akzeptieren.
4. Decision und Storage erneut eindeutig aus den aktuellen Katalogen auflösen;
   fehlende, mehrere, inaktive, archivierte oder `read_only` Storages stoppen.
5. `scan_dir` und Zielpfad unresolved und resolved gegen Workspace-Containment,
   Symlinks, Junctions/Reparse Points und Windows-Gerätenamen prüfen. Ziel-Parent
   muss bereits existieren und innerhalb des konfigurierten `scan_dir` liegen;
   MD-P1 legt keine Ordner an.
6. Filemap kanonisch validieren, Aktualität und Snapshot-Hash prüfen und anschließend
   den realen Zielzustand lesen. Gleicher Zielhash ergibt `already_present`;
   vorhandener anderer Hash ergibt `collision_detected`; fehlendes Ziel bei allen
   erfüllten Preconditions ergibt `ready`.

**Output-Vertrag:** Deterministisches `attachment_promotion_preflight` Schema 1 mit
`status` aus `ready`, `already_present` oder einem begrenzten Stopcode, `reason`,
`candidate_hash`, `review_hash`, `source_sha256`, `storage_id`,
`target_relative_path`, `target_path_fingerprint`, `filemap_snapshot_hash`,
`checked_at` und `preflight_hash`. Keine absoluten Pfade in langlebigen Outputs.
Stopcodes mindestens: `approval_missing`, `approval_invalid`, `approval_expired`,
`candidate_drift`, `source_drift`, `lock_unavailable`, `catalog_drift`,
`storage_not_writable`, `filemap_drift`, `unsafe_path`, `parent_missing`,
`collision_detected` und `preflight_error`.

**Pflichttests:** Happy Path; fehlende/manipulierte/abgelaufene Receipt; jeder
gebundene Payload-Wert einzeln gedriftet; eingebettete Lock- oder Workspace-Werte
ignoriert; fehlender/fremder Lock; Quarantäne-, Disk- und Inventar-Hashdrift;
Katalog-/Storage-/Filemap-Drift; Archiv/read-only; absolute Pfade, `..`, Gerätenamen,
Symlink/Junction/Reparse; fehlender Parent; gleicher Zielhash; Namenskollision;
Race-fähiger Zielzustand; eingefrorene Uhr; Schreibfallen für `open`-Write,
`mkdir`, `unlink`, `replace`, Cloud- und Mailbox-Adapter.

**Abnahme:** Fokussierte Tests, komplette Mail-Desk-Suite, Compileall,
Skill-Validierung und `git diff --check` grün. Nachweis, dass MD-P1 bei jedem Fehler
null Mutationen ausführt. Paket und Progress-Update gemeinsam committen.

### MD-P2 — Atomarer Storage-Writer

**Ziel:** Genau einen durch MD-P1 freigegebenen Anhang idempotent und ohne
Überschreiben in den bereits gemounteten, schreibbaren Storage übertragen.

**Agent und Scope:** Terra-high, eigene frische Session nach grünem MD-P1. MD-P1-
Modul schmal erweitern oder einen klar getrennten Writer im selben Modul ergänzen;
neu `skills/mail-desk/tests/test_maildesk_attachment_promotion_mdp2.py`. Keine
Zielwahl, Verzeichnisanlage, Konvertierung, Filemap-Änderung oder Cloud-Atlas-
Ausführung.

**Input-Vertrag:** Unveränderter Kandidat und Approval-Receipt aus MD-P1,
vollständiges `attachment_promotion_preflight` mit `status: "ready"`, extern
gebundener Workspace/Lock sowie aktueller Katalog und aktuelle Filemap. Das
Preflight-Envelope ist Evidenz, aber keine Autorität: MD-P2 revalidiert dessen Hash
und führt den gesamten MD-P1-Preflight unmittelbar vor dem ersten Write erneut aus.

**Journal:** Pro Promotion ein atomisch geschriebenes
`data/mail-desk/attachment-promotions/<promotion_id>/promotion-journal.json` Schema
1. `promotion_id` wird deterministisch aus `review_hash` und `candidate_hash`
abgeleitet. Phasen: `approved`, `preflight_verified`, `temp_written`,
`target_promoted`, `target_verified`, `source_cleanup_pending`, `completed` sowie
`failed`/`recovery_required`. Jede Phase bindet vorherigen Journal-Hash,
Quell-/Zielhash, relative Pfade, Zeitstempel und begrenzten Fehlercode. Unbekannte,
übersprungene oder widersprüchliche Phasen stoppen; Journal und Ziel werden zuerst
reconciliert, bevor ein Retry schreibt.

**Writer-Ablauf:**

1. Nach erneut grünem MD-P1-Preflight den Zielzustand direkt vor dem Write erneut
   prüfen. Gleiches Ziel bereits vorhanden: als idempotenten Erfolg verifizieren;
   anderer Hash: `collision_detected`, kein Write.
2. Bytes aus der verifizierten Quarantänedatei in eine eindeutig benannte
   Sibling-Temp-Datei im Ziel-Parent kopieren. Exklusiv erzeugen, Limits erneut
   prüfen, Flush und `fsync` ausführen, schließen, Größe und SHA-256 neu prüfen.
3. Temp ohne Clobber atomar zum finalen Ziel promoten. Eine vorhandene Zieldatei
   wird niemals ersetzt; kein `os.replace()`-Fallback. Wenn das Dateisystem keine
   verlässliche atomare No-Clobber-Promotion unterstützt, fail-closed stoppen und
   nur die eigene Temp-Datei bereinigen.
4. Finales Ziel neu öffnen und Größe sowie SHA-256 verifizieren; soweit portabel
   auch Parent-Verzeichnis flushen. Erst danach Journalphase `target_verified`.
5. Quarantänequelle erst nach durablem Zielnachweis entfernen. Das zugehörige
   Inventar atomisch unter seinem bestehenden Inventory-Lock aktualisieren; andere
   Attachments desselben Runs bleiben unberührt. Schlägt Cleanup fehl, bleibt die
   Promotion erfolgreich mit `source_cleanup_pending`; ein Retry darf keine zweite
   Zieldatei erzeugen.

**Output und Zustände:** `attachment_promotion_result` Schema 1 bindet
`promotion_id`, `candidate_hash`, `review_hash`, `preflight_hash`, Storage,
Zielrelativpfad, SHA-256, Größe, Journalpfad/-hash und Status. Zulässige Endzustände:
`promotion_completed`, `already_present_verified`, `source_cleanup_pending`,
`collision_detected` oder `recovery_required`. Ein partieller Fehler darf niemals
`promotion_completed` melden.

**Pflichttests:** erfolgreicher Transfer; Zieldatei entsteht zwischen Preflight und
Write; gleicher/anderer Hash; Cross-Volume-Quelle; partielles Schreiben; ENOSPC,
PermissionError, Flush-/Close-/Hashfehler; Prozessabbruch nach jeder Journalphase;
Retry aus jeder Phase; beschädigtes/vertauschtes Journal; fehlender/fremder Lock;
Katalog-/Filemap-/Receipt-/Quell-/Zieldrift; Symlink-/Reparse-Race am Parent oder
Ziel; No-Clobber ohne unsicheren Fallback; exakt eigene Temp-Bereinigung;
inventargebundener Einzeldatei-Cleanup bei weiteren Run-Anhängen; keine Filemap-,
Katalog-, Mailbox- oder Cloud-Atlas-Mutation.

**Abnahme:** Fault-Injection für jede irreversible Grenze, fokussierte Tests und
Gesamtsuite grün. Journal beweist nach jedem simulierten Interrupt einen eindeutigen
reconcilierbaren Zustand. Paket und Progress-Update gemeinsam committen.

### MD-P3 — Cloud-Atlas-Handoff und Mirror

**Ziel:** Eine bereits verifizierte Promotion nachvollziehbar an Cloud-Atlas
übergeben und dessen lokale Filemap-/Mirror-Sicht aktualisieren, ohne die Promotion
zu wiederholen oder Verantwortlichkeiten zu vermischen.

**Agent und Scope:** Terra-high, eigene frische Session nach grünem MD-P2. Mail-Desk
erzeugt nur den Handoff; Cloud-Atlas erhält einen schmalen Consumer bzw. eine
explizite Handoff-Operation. Erwartete Änderungen:
`attachment_promotion.py`, `references/batch-runner.md`, gezielte Cloud-Atlas-
Referenz/Adapterdatei sowie
`tests/test_maildesk_attachment_promotion_mdp3.py` und passende Cloud-Atlas-Tests.
Vor Änderungen am Cloud-Atlas-Code dessen bestehende öffentliche Scanner-/
Converter-/Filemap-Funktionen wiederverwenden; keine parallele Filemap-Engine bauen.

**Handoff-Vertrag:** `cloud_atlas_refresh_handoff` Schema 1 wird nur aus einem
kanonisch revalidierten MD-P2-Ergebnis `promotion_completed` oder
`already_present_verified` erzeugt. Er bindet Promotion-ID, Journalhash,
Candidate-/Review-/Preflight-Hash, Scope und katalogisierte Entity-ID,
Subtopic-ID falls vorhanden, Storage-ID, `scan_dir`, Zielrelativpfad, Ziel-SHA-256,
Größe, vorherigen Filemap-Snapshot-Hash und `handoff_hash`. Absolute Pfade,
Beschreibungen oder Instruktionen aus Mail/Anhang werden nicht übernommen.

**Cloud-Atlas-Consumer:**

1. Eigenen Workspace-Lock und eigene Katalog-/Storage-/Pfadgrenzen prüfen; der
   Mail-Desk-Handoff kann keine Cloud-Atlas-Autorisierung liefern.
2. Handoff-Hash und Promotion-Journal revalidieren und die reale Zieldatei erneut
   gegen SHA-256/Größe prüfen. Bei Drift keine Filemap- oder Mirror-Mutation.
3. Nur den exakt gebundenen Storage aktualisieren. Bevorzugt einen vorhandenen
   inkrementellen Einzelpfad verwenden bzw. schmal ergänzen; falls die kanonische
   Engine nur einen Storage-Scan unterstützt, ist genau dieser Storage-Scan erlaubt,
   niemals ein ungefragter Workspace-weite Full-Scan.
4. Filemap und gegebenenfalls Markdown-Mirror/Derivat ausschließlich über die
   kanonischen Cloud-Atlas-Writer atomisch aktualisieren. Binär-/Office-Inhalte
   bleiben `untrusted_external`; bestehende OCR-/Konvertierungsregeln gelten
   unverändert. Keine In-place-OCR-Sonderbehandlung aus Mail-Desk übernehmen.
5. Nach dem Refresh die neue Filemap laden und verlangen, dass exakt der Zielpfad
   mit erwartetem Hash enthalten ist. Erst dieser Nachweis ergibt
   `refresh_completed`.

**Statuskopplung:** Mail-Desk schreibt `filemap.json` und Mirror niemals selbst.
Ein fehlender Cloud-Atlas-Adapter, Refresh-Fehler, Timeout oder Verify-Fehler ändert
die bereits verifizierte Promotion in den lokalen Storage-Mount nicht: Ergebnis ist
`promotion_completed_refresh_pending` mit demselben `promotion_id`. Ein Retry führt
nur Refresh und Verify erneut aus, niemals MD-P2. Nach erfolgreichem Verify lautet
der kombinierte Zustand `promotion_completed` / `refresh_completed`. Journal- oder
Zieldrift ergibt `recovery_required` und stoppt.

**Pflichttests:** gültiger Handoff; jedes Hash-/Identity-/Storage-/Pfadfeld
manipuliert; Handoff aus unvollständiger Promotion; fehlender/fremder Lock;
Zieldateidrift; Adapter/Tool fehlt; Timeout; Scanner-/Converter-/Filemap-Fehler;
inkrementeller Refresh bzw. genau ein gebundener Storage-Scan; kein unnötiger
Workspace-Full-Scan; neue Filemap enthält Zielhash; atomare Filemap-/Mirror-Updates;
Retry nach Refresh-Fehler ohne erneute Promotion; bestehender Hash idempotent;
untrusted Metadaten/Prompt-Injection; keine Mailbox-, Katalog- oder zweite
Cloud-Dateimutation.

**Abnahme:** Mail-Desk- und Cloud-Atlas-Fokustests, beide betroffenen Gesamtsuiten,
Compileall, Skill-Validierung und `git diff --check` grün. Ein hermetischer
End-to-End-Test führt Candidate → Approval → MD-P1 → MD-P2 → Handoff → Cloud-Atlas-
Verify durch und beweist zugleich, dass ein Refresh-Retry keine zweite Promotion
ausführt. Paket und Progress-Update gemeinsam committen.

### Paketübergaben und Review

Nach jedem Paket dokumentiert der Coding-Agent in
`FEATURE-REQUEST-PROGRESS.md`: geänderte Dateien, öffentliche Verträge,
Trust-Boundaries, fokussierte und vollständige Testergebnisse, bekannte bewusste
Grenzen und den Commit-Kandidaten. Der Review-Agent prüft zuerst adversarial die
Autorisierungs- und Idempotenzgrenzen, danach Tests und Diff. Korrekturen bleiben im
selben Paket; erst ein grünes Review erlaubt Commit und die nächste frische Session.

## FR-10: Least-Privilege Mailbox Gateway

**Status:** ⬜ Geplant. Bis zur Umsetzung darf bei sandbox-unzugänglicher
Himalaya-Config ausschließlich der bestehende JSON-Manifest-Client eng begrenzt
außerhalb der Sandbox laufen. Das ist keine Freigabe für freie Himalaya-Kommandos
oder einen pauschalen dauerhaften Command-Prefix.

**Ziel:** Ein hostseitiges Gateway hält Config und Credentials außerhalb der
Sandbox und bietet Mail-Desk nur versionierte, schema-validierte Operationen mit
strikter Workspace-, Backend- und Account-Bindung. Es gibt keine Shell- oder
generische Execute-Schnittstelle; Ergebnisse und Fehler sind strukturierte JSON-
Envelopes. Umsetzung paketweise in frischen Terra-high-Sessions, jeweils mit
separatem Review und Commit.

### MD-G1 — Read-only Host-Gateway

Implementiere einen schmalen Hostprozess für `list`, `read`, `search`,
`inspect_attachments` und den bereits reviewgebundenen Attachment-Export. Er
akzeptiert nur ein versioniertes Manifest, bindet Workspace, Account und
Config-Profil serverseitig, erzwingt Größen-, Anzahl-, Laufzeit- und
Parallelitätslimits und gibt weder Configpfade noch Secrets zurück. Unbekannte
Felder, Operationen, Account-Drift und unzugängliche Config stoppen fail-closed.
Tests müssen Schema-Manipulation, Shell-/Argument-Injection, Timeout, Drift,
Parallelitätslimit und Credential-Leakage adversarial abdecken.

### MD-G2 — Gated Mailbox Writes

Erweitere das Gateway um `copy`, `move`, `delete` und `send`, aber nur mit
operationsspezifischer, hashgebundener Human-Receipt, aktivem Workspace-Lock,
Message-ID-/Ort-Preconditions, Idempotenzschlüssel, Zielverifikation und
append-only Audit-Journal. Eine generische Schreib- oder Execute-Operation bleibt
verboten. Partial Failure und Drift führen zu `recovery_required`, nie zu stiller
Fortsetzung.

### MD-G3 — Sandbox-Integration und Ablösung

Ergänze den Mail-Desk um Capability Discovery und einen einzigen Gateway-
Transportadapter. Fehlt oder scheitert das Gateway, stoppt der Live-Mailboxpfad
strukturiert; es gibt keinen stillen Direkt-CLI-Fallback. Hermetische End-to-End-
Tests decken Read, Export, freigegebene Writes, Retry und Recovery ab. Nach grüner
Abnahme werden die temporäre Host-Eskalation und alle dafür erteilten schmalen
Ausnahmen entfernt; Dokumentation und Progress-Datei werden nachgezogen.

**Gesamtabnahme:** Fokustests und vollständige Mail-Desk-Suite, Compileall,
Skill-Validierung und `git diff --check` sind grün. Ein End-to-End-Test beweist,
dass ein Sandbox-Client ohne Secret- oder Configzugriff lesen kann und dass jede
Mailbox-Mutation ohne passende Receipt, Lock oder Preconditions fail-closed
bleibt.

## FR-12: Modularisierung von cloud-atlas convert_cloud_docs.py

**Status:** ⬜ Geplant. Reine Refactoring- und Modularisierungsmaßnahme; keine Verhaltens-
oder Schnittstellenänderung.

### Problem & Motivation

`convert_cloud_docs.py` ist mit **103,5 KB und ~2.200 Zeilen** die größte Einzeldatei im
gesamten Repository. Sie vereint derzeit CLI-Argument-Parsing, Prozess-Pools,
Pandoc-Subprozess-Isolation, LibreOffice-Headless-Aufrufe, Tesseract-OCR-Verzweigung,
Bildlink-Neutralisierung, Frontmatter-Injektion und atomare Writes in einem einzigen Modul.
Dies erschwert Reviews, erhöht das Fehlerrisiko bei Feature-Erweiterungen und belastet
Coding-Agenten mit übermäßigem Kontext.

### Ziel & Invarianten

- Vollständige Entflechtung in modulare Konverter-Treiber unter
  `skills/cloud-atlas/scripts/core/converters/`.
- **100 % Schnittstellen- und Verhaltensstabilität:** Die CLI-Fassade
  `skills/cloud-atlas/scripts/convert_cloud_docs.py` behält alle Argumente
  (`--project-id`, `--topic-id`, `--ocr-policy`, `--file-timeout`, `--jobs`, `--no-ocr`,
  `--redo-ocr`, `--json`, etc.), dieselben Structured CLI Envelopes, Exit-Codes und
  Fehlerformate.
- Alle 41 bestehenden Tests der Cloud-Atlas-Suite müssen ohne Anpassung ihrer Assertions
  grün bleiben.

### CA-M1 — Auslagerung der Konverter-Treiber und Markdown-Cleaner

**Scope:**
1. Erstellung des Unterpakets `skills/cloud-atlas/scripts/core/converters/`:
   - `pandoc.py`: Kapselung des Pandoc-Aufrufs, Timeouts und Markdown-Extraktion.
   - `libreoffice.py`: Headless-Konvertierung alter Binärformate (`.doc`) mit isoliertem
     Temp-Benutzerprofil.
   - `ocr.py`: Tesseract-Pipeline, Prüfung von `local_derivative` vs. `enrich_source`,
     Signaturprüfung für PDFs.
   - `markdown_cleaner.py`: Bildlink-Neutralisierung (`neutralize_missing_local_image_links`),
     zirkelfreies Payload-Hashing (`calculate_markdown_payload_sha256`), Frontmatter-Injektion.
2. Isolierte Unit-Tests für jedes neue Submodul unter `skills/cloud-atlas/tests/`.

### CA-M2 — Verschlankung des Haupt-Runners und Testabnahme

**Scope:**
1. Refactoring von `skills/cloud-atlas/scripts/convert_cloud_docs.py`: Bindet die neuen
   Module aus `core.converters` ein und reduziert die Hauptdatei auf einen schlanken,
   übersichtlichen CLI- und Multiprocessing-Orchestrator (< 350 Zeilen).
2. Vollständige Regressionstestung der gesamten Cloud-Atlas-Testsuite (`test_*.py`).

**Abnahme:** Alle 41 Tests grün, `python -B -m compileall -q skills/cloud-atlas`,
`python scripts/validate-skills-catalog.py` und `git diff --check` sauber.

---

## FR-19: Reconcile-Repair-Härtung — stale Records nachverifizieren

**Status:** ⬜ geplant. Befund aus der Batch-Verarbeitung 2026-W39/3 (2026-09-23,
Env 9428, GroupWise-Backend). Keine Codeänderung im Befundlauf; der Drift wurde
lokal manuell korrigiert. Quelle: Betriebslauf `pipelines/mail-desk-batch.md`
(Consumer-Workspace `boku-user`).

### Problem & Motivation

`reconcile --apply_local_repairs` füllt bewusst nur **fehlende** Index-/Action-Log-/
Evidence-Records nach frischer Ziel-Verifikation (`core/modes/reconcile.py`:
`if not in_index` / `if not in_log`). Ist ein Record vorhanden, aber **stale**,
unterbleibt jede Korrektur. Beobachteter Ablauf (Env 9428,
`Re: Antw: THE CALL DOCUMENT`, Message-ID
`1954756627.1848513.1785929416691@mail.yahoo.com`):

1. Erster Lauf: Item als `keep_in_folder`/`INBOX` ausgeführt → Index- und
   Log-Eintrag mit `final_folder: INBOX`, `envelope_id: 9428` (korrekt für
   diesen Lauf).
2. Repair-Lauf (freigegebene Zieländerung nach `Projekte/In Ausarbeitung/ATAEL`):
   `message copy` wirkt auf dem GroupWise-Backend als Move, die anschließende
   Ziel-Verifikation läuft in den Timeout → `RuntimeError: Target verification
   failed after copy.` (routing fail, keine lokale Persistenz im Repair-Lauf).
3. Reconcile verifiziert die Mail im Ziel (`folder_verified: true`,
   `current_envelope_id: 82`), `apply_local_repairs` repariert aber nur die
   fehlende Evidence; Index und Action-Log bleiben auf `INBOX`/`9428` stehen,
   obwohl die Mail in `Projekte/In Ausarbeitung/ATAEL`/`82` liegt.

Verwandte Beobachtung: `runner-progress.json` bleibt nach dem Repair-Flow auf
`status: "failed"` stehen (Tracker wird nicht nachgeführt); im Befundlauf bewusst
nicht angefasst.

### Ziel & Invarianten

- `apply_local_repairs` vergleicht vorhandene Records mit dem verifizierten
  Zustand und aktualisiert mindestens `final_folder`, `envelope_id` (verifizierte
  Ziel-Env) und `updated_at`; weicht der geloggte `target_folder`/
  `new_envelope_id` ab, wird ein kanonischer `reconciled: true`-Action-Log-Eintrag
  angehängt.
- Keine Mutation ohne frische Ziel-Verifikation (`check_folders: true`) und ohne
  expliziten Approval-Receipt; weiterhin null Mailbox-Mutationen.
- Idempotenz: ein zweiter Repair-Lauf ohne Drift schreibt nichts.
- Der beobachtete Fall (Record vorhanden + verifiziertes Ziel ≠ Record) ist als
  Testfall abgedeckt.

### Abnahme

- Hermetischer Test: stale Index-/Log-Record + verifiziertes Ziel → Repair
  korrigiert beide; unveränderte Records bleiben unangetastet; ohne Verifikation
  kein Write.
- `repaired`-Report weist `index`/`action_log` auch im Stale-Fall aus.
- Mail-Desk-Suite grün; System-Map/Referenzdoku aktualisiert.

**Befundnachweis:** `data/mail-desk/archive/2026-W39/2026-09-23-batch-3-batch-reconcile.json`
und `...-batch-3-repair-manifest.json` (Consumer-Workspace); manuelle Korrektur als
Action-Log-Eintrag mit `"reconciled": true` zur o. g. Message-ID.

**Micro-FR (in MD-RC1 enthalten, nicht separat):** Der Repair-Flow führt den
Batch-Progress-Tracker (`runner-progress.json`) nicht nach — der Status bleibt
`"failed"` stehen, obwohl der Repair-Lauf erfolgreich abgeschlossen hat. MD-RC1
setzt den Tracker im Repair-Pfad deterministisch auf das konsistente End-Statum
(`completed` bzw. dokumentierter Repair-Status) und testet die Nachführung;
kein separates Ticket.

## FR-20: Anhang-Quota — Inline-Bilder dürfen echte Anhänge nicht verdrängen

**Status:** ⬜ geplant. Befund aus der Batch-Verarbeitung 2026-W39/3 (2026-09-23,
Env 9438, `Wtrlt: FW: Reaching out to our partners.`). Keine Verhaltensänderung
im Befundlauf.

### Problem & Motivation

Das Anhang-Zählquota (Default 5) wird in Inventar-Reihenfolge auf **alle**
MIME-Teile angewandt, einschließlich Inline-Signaturbilder. Bei Env 9438 standen
6 Inline-`image/png`-Teile vor den echten Dateien; die drei relevanten Anhänge
(`.EVOLVE_Partner_Communication.docx`, `.PIN_Secondments_Malta.docx`, Logo)
erhielten `policy_status: skipped_count_limit` und wurden nie policy-geprüft
(`attachment_evaluation.status: not_needed`). Die Klassifikation war hier
katalogseitig klar; bei einem unklaren Item wäre materiale `.docx`-Evidenz stumm
ausgeblieben (der FR-15/MD-E1-Pfad hätte `no_allowed_attachments` gesehen).

### Ziel & Invarianten (Optionen, Auswahl im Paket)

- **Option A (bevorzugt):** Inline-Teile (`content_disposition: inline` mit
  `content_id`) zählen nicht auf das Datei-Anhang-Quota; sie erhalten ein eigenes,
  kleineres Limit.
- **Option B:** Das Quota wird auf nicht-inline Teile angewandt, bevor Inline-Teile
  inventarisiert werden (Order-Garantie für echte Anhänge).
- In jedem Fall: `policy_reason` bleibt transparent; keine Extraktion außerhalb
  bestehender Policy-/Quarantäne-Gates; deterministische Inventar-Reihenfolge
  unverändert.

### Abnahme

- Hermetischer Test mit 6 Inline-Bildern + 2 erlaubten `.docx` → beide `.docx`
  werden inventarisiert und policy-geprüft (nicht `skipped_count_limit`).
- **Inline-Quota-Festlegung (Option A):** Die 5 zuerst platzierten Inline-Bilder
  erhalten weiterhin `policy_status: allowed` bis zum eigenen Inline-Limit
  (`MD-A3` legt es fest, Vorschlag 3); das 7. und 8. Inline-Bild überschreiten
  das Inline-Limit und tragen `policy_status: skipped_inline_limit` (neuer,
  transparenter Reason — nicht `skipped_count_limit`); die beiden `.docx`
  werden vollständige Datei-Anhänge (inventarisiert + policy-geprüft). Der Test
  pinnt alle drei Zustände je Teilklasse.
- Bestehende Quota-/Policy-Tests (FR-17/MD-R4, FR-15) bleiben grün bzw. werden
  bewusst angepasst; das Anzahl-Limit bleibt als Kostenbremse wirksam.
- Mail-Desk-Suite grün; System-Map/Referenzdoku aktualisiert.

**Befundnachweis:** `data/mail-desk/archive/2026-W39/2026-09-23-batch-3-executed-manifest.json`
(Item 9438, `attachment_status: available`, drei `skipped_count_limit`-Einträge).

## FR-23: Workspace-Lock-Delegation an Batch-Runner-Subprozesse

**Status:** ⬜ geplant. Befund Batch 2026-W39/4 Draft (2026-09-24, boku-user,
Env 9451): die Anhang-Bewertung („LLL-Leistungsportfolio_11.08.26.docx") endete
fail-closed mit `lock_unavailable` / `review_required: true`, obwohl ein
Workspace-Lock aktiv bestand. Der seit FR-22-Klasse gültige canonical Lock-Guard
([`attachment_evaluation.py:600-608`](skills/mail-desk/scripts/core/attachment_evaluation.py) —
„no fetch/extract I/O may happen before the invocation owns the workspace
lock") prüft, dass die **aufrufende Operation** die Lease besitzt; der
Runner-Subprozess startet aber ohne Lease-Argument und kann die von der
Agent-Session gehaltene Lease weder erkennen noch benutzen.

### Problem & Motivation

1. **Systematische Kollision:** Im beabsichtigten Betriebsmodus hält die
   Agent-Session das Consumer-Workspace-Lock für die Dauer der Batch-Routine
   (Pipeline-Vertrag `mail-desk-batch.md`). Der Runner-Subprozess besitzt die
   Lease nicht, das gehaltene Lock zählt als „missing or foreign" — jede
   Anhang-Extraktion unter Agent-Lock endet fail-closed, obwohl die owning
   Invokation existiert.
2. **Fail-closed bleibt richtig:** Der Guard selbst ist korrekt (kein Fetch vor
   Lock-Besitz); es fehlt ausschließlich der legitime Übergabepfad für die
   Invokation, die das Lock hält. Kein Bypass, keine Auto-Beschaffung.
3. **Betroffener Pfad:** `attachment_evaluation.py:600-608` (upfront guard) und
   die Lock-Verifikation in `op_attachment_fetch`.

### Ziel & Invarianten (MD-L1)

- Runner akzeptiert `--workspace-lease-id <id>` (plus Kontext für die
  Eigentumsprüfung, z. B. `--workspace-harness` oder conversation_id) und reicht
  die Lease an die Anhang-Bewertung weiter.
- `verify_workspace_lock` validiert dann Eigentümerschaft (Lease-ID passt,
  Lease aktiv) statt fail-closed; fremde oder abgelaufene Leases bleiben
  fail-closed `lock_unavailable`.
- Ohne Flag: Verhalten exakt wie heute (kein Verhaltenswechsel für
  Offline-/Test-Runs; auch mit Flag kein Fetch, wenn keine Lease existiert).

### Abnahme

- Hermetische Tests: (a) Agent-held Lock + korrekte Lease-ID → Anhang-Bewertung
  läuft; (b) falsche/abgelaufene Lease → fail-closed `lock_unavailable`
  (Regression bewiesen); (c) kein Lock → wie heute.
- Mail-Desk-Suite grün; Metrik-Stellen per FR-16-Checkliste; System-Map-Sync.

**Quelle:** Befund beim Draft 2026-W39/4 (Env 9451, boku-user):
`attachment_evaluation.py:600-608` fail-closed unter Agent-Lock, ohne
Delegationspfad.
