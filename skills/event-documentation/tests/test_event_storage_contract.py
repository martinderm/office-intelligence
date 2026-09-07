#!/usr/bin/env python3
"""Contract tests for Cloud-Atlas-backed event storage documentation."""

import os
from pathlib import Path
import re
import unittest


SKILL_ROOT = Path(__file__).resolve().parents[1]
SKILL = (SKILL_ROOT / "SKILL.md").read_text(encoding="utf-8")
TEMPLATE = (SKILL_ROOT / "references" / "event-folder-template.md").read_text(encoding="utf-8")


class EventStorageContractTests(unittest.TestCase):
    def test_new_events_require_an_existing_catalogued_cloud_atlas_storage(self):
        self.assertRegex(SKILL, r"bestehenden(?:\*\*)?\s+`cloud_sync\.<storage_id>`")
        self.assertRegex(SKILL, r"Fehlt er, keinen Event-Speicher improvisieren")
        self.assertRegex(TEMPLATE, r"bestehenden\s+`cloud_sync\.<storage_id>`")
        self.assertIn("`project-catalog-entry`", SKILL)
        self.assertIn("`topic-catalog-entry`", SKILL)
        self.assertIn("Erst danach löst `cloud-atlas`", SKILL)
        self.assertIn("`project-catalog-entry`", TEMPLATE)
        self.assertIn("`topic-catalog-entry`", TEMPLATE)
        self.assertNotIn("Katalogeintrag mit `cloud-atlas` pflegen", SKILL)

    def test_storage_configuration_separates_relative_mount_from_local_outputs(self):
        self.assertRegex(SKILL, r"cloud_sync\.<storage_id>\.scan_dir.*workspace-relativer Pfad")
        self.assertRegex(SKILL, r"output_dir`, `output_json` und `output_md`")
        self.assertIn('cloud_storage_id: "<storage_id>"', TEMPLATE)
        self.assertIn("cloud_filemap:", TEMPLATE)
        self.assertRegex(TEMPLATE, r"`scan_dir`, `output_dir`.*workspace-relativ")

    def test_links_preserve_dual_evidence_and_are_portable(self):
        self.assertIn("memory/references/", SKILL)
        self.assertIn("memory/evidence/", SKILL)
        self.assertRegex(SKILL, r"nur mit.*relativen Workspace-Links")
        self.assertRegex(TEMPLATE, r"alle müssen workspace-relativ sein")
        self.assertIn("Todoist", TEMPLATE)
        self.assertIn("Beleganker", TEMPLATE)

    def test_topic_and_project_examples_are_derived_from_their_actual_paths(self):
        examples = (
            (
                SKILL,
                Path("memory/references/topics/<topic>/subtopics/<subtopic>/events/<slug>/index.md"),
                Path("memory/evidence/topics/<topic>/events/<slug>/recordings/2026-06-04-keynote.summary.md"),
                "../../../../../../../evidence/topics/<topic>/events/<slug>/recordings/2026-06-04-keynote.summary.md",
            ),
            (
                SKILL,
                Path("memory/references/projects/<project>/events/<slug>/index.md"),
                Path("memory/evidence/projects/<project>/events/<slug>/recordings/2026-06-04-keynote.summary.md"),
                "../../../../../evidence/projects/<project>/events/<slug>/recordings/2026-06-04-keynote.summary.md",
            ),
            (
                TEMPLATE,
                Path("memory/references/topics/<topic>/subtopics/<subtopic>/events/<event-slug>/index.md"),
                Path("memory/evidence/topics/<topic>/events/<event-slug>/recordings/YYYY-MM-DD-<slug>.summary.md"),
                "../../../../../../../evidence/topics/<topic>/events/<event-slug>/recordings/YYYY-MM-DD-<slug>.summary.md",
            ),
            (
                TEMPLATE,
                Path("memory/references/projects/<project>/events/<event-slug>/index.md"),
                Path("memory/evidence/projects/<project>/events/<event-slug>/recordings/YYYY-MM-DD-<slug>.summary.md"),
                "../../../../../evidence/projects/<project>/events/<event-slug>/recordings/YYYY-MM-DD-<slug>.summary.md",
            ),
        )

        for document, source, target, link in examples:
            self.assertIn(link, document)
            derived_link = Path(os.path.relpath(target, start=source.parent)).as_posix()
            resolved_target = Path(os.path.normpath(os.path.join(source.parent, link))).as_posix()
            self.assertEqual(link, derived_link)
            self.assertEqual(target.as_posix(), resolved_target)

        for document in (SKILL, TEMPLATE):
            self.assertIn("tatsächlichen Workspace-Pfaden", document)

    def test_agent_share_is_migration_only_and_never_template_output(self):
        legacy_links = re.findall(r"`/Agent-Share/`", SKILL)
        self.assertEqual(1, len(legacy_links))
        migration_section = SKILL.split("### Migration vorhandener Altlinks", 1)[1]
        self.assertIn("nicht neu erzeugt", migration_section)
        self.assertIn("nicht automatisch ersetzt", migration_section)
        self.assertNotIn("Agent-Share", TEMPLATE)


if __name__ == "__main__":
    unittest.main()
