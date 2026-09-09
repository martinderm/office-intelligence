"""Focused safety and contract tests for the mailbox-read-only dossier mode."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


MAIL_DESK_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(MAIL_DESK_ROOT / "scripts"))

from core.modes.dossier import run_dossier_mode  # noqa: E402


PROJECT = {
    "id": "meshe",
    "title": "MESHE",
    "status": "active",
    "kuerzel": "MESHE",
    "aliases": ["Microcredentials"],
    "domains": ["eucen.eu"],
    "contacts": [{"email": "partner@eucen.eu"}],
}


class DossierModeTests(unittest.TestCase):
    def run_mode(self, config: dict, projects=None):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        data_dir = Path(temporary.name) / "data" / "mail-desk"
        data_dir.mkdir(parents=True)
        written: dict[str, object] = {}

        def write_json(path: Path, payload: object) -> None:
            written["path"] = path
            written["payload"] = payload

        result = run_dossier_mode(
            config,
            data_dir=data_dir,
            dependencies={
                "load_catalogs": lambda _root: ([PROJECT] if projects is None else projects, []),
                "atomic_write_json": write_json,
            },
        )
        return result, written, data_dir

    def test_prepares_bounded_inspect_handoff_from_safe_catalog_fields(self) -> None:
        result, written, data_dir = self.run_mode({"mode": "dossier", "project": "meshe", "max_count": 7})

        self.assertTrue(result["ok"])
        self.assertEqual(data_dir / "batch-dossier.json", written["path"])
        dossier = result["dossier"]
        self.assertEqual(7, dossier["max_count"])
        self.assertEqual("INBOX", dossier["next_request"]["folder"])
        self.assertIn('subject "MESHE"', dossier["next_request"]["query"])
        self.assertIn('from "eucen.eu"', dossier["next_request"]["query"])
        self.assertEqual("not_configured", dossier["cloud_atlas_preflight"]["state"])
        self.assertEqual([], dossier["cloud_atlas_preflight"]["storage_ids"])
        self.assertEqual(["inspect", "draft"], dossier["review"]["allowed_next_modes"])
        self.assertIn("execute", dossier["review"]["prohibited_automatic_steps"])

    def test_normalizes_one_catalog_domain_prefix_at_sign(self) -> None:
        project = dict(PROJECT, domains=["@eucen.eu"])
        result, _, _ = self.run_mode({"mode": "dossier", "project": "meshe"}, [project])

        self.assertIn('from "eucen.eu"', result["dossier"]["next_request"]["query"])

    def test_prepares_cloud_preflight_only_from_declared_storage_ids(self) -> None:
        project = dict(PROJECT, cloud_sync={"primary": {"scan_dir": "data/cloud/MESHE"}})
        result, _, _ = self.run_mode({"mode": "dossier", "project": "meshe"}, [project])

        handoff = result["dossier"]["cloud_atlas_preflight"]
        self.assertEqual("pending_review", handoff["state"])
        self.assertEqual(["primary"], handoff["storage_ids"])
        self.assertNotIn("scan_dir", handoff)
        self.assertIn("cloud_sync", handoff["prohibited_automatic_steps"])

    def test_excludes_empty_repeated_and_invalid_catalog_domains(self) -> None:
        project = dict(PROJECT, domains=["@", "@@eucen.eu", "eucen .eu", "https://eucen.eu"], contacts=[])
        result, _, _ = self.run_mode({"mode": "dossier", "project": "meshe"}, [project])

        self.assertNotIn('from "', result["dossier"]["next_request"]["query"])

    def test_derives_workspace_root_from_data_dir_not_current_directory(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        data_dir = Path(temporary.name) / "workspace" / "data" / "mail-desk"
        data_dir.mkdir(parents=True)
        roots: list[Path] = []

        with patch("core.modes.dossier.Path.cwd", return_value=Path(temporary.name) / "unrelated"):
            run_dossier_mode(
                {"mode": "dossier", "project": "meshe"},
                data_dir=data_dir,
                dependencies={
                    "load_catalogs": lambda root: (roots.append(root) or [PROJECT], []),
                    "atomic_write_json": lambda _path, _payload: None,
                },
            )

        self.assertEqual([data_dir.parent.parent], roots)

    def test_requires_exact_unique_active_project_and_bounded_count(self) -> None:
        legacy_project = dict(PROJECT)
        legacy_project.pop("status")
        result, _, _ = self.run_mode({"mode": "dossier", "project": "meshe"}, [legacy_project])
        self.assertTrue(result["ok"])

        for config, projects, message in (
            ({"mode": "dossier", "project": "MESHE"}, [PROJECT], "exactly one"),
            ({"mode": "dossier", "project": "meshe"}, [PROJECT, dict(PROJECT)], "exactly one"),
            ({"mode": "dossier", "project": "meshe"}, [dict(PROJECT, status="archived")], "not active"),
            ({"mode": "dossier", "project": "meshe"}, [dict(PROJECT, status=None)], "not active"),
            ({"mode": "dossier", "project": "meshe", "max_count": 51}, [PROJECT], "max_count"),
        ):
            with self.subTest(config=config):
                with self.assertRaisesRegex(ValueError, message):
                    self.run_mode(config, projects)

    def test_rejects_free_query_action_and_non_inbox_scope(self) -> None:
        for config, message in (
            ({"mode": "dossier", "project": "meshe", "query": "from attacker"}, "unsupported fields"),
            ({"mode": "dossier", "project": "meshe", "actions": {"execute": True}}, "unsupported fields"),
            ({"mode": "dossier", "project": "meshe", "source_folder": "Archive"}, "exactly INBOX"),
        ):
            with self.subTest(config=config):
                with self.assertRaisesRegex(ValueError, message):
                    self.run_mode(config)
