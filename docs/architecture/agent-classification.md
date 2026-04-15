# Agent-based Mail Classification Architecture

## Status

Geplantes Zielbild für `mail-processor` innerhalb von `office-intelligence`.

Entscheidung: **Variante A**. Modellgestützte Mail-Auswertung läuft künftig über ein dediziertes OpenClaw-Plugin-Tool und nicht mehr direkt über AcademicAI-Aufrufe im `mail-processor`.

## Motivation

Der direkte Modellzugriff über AcademicAI im `mail-processor` hat sich als mühsam erwiesen:

- zu viel Modell-/Adapterlogik im Betriebsprozess
- unnötige Kopplung zwischen Mail-Pipeline und LLM-Backend
- höherer Aufwand bei Prompting, Parsing und Fehlersuche
- schlechtere Austauschbarkeit des Modellpfads

Ziel ist eine klarere Trennung zwischen:

- **deterministischem Betrieb** (`mail-processor`)
- **interpretativer Klassifikation** (OpenClaw-Tool)

## Architekturprinzip

`mail-processor` bleibt der Orchestrator und alleinige Instanz für Betriebslogik.

Das OpenClaw-Tool übernimmt nur die inhaltliche Auswertung von Mailtexten und liefert strikt strukturiertes JSON zurück.

### Verantwortlichkeiten

#### `mail-processor`

Verantwortlich für:

- Mailzugriff über Himalaya / IMAP
- Envelope-Auswahl
- MIME-Export / Message-Read
- Sanitizing / Textaufbereitung
- Kataloge laden (`projects.json`, `topics.json`)
- heuristische Vorselektion von Kandidaten
- Locking, Retry, Idempotenz, State
- Shadow-/Run-Modus
- finale Routing-Entscheidung
- tatsächliche Mail-Operationen (copy/move)
- Logging und Artefaktpersistenz

#### OpenClaw-Tool `mail_intelligence.classify`

Verantwortlich für:

- Promptaufbau aus vorbereitetem Input
- genau einen modellgestützten Analyseaufruf
- Validierung und Normalisierung des Modelloutputs
- Rückgabe eines festen JSON-Ergebnisses

Nicht verantwortlich für:

- Mailzugriff
- Routing
- State-Änderungen
- Dateioperationen im `mail-processor`
- autonome Agentenentscheidungen außerhalb des Vertrags

## Ziel-Komponenten

### 1. `mail-processor`

Bestehendes Repo bleibt der operative Kern.

Geplante interne Strukturergänzung:

- `src/classification/classifier.ts` — Interface für Klassifikations-Backends
- `src/classification/openclaw-tool-classifier.ts` — Tool-basierte Implementierung
- `src/classification/fusion.ts` — Zusammenführung von Heuristik + Tool-Resultat
- `src/classification/contracts.ts` — lokale Typen / Schemas für Tool-Input/Output

Der bestehende direkte LLM-Pfad wird mittelfristig durch den Tool-Pfad ersetzt oder nur noch als Fallback/Testpfad behalten.

### 2. OpenClaw-Plugin-Tool

Neues dediziertes Tool, z. B.:

- `mail_intelligence.classify`

Optional später getrennte Schwester-Tools:

- `mail_intelligence.discover_project_candidates`
- `mail_intelligence.suggest_catalog_updates`

Wichtig: Discovery und Routing-Klassifikation bleiben getrennte Aufgaben mit getrennten Contracts.

## Request-Flow pro Mail

### Schritt 1: Mail laden

`mail-processor` liest eine Mail aus der Quellmailbox.

### Schritt 2: Vorverarbeitung

`mail-processor` erzeugt einen vorbereiteten Analysekontext:

- Message-ID
- Subject
- From / Date
- Current message
- Older quoted context
- sanitisierten Effektivtext
- optionale Thread-Hinweise

### Schritt 3: Kandidatenraum begrenzen

Vor dem Tool-Call erzeugt `mail-processor` eine heuristische Vorselektion:

- Top-N Projektkandidaten
- Top-N Topic-Kandidaten
- Workpackages nur aus dem wahrscheinlichsten Projekt oder enger Auswahl

Ziel: Das Modell soll nur innerhalb eines kontrollierten Kandidatenraums entscheiden.

### Schritt 4: Tool-Call

`mail-processor` ruft das Tool `mail_intelligence.classify` mit vorbereitetem JSON auf.

### Schritt 5: Modellaufruf im Tool

Das Tool baut daraus einen festen Prompt und führt genau einen LLM-Aufruf über die OpenClaw-Runtime aus.

### Schritt 6: Validierung im Tool

Das Tool:

- parst den Modelloutput
- validiert gegen Schema
- normalisiert Confidence-Werte
- verwirft unzulässige Kandidaten
- gibt nur gültiges JSON zurück

### Schritt 7: Fusion im `mail-processor`

`mail-processor` kombiniert:

- heuristische Scores
- Tool-Resultat
- Schwellwerte
- Guardrails

### Schritt 8: Finale Entscheidung

- Shadow-Modus: nur protokollieren
- Run-Modus: nur bei belastbarer Entscheidung routen
- bei Unsicherheit: Inbox lassen, markieren oder als Review/Suggestion behandeln

## Wie die LLM-Aufrufe künftig laufen

## Variante A: Tool-intern über OpenClaw-Runtime

Das Tool kapselt den gesamten Modellzugriff.

Ablauf:

1. `mail-processor` ruft `mail_intelligence.classify` auf
2. das Tool erhält strukturierten Input
3. das Tool baut einen strikten Prompt mit Output-Vertrag
4. das Tool ruft das konfigurierte Modell über die OpenClaw-Runtime auf
5. das Tool validiert die Antwort streng
6. das Tool gibt nur JSON an den Aufrufer zurück

### Wichtige Eigenschaften

- kein `sessions_spawn` pro Mail
- kein frei formulierender Chat-Agent als Zwischenstufe
- kein Modellzugriff mehr direkt aus dem `mail-processor`
- Modellwahl, Prompting und Parsing werden zentral im Tool gekapselt

## Prompting-Prinzipien

Das Tool arbeitet nicht als offener Chat-Agent, sondern als strikt geführter Klassifizierer.

### Modellinput

- Mail-Metadaten
- Current message
- Older context with lower weight
- erlaubte Projekt-/Topic-/Workpackage-Kandidaten
- klare Anweisungen zur Unsicherheit und zur Auswahl nur aus dem erlaubten Set

### Modellregeln

- nur Kandidaten aus dem erlaubten Set auswählen
- keine neuen Projekt- oder Topic-Namen erfinden
- Workpackages nur innerhalb des gewählten Projekts vorschlagen
- wenn unklar, niedrige Confidence zurückgeben
- Ausgabe nur als gültiges JSON

## Ziel-Contract des Tools

### Input-Schema (konzeptionell)

```json
{
  "mail": {
    "message_id": "<string>",
    "subject": "<string>",
    "from": "<string>",
    "date": "<string>",
    "current_message": "<string>",
    "older_context": "<string>",
    "sanitized_text": "<string>"
  },
  "catalog_hints": {
    "projects": [],
    "topics": [],
    "workpackages": []
  },
  "options": {
    "include_needs_reply": true
  }
}
```

### Output-Schema (konzeptionell)

```json
{
  "projectCandidates": [
    { "id": "project-a", "confidence": 0.81, "evidence": ["subject", "sender domain"] }
  ],
  "topicCandidates": [
    { "id": "topic-x", "confidence": 0.73, "evidence": ["keyword in current message"] }
  ],
  "workpackageCandidates": [
    { "id": "wp-2", "confidence": 0.66, "evidence": ["milestone mention"] }
  ],
  "needsReply": {
    "score": 0.84,
    "reasons": ["direct request", "question"]
  },
  "notes": "optional short internal note"
}
```

## Sicherheits- und Qualitätsprinzipien

Das Tool liefert **Evidenz**, nicht Wahrheit.

Die finale Entscheidung bleibt immer im `mail-processor`.

### Daher gilt

- Heuristik und Tool-Resultat werden fusioniert, nicht blind übernommen
- bei Konflikt oder niedriger Trennschärfe kein Auto-Routing
- bei Fehlern im Tool keine Mail-Operation
- ungültiger Tool-Output wird verworfen
- Shadow-first bleibt Standard

## Fehlerverhalten

Wenn der Tool-Call fehlschlägt:

- kein Routing
- Fehler im State protokollieren
- optional Retry, falls sinnvoll
- Mail verbleibt unverändert in der Quelllage bzw. im Shadow-Fluss

Wenn Tool-Output ungültig ist:

- Ergebnis verwerfen
- Fehler loggen
- keine Folgeoperationen ausführen

## Migration von AcademicAI

### Zielzustand

Der direkte Pfad über `LLM_BASE_URL`, `LLM_API_KEY`, `LLM_MODEL` im `mail-processor` ist nicht mehr Primärpfad für operative Klassifikation.

### Geplanter Migrationspfad

1. Klassifikations-Interface im `mail-processor` einziehen
2. bestehende direkte LLM-Logik dahinter kapseln
3. Tool-basierte Implementierung ergänzen
4. Fusion und Entscheidungslogik auf das neue Resultat umstellen
5. direkte AcademicAI-Nutzung aus dem Standardpfad entfernen oder nur als Fallback lassen
6. Discovery später getrennt auf eigenes Tool migrieren

## Offene Designfragen

- Wie genau soll das Plugin-Tool den Modellzugriff über die OpenClaw-Runtime technisch kapseln?
- Soll die Modellwahl hart im Tool konfiguriert oder pro Request begrenzt steuerbar sein?
- Wie groß darf der Kandidatenraum pro Aufruf werden, bevor Promptgröße und Kosten kippen?
- Wie wird Thread-Kontext zugeliefert und begrenzt?
- Welche Felder sollen verbindlich in `state.jsonl` landen, um Tool-basierte Entscheidungen nachvollziehbar zu machen?

## Umsetzungspakete

Die Migration wird in getrennte, überprüfbare Pakete geschnitten. Ziel ist, Betriebsrisiko zu senken und die neue Architektur zuerst im Shadow-Modus belastbar zu machen.

### Paket 1: Contract & Entscheidungsmodell

Ziel:
- verbindlichen Contract für `mail_intelligence.classify` festziehen
- Eingabe-/Ausgabefelder stabil definieren
- Fusionslogik zwischen Heuristik und Tool-Resultat festlegen

Umfang:
- JSON-Schema für Tool-Input definieren
- JSON-Schema für Tool-Output definieren
- erlaubte Confidence-Semantik dokumentieren
- erlaubte Evidence-Typen definieren
- Regeln für Konfliktfälle dokumentieren
- Schwellwerte / Tie-Break-Regeln für finale Routing-Entscheidung festlegen

Ergebnis:
- dokumentierter Contract
- klare Regeln für `route`, `shadow-only`, `review`, `keep-in-inbox`

#### Paket-1-Arbeitsstand

Status: **begonnen**

#### Ziel des Contracts

Das Tool `mail_intelligence.classify` soll pro Mail genau einen synchronen Analysevorgang ausführen und strikt validierbares JSON liefern.

Der Contract muss so eng sein, dass:

- das Tool keine freien Halluzinationsflächen bekommt
- der `mail-processor` die Antwort deterministisch weiterverarbeiten kann
- Modellwechsel die Schnittstelle nicht verändern
- Fehlerfälle klar von inhaltlicher Unsicherheit getrennt sind

#### Contract-Prinzipien

1. **Input ist vorbereitet, nicht roh**
   - das Tool bekommt keine komplette rohe MIME-Mail als Primärinput
   - Vorverarbeitung passiert im `mail-processor`

2. **Kandidatenraum ist geschlossen**
   - Projekte, Topics und Workpackages dürfen nur aus dem zugelieferten erlaubten Set gewählt werden
   - freie Label-Erfindung ist unzulässig

3. **Output ist Evidenz-basiert**
   - das Tool liefert Kandidaten + Konfidenz + Evidence
   - keine direkte Routing-Anweisung

4. **Unsicherheit ist ein gültiges Ergebnis**
   - niedrige Confidence ist korrektes Verhalten
   - leere Kandidatenlisten sind erlaubt

5. **Tool-Fehler und Modell-Unsicherheit sind getrennt**
   - technischer Fehler ≠ „keine gute Zuordnung“

#### Tool-Input-Schema v1 (fachlich)

```json
{
  "schema_version": 1,
  "mail": {
    "message_id": "<string>",
    "subject": "<string>",
    "from": "<string>",
    "date": "<string>",
    "current_message": "<string>",
    "sanitized_text": "<string>",
    "headers": {
      "reply_to": "<string|null>",
      "return_path": "<string|null>",
      "list_id": "<string|null>",
      "in_reply_to": "<string|null>",
      "references": ["<string>"]
    },
    "thread_context": [
      {
        "source": "artifact",
        "message_id": "<string>",
        "date": "<string>",
        "from": "<string>",
        "subject": "<string>",
        "relation": "ancestor",
        "current_message": "<string|null>",
        "older_context": "<string|null>",
        "effective_text": "<string|null>"
      },
      {
        "source": "raw_reference",
        "message_id": "<string|null>",
        "date": "<string|null>",
        "from": "<string|null>",
        "subject": "<string|null>",
        "relation": "ancestor",
        "raw_text": "<string>"
      }
    ]
  },
  "catalog_hints": {
    "projects": [
      {
        "id": "<string>",
        "title": "<string>",
        "aliases": ["<string>"],
        "keywords": ["<string>"],
        "domains": ["<string>"],
        "contacts": [{ "email": "<string>" }],
        "workpackages": [
          {
            "id": "<string>",
            "title": "<string>",
            "aliases": ["<string>"],
            "keywords": ["<string>"]
          }
        ],
        "hint_rank": 1
      }
    ],
    "topics": [
      {
        "id": "<string>",
        "title": "<string>",
        "aliases": ["<string>"],
        "keywords": ["<string>"],
        "domains": ["<string>"],
        "contacts": [{ "email": "<string>" }],
        "hint_rank": 1
      }
    ]
  },
  "options": {
    "include_needs_reply": true,
    "max_project_candidates": 4,
    "max_topic_candidates": 4,
    "max_workpackage_candidates": 3
  }
}
```

#### Input-Regeln

- `schema_version` ist Pflicht
- `mail.sanitized_text` ist Pflicht und Hauptanalysefeld
- `mail.current_message` ist Pflicht, sofern extrahierbar
- `mail.thread_context` ist optional und enthält höchstens wenige Kontextelemente
- `mail.thread_context[].source` unterscheidet zwischen bekannten Artefakten (`artifact`) und unbekanntem Referenzvolltext (`raw_reference`)
- bei `source=artifact` sollen bevorzugt deterministisch gewonnene Kontextfelder aus vorhandenen Mail-JSON-Artefakten verwendet werden
- bei `source=raw_reference` wird begrenzter Volltext einer noch nicht verarbeiteten Referenzmail mitgegeben
- `catalog_hints.projects` und `catalog_hints.topics` dürfen leer sein, aber das Tool darf dann keine IDs erfinden
- statt numerischer Heuristik-Scores wird nur eine schwache Vorauswahlreihenfolge über `hint_rank` mitgegeben
- `hint_rank` ist ein dezenter Hinweis auf die Vorauswahl, keine Vorentscheidung und kein Ersatz für Textbezug

#### Tool-Output-Schema v1 (fachlich)

```json
{
  "schema_version": 1,
  "projectCandidates": [
    {
      "id": "project-a",
      "confidence": 0.81,
      "evidence": ["subject_match", "sender_domain", "current_message"]
    }
  ],
  "topicCandidates": [
    {
      "id": "topic-x",
      "confidence": 0.73,
      "evidence": ["keyword_match", "current_message"]
    }
  ],
  "workpackageCandidates": [
    {
      "id": "wp-2",
      "project_id": "project-a",
      "confidence": 0.66,
      "evidence": ["task_reference", "milestone_reference"]
    }
  ],
  "needsReply": true,
  "warnings": ["ambiguous_project_overlap"]
}
```

#### Output-Regeln

- `schema_version` ist Pflicht
- alle Kandidatenlisten dürfen leer sein
- `id` muss immer aus dem zugelieferten Kandidatenraum stammen
- `project_id` bei Workpackages muss zu einem zugelassenen Projekt gehören
- `confidence` liegt in `[0,1]`
- `needsReply` ist in v1 rein binär (`true`/`false`)
- `evidence` ist eine kontrollierte Kurzliste, keine freien Romane
- Output enthält nur IDs, keine freien Labels
- `warnings` sind optional und verwenden kontrolliertes Vokabular
- kein Freitext-`summary` in v1

#### Vorgeschlagene kontrollierte Evidence-Typen

Für v1 bevorzugt als kontrolliertes Vokabular:

- `subject_match`
- `sender_domain`
- `sender_contact`
- `reply_chain`
- `current_message`
- `thread_context`
- `keyword_match`
- `alias_match`
- `workpackage_reference`
- `task_reference`
- `milestone_reference`

Freitext-Evidence sollte in v1 vermieden oder beim Tool normalisiert werden.

#### Vorgeschlagene kontrollierte Warning-Typen

Für v1 als kontrolliertes Vokabular, bei Bedarf erweiterbar:

- `ambiguous_project_overlap`
- `weak_project_signal`
- `topic_stronger_than_project`
- `workpackage_without_project`
- `thread_context_used`
- `insufficient_current_message_signal`
- `catalog_gap_suspected`

#### Confidence-Semantik v1

Vorschlag:

- `0.00 - 0.39` → schwach / nicht routingfähig
- `0.40 - 0.64` → plausible Hypothese, aber reviewbedürftig
- `0.65 - 0.79` → gut belastbar für Shadow-Auswertung
- `0.80 - 1.00` → starkes Signal

Wichtig:
- hohe Confidence darf nicht allein genügen, wenn andere Guardrails dagegen sprechen
- niedrige Confidence ist kein Fehler, sondern normales Verhalten bei schwachem Signal

#### Konfliktregeln zwischen Heuristik und Tool

V1-Vorschlag:

1. **starke Übereinstimmung**
   - Heuristik Top-1 und Tool Top-1 zeigen auf dasselbe Projekt
   - ausreichender Abstand zu Top-2
   - Routing-Kandidat möglich

2. **Tool stark, Heuristik schwach/null**
   - vorerst nur Shadow/Review
   - kein automatisches Routing in V1

3. **Heuristik stark, Tool leer oder schwach**
   - konservativ bleiben
   - eher Shadow-only oder Inbox behalten

4. **Top-1/Top-2 zu nah beieinander**
   - kein Routing
   - Warnung `ambiguous_project_overlap`

5. **Topic klarer als Projekt**
   - Topic protokollieren, aber Projekt nicht routen

6. **Workpackage ohne belastbares Projekt**
   - Workpackage ignorieren

#### Entscheidungszustände im `mail-processor`

Die Fusion soll zunächst auf diese Zustände abbilden:

- `route`
- `shadow_only`
- `review`
- `keep_in_inbox`
- `classification_failed`

Bedeutung:

- `route` → nur im Run-Modus und nur bei belastbarer Gesamtlage
- `shadow_only` → starke Signale, aber noch nicht für echte Aktion freigeschaltet
- `review` → relevante Hypothese, aber zu unsicher/ambig
- `keep_in_inbox` → keine brauchbare Zuordnung
- `classification_failed` → technischer Fehler im Tool/Parsing/Runtime

#### Gewinnung des Thread-Kontexts

Frage: Wie kommen wir an den Thread-Kontext?

Entscheidungslinie:

- kein separater Verdichtungs-/Snippet-Schritt vor der Klassifikation
- Thread-Kontext wird direkt in **demselben LLM-Aufruf** mitverarbeitet, der auch die eigentliche Klassifikation macht
- da der Fall im Live-Betrieb voraussichtlich selten ist, lohnt sich kein eigener Vorverarbeitungs-LLM-Schritt

V1-Vorschlag:

1. `mail-processor` wertet `In-Reply-To` und `References` aus
2. wenn referenzierte Mails bereits verarbeitet/bekannt sind, nutzt der `mail-processor` deren vorhandene lokale JSON-Artefakte als Primärquelle
3. aus diesen JSON-Artefakten werden deterministisch die relevanten Kontextfelder gelesen und direkt als `thread_context` an das Tool übergeben
4. wenn referenzierte Mails noch **nicht** verarbeitet sind, aber lokal als Mailinhalt vorliegen oder beschafft werden können, werden sie als begrenzter Volltext-Kontext mitgegeben
5. die Gewichtung dieses Kontextes passiert erst innerhalb des normalen Klassifikationsaufrufs
6. kein zusätzlicher allgemeiner Remote-Thread-Fetch im ersten Schritt, um Komplexität und Laufzeit klein zu halten

Bevorzugte Reihenfolge für Thread-Kontext:

1. strukturierte lokale JSON-Artefakte bekannter Mails
2. lokale exportierte/verfügbare Volltexte unbekannter, aber referenzierter Mails
3. kein Thread-Kontext

#### Erforderliche Verbesserungen am Mail-JSON-Artefakt

Aus Sicht der neuen Architektur muss das aktuelle Mail-Artefakt-Schema nachgeschärft werden.

##### 1. Kanonische Thread-IDs

Neu vorzusehen:

```json
"thread": {
  "messageIdNormalized": "<string>",
  "inReplyToNormalized": "<string|null>",
  "referencesNormalized": ["<string>"]
}
```

Regeln:

- Lookup-Key ist die normalisierte Message-ID ohne Winkelklammern
- `stableId` soll diesem Lookup-Key entsprechen
- `inReplyToNormalized` und `referencesNormalized` müssen im selben Format vorliegen
- Thread-Suche darf nicht auf Envelope-IDs beruhen

##### 2. Wiederverwendbare Kontextfelder

Neu vorzusehen:

```json
"context": {
  "currentMessageText": "<string|null>",
  "olderContextText": "<string|null>",
  "effectiveText": "<string>",
  "previewText": "<string|null>"
}
```

Ziel:

- bekannte Mails sollen ohne Neuparsing des gesamten Artefakts als Thread-Kontext nutzbar sein
- `preview` allein ist dafür zu promptnah und uneinheitlich
- `sanitizing.text` allein ist oft zu roh und zu MIME-lastig

##### 3. Strukturierte Header-Felder für Thread-Nutzung

Bereits vorhanden, aber künftig verbindlicher und normalisiert nutzbar:

- `mailMeta.messageId`
- `mailMeta.inReplyTo`
- `mailMeta.references`
- `mailMeta.subject`
- `mailMeta.from`
- `mailMeta.date`

Empfehlung:

- `mailMeta.references` nicht nur als Rohstring, sondern zusätzlich als normalisierte ID-Liste pflegen

##### 4. Kein Thread-Lookup aus LLM-Feldern

Wichtig:

- Thread-Findung darf **nicht** auf `llm`, `notes`, `keywords` oder ähnlichen Freitextfeldern beruhen
- diese Felder können optional Zusatzkontext sein, aber nicht die primäre Lookup- oder Join-Basis

##### 5. Priorisierte Artefaktfelder für `thread_context`

Wenn bekannte Mails referenziert werden, sollen bevorzugt diese Felder in `thread_context` einfließen:

1. `thread.messageIdNormalized`
2. `mailMeta.subject`
3. `mailMeta.from`
4. `mailMeta.date`
5. `context.currentMessageText`
6. `context.olderContextText`
7. `context.effectiveText`

Nur wenn diese Felder fehlen, soll auf rohe Sanitizing-Felder oder Export-Volltext zurückgefallen werden.

Regeln für `thread_context` v1:

- maximal 2 bis 3 Kontextelemente
- bekannte Mails deterministisch aus JSON-Artefakten ableiten
- unbekannte Mails nur als eng begrenzten Volltext zuliefern
- kein separater Snippet-Generator vorab
- Thread-Kontext ist low-weight gegenüber der aktuellen Mail
- fehlender Thread-Kontext ist normal und kein Fehler

Technischer Leitgedanke:

- **bekannte Mails**: vorhandene Struktur nutzen, kein unnötiger Zusatzschritt
- **unbekannte Mails**: direkt im Klassifikationsaufruf mitgeben, kein zweiter LLM-Pass
- Klassifikation und Kontextgewichtung bleiben in einem einzigen Analysevorgang

Spätere Ausbaustufe:

- optional gezielter Thread-Nachzug aus IMAP anhand `Message-ID`/`References`
- optional Nutzung bereits vorhandener Klassifikations-/State-Felder aus früheren Mail-Artefakten als zusätzlicher Kontext
- nur wenn Nutzen, Kosten und Robustheit das rechtfertigen

#### Festgezogene Entscheidungen für Paket 1

- `summary` wird in v1 weggelassen
- `warnings` nutzen kontrolliertes Vokabular und werden bei Bedarf erweitert
- statt numerischem Heuristik-Score wird nur eine schwache Ranginformation (`hint_rank`) mitgegeben
- Thread-Kontext wird als begrenzter Zusatzkontext modelliert, nicht als freier Alttextblock
- bekannte referenzierte Mails werden deterministisch aus vorhandenen JSON-Artefakten ausgewertet
- unbekannte referenzierte Mails dürfen als begrenzter Volltext-Kontext in denselben Klassifikationsaufruf gehen
- es gibt in v1 keinen separaten Verdichtungs- oder Snippet-LLM-Schritt vor der Klassifikation
- das Mail-JSON-Artefakt wird um normalisierte Thread-IDs und wiederverwendbare Kontextfelder nachgeschärft
- Kandidatenlimits in v1:
  - Projekte: max 4
  - Topics: max 4
  - Workpackages: max 3
- Output-Kandidaten enthalten nur IDs
- `needsReply` bleibt ein separater binärer Analysekanal und wird nicht als Routing-Verstärker missbraucht

#### Verbleibende offene Punkte in Paket 1

- Soll `hint_rank` im Prompttext explizit erwähnt oder nur strukturell mitgegeben werden?
- Wie eng soll Volltext-Kontext unbekannter referenzierter Mails begrenzt werden?
- Welche Alt-Artefakte ohne neue `thread`/`context`-Blöcke brauchen eine Rückfalllogik?
- Soll `preview` mittelfristig durch klarere `context.*`-Felder ersetzt oder nur ergänzt werden?

### Paket 2: `mail-processor` auf Klassifikations-Abstraktion umbauen

Ziel:
- direkten LLM-Zugriff aus der Kernlogik herauslösen
- austauschbare Klassifikations-Schnittstelle einziehen

Umfang:
- `src/classification/classifier.ts` einführen
- bestehende LLM-Logik hinter ein Interface hängen
- Heuristik, Tool-Resultat und Fusion modularisieren
- `cli.ts` von direkter Klassifikationslogik entlasten
- Typen / Contracts in eigene Module ziehen

Ergebnis:
- `mail-processor` kennt nur noch ein Klassifikations-Backend, nicht mehr die konkrete Modellanbindung

#### Paket-2-Zielbild

Die heutige Struktur hat den Kern der Klassifikation noch über mehrere Dateien verteilt, insbesondere:

- `src/cli.ts`
- `src/llm.ts`
- `src/matcher.ts`
- `src/preprocess.ts`
- Mail-Artefakt-Write-Pfade rund um `msgs/` und `state.jsonl`

Für die neue Architektur muss der operative Run-Pfad klar von der eigentlichen Klassifikationslogik getrennt werden.

#### Zielzuschnitt im Code

Neue oder umgeschnittene Module:

```text
src/
  classification/
    classifier.ts
    contracts.ts
    heuristic-classifier.ts
    openclaw-tool-classifier.ts
    fusion.ts
    thread-context.ts
    artifact-schema.ts
```

Bedeutung:

- `classifier.ts`
  - gemeinsames Interface für Klassifikations-Backends

- `contracts.ts`
  - TypeScript-Typen für Tool-Input, Tool-Output, Fusionsresultat, Entscheidungszustände

- `heuristic-classifier.ts`
  - bisherige heuristische Zuordnung aus `matcher.ts`, in eine klar nutzbare Backend-/Signalform gebracht
  - inzwischen als eigenes Backend-Modul angelegt

- `openclaw-tool-classifier.ts`
  - neue Implementierung für den späteren Tool-Call `mail_intelligence.classify`

- `fusion.ts`
  - Regeln zur Zusammenführung von Heuristik + Tool-Resultat + Guardrails
  - aktuell bereits als erster expliziter Fusionsbaustein für `ClassificationResult` angelegt

- `thread-context.ts`
  - Aufbereitung von `thread_context` aus bekannten Artefakten oder unbekanntem Referenzvolltext

- `artifact-schema.ts`
  - Typen und Hilfslogik für Mail-JSON-Artefakte (`thread.*`, `context.*`, Lookup-Helfer)

#### Bestehende Dateien, die voraussichtlich angepasst werden müssen

##### 1. `src/cli.ts`

Aktueller Zustand:
- enthält Run-Orchestrierung und große Teile des konkreten Klassifikationsflusses

Geplante Änderung:
- `cli.ts` bleibt Run-Orchestrator
- Klassifikation wird in klar abgegrenzte Aufrufe ausgelagert
- `cli.ts` soll nicht mehr wissen, **wie** LLM oder Tool intern arbeiten

Konkrete Zieländerungen:
- Klassifikations-Backend instanziieren
- Preprocessing anstoßen
- Thread-Kontext bauen lassen
- Heuristik + Tool/Fusion konsumieren
- Ergebnis in State/Artefakt/Routing überführen

##### 2. `src/llm.ts`

Aktueller Zustand:
- direkter Modellzugriff für Klassifikation und Discovery
- alte JSON-Schemata mit freien Labels / `needsReply.score`

Geplante Änderung:
- operative Mail-Klassifikation aus `llm.ts` herauslösen
- `llm.ts` nicht mehr als Primärpfad für Routing-Klassifikation verwenden
- Discovery kann vorerst separat dort bleiben, bis Paket 6 dran ist

Konkrete Zieländerungen:
- Klassifikations-spezifische Teile schrittweise in `openclaw-tool-classifier.ts` bzw. Tool-Contract überführen
- `llm.ts` höchstens als Altpfad/Fallback/Testpfad behalten

##### 3. `src/matcher.ts`

Aktueller Zustand:
- Heuristik und Heuristik+LLM-Merge gemischt

Geplante Änderung:
- reine Heuristiklogik herausziehen und klar abgrenzen
- keine direkte Vermischung mit altem LLM-Merge behalten

Konkrete Zieländerungen:
- `matchProject(...)` als heuristische Signalquelle erhalten oder extrahieren
- `mergeHeuristicAndLlm(...)` perspektivisch ersetzen durch neue Fusion in `classification/fusion.ts`

##### 4. `src/preprocess.ts`

Aktueller Zustand:
- liefert vorbereiteten Mailtext für Klassifikation

Geplante Änderung:
- Ergebnis soll gezielter in wiederverwendbare Kontextelemente aufgeteilt werden
- nötig für künftige Artefaktfelder `context.*`

Konkrete Zieländerungen:
- explizit unterscheidbare Felder für:
  - `currentMessageText`
  - `olderContextText`
  - `effectiveText`
  - ggf. `previewText`

##### 5. Mail-Artefakt-Write-Pfad

Aktueller Zustand:
- Mail-JSON-Dateien enthalten bereits viele Daten, aber noch keine saubere Thread-/Context-Normalform

Geplante Änderung:
- Artefakt-Writer müssen das neue Schema ergänzen

Konkrete Zieländerungen:
- normalisierte Thread-IDs schreiben
- `context.*` schreiben
- `mailMeta.references` zusätzlich normalisiert als Liste nutzbar machen
- Lookup über `stableId` / normalisierte Message-ID absichern

#### Konkrete Refactoring-Arbeitspakete innerhalb von Paket 2

##### Paket 2A: Verträge und Typen einziehen

- neue TS-Typen für:
  - `ClassificationInput`
  - `ClassificationResult`
  - `ThreadContextEntry`
  - `RoutingDecisionState`
  - Mail-Artefakt-Erweiterungen (`thread`, `context`)
- umgesetzt in:
  - `src/classification/contracts.ts`
  - `src/classification/classifier.ts`
  - ergänzende Artefakt-Typen in `src/types.ts`

Ergebnis:
- klare statische Grundlage für die folgenden Umbauten

##### Paket 2B: Heuristik vom alten LLM-Merge trennen

- `matcher.ts` in reine Heuristik zurückführen
- alte `mergeHeuristicAndLlm(...)`-Logik nicht weiter als Zielbild verwenden
- Fusion neu und explizit modellieren

Ergebnis:
- eine saubere heuristische Erststimme statt impliziter Mischlogik
- Altlogik kann vorübergehend explizit in ein Legacy-Modul ausgelagert bleiben, damit der operative Pfad weiterläuft, ohne `matcher.ts` als Mischmodul zu behalten

##### Paket 2C: Thread-Kontext-Baustein einziehen

- Hilfsmodul bauen, das aus
  - `In-Reply-To`
  - `References`
  - vorhandenen Mail-Artefakten
  - optional lokalem Referenzvolltext
  einen `thread_context`-Block aufbaut

Ergebnis:
- `cli.ts` muss Thread-Kontext nicht selbst zusammenzimmern

Aktueller Umsetzungsstand:
- `src/classification/thread-context.ts` eingeführt
- Lookup bekannter Referenzmails läuft über normalisierte Message-ID gegen vorhandene JSON-Artefakte
- bevorzugt werden neue Felder aus `thread.*` und `context.*`, mit Fallback auf ältere Felder wie `mailMeta.*` und `preview`
- aktuell werden nur bekannte Artefakte als `thread_context` genutzt, noch kein Fetch/Import unbekannter Referenzmails

##### Paket 2D: Artefakt-Schema erweitern

- Writer/Reader für Mail-JSON-Artefakte anpassen
- alte Felder erhalten, neue Felder ergänzen
- Rückfalllogik für ältere Artefakte definieren

Ergebnis:
- bekannte Mails werden als Thread-Kontext maschinenfreundlich nutzbar

Aktueller Umsetzungsstand:
- Writer ergänzt `thread.messageIdNormalized`, `thread.inReplyToNormalized`, `thread.referencesNormalized`
- Writer ergänzt `context.currentMessageText`, `context.olderContextText`, `context.effectiveText`, `context.previewText`
- `mailMeta.referencesNormalized` wird zusätzlich mitgeschrieben
- Vollständigkeitsprüfung neuer Artefakte verlangt nun die neuen `thread`- und `context`-Blöcke
- Rückfalllogik für Alt-Artefakte als Reader-/Lookup-Thema ist noch offen

##### Paket 2E: Klassifikations-Backend austauschbar machen

- `classifier.ts`-Interface einziehen
- zunächst evtl. ein internes Übergangs-Backend nutzen
- später `openclaw-tool-classifier.ts` sauber andocken

Ergebnis:
- der operative Mail-Run ist entkoppelt vom konkreten Modellpfad

Aktueller Umsetzungsstand:
- `src/classification/legacy-llm-classifier.ts` als Übergangs-Backend eingeführt
- `cli.ts` konsumiert nun ein `MailClassifier`-Backend statt den direkten LLM-Aufruf selbst zusammenzubauen
- der operative Pfad ist damit an eine Klassifikations-Schnittstelle gehängt, auch wenn aktuell noch das Legacy-Backend darunter arbeitet

#### Aktueller Stand Paket 2

- **2A umgesetzt:** Contracts, Classifier-Interface und Artefakt-Erweiterungstypen sind angelegt.
- **2B begonnen:** `matcher.ts` ist auf reine Heuristik reduziert, der bisherige Heuristik+LLM-Merge liegt übergangsweise separat in `src/classification/legacy-llm-merge.ts`.
- **2D teilweise umgesetzt:** neue Artefaktfelder werden bereits geschrieben; Reader-/Fallback-Logik für Alt-Artefakte ist nun teilweise im Thread-Kontext-Lookup berücksichtigt, aber noch nicht als allgemeiner Reader zentralisiert.
- **2C teilweise umgesetzt:** Thread-Kontext-Baustein ist eingeführt und lokal in den bestehenden Klassifikationspfad eingehängt.
- **2E teilweise umgesetzt:** operativer Pfad konsumiert jetzt ein Classifier-Backend; darunter hängt jetzt primär `src/classification/openclaw-tool-classifier.ts` mit dem finalen Contract. Der Legacy-LLM-Adapter bleibt parallel als Fallback bestehen.
- `catalog_hints` werden inzwischen im neuen Input-Vertrag real aus Projekten und Topics befüllt, statt leer als Platzhalter übergeben zu werden.
- erster expliziter `fusion.ts`-Baustein ist eingezogen, damit die Entscheidungslogik nicht mehr nur implizit aus Top-1-Ableitungen besteht.
- `heuristic-classifier.ts` liegt nun als separates Backend vor und wird bereits vom Legacy-Adapter als eigene Erststimme genutzt.

#### Empfohlene Umsetzungsreihenfolge in Paket 2

1. **2A Verträge und Typen**
2. **2B Heuristik trennen**
3. **2D Artefakt-Schema erweitern**
4. **2C Thread-Kontext-Baustein**
5. **2E Backend-Abstraktion andocken**
6. **2E.1 OpenClaw-Tool-Backend als Primärpfad einhängen, Legacy parallel als Fallback behalten**

Warum so:
- erst Typen und Grenzen
- dann Altlogik entwirren
- dann Datenbasis sauber machen
- dann Thread-Kontext
- dann Backend austauschbar machen

#### Fertig-Kriterien für Paket 2

Paket 2 gilt erst dann als sauber vorbereitet, wenn:

- `cli.ts` keine direkte Modell-/Promptlogik mehr enthält
- Heuristik getrennt von Tool-Fusion vorliegt
- Mail-Artefakte normalisierte `thread.*`- und `context.*`-Felder schreiben können
- `thread_context` aus bekannten Artefakten aufgebaut werden kann
- der operative Run nur noch ein Klassifikations-Interface konsumiert

### Paket 3: OpenClaw-Plugin-Tool `mail_intelligence.classify`

Ziel:
- dediziertes, synchrones Analyse-Tool für Mail-Klassifikation bauen

Umfang:
- Plugin-Tool-Grundstruktur anlegen
- Promptbau kapseln
- Modellaufruf über OpenClaw-Runtime implementieren
- Schema-Validierung des Modelloutputs implementieren
- Kandidatenraum-Prüfung einbauen (nur erlaubte IDs)
- Fehlerfälle sauber normalisieren

Ergebnis:
- nutzbares Tool mit stabilem JSON-Vertrag
- keine Side Effects außerhalb des Tool-Outputs

#### Paket-3-Arbeitsstand

Status: **minimal lauffähig angelegt**

Aktueller Umsetzungsstand:
- lokales Plugin-Skelett unter `plugin/mail-intelligence/` angelegt
- Manifest `openclaw.plugin.json` angelegt
- Tool `mail_intelligence.classify` registriert
- Tool-Input-Schema an den festgezogenen Contract angelehnt
- Tool kapselt Promptbau, Modellaufruf und strikte Ergebnis-Normalisierung
- `src/classification/openclaw-tool-classifier.ts` ruft jetzt den echten Gateway-Endpunkt `/tools/invoke` auf statt nur einen pseudo-toolartigen Chat-Request zu bauen
- Kandidatenraum-Prüfung bleibt sowohl im Tool als auch im `mail-processor`-Adapter aktiv
- Legacy-LLM-Backend bleibt parallel als Fallback bestehen

Bewusst noch nicht fertig in Paket 3:
- Plugin ist lokal im Repo angelegt, aber noch nicht als installierte/aktiv genutzte Gateway-Extension ausgerollt
- Modellwahl im Plugin ist vorerst einfach gehalten
- ausführlichere Beobachtbarkeit und Vergleichsausgaben folgen in Paket 4

### Paket 4: Shadow-Integration & Beobachtbarkeit

Ziel:
- Tool-basierte Klassifikation zuerst ohne Routing-Risiko im Shadow-Modus beobachten

Umfang:
- Tool-Resultat im State / Artefakt-Output protokollieren
- Vergleich Heuristik vs. Tool sichtbar machen
- Konfliktfälle markieren
- Fehlerraten und ungültige Outputs erfassbar machen
- Beispielmails für Review sammeln

Ergebnis:
- belastbares Bild über Qualität und Stabilität vor produktivem Routing

### Paket 5: Routing-Fusion aktivieren

Ziel:
- neue Tool-basierte Klassifikation kontrolliert in die finale Routing-Entscheidung übernehmen

Umfang:
- Fusionslogik produktiv schalten
- Guardrails scharf setzen
- Konflikt- und Unsicherheitsfälle konservativ behandeln
- Shadow-/Run-Verhalten sauber differenzieren

Ergebnis:
- operativer Routing-Pfad mit OpenClaw-Tool-Unterstützung
- Safe Default bei Unsicherheit bleibt erhalten

### Paket 6: Altpfad und Discovery neu ordnen

Ziel:
- AcademicAI-Direktpfad entkoppeln und Discovery später sauber separat migrieren

Umfang:
- bisherigen direkten `llm.ts`-Pfad auf Fallback/Testrolle reduzieren oder entfernen
- Discovery explizit von Routing-Klassifikation trennen
- optional Schwester-Tool für Discovery vorbereiten
- Doku, Env-Model und Betriebsanleitung bereinigen

Ergebnis:
- klarer Zielpfad ohne Architektur-Doppelgleisigkeit
- Discovery bleibt eigener Workflow

## Empfehlung für die nächste Umsetzungsetappe

1. Paket 1 vollständig ausarbeiten
2. danach Paket 2 als Refactoring-Basis umsetzen
3. dann Paket 3 als minimal lauffähiges Tool bauen
4. Paket 4 zuerst nur im Shadow-Modus testen
5. Paket 5 erst nach sichtbarer Qualitätsprüfung aktivieren
6. Paket 6 zum Schluss für Aufräumen und Entkopplung verwenden
