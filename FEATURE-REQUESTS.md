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
| `FR-11` | 🟨 in Umsetzung | Quarantäne-Inventar und Cleanup-Funktionen aus FR-08; `MD-Q1` und `MD-Q2` abgenommen | `MD-Q3` |

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

## FR-11: Attachment-Quarantäne-Hygiene und Lebenszyklus

**Status:** 🟨 In Umsetzung. `MD-Q1` und `MD-Q2` sind abgenommen; nächstes Paket ist `MD-Q3`.
FR-08 legt abgerufene Anhänge und das zugehörige
`.quarantine-inventory.json` unter `data/mail-desk/attachments/<run_id>/` ab. Diese
Dateien sind lokale Laufzeit-/Quarantänedaten und dürfen weder gestaged noch
committed werden. Mail-Desk-Metadaten, Logs, Manifeste und Evidence außerhalb
dieses Unterbaums bleiben weiterhin versionierbar.

**Indexentscheidung:** `final-location-index.json` bleibt ausschließlich der
scriptverwaltete Index der verifizierten Mailboxposition einer Nachricht. Er erhält
keine Attachment-, Quarantäne- oder lokalen Dateipfade. Zusätzlich entsteht ein
kleiner, versionierter `data/mail-desk/attachment-quarantine-index.json`: Er ist das
operative Verzeichnis aller erfolgreich analysierten Anhänge, die nach physischer
Verifikation aktuell noch lokal in Quarantäne liegen. Die Binärdateien und das
run-lokale `.quarantine-inventory.json` bleiben ignorierte Laufzeitdaten.

Der neue Index enthält keine extrahierten Inhalte und keine absoluten Hostpfade.
Eine spätere Dispositionsentscheidung (`retain`, `discard`, `promote`) wird separat
append-only dokumentiert. Eine Entscheidung `promote` führt selbst keine Promotion
aus; sie verweist nur auf den freigegebenen FR-09-Workflow. FR-09-Receipts und
Journale bleiben alleiniger Nachweis der tatsächlichen Promotion.

### MD-Q1 — Git-Hygiene und Vertragsabsicherung

**Scope:** kleines, isoliertes Paket; frische Terra-medium-Session genügt. Vor der
Änderung `AGENTS.md`, `.gitignore`, `skills/mail-desk/SKILL.md`,
`references/cli-operations.md`, `core/attachment_fetch.py` und die MD-A2-Tests
lesen. Workspace-Lock setzen; fremde Änderungen nicht stagen, committen oder
verändern.

1. In der Attachment-CLI-Referenz einen dokumentierten, hermetisch getesteten
   Integrationsvorschlag für Consumer-Workspaces bereitstellen. Keine direkte
   Änderung einer `.gitignore` im Skill-Bundle verlangen; der Skill darf
   Consumer-`.gitignore`-Dateien nie autonom verändern.
2. In der Attachment-CLI-Referenz knapp festhalten, dass der gesamte
   `attachments/<run_id>/`-Baum inklusive Inventar, Lock-, Temp-, Extraktions- und
   Binärdateien lokale Laufzeitdaten sind. Sie werden weder als Evidence noch als
   Final-Index-Inhalt behandelt. Der geplante Quarantäneindex liegt bewusst als
   separate Datei außerhalb dieses ignorierten Unterbaums.
3. Bestehende Quarantänedateien nicht löschen, verschieben, stagen oder öffnen.
   Bereits versehentlich getrackte Dateien sind eine Stop-Bedingung und werden nicht
   autonom aus dem Index entfernt.
4. Einen hermetischen Test oder ein deterministisches Prüfskript ergänzen, das den
   dokumentierten Integrationsvorschlag extrahiert und in einem temporären
   Git-Repository beweist: PDF, beliebige Binärdatei, `.quarantine-inventory.json`,
   Inventory-Lock und Temp-Dateien unter `data/mail-desk/attachments/` sind ignoriert;
   `action-log.jsonl`, `final-location-index.json`, Batch-Manifeste und Evidence
   außerhalb dieses Unterbaums bleiben trackbar.

**Abnahme:** fokussierter Ignore-Vertragstest, vollständige Mail-Desk-Suite,
Compileall, Skill-Validierung und `git diff --check` grün. Das Paket ändert
weder Attachment-Fetch-, Cleanup-, Promotion- noch Final-Index-Schemata.

### MD-Q2 — Versionierter Quarantäneindex

**Scope:** separates Folgepaket nach grünem MD-Q1. Implementiere ausschließlich
scriptbasierten Zugriff auf
`data/mail-desk/attachment-quarantine-index.json` Schema 1. Manuelles Lesen oder
Schreiben im operativen Agentenfluss ist verboten. Der Writer benötigt den
Workspace-Lock und atomaren Replace; parallele oder fremde Locks stoppen.

Ein Eintrag wird erst nach erfolgreicher Extraktion/Analyse und erneuter physischer
Verifikation gegen `.quarantine-inventory.json`, SHA-256 und Größe angelegt. Der
deterministische `attachment_id` bindet normalisierte Message-ID, MIME-Part-Locator
und Inventar-SHA-256. Pflichtfelder:

- `attachment_id`, normalisierte `message_id`, Account und ursprünglicher Folder;
- Part-Locator, bereinigter Dateiname, normalisierter MIME-Typ, SHA-256 und Größe;
- `run_id` und workspace-relativer Quarantänepfad, niemals ein absoluter Pfad;
- `analysis_status: "completed"`, Analysezeitpunkt sowie Version/Hash des
  Extraktions- bzw. Analysevertrags;
- `lifecycle_state: "quarantined"` und optionaler Verweis auf die jüngste
  Dispositionsentscheidung.

Keine Mailtexte, extrahierten Inhalte, LLM-Prompts/-Antworten, Credentials oder
temporären Envelope-IDs aufnehmen. Idempotente Wiederholung mit identischer
Bindung ist No-op; abweichender Pfad, Hash, Größe oder Identität ist Drift und
stoppt. Ein read-only `reconcile` prüft Index gegen Laufzeitinventar und Datei.
Fehlende oder manipulierte Dateien werden als `missing_review`/`drift` gemeldet,
nicht still aus dem Index entfernt oder neu geschrieben.

**Pflichttests:** atomarer Writer, Lock, deterministische ID, Idempotenz,
Pfad-Containment, Symlink/Reparse Point, manipuliertes Inventar, Hash-/Größendrift,
fehlende Datei, unbekannte Felder/Statuswerte, keine absoluten Pfade oder Inhalte,
Reconcile ohne Mutation sowie zwei analysierte PDFs aus getrennten Runs. Der
Final Location Index bleibt byte-identisch.

### MD-Q3 — Disposition, Promotion-Link und sichere Bereinigung

**Scope:** separates Folgepaket nach grünem MD-Q2. Ergänze die versionierte,
append-only Datei `data/mail-desk/attachment-disposition-log.jsonl` mit
scriptbasiertem Writer. Jede Entscheidung bindet `decision_id`, `attachment_id`,
aktuellen Quarantäneindex-Eintragshash, Entscheidung (`retain`, `discard`,
`promote`), Zeitstempel, Human-Receipt-Hash und optional eine Begründung ohne
extrahierten Inhalt.

`retain` darf ein optionales `review_after` setzen. `discard` autorisiert die
physische Bereinigung erst in einem separaten Apply-Schritt. `promote` autorisiert
nur die Übergabe an FR-09 und bindet dessen Candidate-/Review-Hash; erst ein
verifiziertes FR-09-Ergebnis darf `promotion_id` und Promotionstatus nachtragen.
Keine doppelte Promotion-Engine und keine Behauptung eines Cloud-Remote-Syncs.

Vor jeder Löschung sind Workspace-Lock, Run-ID-Validierung, Pfad-Containment,
Reparse-/Symlink-Schutz, Indexeintragshash und aktuelles
`.quarantine-inventory.json` zu prüfen. Aktive Runs, offene/fehlgeschlagene
Promotionen und Anhänge ohne passende `discard`-Receipt bleiben unangetastet.
Nach verifizierter Löschung wird der aktive Quarantäneindex atomisch aktualisiert;
das append-only Dispositionslog bewahrt den Audit-Trail.

Der Default ist read-only Reporting (`eligible`, `protected`, `invalid`). Partial
Failure bleibt sichtbar und darf weder Inventar noch Verzeichnis teilweise als
erfolgreich bereinigt melden. Tests decken Receipt-/Hash-Drift, Retention-Grenzen,
manipulierte Run-IDs, Symlinks/Reparse Points, aktive Promotionen, Idempotenz und
Abbruch zwischen Dateien ab. Keine Mailbox-, Evidence- oder Final-Index-Mutation.
