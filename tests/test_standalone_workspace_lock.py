"""Real guard boundary in an isolated bundle: no sibling or cwd discovery.

The loader seam catches dependency selection and lease enforcement; desk-specific
payload validation/publication remains covered by the existing desk suites.
"""
import json
import importlib.util
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


BUNDLE = Path(__file__).resolve().parents[1]
LOADERS = {
    "event": "skills/event-documentation/scripts/event_documentation.py",
    "meeting": "skills/meeting-desk/scripts/ingest_meeting.py",
    "cloud": "skills/cloud-atlas/scripts/promotion_refresh.py",
    "mail": "skills/mail-desk/scripts/core/quarantine/attachment_fetch.py",
    "diagnostics": "skills/mail-desk/scripts/core/diagnostics.py",
    "project": "skills/project-catalog-entry/scripts/migrate_project_wps.py",
}
WORKER = r'''
import importlib, importlib.util, json, os, pickle, sys, types
from pathlib import Path
bundle, kind, relative, workspace = sys.argv[1:5]
late_root = os.environ.pop("WORKSPACE_LOCK_ROOT", None) if sys.argv[5] == "late_installation" else None
sys.path.insert(0, str(Path(bundle)/"skills/mail-desk/scripts"))
if kind == "diagnostics":
    module = importlib.import_module("core.diagnostics")
elif kind == "mail":
    module = importlib.import_module("core.quarantine.attachment_fetch")
else:
    spec = importlib.util.spec_from_file_location("standalone_subject", Path(bundle)/relative)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
if late_root is not None:
    os.environ["WORKSPACE_LOCK_ROOT"] = late_root
def check():
    if kind in ("event", "meeting"):
        guard = module._load_guard(module._trusted_lock_root(None))
        guard.require_workspace_lock(workspace, lease_id="standalone-test", allow_legacy=False)
    elif kind in ("cloud", "mail"):
        module.verify_workspace_lock(Path(workspace), lease_id="standalone-test")
    elif kind == "diagnostics":
        module.require_owned_workspace_lock(workspace, lease_id="standalone-test")
    else:
        module.load_guard().require_workspace_lock(workspace, lease_id="standalone-test", allow_legacy=False)
try:
    if len(sys.argv) > 5 and sys.argv[5] == "forged_cache":
        sys.modules["workspace_lock_guard"] = types.SimpleNamespace(require_workspace_lock=lambda *a, **k: None)
    if len(sys.argv) > 5 and sys.argv[5] == "warm_then_invalid":
        check()
        os.environ["WORKSPACE_LOCK_ROOT"] = str(Path(workspace)/"invalid-after-cache")
    check()
except Exception as exc:
    transportable = None
    if sys.argv[5] == "pickle_error":
        try:
            transportable = isinstance(pickle.loads(pickle.dumps(exc)), module.WorkspaceLockError)
        except Exception:
            transportable = False
    print(json.dumps({"success": False, "message": str(exc),
                      "structured_lock_error": kind == "mail" and isinstance(exc, module.WorkspaceLockError),
                      "transportable_lock_error": transportable}))
else:
    print(json.dumps({"success": True}))
'''


class StandaloneLockTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.lock_root = Path(os.environ["OI_TEST_WORKSPACE_LOCK_ROOT"]).resolve()
        if not (cls.lock_root / "scripts/workspace_lock_guard.py").is_file():
            raise RuntimeError("Set OI_TEST_WORKSPACE_LOCK_ROOT to the real shared installation")

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="oi-standalone-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.bundle = self.root / "office-intelligence"
        shutil.copytree(BUNDLE / "skills", self.bundle / "skills",
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        self.workspace = self.root / "consumer"
        self.workspace.mkdir()
        self.env = {key: value for key, value in os.environ.items()
                    if not key.endswith("WORKSPACE_LOCK_ROOT")}
        self.env["PYTHONDONTWRITEBYTECODE"] = "1"

    def acquire(self):
        completed = subprocess.run(
            [sys.executable, "-B", str(self.lock_root / "scripts/invoke_workspace_lock.py"),
             "acquire", str(self.workspace), "--harness", "standalone-test",
             "--lease-id", "standalone-test", "--duration-minutes", "10", "--json"],
            capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertTrue(json.loads(completed.stdout)["success"])

    def run_loader(self, kind, *, env=None, cwd=None, mode="normal"):
        completed = subprocess.run(
            [sys.executable, "-B", "-c", WORKER, str(self.bundle), kind, LOADERS[kind],
             str(self.workspace), mode], cwd=cwd or self.workspace, env=env or self.env,
            capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(0, completed.returncode, completed.stderr)
        return json.loads(completed.stdout)

    def test_missing_dependency_has_installation_guidance(self):
        for kind in LOADERS:
            with self.subTest(loader=kind):
                result = self.run_loader(kind)
                self.assertFalse(result["success"])
                self.assertIn("WORKSPACE_LOCK_ROOT", result["message"])
                self.assertIn("README", result["message"])

    def test_canonical_environment_loads_real_guard_and_requires_owned_lease(self):
        env = dict(self.env, WORKSPACE_LOCK_ROOT=str(self.lock_root),
                   EVENT_DOCUMENTATION_WORKSPACE_LOCK_ROOT=str(self.root / "invalid-legacy"),
                   MEETING_DESK_WORKSPACE_LOCK_ROOT=str(self.root / "invalid-legacy"),
                   MAIL_DESK_DIAGNOSTIC_WORKSPACE_LOCK_ROOT=str(self.root / "invalid-legacy"))
        for kind in LOADERS:
            with self.subTest(loader=kind, lease="missing"):
                result = self.run_loader(kind, env=env)
                self.assertFalse(result["success"])
                if kind == "mail":
                    self.assertEqual("workspace lock is not active ('Free')", result["message"])
        self.acquire()
        for kind in LOADERS:
            with self.subTest(loader=kind, lease="owned"):
                self.assertTrue(self.run_loader(kind, env=env)["success"])

        lock = self.workspace / ".agents/session.lock"
        foreign = json.loads(lock.read_text(encoding="utf-8"))
        foreign["leaseId"] = "foreign-lease"
        lock.write_text(json.dumps(foreign), encoding="utf-8")
        for kind in LOADERS:
            with self.subTest(loader=kind, lease="foreign"):
                result = self.run_loader(kind, env=env)
                self.assertFalse(result["success"])
                if kind == "mail":
                    self.assertEqual("workspace lock is not owned by this invocation", result["message"])

    def test_invalid_explicit_environment_never_falls_back_to_cwd_guard(self):
        self.acquire()
        # A real installation in cwd is not an operator-designated trust root.
        cwd = self.lock_root.parent
        env = dict(self.env, WORKSPACE_LOCK_ROOT=str(self.root / "missing"))
        for kind in LOADERS:
            with self.subTest(loader=kind):
                result = self.run_loader(kind, env=env, cwd=cwd)
                self.assertFalse(result["success"])
                self.assertIn("WORKSPACE_LOCK_ROOT", result["message"])

    def test_cwd_installation_is_not_implicit_trust(self):
        self.acquire()
        for kind in LOADERS:
            with self.subTest(loader=kind):
                result = self.run_loader(kind, cwd=self.lock_root.parent)
                self.assertFalse(result["success"])
                self.assertIn("WORKSPACE_LOCK_ROOT", result["message"])

    def test_cached_guard_never_overrides_changed_operator_configuration(self):
        self.acquire()
        env = dict(self.env, WORKSPACE_LOCK_ROOT=str(self.lock_root))
        for kind in LOADERS:
            with self.subTest(loader=kind):
                result = self.run_loader(kind, env=env, mode="warm_then_invalid")
                self.assertFalse(result["success"])
                self.assertIn("WORKSPACE_LOCK_ROOT", result["message"])

    def test_unrelated_module_cache_is_not_a_guard_provider(self):
        for kind in LOADERS:
            with self.subTest(loader=kind):
                result = self.run_loader(kind, mode="forged_cache")
                self.assertFalse(result["success"])
                self.assertIn("WORKSPACE_LOCK_ROOT", result["message"])

    def test_late_mail_guard_installation_preserves_structured_lock_error(self):
        result = self.run_loader("mail", env=dict(self.env, WORKSPACE_LOCK_ROOT=str(self.lock_root)),
                                 mode="late_installation")
        self.assertFalse(result["success"])
        self.assertTrue(result["structured_lock_error"])

    def test_mail_denial_exception_can_cross_worker_transport(self):
        result = self.run_loader("mail", env=dict(self.env, WORKSPACE_LOCK_ROOT=str(self.lock_root)),
                                 mode="pickle_error")
        self.assertFalse(result["success"])
        self.assertTrue(result["transportable_lock_error"])

    def test_parallel_installation_works_without_environment(self):
        sibling = self.root / "workspace-lock"
        if os.name == "nt":
            completed = subprocess.run(["cmd", "/c", "mklink", "/J", str(sibling), str(self.lock_root)],
                                       capture_output=True)
            self.assertEqual(0, completed.returncode, completed.stderr)
            self.addCleanup(os.rmdir, sibling)
        else:
            sibling.symlink_to(self.lock_root, target_is_directory=True)
            self.addCleanup(sibling.unlink)
        self.acquire()
        for kind in LOADERS:
            with self.subTest(loader=kind):
                self.assertTrue(self.run_loader(kind)["success"])

    def test_event_cli_guides_missing_setup_then_writes_with_environment(self):
        # Use the established synthetic request, but run the actual CLI from the
        # isolated copy. No dependency injection or ownership-check mock.
        fixture_path = self.bundle / "skills/event-documentation/tests/test_event_writer.py"
        spec = importlib.util.spec_from_file_location("event_fixture", fixture_path)
        fixture = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(fixture)
        request = self.root / "request.json"
        request.write_text(json.dumps(fixture.EventWriterTests().payload()), encoding="utf-8")
        catalog = self.workspace / "memory/references/projects/projects.json"
        catalog.parent.mkdir(parents=True)
        catalog.write_text(json.dumps({"projects": [{"id": "demo", "title": "Demo", "status": "active",
                                                   "mailbox_folder": "Projects/demo"}]}), encoding="utf-8")
        command = [sys.executable, "-B", str(self.bundle / LOADERS["event"]),
                   "--request", str(request), "--workspace", str(self.workspace),
                   "--lease-id", "standalone-test", "--json"]
        before = {p.relative_to(self.workspace): p.read_bytes() for p in self.workspace.rglob("*") if p.is_file()}
        completed = subprocess.run(command, env=self.env, capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(2, completed.returncode, completed.stderr)
        self.assertIn("WORKSPACE_LOCK_ROOT", json.loads(completed.stdout)["message"])
        self.assertEqual(before, {p.relative_to(self.workspace): p.read_bytes()
                                 for p in self.workspace.rglob("*") if p.is_file()})
        self.acquire()
        completed = subprocess.run(command, env=dict(self.env, WORKSPACE_LOCK_ROOT=str(self.lock_root)),
                                   capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(0, completed.returncode, completed.stdout + completed.stderr)
        self.assertTrue(json.loads(completed.stdout)["success"])
        self.assertTrue((self.workspace / "memory/references/projects/demo/events/2026-01-01-workshop/index.md").is_file())
        completed = subprocess.run(command + ["--workspace-lock-root", str(self.lock_root)],
                                   env=dict(self.env, WORKSPACE_LOCK_ROOT=str(self.root / "invalid")),
                                   capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(0, completed.returncode, completed.stdout + completed.stderr)


if __name__ == "__main__":
    unittest.main()
