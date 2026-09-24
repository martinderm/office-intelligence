# docs/features — Feature-Request-Records

Record library (ICM Form 3) für die Feature Requests des Bundles.

## Contents

- `FR-<n>.md` — Record je **aktivem** FR: YAML-Frontmatter (`id`, `type: feature-request`,
  `status`, `packages`, `next_package`, `blocker`) plus die verbindliche Paketkarte
  (SSOT: Ziel & Invarianten, Abnahme, Umsetzungsnachweis).
- `_archive.md` — Langzeit-Archiv aller abgeschlossenen FRs (Archivstatus-Tabelle +
  FR-Sektionen).
- `FEATURE-REQUEST-PROGRESS.md` bleibt als ephemeres Arbeitsdokument am Repository-Root
  (L4-Produkt, wird nach jeder Abnahme bereinigt) — bewusst NICHT hier, damit der
  ephemere Stand nicht mit den stabilen Records vermischt wird.

## Routing

- „Wo ist der Status aller FRs?" → Katalog [`../../FEATURE-REQUESTS.md`](../../FEATURE-REQUESTS.md) (Status-Tabelle).
- „Spezifikation von FR-X?" → Record-File in diesem Ordner.
- „Was ist schon erledigt?" → `_archive.md` (Archivstatus-Tabelle).

## Regeln

- Der Record ist die **eine Heimat** jedes FR-Fakts (one home per fact); der Katalog
  trägt nur Statuszeile + Link.
- Statusänderungen erfolgen **im Record** (Frontmatter + Statuszeile) und werden
  synchron in die Katalog-Tabelle übernommen; der Katalog ist der Suchindex, nicht
  die Quelle.
- Neue FR = neues Record-File nach dem Frontmatter-Muster der Bestehenden (copy,
  not blank page); Archivierung = Abschnitt in `_archive.md` + Statuszeile aus dem
  Katalog entfernen.
