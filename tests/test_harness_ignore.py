"""Fresh-clone ignore policy, independent of global and info/exclude rules."""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest


BUNDLE = Path(__file__).resolve().parents[1]


class HarnessIgnoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="oi-ignore-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.template = self.root / "empty-template"
        self.template.mkdir()
        self.empty_global = self.root / "empty-global-ignore"
        self.empty_global.write_text("", encoding="utf-8")
        self.env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
        self.git("init", "--quiet", "--template=" + str(self.template))
        (self.repo / ".gitignore").write_bytes((BUNDLE / ".gitignore").read_bytes())
        self.git("add", "--", ".gitignore")
        self.baseline = self.git("status", "--porcelain", "--untracked-files=all")
        for relative in ("runs/synthetic/evidence.json", "opencode.json", ".pytest_cache/v/cache/nodeids",
                         "skills/mail-desk/.pytest_cache/v/cache/lastfailed"):
            target = self.repo / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("synthetic\n", encoding="utf-8")

    def git(self, *args):
        completed = subprocess.run(
            ["git", "-C", str(self.repo), "-c", "core.excludesFile=" + str(self.empty_global), *args],
            env=self.env, capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(0, completed.returncode, completed.stderr)
        return completed.stdout

    def test_rules_are_versioned_in_fresh_repository(self):
        output = self.git("check-ignore", "-v", "--", "runs", "opencode.json", ".pytest_cache",
                          "skills/mail-desk/.pytest_cache")
        rows = [row.split("\t", 1) for row in output.splitlines()]
        self.assertEqual(4, len(rows))
        self.assertTrue(all(source.startswith(".gitignore:") for source, _ in rows), output)
        for rule, path in (("/runs/", "runs"), ("/opencode.json", "opencode.json"),
                           (".pytest_cache/", ".pytest_cache"),
                           (".pytest_cache/", "skills/mail-desk/.pytest_cache")):
            self.assertTrue(any(source.endswith(":" + rule) and name == path for source, name in rows), output)
        self.assertEqual(self.baseline, self.git("status", "--porcelain", "--untracked-files=all"))

    def test_add_all_does_not_stage_local_artifacts(self):
        (self.repo / "keep.txt").write_text("legitimate source\n", encoding="utf-8")
        self.git("add", "-A")
        self.assertEqual([".gitignore", "keep.txt"], self.git("diff", "--cached", "--name-only").splitlines())
        self.assertEqual("", self.git("ls-files", "--others", "--exclude-standard"))


if __name__ == "__main__":
    unittest.main()
