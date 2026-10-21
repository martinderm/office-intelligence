# Issue #10 — begrenzte Suite-Flake-Diagnose

## Stand und Scope

Basis: frisch gefetchtes `origin/main` `86f49f4` (einschließlich #25/#16),
eigene Lane `fix/issue-10-stability`. Full-Auto ist nur für diese Lane
autorisiert; kein Merge, keine Live-Mail-/SaaS-/Consumer-Mutation. Der einzige
Code-Diff betrifft die MD-A3-Test-Fixture, nicht die Quarantäne-Implementierung.
Risiko konservativ **high**, weil der sicherheitsrelevante Prüfstand betroffen ist.

## Pflicht-Erstläufe und Umgebung

Vor jedem Eingriff wurden `test_maildesk_attachments_mda3.py` und
`test_maildesk_attachment_evaluation_mde1.py` isoliert mit `unittest discover`
ausgeführt. Die ersten Frames waren **rot**: 37 Tests / 1 Error bzw. 112 Tests /
76 Errors (`Canonical workspace-lock guard is unavailable`). Das sind keine
READY-Reproduktionen. Die Original-Logs bleiben im lokalen Run erhalten.

Der Guard-Loader sucht Vorfahren von Quelldatei und CWD nach dem Shared-Sibling
`workspace-lock/scripts/workspace_lock_guard.py`. Ein externer Worktree hat diesen
Sibling nicht. Mit dem echten Shared-Skills-Verzeichnis als CWD und absolutem
Testpfad waren dieselben Module unverändert **37 bzw. 112 Tests grün**.
Kein Guard wurde kopiert und kein Produktpfad angepasst. Andere Repo-Tests
verwenden feste relative Sibling-Pfade; ihre Vollprüfung benötigt einen
entsprechend provisionierten Layout-Snapshot, nicht nur einen anderen CWD.

## Reproduktion und minimaler Fix

Der Issue-Kommentar beschreibt einen weiterhin vorhandenen globalen Marker:
`tempfile.gettempdir()/flaky_lock_marker_mda3.txt`. Die Fixture erlaubt den
ersten Lock-Check und verwirft den zweiten vor Promotion. Ein anderer oder
abgebrochener Testlauf konnte denselben Marker erzeugen oder konsumieren.

Die neue Regression verschachtelt zwei logische Test-Workspaces ohne parallele
Last: erster Check A, erster Check B, zweiter Check A, zweiter Check B.
Das System-Temp-Verzeichnis wird dabei ausschließlich für diese synchrone Probe
auf deren privates `TemporaryDirectory` umgebogen; fremder Temp-Zustand bleibt
unangetastet. **Echtes RED vor dem Fix:** Schon der erste Check B wirft
`WorkspaceLockError`, weil er A's Marker konsumiert.

Die picklable Fixture verwendet jetzt ausschließlich
`data_dir/.lock-loss-marker-mda3` im jeweiligen privaten Test-Workspace.
Die Regression verbietet zusätzlich jeden Aufruf von `gettempdir()`.
Der bestehende End-to-End-Test behält echten Spawn und OCR bei und prüft
Lockverlust, fehlende Promotion, Temp-Cleanup, verbrauchten Marker sowie
unveränderte Quelldaten. **GREEN:** Regression und End-to-End-Pfad bestanden.
Der direkte Fixture-Test fängt Zustandsvermischung ab, nicht Prozess-/OS-Timing;
der bestehende Extraktionstest deckt zusätzlich die reale Prozessgrenze ab.

## READY und Timing: bewusste Diagnosegrenze

Die getrennten Hypothesen waren globaler Fixture-Zustand, Spawn-/Import-Latenz
vor READY und Wallclock-Messung inklusive Start/Cleanup. Nur die erste wurde
als Fehler deterministisch reproduziert und minimal behoben.

Quelllektüre zeigt: `run_with_timeout` wartet nach `proc.start()` maximal
5 Sekunden auf READY, prüft Confinement, sendet START und wartet erst dann
`timeout_seconds` auf das Ergebnis. Die engen Wallclock-Asserts in MD-A3 messen
hingegen den gesamten Aufruf inklusive Spawn, Confinement und Tree-Kill/Join.
Das ist eine belegte unterschiedliche Messgrenze, **kein** Nachweis, welcher
Abschnitt einen konkreten historischen Flake verursachte. Es gibt hier keine
gemessene Latenzzerlegung und keinen unter normaler Last reproduzierten
READY-Timeout. Das 5-Sekunden-Limit allein rechtfertigt keinen Fix.

Deshalb bleiben READY/START, Prozess-Isolation, Job-/PGID-Confinement,
Policy-Timeouts, Quarantänegrenzen und sämtliche bestehenden Timing-Asserts
unverändert. Kein Sleep-/Timeout-Aufschlag, kein Retry, keine Error-Unterdrückung,
kein künstlicher Lastgenerator. Die READY-/Timing-Familie ist **nicht** als
behoben oder laststabil freigegeben; #10 darf nicht allein wegen dieses
Fixture-Fixes geschlossen werden.

## Verifikation und Bindung

Lokaler Run: `data/issue10-evidence/` (ignoriert, bleibt für die Übergabe erhalten).
Enthalten sind rote Erstläufe, provisionierte Baselines, echtes Fixture-RED,
GREEN und begrenzte serielle isolierte Familienläufe. MD-A1/A2/A3/A4/A5 und MDE1
waren nach dem ersten Fix grün; diese Zwischenläufe ersetzen keine finale
Vier-Suiten-Abnahme. Finale Befehle, Exit-Codes, Plattform-Skips, Logs und
unabhängiges Review werden im PR an den finalen HEAD/Tree gebunden.
Keine historischen Gesamtzähler werden als neue Messung übernommen.

Vollprüfung: Mail-Desk, Cloud-Atlas, Event-Documentation und Projektkatalog
seriell, mit Original-Shared-Siblings und bytegeprüftem Quellstand. Cloud-Atlas
wird über pytest einschließlich seiner Subtests geprüft. Syntaxprüfung erfolgt
in-memory; hinzu kommen Diff-/Index-/Scope-Hygiene, Secret-Scan und der echte
Shared-Katalogvalidator. Fremde Katalogfehler sind separat auszuweisen, nicht
zu reparieren oder als grün zu deklarieren. Ein Kernel-/Workspace-Validator
ist für dieses Skill-Bundle ohne Workspace-Profil nicht gebunden.

## Übergabe und Querverflechtungen

Keine Umsetzung von #12/#18/#19. Die #19/#18-Draft-Lane bleibt lesend und
unverändert; #25/#16 sowie Event-Writer und Draft-Transport bleiben erhalten.
Der Fixture-Diff überlappt ausschließlich MD-A3; ein späterer READY-Fix betrifft
möglicherweise zusätzlich MDE1, OCR und deren verschachtelte Spawn-Aufrufe.
Nächster sicherer Writer: nach Lease-Freigabe aktuellen Main-/PR-Stand erneut
prüfen, bei natürlichem Fehler die Phasen Spawn → READY → Confinement → START →
Result → Cleanup einzeln messen und erst bei verstandener Ursache ändern.
Es gibt keine uneingeschränkte Integrations- oder Merge-Freigabe für #10.

## Restvalidierung 2026-10-21 (Issue #10)

Der Abschlussvermerk oben („Die READY-/Timing-Familie ist **nicht** als behoben oder
laststabil freigegeben") wurde am 2026-10-21 mit einem vorab registrierten
Messplan überprüft. Plan, Amendment A1, Umgebung, Lastprofil und alle Logs liegen
im Run-Verzeichnis des Vorgangs (außerhalb des Bundles). Messgegenstand war
`main` = `fcbbb4f` im Primärclone (Host-Layout mit den Nachbarn `workspace-lock`
und `Shared-Memory/agent-architecture`); keine Änderung an Repository, Produkt-
oder Testcode, kein Liveimport, kein Mailboxzugriff.

### Ablauf

| Schritt | Tests | Ergebnis |
| --- | ---: | --- |
| R1 `mda3` isoliert ×3 | 38 | 3× OK |
| R2 `mda4`+`mda5` isoliert ×2 | 100 | 2× OK |
| R3 mail-desk-Vollframe ×2 | 1.479 | 1× OK, 1× 1 Failure |
| R4 Bundle-Vollframe | 12 + 243 + 73 + 99 + 28 | OK (3 Plattformskips) |

### Fehlerbild

F1 (Timing-Assertion): `test_process_worker_terminates_and_prevents_late_write`,
`AssertionError: 1.117… not less than 1.0`. F2 (READY-Handshake): 0 Fälle in
diesen Läufen. F3 (Marker/Cross-Run): 0 Fälle — der Workspace-lokale Marker
greift. F4: 0 Fälle.

### Ursache (gemessen, Amendment A1)

| Phase | idle (Median) | unter Last (Median) | Max |
| --- | ---: | ---: | ---: |
| Spawn-Baseline (Interpreter-Start) | 0,130 s | 0,267 s | 0,286 s |
| Spawn → READY → Confinement → START → Result → Cleanup | 0,239 s | 0,598 s | 0,692 s |
| Testaufruf gesamt (Timeout-Fenster + Terminate + Join) | 0,514 s | 0,921 s | 1,049 s |

Die 1,0-s-Wallclock-Assertion prüfte nicht die Vertragsaussage, sondern die
Windows-Spawn-Latenz des Gesamtaufrufs. Produktseitig war kein Defekt belegt:
READY-Handshake 0 Fehler, Timeout-Fenster 20/20 eingehalten.

### Konsequenz

Die Assertions sind in PR #46 an den Produktvertrag gebunden: Hängungsschutz aus
READY-Handshake-Budget (`core.quarantine.attachment_extract.READY_HANDSHAKE_TIMEOUT_SECONDS`)
plus konfiguriertem Timeout-Fenster plus Aufräum-Marge, untere Schranke und
differentielle Promptness-Prüfung gegen einen Baseline-Aufruf ohne Timeout.
Produktionsbudgets, READY/START, Confinement und Quarantäne bleiben unverändert;
der Negativnachweis (gebrochene Timeout-Durchsetzung → alle vier Tests rot) ist
im PR dokumentiert.

### Zweiter, getrennter Befund (CPU-Sättigung)

Bei voller CPU-Sättigung (8 Burner-Prozesse auf 8 logischen Kernen) trat in
anderen MD-A3-Tests die historische F2-Familie auf: `RuntimeError: Worker process
failed to send READY handshake signal.` Der Worker braucht unter Sättigung
länger als das 5-s-READY-Budget, um seine Importkette zu laden und READY zu
senden; das Verhalten ist fail-closed, aber die Familienzugehörigkeit ist damit
belegt. Der Befund ist vom Timing-Thema getrennt und wird in Issue #47 geführt;
dort ist auch der nachzuholende Lastvergleich gegen den unveränderten
Basisstand als offener Schritt notiert.
