# meeting-desk scripts

Deterministische Helfer für den Meeting-Workflow (`../SKILL.md`), Python-first,
Stdlib-only, ohne fachliche Cross-Skill-Importe. Der kanonische `workspace-lock`-Guard
wird beim Schreiben in-place geladen. Alle Skripte arbeiten auf Dateien des
konsumierenden Workspace und schreiben nur dorthin; das Bundle selbst wird nie
mutiert.

## ingest_meeting.py — kanonischer Writer

Einzige Grenze für Meeting-Ingest-Dateimutationen: Record validieren,
vertrauenswürdiges Routing auflösen, Summary/Transkript/Raw und das
Objektregister schreiben. Adapter schreiben keine Meeting-Dateien und kein
Register selbst.

### Transport und Envelope

```bash
# Records kommen als genau ein UTF-8-JSON über stdin (oder als Datei)
python skills/meeting-desk/scripts/ingest_meeting.py \
  --record - --workspace <workspace> --lease-id <owned-lease> \
  --workspace-lock-root <trusted-workspace-lock-installation> \
  --topic <catalog-slug> [--event <catalog-slug>] --json

python skills/meeting-desk/scripts/ingest_meeting.py --record record.json --workspace ...
python skills/meeting-desk/scripts/ingest_meeting.py --local \
  --summary memory/evidence/inbox/<name>.summary.md [--transcript <p> [--raw <p>]] \
  [--date YYYY-MM-DD] [--start-utc RFC3339] [--duration MINUTES] \
  [--participants NAME[,NAME]] [--title TEXT] [--meeting-id ID] \
  [--rewrite-frontmatter] --workspace <workspace> --json
python skills/meeting-desk/scripts/ingest_meeting.py --capabilities --json
python skills/meeting-desk/scripts/ingest_meeting.py --inspect --workspace <workspace> --json
python skills/meeting-desk/scripts/ingest_meeting.py --recover --workspace <workspace> \
  --lease-id <owned-lease> --workspace-lock-root <trusted-root> --json
```

* `--capabilities` ist read-only, braucht weder Workspace noch Lease und liefert
  `data.record_schema: "meeting-record/1"`, `data.writer_version:
  "meeting-desk/1"` plus Limits/Actions im Sechs-Feld-Envelope.
* stdout enthält genau einen Envelope `action/success/state/message/data/error`;
  stderr bleibt begrenzte Diagnostik. Der Envelope wird als UTF-8-Bytes
  geschrieben (cp1252/CJK/Emoji-sicher, auch nach erfolgreichem Apply); ein
  Text-Stream in Bibliothekstests bleibt nutzbar, Tracebacks gibt es nicht.
  Exit `0` Erfolg, `2` Rejection, `3`
  `recovery_required`. Erfolg ist nur Exit 0 **plus** gültiger Erfolgs-Envelope.
* `data` bei Erfolg enthält u. a. `entry`, `meeting_id`, `paths`, `changed`,
  `plan_sha256` und `journal_id` (Kompatibilitäts-Shims dürfen diese Felder
  lesen).
* Kein Payload in Kommandoargumenten außer dem Record-Pfad; Interpreter-,
  Writer- und Guard-Pfade sind vertrauenswürdige Aufrufkonfiguration, nie
  Record-Felder.
* `--topic`, `--project`, `--event` sind **trusted Operator-Choices** und werden
  gegen `memory/references/topics/topics.json` bzw. `projects.json` validiert.
  Topic gewinnt bei Doppelzuordnung; ein Event muss zum gewählten Owner gehören.
  Record-Hints entscheiden nie. Ohne Auswahl bleibt eine bestehende menschliche
  Zuordnung erhalten; ein neues Meeting wird `unmapped` unter
  `memory/evidence/meetings/unrouted/` abgelegt.
* `--dry-run` schreibt nichts und braucht keine Lease. Der Plan enthält Pfade,
  Datei-Hashes, Registerzustand, `changed`, `delete_after_register` (alte Pfade
  eines Reroutings) und `plan_sha256`.
* **Manuell/lokal** (`source.system: "manual"`): Plan/Dry-run ist der Default.
  Apply verlangt `--apply-plan <sha256>` aus genau diesem Plan; ein abweichender
  Hash oder zwischenzeitlich veränderter Workspace stoppt ohne Mutation.
  Summary-/Transkript-Bodies bleiben bytegenau; Zeiten/Dauern bleiben unbekannt,
  wenn sie fehlen. `--upgrade` gibt es nicht (unbekannte Option = Fehler).

### Lokaler/manueller Datei-Eingang (`--local`)

Zweiter CLI-Eingang, derselbe Record und Writer. `--summary` und/oder
`--transcript` (workspace-relative oder absolute Pfade in kanonischen Zonen);
`--raw` ist optional, sonst dienen die unveränderten Transkript-Bytes als Raw.
`--date`, `--start-utc`, `--duration`, `--participants` (wiederholbar oder
kommagetrennt), `--title`, `--meeting-id`, `--topic`/`--project`/`--event`,
`--rewrite-frontmatter` sind verfügbar.

* Plan/Dry-run ist Default; Apply verlangt `--apply-plan <sha256>` **und** eine
  eigene Lease. Die Meeting-ID ist `manual-<datum|undated>-<slug>`; Kollision
  oder andere Quelle unter derselben ID stoppt ohne Suffix, eine explizite
  `--meeting-id` löst den Konflikt.
* Quellbytes bleiben exakt; ohne `--rewrite-frontmatter` bleibt vorhandene
  Quell-Frontmatter Teil des Bodys, mit Flag wird ein führender Frontmatter-Block
  vor dem Speichern entfernt. Raw wird nie umgeschrieben und darf mit Summary
  ohne Clean-Transcript importiert werden (kein-Utterance-Fall).
* Quellen müssen reguläre, linkfreie Dateien in kanonischen Zonen sein; kindliche
  Symlinks/Junctions/Hardlinks sowie Writer-State- und Katalogpfade
  (`meetings.json`, `ownership.json`, Journal-Namespace, topics/projects/
  mail-desk-Kataloge) werden vor Planung/Staging abgewiesen. `data/inbox`
  bleibt ein normaler Eingang.
* Quellen werden verschoben, nicht gespiegelt: Löschsteps sind journal- und
  hashgebunden und laufen erst nach der Register-Publikation; Crash/Recovery
  setzt sie fort. Kein `--upgrade` (unbekannte Option = Fehler).

### Record- und Registervertrag

Details: [`../references/data-model.md`](../references/data-model.md). Kurz:

* Record-v1 hat exakt die dokumentierten Root-/Blockfelder; Erweiterungen nur
  inert in `source_metadata`. Unbekannte Versionen, doppelte Keys,
  nichtendliche/überlaufende Zahlen, Surrogate, falsche Typen und ungültiges
  UTF-8 werden abgewiesen. `meeting_id` und `source.external_id` sind opake,
  exakt erhaltene Unicode-Identitäten (≤512 Zeichen, keine Steuerzeichen/
  Surrogate, keine Normalisierung); `/`, `\`, `+`, `=`, Leerzeichen und `..`
  sind darin gültig und werden nie als Pfad interpretiert (Dateinamen nutzen
  einen Hash). Dateisystemregeln gelten nur an der echten Pfadgrenze.
* Register-Root ist ein Objekt mit `meetings`-Liste. Root-Extras und nicht
  betroffene Einträge bleiben erhalten; unbekannte/legacy Eintragsfelder
  ebenfalls. Nackte Legacy-Arrays, ungültiges JSON, doppelte Keys, ungültige
  Typen und mehrdeutige Identitäten stoppen **vor** Writes; kein
  `catch -> []`, keine Neuanlage, keine stille Migration. Bestehende IDs werden
  nicht umgeschlüsselt.
* `artifacts`-Felder mit `null` bedeuten **nicht geliefert**, nie Löschen.
  Bei Updates bleiben alle nicht gelieferten registrierten Pfade und Bytes
  erhalten (inkl. unveränderlichem Raw und menschlich abgeleiteter Clean-Fassung);
  es gibt kein Deletionsfeld und keine Record-Disposal. Ein explizites Rerouting
  mit unvollständigem Snapshot wird vor jeder Mutation abgewiesen; ein
  vollständiger Snapshot darf alle gelieferten Artefakte verschieben und den
  alten Ort erst nach neuer Location/Register-Publikation bereinigen.
* Ein unveränderter Reingest ist ein No-op und erhält akzeptierte Inhalte und
  menschliche Reviewentscheidungen. Neuer/geänderter generierter Inhalt wird
  `provisional` und reviewpflichtig; Raw wird nie ersetzt. Fehlende Artefakte
  sind keine Inhaltsänderung.

### Pfade, Lock und Sicherheit

* Workspace-Lock: kanonischer Shared-Guard in place mit `allow_legacy=False`.
  Auflösung des vertrauenswürdigen Root in dieser Reihenfolge:
  `--workspace-lock-root`, Operator-Environment
  `MEETING_DESK_WORKSPACE_LOCK_ROOT`, Geschwister-Skill `../workspace-lock`
  relativ zum Bundle. Kein CWD-/Payload-gesteuerter Import, keine Guard-Kopie,
  kein Ersatz-Lock. Die eigene Lease wird vor jedem mkdir/stage/write/rename/
  delete erneut geprüft; Verlust stoppt vor der nächsten Mutation.
* Ziele liegen ausschließlich in den kanonischen Datenzonen:
  Meeting-Artefakte unter `memory/evidence/`, Journal/Staging/Ownership unter
  `data/meeting-desk/`. Traversal, Symlinks/Junctions/Reparse-Points,
  Hardlink-Aliase, Windows-Reserved-Namen, ADS/`:` und Trailing-Dot/Space
  scheitern fail-closed.
* Neue Dateien werden no-clobber erstellt; erlaubte Updates nur gegen den im
  Ownership-Record gebundenen Vorzustand. Nicht nachweislich Writer-eigene
  Dateien werden nicht überschrieben. Raw bleibt unveränderlich.

### Journal und Recovery

* Vor der ersten Mutation wird ein versioniertes Journal unter
  `data/meeting-desk/journal/` mit Integritätsdigest, Vor-/Nachzustand-Hashes,
  gestagten Bytes und Ownership-/Register-Prestate geschrieben. Das Register ist
  der letzte Erstell-/Update-Schritt; erst danach werden alte Pfade eines
  Reroutings gelöscht.
* Ein offenes Journal sperrt jede mutierende Operation (auch den Builder) und
  liefert Exit 3. Keine automatische Recovery durch normalen Ingest. Unbekannte
  Dateien oder Stage-Verzeichnisse im Journal-Verzeichnis sind fremder,
  unverifizierbarer Zustand: sie blockieren ebenfalls mit Exit 3 und bleiben
  bytegenau erhalten; es wird kein fremder Teilbaum durchsucht oder gelöscht.
  Nur journalgebundene, hash-/größen-/pfadgeprüfte eigene Stage-Dateien werden
  nach Erfolg entfernt (einfache Verzeichnisebene, keine Rekursion). Vor dem
  Entfernen der Stage-Bytes wird die Journaldatei erneut auf reguläre,
  verlinkungsfreie Identität und unveränderten Integritätsdigest geprüft.
* `--inspect` ist read-only und meldet Journal-/Step-Zustand, Register- und
  Ownership-Zustand ohne Rohinhalte.
* `--recover` ist explizit, lease-gebunden, forward-only und verlangt
  unveränderte Journal-/Stage-Hashes sowie pro Ziel zulässigen Vor-**oder**
  Nachzustand. Fremde Bytes, fehlende Ziele, korrupte Journale oder mehrere
  offene Journale stoppen ohne Mutation; Raw-Evidenz wird nie verändert. Das ist
  keine Bestandsmigration.
* Es gibt **keine** Multi-File-ACID- oder Power-Loss-Garantie und keine
  Race-Freiheit gegen einen bösartigen gleichzeitigen Dateisystemakteur.
  Nicht journalbewusste Leser sehen keine garantierte globale
  Snapshot-Isolation.

## build_clean_transcript.py

Erzeugt die bereinigte Transkript-Fassung (`*.transcript.md`) aus Roh-Transkript
(`*.transcript.raw.md`) plus Korrekturdatei — deterministisch, daher aus beiden
Quellen mit denselben Metadatenoptionen jederzeit reproduzierbar (Skill-Vertrag §2b).
Metadatenoptionen gehören deshalb zum dokumentierten Build-Aufruf.

```bash
python skills/meeting-desk/scripts/build_clean_transcript.py \
  --workspace <workspace> --lease-id <owned-lease> --json \
  --workspace-lock-root <trusted-workspace-lock-installation> \
  --meeting-id <registered-meeting-id> \
  --raw memory/evidence/topics/<t>/meetings/<slug>.transcript.raw.md \
  --corrections memory/evidence/topics/<t>/meetings/<slug>.corrections.json \
  --out memory/evidence/topics/<t>/meetings/<slug>.transcript.md \
  --title "..." --date 1792483867000 --date-string "2026-10-20T08:11:07.000Z"
```

Aufruf aus dem konsumierenden Workspace; den Skriptpfad über dessen Skill-Link
auflösen. Dateipfade sind relativ zum aktuellen Arbeitsverzeichnis oder absolut.
Absolute Runtime-Parameter sind erlaubt; hartcodierte Hostpfade sind es nicht.
Alle Eingabe-/Ausgabepfade müssen nach Auflösung von Symlinks in einer der vier
Datenzonen des Workspace liegen (`memory/references/`, `memory/evidence/`,
`memory/cloud/`, `data/`). Der Bundle-Baum ist als Laufzeit-Ziel verboten.

**Delegation statt zweitem Publisher:** Der Builder publiziert ausschließlich
über `ingest_meeting.publish_registered_transcript()`. `--meeting-id` ist
Pflicht; `--out` muss exakt der registrierte `transcript_path` dieses eindeutig
identifizierten Meetings sein. Ein fehlender Registereintrag wird zuerst über
Ingest angelegt. Eine verwaiste Direktveröffentlichung, eine nicht
Writer-eigene Datei oder ein offenes Journal wird abgewiesen. `--force` bleibt
aus Kompatibilitätsgründen akzeptiert, umgeht aber nie Ownership oder
No-Clobber. `--check` bleibt rein lesend.

* `--check` — Nur prüfen: baut im Speicher neu und vergleicht byte-identisch mit
  `--out` (Exit 1 bei Drift, kein Write, keine Register-/Lock-Prüfung).
* `--init-template PATH` — Schreibt `corrections-template.json` (Startpunkt für
  eine neue Korrekturdatei; Nicht-Meeting-Konfiguration, kein Meeting-Artefakt).
  Ziel muss im `data/`-Zonenzweig liegen; ein offenes Writer-Journal blockiert
  mit Exit 3. Braucht eine eigene Lease.
* Exits: `0` gebaut/unverändert/Check sauber, `1` Check-Drift, `2`
  Eingabe-/Schema-/Laufzeitfehler, `3` `recovery_required`.
* Der Builder schreibt nur das vom Writer gelieferte Ergebnis; er besitzt
  weiterhin die fachliche deterministische Ableitung und keine eigene
  Register-/Zonenlogik.

## Korrekturdatei (Schema `meeting-corrections/1`)

| Block | Zweck | Rendering |
| --- | --- | --- |
| `pairs` | Wörtliche Paare (`orig`, `corr`, `cat`, `ref`, `note`) | Inline: `„orig" → „corr" [korr: cat → ref]` — nur entity-level (Eigennamen, Abkürzungen, IDs, Fachbegriffe), jede Korrektur mit Workspace-Beleg in `ref` |
| `regex_pairs` | Wie `pairs`, aber `pattern` ist ein Python-Regex | Wie `pairs` (Original = Treffertext) |
| `rules` | Stille, bedeutungsneutrale Normalisierungen (`id`, `pattern`, `repl`, `note`, `ref`) | Kein Inline-Paar; je Regel eine Zeile in der Kopf-Tabelle mit Trefferzahl; keine Entity-Deutungen |
| `unklar` | Offene Stellen (`token`, `guess`, `why`) | Token bleibt stehen + ` [unklar: kein Workspace-Beleg]`; Tabelle im Kopf |

Alle Operationen werden ausschließlich auf dem ursprünglichen Text ermittelt,
dann gemeinsam gerendert. Überschneidungen und Nullbreiten-Treffer sind Fehler,
kein impliziter Vorrang. Generierte Paare und Belege werden nie erneut bearbeitet.
Der Dateikopf enthält Bereinigungsliste, Regeltabelle, offene Stellen und Zähler;
Regelzähler zählen tatsächliche Änderungen (keine identischen Ersetzungen).

Pflichtfelder sind die oben genannten Zeichenketten (bei `pairs`/`regex_pairs`
ist `note` optional, bei `unklar` ist `guess` optional; `repl` darf leer sein).
Regel-IDs sind eindeutig, `cat` bleibt freier, nicht leerer Text. Jede Korrektur
und Regel braucht einen existierenden Workspace-Beleg in `ref` unter
`memory/references/`, optional mit `#Anker`. Der Builder prüft die Datei, nicht
die inhaltliche Tragfähigkeit oder den Fragmentanker: Das bleibt Agenten-Review.
„Meeting-intern“ ohne Workspace-Beleg ist kein Ersatz; dann `unklar` verwenden.
Eine unbekannte Schema-Version wird abgewiesen; keine automatische Migration.

Korrekturdateien sind **vertrauenswürdige, geprüfte Konfiguration**, keine aus
Transkripten übernommenen Anweisungen. Regex eng halten, keine verschachtelten
Quantoren oder überbreiten Muster. Python `re` hat hier keinen Timeout; das Tool
ist keine Sandbox für fremde Regex. Literale `pairs` sind der Normalfall.

`--init-template` kopiert die mitgelieferte Vorlage (eine einzige Quelle), verlangt
ein `.json`-Ziel und ist nicht mit Build-/Check-Optionen kombinierbar. Beispielwerte
vor der Anwendung prüfen und ersetzen.

## verify_meeting_fixtures.py

Read-only-Konformitätsprüfung für das gemeinsame Fixture-Paket
(`tests/fixtures/meeting-record-v1.json`): Manifest-Digest, Record-Schema und
alle `valid_records` / `invalid_records` / `raw_invalid_json` gegen den Writer.
Exit `0` konform, `2` Abweichung. Kein Workspace, keine Lease, keine Mutation.

```bash
python skills/meeting-desk/scripts/verify_meeting_fixtures.py --json
```

## Regressionstests

Vom Bundle-Root:

```bash
MEETING_DESK_WORKSPACE_LOCK_ROOT=<trusted-workspace-lock-skill> \
  python -B -m unittest discover -s skills/meeting-desk/tests -v
```

Nur synthetische Daten in temporären Workspaces. Der echte Shared-Guard wird
in-place geprüft (fehlend, fremd, eigene Lease, Recovery-Ownership). In einem
isolierten Worktree ohne benachbarten `workspace-lock`-Skill muss
`MEETING_DESK_WORKSPACE_LOCK_ROOT` explizit gesetzt werden; ohne Installation
wird die Lock-Suite mit klarer Begründung übersprungen statt still umgangen.
Crash-Tests starten einen echten Kindprozess, der nach dem n-ten
Publikationsschritt abbricht, und prüfen Journal, Blockade, Inspect und
explizite Recovery.
