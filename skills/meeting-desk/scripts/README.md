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
  "meeting-desk/1"` plus Limits/Actions im Sechs-Feld-Envelope. `data.actions`
  nennt `ingest_record`, `ingest_local`, `publish_registered_transcript`,
  `publish_registered_summary` (Issue #52 P1), `retitle_registered_meeting`
  (Issue #52 P5), `inspect`, `recover`.
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
* **Größe:** jede Quelle (`--summary`, `--transcript`, `--raw`) ist auf
  `MAX_LOCAL_ARTIFACT_BYTES` = 4 MiB (4 194 304 UTF-8-Bytes der Datei, vor dem
  Dekodieren geprüft) begrenzt; genau 4 MiB passieren, +1 Byte wird ohne
  Mutation mit `local artifact '<art>' (<relativer Pfad>) exceeds 4194304 bytes
  (4 MiB)` abgewiesen. Das ist eine Byte-Grenze, keine Zeichenzahl. Der
  `--record`-Transport bleibt unverändert bei 512 KiB Zeichen pro Artefakt und
  1 MiB pro Record (`--capabilities`: `limits.local_artifact_bytes`,
  `artifact_chars`, `record_bytes`).
* **Steuerzeichen:** nur UTF-8; `\t`, `\n`, `\r` sind erlaubt, NUL und alle
  anderen C0-Zeichen sowie U+007F werden mit Artefaktart, Pfad und Codepoint
  (z. B. `U+000B`) abgewiesen. Keine stille Normalisierung; CRLF und BOM werden
  unverändert publiziert.
* **Adapter-Konvention:** große Raws (> 512 KiB Zeichen) werden in `data/inbox/`
  gestaged und über `--local` eingeliefert; `--record` bleibt für portable Records
  mit 512 KiB Zeichen / 1 MiB.
* **Eskalationsregel (> 4 MiB):** nicht in Schein-Meetings splitten, keinen Stub
  registrieren, nicht manuell in die Evidence-Zone schreiben. Quelle behalten,
  Größe und SHA-256 melden, den Owner entscheiden lassen und auf Issue #49
  verweisen.
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
  `WORKSPACE_LOCK_ROOT`, kompatibler Alias `MEETING_DESK_WORKSPACE_LOCK_ROOT`, Geschwister-Skill `../workspace-lock`
  relativ zum Bundle. Kein CWD-/Payload-gesteuerter Import, keine Guard-Kopie,
  kein Ersatz-Lock. Die eigene Lease wird vor jedem mkdir/stage/write/rename/
  delete erneut geprüft; Verlust stoppt vor der nächsten Mutation.
* **Falscher Lock-Root (#52 P3):** Der Root ist immer das Skill-Verzeichnis selbst
  (Beispiel `.agents/skills/workspace-lock`), nicht dessen Elternverzeichnis
  `.agents/skills`. Es gibt weder Suche noch Ersatzauflösung. Fehlt unter dem
  gewählten Root eine der beiden Dateien `<root>/scripts/workspace_lock_guard.py`
  und `<root>/scripts/invoke_workspace_lock.py`, nennt die Ablehnung (Writer-CLI/-API
  und Builder, auch `--init-template`) den gewählten Root, beide erwarteten Pfade und
  dieses Beispiel; es wird nichts geladen oder geschrieben. Das ist ein
  Installationsfehler und wird nicht als Lease-Fehler gemeldet. Fehlende, fremde
  oder abgelaufene Lease bleibt unverändert fail-closed („an owned active
  workspace-lock lease is required“).
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

### Summary-Ersatz (`publish_registered_summary`, Issue #52 P1)

Python-API neben `publish_registered_transcript` (kein CLI-Flag), in
`--capabilities` unter `data.actions` ausgewiesen:

```python
ingest_meeting.publish_registered_summary(
    content, workspace_root=..., lease_id=..., meeting_id=<registered-id>,
    conversation_id=None, trusted_workspace_lock_root=None, dry_run=False)
```

* Ersetzt **nur** den registrierten `summary_path` des eindeutig identifizierten
  Meetings. Transkript, Raw und deren Ownership-Einträge bleiben byte- bzw.
  eintragsgleich; im Register ändern sich nur `content_status`
  (`provisional`), `review_recommended`, `review_reason` und `last_synced_at`.
* `content` ist die komplette Summary-Datei inkl. Frontmatter (UTF-8, byte-exakt,
  ≤ 4 MiB, nicht leer, keine NUL/C0/U+007F-Steuerzeichen außer TAB/LF/CR). Der Writer
  rendert oder prüft das Frontmatter nicht — der Aufrufer hält es konsistent.
* **Nur Ersatz, nie Neuanlage:** fehlt der registrierte `summary_path` oder die
  registrierte Datei, wird abgewiesen (Exit 2, keine Mutation, kein Orphan);
  Neuanlage über `ingest_record`/`--local`.
* **Fail-closed:** abgewiesen werden kein/mehrere Registertreffer, Pfad außerhalb
  `memory/evidence/`, Transkript-/Raw-Dateiname oder mit einem anderen
  Register-Pfadfeld (auch eines anderen Meetings) geteilter Pfad, fehlender/
  doppelter/artfremder/an ein anderes Meeting gebundener Ownership-Eintrag
  (`kind` und `meeting_id` müssen exakt `summary` bzw. die stabile ID — nie ein
  Alias — sein; fehlende Felder werden abgewiesen) und Hash-Abweichung
  (Handedit).
* Identischer Inhalt → `Unchanged` ohne Write; `dry_run=True` liefert den Plan
  (`action: publish_summary`) ohne Lease. Offenes oder nach unterbrochener
  Publikation verbliebenes Journal → `recovery_required` (Exit 3), Wiederholung
  nur nach explizitem `--recover`.

### Titelkorrektur (`retitle_registered_meeting`, Issue #52 P5)

Python-API neben den beiden Publish-Seams (kein CLI-Flag, keine Adapter-Anbindung),
in `--capabilities` unter `data.actions` ausgewiesen. Ersetzt den „Handedit
von Titel in Summary/Transkript/Register", der die Ownership bricht:

```python
ingest_meeting.retitle_registered_meeting(
    title, workspace_root=..., lease_id=..., meeting_id=<registered-id>,
    conversation_id=None, trusted_workspace_lock_root=None, dry_run=False)
```

* Setzt **nur** den Titel: `title` des Registereintrags plus die einzelne
  `title:`-Zeile im führenden Frontmatter des registrierten Summarys und
  Transkripts (writer-gerendert und Builder-Kopf: `title: "<JSON-String>"`). Alle
  anderen Zeilen, Zeilenenden, Bodies und Frontmatter-Felder (`corrections_count`,
  `unklar_count`, `provenance`, …) sowie alle anderen Registerfelder — auch
  Review-/Inhaltsstatus — bleiben unverändert.
* **Pfade und Slugs bleiben stabil:** keine Umbenennung, kein neuer Dateistamm,
  kein Rekey. Das Raw-Transkript hat per Vertrag kein Frontmatter, ist unveränderlich
  und wird nie zum Schreiben gelesen: Bytes und Ownership-Eintrag bleiben identisch.
* `title` wie `record.title` (nicht leer, ≤ 500 Zeichen, keine NUL/C0/U+007F-
  Steuerzeichen außer TAB/LF/CR, keine losen Surrogates).
* Ein Artefakt ohne Frontmatter oder ohne `title:`-Zeile bleibt byte-identisch
  (kein Titel, der inkonsistent sein könnte); `plan.files[].frontmatter_title`
  nennt `updated`/`current`/`absent`.
* **Fail-closed (Exit 2, keine Mutation):** kein/mehrere Registertreffer; weder
  Summary noch Transkript registriert; Pfad außerhalb `memory/evidence/`; fehlende
  registrierte Datei; Pfad geteilt/aliasiert oder mit falschem Dateinamen; fehlender/
  doppelter/artfremder/an ein anderes Meeting oder einen Alias gebundener Ownership-
  Eintrag; Hash-Abweichung (Handedit); unterminierter Frontmatter-Block, mehrere
  `title:`-Zeilen oder ein Wert, der kein einzeiliger JSON-String ist.
* Ownership: nur die tatsächlich geänderten Dateien erhalten einen neuen Eintrag
  (SHA-256, Größe, Zeit); unberührte Artefakte behalten ihren Eintrag unverändert.
  Reihenfolge wie bei jeder Publikation: Dateien, Ownership, Register zuletzt;
  danach bleiben `build_clean_transcript`, `publish_registered_summary` und
  `ingest_record` ohne Wiederherstellung zulässig.
* Idempotent: gleicher Titel → `Unchanged`, kein Write; ein teilweise
  inkonsistenter Bestand (z. B. Register alt, Dateien neu) wird nur dort repariert.
  `dry_run=True` liefert den Plan (`action: retitle_meeting`, `title.from/to`)
  ohne Lease. Offenes oder unterbrochenes Journal → `recovery_required` (Exit 3).
* Bekannte Grenze: wurde ein Transkript über `publish_registered_transcript` unter
  einem **Alias** statt der stabilen ID publiziert, ist sein Ownership-Eintrag an den
  Alias gebunden; Retitle weist es dann ab („not bound to this meeting"), statt eine
  Fremdbindung zu überschreiben. Eine Wiederholung mit identischen Bytes ändert die
  Bindung nicht; erst eine inhaltliche Änderung des Transkripts unter der stabilen ID
  bindet es neu (Owner-Entscheidung, falls dieser Fall auftritt).
* Der Builder rendert den Titel aus seiner `--title`-Option: nach einer
  Titelkorrektur gehört der **neue** Titel in den dokumentierten Build-Aufruf,
  sonst setzt der nächste Lauf den Kopf zurück und `--check` meldet Drift.

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

**Prozesskette (#52 P8).** Die Bereinigung ist der Builder-Lauf oben
(`--raw --corrections --out`). Danach prüft `--check` ausschließlich die
byte-identische Reproduzierbarkeit der bestehenden Ausgabedatei mit denselben
Metadatenoptionen (Exit 0 = reproduzierbar, Exit 1 = Drift); `--check` ersetzt
den Builder-Lauf nicht. Danach folgt der Summary-Review (manuelle Prüfung, bei
Korrekturbedarf über `publish_registered_summary()`); den Abschluss bildet der
**manuelle EVID-Eintrag nach SKILL §4** im Monats-Journal. Pflicht sind die drei
Schritte Builder-Lauf, `--check` und Summary-Review, auch ohne Korrekturbedarf
(Skill §2b); keine Wrapper-Pflicht.

**`--date` (#52 P2).** Der Builder-Kopf kennt drei Formen, jeweils ohne externe
Epoch-Umrechnung:

| `--date` | Kopfzeile `date:` | `dateString:` (ohne `--date-string`) |
| --- | --- | --- |
| fehlt | `null` | `""` |
| Ganzzahl (Epoch-Millisekunden, wie bisher) | die Ganzzahl | `""` |
| strikter ISO-Kalendertag `YYYY-MM-DD` | der Tag als quotierter String, z. B. `"2026-10-27"` | derselbe ISO-Tag |

Ein Kalendertag trägt keine Uhrzeit: Es wird weder ein Mitternachts-Start erfunden
noch ein Epoch-Wert berechnet. Ein explizit angegebenes `--date-string` bleibt in
allen Fällen unverändert. Der Tag wird als echter Kalendertag geprüft (Schaltjahr,
Monat/Tag-Grenzen, nur ASCII-Ziffern); jeder andere Wert wird mit einer
Fehlermeldung zu `--date` abgewiesen (Exit 2, auch bei `--check` und bei
`--init-template`), bevor gelesen, veröffentlicht oder geschrieben wird. Eine reine
Ziffernfolge ist immer Epoch-Millisekunden — auch `20261027`; ein kompakter Tag ist
keine unterstützte Form. Dieses Kopf-Format gilt nur für den Builder; das Register
(`meeting_date`, `start`) wird nicht umgeschrieben und es gibt keine neue
Schemaversion. Für byte-identischen Rebuild und `--check` müssen dieselben
`--date`/`--date-string`-Optionen erneut angegeben werden; ein anderer Wert ist
Drift bzw. ein neuer Inhalt.

Aufruf aus dem konsumierenden Workspace; den Skriptpfad über dessen Skill-Link
auflösen. Dateipfade sind relativ zum aktuellen Arbeitsverzeichnis oder absolut.
Absolute Runtime-Parameter sind erlaubt; hartcodierte Hostpfade sind es nicht.
Alle Eingabe-/Ausgabepfade müssen nach Auflösung von Symlinks in einer der vier
Datenzonen des Workspace liegen (`memory/references/`, `memory/evidence/`,
`memory/cloud/`, `data/`). Der Bundle-Baum ist als Laufzeit-Ziel verboten. **Issue #52 P7:**
`memory/operations/` ist **ausschließlich** als Leseort für Beleg-Referenzen in Korrekturdateien zugelassen; Schreiboperationen bleiben auf die vier Data-Zones beschränkt.

**Delegation statt zweitem Publisher:** Der Builder publiziert ausschließlich
über `ingest_meeting.publish_registered_transcript()`. `--meeting-id` ist
Pflicht; `--out` muss exakt der registrierte `transcript_path` dieses eindeutig
identifizierten Meetings sein. Ein fehlender Registereintrag wird zuerst über
Ingest angelegt. Eine verwaiste Direktveröffentlichung, eine nicht
Writer-eigene Datei oder ein offenes Journal wird abgewiesen; eine Plan-Rejection
bei offenem Journal (z. B. veraltete Ownership nach unterbrochener Publikation)
meldet `recovery_required` (Exit 3). `--force` bleibt
aus Kompatibilitätsgründen akzeptiert, umgeht aber nie Ownership oder
No-Clobber. `--check` bleibt rein lesend.

* `--check` — Nur prüfen: baut im Speicher neu und vergleicht byte-identisch mit
  `--out` (Exit 1 bei Drift, kein Write, keine Register-/Lock-Prüfung).
* `--init-template PATH` — Schreibt `corrections-template.json` (Startpunkt für
  eine neue Korrekturdatei; Nicht-Meeting-Konfiguration, kein Meeting-Artefakt).
  Ziel muss im `data/`-Zonenzweig liegen; ein offenes Writer-Journal blockiert
  mit Exit 3. Braucht eine eigene Lease; ein angegebenes ungültiges `--date`
  bricht vor jedem Write ab, ein falscher Lock-Root meldet die erwarteten
  Guard-Pfade (siehe oben).
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
`memory/references/` oder `memory/operations/`, optional mit `#Anker`. Der Builder
prüft die Datei, nicht die inhaltliche Tragfähigkeit oder den Fragmentanker: Das
bleibt Agenten-Review. `memory/operations/` ist ausschließlich Leseort; der
Builder akzeptiert dort weder fehlende Dateien noch Traversals, absolute Pfade
oder ausbrechende Symlinks.
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
