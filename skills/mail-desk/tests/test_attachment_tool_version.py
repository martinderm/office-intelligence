"""Contract pins for the metadata-only ``ocrmypdf`` tool-version lookup (Issue #10).

``_get_tool_version("ocrmypdf")`` reports the installed distribution version via
``importlib.metadata``.  It must not import the ``ocrmypdf`` module (and therefore
must not pay for the OCR tool import, for example in timeout/error reporting), and
missing or unreadable distribution metadata must stay honestly ``unknown`` without
an import fallback.  The value is a package-version report, not an attestation of
an independent worker runtime.

All mocks cover only the real external distribution/metadata boundary.  The
no-import proofs run in a fresh subprocess with a narrow ``ocrmypdf`` import guard
that is removed in ``finally``; no module-lifetime monkeypatch is installed.
"""

from __future__ import annotations

import importlib.metadata
import json
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

MAIL_DESK_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = MAIL_DESK_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

from core import attachment_extract as aextract  # noqa: E402


def _installed_distribution_version(name: str) -> str:
    """Read ``Version`` directly from the installed distribution ``METADATA`` file.

    The expected value deliberately does not come from ``importlib.metadata.version``;
    it is parsed from the installed distribution itself, independently of the helper
    the product uses.
    """
    distribution = importlib.metadata.distribution(name)
    metadata_text = distribution.read_text("METADATA")
    if metadata_text is None:
        raise unittest.SkipTest(f"{name} distribution has no readable METADATA")
    for line in metadata_text.splitlines():
        if line.startswith("Version:"):
            return line.split(":", 1)[1].strip()
    raise AssertionError(f"{name} distribution METADATA has no Version field")


_ISOLATED_LOOKUP_TEMPLATE = """
import importlib.metadata
import json
import sys

sys.path.insert(0, sys.argv[1])

attempts = []


class _BlockOcrmypdfImport:
    def find_spec(self, fullname, path=None, target=None):
        if fullname == "ocrmypdf" or fullname.startswith("ocrmypdf."):
            attempts.append(fullname)
            raise ImportError("ocrmypdf import is forbidden for metadata lookup")
        return None


guard = _BlockOcrmypdfImport()
sys.meta_path.insert(0, guard)
try:
    from core import attachment_extract as aextract

    original_version = importlib.metadata.version

    def _metadata_version(name):
        if name == "ocrmypdf":
            __FAILURE__
        return original_version(name)

    importlib.metadata.version = _metadata_version
    try:
        result = aextract._get_tool_version("ocrmypdf")
    finally:
        importlib.metadata.version = original_version
finally:
    sys.meta_path[:] = [finder for finder in sys.meta_path if finder is not guard]

print(json.dumps({
    "result": result,
    "attempts": attempts,
    "imported": "ocrmypdf" in sys.modules,
}))
"""


class AttachmentToolVersionTests(unittest.TestCase):
    def _run_isolated_lookup(self, failure: str) -> dict[str, object]:
        script = _ISOLATED_LOOKUP_TEMPLATE.replace("__FAILURE__", failure)
        completed = subprocess.run(
            [sys.executable, "-B", "-c", script, str(SCRIPTS_DIR)],
            cwd=str(MAIL_DESK_ROOT),
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        self.assertEqual(0, completed.returncode, completed.stderr)
        return json.loads(completed.stdout)

    def test_ocrmypdf_returns_installed_distribution_metadata_version(self) -> None:
        expected = _installed_distribution_version("ocrmypdf")
        self.assertEqual(expected, aextract._get_tool_version("ocrmypdf"))

    def test_ocrmypdf_version_lookup_does_not_import_tool_module(self) -> None:
        data = self._run_isolated_lookup("return original_version(name)")
        self.assertEqual([], data["attempts"])
        self.assertFalse(data["imported"])
        self.assertEqual(_installed_distribution_version("ocrmypdf"), data["result"])

    def test_missing_ocrmypdf_metadata_returns_unknown_without_tool_import(self) -> None:
        data = self._run_isolated_lookup(
            "raise importlib.metadata.PackageNotFoundError('ocrmypdf')"
        )
        self.assertEqual("unknown", data["result"])
        self.assertEqual([], data["attempts"])
        self.assertFalse(data["imported"])

    def test_unexpected_ocrmypdf_metadata_error_returns_unknown_without_tool_import(
        self,
    ) -> None:
        data = self._run_isolated_lookup(
            "raise RuntimeError('metadata backend exploded')"
        )
        self.assertEqual("unknown", data["result"])
        self.assertEqual([], data["attempts"])
        self.assertFalse(data["imported"])

    def test_builtin_branch_unchanged_and_does_not_touch_metadata(self) -> None:
        expected = (
            f"python-{sys.version_info.major}.{sys.version_info.minor}."
            f"{sys.version_info.micro}"
        )
        with patch(
            "importlib.metadata.version",
            side_effect=AssertionError("metadata lookup must not run for built_in"),
        ) as version_mock:
            self.assertEqual(expected, aextract._get_tool_version("built_in"))
        version_mock.assert_not_called()


if __name__ == "__main__":
    unittest.main()
