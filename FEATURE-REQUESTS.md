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
| `FR-13` | ✅ Abgeschlossen; MD-M1 (T01–T04) und MD-M2 (Quarantäne-Paketierung unter `core/quarantine/` mit identitätserhaltenden Legacy-Shims) implementiert, getestet und paketabgenommen; **FR-13 geschlossen** | Domänenorientierte Classifier-Entflechtung: kanonisches `core/matching/`-Paket, kontrahierte Facade (810 Zeilen ≤ 813), Kompatibilitätsvertrag und einmalige `classifier_revision`-Rotation; Quarantäne-Paketierung: sechs Owner unter `core/quarantine/`, `sys.modules`-aliasende Shims an alten Pfaden, Monkeypatch-Seams und `core.__init__`-Re-Exports unverändert, 804 Tests grün | keins |
| `FR-15` | ✅ Abgeschlossen; MD-E1 (T01–T07) und MD-E2 (T01–T04) vollständig implementiert, getestet und paketabgenommen; FR-15 geschlossen | FR-08, FR-11 und FR-14 liefern Inventar, Fetch, Extraktion, Handoff und Coverage; MD-E1 (`attachment_evaluate`) liefert das staged `attachment_evaluation` plus validierten Handoff bei null Mailbox-/Promotion-/Export-/Dispositionswrites; MD-E2 verdrahtet die standardmäßig aktive `draft`-Auswertung, die einmalige `untrusted_external`-Neuklassifikation, den opt-in `inspect`-Vorschlag und die finale `DraftManifest`-Installation | keins; nächstes Paket ist `FR-13`/`MD-M1` |
| `FR-16` | ⬜ geplant; reine Dokumentations-/Metrik-Hygiene aus der MD-M2-Retrospektive | Identifikation der Metrik-Vervielfältigung und des monolithischen Tabellenzellen-Anti-Patterns | `DOC-M1` (Metrik-SSOT), `DOC-M2` (Zellen-Splitting); siehe Paketkarte unten |
| `FR-17` | ✅ Abgeschlossen; MD-R1–R7 vollständig implementiert, getestet und paketabgenommen; **FR-17 geschlossen** | Alle 10 Befunde (B-1 bis B-10) behoben: katalogtreues Routing mit `routing_priority` und DNR für Projekte+Topics inkl. Newsletter-Mapping, DNR-gegatete Sibling-Kohärenz, begrenzte Client-Deadlines, Subset-Verify-Scope mit Runner-Provenienz und kanonischem Evidence-Fallback, deterministische MIME-Inventarkette mit Konsistenz-Gate, Inline-Bild-Policy, `keep_in_folder`-Vertragsdokumentation, `progress_*.tmp`-Hygiene; 896 Tests grün | keins |


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

## FR-13: Domänenorientierte Binnengliederung und Matcher-Entflechtung von mail-desk

**Status:** ✅ **Abgeschlossen** (reine Refactoring- und Modularisierungsmaßnahme ohne
Verhaltens- oder Schnittstellenänderung). **`MD-M1` ist mit T01–T04 abgeschlossen**
(kanonisches `core/matching/`, Facade auf 810 physische Zeilen kontrahiert, vollständiger
Kompatibilitätsvertrag, dokumentierte einmalige `classifier_revision`-Rotation);
**`MD-M2` ist abgeschlossen und paketabgenommen (FR-13 geschlossen).**

### Problem & Motivation

`skills/mail-desk/scripts/core/` enthält derzeit 24 Module mit über 500 KB Code in einer
völlig flachen Verzeichnisstruktur. Darin befindet sich mit `classifier.py` (**91,6 KB,
~2.000 Zeilen**) ein weiterer massiver Monolith, der Datums-Parsing, Katalog-Laden,
Projekt-Workpackage-Matching, Deliverables-, Task- und Milestone-Matching, Topic-Matching,
Mehrdeutigkeits-Filter und Manifest-Drafting in einer Datei bündelt. Zudem sind die
sicherheitskritischen Quarantäne-Dateien (MD-A/MD-Q) nicht als geschlossene Domäne
abgegrenzt.

### Ziel & Invarianten

- Strukturierung von `mail-desk/scripts/core/` in fachliche Subpakete:
  - `quarantine/` (MD-A/MD-Q Subsystem für Anhänge)
  - `matching/` (Triage- und Katalog-Abgleich-Heuristiken)
  - `transport/` (Himalaya CLI, Search by ID, Preflight)
- **100 % Abwärtskompatibilität:** Alle bestehenden CLI-Fassaden
  (`mail_desk_batch_runner.py`, `mail_desk_attachment_quarantine_index.py`, etc.) und
  alle Re-Exports in `scripts/core/__init__.py` bleiben erhalten.
- Keine Änderung an den 16 Pflichtfeldern des Quarantäneindex oder den Hash-Garantien.
- Alle >489 Tests der Mail-Desk-Suite müssen ohne Assertion-Brüche grün bleiben.

### MD-M1 — Entflechtung des 2.000-Zeilen-Monolithen classifier.py

**Scope:**
1. Erstellung des Unterpakets `skills/mail-desk/scripts/core/matching/`:
   - `date_parser.py`: Datums-Parsing (`parse_date_to_year_month`).
   - `project_matching.py`: Workpackage-, Task-, Deliverable- und Milestone-Matching
     gegen Schema v3.
   - `topic_matching.py`: Topic- und Subtopic-Matching gegen `topics.json`.
   - `ambiguity.py`: Filterung mehrdeutiger Kandidaten, Beibehaltung in INBOX bei Unklarheit.
2. `classifier.py` importiert die Sub-Matcher und bleibt als konsolidierte API-Fassade
   bestehen; Reduzierung der Dateigröße um > 60 %.

**MD-M1-Abnahme (T01–T04, abgeschlossen):** Das Unterpaket
`skills/mail-desk/scripts/core/matching/` besitzt die kanonischen Owner `date_parser.py`,
`ambiguity.py`, `project_matching.py` und `topic_matching.py`; `classifier.py` bleibt die
kompatible Facade (810 physische Zeilen, ≤ 813; Baseline 2.035) und hält Katalog-I/O,
Full-Reader-I/O, Zwei-Pass-Orchestrierung, Thread-Referenzparsing/-Parent-Lookup,
Anhangsbindung und Manifest-Drafting. Die Thread-Ordner-Inheritance und der
Full-Read-Evidenz-Rebuild sind als `match_thread_project_inheritance`/
`resolve_full_read_project_evidence` (Projekt) bzw. `match_thread_topic_inheritance`/
`resolve_full_read_topic_evidence` (Topic) an diese Owner geroutet (Reihenfolge
Projekt-vor-Topic, exakte Katalogobjekte und Entscheidungs-/Evidenz-/Notiz-/Zielsemantik
erhalten). Der Kompatibilitätsvertrag
`skills/mail-desk/tests/test_classifier_compatibility_contract.py` sichert die gesamte
Basissymbol-, Re-Export-, DI-, Lazy-Import- und Monkeypatch-Oberfläche. Der
`classifier_revision`-Fingerprint bindet weiterhin exakt die geordnete Fünf-Quellen-AST-
Menge (`classifier.py`, `matching/ambiguity.py`, `matching/date_parser.py`,
`matching/project_matching.py`, `matching/topic_matching.py`); da sich diese Quellen in
MD-M1 einmalig geändert haben, rotierten vorhandene `classifier_revision`-Werte genau
einmal (genehmigt). Git-Index-Metrik: 119 getrackte Dateien / 58 unter `scripts/`
(48 unter `scripts/core`) / 46 Testmodule / 786 Tests.

### MD-M2 — Paketierung der Quarantäne-Module unter core/quarantine/ (abgeschlossen)

**MD-M2-Abnahme:** Die sechs zusammengehörigen Quarantäne-/Anhangsmodule sind kanonisch
unter `skills/mail-desk/scripts/core/quarantine/` paketiert: `quarantine_index.py`
(umbenannt aus `attachment_quarantine_index.py`), `attachment_fetch.py`,
`attachment_extract.py`, `attachment_filing.py`, `attachment_policy.py` und
`attachment_handoff.py`. Inhalt byte-erhalten (bis auf zwei Relative-Import-Korrekturen
in `attachment_filing.py`); die alten `core/attachment_*.py`-Pfade sind dünne
`sys.modules`-aliasende Shims, die Objektidentität für Legacy-Importe, `mock.patch`-Strings,
`patch.object`-Seams und alle 33 `core.__init__`-Re-Exports garantieren; die 17 kanonischen
Schema-1-Pflichtfelder, Hash-Garantien und der `classifier_revision`-Fingerprint (bindet
weiterhin ausschließlich `classifier.py` + `matching/*`) sind unverändert. Struktureller
Nachweis: `tests/test_quarantine_package_structure.py` (Paketstruktur, Owner-Vollständigkeit,
Cloud-Atlas-Discovery vom tieferen Level) und
`tests/test_quarantine_compatibility_contract.py` (Modul-/Symbolidentität, Seam-Wirksamkeit,
Fingerprint-Quellenmenge, eingefrorenes `core.__all__`) — genuine strukturelle Red-Tests
(`ModuleNotFoundError: core.quarantine`) vor der Implementierung. Vollständige Mail-Desk-Suite
804/804 grün; Compileall, Skill-Katalog, Workspace-Validator und `git diff --check` sauber.
Git-Index-Metrik: 128 getrackte Dateien / 65 unter `scripts/` (55 unter `scripts/core`) /
48 Testmodule / 804 Tests.

**Scope:**
1. Überführung der zusammengehörigen Quarantäne- und Anhangsmodule in
   `skills/mail-desk/scripts/core/quarantine/`:
   - `attachment_quarantine_index.py`
   - `attachment_fetch.py`
   - `attachment_extract.py`
   - `attachment_filing.py`
   - `attachment_policy.py`
   - `attachment_handoff.py`
2. Bereitstellung transparenter Re-Exports in `scripts/core/` zur Garantie nahtloser
   Kompatibilität für bestehende Test-Suites und externe Konsumenten.

**Abnahme:** Vollständige Mail-Desk-Suite (>489 Tests) grün, Compileall,
`validate-skills-catalog.py` und `git diff --check` sauber.

---

## FR-15: Automatische Anhang-Auswertung bei unklaren Mails

**Status:** ✅ **FR-15 ist abgeschlossen.** **MD-E1 ist vollständig implementiert,
getestet und als Paket abgenommen**, **MD-E2 ist mit T01–T04 vollständig implementiert,
getestet und paketabgenommen (hermetischer Real-Pfad-Akzeptanztest)**. FR-08, FR-11 und FR-14
stellen die erforderlichen MIME-, Quarantäne-, Extraktions-, Handoff- und Coverage-Verträge
bereit; deren Sicherheitsgrenzen bleiben unverändert. FR-15 orchestriert diese
bestehenden Bausteine und baut keine zweite Fetch-, Extraktions- oder
Klassifikationslogik.

**Stand:** Der staged `attachment_evaluation`-Vertrag (`MD-E1` → `MD-E2`), die
Receipt-Klassen-Grenze, die Lock-Legacy-Schließung, der Tracked-Quarantäne-Preflight,
die Materialitätsbindung und die Aufräum-Verantwortung sind unten verbindlich
beschrieben. Die unter „Blockierende Sicherheitsvoraussetzungen für MD-E1" genannten
Punkte sind implementiert, getestet und mit der System Map synchronisiert und damit
Teil der abgenommenen MD-E1-Abnahme; sie waren keine separaten, aufschiebbaren
Tickets. MD-E1 endet am validierten Handoff; **Reklassifikation und
`DraftManifest`-Installation sind ausschließlich MD-E2 und in MD-E1 weder
implementiert noch behauptet.** Mit **MD-E2-T01** ist die standardmäßig aktive
`draft`-Verdrahtung, die Option `--evaluate-attachments`/`--no-evaluate-attachments`
und die einmalige Neuklassifikation samt additiver `DraftManifest`-Installation
implementiert. Mit **MD-E2-T02** ist diese Grenze fail-closed gehärtet: jede bounded
MD-E1-Fehler-/No-Op-Ursache bleibt item-lokal in Review/`INBOX` (`lock_unavailable`,
`policy_blocked`, `quota_exceeded`, `fetch_failed`, `extraction_failed`,
`handoff_invalid`), ein `ready`-Handoff wird vor der Klassifikation kanonisch gegen
Identität, Vorab-Entscheid und Anhangs-Inventar revalidiert, fortbestehende
Mehrdeutigkeit erhält `completed`/`still_ambiguous` mit `auto_evaluated` und sicheren
`files[]`, `classifier_revision` bindet zusätzlich den normalisierten AST des
Classifier-Regelmoduls, unerwartete Backend-/Programmiervertragsfehler schlagen über
`AttachmentReclassificationContractError` fail-loud fehl, und ein deterministischer,
PII-freier Run-ID je Nachricht erreicht im zweiten Default-Lauf MD-E1
`already_fetched` ohne Doppel-Fetch. Mit **MD-E2-T03** ist der opt-in `inspect`-Vorschlag
(`core/modes/inspect.py`) implementiert: `inspect` bleibt ohne Opt-in rein lesend, nur
`evaluate_attachments: true` erzeugt einen top-level, nicht ausführbaren
`manifest_proposal` über denselben Item-Flow, und eine ausführbare Batch-Manifest-Datei
entsteht weiterhin nur bei explizitem `manifest_file`. Mit **MD-E2-T04** ist die
Paketabnahme über den hermetischen Akzeptanztest
`skills/mail-desk/tests/test_batch_runner_mde2_acceptance.py` abgeschlossen: ein einziger
realer Pfad Body/Full-Read → mehrdeutig → realer `text/plain`-Anhang → genau eine
`untrusted_external`-Neuklassifikation → persistiertes Projekt-`DraftManifest` mit bounded
`attachment_evaluation` (`used_for_classification: true`, 64-Hex-`classifier_revision`
gebunden an Classifier-Regeln plus konsumierten Anhangs-Hash) bei null Mailbox-/Netzwerk-
und null Execute-/Promote-/Export-/Filing-/Dispositions-/Katalog-/Cloud-Seiteneffekten.
**FR-15 ist damit geschlossen.**

### Problem und Ziel

Der aktuelle Draft-/Classify-Pfad wertet Header und Body aus, orchestriert aber
die bereits vorhandene Anhangspipeline nicht automatisch. Dadurch bleiben Mails
mit schwachem Body trotz routing-relevanter realer Anhänge als `unknown` /
`unclassified`, mit niedriger Confidence oder `review_required` in `INBOX`. Reale
Beispiele sind eine Mail mit generischem Betreff und Arbeitsplatzbeschreibung
sowie eine Alumni-/LLL-Mail, deren Office-Anhänge erst den fachlichen Kontext
eindeutig machen.

FR-15 wertet bei unklaren Mails ausschließlich policykonforme, aus der echten
RFC-822-MIME-Struktur gebundene Anhänge innerhalb fester Quoten aus und führt den
begrenzt extrahierten Inhalt als `untrusted_external` einer zweiten
Klassifikation zu. Ein weiterhin unklares oder nicht sicher auswertbares Item
bleibt fail-closed in Review/`INBOX`. Die Auswertung verändert keine Mailbox,
promotet oder exportiert keine Datei und autorisiert keine Disposition.

### Verbindlicher Trigger

Eine automatische Auswertung ist nur zulässig, wenn nach der bestehenden
Body-/Full-Read-Klassifikation mindestens eines gilt:

- `decision.kind == "unknown"` oder `decision.id == "unclassified"`;
- `decision.confidence == "low"`;
- `decision.review_required == true`;
- eine dokumentierte `read_escalation` konnte keine eindeutige Zuordnung erzeugen;

und mindestens ein kanonisch revalidierter MIME-Part `fetch_status: "available"`
sowie `policy_status: "allowed"` besitzt und innerhalb aller Einzel- und
Gesamtquoten liegt. Caller-seitig behauptete Policy-, Fetch- oder Part-Werte sind
keine Autorität. Unklare Mails ohne geeigneten Anhang verhalten sich unverändert.

### Autorisierungs- und Sicherheitsvertrag

Der vorgeschlagene maschinelle Receipt-Pfad ist eine bewusste neue
Autorisierungsform und darf den generischen MD-A2-Review-Gate nicht in eine frei
aufrufbare Selbstfreigabe verwandeln:

- Die Evaluierungsautorisierung wird ausschließlich intern aus der
  vertrauenswürdigen Draft-Control-Plane erzeugt, nie aus Mailinhalt,
  Eingabemanifest oder einem vom Caller gelieferten Receipt übernommen. Caller,
  Mail und Manifest können die maschinelle Evaluierungsautorisierung weder liefern
  noch auswählen; ein vom Caller übergebenes Receipt wird vom MD-E1-Pfad nicht als
  Autorität akzeptiert.
- Die maschinelle Autorisierung trägt `receipt_class: "machine"` und
  `receipt_type: "attachment_auto_evaluation"` und bindet mindestens `receipt_id`,
  `request_hash` gleich dem kanonischen MD-A2-`review_hash`, `approved_at`,
  `approved_by: "mail_desk_auto_evaluator"`, Policy-Revision, Account,
  Message-ID, Folder, Envelope-ID, Part-Locator und Inventar-Hash. Ausgabe und
  Auditstatus kennzeichnen sie ausdrücklich als `auto_evaluated`.
- **Receipt-Klassen-Grenze (schmal, nicht pauschal):**
  `attachment_auto_evaluation` ist ausschließlich eine interne
  Maschinen-Autorisierung für den begrenzten MD-E1-Fetch/Evaluate-Flow. Sie ist
  als Human Approval fail-closed abzulehnen von Filing, Promotion, Export,
  Disposition und jedem nicht zum MD-E1-Flow gehörenden direkten Fetch-Pfad.
- **Keine pauschale Bruch-Migration für Human-Receipts:** Die seit FR-08
  ausgelieferte und persistierte Form typenloser menschlicher MD-A2-Receipts (ohne
  `receipt_type`) bleibt gültig und wird durch MD-E1 nicht stillschweigend
  invalidiert. MD-E1 führt daher keine pauschale Schema-Migration ein, sondern nur
  eine schmale, kontext- bzw. receipt-klassenbewusste Validierungsänderung: Der
  erwartete Receipt-Typ wird je Aufrufkontext explizit festgelegt.
  Human-Approval-Aufrufstellen weisen jede maschinelle Receipt-Klasse ab, während
  der MD-E1-Fetch/Evaluate-Pfad ausschließlich die intern erzeugte Maschinenklasse
  akzeptiert.
- **Kein Duplikat des Hash-Validators, aber Pflicht zum Klassen-Guard:** MD-E1
  implementiert keinen zweiten Hash-/Request-Validator, sondern verwendet die
  bestehenden Hash-/Struktur-Validatoren weiter. Der MD-E1-Scope meint damit „kein
  Duplikat des Hash-Validators", nicht „kein Receipt-Guard". Der schmale
  Receipt-Klassen-/Issuer-/Policy-Guard zur Durchsetzung obiger Grenze ist
  ausdrücklich Teil der MD-E1-Implementierung.
- `op_attachment_fetch()` behält seinen bestehenden expliziten Receipt- und
  Drift-Vertrag. Der neue Orchestrator darf ihn nur nach aktiver, zum ausführenden
  Harness gehörender Workspace-Lock-Prüfung aufrufen. Fehlender oder fremder Lock
  stoppt vor jedem Quarantäne-Write.
- Promotion, Export, Filing und Disposition akzeptieren diese
  Evaluierungsautorisierung niemals als Human Approval. Ihre bestehenden Human
  Gates bleiben unverändert.
- `detect_mime_and_active_content`, Extension-/MIME-Konsistenz, 15 MB je Datei,
  25 MB je Mail, maximal fünf Dateien, Pfad-Containment, Symlink-/Reparse-Schutz,
  SHA-256-Revalidierung und atomare No-Clobber-Writes bleiben fail-closed bindend.
- **Materialität:** Jeder MD-E1-Anhang, der ausgewertet wird, um die initiale
  Mehrdeutigkeit einer unklaren Mail aufzulösen, wird über die MD-A4-Materialität
  `required_for_decision` gebunden (Handoff-Erzeugung mit
  `default_materiality: "required_for_decision"`). Teilweise oder nicht verfügbare
  erforderliche Evidenz bleibt fail-closed/Review (`blocked_on_required_attachment`)
  und wird niemals stillschweigend als bloß ergänzend (`supplementary`)
  herabgestuft.
- Extraktion und Handoff verwenden die bestehenden Limits von maximal 15.000
  Zeichen je Anhang und 30.000 Zeichen je Mail einschließlich sichtbarer
  Truncation-Marker. Der Classifier erhält nur den validierten, gekapselten
  `attachment_analysis_handoff`, nie freie Extraktionsergebnisse.
- `data/mail-desk/attachments/` bleibt flüchtig und git-ignoriert. Ein bereits
  getrackter Quarantänepfad ist eine Stop-Bedingung; FR-15 verändert keine
  Consumer-`.gitignore`-Datei autonom.

#### Blockierende Sicherheitsvoraussetzungen für MD-E1 (Teil der MD-E1-Abnahme)

Diese Punkte waren keine separaten, aufschiebbaren Tickets; sie sind mit MD-E1
implementiert, getestet und mit der System Map synchronisiert, weshalb MD-E1 als
abgenommen gilt.

1. **Lock-Legacy-Bypass geschlossen:** Der frühere Drift, dass
   `attachment_fetch.verify_workspace_lock()` `WORKSPACE_LOCK_ALLOW_LEGACY`
   auswertete und `allow_legacy` an den Guard weiterreichte (und
   `attachment_extract.py` denselben Parameter führte), ist mit MD-E1 beseitigt:
   der env-basierte Legacy-Bypass ist aus den Attachment-Fetch-/Quarantäne-Pfaden
   entfernt, und weder Runtime, Caller noch Manifest können Legacy aktivieren.
   Zulässig ist ausschließlich die vertrauenswürdige Lease-/Conversation-ID aus der
   Harness-Control-Plane. Tests belegen, dass weder Env noch Parameter-/Manifest-
   Bypass einen Schreibpfad öffnen.
2. **Produktions-Preflight gegen getrackte Quarantäne implementiert:** Der frühere
   Zustand, dass die Prüfung auf getrackte Quarantänedateien nur in Tests existierte,
   ist mit MD-E1 geschlossen. Vor dem ersten Quarantäne-Write wurzelt ein
   Produktions-Preflight am vertrauenswürdigen `workspace_root` und behandelt jede
   getrackte Datei unter `data/mail-desk/attachments/` als begrenzten Stop
   (fail-closed). Der Preflight verändert `.gitignore` nicht autonom und definiert
   ein testbares, begrenztes Fail-Closed-Verhalten ohne unsichere Shell.
3. **Receipt-Klassen-Guard:** Die unter „Receipt-Klassen-Grenze" beschriebene
   schmale, kontextbewusste Validierung ist Bestandteil von MD-E1 und mit Tests für
   jeden Human-Approval-Pfad sowie den direkten/unrelated Fetch-Pfad nachgewiesen
   (implementiert).

#### Lebenszyklus und Aufräum-Verantwortung

- MD-E1 bewahrt verifizierte Quarantäne-Artefakte und das Inventar für MD-E2 auf.
  Weder MD-E1 noch MD-E2 löschen sie stillschweigend.
- Nach erfolgreicher Neuklassifikation oder terminalem Fehler bleibt das Aufräumen
  eine ausdrückliche Aktion der integrierenden Control-Plane unter aktivem Lock
  und dem bestehenden, validierten Quarantäne-/Dispositions-Lebenszyklus. FR-15
  führt keine automatische Garbage Collection ein.

### Staged Manifest-Vertrag (MD-E1 → MD-E2)

MD-E1 gibt ein kanonisches **Zwischenergebnis** `attachment_evaluation` zurück und
schreibt/behauptet **kein** final klassifiziertes Draft-Item. Im Zwischenergebnis
sind `used_for_classification` immer `false` und `classifier_revision` immer
`null`. Das Feld `status` beschreibt ausschließlich die Auswertungsstufe, niemals
das Klassifikationsergebnis. MD-E1 installiert das Zwischenergebnis nicht in ein
persistiertes `DraftManifest`-Item.

MD-E2 installiert das Feld als genau **ein additives Feld je Draft-Item** in das
`DraftManifest`, sobald der einmalige Reklassifikationsversuch einen terminalen
Ausgang erreicht hat; MD-E1 installiert es nicht. MD-E2 ist nicht Teil des
MD-E1-Implementierungsumfangs.

`used_for_classification: true` zusammen mit einem 64-Hex-`classifier_revision`
wird **nur** gesetzt, wenn kumulativ gilt: genau **eine** tatsächliche
Neuklassifikation mit dem validierten `attachment_analysis_handoff` wurde
ausgeführt **und** ihr Ergebnis ist erfolgreich und nicht mehrdeutig **und** sie
hat die gebundenen Anhangs-Eingaben tatsächlich verwendet. Das
`classifier_revision` bindet die kanonischen Klassifikationsregeln und die
tatsächlich verwendeten Input-Hashes. In allen anderen Fällen bleiben
`used_for_classification: false` und `classifier_revision: null` — auch dann, wenn
ein Reklassifikationsversuch tatsächlich stattgefunden hat. Das umfasst
ausdrücklich `not_needed`, `skipped`, `failed`, `still_ambiguous` und „kein
Versuch". Ein ausgeführter Klassifikationsaufruf allein führt niemals zu `true`.

MD-E1-Zwischenergebnis (staged, `used_for_classification` immer `false`):

> **Notation:** Pipe-getrennte Werte wie `completed|not_needed|skipped|failed`,
> `auto_evaluated|not_applicable` oder `full|truncated` sind Notation für sich
> gegenseitig ausschließende Alternativen und **niemals** literale Laufzeitwerte.
> In spitzen Klammern gesetzte Werte wie `<64-hex>` oder `<safe-run-id>` sind
> Platzhalter, keine Literale.

```json
{
  "attachment_evaluation": {
    "status": "completed|not_needed|skipped|failed",
    "reason": "bounded_machine_code",
    "authorization": "auto_evaluated|not_applicable",
    "files": [
      {
        "filename": "source.docx",
        "sha256": "<64-hex>",
        "mime_type": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "chars": 15000,
        "coverage": "full|truncated",
        "run_id": "<safe-run-id>"
      }
    ],
    "used_for_classification": false,
    "classifier_revision": null
  }
}
```

MD-E2-finales Draft-Item (illustrativ, nur bei erfolgreicher, nicht mehrdeutiger Neuklassifikation nach genau einem Versuch):

```json
{
  "attachment_evaluation": {
    "status": "completed",
    "reason": "classification_clear",
    "authorization": "auto_evaluated",
    "files": ["... unverändert aus dem MD-E1-Zwischenergebnis ..."],
    "used_for_classification": true,
    "classifier_revision": "<64-hex>"
  }
}
```

`reason` verwendet eine begrenzte, dokumentierte Wertemenge, mindestens
`classification_clear`, `no_attachments`, `no_allowed_attachments`,
`lock_unavailable`, `policy_blocked`, `quota_exceeded`, `fetch_failed`,
`extraction_failed`, `handoff_invalid` und `still_ambiguous`. Langlebige Outputs
enthalten keine absoluten Pfade oder Rohinhalte. `classifier_revision` bindet die
kanonischen Klassifikationsregeln und tatsächlich verwendeten Input-Hashes; eine
freie Versionszeichenfolge genügt nicht.

**Status für fortbestehende Mehrdeutigkeit (verbindlich):** Die Paketkarte
definiert keinen eigenen Status für `still_ambiguous`. Es wird kein neuer Status
eingeführt; `still_ambiguous` verwendet den bestehenden Status `completed`
(`status: "completed"`, `reason: "still_ambiguous"`). `used_for_classification`
bleibt dabei `false` und `classifier_revision` `null`.

### MD-E1 — Policygebundener Evaluierungs-Orchestrator (✅ abgenommen)

**Status:** ✅ Implementiert, getestet und als Paket abgenommen (FR-15/MD-E1-T01–T07);
Evidenz in [`FEATURE-REQUEST-PROGRESS.md`](FEATURE-REQUEST-PROGRESS.md). MD-E1 endet am
validierten Handoff; Reklassifikation und `DraftManifest`-Installation gehören ausschließlich
zu MD-E2.

**Ziel:** Einen schmalen, separat testbaren `attachment_evaluate`-Orchestrator
bereitstellen, der reale MIME-Kandidaten revalidiert, die interne
Evaluierungsautorisierung deterministisch erzeugt und die bestehenden MD-A2/A3/A4-
Bausteine linear ausführt.

**Scope:** Bestehende öffentliche Funktionen aus `attachment_fetch.py`,
`attachment_policy.py`, `attachments.py`, `attachment_extract.py` und
`attachment_handoff.py` wiederverwenden. Kein eigener Downloader, MIME-Parser,
Office-Konverter, OCR-Pfad und kein zweiter Hash-/Request-Validator (Duplikat der
bestehenden Validierung). Ein schmaler, kontext-/receipt-klassenbewusster
Receipt-Guard ist dagegen ausdrücklich Teil des Scopes, weil er die unter
„Receipt-Klassen-Grenze" geforderte Autorisierungsgrenze durchsetzt. Der
Orchestrator liefert das kanonische, staged `attachment_evaluation`
(`used_for_classification: false`, `classifier_revision: null`) und den validierten
`attachment_analysis_handoff`; er klassifiziert noch nicht, installiert das Feld
nicht in ein persistiertes Draft-Item und führt keine Mailbox-, Evidence-,
Katalog-, Cloud- oder Dispositionsmutation aus.

**Blockierende Sicherheitsvoraussetzungen (Teil der MD-E1-Abnahme):** Die drei
unter „Blockierende Sicherheitsvoraussetzungen für MD-E1" genannten Punkte —
Lock-Legacy-Bypass-Schließung, Produktions-Preflight gegen getrackte Quarantäne und
der Receipt-Klassen-Guard — sind mit MD-E1 implementiert und getestet; MD-E1 ist
damit abgenommen. Sie waren keine separaten, aufschiebbaren Tickets.

**Pflichttests:** Trigger-Matrix; echte Part-Bindung; caller-seitig gefälschte
Policy-/Receipt-Werte; fehlender/fremder Lock; aktiver Inhalt; disallowed Extension;
MIME-/Extension-Drift; Einzel-/Gesamtgröße und Anzahl; idempotentes
`already_fetched`; Hash-/Inventar-/Identity-Drift; `.docx`, `.doc`, `.pdf`, `.xlsx`
und `.pptx`; Extraktions-Timeout; 15.000-/30.000-Zeichenbudgets und `truncated`;
keine Mailbox-, Promotion-, Export- oder Dispositionsoperation. Zusätzlich:

- Env- und Parameter-/Manifest-Legacy-Bypass öffnen keinen Schreibpfad;
- der Produktions-Preflight stoppt bei einer getrackten Datei unter
  `data/mail-desk/attachments/` vor dem ersten Quarantäne-Write und verändert
  `.gitignore` nicht;
- Human-Approval-Pfade (Filing, Promotion, Export, Disposition) lehnen die
  maschinelle Receipt-Klasse fail-closed ab, während typenlose menschliche
  MD-A2-Receipts unverändert funktionieren;
- der MD-E1-Pfad akzeptiert kein vom Caller/Mail/Manifest geliefertes maschinelles
  Receipt;
- das staged `attachment_evaluation` hat immer `used_for_classification: false`
  und `classifier_revision: null`;
- ausgewertete Anhänge sind über Materialität `required_for_decision` gebunden,
  und teilweise/nicht verfügbare erforderliche Evidenz bleibt fail-closed/Review.

**Abnahme:** Fokussierte MD-E1-Tests, vollständige Mail-Desk-Suite, Compileall,
Skill-/Workspace-Validierung und `git diff --check` grün. Nachweis, dass
(a) der staged Contract eingehalten wird, (b) die drei
Sicherheitsvoraussetzungen implementiert und getestet sind und (c) MD-E1 null
Mailbox-, Promotion-, Export- und Dispositionswrites ausführt. Paket, Progress-
und System-Map-Update werden gemeinsam committed.

### MD-E2 — Draft-Integration und Neuklassifikation

**Status:** ✅ Abgeschlossen; **MD-E2-T01–T04 implementiert und paketabgenommen**
(standardmäßig aktive `draft`-Verdrahtung, `--evaluate-attachments`/`--no-evaluate-attachments`,
einmalige Neuklassifikation, additive `DraftManifest`-Installation; fail-closed-Outcome-Matrix,
kanonische Handoff-Revalidierung, Code-/Katalog-/Hash-gebundene
`classifier_revision`, `still_ambiguous`-Erhalt und deterministische
`already_fetched`-Idempotenz; opt-in `inspect`-`manifest_proposal` über denselben
Item-Flow mit `manifest_file`-Grenze; T04 hermetischer Real-Pfad-Akzeptanztest). **FR-15 ist
damit geschlossen.**

**Ziel:** Nach der bestehenden Body-/Full-Read-Klassifikation nur unklare Items
über MD-E1 anreichern und exakt einmal mit dem validierten Anhangs-Handoff erneut
klassifizieren.

**Staged Übergang aus MD-E1:** MD-E2 übernimmt das staged `attachment_evaluation`
aus MD-E1 und installiert es als genau ein additives Feld je Draft-Item in das
`DraftManifest`. `used_for_classification: true` und ein 64-Hex-`classifier_revision`
setzt MD-E2 **nur** unter den oben im staged Manifest-Vertrag definierten
kumulativen Bedingungen: genau eine tatsächliche Neuklassifikation mit validiertem
Handoff, erfolgreiches und nicht mehrdeutiges Ergebnis und tatsächliche Verwendung
der gebundenen Eingaben. Für `not_needed`, `skipped`, `failed`, `still_ambiguous`,
„keine Neuklassifikation" und jeden erfolglosen oder mehrdeutigen Versuch bleiben
beide Felder `false`/`null`. MD-E2 ist **nicht** Teil des
MD-E1-Implementierungsumfangs; die Umsetzung erfolgt in einem eigenen, frisch
freigegebenen Paket nach grünem MD-E1.

**Aufräum-Verantwortung:** MD-E2 bewahrt verifizierte Quarantäne-Artefakte und
Inventar für einen späteren expliziten Cleanup auf. Ein Aufräumen ist eine
ausdrückliche Aktion der integrierenden Control-Plane unter aktivem Lock und folgt
dem bestehenden validierten Quarantäne-/Dispositions-Lebenszyklus. Weder MD-E1 noch
MD-E2 führen eine automatische Garbage Collection ein.

**Scope:** `draft` erhält die Auswertung standardmäßig aktiviert; eine explizite
CLI-Option `--evaluate-attachments` und ihr sicherer Deaktivierungsgegenpart werden
im Paket spezifiziert, ohne bestehende autonome `pipeline`-Freigaben auszuweiten.
`inspect` darf die gleiche Auswertung optional anbieten. Die zweite Klassifikation
darf `kind`, `id`, kataloggebundene Unterentscheidungen und `needs_reply` nur über
die bestehenden Classifier-Regeln neu bestimmen. Sie erfindet keine Ziele. Bei
Fehler, Teildeckung ohne ausreichende Evidenz oder fortbestehender Mehrdeutigkeit
bleiben `keep_in_folder`, `review_required: true` und niedrige Confidence erhalten.
Eine eindeutige Entscheidung erzeugt Evidence erst im bestehenden nachgelagerten,
quellengebundenen Flow; die Evaluierung selbst schreibt keine Evidence.

**Pflichttests:** Routing-relevanter Anhang macht ein unklares Item eindeutig;
unklare Mail ohne Anhang bleibt unverändert in Review; blockierter, zu großer oder
aktiver Anhang bleibt fail-closed; kein Mailbox-Write; zweiter Lauf erzeugt keinen
Doppel-Fetch; Truncation bleibt sichtbar; Anhangstext bleibt gekapseltes
`untrusted_external`; manipuliertes Handoff stoppt; Batch-Mischfall isoliert Fehler
auf das betroffene Item; bestehender klarer Draft wird nicht ausgewertet.

### Dokumentation und Abnahme

Die Dokumentation ist mit der **abgenommenen MD-E1-Implementierung** synchron
nachgezogen. Der frühere Spezifikations-Commit hatte ausschließlich den damaligen
Implementierungsdrift in der Mail-Desk-Subsystem-System-Map bereinigt und den
MD-E1-Blocker markiert; er dokumentierte bewusst noch **keine** MD-E1-Laufzeit als
bereits implementiert. Mit der MD-E1-Abnahme sind synchron aktualisiert:

- `skills/mail-desk/SKILL.md`: `attachment_evaluate`-Seam in Kernfluss Schritt 3 und
  die MD-E2-Grenze für `draft`/`inspect`-Verdrahtung und `--evaluate-attachments`;
- `skills/mail-desk/references/batch-runner.md`: `attachment_evaluation`-Schema,
  Beispiele und die explizite MD-E2-Abgrenzung;
- `skills/mail-desk/references/cli-operations.md`: kanonischer Ablauf
  Inspect → policygebundener Fetch → begrenzte Extraktion → validierter Handoff
  (Ende MD-E1) mit bounded Fehlern und MD-E2-Grenze;
- `skills/mail-desk/references/backends/himalaya.md`: Raw-MIME-/Fetch-Grenze,
  Lock-/Preflight-/Policy-Kontrollen, Extraktions-Timeout → `extraction_failed`
  und fehlender automatischer Retry/Reclassify/Write;
- L2 `skills/mail-desk/docs/system-map/{README,objects,processes,effects}.md` und
  L1 `docs/system-map/{README,objects,processes,effects}.md`: automatische
  Evaluierung, neue Autorisierungsgrenze und MD-E1-Paketabnahme;
- diese Statuszeile, die Paketkarten und `FEATURE-REQUEST-PROGRESS.md`.

**Gesamtabnahme MD-E1:** Alle oben genannten Verhaltens- und Sicherheitstests, die
vollständige entdeckte Mail-Desk-Suite, Compileall, Skill-/Workspace-Validierung und
`git diff --check` sind grün. Ein hermetischer End-to-End-Test beweist
Inspect → policygebundenen Fetch → begrenzte Extraktion → validierten Handoff für
einen klarstellenden Anhang und zugleich null Mailbox-, Promotion-, Export- und
Dispositionswrites. **MD-E1 endet am Handoff; Reclassify und
`DraftManifest`-Installation sind ausschließlich MD-E2 und werden von MD-E1 weder
ausgeführt noch behauptet.** FR-15 ist mit der abgenommenen
MD-E2-Umsetzung (T01–T04) geschlossen. FR-15 und FR-13 verändern dieselben
Classifier-/Attachment-Grenzen und dürfen nicht parallel umgesetzt werden; **MD-E2 wurde
zuerst abgeschlossen (FR-15 geschlossen), danach wurde FR-13/`MD-M1` (T01–T04) gegen diesen
abgenommenen MD-E2-Baseline implementiert und paketabgenommen; danach wurde
`MD-M2` (Quarantäne-Paketierung unter `core/quarantine/`) implementiert und
paketabgenommen.** **FR-13 ist geschlossen.**

**T02/T03-Synchronisation:** Die fail-closed-Härtung (MD-E2-T02) wurde im selben
Arbeitsschritt in `skills/mail-desk/scripts/core/attachment_reclassification.py`,
den fokussierten Tests (`test_batch_runner_mde2_hardening.py`,
`test_batch_runner_mde2_draft.py` mit kanonischen Fixtures in `mde2_fixtures.py`),
den Operator-Docs (`SKILL.md`, `references/batch-runner.md`,
`references/cli-operations.md`, `references/backends/himalaya.md`), beiden
System-Map-Ebenen und dieser Status-/Fortschrittsdatei nachgezogen. Der opt-in
`inspect`-Vorschlag (MD-E2-T03) wurde synchron in
`skills/mail-desk/scripts/core/modes/inspect.py` und dem neuen fokussierten Testmodul
`test_batch_runner_mde2_inspect.py` (12 Tests) sowie denselben Operator-Docs, beiden
System-Map-Ebenen und dieser Status-/Fortschrittsdatei nachgezogen. Die Paketabnahme
(MD-E2-T04) wurde über das neue fokussierte Testmodul
`skills/mail-desk/tests/test_batch_runner_mde2_acceptance.py` (1 Test) und dieselben
Operator-Docs, beide System-Map-Ebenen sowie diese Status-/Fortschrittsdatei synchron
nachgezogen; die Git-Index-Metrik ist auf 111 getrackte Dateien / 54 unter `scripts/`
(43 unter `scripts/core`) / 42 Testmodule / 706 Tests aktualisiert. **FR-15 ist geschlossen;
FR-13 ist mit MD-M1 (T01–T04) und MD-M2 (Quarantäne-Paketierung) abgeschlossen und geschlossen.**

---

## FR-16: Dokumentations- und Metrik-Hygiene aus der MD-M2-Retrospektive

**Status:** ⬜ Geplant. Reine Dokumentationsmaßnahme ohne Verhaltens- oder
Schnittstellenänderung. Quelle: Retrospektive des Runs
`daedalus/runs/2026-09-22-office-intelligence-fr13-md-m2` (FR-13/MD-M2).

### Problem & Motivation

Während der MD-M2-Paketierung offenbarte die System-Map- und Ledger-Pflege drei
systematische Dokumentationsrisiken, die Fehlversuche und Copy-Paste-Fehler
verursachen:

1. **Metrik-Vervielfältigung ohne SSOT:** Die Git-Index-Metrik (Dateien,
   `scripts/`-Anzahl, `scripts/core`-Anzahl, Testmodule, Testanzahl; aktuell
   128/65/55/48/804) steht in mindestens sechs Dateien (L1 `docs/system-map/README.md`
   §2, L2 `skills/mail-desk/docs/system-map/README.md` Kopfzeile, beide FR-Ledger,
   Progress-Datei). Jede Änderung ist manuelles Copy-Paste-Roulette über alle Stellen;
   im MD-M2-Run mussten sechs Stellen einzeln gefunden und synchronisiert werden, ohne
   Checkliste.
2. **Monolithische Tabellenzellen:** Die L1/L2-README-Komplexitätsmatrizen enthalten
   Zellen mit >2.000 Zeichen (L1 §2 mail-desk-Zelle, L2 §2 Quarantäne-Engine-Zelle).
   Sie widersprechen dem eigenen Anti-Context-Bloat-Ziel der System Map, sind mit
   Editier-Tools kaum atomar editierbar (im MD-M2-Run vier fehlgeschlagene Edit-Versuche
   an einer einzigen Zeile) und verschleiern die Struktur (Verantwortlichkeiten als
   Fließtext-Wand statt zitierbarer Sub-Abschnitte).
3. **Bekannter Stale-Verweis im Konsumenten-Workspace:** Daedalus'
   `memory/references/office-intelligence.md` (föderierte Referenz auf dieses Bundle)
   nennt weiterhin „257 Dateien, >489 Tests, 16 Pflichtfelder" für den Mail-Desk;
   real sind es 128 Dateien, 804 Tests, 17 Pflichtfelder. Der Verweis wurde von zwei
   unabhängigen Subagenten-Reviews als driftend gemeldet, aber nie korrigiert. Ein
   Konsumenten-Workspace, der von dieser Datei ausgeht, trifft Fehlentscheidungen.

### Ziel & Invarianten

- **DOC-M1 — Metrik-SSOT:** Die Git-Index-Metrik erhält eine einzige kanonische
  Definition pro Ebene (L1-Kopfzeile referenziert die L2-Kopfzeile oder umgekehrt;
  Ledger referenzieren „siehe System Map" statt Literalwerte, außer in
  paketabnahme-spezifischen historischen Snapshots, die bewusst eingefroren sind).
  Dazu eine Checkliste aller Metrik-Stellen (mindestens: L1 README §2, L2 README
  Kopfzeile, `FEATURE-REQUESTS.md` Statuszeilen, `FEATURE-REQUEST-PROGRESS.md`) im
  L1-README §3 (Navigationsmatrix), damit jede künftige Metrik-Änderung deterministisch
  nachvollziehbar ist.
- **DOC-M2 — Zellen-Splitting:** Die >2.000-Zeichen-Zellen der L1- und L2-Komplexitätsmatrizen
  werden gekappt: Die Tabellenzelle behält maximal einen Kurzstatus (≤ 200 Zeichen) plus
  Link auf einen eigenen Sub-Abschnitt im selben Dokument, der die Verantwortlichkeiten
  strukturiert (ggf. eigene Liste statt Fließtext). Keine Informationslöschung —
  Umstrukturierung mit identischem Informationsgehalt, zitierfähigen Ankern.
- **DOC-M3 — Stale-Reference-Fix:** Korrektur der Daedalus-seitigen
  `memory/references/office-intelligence.md` auf die realen Werte
  (128/804/17); da diese Datei außerhalb dieses Repositories liegt, wird sie als
  Orchestrator-Aktion im Daedalus-Workspace durchgeführt und hier nur als erledigt
  vermerkt (Föderations-Disziplin: Quell-Map ist SSOT, Referenz folgt).

### Abnahme

- Alle Metrik-Stellen zeigen identische Werte; die Stellen-Checkliste existiert und
  deckt alle Vorkommnisse ab (per `git grep` über die Zahlen nachweisbar).
- Keine Tabellenzelle der L1/L2-Komplexitätsmatrizen überschreitet ~200 Zeichen
  Status plus Link.
- Die Daedalus-Referenz zeigt 128/804/17; nachweislich keine „257/>489/16"-Vorkommnisse
  mehr.
- Dokumentations-Validierung, Skill-Katalog, Workspace-Validator und
  `git diff --check` grün; keine Code- oder Teständerungen.

### Out of Scope

- Jede inhaltliche Neuschreibung von Verantwortlichkeitsbeschreibungen.
- Änderungen an Test- oder Produktionscode.
- Automatische Metrik-Generierung per Skript (möglicher künftiger FR).

---

## FR-17: Routing-Katalogtreue, Batch-Determinismus und Vertragshygiene

**Status:** ⬜ Geplant. Ergebnis einer produktiven Testbatch des konsumierenden
BOKU-Workspace (10 Mails, Envelope 9387–9404, Account `BOKU-MARTIN`, 2026-09-22).
Alle Befunde wurden mit drei identischen `draft`-Läufen und zusätzlich offline
gegen die echten Kataloge reproduziert. Es ist eine **verhaltensändernde
Korrektur im Classifier-/Routing-Pfad** plus begleitende Vertrags- und
Hygiene-Fixes. Keine Mailbox-Mutation, keine Promotion, kein Cloud-/Task-Pfad.

### Reproduktionsumfeld

- Konsumierender Workspace mit Backend-Bindung `.agents/mail-desk-backend.json`
  (`schema_version: 1`, `backend: himalaya`, `account: BOKU-MARTIN`), also der
  kanonische MD-H3-Pfad; `--account BOKU-MARTIN` stimmte exakt überein.
- Aufruf (dreimal identisch, jeweils mit gültigem Workspace-Lock und
  `WORKSPACE_LOCK_LEASE_ID` aus der Harness-Control-Plane):
  `mail_desk_batch_runner.py --draft 10 --order oldest --folder INBOX --skip-known
  --data-dir data/mail-desk --account BOKU-MARTIN`.
- Wirkung: ausschließlich read-only Mailboxzugriffe plus zwei MD-E1-Quarantäne-Fetches;
  **keine** Mailbox-, Index-, Log-, Evidence- oder Execute-Mutation.
- Kandidaten: Envelope 9387–9393 und 9400/9403/9404 aus `INBOX` (chronologisch,
  `skip_known` übersprang bereits indizierte ältere Mails).

### Befunde

| ID | Schwere | Befund | Kern-Evidenz |
|---|---|---|---|
| B-1 | hoch | `routing_priority` ist ein toter Katalogwert; Katalogreihenfolge entscheidet, ein reiner Kontakt-Treffer schlägt einen Exaktcode im Betreff | Env 9388 → `usage-ng` statt `atael`, offline reproduziert |
| B-2 | hoch | Topic-`do_not_route_if` wird nie ausgewertet; Newsletter landen trotz Ausschluss im Topic | Env 9400 → `netzwerke`/`eu-projekte-und-oead`, offline reproduziert |
| B-3 | hoch | `draft` ist unter transienten MIME-Timeouts nicht deterministisch; Outcome kippt zwischen Klassifikation und Review+Fetch | 3 Läufe, 2 verschiedene Manifeste (9392/9393 kippen) |
| B-4 | mittel | `notes` und Anhangs-Felder widersprechen der finalen Entscheidung | `unknown` + Notes „Themenbezogene Zuordnung…“ + leeres Inventar + befüllte `files[]` |
| B-5 | mittel | Automatische Auswertung fetcht Inline-Signaturbilder als `required_for_decision` | 2 Fetches mit `is_inline: true`, `chars: 0`, beide `still_ambiguous` |
| B-6 | niedrig | `keep_in_folder` fehlt in der dokumentierten `action.type`-Menge | produktiv genutzt an 6 Code-Stellen |
| B-7 | niedrig | `progress_*.tmp` bleiben als untracked Rauschen liegen | 2 Dateien nach 3 Läufen, nicht git-ignoriert |
| B-8 | hoch | Client-Suche ohne Ordnerliste hat keinen begrenzten Gesamt-Timeout; Hänger blockiert danach ~10 min auch Einzelabfragen | Suche >5 min ohne Fehler; danach `himalaya_timeout` bei `envelope list -s 1` |
| B-9 | mittel | Standalone-`verify` kann bei gemischten Batches (Projekt/Topic + Archiv) den Handoff nicht freigeben | zwei reale Läufe: Scope 10 und Scope 9 beide `not_required` |
| B-10 | mittel | `verify`-Evidence-Fallback globt `memory/references/**/evidence` (im Consumer leer) → `in_evidence: None`, Evidence wird nicht geprüft | 10/10 „consistent“ bei 0 gefundenen Evidence-Dateien |

#### B-1 — `routing_priority` ist wirkungslos; Katalogreihenfolge und Kontakttreffer dominieren

- `routing_priority` wird in `skills/mail-desk/scripts/**` **nirgends** gelesen
  (Ripgrep über den gesamten Skriptbaum: 0 Treffer), obwohl beide Kataloge das
  Feld pflegen (`projects.json`: `week` 80, `li4lam` 75, `atael` 65, `rellde` 65,
  `meshe`/`evolve`/`usage-ng` 50, abgeschlossene 40; `topics.json`:
  `netzwerke` 60).
- `core/matching/project_matching.py::select_project_match` iteriert die
  **Katalogreihenfolge** und gibt den ersten Treffer zurück. Stufe 1a (ID/`kuerzel`/
  Alias im Betreff) wäre für `atael` erfolgreich (Betreff-Token `ATAEL`), wird aber
  nie erreicht, weil `usage-ng` an Katalogindex 3 vor `atael` (Index 5) steht und in
  Stufe 1c bereits über einen reinen Kontakt-Treffer mit `confidence: high` gewinnt.
- Offline-Reproduktion mit den echten Katalogen (ohne Mailboxzugriff, nur
  `memory/references/{projects,topics}/*.json` des Consumer-Workspace):
  - Betreff `WG: For Action - ATAEL - 101323118 - GAP-101323118 - Evaluation results  FYI`,
    `parties` enthält `sybille.michaelis@tum.de` → `{"id": "usage-ng", "confidence": "high"}`.
  - Identischer Betreff **ohne** TUM-Kontakt → `{"id": "atael", "confidence": "high",
    "folder": "Projekte/In Ausarbeitung/ATAEL"}`.
- Reale Folge: Env 9388 (`message_id: 52270af9e92f47e2a4c9a2a124fa6266@tum.de`) wurde als
  `decision.kind: project`, `id: usage-ng` gedraftet, obwohl Betreff, Anhänge
  (`Rejection decision Information Letter.pdf`, sha256
  `932f9e6f9bb9ce30ca48b6a193336f1397c34e1f378eec771d5a11b657f68818`;
  `101323118_ATAEL_ESR.pdf`, sha256
  `be92b098a8263fff4697f6a23b0c880f3363aaa51c697deeac3b18ff12057717`)
  und Katalog eindeutig `atael` sind.
- Sibling-Split: Env 9387 (`message_id:
  734474456.1162619.1784818593223@wlldb00951.cc.cec.eu.int`, Betreff
  `For Information - ATAEL - …`) bleibt `unknown/unclassified` in `INBOX`, weil der
  Absender `EC-NO-REPLY-GRANT-MANAGEMENT@…` über `do_not_route_if: ['newsletter','no-reply']`
  jedes Projekt-Routing unterdrückt. Derselbe Vorgang landet damit potenziell in zwei
  verschiedenen Zielen, ohne dass der Reviewer den unterdrückten Kandidaten sieht.

#### B-2 — Topic-`do_not_route_if` ist nicht implementiert

- `do_not_route_if` existiert ausschließlich in `core/matching/project_matching.py`
  (Prüfblock vor der Match-Schleife). `core/matching/topic_matching.py` hat keine
  entsprechende Behandlung; ein Grep über `core/**` findet `do_not_route` nur im
  Projekt-Matcher.
- `topics.json` → `netzwerke.do_not_route_if: ['newsletter', 'no-reply']` ist damit
  wirkungslos.
- Offline-Reproduktion: Betreff `OeAD / Hochschule International Newsletter 7/2026`
  → `netzwerke` (`high`) mit `preselected_subtopic: eu-projekte-und-oead`, obwohl
  der Betreff das DNR-Signal `Newsletter` enthält.
- Reale Folge: Env 9400 (`message_id:
  838d70b285107ce4b6b19c334.14d8e54149.20260728071005.e7b64fd196.86f35121@mail141.atl271.mcdlv.net`)
  wurde nach `Themen/Netzwerke` gedraftet.
- Zielkonflikt: Der konsumierende Workspace führt externe Newsletter nach
  `pipelines/mail-desk-batch.md` → `Newsletter` (dort liegt z. B. Env 9377,
  `message_id: d112dc7be456467a80d59d29d2c7a32a@1017`, `decision.kind: archive`,
  final `Newsletter`-Envelope 77). Eine Mailbox-Suche nach `Hochschule International`
  über `Newsletter`, `Themen/Netzwerke` und `INBOX` fand **keine** früheren
  OeAD-Ausgaben (nur `INBOX` 9585); der Konflikt ist also nicht durch Historie
  entschieden und wiederholt sich je Ausgabe.
- Kein Katalog kennt ein Ziel `Newsletter` (`topics.json` hat keinen
  `newsletter`-Eintrag); der Pfad ist ein Sonderfall außerhalb der
  `references/folder-rules.md`-Routingtabelle.

#### B-3 — Nicht-Determinismus bei transienten MIME-Timeouts

- Drei identische Läufe, Ergebnis: Lauf 1 und Lauf 3 sind byte-identisch
  (`review.execute_request_sha256:
  75ae9c41456aa3746c765f413b6aa45d71c7a0e7af3c8af18d3939515121f465`), Lauf 2 weicht
  ab (`55bf1bc2b963fc3115c2cb472c39381e875aba8d76440abe92aaca7813e12891`).
- Gekippte Items (jeweils gleicher Input, gleicher Mailboxzustand):

  | Env | Message-ID | Lauf 1/3 | Lauf 2 |
  |---|---|---|---|
  | 9392 | `6a637340020000f1000d514d@gwia1.boku.ac.at` | `netzwerke/medium`, `not_needed/classification_clear` | `unclassified/low`, `completed/still_ambiguous`, `attachment_status: attachment_inventory_unavailable` |
  | 9393 | `7fd93e10-a0f2-4759-8383-89a444245a96@eucen.eu` | `unclassified/low`, `completed/still_ambiguous` | `netzwerke/medium`, Subtopic `eucen`, `not_needed/classification_clear` |

- Fehlertext des Review-Zweigs: `attachment_error: "Failed to export or inspect
  message MIME: Himalaya timed out; refusing to retry the mailbox command."`
- Mechanik: Der primäre MIME-Export (`inspect_attachments`) läuft transient in einen
  Timeout; `core/classifier.py` erzwingt daraus Review
  (`attachment_inventory_unavailable`, ~Zeile 646). MD-E2 startet danach dennoch
  `attachment_evaluate`, dessen eigener MIME-Abruf im selben Lauf gelingt → es
  entsteht ein Quarantäne-Fetch, der die Ausgangsmehrdeutigkeit nicht auflöst.

#### B-4 — Widersprüchliche Item-Felder und irreführende `notes`

- Gleichzeitig im selben Item: `attachments: []`,
  `attachment_status: "attachment_inventory_unavailable"`, `attachment_error` gesetzt
  **und** `attachment_evaluation.status: "completed"`, `reason: "still_ambiguous"`,
  `authorization: "auto_evaluated"` mit befülltem `files[]` auf eine real gefetchte
  Datei (Lauf 1 Env 9393; Lauf 2 Env 9392).
- `notes` behaupten eine Zuordnung („Themenbezogene Zuordnung zu Netzwerke
  (Betreff: …)“), obwohl `decision.kind: "unknown"`/`id: "unclassified"` bleibt.
  Ein Reviewer kann nicht erkennen, welche Aussage gilt.

#### B-5 — Inline-Signaturbilder werden als `required_for_decision` gefetcht

- Env 9393: `35-years-signature_XS.png`, sha256
  `b2e14e6fb0d7cb3a9c36e350f587ecad6ea1a75a97d33afa5134ba1e0fdd561a`,
  `is_inline: true`, `content_disposition: inline`, `chars: 0` → Quarantäne-Run
  `eval_68ef4d21fded25ab8527843bf253bad2`.
- Env 9392: `IMAGE.png`, sha256
  `8fb95ec6029dfa51c618a5a4519bb4d656cd31e4f32d35e143b822b740a98af3`,
  `is_inline: true`, `chars: 0` → Quarantäne-Run
  `eval_b12670e2d7acf37dfbcb5ab1105571a4` (Lauf 2).
- Beide Auswertungen endeten `still_ambiguous`, die Mails blieben in Review.
  Nutzen: keiner; Nebenwirkung: Quarantäne-/PII-/Quoten-Fläche und ein zusätzlicher
  fehleranfälliger MIME-Abruf.

#### B-6 — `keep_in_folder` fehlt in der dokumentierten Vertragsmenge

- Produktiv genutzt: `core/classifier.py` (Vorlage, Zielwahl, Review-Zweig),
  `core/attachment_reclassification.py`, `core/quarantine/attachment_handoff.py`,
  `core/modes/execute.py`.
- Dokumentiert: `references/batch-runner.md` (execute-Schema) führt `action.type`
  als `["copy_as_move", "move", "copy", "none", "archive"]`;
  `references/folder-rules.md` beschreibt die Retention-in-`INBOX`-Semantik nicht.
  Die Dokumentation ist damit enger als der akzeptierte Vertrag.

#### B-7 — `progress_*.tmp` bleiben liegen

- Nach den Läufen blieben `data/mail-desk/progress_1wv8jnx_.tmp` und
  `progress_vana2exn.tmp` zurück (in der Vor-Batch ebenfalls drei Dateien). Sie sind
  nicht git-ignoriert (`.gitignore` des Consumer-Workspace enthält nur
  `/data/mail-desk/attachments/`) und erzeugen untracked Rauschen in einem
  versionierten Verzeichnis.

#### Ausdrücklich verifiziert und zu erhalten (kein Fixbedarf)

- **Run-ID-Determinismus:** `core/attachment_reclassification.py::derive_evaluation_run_id`
  (bindet Account, Folder, Envelope-ID und normalisierte Message-ID) ist
  deterministisch und PII-frei; die offline nachgerechneten Werte stimmen exakt mit
  den beobachteten `files[].run_id` überein (Env 9393 →
  `eval_68ef4d21fded25ab8527843bf253bad2`; Env 9392 →
  `eval_b12670e2d7acf37dfbcb5ab1105571a4`).
- **Kein Doppel-Fetch:** Die erneute Auswertung von Env 9393 in Lauf 3 nutzte
  denselben Quarantäne-Run; `35-years-signature_XS.png` behielt seine mtime
  (14:27:26), nur `.quarantine-inventory.json` wurde neu geschrieben (14:36:15).
  Das FR-15/MD-E2-Idempotenzversprechen hält.
- **Quarantäne-Preflight erfüllt:** `git ls-files data/mail-desk/attachments/` ist
  leer; `.gitignore` enthält `/data/mail-desk/attachments/`.
- **Draft ohne Mailbox-Mutation**, Lock-Ownership und Backend-Bindung wirksam.

### Ziel & Invarianten

- Routing wird **katalogtreu und deterministisch**: Priorität, Exaktcodes,
  Kontakt-/Domänensignale und Ausschlüsse folgen einer dokumentierten, testbaren
  Ordnung; gleicher Input und gleicher Mailboxzustand ergeben dasselbe Manifest.
- Exaktcode-/Betrefftreffer schlagen reine Kontakt-/Domänentreffer; `routing_priority`
  ist die dokumentierte erste Ordnungsinstanz, die Katalogreihenfolge nur der
  stabile Gleichstands-Tiebreaker.
- `do_not_route_if` gilt für **Projekte und Topics** mit identischer, begrenzter
  Semantik; unterdrückte Kandidaten bleiben als Review-Hinweis sichtbar.
- Keine Abschwächung der FR-15-Grenzen: null neue Schreibpfade,
  `allow_legacy=False` unverändert, Receipt-Klassen-Guard unverändert, keine
  Doppel-Fetches, keine automatische Garbage Collection.
- Vertrags- und Schemaänderungen werden synchron in `SKILL.md`, `references/*`,
  L1/L2-System-Map und beiden FR-Ledgern nachgeführt (Bundle-`AGENTS.md`-Regel).
- Änderungen an `classifier.py`/`core/matching/*` rotieren `classifier_revision`
  genau einmal (genehmigt, wie bei FR-13/MD-M1); betroffene Tests und
  Dokumentationen werden mitgezogen.

### MD-R1 — Prioritäts- und Ausschlusslogik für Projekte und Topics

**Scope:**
1. `core/matching/project_matching.py`: `routing_priority` aus dem Katalog anwenden
   (numerisch, höher zuerst); Gleichstand und fehlender Wert fallen auf die stabile
   Katalogreihenfolge zurück. Innerhalb der Kandidatenbewertung müssen exakte
   ID-/`kuerzel`-/Alias-/`typical_subject_patterns`-Treffer im Betreff einen reinen
   Kontakt-/Domänentreffer eines anderen Projekts schlagen (Signalgüte vor
   Katalogreihenfolge). Die bestehende Treffersemantik (Schwellen, Confidence,
   `do_not_route_if`) bleibt erhalten.
2. `core/matching/topic_matching.py`: `do_not_route_if` mit derselben Semantik und
   denselben Prädikaten wie im Projekt-Matcher anwenden; die Prädikatlogik in einen
   gemeinsamen, testbaren Helper ziehen (kein Duplikat). `newsletter` darf dabei
   **nur** über Betreff-/Header-Signale greifen, nicht über beliebigen Body-Text
   (sonst unterdrückt jedes beiläufige Wort „Newsletter“ ein Topic).
3. Unterdrückte Kandidaten sichtbar machen: Wenn alle Projekt-/Topic-Treffer durch
   DNR entfallen, entsteht kein stilles `unknown`, sondern ein begrenzter
   Review-Grund plus `decision.suppressed_candidates[]` (ausschließlich
   Katalogdaten: `kind`, `id`, `suppression_reason`; niemals Mailinhalt als Ziel).
4. Newsletter-Zielpfad explizit definieren: Ein durch `newsletter` unterdrücktes
   Topic/Projekt wird deterministisch auf den bestehenden `Newsletter`-Pfad
   abgebildet (Archiv-/Massenmail-Semantik, `copy_as_move`) **oder** bleibt mit
   Review-Grund in `INBOX`. Die gewählte Regel wird in
   `references/folder-rules.md` und in der Consumer-Pipeline dokumentiert; der
   Katalog erhält – falls nötig – einen kanonischen Ziel-Eintrag statt eines
   Sonderpfads.

**Pflichttests:** Prioritätsordnung inkl. Gleichstand und fehlendem Wert; Exaktcode
schlägt Kontakt; 9388-Analogon endet bei `atael`; 9387-Analogon erhält Review mit
`suppressed_candidates`; 9400-Analogon endet nicht in `netzwerke`; DNR-Prädikate
inkl. Body-Regressionsfall; Newsletter-Zielpfad deterministisch; bestehende
Mail-Desk-Suite ohne unbegründete Assertion-Brüche.

### MD-R2 — Thread-/Sibling-Kohärenz

**Scope:** Forward-/Referenzketten (`references`, `in_reply_to`) mit identischem,
exakt aufgelöstem Katalogcode dürfen nicht in unterschiedliche Projekte laufen.
Trägt die Klassifikation eines Siblings einen eindeutigen Projektcode, erhält der
blockierte Partner mindestens einen Review-Hinweis mit demselben Kandidaten. Ein
automatisches Umrouten des blockierten Originals ist nur zulässig, wenn der Kandidat
exakt, eindeutig und nicht DNR-verboten ist. Keine Zielableitung aus Mailinhalt;
`candidates`-Semantik bei Mehrdeutigkeit bleibt erhalten.

**Pflichttests:** 9387/9388-Paar; Kette mit unbekanntem Sibling; widersprüchliche
Kandidaten bleiben `candidates`; DNR bleibt wirksam; keine Routing-Änderung ohne
eindeutigen Katalogcode.

### MD-R3 — Deterministische MIME-Inventarkette und Feldkonsistenz

**Scope:**
1. Innerhalb eines Laufs genau **einen** kanonischen MIME-Abruf je Item verwenden;
   Classifier und MD-E2 dürfen nicht zwei unterschiedlich getimte Exporte sehen. Ein
   transienter Timeout führt zu einem definierten, wiederholbaren Ergebnis (Review
   mit `attachment_inventory_unavailable`), ohne im selben Lauf nachzulagern.
2. `attachments[]`, `attachment_status`, `attachment_error` und
   `attachment_evaluation` müssen zueinander konsistent sein. Ein befülltes
   `files[]` mit real gefetchter Datei bei gleichzeitigem
   `attachment_inventory_unavailable`/leerem Inventar ist unzulässig; entweder wird
   das Inventar vorher erneut aufgelöst oder das MD-E2-Ergebnis verworfen.
3. `notes` werden ausschließlich aus der **finalen** Entscheidung erzeugt; keine
   Zuordnungsformulierung bei `unknown`/`unclassified`.
4. Die FR-15/MD-E2-Idempotenz (deterministischer Run, kein Doppel-Fetch) bleibt
   erhalten und wird um den Timeout-Fall erweitert (zweiter Lauf erreicht denselben
   Run).

**Pflichttests:** Timeout-Injektion beim ersten vs. zweiten Export; drei Läufe
ergeben ein identisches Manifest; Feldkonsistenz-Assertions; Notes-Konsistenz;
Quarantäne-Reuse ohne Neu-Fetch.

### MD-R4 — Inline-Bild-Policy für die automatische Auswertung

**Scope:** Inline-Parts (`content_disposition: inline` mit `content_id`) und
Bild-MIME-Typen ohne extrahierbaren Text sind **kein**
`required_for_decision`-Trigger der automatischen Auswertung. Sie bleiben
Inventar-Metadaten; ein Fetch erfolgt nur über den bestehenden, menschlich
freigegebenen MD-A2-Pfad. Die Regel ist eng zu fassen, zu begründen (PII, Quoten,
Signalarmut) und verändert die MD-A1/MD-A2/MD-A5-Verträge nicht.

**Pflichttests:** 9393/9392-Analoga werden nicht automatisch gefetcht; echte
routing-relevante Anhänge (Office/PDF mit Extrakt) lösen weiter aus;
`chars: 0`-Grenzfälle; Human-Approval-Pfad unverändert.

### MD-R5 — Vertragsdokumentation und Laufzeit-Hygiene

**Scope:**
1. `keep_in_folder` in `references/batch-runner.md` (Schema-Enum plus Semantik
   „verbleibt im Quellordner, kein Mailbox-Write“) und in
   `references/folder-rules.md` dokumentieren; ein Contract-Test stellt sicher,
   dass die dokumentierte Menge exakt der akzeptierten Menge entspricht.
2. Progress-Temp-Hygiene: `core/progress.py` bzw. der Runner räumen eigene
   `progress_*.tmp` in jedem Endzustand (success/failed/aborted) auf oder legen sie
   unter einen git-ignorierten Unterpfad; ein Test deckt die Abbruchpfade ab.
3. Consumer-Abstimmung (Dokumentation, kein Code): Newsletter-Regel der
   BOKU-Pipeline und `folder-rules.md`/Katalog auf **eine** Quelle der Wahrheit
   bringen; eine Abweichung ist ein Review-Grund, kein stiller Sonderfall.

**Pflichttests:** Enum-/Doku-Contract-Test; Temp-Aufräumen nach Erfolg, Fehler und
Abbruch; keine neue Ignore-Regel nötig, oder die Ignore-Ergänzung ist dokumentiert
und getestet.

### Gemeinsame Abnahme (FR-17)

- Fokussierte Tests je Paket, vollständige Mail-Desk-Suite, `compileall`,
  Skill-Katalog-/Workspace-Validierung und `git diff --check` grün.
- Reproduktionsnachweis: Das 10-Mail-Manifest der Testbatch ist nach `MD-R1`–`MD-R4`
  über **drei** identische `draft`-Läufe byte-identisch; `9388 → atael`,
  `9387 → Review mit ATAEL-Kandidat`, `9400 → definierter Newsletter-Pfad`,
  `9392/9393 → deterministischer, konsistenter Endzustand ohne automatischen
  Inline-PNG-Fetch`.
- Nachweis, dass keine neuen Schreibpfade, keine Legacy-Lock-Bypässe und kein
  Doppel-Fetch entstehen; die FR-15-Tests bleiben grün (Ausnahme: die bewusste
  MD-R4-Inline-Policy-Erweiterung).
- Synchronisation: `SKILL.md`, `references/*`, L1/L2-System-Map,
  `FEATURE-REQUESTS.md`, `FEATURE-REQUEST-PROGRESS.md` konsistent;
  `classifier_revision`-Rotation dokumentiert.

### Ausführungsprofil und Reihenfolge

- `MD-R1` und `MD-R2` ändern dieselben Classifier-/Matching-Dateien und werden
  **nicht parallel** umgesetzt; `MD-R2` startet erst nach grünem `MD-R1`.
- `MD-R3` und `MD-R4` berühren die abgeschlossene FR-15-Fläche
  (`attachment_reclassification.py`, `attachment_evaluation.py`, Policy) und werden
  ebenfalls seriell umgesetzt; keine parallelen Writer auf denselben Dateien.
- `MD-R5` ist unabhängig und kann jederzeit laufen (reine Dokumentations-/
  Hygieneänderung), darf aber `MD-R3`-Temp-Pfade nicht überschreiben.
- Jedes Paket: frische Session, Tests zuerst, kleinste Implementierung, fokussierte
  Tests, Gesamtsuite, unabhängiges Review, dann ein Commit.

### Nachtrag 2026-09-22 — Befunde aus der Execute-/Verify-Phase derselben Testbatch

Nach der Review-Korrektur wurden dieselben 10 Mails real ausgeführt: `execute`
10/10 erfolgreich, `verify` 10/10 konsistent, `reconcile` (read-only) `completed`
mit quellengebundenem Handoff (9 Items). Dabei kamen drei weitere, reproduzierbare
Befunde hinzu.

#### B-8 — Client-Suche ohne Ordnerliste hat keinen begrenzten Gesamt-Timeout

- Aufruf: `mail_desk_himalaya_client.py --input …` mit
  `{"action": "search", "query": "Besuch von Univ. Kenia"}` **ohne** `folders`.
- Ergebnis: Lauf hing >5 Minuten (Abbruch durch die aufrufende Shell); die
  Ausgabedatei blieb 0 Bytes, es gab keinen strukturierten Timeout-Fehler.
- Folge: Rund 10 Minuten lang scheiterten danach auch einfache Abfragen
  (`execute`-Readiness-Preflight mit `envelope list -s 1` sowie ein direktes
  `list_envelopes`) mit `himalaya_timeout`, obwohl TCP auf dem IMAP-Endpunkt
  erreichbar war; nach ca. 2 Minuten Wartezeit antwortete der Server wieder in 0,9 s.
- Vermutete Ursache: viele parallele Ordnerabfragen ohne Gesamt-Deadline; eine
  serverseitige Session-Limitierung wirkt danach kurzzeitig nach. Der Batch selbst
  blieb fail-closed (Readiness stoppte vor jeder Mutation).
- Anforderung: begrenzter Gesamt-Timeout (Deadline) für `search` und alle
  Client-Operationen; Hänger enden als `himalaya_timeout`, nie als stiller Hänger;
  optional `folders` verpflichtend machen oder den Hinweis dokumentieren; kein Retry.

#### B-9 — Standalone-`verify` kann bei gemischten Batches keinen Handoff freigeben

- `_verified_execute_candidate` (`core/modes/verify.py`) verlangt gleichzeitig
  `result_ids == verify_scope_ids` **und** bei `pending`-Kandidat
  `candidate_ids == verify_scope_ids`.
- Der `synthesis_candidate` enthält konstruktiv nur `project`/`topic`-Items. Ein
  Batch mit Archiv-/Newsletter-Item (hier Env 9400, `kind: archive`) besitzt daher
  keine Scope-Menge, die beide Gleichheiten erfüllt:
  - Scope = alle 10 Items → Kandidat 9 ≠ 10 → `synthesis_handoff: not_required`,
    kein `completion_report`.
  - Scope = Kandidat 9 Items → `results` 10 ≠ 9 → ebenfalls `not_required`.
- Beide Läufe sind im Consumer-Archiv belegt
  (`2026-09-22-batch-1-batch-verify.json` und
  `2026-09-22-batch-1-batch-verify-candidate-scope.json`). Nur `reconcile` — bzw.
  der interne `pipeline`-Pfad, der diesen Gate nicht durchläuft — setzt den Handoff
  auf `pending` (9 Items, `completion_report: completed`).
- Zusätzlich: Der Runner-Envelope trägt im `data`-Objekt kein `mode`/`ok`; ein
  gespeichertes Execute-Ergebnis wird daher auch über `batch_file` nicht als
  Provenienz akzeptiert (`batch_data.get("mode") == "execute"`).
- Anforderung: Teilmengen-Semantik (`candidate_ids ⊆ verify_scope_ids ==
  result_ids`) oder explizite, dokumentierte Scope-Bildung; Tests für gemischte
  Batches; der Runner-Envelope soll als Provenienz nutzbar sein oder seine exakt
  erwartete Form ist zu dokumentieren.

**Paket-Zuordnung der Nachträge:** B-8 → neues Paket `MD-R6` (Client-Deadlines und
Hänger-Freiheit), B-9 und B-10 → neues Paket `MD-R7` (Standalone-Verify-Provenienz,
Scope-Bildung und Evidence-Prüfung). Beide Pakete sind unabhängig von `MD-R1`–
`MD-R5` (keine gemeinsamen Dateien mit `MD-R1`/`MD-R2`; `MD-R7` berührt `verify.py`)
und werden in die gemeinsame Abnahme aufgenommen.

#### B-10 — `verify`-Evidence-Fallback prüft einen leeren Root und kann nicht `False` melden

- Ohne explizite `evidence`-Spezifikation (z. B. bei reinen `message_ids`) globt
  `core/modes/verify.py` `memory/references/**/evidence/*.md` und liefert
  `in_evidence = None`, wenn nichts gefunden wird.
- Im consumerenden Workspace existiert unter `memory/references/` **kein**
  `evidence`-Ordner (0 Dateien); die kanonische Evidenz liegt unter
  `memory/evidence/**`.
- Folge: Ein `verify` mit reinen Message-IDs meldete 10/10 „consistent“, ohne die
  Evidence-Ebene zu prüfen (`in_evidence: None` für alle Items). Die
  `reconcile`-Referenz führte denselben Batch anschließend korrekt mit `ev: True`.
- Anforderung: Fallback an das kanonische Evidenz-Layout angleichen (oder eine
  explizite `evidence`-Map verlangen); fehlende kanonische Evidenz muss `False`
  ergeben und `consistent` beeinflussen; Test.

### Out of Scope

- Änderung realer Mailbox-Ordner oder Umlabeln bereits gerouteter Mails.
- Neue Katalogfelder jenseits der vorhandenen `routing_priority`/`do_not_route_if`.
- Promotions-, Cloud-, Task- oder Dispositionspfade (FR-09/FR-10/FR-16 bleiben
  unberührt); `attachment_evaluation` bleibt ohne Promotion-/Export-Seiteneffekt.
- Automatische Bereinigung bestehender Quarantäne-Runs (weiterhin explizite
  Control-Plane-Aktion).
