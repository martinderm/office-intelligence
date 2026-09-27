# Jev-Integration für Mail-Desk — Konzeptanalyse

**Status:** Konzeptdokument (kein Feature-Record, keine Umsetzung; Vorarbeit für
ein potenzielles FR-Record-Verfahren). Erstellt 2026-09-27.
**Vertragshistorie:** Analyse gegen den Shared Skill
[`jev-integration`](../../skills/jev-integration/SKILL.md) (Submodule), dessen
Referenzen per Frische-Checker am 2026-09-27 als 18/18 frisch verifiziert wurden;
Live-Quellen ([TypeSafe-Doku](https://docs.typesafe.ai/llms.txt),
[OpenCode-Zen-Doku](https://opencode.ai/docs/zen/)) wurden am selben Tag direkt
geprüft. Ticket-IDs sind in System Map und `docs/features/_archive.md` kanonisch.

---

## 1. Was Jev ist

Jev (TypeSafe System One) bewertet einen gelieferten `state` gegen **typisierte
Fragen** und liefert strukturierte Antworten statt generierten Texts: `Choice`
(eine Option aus Menge + Wahrscheinlichkeitsverteilung + Confidence), `Score`
(geordnete Rubrik), `Noul` (Ja-Wahrscheinlichkeit). Der AnwendungCode behält
Workflow, Arithmetik, Policy, Schwellenwerte und Aktionen; Jev liefert nur kleine,
unabhängig reviewbare TeilmaleUrteile über unstrukturierte Inhalte. Modellstand:
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
| Privacy | Vendor-Policy | Zero-Retention dokumentiert; Jev nicht als Ausnahme gelistet |

Der Zen-Endpoint ist live verifiziert (Modellliste enthält `jev-1.13` und
`jev-1.13-free`; POST ohne Key → 401 mit Bearer-Anforderung). Ein OpenCode-Zen-Key
ist auf dieser Maschine bereits in OpenCodes `auth.json` vorhanden (Existenz
geprüft, Wert nie gelesen). Die Evaluierungsphase kann daher **kostenfrei über
`jev-1.13-free`** laufen; ein früherer Blocker (fehlender API-Key) entfällt.

**Credential-Disziplin (verbindlich):** Der Runtime-Adapter liest den Key
ausschließlich aus der Host-Umgebung (`OPENCODE_API_KEY`). Das Lesen von
`auth.json` oder das Kopieren von Credentials in den Workspace bleibt verboten
(Dual-Evidence-/Credentials-Grenzen des Bundles). Die Workspace-Bindung
`.agents/mail-desk-backend.json` bleibt credentials-frei; ein Jev-Zugriff ist
kein Mailbox-Backend und ändert daran nichts.

**Datengrenzen (verbindlich):** Mailinhalte sind personenbezogen. Vor der ersten
Aktivierung ist ein explizites Human-Approval-Gate Pflicht (was wird gesendet:
Betreff/Preview/Body-Verdichtung, niemals Anhänge, Indexe oder Evidenzdateien im
Ganzen). Zen dokumentiert Zero-Retention für Jev; das ersetzt die
Workspace-seitige Freigabepflicht nicht.

## 3. Seam-Analyse

Der Mail-Desk-Classifier ist vollständig lokal-deterministisch (Katalog-Scores,
Regex-Trigger, keine Laufzeit-API-Aufrufe). Vier Kandidatenseams:

| # | Seam | Heute | Bewertung |
|---|---|---|---|
| 1 | **Unclassified-Disambiguation** | Items mit `kind: "unknown"` / `confidence: "low"` landen deterministisch in Review (`pending-review`); Ties via `core/matching/ambiguity.py` | **Empfohlen.** Klarer Nutzen (Review-Last), natürliche fail-closed-Form: Upgrade nur bei eindeutigem Winner + hoher Confidence, sonst byte-identisches Altverhalten |
| 2 | `needs_reply`-Assist | Regex-Trigger + `downgrade_if_closing` (MD-R8-Muster) | Später als Fan-out-Zusatzfrage (gleicher State, gleicher Request); nie Auto-Assert von Reply-Anforderungen |
| 3 | Spam/Notification-Regexes | Hartcodierte konservative Muster | Geringer ROI; bestehende Regexes sind sicherheitsseitig ok |
| 4 | Full-Read-Trigger | Deterministisch | **Nicht geeignet** — Netzwerkcall je Preview wäre neuer Failure-Path ohne Classification-Nutzen |

Nicht geeignet: Dossier-Synthese (offene Textgenerierung — explizit nicht Jevs
Funktion; dort ist der LLM-Handoff-Vertrag etabliert); alles Hash-, Datums-,
Quoten- und Indexgebundene (bleibt im Code).

## 4. Empfohlener Slice 1: Opt-in-Disambiguation (Seam 1)

**Ort:** neues `scripts/core/jev_adjudication.py`; Aufruf im `draft`-Mode **nach**
`install_draft_attachment_evaluations` und **vor** `add_draft_contract` (nur dort
sind alle Klassifikations- und Anhangspässe abgeschlossen). Der `pipeline`-Modus
erbt das Verhalten über denselben Draft-Pfad.

**Frage:** ein `Choice` über die deterministischen Kandidaten der Mail
(Top-k aus Score-Ranking bzw. `suppressed_candidates`) plus expliziter
`other`-Option; `state` = untrusted Betreff + Preview + Body-Verdichtung +
Kandidatenliste (IDs/Titel). Eine Mail je Request; Batching je Batch-Lauf mit
Quota-Cap.

**Code-owned Policy (kein automatisches Vertrauen):**

- Upgrade einer `unknown`-Klassifikation nur wenn: Antwort eindeutig,
  Option ≠ `other`, Confidence ≥ Schwelle (Schwelle wird gemessen, nie
  angenommen — siehe §6), und Zielkatalogeintrag existiert im Workspace-Katalog.
- Sonst: **byte-identisches heutiges Verhalten** (fail-closed in Review).
- Das Ergebnis landet ausschließlich als additive, nicht-authoritative
  Metadaten `decision.jev_adjudication` (analog zum etablierten
  `untrusted_external_text`-Muster); bestehende Felder werden nie überschrieben.
- Service-/API-Fehler sind kein Grund zur stillen Fallback-Klassifikation:
  Fehler werden item-lokal als `jev_adjudication.status: "unavailable"`
  dokumentiert, das Item verbleibt unverändert in Review.

**Guardrails:**

1. **Default AUS:** Konfigurationsfeld (z. B. `evaluate_jev: false`) nach dem
   Muster der `evaluate_attachments`-Option; ohne Opt-in ist kein Verhalten
   sichtbar (byte-identisch).
2. **Credentials:** nur `OPENCODE_API_KEY` aus Host-Env; fehlender Key = Seam
   unavailable, nie Absturz.
3. **Caching & Idempotenz:** Antwort-Cache keyed über (normalisierte Message-ID,
   State-Content-Hash, Modellversion) in der Data-Zone `data/` — Re-Runs kosten
   keine doppelten Requests (Muster des `content_hash`-Bindungs aus dem
   Anhangs-Handoff).
4. **Quota/Timeout:** maximale Jev-Calls je Batch (konfigurierbar, Default klein)
   und hartes Timeout je Request; Budget-Überschreitung → unverändertes
   Review-Verhalten.
5. **Modell-Pinning:** Zen-ID `jev-1.13` gepinnt; Upgrade erst nach erneuter
   Evaluierung (§6). Die Antwort-`model`-Zeichenkette wird im Cache abgelegt und
   verifiziert (offener Punkt, siehe §7).

## 5. Client-Entscheidung: stdlib `urllib`, nicht das SDK

Der offizielle `typesafe-sdk` (v0.7.2 geprüft) zieht `httpx2` + `pydantic` +
`tenacity` als Dependencies und kollidiert damit mit der stdlib-only-Philosophie
des Bundles. Der HTTP-Vertrag ist winzig (ein Endpoint, drei Fragetypen, vier
Fehlercodes) und mit `urllib` + eigenem bounded Retry (Backoff, `retry-after`
falls vorhanden, getrenntes Handling von 401/422 vs. 429/529) abbildbar. Der
Client wird als injizierbare Dependency verdrahtet (existierendes Muster:
`full_reader`-Injektion im Draft-Pfad), damit Tests hermetisch bleiben. Das SDK
bleibt eine Option (`TYPESAFE_BASE_URL`-Umschaltung auf Zen), falls spätere
Seams die Retry-/Typelogik aufwerten.

## 6. Evaluationsplan (vor jeder Aktivierung)

- **Datenbasis:** zurückliegende boku-user-Batches mit `unknown`/`low`-Items;
  Zielmetriken: Anteil automatisch auflösbarer Items, Falsch-Upgrades (gegen
  manuelle Review-Entscheidungen), Review-Volumen-Ersparnis.
- **Modell:** Evaluierung über `jev-1.13-free` (kostenfrei); Produktionspinning
  `jev-1.13` erst nach bestätigter Übereinstimmung (Antwort-`model`-Feld
  verifizieren).
- **Sprache:** Mails sind überwiegend deutsch; Vendor dokumentiert Englisch als
  stärkste Sprache. Die Evaluierung läuft daher primär auf deutschsprachigen
  Fällen (Skill-Anforderung: Zielsprache testen).
- **Adversarial:** Mailtexte sind untrusted; getestet wird mit realistischen
  hostilen Inhalten (Prompt-Injection im Betreff/Body) vor jeder Auto-Aktion.
- **Kosten:** ~3k Input-Tokens/Mail ≈ $0,00013 pro Mail bei `jev-1.13`
  (Output frei) — vernachlässigbar; Free-Modus macht die Evaluierung gratis.

## 7. Offene Punkte (für das spätere FR-Verfahren zu klären)

1. Exakte `state`-/Fragenformulierung und Kandidatenlimit (Top-k).
2. Cache-Dateiname/Schema in der Data-Zone und Aufbewahrung.
3. Metadatenschema `decision.jev_adjudication` (Felder, Fehlerräume).
4. CLI-/Konfig-Flagname (`evaluate_jev` vs. `jev_adjudication`) und
   Kompatibilität mit `--draft`/`--inspect`-Flagverträgen.
5. Zen-Rate-Limits und Antwort-`model`-Zeichenkette (live mit Key verifizieren).
6. Ob `pipeline`-Läufe denselben Seam ohne Zusatzflag erben (Review-Pflicht im
   Record).
7. Datengrenzen-Gate: Human-Approval-Fluss für personenbezogene Mailinhalte vor
   erster Aktivierung.

## 8. Voraussetzungen

- OpenCode-Zen-Key im Host-Env (`OPENCODE_API_KEY`) — lokal vorhanden.
- Kein weiterer Key nötig (`jev-1.13-free` für Evaluierung, `jev-1.13` für
  Produktion).
- Keine Code-Änderungen in diesem Dokument-Zustand; Mail-Desk bleibt unverändert
  netzfrei bis ein FR-Record das Verfahren eröffnet.