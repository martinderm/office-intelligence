# Historische Root-FR-Ledger — Snapshot 2026-10-18

Einmaliges Archiv-Inventar, **kein neues Meta-Ledger**. Einziger aktiver Backlog:
[GitHub Issues](https://github.com/martinderm/office-intelligence/issues).
Workstatus und Run-Abnahme stehen im Issue/PR/Run; aktuelle Verträge und L1/L2-Maps
bleiben kanonisch. Die archivierten Zukunftsformulierungen sind historische
Aussagen, keine aktive Ledger-/Record-Pflicht.

## Provenienz und unveränderter Inhalt

Quelle beider Snapshots: Root-Dateien aus Commit
`38b9ba2679d67592f2f5d7bc61548789be2004e6`, Quelldatum
`2026-10-18T19:23:38+02:00` (Commit-Zeit). Die UTF-8/LF-Dateien entsprechen
**bytegetreu den Git-Blobs**, nicht einer nachträglich bereinigten Darstellung.

| Snapshot | Bytes | SHA-256 |
| --- | ---: | --- |
| [Katalog](2026-10-18/FEATURE-REQUESTS.md.txt) | 8785 | `5cb72c1c18d93f0a2ea68cee78c0b6336b3ae2fbb25329fb984ac500fce8fffd` |
| [Progress](2026-10-18/FEATURE-REQUEST-PROGRESS.md.txt) | 76476 | `c9c58c06805d5c7759993bced84962b1b6e4ef604cb39aa08c383a56f72c9478` |

Die Erweiterung `.md.txt` ist bewusst gewählt: der Inhalt bleibt unverändert
als Textnachweis lesbar, wird aber nicht mit nun falscher relativer Markdown-Basis
als aktiver Router ausgegeben. Ursprüngliche Basis aller enthaltenen relativen
Links war der **Repository-Root**, nicht dieses Archivverzeichnis. Die folgende
Linkauflösung ersetzt die Navigation, ohne historische Bytes umzuschreiben.

## Auflösung der damaligen relativen Markdown-Links

Alle unterschiedlichen relativen Markdown-Ziele der beiden Snapshots:

| Ursprüngliches Ziel (Basis: Root) | Heute auflösbares Ziel |
| --- | --- |
| `FEATURE-REQUESTS.md` | [unveränderter damaliger Katalog](2026-10-18/FEATURE-REQUESTS.md.txt); [Root-Stub](../../../FEATURE-REQUESTS.md) |
| `FEATURE-REQUEST-PROGRESS.md` | [unveränderter damaliger Progress](2026-10-18/FEATURE-REQUEST-PROGRESS.md.txt); [Root-Stub](../../../FEATURE-REQUEST-PROGRESS.md) |
| `docs/features/` | [historische Record-Library](../../features/CONTEXT.md) |
| `docs/features/FR-10.md` | [FR-10](../../features/FR-10.md) |
| `docs/features/FR-12.md` | [FR-12](../../features/FR-12.md) |
| `docs/features/FR-25.md` | [FR-25](../../features/FR-25.md) |
| `docs/features/FR-28.md` | [FR-28](../../features/FR-28.md) |
| `docs/features/_archive.md` | [historische Paketabnahmen](../../features/_archive.md) |
| `docs/system-map/objects.md` | [aktuelle Objektkarte](../../system-map/objects.md) |
| `COMPLIANCE-REPORT-AGENT-ARCHITECTURE.md` | [historischer Bericht](../../../COMPLIANCE-REPORT-AGENT-ARCHITECTURE.md) |

Absolute GitHub-URLs bleiben als Quelltext vollständig erhalten; ihnen wird keine
neue lokale Basis zugewiesen. Inbound-Links und `catalog`/`progress`-Frontmatter
der vier Legacy-Records bleiben über die kurzen Rootstubs auflösbar. Im erfassten
Repo-Bestand gibt es keine URL-Fragmente auf die beiden Root-Ledger.
Die alten Textverweise „§ FR-17“/„§ FR-22“ im Progress hatten schon im Quellstand
keine entsprechende Katalog-Sektion; sie werden nicht als funktionierende Anker
ausgegeben. Externe unbekannte Fragment-Verbraucher sind nicht geprüft; für
deren historische Ziele bleibt der Quellcommit maßgeblich.

## Legacy-Inventar — damaliger Status, keine neue Abschlussentscheidung

| Record | Katalogstatus im Snapshot | Frontmatter unverändert | Explizite Issue-Zuordnung im Bestand |
| --- | --- | --- | --- |
| [FR-10](../../features/FR-10.md) | geplant (MD-G1) | `geplant` | keine festgestellt |
| [FR-12](../../features/FR-12.md) | geplant (CA-M1) | `geplant` | keine festgestellt |
| [FR-25](../../features/FR-25.md) | geplant (low), MD-P1/MD-P2 | `geplant-low` | keine festgestellt |
| [FR-28](../../features/FR-28.md) | in Umsetzung (DOC-P1 läuft) | `in-umsetzung` | [#4](https://github.com/martinderm/office-intelligence/issues/4) |

**Fehlende fachliche Migrationsentscheidung:** Für FR-10/12/25 ist keine explizite
Issue-Zuordnung im Bestand dokumentiert. Ob weiterzuverfolgen, bereits anderswo
abgedeckt oder bewusst zurückzustellen, entscheidet der Owner gesondert. Es
wurden keine neuen Issues angelegt und keine Legacy-Status als erledigt umgedeutet.
FR-12 ist eine Legacy-ID, nicht automatisch das gleich nummerierte GitHub-Issue.

Unverändert erhaltene Widersprüche: FR-17 steht in der Archivstatus-Tabelle als
abgeschlossen, im eigenen historischen Abschnitt jedoch als geplant. FR-25 steht
aktiv/geplant im Katalog, wird aber durch dessen Prosa-Range „21–26“ zugleich
als abgeschlossen eingeordnet. Diese Unstimmigkeiten werden hier nur benannt,
nicht still aufgelöst. Die unterschiedliche Reihenfolge FR-14/13 in der
Archivstatus-Tabelle bleibt ebenfalls erhalten.

Die übrigen Issue-Nachweise im Snapshot (#2/#8/#9/#10/#11/#14/#16/#20) sind
ausdrücklich **keine neuen FR-Records** und keine aktuelle Statusliste.
Die unveränderten Abnahmen stehen in [docs/features/_archive.md](../../features/_archive.md);
der bestehende [FEATURE-REQUEST-ARCHIVE.md-Stub](../../../FEATURE-REQUEST-ARCHIVE.md)
bleibt bestehen. Historische Aussagen zu #18/#19 entscheiden deren heutigen
Status nicht neu.

## Spätere Lane-Reconciles

Die pausierte #19-Lane enthält laut Writerübergabe bereits zusätzliche Progress-
und Katalogzeilen, #12 uncommittierte Ledger-Nachweise in ihrer eigenen Lane.
Diese fremden Inhalte sind **nicht** Bestandteil dieses Main-Snapshots und wurden
nicht verändert oder verworfen. Beim späteren seriellen Reconcile nach Merge:
Snapshots eingefroren lassen, zusätzliche Lane-Nachweise verlustfrei im jeweiligen
Issue/PR/Run erhalten und die Rootstubs nicht wieder als Ledger aufbauen.
Konkrete Konflikte erst am dann aktuellen Stand lösen; keine pauschale
Konfliktauflösung oder Übernahme fremder Code-/Reviewstände durch diese Migration.
