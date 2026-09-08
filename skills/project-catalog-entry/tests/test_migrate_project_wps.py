from __future__ import annotations
import importlib.util, json, os, subprocess, sys, tempfile, unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "migrate_project_wps.py"
LOCK = ROOT.parents[2] / "workspace-lock" / "scripts" / "invoke_workspace_lock.py"

def module():
    spec = importlib.util.spec_from_file_location("migrate_project_wps", SCRIPT); assert spec and spec.loader
    value = importlib.util.module_from_spec(spec); sys.modules[spec.name] = value; spec.loader.exec_module(value); return value

migration = module()

def catalog(version=2, workpackages=None):
    return [{"id":"meshe","title":"MESHE-like","mailbox_folder":"Projects/MESHE","aliases":["MESHE"],"cloud_sync":{"x":{"scan_dir":"x"}},"workpackages": workpackages or [],"schema_version":version}]

class MigrationTests(unittest.TestCase):
    def make_workspace(self, body=""):
        temp = tempfile.TemporaryDirectory(); root = Path(temp.name); path = root / "memory/references/projects/meshe/workpackages"; path.mkdir(parents=True)
        (path / "wp1-coordination.md").write_text(body, encoding="utf-8")
        index = path.parent / "index.md"; index.write_text("## Milestones\n- MS1 — Kick-off — Related WPs: wp1\n", encoding="utf-8")
        catalog_path = root / "memory/references/projects/projects.json"; catalog_path.write_text(json.dumps(catalog()), encoding="utf-8")
        return temp, root, catalog_path

    def run_cli(self, root, catalog_path, *extra):
        return subprocess.run([sys.executable, str(SCRIPT), "--catalog", str(catalog_path), "--projects-root", str(root / "memory/references/projects"), "--json", *extra], text=True, capture_output=True, check=False)

    def test_meshe_like_dry_run_is_lossless_and_extracts_project_milestone(self):
        temp, root, path = self.make_workspace("# WP1 — Coordination\n## Scope\n- Lead: EUCEN\n- BOKU-Rolle: Contributor\n- Status: active\n- WP-Nummer: 1\n## Tasks\n- T1.1 — Kick-off — Lead: EUCEN\n## Deliverables\n- D1.1 — Plan — Type: Report — Due: M3\n")
        with temp:
            before = path.read_text(encoding="utf-8"); result = self.run_cli(root, path)
            self.assertEqual(result.returncode, 0, result.stderr); data=json.loads(result.stdout)["data"]
            self.assertIn('"schema_version": 3', data["diff"]); self.assertIn('"MS1"', data["diff"]); self.assertEqual(path.read_text(encoding="utf-8"), before)

    def test_project_without_workpackages_migrates_and_v3_is_idempotent(self):
        temp, root, path = self.make_workspace("")
        with temp:
            (root / "memory/references/projects/meshe/workpackages/wp1-coordination.md").unlink()
            result = self.run_cli(root, path); self.assertEqual(result.returncode, 0); self.assertIn('"milestones": []', json.loads(result.stdout)["data"]["diff"])
            path.write_text(json.dumps([{**catalog()[0], "schema_version": 3, "milestones": []}]), encoding="utf-8")
            result = self.run_cli(root, path); self.assertEqual(json.loads(result.stdout)["state"], "NoChanges")

    def test_ambiguity_blocks_migration(self):
        temp, root, path = self.make_workspace("# Coordination without an identifier\n")
        with temp:
            result=self.run_cli(root,path); self.assertEqual(result.returncode,1); self.assertEqual(json.loads(result.stdout)["state"],"PendingReview")

    def test_existing_routing_and_unmentioned_structures_are_preserved(self):
        temp, root, path = self.make_workspace("# WP1 — Coordination\n")
        with temp:
            document = catalog(workpackages=[{"id":"wp2","title":"Legacy WP","status":"active","aliases":["keep"]}])
            document[0]["milestones"] = [{"id":"MS0","title":"Existing milestone"}]
            after, diagnostics = migration.transform(document, root / "memory/references/projects", [])
            self.assertEqual(diagnostics, [])
            self.assertEqual(after[0]["aliases"], ["MESHE"])
            self.assertEqual(after[0]["cloud_sync"], {"x":{"scan_dir":"x"}})
            self.assertEqual(after[0]["workpackages"][1]["aliases"], ["keep"])
            self.assertEqual(after[0]["milestones"][0]["id"], "MS0")

    def test_evolve_task_heading_and_checkpoint_warning_are_not_blocking(self):
        temp, root, path = self.make_workspace("# WP1 — Coordination\n## Tasks\n### Task 1.1 — Stable EVOLVE task\n- checkpoint discussed\n")
        with temp:
            result = self.run_cli(root, path); payload = json.loads(result.stdout)
            self.assertEqual(result.returncode, 0, payload)
            self.assertIn('"Task 1.1"', payload["data"]["diff"])
            self.assertEqual(payload["data"]["diagnostics"][0]["severity"], "warning")

    def test_parenthesized_wp_milestone_with_nested_metadata_is_project_wide(self):
        temp, root, path = self.make_workspace("# WP1 — Coordination\n## Milestones (Antrag)\n- MS1 — Kick-off\n  - Lead Beneficiary: EUCEN\n  - Fälligkeit: M2\n  - Verifikation: Minutes\n")
        with temp:
            result = self.run_cli(root, path); payload = json.loads(result.stdout)
            self.assertEqual(result.returncode, 0, payload)
            self.assertIn('"related_wps": [', payload["data"]["diff"])
            self.assertIn('"due_month": "M2"', payload["data"]["diff"])

    def test_apply_refuses_remaining_warning(self):
        temp, root, path = self.make_workspace("# WP1 — Coordination\n- checkpoint discussed\n")
        with temp:
            lease="warning-lease"; subprocess.run([sys.executable,str(LOCK),"acquire",str(root),"--harness","test","--lease-id",lease,"--json"],text=True,capture_output=True,check=False)
            result = self.run_cli(root, path, "--apply", "--workspace-root", str(root), "--lease-id", lease)
            self.assertEqual(result.returncode, 1, result.stdout)
            self.assertEqual(json.loads(result.stdout)["state"], "PendingReview")

    def test_exact_warning_acceptance_permits_owned_apply_and_is_auditable(self):
        temp, root, path = self.make_workspace("# WP1 — Coordination\n- checkpoint discussed\n")
        with temp:
            lease="accepted-warning-lease"; subprocess.run([sys.executable,str(LOCK),"acquire",str(root),"--harness","test","--lease-id",lease,"--json"],text=True,capture_output=True,check=False)
            result = self.run_cli(root, path, "--apply", "--workspace-root", str(root), "--lease-id", lease, "--accept-warning", "unstable_checkpoint")
            payload=json.loads(result.stdout); self.assertEqual(result.returncode, 0, payload)
            self.assertEqual(payload["data"]["accepted_warning_codes"], ["unstable_checkpoint"])
            self.assertEqual(payload["data"]["unaccepted_warning_codes"], [])

    def test_unknown_warning_acceptance_fails_closed(self):
        temp, root, path = self.make_workspace("# WP1 — Coordination\n")
        with temp:
            result=self.run_cli(root,path,"--accept-warning","typo")
            payload=json.loads(result.stdout); self.assertEqual(result.returncode,1); self.assertEqual(payload["state"],"Invalid")

    def test_acceptance_never_bypasses_blocking_diagnostic(self):
        temp, root, path = self.make_workspace("# Coordination without identifier\n")
        with temp:
            result=self.run_cli(root,path,"--accept-warning","ambiguous_markdown")
            self.assertEqual(result.returncode,1); self.assertEqual(json.loads(result.stdout)["state"],"Invalid")

    def test_managed_decoy_is_not_promoted(self):
        temp, root, path = self.make_workspace("# WP1 — Coordination\n## Tasks\n- T1.1 — Real task\n## Aktueller Stand (Managed)\n- T9.9 — Decoy\n")
        with temp:
            payload=json.loads(self.run_cli(root,path).stdout)
            self.assertIn('"T1.1"', payload["data"]["diff"])
            self.assertNotIn('"T9.9"', payload["data"]["diff"])

    def test_partial_source_preserves_existing_and_blocks_conflict(self):
        temp, root, path = self.make_workspace("# WP1 — Coordination\n## Tasks\n- T1.1 — Changed title\n")
        with temp:
            original = json.loads(path.read_text(encoding="utf-8")); original[0]["workpackages"]=[{"id":"wp1","title":"Coordination","status":"active","tasks":[{"id":"T1.1","title":"Old title","lead":"BOKU"},{"id":"T1.2","title":"Keep"}],"deliverables":[]}]
            path.write_text(json.dumps(original),encoding="utf-8")
            payload=json.loads(self.run_cli(root,path).stdout)
            self.assertEqual(payload["state"], "PendingReview")
            self.assertIn("task_conflict", str(payload["data"]["diagnostics"]))

    def test_scoped_dry_run_defers_full_validation_but_apply_refuses_v2_remainder(self):
        temp, root, path = self.make_workspace("# WP1 — Coordination\n")
        with temp:
            current = json.loads(path.read_text(encoding="utf-8")); current.append({"id":"other","title":"Other","mailbox_folder":"Projects/Other","workpackages":[],"schema_version":2})
            path.write_text(json.dumps(current), encoding="utf-8")
            dry = self.run_cli(root, path, "--project", "meshe"); self.assertEqual(dry.returncode, 0, dry.stdout)
            self.assertEqual(json.loads(dry.stdout)["data"]["full_catalog_validation"], "deferred")
            lease="scope-lease"; subprocess.run([sys.executable,str(LOCK),"acquire",str(root),"--harness","test","--lease-id",lease,"--json"],text=True,capture_output=True,check=False)
            applied = self.run_cli(root, path, "--project", "meshe", "--apply", "--workspace-root", str(root), "--lease-id", lease)
            self.assertEqual(applied.returncode, 1, applied.stdout)

    def test_apply_requires_owned_lock_then_writes(self):
        temp, root, path = self.make_workspace("# WP1 — Coordination\n")
        with temp:
            no_lock=self.run_cli(root,path,"--apply","--workspace-root",str(root)); self.assertEqual(no_lock.returncode,2)
            lease="test-lease"; acquire=subprocess.run([sys.executable,str(LOCK),"acquire",str(root),"--harness","test","--lease-id",lease,"--json"],text=True,capture_output=True,check=False); self.assertEqual(acquire.returncode,0,acquire.stdout + acquire.stderr)
            applied=self.run_cli(root,path,"--apply","--workspace-root",str(root),"--lease-id",lease); self.assertEqual(applied.returncode,0,applied.stdout); self.assertEqual(json.loads(path.read_text(encoding="utf-8"))[0]["schema_version"],3)

    def test_atomic_failure_preserves_original_and_returns_exit_two(self):
        with tempfile.TemporaryDirectory() as tmp:
            target=Path(tmp)/"catalog.json"; target.write_text('{"before": true}\n',encoding="utf-8")
            original=target.read_text(encoding="utf-8"); old=migration.os.replace
            try:
                migration.os.replace=lambda *_: (_ for _ in ()).throw(OSError("simulated replace failure"))
                with self.assertRaises(OSError): migration.atomic_write(target,{"after":True})
            finally: migration.os.replace=old
            self.assertEqual(target.read_text(encoding="utf-8"),original)

    def test_main_returns_exit_two_and_envelope_on_atomic_failure(self):
        temp, root, path = self.make_workspace("# WP1 — Coordination\n")
        with temp:
            old_write, old_guard = migration.atomic_write, migration.load_guard
            try:
                migration.atomic_write=lambda *_: (_ for _ in ()).throw(OSError("simulated"))
                migration.load_guard=lambda: type("Guard",(),{"require_workspace_lock":lambda *_a,**_k: None})
                stream=StringIO();
                with redirect_stdout(stream): code=migration.main(["--catalog",str(path),"--projects-root",str(root / "memory/references/projects"),"--apply","--workspace-root",str(root),"--lease-id","x","--json"])
            finally: migration.atomic_write, migration.load_guard = old_write, old_guard
            self.assertEqual(code,2); self.assertEqual(json.loads(stream.getvalue())["state"],"Failed")

if __name__ == "__main__": unittest.main()
