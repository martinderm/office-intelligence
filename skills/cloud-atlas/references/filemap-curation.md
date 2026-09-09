# Filemap-Curation-Overlay

`curation_json` ist optional pro deklarierter `cloud_sync`-Storage-Konfiguration. Ist es nicht gesetzt, bleibt Cloud Atlas vollständig rückwärtskompatibel. Die Datei ist der versionierte SSOT für manuelle Beschreibungen und fachliche Zusatzfelder; `memory/cloud/**/filemap.json` bleibt lokaler, abgeleiteter Zustand und darf gitignoriert sein.

Der Lauf lädt das Overlay ausschließlich über den Katalog, prüft Datei, JSON, Schema, Workspace-Grenze sowie exakte Scope-, Target- und Storage-Identität **vor** jeder Konvertierung oder Ausgabe. Die `source_receipt.sha256` bindet die Filemap-Extraktion, aus der kuratiert wurde; der Lauf akzeptiert sie als Provenienz und verlangt die alte Filemap nicht.

```json
{
  "$schema": "https://raw.githubusercontent.com/martinderm/office-intelligence/main/skills/cloud-atlas/references/filemap-curation.schema.json",
  "schema_version": 1,
  "kind": "cloud-filemap-curation",
  "scope": "project",
  "target": {"id": "meshe"},
  "storage_id": "default",
  "source_receipt": {
    "sha256": "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
    "file_count": 4,
    "created_at": "2026-09-09T20:00:00+02:00"
  },
  "entries": {
    "data/cloud/MESHE/Vertrag.pdf": {
      "source_sha256": "abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789",
      "description": "Unterzeichneter Hauptvertrag",
      "custom": {"category": "legal", "reviewed": true}
    }
  }
}
```

`scope` ist bei Projekten `project`, bei Topics `topic` und bei Subtopics `subtopic`; ein Subtopic verwendet `target: {"topic_id": "…", "subtopic_id": "…"}`. Entries sind an ihren workspace-relativen Quellpfad unter `scan_dir` gebunden. Falls ein Pfad umbenannt wurde, kann `source_sha256` genau einen passenden aktuellen Inhalt wiederfinden. Mehrdeutige Hash-Treffer brechen fail-closed ab.

Nur `description` und die Felder unter `custom` werden in die Filemap übernommen. Scanner- und Converter-Felder wie `size`, `mtime`, `sha256`, Mirror/Derivat, OCR-, Konvertierungs- und Artifact-Metadaten sind verboten. Der aktuelle technische Scan bleibt somit immer maßgeblich. Sobald ein Overlay aktiv ist, ist es die vollständige Curation-Ansicht: Nicht enthaltene Einträge oder Keys entfernen alte lokale Beschreibungen/Zusatzfelder statt sie aus der abgeleiteten Filemap wiederzubeleben.
