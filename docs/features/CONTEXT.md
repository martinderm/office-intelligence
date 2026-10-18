# docs/features — historische Feature-Request-Nachweise

## Aktuelles Routing

Einziger aktiver Backlog sind die
[GitHub Issues](https://github.com/martinderm/office-intelligence/issues).
Workstatus und Run-Abnahme gehören in das jeweilige Issue/PR und dessen Run.
Aktuelle Verträge/Specs und betroffene L1/L2-System-Maps bleiben kanonisch;
historische Paketkarten ersetzen diese nicht.

Keine neuen FR-Records, keine Statussynchronisation in Root-Ledger. Die frühere
Pflicht „beide Ledger synchronisieren“ ist durch betroffene Maps und aktuelle
Verträge sowie Issue-/PR-Evidence abgelöst (siehe [AGENTS.md](../../AGENTS.md)).

## Erhaltener Bestand

- `FR-<n>.md`: unveränderte Legacy-Records mit damaligem Frontmatter und Status.
  `catalog`/`progress` führen kompatibel zu historischen Root-Linkstubs, nicht
  zu einem aktiven Arbeitsauftrag. Offen bedeutet nicht nachträglich erledigt.
- [`_archive.md`](_archive.md): unveränderte historische Paketabnahmen und
  damalige Prozessbeschreibungen; dortige Zukunftsformulierungen sind **keine**
  aktuelle Routing-/Ledgerpflicht.
- [Ledger-Snapshot und Inventar](../archive/feature-ledgers/README.md):
  Quelldatum/-Commit, Hashes, unveränderte Textkopien und Linkauflösung.
- [`FEATURE-REQUESTS.md`](../../FEATURE-REQUESTS.md) und
  [`FEATURE-REQUEST-PROGRESS.md`](../../FEATURE-REQUEST-PROGRESS.md): kurze
  historische Linkstubs; keine neue Status- oder Evidenzablage.

Ungeklärte Legacy-Zuordnungen werden im einmaligen Archiv-Inventar benannt,
nicht stillschweigend abgeschlossen oder massenhaft in neue Issues umgewandelt.
Für spätere #19/#12-Reconciles: Snapshots bleiben eingefroren; neue Lane-Nachweise
nach Issue/PR/Run übernehmen, Rootstubs nicht wieder aufblähen. Konflikte seriell
am dann aktuellen Integrationsstand lösen, nicht in fremden Worktrees.
