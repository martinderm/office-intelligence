# Source-bound Event-Writer

## Autorität und Grenzen

`scripts/event_documentation.py` schreibt ausschließlich lokale, mailbasierte
**UNGEPRÜFTE Entwürfe**. Referenzablage ist keine fachliche Freigabe. Jeder
Markdown-Output zeigt `MAILBASED UNGEPRUEFTER ENTWURF`; das Index-Frontmatter
enthält `evidence_level: unreviewed-draft`, `review_state: unreviewed` und
`status: draft`. Herkunft, Beleganker und Completeness bleiben sichtbar.
Untrusted Texte werden für Markdown/HTML inert dargestellt; JSON bewahrt die
gebundene Provenienz. Hashes belegen lokale Integrität, nicht Echtheit der Mails,
Live-Account-Bindung oder menschliche Autorisierung.

Keine Katalog-, Mailbox-, Cloud-, Meeting- oder Task-Mutation, kein Versand,
keine Content-Freigabe, kein eigener Event-Cloudspeicher. Der Writer schreibt
niemals in sein eigenes Bundle. Die allgemeinen manuellen Event-/Cloud-/Todoist-
Workflows des Skills werden durch diesen begrenzten Writer nicht ausgeführt.

## API und CLI

`apply_event(payload, *, workspace_root, lease_id, conversation_id=None,
trusted_workspace_lock_root=None)` gibt ein begrenztes Dict mit `ok` zurück.
Der Payload enthält **genau** `schema_version: 1`, `work_order`, `content`.
Boolean-Schemaversionen, unbekannte Felder, doppelte JSON-Keys, NaN/Infinity,
ungültige Typen und Requests über 2 MiB scheitern fail-closed.

```text
python -B <bundle>/skills/event-documentation/scripts/event_documentation.py \
  --request data/event-documentation/request.json --workspace . \
  --lease-id <trusted-lease> --workspace-lock-root <trusted-skill-installation> --json
```

Optional: `--conversation-id`. Der Sechs-Feld-Envelope enthält
`action/success/state/message/data/error`; Exit 0 bei Erfolg, 2 bei Fehlern.
Keine ungefilterten Payload-/Hostpfad-/Lease-Echos oder Tracebacks in Fehlern.
Anfragen und zurückgegebene Receipts kann der Caller unter `data/` ablegen;
der Writer persistiert sie nicht zusätzlich.

## Echte Shared-Dependency und Lease

Die vertrauenswürdige Installation von `workspace-lock` wird in dieser Reihenfolge
aufgelöst: API-Parameter `trusted_workspace_lock_root` / CLI `--workspace-lock-root`,
Operator-Environment `EVENT_DOCUMENTATION_WORKSPACE_LOCK_ROOT`, dann der
Geschwister-Skill `../workspace-lock` relativ zum Bundle. Kein CWD-Scan und keine
Übernahme von Dependency-Pfaden oder Lock-IDs aus Mail-/Payload-Inhalten.
Die explizite Installation ist ausführbarer **vertrauenswürdiger Operator-Code**,
keine Eingabequelle für ingestrierte Inhalte. Guard und CLI müssen dort wirklich
vorhanden sein; keine Kopien, Ersatz-Locks oder stillen Provider-/Legacy-Fallbacks.

Der Shared Guard läuft in place mit `allow_legacy=False`. Der Harness hält eine
aktive eigene Lease; der Writer erwirbt, erneuert oder released sie nicht.
Ownership wird vor Mutation und erneut während Veröffentlichung/Cleanup geprüft.
Der vom Caller gewählte Root wird als Trust Anchor aufgelöst; unterhalb davon
werden Symlinks, Junctions/Reparse-Points, Hardlinks und nichtreguläre Ziele
abgelehnt. Traversal, ADS, Windows-Aliase und reservierte Namen scheitern.

## Work-Order und Source-Profile

Der unveränderte Work-Order enthält genau:
`schema_version: 1`, `owner: "event-documentation"`,
`status: "pending_content_review"`, `event`, `sources`, `attachments`,
`completeness`, `source_snapshot_sha256`.

Zwei getrennte Profile, keine Teil-/Mischformen:

- **Basis:** Jede Source hat genau `id`, `kind: "mail"`, normalisierte
  `message_id`, lowercase `content_sha256`; `attachments` ist leer.
  Completeness hat genau `exhaustive: false`, eine nichtleere `limitations`-Liste.
  Keine zusätzlichen Locator- oder Echtheitsbehauptungen aus diesem Profil ableiten.
- **Rich / Fassade:** Sources besitzen zusätzlich vollständig `locations`,
  `thread_parents`, `calendar_objects`; IDs entsprechen
  `"mail-" + SHA256(canonical_json(message_id))`. Locations binden Account,
  Ordner, Envelope und Message an den expliziten Scope. Anhänge haben
  `id/sha256/size_bytes/provenance` und bewahren alle MIME-Provenienzen;
  jede Provenienz muss zu einer gebundenen Source-Location gehören.
  Completeness entspricht exakt dem Fassadenvertrag einschließlich
  `scan_basis: "first_page_envelopes"`, Scope und kanonischen Limitations.

Source-IDs und normalisierte Message-IDs sind eindeutig. Der Snapshot-Hash bindet
exakt `{"sources": ..., "attachments": ...}`. JSON-Kanonisierung:
`sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False`;
SHA-256 über UTF-8. Der Work-Order-Hash bindet den **gesamten unveränderten**
Work-Order; keine Normalisierung unter Beibehaltung alter Hashes.

`content` hat genau `overview`, `planned_agenda`, `observed_decisions`,
`reply_triage`, `todo_triage` (je Listen). Items haben `text` und nichtleere,
eindeutige `source_ids` aus dem Snapshot; Triage-Items zusätzlich einen strikt
booleschen, unabhängig bestimmten `needed`-Wert. Beobachtete Entscheidungen sind
ungeprüfte **Meldungen**, nicht aus geplanter Agenda abgeleitete Beschlüsse.
Die Triage-Ausgabe zeigt Bedarf ja/nein, keine angeblich erledigten Aufgaben.

## Owner, Bootstrap und fünf Outputs

Projekt-Owner werden eindeutig aus `memory/references/projects/projects.json`
aufgelöst (Liste oder `projects`-Liste; bestehende minimale Routing-Einträge
bleiben lesbar). Der Writer prüft gezielt Owner-Identität und Katalogbindung,
migriert oder ersetzt aber nicht die vollständige Projekt-v3-Validierung.
Ein Projektstatus wird nicht in die Topic-spezifischen Active/Inactive-
Routingregeln umgedeutet: explizit beauftragte Archiv-Entwürfe bleiben möglich.
Der Writer trifft keinen automatischen Mail-Routingentscheid.
`event` verlangt `owner_kind: "project"`, `owner_id`, `id`, `title`, ISO-`start`,
optional `end >= start`. Projekt-/Event-IDs behalten die Fassadengrammatik
`[a-z0-9][a-z0-9_-]{0,99}` unter zusätzlichem Dateisystemschutz.

Topic-Owner verlangen `owner_kind: "topic"`, `owner_id` und `subtopic_id`;
Topic/Subtopic/Event verwenden strikte Slugs. Die eindeutige katalogisierte Kette
aus `memory/references/topics/topics.json` ist erforderlich. Event-Titel,
`start/end` und kanonisches `reference_md` müssen zum ausgewählten Event passen
(`starts_on/ends_on` im Katalog); Routingstatus, Phase und explizite geerbte
Storage-Selektoren werden geprüft. Keine heuristische Subtopic-Wahl oder direkten
Root-Topic-Events. Doppelte ausgewählte Owner/Subtopics/Event-IDs stoppen.

**Bootstrap-Ausnahme:** Erst nach gültiger Topic/Subtopic-Event-Katalogisierung
darf ein fehlendes kanonisches `index.md` als ungeprüfter Entwurf entstehen.
Vorhandene menschliche Dossiers werden nie überschrieben. Matcher-Anforderungen
bleiben unverändert, insbesondere die Prüfung eines vorhandenen Dossiers.

Die Referenz liegt unter `memory/references/projects/<p>/events/<e>/index.md`
oder `memory/references/topics/<t>/subtopics/<s>/events/<e>/index.md`.
Die vier Evidence-Dateien sind `action-items.md`, `source-log.json`,
`attachment-inventory.json`, `compliance-report.json` unter
`memory/evidence/projects/<p>/events/<e>/` beziehungsweise
`memory/evidence/topics/<t>/events/<e>/` — **ohne Subtopic-Komponente**.
Gleiche Event-IDs in Geschwister-Subtopics kollidieren und stoppen vor Writes.
Der Index verlinkt die gemeldeten Entscheidungen relativ in die Evidence.
Vorhandene zusätzliche menschliche Notizen bleiben erhalten.

## No-Clobber, Wiederholung und Recovery

Kompletter Preflight vor `mkdir` oder Staging: Payload, Quellen, Katalog,
Dependency, eigene Lease, alle Ziele und bestehende Konflikte. Neue Dateien
werden exklusiv gestagt, geflusht und per `os.link` ohne Überschreiben
veröffentlicht; kein `os.replace`-Fallback auf bestehende Ziele. Anschließend
werden Identität und Bytes sämtlicher Outputs erneut geprüft.

Ein vollständiger byte-identischer Dateisatz ist ein unveränderter Erfolg.
Abweichungen, Teilbestände, leere Event-Zielverzeichnisse und Staging-Reste
stoppen; keine automatische Reparatur oder Ergänzung. Bei behandelbaren Fehlern
werden nur nachweislich eigene, unveränderte neue Dateien/Verzeichnisse unter
weiterhin eigener Lease zurückgenommen. Substituierte oder extern veränderte
Bestände bleiben erhalten. Lease-Verlust stoppt auch Cleanup.

`event_write_recovery_required` meldet zurückgebliebene oder unsichere Bestände
nach begonnenem Write. Andere Rejections sind ebenfalls **keine** Erlaubnis zur
automatischen Reparatur. Nach Crash: Zustand read-only untersuchen, vorhandene
Bytes sichern und separat autorisierte Recovery unter eigener Lease durchführen;
erneutes Apply erst nach geklärtem Ausgangszustand. Es gibt **kein Journal** und
**kein All-or-nothing** über beide Evidence-Zonen. Gegen einen bösartigen,
gleichzeitig umhängenden Dateisystemakteur wird keine Race-Freiheit behauptet.

## Receipt und echte Integration

Erfolg liefert `artifacts` und `output_receipt` mit genau
`schema_version: 1`, `work_order_sha256`, `source_snapshot_sha256`,
`outputs: [{path, sha256}]` über alle fünf Output-Datei-Bytes. Das Receipt wird
nicht in eine selbst referenzierende Output-Hashdatei geschrieben. Immer
`content_approved: false`, `content_review: "human_content_review_required"`;
keine `approval_receipt`.

`skills/mail-desk/tests/test_event_dossier_writer_integration.py` führt echten
Projekt-Fassaden-Handoff → echten Writer → echten Report aus. Nur das vorgelagerte
operative Mailbox-Apply bleibt eine explizite synthetische Fixture, kein Live-E2E.
Topic-Tests verbinden echten Writer und erweiterten read-only Report; ein
Topic-Handoff wird nicht erfunden. Der Report verlangt weiterhin das Rich-Profil
und prüft lokale Hashintegrität, keine Katalog-/Quellen-Echtheit oder Freigabe.
