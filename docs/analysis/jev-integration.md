# Jev-Integration für Mail-Desk — Konzeptanalyse

**Status:** Konzeptdokument (kein Feature-Record, keine Umsetzung; Vorarbeit für
ein potenzielles FR-Record-Verfahren). Erstellt 2026-09-27.
**Vertragshistorie:** Analyse gegen den Shared Skill
[`jev-integration`](../../../jev-integration/SKILL.md) (Submodule), dessen
Referenzen per Frische-Checker am 2026-09-27 als 19/19 frisch verifiziert wurden;
Live-Quellen ([TypeSafe-Doku](https://docs.typesafe.ai/llms.txt),
[OpenCode-Zen-Doku](https://opencode.ai/docs/zen/)) wurden am selben Tag direkt
geprüft. Ticket-IDs sind in System Map und `docs/features/_archive.md` kanonisch.

---

## 1. Was Jev ist

Jev (TypeSafe System One) bewertet einen gelieferten `state` gegen **typisierte
Fragen** und liefert strukturierte Antworten statt generierten Texts: `Choice`
(eine Option aus Menge + Wahrscheinlichkeitsverteilung + Confidence), `Score`
(geordnete Rubrik), `Noul` (Ja-Wahrscheinlichkeit). Der Anwendungscode behält
Workflow, Arithmetik, Policy, Schwellenwerte und Aktionen; Jev liefert nur kleine,
unabhängig prüfbare Teilurteile über unstrukturierte Inhalte. Modellstand:
`jev-1.13.0` (Vendor) / `jev-1.13` (Zen), Text-only, 64k-Kontext, Output-Tokens
kostenfrei. Bekannte Schwächen (Jev 1.13): wörtliche Textlesart, schwache
Zahl-/Datumsvergleiche — Arithmetik und Datumsordnung bleiben im Code.

## 2. Gateway: OpenCode Zen

Jev ist über zwei Endpunkte erreichbar; für dieses Bundle ist **OpenCode Zen der
vorgesehene Gateway**:

| Aspekt | Vendor (`api.typesafe.ai`) | OpenCode Zen (`opencode.ai/zen/v1/systemone`) |
|---|---|---|
| Auth | `TYPESAFE_API_KEY` (Bearer) | `OPENCODE_API_KEY` (Bearer) |
| Model-IDs | `jev-1.13.0`, `jev-latest` | `jev-1.13`, `jev-1.13-free` (limitiert kostenlos) |
| Preis Input/Output | $42/Btok / frei | $0,042/Mtok / frei (identisch, kein Aufschlag) |
| Rate Limits | 250k tok/s, 1.200 req/min (dokumentiert) | nicht dokumentiert (offener Punkt) |
| Privacy | Vendor-Policy | Provider-Zero-Retention-Aussage; Jev nicht als Ausnahme gelistet; Gateway-Verarbeitung offen |

Der Zen-Endpoint ist live verifiziert (Modellliste enthält `jev-1.13` und
`jev-1.13-free`; POST ohne Key → 401 mit Bearer-Anforderung). Ein OpenCode-Zen-Key
ist auf dieser Maschine in OpenCodes `auth.json` vorhanden (Existenz geprüft,
Wert nie gelesen). Das belegt **keinen** Key im Host-Env: Vor jedem Live-Request
muss `OPENCODE_API_KEY` separat bereitgestellt und ohne Ausgabe des Werts geprüft
werden. Das Free-Modell ist nur zeitlich begrenzt verfügbar; seine Nutzung ist
keine Freigabe für die Übermittlung von Mailinhalten.

**Credential-Disziplin (verbindlich):** Der Runtime-Adapter liest den Key
ausschließlich aus der Host-Umgebung (`OPENCODE_API_KEY`). Das Lesen von
`auth.json` oder das Kopieren von Credentials in den Workspace bleibt verboten
(Dual-Evidence-/Credentials-Grenzen des Bundles). Die Workspace-Bindung
`.agents/mail-desk-backend.json` bleibt credentials-frei; ein Jev-Zugriff ist
kein Mailbox-Backend und ändert daran nichts.

**Harness-Agnostik:** `OPENCODE_API_KEY` ist ein Zen-API-Key, keine Bindung an
die OpenCode-Laufzeit. Der Mail-Desk-Client sendet den Bearer-Key selbst per
HTTPS; Codex, OpenCode oder ein anderer Harness können denselben stdlib-Client
starten, sofern **dessen Prozess** die Variable erhält. Die Workspace-Datei
steuert das Opt-in und Budget, nicht den geheimen Wert. Ein Klartext-Key dort
würde ihn in einen gemeinsam lesbaren und möglicherweise versionierten
Workspace verschieben, ohne den HTTP-Client portabler zu machen. Kann ein
Harness die Variable nicht direkt bereitstellen, muss ein hosteigener
Secret-Store oder Starter sie vor dem Prozessstart injizieren; höchstens eine
nicht geheime Secret-Referenz wäre als spätere Konfigurationsoption denkbar.
Ohne verfügbaren Key erfolgt kein externer Request und der aktivierte Seam
meldet explizit `unavailable`.

**Datengrenzen (verbindlich):** Mailinhalte können personenbezogene und vertrauliche
Informationen enthalten. Das explizite Human-Approval-Gate gilt **vor dem ersten
externen Request**, auch bei Replay, Kalibrierung und Free-Modell-Tests. Die
Freigabe muss Datenbestand, betroffene Mailbox/Accounts, Zweck, konkrete
gesendete Felder, Datenminimierung/Redaktion, US-Verarbeitung und zulässige
Vertrags-/Datenschutzgrundlage abdecken; sie ist nicht durch einen technischen
Opt-in-Schalter ersetzbar. Nur freigegebene, begrenzte Betreff-/Preview-/
Body-Auszüge dürfen den Host verlassen, niemals Anhänge, Indexe oder
Evidenzdateien im Ganzen. OpenCode sagt Zero-Retention und kein Training für
seine Modell-Provider zu und nennt Jev nicht als Ausnahme; die öffentliche
Zen-Seite belegt damit weder ein vollständiges Gateway-Logging-Verhalten noch
die rechtliche Eignung für diese Mails. Ohne Freigabe sind nur lokale,
synthetische oder ausreichend anonymisierte Trockenläufe zulässig.

## 3. Seam-Analyse

**Zielbild:** Der Python-Classifier erledigt weiter alle explizit geregelten,
deterministischen Aufgaben. Jede danach **fachlich offene semantische
Klassifikationsfrage** kann, nach Datenfreigabe und Policy-Prüfung, an Jev
gehen. Jev ersetzt weder Katalog-/Sperrregeln noch technische Fehlerbehandlung
oder Ausführungsautorisierung. Der erste Slice bleibt ein Review-Vorschlag;
spätere, feldspezifische Automatisierung wird separat evaluiert.

Der aktuelle Classifier kennt keine allgemeine Liste offener Fragen. Besonders
`needs_reply: false` bedeutet heute oft „kein Regex-Trigger“, nicht nachweislich
„semantisch geprüft und verneint“. Ein neuer **transienter Lückenvertrag** muss
deshalb je Entscheidungsdimension `resolved`, `unresolved`, `policy_blocked`
oder `evidence_unavailable` ausweisen, mit Reason-Code, erlaubten Katalogzielen
und Bindung an die effektive Mailquelle. Nur `unresolved` geht an Jev. Diese
Instrumentierung ist Voraussetzung, damit „alle nicht erfassten Aufgaben“
prüfbar ist. Solange für `needs_reply: false` kein belastbares Negativkriterium
vorliegt, kann diese Dimension bei vielen Mails offen sein; der tatsächliche
Request-Anteil ist zu messen, nicht als klein anzunehmen.

| Offene Dimension | Heutige Signale | Mögliche Jev-Frage | Grenze |
|---|---|---|---|
| Projekt-/Topic-Zuordnung | `unknown`/`unclassified`, teils niedrige Confidence | `Choice` über zulässige Katalogziele + `other` | Gesperrte Ziele nie zur Wahl; Kataloglücke bleibt Review |
| Projekt-Details | `artifact_candidates` oder fehlende eindeutige Workpackage-/Task-/Deliverable-/Milestone-Zuordnung | `Choice` je tatsächlich offener, kataloggebundener Dimension | Nur unter validiertem Projekt; IDs und Hierarchie prüft Python |
| Topic-Details | `subtopic_candidates`, `operation_candidates`, `event_candidates` oder fehlende eindeutige Unterzuordnung | `Choice` je offener Dimension + `other` | Nur unter validiertem Topic; Cross-Kind-Konflikte nicht wegstimmen |
| Antwort-/Handlungsbedarf | `needs_reply`-Regexes, Closing-Downgrade, Sent-Check; kein expliziter Ungewissheitswert | `Noul` für konkrete Antwortbitte und ggf. getrennte Handlungsbitte | `false` ohne Trigger nicht automatisch als geklärt behandeln; Sent-Check bleibt autoritativ |
| Nachrichtentyp und Mehrfachbezug | Konservative Notification-/Spam-Muster, mögliche Projekt-/Topic-Überschneidung | `Choice` für Typ; getrennte Frage nach weiterem relevanten Bezug | Kein automatisches Löschen/Spam-Routing; Mehrfachbezug nicht in einen erzwungenen Winner pressen |

Zusätzlich denkbar: Ein `other`-/No-Match-Ergebnis als **Kataloglücken-Signal**
für menschliche Katalogpflege und eine separate Relevanz-/Prioritätsfrage für
Review-Reihenfolge. Jev soll dabei keine neuen Katalog-IDs oder Mailtexte
generieren. Dossier-Synthese ist offene Textgenerierung und bleibt außerhalb
dieses Seams; Hashes, Datumsordnung, Quoten, Identitätsprüfung, `do_not_route_if`
und Full-Read-Trigger bleiben in Python.

## 4. Empfohlener Slice 1: Opt-in-Semantik-Fallback als Review-Vorschlag

**Ort:** neues `scripts/core/jev_adjudication.py`; Aufruf im `draft`-Mode **nach**
`install_draft_attachment_evaluations` und **vor** `add_draft_contract` (nur dort
sind alle Klassifikations- und Anhangspässe abgeschlossen). `pipeline` ruft
`draft_manifest` eigenständig auf und durchläuft **nicht** diesen Draft-Hook;
Slice 1 ist deshalb ausdrücklich nur für `draft`. Eine spätere Pipeline-Anbindung
braucht einen separaten Opt-in-, Review- und Auto-Execute-Vertrag.

**Eligibility:** Mindestens eine `unresolved`-Dimension mit ausreichender,
freigegebener Evidenz. Ein explizites `review_required: false` auf einem bereits
policyentschiedenen Pfad (etwa Newsletter), fehlgeschlagene Full-Reads,
nicht verfügbare oder widersprüchliche Anhangsinventare, ungültige Handoffs und
weiter erforderliche Anhangsevidenz blockieren den Item-Request. Für eine
bereits eindeutig zugeordnete Mail dürfen dennoch andere offene Dimensionen
(z. B. Antwortbedarf oder Unterzuordnung) geprüft werden; eine bestehende
Klassifikation wird dadurch nicht neu verhandelt.

**`needs_reply`-Vorfilter (dimensionsbezogen, hoher Recall):** Die folgende
Prüfung entscheidet nur, ob die Antwortbedarfsfrage an Jev geht; sie darf andere
offene Fragen derselben Mail nicht unterdrücken. Sie verwendet die neu geschriebene
Nachricht statt zitierter Thread-Historie und unterscheidet ein bewiesenes
Negativ von einem fehlenden Regex-Treffer:

1. **Lokal geklärt:** Ein verlässlich positiver Antwort-Trigger bleibt `true`
   und benötigt für diese Dimension keinen Jev-Call. Ein verlässlich negativer
   Fall kann etwa ein bekannter No-Reply-/Automationsabsender, eine eindeutig
   reine Benachrichtigung, eine **sicher** bereits beantwortete Mail oder ein
   geprüfter reiner Dankes-/Abschlussgruß ohne neue Bitte sein. Eine bloße
   Closing-Floskel in einer sonst inhaltlichen Mail ist kein Negativbeweis;
   zweifelhafte Sent-Treffer sind es ebenso wenig. Der heutige Sent-Lookup in
   `classifier.py` läuft nur bei bereits positivem `needs_reply`; für ein
   negatives Vorfilter-Veto müsste er ausdrücklich auch bei `false` ausgeführt
   und auf eindeutige Treffer begrenzt werden.
2. **Jev-Kandidat:** Bei `needs_reply: false` ohne belastbares Negativ werden
   insbesondere direkt an die Mailbox-Person gerichtete neue Nachrichten,
   persönliche laufende Threads und schwache Bitten zur semantischen Prüfung
   vorgemerkt. Schwache Signale sind z. B. ein Fragezeichen im neuen Text,
   Bitte, Frist, Entscheidung, Freigabe, Feedback oder erwartete Rückmeldung.
   `reply_heuristics.needs_reply_review` kann dafür einen Teil der Signale
   liefern und entfernt zitierte Vorgänger; Fragezeichen und indirekte Bitten
   erfordern eine ergänzende Prüfung. Fehlen solche Wörter, beweist das
   **keinen** negativen Antwortbedarf: Bei ausreichender Evidenz und Budget
   bleiben gewöhnliche persönliche Nachrichten Jev-Kandidaten.
3. **Ungeklärt ohne Request:** Fehlt die freigegebene aktuelle Mail-Evidenz oder
   greift ein Policy-/Budget-Block, bleibt die Dimension lokal offen bzw. im
   Review. Sie wird nicht durch `false` als semantisch entschieden verbucht.

**Fragen:** Ein erster Request pro Mail mit nur den tatsächlich offenen
typisierten Fragen. Jev kann mehrere Fragen gegen denselben `state` parallel
beantworten (die bereits im Jev-Skill geroutete
[Fan-out-Referenz](../../../jev-integration/references/fan-out.md),
[Originaldokumentation](https://docs.typesafe.ai/patterns/fan-out)); zusätzliche
Fragen und Optionen verbrauchen dennoch Input-Tokens. Python
verwendet nur die für den jeweiligen Fall relevanten Antworten. Projekt-/Topic-
Optionen stammen aus einem read-only Extractor, der die aktiven
`do_not_route_if`-Prädikate und übrigen Katalogsperren anwendet;
`suppressed_candidates` sind **keine** Jev-Optionen. Das Root-Matching stellt
derzeit keinen allgemeinen Top-k-Rank als Draft-Vertrag bereit. Sind alle
zulässigen Ziele samt Beschreibungen innerhalb des API-/Token-Budgets, können
sie vollständig als `Choice`-Optionen übergeben werden
([Choice-Limit](https://docs.typesafe.ai/primitives/choice): maximal 255 Optionen
einschließlich `other`); sonst braucht es eine deterministische,
recall-geprüfte Vorauswahl oder eine hierarchische zweite Frage/Anfrage.
Fehlen zulässige Optionen, bleibt diese Dimension ohne Jev-Request im Review;
Jev darf nie eine ID erfinden. Abhängige Unterkategorien werden nur bei einem
validierten Parent benutzt; wenn dieser erst durch Jev vorgeschlagen wird, ist für deren
verbindliche Bewertung ggf. ein zweiter Request nötig. `state` enthält nur die
freigegebenen, begrenzten Mail-Auszüge und die nötigen Katalogbeschreibungen.
Pro Mail und Batch gelten feste Request-, Frage-, Options- und Token-Budgets.

**Code-owned Policy (kein automatisches Vertrauen):**

- Slice 1 ändert **niemals** `decision.kind/id/confidence/review_required`,
  `action`, `evidence`, `synthesis_targets` oder die Review-Pflicht. Je offene
  Dimension darf eine eindeutige Antwort mit gültiger Option und später
  gemessener, **dimensionsspezifischer** Schwelle nur einen Review-Vorschlag
  erzeugen; sonst lautet ihr begrenztes Ergebnis `abstain`. `Noul`-Schwellen
  werden getrennt von `Choice` kalibriert; ein `other` ist kein Routingziel.
- Das Ergebnis landet ausschließlich als additive, nicht-authoritative,
  inhaltsarme Metadaten `decision.jev_adjudication`. Bei deaktiviertem Opt-in
  bleibt das Manifest byte-identisch; bei aktiviertem Opt-in bleiben Routing und
  Aktionen identisch, das Manifest enthält aber das neue Metadatenfeld.
- Service-/API-Fehler werden item-lokal als
  `decision.jev_adjudication.status: "unavailable"` mit sicherem Reason-Code
  dokumentiert. Das Item bleibt unverändert im Review; es erfolgt keine stille
  Ersatzklassifikation.
- Ein **späteres automatisches Anwenden** ist je Dimension ein eigener
  FR-Scope. Routingänderungen müssen aus validierten Vorschlägen ein
  konsistentes Item einschließlich `decision`, `action`, `evidence` und
  `synthesis_targets` über eine vertrauenswürdige Policy-/Materialisierungsfunktion
  erzeugen und den Execute-Gate erneut prüfen. Ein bloßes Überschreiben von
  `kind/id/confidence` wäre unzulässig. Für `needs_reply` ist insbesondere ein
  falsch negatives „keine Antwort nötig“ gesondert zu bewerten; ein Jev-Fehler
  darf keine bestehende Antwortpflicht löschen.

**Workspace-Opt-in (vorgeschlagener Konfigurationsvertrag):** Die vorhandene,
credentials-freie `.agents/mail-desk-backend.json` im **konsumierenden
Workspace** soll Jev für Slice 1 mitsteuern. Der heutige Loader akzeptiert in
Schema 1 exakt `schema_version`, `backend`, `account`; ein zusätzliches Feld
funktioniert derzeit **nicht**. Das spätere FR muss daher ein validiertes,
rückwärtskompatibles Schema 2 einführen. Beispiel für Schema 2:

```json
{
  "schema_version": 2,
  "backend": "himalaya",
  "account": null,
  "jev": {
    "enabled": true,
    "max_requests_per_batch": 10
  }
}
```

Schema-1-Dateien bleiben unverändert gültig und bedeuten **Jev aus**. In
Schema 2 bedeutet `jev.enabled: false` ebenfalls aus: kein Jev-Client, Cache-
oder Netzwerkzugriff, byte-identisches Draft-Manifest und unveränderte
Klassifikation. Die bisherige Backend-/Account-Bindung und ihre strenge
Validierung bleiben erhalten. `jev.enabled: true` gilt ausschließlich für den
direkten `draft`-Mode und ist **keine** Datenschutzfreigabe. Schema und Typen
werden vor dem ersten Mailbox-Read streng geprüft; unbekannte Felder,
Nicht-Booleans und ein ungültiges Budget sind Konfigurationsfehler. Das Budget
darf eine fest eingebaute Sicherheitsobergrenze nur senken. Endpoint und
Produktionsmodell bleiben im Code gepinnt; der API-Key bleibt ausschließlich im
Host-Env. Ohne gebundene Human-Freigabe für den konfigurierten Account und die
Outbound-Felder wird ein aktivierter Lauf vor dem ersten externen Request
fail-loud gestoppt.

Der Runner kann auch per `--input` eine JSON-Datei pro Lauf lesen; direkte
`--draft`-Aufrufe nutzen jedoch ihre eigene Konfiguration, und erfolgreiche
Input-Läufe löschen ihre Eingabedatei standardmäßig. In Slice 1 darf deshalb
weder ein CLI-Flag noch per-Run-JSON `jev.enabled` aus der Workspace-Datei
übersteuern. Der gemeinsame Loader muss Schema 1 weiterhin als deaktiviert
verstehen und in Schema 2 den Jev-Block vor jedem Mailbox-Zugriff validieren.

**Guardrails:**

1. **Default AUS:** Schema 1 oder Schema 2 mit `jev.enabled: false` lassen
   Jev aus; nur Schema 2 mit `jev.enabled: true` öffnet den Seam im direkten
   `draft`-Mode. Deaktivierte Läufe bleiben byte-identisch.
2. **Credentials:** nur `OPENCODE_API_KEY` aus Host-Env; fehlender Key = Seam
   unavailable, nie Absturz.
3. **Caching & Wiederholung:** Der Cache-Key bindet die kanonische vollständige
   Outbound-Anfrage (State **und** Fragen/Kriterien/Kandidaten), Endpoint,
   angefragtes Modell, Account-/Folder-/Message-Identität und die Version der
   lokalen Vorschlags-Policy. Nur begrenzte, validierte Antwortfelder, kein
   Roh-Mailtext, Key oder vollständiger Request werden unter `data/` mit
   Aufbewahrungsgrenze gespeichert; Writes sind lock-gebunden und atomar.
   Cache-Hits vermeiden wiederholte Requests bestmöglich. Ein Absturz zwischen
   API-Erfolg und Cache-Write kann erneut senden: **Exactly-once wird nicht
   behauptet**.
4. **Quota/Timeout:** maximale Jev-Calls je Item und Batch (konfigurierbar,
   Default klein), Frage-/Options-/Token-Limit und hartes Timeout je Request
   samt Gesamtbudget; Budget-Überschreitung → unverändertes Review-Verhalten.
5. **Modell-Pinning:** Zen-ID `jev-1.13` für eine spätere Produktion festlegen;
   Upgrade erst nach erneuter Evaluierung (§6). Die Antwort-`model`-Zeichenkette
   wird vor Nutzung geprüft und im Cache abgelegt. Die genaue Zen-Abbildung auf
   Vendor-Version/Gewichte bleibt bis zu einer Live-Prüfung offen (§7).
6. **Transport & Antwort:** fester HTTPS-Host/Endpoint, keine Redirects auf
   andere Hosts, feste Request-/Response-Größen und Gesamtlaufzeit begrenzen.
   Antwort nur nach strikter Prüfung von Modell, allen erwarteten Frage-IDs,
   Antworttypen, zulässigen `choice`-Optionen und endlichen `Noul`-/
   Wahrscheinlichkeits-/Confidence-Werten in `[0, 1]` verwenden. Fehlende oder
   fehlerhafte Felder und unbekannte HTTP-Status bleiben `unavailable` für die
   betroffene Dimension; Logs/Fehlermetadaten enthalten weder Payload noch Key
   noch rohe Provider-Fehlertexte.

## 5. Client-Entscheidung: stdlib `urllib`, nicht das SDK

Der offizielle `typesafe-sdk` (v0.7.2 geprüft) zieht `httpx2` + `pydantic` +
`tenacity` als Dependencies und kollidiert damit mit der stdlib-only-Philosophie
des Bundles. Slice 1 benötigt einen festen Endpoint mit `Choice`- und `Noul`-
Fragen in einem kleinen JSON-Vertrag; `urllib` mit explizitem Schema-Check,
begrenzter Gesamtzeit und bounded Retry ist dafür tragfähig. 401/422 vs.
429/529 sind die **Vendor**-Fehlerfälle; Zen-Status und `retry-after` sind noch
nicht verifiziert. Bekannte Retry-Fälle erhalten ein hartes Gesamtbudget,
unbekannte Fehler gehen ohne
Rohtext im Manifest auf `unavailable`. Der Client wird als injizierbare
Dependency verdrahtet (existierendes Muster: `full_reader`-Injektion im
Draft-Pfad), damit Tests hermetisch bleiben. Das SDK bleibt eine Option,
falls spätere Seams die Retry-/Typelogik aufwerten; ein Wechsel des SDK auf
Zen per `TYPESAFE_BASE_URL` muss zuvor gegen den tatsächlichen Zen-Pfad und
Auth-Vertrag getestet werden.

## 6. Evaluationsplan (vor jeder Aktivierung)

- **Datenbasis:** zuerst synthetische oder ausreichend anonymisierte Fälle
  lokal; historische boku-user-Batches über **alle ausgewiesenen offenen
  Dimensionen** nur nach dem Human-Approval-Gate aus §2. Für jede bewertete
  Dimension braucht es ein unabhängiges manuelles Review-Label, einschließlich
  echter Negativfälle und Kataloglücken. Zielmetriken **je Dimension und
  Sprachgruppe**: Lückenabdeckung, Vorschlagsabdeckung, falsche Vorschläge,
  Abstention-Rate und potenzielle Review-Volumen-Ersparnis. Ein tatsächlich
  sinkendes Review-Volumen wird in Slice 1 nicht behauptet, da der Mensch
  weiterhin entscheidet.
- **Modell:** `jev-1.13-free` kann nach Freigabe für eine kostengünstige
  Exploration dienen, solange das zeitlich begrenzte Angebot verfügbar ist.
  Eine gleiche Antwort-`model`-Zeichenkette belegt keine gleiche
  Fehlerverteilung/Serving-Konfiguration wie `jev-1.13`. Vor jeder späteren
  automatischen Aktion braucht der bezahlte Produktionspfad einen eigenen,
  gelabelten Holdout und eine Schwellenwert-Kalibrierung.
- **Sprache:** Mails sind überwiegend deutsch; Vendor dokumentiert Englisch als
  stärkste Sprache. Die Evaluierung läuft daher primär auf deutschsprachigen
  Fällen (Skill-Anforderung: Zielsprache testen).
- **Adversarial:** Mailtexte sind untrusted; getestet wird mit realistischen
  hostilen Inhalten (Prompt-Injection im Betreff/Body) vor jeder Auto-Aktion.
- **Kosten:** ~3k Input-Tokens/Mail ≈ $0,00013 pro Mail bei `jev-1.13`
  (Output frei) ist nur eine Beispielrechnung, kein Budget für den breiteren
  Fan-out. Zusätzliche Fragen, Katalogoptionen und ggf. ein zweiter
  Hierarchie-Request erhöhen Input-Tokens. Gemessen werden Kosten pro Mail,
  pro geklärter Dimension und pro vermiedenem Review-Fall; unnötige Fragen
  werden weggelassen. Das Free-Modell ist nur für begrenzte Zeit kostenlos.
- **`needs_reply`-Vorfilter:** Auf einem gelabelten, deutschsprachigen Holdout
  die übersehenen Antwortbitten (False Negatives) und Jev-Calls pro 100 Mails
  gegen „alle elegiblen Mails an Jev“ messen. Fälle mit indirekter Bitte ohne
  Signalwort, Frage in zitierter Historie, Dank plus neuer Aufgabe, unsicherem
  Sent-Treffer und automatischem Absender müssen enthalten sein. Ausschlüsse
  erst nach nachgewiesen hohem Recall als `resolved` behandeln; bis dahin
  konservativ an Jev geben oder sichtbar im Review belassen.

## 7. Offene Punkte (für das spätere FR-Verfahren zu klären)

1. Transienter Lückenvertrag pro Dimension: Wann sind `needs_reply: false`,
   fehlende Detail-IDs oder `unknown` fachlich offen statt eindeutig negativ?
   Exakte `state`-/Fragenformulierung, zulässige Kandidaten, Frage-/Options-
   Limits und Ausschlussgründe.
2. Cache-Dateiname/Schema, Aufbewahrung, Lock-/Atomik-Vertrag und
   Wiederholungssemantik in der Data-Zone.
3. Begrenztes Metadatenschema `decision.jev_adjudication` **je Dimension**
   (Status, Vorschlag, Modell, sichere Reason-Codes; keine Mailtexte oder
   Provider-Fehlertexte).
4. Schema-2-Migration der bestehenden `.agents/mail-desk-backend.json` im
   späteren FR-Record fixieren: Schema-1-Kompatibilität, strenge Jev-Block-
   Validierung, Maximalbudget, Freigabe-Bindung und Konfliktverhalten mit
   per-Run-JSON. Slice 1 erhält keinen CLI-Enable-Override; `inspect` und
   `pipeline` bleiben ohne Jev.
5. Zen-Rate-Limits, Fehler-/Retry-Vertrag, Antwort-`model`-Zeichenkette und
   Abbildung von `jev-1.13-free`/`jev-1.13` (live nur nach Freigabe prüfen).
6. Eigenständiger `pipeline`-Scope mit ausdrücklichem Review- und Auto-Execute-
   Gate, falls später gewünscht; Slice 1 enthält ihn nicht.
7. Datengrenzen-Gate: dokumentierte Freigabe vor **jedem ersten externen**
   Testlauf mit personenbezogenen Mailinhalten, einschließlich Replay.
8. Feldspezifische Materialisierungs- und Auto-Action-Verträge samt
   unabhängiger bezahlter Holdout-Evaluation vor einem späteren Upgrade von
   Review-Vorschlägen zu automatischen Entscheidungen.
9. Ob ein Parent-und-Detail-Fall innerhalb des Kontextbudgets mit einem
   spekulativen Fan-out lösbar ist oder einen zweiten Request braucht; Kosten
   und Fehlerwirkung beider Varianten messen.
10. Konkrete Negativkriterien des `needs_reply`-Vorfilters, unabhängiger
    Sent-Lookup für bisher negative Items und zulässige False-Negative-Rate
    gegen das Request-Budget anhand gelabelter Mail-Batches festlegen.

## 8. Voraussetzungen

- Ein freigegebener OpenCode-Zen-Key muss für den Prozess im Host-Env
  (`OPENCODE_API_KEY`) gesetzt sein; die bloße Existenz in `auth.json` reicht nicht.
- Nur die ausdrücklich aktivierte Jev-Sektion in Schema 2 der bestehenden
  `.agents/mail-desk-backend.json` ist der Slice-1-Opt-in-Schalter. Schema 1
  und `jev.enabled: false` lassen Jev vollständig aus.
- Für Live-Evaluierung mit Mailinhalten ist zusätzlich das Human-Approval-Gate
  aus §2 erfüllt. `jev-1.13-free` ist nur vorübergehend kostenlos;
  `jev-1.13` ist der vorgesehene Produktionspfad nach eigener Kalibrierung.
- Keine Code-Änderungen in diesem Dokument-Zustand; Mail-Desk bleibt unverändert
  netzfrei bis ein FR-Record das Verfahren eröffnet.
