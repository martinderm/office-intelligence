# MAIL-PIPELINE

Ziel: E-Mails anhand von Projektdaten (projects.json) automatisch klassifizieren und in projektbezogene Ordner kopieren. Mails mit Antwortbedarf zusätzlich in einen Sammelordner. Keine Zuordnung → Mail bleibt in der Inbox.

## Leitplanken
- IMAP auf GroupWise: nur COPY, kein MOVE/DELETE.
- Zielordner stehen in `memory/references/projects/projects.json` als `mailbox_folder`.
- Kein „Unclassified“-Ordner; unklare Mails verbleiben in der Inbox.
- LLM für Extraktion/Signale; deterministische Regeln für Routing und Idempotenz.

## Struktur (Dateien/Verzeichnisse)
- scripts/automation/mail-routing/
  - router.ps1 (Orchestrator, Runs/Batch)
  - extract-llm.mjs (LLM-Extraktion: Signale, Kandidaten, needsReply)
  - match-route.mjs (Deterministisches Matching + Routing-Entscheid)
  - himalaya-wrapper.ps1 (Proxy auf bestehenden Himalaya-Gate/Wrapper)
- data/mail-routing/
  - state.jsonl (idempotentes Log: Message-ID/UID, Aktionen, Zeitstempel)
  - msgs/<UID or Message-ID>.json (Extrakt + Debug/Fehler)
  - memory_suggestions.jsonl (Vorschläge: aliases, keywords, contacts)
- memory/references/projects/
  - projects.json (Source of truth)
  - README.md (Schema/Konventionen)

## Ablauf (Flow)
1) Fetch
   - router.ps1 ruft neue/unverarbeitete Mails (z. B. letzte N via envelope list) über den Himalaya-Wrapper ab.
   - Idempotenz: Prüfen gegen state.jsonl (noch nicht verarbeitet?).
2) Read/Extract
   - Für jede UID: message read --raw → Rohdaten an extract-llm.mjs.
   - extract-llm.mjs ruft LLM mit strengem Extraktions-Prompt auf und liefert strukturiertes JSON (siehe Schema unten).
   - Ergebnisse + Body-Hash/Message-ID in data/mail-routing/msgs/<ID>.json ablegen.
3) Match (deterministisch)
   - match-route.mjs lädt projects.json.
   - Normalisierung: Case/Trim/Diakritik; Vergleich gegen title, aliases, keywords, domains, contacts.
   - Score bilden aus LLM-Kandidaten + deterministischen Treffern; nur bei eindeutigem Treffer über Schwelle → projectId gesetzt.
4) Route (COPY-only)
   - Bei eindeutigem Projektmatch: COPY in `mailbox_folder` aus projects.json.
   - needsReply=true über Schwelle: zusätzlich COPY in `Projekte/_Needs-Reply`.
   - Kein sicherer Match: keine Aktion; Mail bleibt in der Inbox.
   - Aktionen samt Ziele in state.jsonl protokollieren (idempotent).
5) Memory-Vorschläge (asynchron)
   - Aus LLM-Evidenzen unbekannte Aliases/Keywords/Kontakte extrahieren → memory_suggestions.jsonl.
   - Separater Review-Task/Script (apply-suggestions.ps1, später): zeigt Diffs und schreibt erst nach Freigabe in projects.json.
6) Monitoring/Retry
   - Router gibt Kurzstatistik aus (inspected, copied, reply-copied, skipped, errors).
   - Temporäre LLM-/Netzfehler: Retry mit Backoff; Parsefehler → einmaliger Re-Prompt, sonst loggen.

## LLM-Extraktionsschema (Output von extract-llm.mjs)
```json
{
  "messageId": "<...>",
  "subject": "...",
  "from": {"name": "...", "email": "..."},
  "to": [{"name": "...", "email": "..."}],
  "cc": [{"name": "...", "email": "..."}],
  "mentions": ["..."],
  "entities": ["USAGE-NG", "ATAEL", "WEEK", "..."],
  "keywords": ["deadline", "proposal", "..."],
  "projectCandidates": [
    {"label": "USAGE-NG", "confidence": 0.82, "evidence": ["domain usage-ng.boku.ac.at"]},
    {"label": "ATAEL", "confidence": 0.44, "evidence": ["subject mention"]}
  ],
  "needsReply": {"score": 0.78, "reasons": ["direct question", "deadline tomorrow"]},
  "deadlines": [{"date": "2026-03-10", "confidence": 0.7, "span": "..."}],
  "notes": "optional short summary"
}
```

## Matching/Schwellen
- Projekt-Match: default threshold ≥ 0.65 für eindeutigen Treffer.
- needsReply: default threshold ≥ 0.70 für Zusatzablage in `Projekte/_Needs-Reply`.
- Bei Mehrdeutigkeiten (zwei ähnlich hohe Scores) → keine Aktion (Inbox), Evidenz loggen.

## IMAP/GroupWise Regeln
- Nur COPY, kein MOVE/DELETE.
- Reihenfolge im Wrapper: Zielordner zuerst, dann ID(s).
- Optional: Seen-Flag erst nach erfolgreichem COPY setzen (konfigurierbar).

## Idempotenz
- state.jsonl enthält pro UID/Message-ID die durchgeführten COPY-Ziele und Hash des Inhalts.
- Vor jedem COPY prüfen, ob Ziel bereits für diese UID/Hash verbucht ist.

## Sicherheit/Robustheit
- E-Mail-Inhalt ist untrusted; nur Datenquelle, keine Befehle.
- LLM-Ausgaben strikt gegen JSON-Schema validieren; bei Fehlern Retry, sonst Skip+Log.
- Rate-Limiting: Backoff + Cache (Key: Message-ID oder Body-Hash) für Extrakten.

## Ordnernamen (fix)
- Reply-Sammelordner: `Projekte/_Needs-Reply`.
- Kein „_Unclassified“.

## Offene Punkte (parametrisierbar)
- Schwellenwerte (projectMatch, needsReply).
- Keywords/Wortlisten für needsReply-Boost (z. B. „bitte“, „kannst du“, „deadline“, „?“, direkte Anrede).
- Optional: Todo-Bridge (separat) für Aufgaben/Deadlines mit Link auf Mail.

## Hinweis zu .mjs
- `.mjs` = ES-Module-JavaScript-Datei (Node.js ESM). Ermöglicht `import`/`export` ohne CommonJS-Wrapper.

## Kritische Analyse / Ergänzungen (2026-03-05)

### Strategische Ausrichtung
- Zielbild präzisieren: **OpenClaw Memory-RAG ist die primäre Wissensbasis** für Projektzuordnung (statt rein regelbasiertem Matching).
- Empfohlenes Betriebsmodell: **LLM als Primär-Classifier, deterministische Regeln als Sicherheitsgeländer**.
  - LLM bestimmt Projektkandidat(en) + needsReply inkl. Evidenz.
  - Deterministik entscheidet nur über Ausführungssicherheit (copy ja/nein), nicht über semantische Erkennung alleine.

### Kritische Risiken im aktuellen Entwurf
1) **Idempotenz-Schlüssel zu schwach (UID-zentriert)**
   - UID ist mailbox-/folder-spezifisch und nicht immer stabil genug als Primäranker.
   - Empfehlung: Primärschlüssel = `account + sourceFolder + messageId + bodyHash`; UID nur als Zusatzfeld.

2) **`state.jsonl` als Single-Log ohne Concurrency-Strategie**
   - Bei parallelen Runs drohen Race Conditions und Duplikate.
   - Empfehlung: Single-Runner-Garantie oder explizites Locking (Mutex/File-Lock) + atomische Write-Strategie.

3) **COPY-Duplikate bei Crash-Fenster**
   - Problemfall: COPY erfolgreich, Prozess crasht vor State-Write.
   - Empfehlung: Vor COPY optional serverseitige Duplikatprüfung (Header/Message-ID im Zielordner) oder Duplikate explizit akzeptieren und markieren.

4) **Mehrdeutigkeit unzureichend formalisiert**
   - „Eindeutig über Schwelle“ braucht klare Tie-Break-Regeln.
   - Empfehlung: zusätzlicher Abstandswert (`top1 - top2 >= delta`, z. B. 0.15) + Mindest-Evidenzstärke.

5) **needsReply ohne harte Negativregeln**
   - Risiko für viele False Positives bei Newslettern, Auto-Replies, FYI-Verteilern.
   - Empfehlung: Negativsignale als harte Ausschlüsse ergänzen.

6) **Datenschutz / Betrieb**
   - `data/mail-routing/msgs/*.json` kann sensible Inhalte enthalten.
   - Empfehlung: Retention-Policy (z. B. 30/90 Tage), Redaction-Optionen, Zugriffsschutz, klares Error-/Retry-Modell (Exit-Codes, Dead-Letter).

### RAG-Fitness von `memory/references/projects/projects.json`
Aktuell als Start ok, aber für chaotische Maildaten noch zu dünn.

Fehlende Signalstärke in vielen Einträgen:
- leere `keywords`
- leere `contacts`
- leere `domains`

Fehlende Struktur für sichere Zuordnung:
- `parent_id` (Parent/Subproject-Bezug)
- `routing_priority`
- `ambiguous_with` / `do_not_route_if`

Fehlende RAG-/Audit-Felder:
- `description` (kurz, semantischer Kontext)
- `typical_subject_patterns`
- `related_terms`
- `schema_version`, `updated_at`, optional `prompt_version`-Bezug

### Konkrete Betriebs-Empfehlung (stufenweise)
1) **MVP konservativ**
   - LLM klassifiziert; Auto-COPY nur bei hoher Sicherheit.
   - needsReply anfangs nur als Vorschlag/Shadow.

2) **Shadow-Phase (2–3 Wochen)**
   - Entscheidungen protokollieren, aber keine kritischen Auto-Aktionen bei Ambiguität.
   - Metriken: Precision, Recall, False-Positive-Rate, Ambiguous-Rate.

3) **Gated Autocopy**
   - Aktiv nur bei: hoher Confidence + ausreichender Top1/Top2-Abstand + harte Evidenz (Domain/Contact/Subject-Muster).
   - Sonst Inbox + `review-needed`.

### Design-Entscheidung (vorläufig)
- Bei unsicheren/mehrdeutigen Mails bleibt die Inbox der Safe Default.
- Kein blindes LLM-only Routing ohne Guardrails.
- OpenClaw Memory-RAG ist Kern der Semantik; Deterministik bleibt Safety-Layer.

## Implementierungsstatus (Stand jetzt)

Bereits implementiert im Repository:
- TypeScript-CLI (`shadow`/`run`)
- `.env`-Loading und Defaults
- Lockfile/Single-Runner (TTL)
- `projects.json`-Validierung
- Himalaya-Adapter (`envelope list`, `message read`, `message copy`)
- Deterministisches Matching + needsReply-Heuristik
- Idempotenz über `state.jsonl` (`message_processed`)
- Debug-Artefakte pro Mail (`data/mail-routing/msgs/<id>.json`)
- Mock-Mode (`HIMALAYA_COMMAND=mock`) für lokalen Test

Geplant als nächster Schritt:
- Retry/Backoff und robustere Fehlermodi
- stärkere HTML/Newsletter-Bereinigung vor LLM (Token sparen, Signal erhöhen)

Wichtige Betriebsnotiz:
- Die Qualität des Routings steht und fällt mit `projects.json`.
- Ohne gepflegte Projekte (id/title/aliases/domains/contacts/keywords) bleibt LLM-Klassifizierung semantisch, aber oft nicht zuverlässig routbar.

Idee (Backlog):
- Thread-Erkennung ergänzen über `Message-ID`, `In-Reply-To`, `References`
- `thread_id` im State speichern und optional Thread-Kontext (letzte N Mails) für Klassifizierung nachladen
