# Filemap-Schema (OI-08)

`filemap.json` ist ein `cloud-filemap`-Container und kein einzelnes
`data-zone-artifact`. Der Container beschreibt den Scope, den Storage, den
gescannten Pfad und ein Inventar von Quelldateien. Die bestehenden
Inventarfelder (`version`, `mtime`, `size`, `sha256`, `description`,
Konvertierungsstatus und kuratierte Zusatzfelder) bleiben kompatibel.

Für einen konvertierten lokalen Markdown-Mirror kann ein Dateieintrag
`artifact_metadata` enthalten. Dieses Objekt ist ausschließlich der kanonische
Cloud-Metadatensatz des konkreten Mirrors und folgt dem normativen
`data-zone-artifact.schema.json` aus `Shared-Memory`; die Filemap selbst wird
nicht gegen dieses Artifact-Schema validiert. Legacy-Mirrors ohne kanonisches
Frontmatter bleiben lesbar und erhalten kein künstlich erzeugtes
`artifact_metadata`.

Die Generator-Ausgabe verwendet `schema_version: 1` und wird vor dem atomaren
Schreiben deterministisch geprüft. Dateischlüssel und Pfade müssen
workspace-relativ sein; SHA-256-Werte werden als 64-stellige Hexwerte und
Zeit-/Größenfelder in den bestehenden Filemap-Formaten geprüft. Unbekannte
Dateieintragsfelder bleiben für manuell kuratierte Metadaten zulässig.
