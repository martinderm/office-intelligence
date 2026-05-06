# office-intelligence

Leichtgewichtiger, produktiver Kern fuer agentische Mailarbeit und Wissenspflege.

Der aktive Betriebsmodus in diesem Repository ist:

- mail-desk fuer operative Einzelmail-Triage
- project-catalog-entry fuer Projektkatalog und Projekt-Wissensstruktur
- topic-catalog-entry fuer Topickatalog und thematische Wissensstruktur

## Aktueller Fokus (main)

Der Branch main ist bewusst auf den funktionierenden Kern reduziert:

- Mailbearbeitung ueber mail-desk
- Routing anhand von Projekt- und Topic-Katalog
- schlanke, nachvollziehbare Datenablage unter data/mail-desk
- gezielte Wissenspflege unter memory/references

Nicht aktiv im Hauptpfad:

- schwere, hybride/deterministische Batch-Mailpipeline
- experimentelle End-to-End-Automatisierung mit Teilfunktionalitaet

## Hinweis auf den Hybrid-Branch

Die hybride, teilweise funktionsfaehige Mailverarbeitung (inklusive OpenClaw-Plugin-Integration) ist bewusst ausgelagert nach:

- feature/automation-mail-processor-openclaw

Dieser Branch dient als Entwicklungs- und Experimentierpfad. Der produktive Standard bleibt main.

## Repository-Struktur

Relevante Kernbereiche:

- skills/mail-desk
- skills/project-catalog-entry
- skills/topic-catalog-entry
- memory/references/projects/projects.json
- memory/references/topics/topics.json
- data/mail-desk

## Kern-Workflow

1. Mail lesen und einordnen (mail-desk)
2. Projekt-/Topic-Kontext laden
3. Routing-Entscheidung treffen
4. in data/mail-desk protokollieren
5. bei belastbaren neuen Fakten memory/references aktualisieren

## Skills im Detail

### mail-desk

Pfad:

- skills/mail-desk/SKILL.md

Zweck:

- eine Mail nach der anderen bearbeiten
- klare, nachvollziehbare Entscheidungen
- kein versteckter Massenlauf
- Message-ID als stabile Referenz

Wichtige Datenpfade:

- data/mail-desk/action-log.jsonl
- data/mail-desk/pending-review.jsonl
- data/mail-desk/replies-needed.jsonl
- data/mail-desk/final-location-index.json
- data/mail-desk/archive/

### project-catalog-entry

Pfad:

- skills/project-catalog-entry/SKILL.md

Zweck:

- Projekte sauber anlegen und pflegen
- routingrelevante Metadaten in projects.json halten
- Projektwissen in Projektordnern strukturieren

Source of truth:

- memory/references/projects/projects.json

### topic-catalog-entry

Pfad:

- skills/topic-catalog-entry/SKILL.md

Zweck:

- Topics/Subtopics sauber pflegen
- thematische Routing-Signale stabil halten
- Topicwissen strukturiert dokumentieren

Source of truth:

- memory/references/topics/topics.json

## Installation in Agent-Workspaces

Standard-Rollout:

- skills/mail-desk
- skills/project-catalog-entry
- skills/topic-catalog-entry

Minimaler Rollout:

1. die drei Skill-Ordner in den Ziel-Agent-Workspace kopieren
2. sicherstellen, dass `memory/references/projects/projects.json` und `memory/references/topics/topics.json` vorhanden sind
3. pro Mail den Workflow aus `skills/mail-desk/SKILL.md` verwenden

## Katalog- und Referenzmodell

Gemeinsame Katalog-Doku:

- memory/references/README.md

Projektkatalog:

- memory/references/projects/projects.json

Topickatalog:

- memory/references/topics/topics.json

Leitprinzip:

- Katalogdateien enthalten strukturierte Routing-Metadaten
- Fachwissen liegt in den jeweiligen Projekt-/Topic-Unterordnern

## Lokaler Start

Falls nur Skills/Katalogpflege und Dokumentation genutzt werden, ist kein Build oder Pipeline-Run noetig.

## Betriebsgrenzen auf main

- Kein automatisches Batch-Routing als Standard
- Keine implizite Vollautomation ueber alle Mails
- Fokus auf kontrollierte, agentische Einzelbearbeitung

## Erweiterungen

Ergaenzende API-Skills fuer angrenzende Workflows koennen weiterhin separat genutzt werden, zum Beispiel:

- fireflies-api fuer Meeting- und Transcript-Arbeit
- todoist-api fuer Task-Routing

## Migrationshinweis

Wenn du von aelteren Stands kommst, in denen mail-processor als primaerer Laufweg dokumentiert war:

- nutze auf main den mail-desk-zentrierten Kern
- verwende den Hybrid-Branch nur fuer gezielte Entwicklungsarbeiten
