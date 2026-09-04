# Legacy-CLI-Adapter (befristet)

`scripts/legacy_cli_adapter.py` ist ein expliziter, reiner JSON-Übersetzer für
zwei tatsächlich historische Mail-Desk-Ausgabeformen. Er startet **keine**
kanonische CLI und führt keine beliebigen Child-Commands aus. Die kanonischen
CLIs bleiben deshalb alleinige Wahrheit für `success`, `state`, `data` und
`error`.

## Inventur und Scope

Die Git-Historie vor OI-11a/b/c und OI-11c zeigt folgende maschinenlesbare
Formen:

- `inspect_manifest` und `himalaya_client` verwendeten
  `status`/`data`/`error`.
- `final_location_index`, `mailbox_preflight`, `move_and_patch` und
  `batch_runner` verwendeten flache Objekte mit `ok`.
- `resolve_case` hatte zwar eine frühere `resolved`-Erfolgsangabe, aber kein
  bekannter `ok`-/`status`-Konsument. Es erhält daher kein Profil.
- Die OI-09a/b/c-Cloud-CLIs hatten vor der Migration keinen JSON-
  `ok`-/`status`-Vertrag: `sync_project_cloud` verkettete Textausgabe,
  `gen_filemap` und `convert_cloud_docs` gaben lesbaren Fortschritt aus.
  Es gibt bewusst kein erfundenes Cloud-Profil.

Eine Repository-Suche fand keine aktuellen internen Aufrufer dieses Adapters
und keine fortbestehenden produktiven Aufrufe der früheren JSON-Shapes. Die
Profile sind daher nur ein opt-in Übergangsweg für nachweislich externe,
historische Konsumenten.

## Aufruf

Der Adapter liest genau ein kanonisches JSON-Envelope von stdin oder aus einer
UTF-8-Datei. `--input` wird nur gelesen und niemals entfernt oder verändert.

```bash
python3 scripts/legacy_cli_adapter.py --profile mail-desk-status-v1 < canonical.json
python3 scripts/legacy_cli_adapter.py --profile mail-desk-ok-v1 --input canonical.json
```

Er akzeptiert ausschließlich genau diese Keys mit diesen Typen:

| Key | Typ / Regel |
| --- | --- |
| `action`, `state` | nichtleere, getrimmte Zeichenfolge |
| `success` | Boolean |
| `message` | Zeichenfolge |
| `data` | JSON-Objekt |
| `error` | bei Erfolg `null`, bei Fehler ein Objekt mit nichtleerem `type` und String `message` |

Unbekannte oder zusätzliche Keys, ungültige Typen, unbekannte Profile und nicht
freigegebene Action/Profile-Paare werden abgewiesen. Der Adapter rät nicht.
`--help` schreibt die Usage nach stderr und gibt mit Exitcode `0` ein
kanonisches Erfolgs-Envelope des Adapters aus; ungültige Argumente schreiben
ebenfalls Usage nach stderr, aber mit kanonischem Fehler-Envelope und Exitcode
`2`.

## Profile und Feldabbildung

| Profil | Zulässige `action` | Rekonstruierte Payloads | Begrenzung |
| --- | --- | --- | --- |
| `mail-desk-status-v1` | `inspect_manifest`, `himalaya_client` | `inspect_manifest` entpackt `data.result`. Direkte Himalaya-Operationen entpacken `data.result`. Himalaya-Manifeste erhalten wieder `all_succeeded`, `total_operations`, `results[]` mit `action`/`success`/`result|error` und `input_file_deleted`. | Das historische `op_index` eines Himalaya-Manifest-Resultats wurde beim kanonischen Übergang nicht erhalten und kann nicht rekonstruiert werden. |
| `mail-desk-ok-v1` | `final_location_index`, `mailbox_preflight`, `move_and_patch`, `batch_runner` | Batch-Runner gibt wieder `mode` statt `operation` aus. Final-Index gibt für direkte `stats`/`query` wieder flache Felder plus `action` aus; ein Manifest-`query` nutzt seine bereits flachen Resultatfelder. `input_file_deleted` eines Final-Index-Manifests erscheint nur, wenn es im kanonischen Payload vorhanden ist. Ein `NotFound`-`lookup` behält historisch `ok:true`, `action:"lookup"`, `found:false`, `message_id`, `item:null`. Preflight entfernt `operation`. Move/Patch stellt den belegten Erfolgs-Shape `message_id`, `moved`, Quell-/Zielordner, `new_envelope_id`, `index_updated` wieder her. | Bei Move/Patch-Fehlern gab es mehrere frühere Minimalformen; nur `ok:false` und das strukturierte Fehlerobjekt werden deshalb ohne Raten ausgegeben. |

Bei tatsächlich flachen historischen Feldern verweigert der Adapter
deterministisch eine Kollision mit `ok` oder `error`; außerdem darf
`batch_runner.data.mode` den rekonstruierten `mode` nicht überschreiben und
`final_location_index.data.result.action` nicht das feste Legacy-`action` von
`stats` oder `query` ersetzen. Er überschreibt keine Nutzdaten und erzeugt
keine konkurrierende Bedeutung. Reine kanonische Hilfsfelder wie `operation`
werden nicht durchgereicht.

Ein kanonischer Fehler bleibt in beiden Profilen ein Legacy-Fehler mit einem
nicht-null, strukturierten `error`-Objekt. Der Adapter kann keinen ursprünglichen
CLI-Exitcode aus einem Envelope rekonstruieren; jeder übersetzte kanonische
Fehler endet daher zuverlässig mit Exitcode `1`. Insbesondere liefert ein
kanonischer Final-Index-`NotFound` aus Payload-Kompatibilität weiterhin
`ok:true` und `found:false`, aber zusätzlich ein strukturiertes, nicht-null
`error`-Objekt und Exitcode `1`. Historisch war der Exitcode in diesem Fall `2`;
dieser Wert ist im kanonischen Envelope nicht enthalten und daher nicht
wiederherstellbar.

## Exitcodes und Fehlervertrag

| Exitcode | Bedeutung | stdout |
| --- | --- | --- |
| `0` | kanonischer Erfolg wurde übersetzt | genau ein Legacy-JSON-Objekt |
| `1` | kanonischer Fehler wurde übersetzt | genau ein Legacy-JSON-Fehlerobjekt |
| `2` | Adapter-Eingabe/Profile/Flattening ungültig | genau ein **kanonisches** Adapter-Fehler-Envelope (`action="legacy_cli_adapter"`) |

Adaptereigene Fehler bleiben absichtlich kanonisch und nennen in `data` die
zulässigen Profile. So entsteht kein zweiter, undokumentierter Legacy-
Fehlervertrag. Es gibt keine Ausgabe neben dem einen JSON-Objekt auf stdout;
Usage/Argumentdiagnosen stehen ausschließlich auf stderr.
Diese adaptereigenen Erfolgs- und Fehler-Envelopes werden mit dem gemeinsamen
OI-10-Helper `core.envelope` gebaut und emittiert; dessen Begrenzung für
Meldungstext gilt daher auch für Adapterfehler.

## Migration und Entfernung

Neue oder geänderte Konsumenten verwenden direkt die kanonischen Felder
`success`, `state`, `data` und `error`; sie rufen diesen Adapter nicht auf.
Dieses Übergangstool wird spätestens im OI-17-Abschlussaudit erneut inventarisiert
und darf entfernt werden, sobald die Inventur keine nachweislichen externen
Konsumenten eines der zwei Profile mehr ausweist und die vollständige
Office-Intelligence-Testsuite grün ist. Eine Entfernung erfolgt nicht
automatisch und benötigt den dann geltenden Review-Gate.
