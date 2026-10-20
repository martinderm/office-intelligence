# meeting-desk scripts

Deterministische Helfer für den Meeting-Workflow (`../SKILL.md`), Python-first,
Stdlib-only, ohne fachliche Cross-Skill-Importe. Der kanonische `workspace-lock`-Guard
wird beim Schreiben in-place geladen. Alle Skripte arbeiten auf Dateien des
konsumierenden Workspace und schreiben nur dorthin; das Bundle selbst wird nie
mutiert.

## build_clean_transcript.py

Erzeugt die bereinigte Transkript-Fassung (`*.transcript.md`) aus Roh-Transkript
(`*.transcript.raw.md`) plus Korrekturdatei — deterministisch, daher aus beiden
Quellen mit denselben Metadatenoptionen jederzeit reproduzierbar (Skill-Vertrag §2b).
Metadatenoptionen gehören deshalb zum dokumentierten Build-Aufruf.

```bash
python skills/meeting-desk/scripts/build_clean_transcript.py \
  --workspace <workspace> --lease-id <owned-lease> --json \
  --raw memory/evidence/topics/<t>/meetings/<slug>.transcript.raw.md \
  --corrections memory/evidence/topics/<t>/meetings/<slug>.corrections.json \
  --out memory/evidence/topics/<t>/meetings/<slug>.transcript.md \
  --meeting-id manual-... --title "..." --date 1792483867000 \
  --date-string "2026-10-20T08:11:07.000Z"
```

Aufruf aus dem konsumierenden Workspace; den Skriptpfad über dessen Skill-Link
auflösen. Dateipfade sind relativ zum aktuellen Arbeitsverzeichnis oder absolut.
Absolute Runtime-Parameter sind erlaubt; hartcodierte Hostpfade sind es nicht.
Alle Eingabe-/Ausgabepfade müssen nach Auflösung von Symlinks in einer der vier
Datenzonen des Workspace liegen (`memory/references/`, `memory/evidence/`,
`memory/cloud/`, `data/`). Der Bundle-Baum ist als Laufzeit-Ziel verboten.

Build und Template-Erzeugung verlangen einen aktiven eigenen Workspace-Lock
(`--lease-id` oder `--conversation-id`); keine automatische Übernahme und kein
Legacy-Bypass. `--force` erlaubt nur den bewussten Ersatz des Outputs, niemals
das Überschreiben von Raw/Korrekturdatei (auch nicht über Symlink/Hardlink).

* `--check` — Nur prüfen: baut im Speicher neu und vergleicht byte-identisch mit
  `--out` (Exit 1 bei Drift, kein Write).
* `--init-template PATH` — Schreibt `corrections-template.json` (Startpunkt für
  eine neue Korrekturdatei).
* `--force` — Bestehende `--out`-Datei überschreiben (Default: No-Clobber, Exit 2).
* Ausgabe: mit `--json` (auch Default) ein Standard-Envelope auf stdout:
  `action`, `success`, `state`, `message`, `data`, `error`.
  Modus und Zähler stehen unter `data`; Fehler erzeugen keinen Traceback.
* Exits: `0` gebaut / Check sauber, `1` Check-Drift, `2` Eingabe-/Schema-/
  Laufzeitfehler. Writes sind atomar (Tempfile + `os.replace`), LF, UTF-8.

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

## Beispiel (Pilotlauf 20.10.2026)

Der Pilotlauf ist keine Abnahme des neuen Builders. Seine Belegqualität und
Gesprächskontext-Deutungen müssen separat geprüft werden; hier wird weder eine
vollständige Workspace-Belegung behauptet noch das Pilotartefakt freigegeben.
Ergebnisdateien liegen im konsumierenden Workspace, nicht hier.

## Regressionstests

Vom Bundle-Root: `python -B -m unittest discover -s skills/meeting-desk/tests -v`.
Nur synthetische Daten in temporären Workspaces; reale Lock-Integration wird
einschließlich fehlendem, fremdem und eigenem Lease geprüft. Symlink-Test wird
übersprungen, falls das Betriebssystem die Anlage nicht erlaubt.
