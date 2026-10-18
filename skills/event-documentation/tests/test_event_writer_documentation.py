"""Normative draft/bootstrap changes must stay explicit; no runtime I/O."""
from pathlib import Path
import unittest

BUNDLE = Path(__file__).resolve().parents[3]


class WriterDocumentationTests(unittest.TestCase):
    def test_event_skill_explicitly_allows_unreviewed_mail_drafts(self):
        text = (BUNDLE / "skills/event-documentation/SKILL.md").read_text(encoding="utf-8")
        self.assertIn("UNGEPRÜFTER Entwurf", text)
        self.assertNotIn("Das Event-Dossier wird nicht aus Mailtext abgeleitet.", text)
        self.assertIn("event-writer.md", text)

    def test_topic_bootstrap_is_not_a_matcher_bypass(self):
        text = (BUNDLE / "skills/topic-catalog-entry/SKILL.md").read_text(encoding="utf-8")
        self.assertIn("Bootstrap-Ausnahme", text)
        self.assertIn("Matcher-Anforderungen bleiben unverändert", text)
        self.assertIn("menschliche Dossiers", text)

    def test_writer_contract_states_bounded_recovery_and_real_integration(self):
        text = (BUNDLE / "skills/event-documentation/references/event-writer.md").read_text(encoding="utf-8")
        for phrase in ("No-Clobber", "kein Journal", "kein All-or-nothing", "Recovery",
                       "EVENT_DOCUMENTATION_WORKSPACE_LOCK_ROOT", "test_event_dossier_writer_integration.py"):
            self.assertIn(phrase, text)


if __name__ == "__main__":
    unittest.main()
