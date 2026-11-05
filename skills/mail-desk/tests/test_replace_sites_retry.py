"""Issue #78: every remaining replace-an-existing-file site rides on the bounded retry.

Issue #70 put the shared atomic writer and the recovery/promotion/discard journals
on ``core.common.replace_with_retry``. These tests pin the remaining sites that
replace an existing file (evidence, progress, indices, quarantine inventories,
handoff, preflight state) to the same policy: a transient Windows sharing error is
retried, an exhausted budget is a classified ``AtomicReplaceError`` (wrapped only
where the site already had its own error contract), and any other error is never
retried. The staging write and ``fsync`` stay outside the retry.
"""

from __future__ import annotations

import ast
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any, Callable
from unittest.mock import patch


MAIL_DESK_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_ROOT = MAIL_DESK_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS_ROOT))

import mailbox_preflight  # noqa: E402
from core import attachment_disposition_log, attachment_promotion, common, evidence, index  # noqa: E402
from core.progress import BatchProgressTracker  # noqa: E402
from core.quarantine import attachment_fetch, quarantine_index  # noqa: E402

from test_common_atomic import ReplaceScript, sharing_error  # noqa: E402


SHA_A = "a" * 64
SHA_B = "b" * 64
MESSAGE_ID = "m1@example.test"


class Site:
    """One replace site: how to trigger it and how to observe its outcome."""

    def __init__(
        self,
        name: str,
        target: Path,
        call: Callable[[], Any],
        updated: Callable[[], bool],
        *,
        swallows: bool = False,
        wrapper: type[BaseException] | None = None,
        cleans_staging: bool = True,
        fsyncs: bool = False,
    ) -> None:
        self.name = name
        self.target = target
        self.call = call
        self.updated = updated
        self.swallows = swallows  # best-effort writer: never raises to its caller
        self.wrapper = wrapper  # site's own error class that chains the OSError
        self.cleans_staging = cleans_staging  # pre-existing: some sites leave their tmp file
        self.fsyncs = fsyncs  # sites with a durable ``fsync`` staging step
        self.before = target.read_bytes()


def _inventory_doc() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "messages": {
            MESSAGE_ID: {
                "files": {
                    "a.pdf": {"sha256": SHA_A, "size_bytes": 3},
                    "b.pdf": {"sha256": SHA_B, "size_bytes": 3},
                },
                "count": 2,
                "total_bytes": 6,
            }
        },
    }


def _write_inventory(run_dir: Path) -> Path:
    run_dir.mkdir(parents=True, exist_ok=True)
    inv_file = run_dir / attachment_fetch.INVENTORY_FILENAME
    inv_file.write_text(json.dumps(_inventory_doc(), indent=2, sort_keys=True), encoding="utf-8")
    return inv_file


def build_sites(root: Path) -> list[Site]:
    sites: list[Site] = []

    def sub(name: str) -> Path:
        path = root / name
        path.mkdir()
        return path

    # core/evidence.py
    ev = sub("evidence") / "evidence.md"
    ev.write_text("before\n", encoding="utf-8")
    sites.append(Site(
        "evidence", ev,
        lambda: evidence._atomic_write_text(ev, "after\n"),
        lambda: ev.read_text(encoding="utf-8") == "after\n",
        fsyncs=True,
    ))

    # core/progress.py (best-effort: failures are swallowed by contract)
    progress_dir = sub("progress")
    tracker = BatchProgressTracker("execute", 3, progress_dir, run_id="run-78", console_log=False)
    progress_file = progress_dir / "runner-progress.json"
    sites.append(Site(
        "progress", progress_file,
        lambda: tracker.step("after"),
        lambda: json.loads(progress_file.read_text(encoding="utf-8"))["progress"]["current_step"] == "after",
        swallows=True,
    ))

    # core/index.py
    idx = sub("index") / "final-location-index.json"
    idx.write_text('{"schema_version": 1, "updated_at": "before", "items": {}}\n', encoding="utf-8")
    sites.append(Site(
        "final_index", idx,
        lambda: index.save_final_index_atomic(
            idx, {"schema_version": 1, "updated_at": "after", "items": {}}
        ),
        lambda: json.loads(idx.read_text(encoding="utf-8"))["updated_at"] == "after",
        cleans_staging=False,
    ))

    # core/quarantine/quarantine_index.py
    qidx = sub("quarantine_index") / "quarantine-index.json"
    qidx.write_text('{"before": true}\n', encoding="utf-8")
    sites.append(Site(
        "quarantine_index", qidx,
        lambda: quarantine_index.save_quarantine_index_atomic(
            qidx,
            {"schema_version": quarantine_index.SCHEMA_VERSION, "updated_at": "after", "items": {}},
        ),
        lambda: json.loads(qidx.read_text(encoding="utf-8")).get("updated_at") == "after",
        cleans_staging=False,
    ))

    # core/attachment_promotion.py: inventory write
    promo_inv = sub("promotion_inventory") / attachment_promotion.INVENTORY_FILENAME
    promo_inv.write_text('{"before": true}\n', encoding="utf-8")
    sites.append(Site(
        "promotion_inventory", promo_inv,
        lambda: attachment_promotion._write_inventory_atomic(promo_inv, b'{"after": true}\n'),
        lambda: promo_inv.read_bytes() == b'{"after": true}\n',
        fsyncs=True,
    ))

    # core/attachment_promotion.py: handoff write
    handoff_path = sub("promotion_handoff") / "handoff.json"
    handoff_path.write_text('{"before": true}\n', encoding="utf-8")
    body = {"kind": "issue-78"}
    handoff = {**body, "handoff_hash": attachment_promotion.canonical_json_sha256(body)}
    sites.append(Site(
        "promotion_handoff", handoff_path,
        lambda: attachment_promotion.write_cloud_atlas_refresh_handoff(handoff, handoff_path),
        lambda: json.loads(handoff_path.read_text(encoding="utf-8")).get("kind") == "issue-78",
        fsyncs=True,
    ))

    # core/attachment_disposition_log.py: inventory write (own InventoryUpdateError contract)
    disp_dir = sub("disposition_inventory")
    disp_inv = _write_inventory(disp_dir)
    sites.append(Site(
        "disposition_inventory", disp_inv,
        lambda: attachment_disposition_log.update_quarantine_inventory_atomic(
            disp_dir, f"<{MESSAGE_ID}>", "a.pdf"
        ),
        lambda: "a.pdf" not in disp_inv.read_text(encoding="utf-8"),
        wrapper=attachment_disposition_log.InventoryUpdateError,
        fsyncs=True,
    ))

    # core/quarantine/attachment_fetch.py: inventory write
    fetch_dir = sub("fetch_inventory")
    fetch_inv = _write_inventory(fetch_dir)
    sites.append(Site(
        "fetch_inventory", fetch_inv,
        lambda: attachment_fetch._record_in_quarantine_inventory_unlocked(
            fetch_dir, MESSAGE_ID, "c.pdf", SHA_A, 5
        ),
        lambda: "c.pdf" in fetch_inv.read_text(encoding="utf-8"),
        cleans_staging=False,
    ))

    # scripts/mailbox_preflight.py
    pre = sub("preflight") / "mailbox-preflight-state.json"
    pre.write_text('{"before": true}\n', encoding="utf-8")
    sites.append(Site(
        "mailbox_preflight", pre,
        lambda: mailbox_preflight._write_state_atomic(pre, {"after": True}),
        lambda: json.loads(pre.read_text(encoding="utf-8")).get("after") is True,
        cleans_staging=False,
    ))
    return sites


class ReplaceSiteRetryTests(unittest.TestCase):
    """Each former bare-replace site: transient success, classified end, no retry otherwise."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.sites = build_sites(Path(self._tmp.name))

    def _run(self, site: Site, script: ReplaceScript):
        """Call the site under a scripted ``os.replace``; return (raised, sleep mock)."""
        with patch.object(common.os, "replace", side_effect=script), \
                patch.object(common.time, "sleep") as sleep:
            try:
                site.call()
            except BaseException as error:  # noqa: BLE001 - classified by the caller
                return error, sleep
        return None, sleep

    def _underlying(self, site: Site, raised: BaseException | None) -> BaseException | None:
        if site.wrapper is not None:
            self.assertIsInstance(raised, site.wrapper)
            return raised.__cause__
        return raised

    def _assert_no_staging_files(self, site: Site):
        if site.cleans_staging:
            self.assertEqual([site.target.name], sorted(p.name for p in site.target.parent.iterdir()
                                                        if not p.name.startswith(".quarantine-inventory.lock")),
                             site.name)

    def test_transient_sharing_errors_are_retried_and_the_target_is_replaced(self):
        for site in self.sites:
            with self.subTest(site=site.name):
                script = ReplaceScript([sharing_error(5), sharing_error(32)])
                raised, sleep = self._run(site, script)

                self.assertIsNone(raised)
                self.assertEqual(3, script.calls)
                self.assertEqual(
                    list(common.REPLACE_RETRY_DELAYS_SECONDS[:2]),
                    [call.args[0] for call in sleep.call_args_list],
                )
                self.assertTrue(site.updated())
                self._assert_no_staging_files(site)

    def test_exhausted_retry_is_a_classified_error_and_keeps_the_previous_file(self):
        attempts = len(common.REPLACE_RETRY_DELAYS_SECONDS) + 1
        for site in self.sites:
            with self.subTest(site=site.name):
                script = ReplaceScript([sharing_error(5)] * 100)
                raised, sleep = self._run(site, script)

                self.assertEqual(attempts, script.calls, "attempts are bounded")
                self.assertEqual(attempts - 1, sleep.call_count)
                if site.swallows:
                    self.assertIsNone(raised, "best-effort writer keeps swallowing")
                else:
                    cause = self._underlying(site, raised)
                    self.assertIsInstance(cause, common.AtomicReplaceError)
                    self.assertEqual("atomic_replace_failed", cause.reason_code)
                    self.assertEqual(attempts, cause.attempts)
                    self.assertEqual(5, cause.cause_winerror)
                    self.assertIsInstance(cause, OSError)
                    self.assertNotIn(str(site.target.parent), str(cause), "no absolute path echo")
                self.assertEqual(site.before, site.target.read_bytes())
                self._assert_no_staging_files(site)

    def test_non_transient_errors_are_not_retried(self):
        permanent_errors = [
            OSError("replace failed"),
            PermissionError(13, "Permission denied"),  # no winerror: not a sharing violation
            sharing_error(1314),  # a Windows error code outside the sharing/denied set
        ]
        for site in self.sites:
            for error in permanent_errors:
                with self.subTest(site=site.name, error=repr(error)):
                    script = ReplaceScript([error])
                    raised, sleep = self._run(site, script)

                    self.assertEqual(1, script.calls)
                    sleep.assert_not_called()
                    if site.swallows:
                        self.assertIsNone(raised)
                    else:
                        self.assertIs(error, self._underlying(site, raised))
                    self.assertEqual(site.before, site.target.read_bytes())
                    self.assertNotIsInstance(raised, common.AtomicReplaceError)
                    self._assert_no_staging_files(site)

    def test_staging_write_failure_is_not_retried(self):
        for site in self.sites:
            if not site.fsyncs:
                continue
            with self.subTest(site=site.name):
                with patch.object(common.os, "fsync", side_effect=sharing_error(5)) as fsync, \
                        patch.object(common.time, "sleep") as sleep:
                    with self.assertRaises(site.wrapper or OSError) as ctx:
                        site.call()
                self.assertNotIsInstance(ctx.exception, common.AtomicReplaceError)
                self.assertNotIsInstance(ctx.exception.__cause__, common.AtomicReplaceError)
                sleep.assert_not_called()
                self.assertEqual(1, fsync.call_count)
                self.assertEqual(site.before, site.target.read_bytes())


@unittest.skipUnless(os.name == "nt", "open handles block os.replace only on Windows")
class ReplaceSiteOpenHandleTests(unittest.TestCase):
    """Real WinError 5 from a lingering reader, the exact failure mode of issue #78."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.sites = build_sites(Path(self._tmp.name))

    def test_site_recovers_when_the_foreign_handle_is_released(self):
        for site in self.sites:
            with self.subTest(site=site.name):
                handle = open(site.target, "rb")
                self.addCleanup(handle.close)

                with patch.object(common.time, "sleep", side_effect=lambda _s: handle.close()) as sleep:
                    site.call()

                self.assertGreaterEqual(sleep.call_count, 1, "released during a backoff")
                self.assertTrue(site.updated())

    def test_site_with_a_permanently_held_handle_ends_classified(self):
        for site in self.sites:
            with self.subTest(site=site.name):
                handle = open(site.target, "rb")
                try:
                    with patch.object(common.time, "sleep") as sleep:
                        try:
                            site.call()
                            raised: BaseException | None = None
                        except BaseException as error:  # noqa: BLE001
                            raised = error
                finally:
                    handle.close()

                self.assertEqual(len(common.REPLACE_RETRY_DELAYS_SECONDS), sleep.call_count)
                if site.swallows:
                    self.assertIsNone(raised)
                else:
                    cause = raised.__cause__ if site.wrapper else raised
                    self.assertIsInstance(cause, common.AtomicReplaceError)
                self.assertEqual(site.before, site.target.read_bytes())


class NoBareReplaceGuardTests(unittest.TestCase):
    """Issue #78: a new bare replace in mail-desk code must be a conscious decision."""

    ALLOWED = {Path("core/common.py")}  # the helper itself

    def test_scripts_only_replace_files_through_the_bounded_helper(self):
        offenders: list[str] = []
        for path in sorted(SCRIPTS_ROOT.rglob("*.py")):
            relative = path.relative_to(SCRIPTS_ROOT)
            if relative in self.ALLOWED:
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                    continue
                func = node.func
                bare_os = (
                    func.attr in {"replace", "rename"}
                    and isinstance(func.value, ast.Name)
                    and func.value.id == "os"
                )
                # ``str.replace`` takes two positional arguments, ``datetime.replace``
                # keywords only; ``Path.replace`` takes exactly one positional target.
                bare_path = func.attr == "replace" and len(node.args) == 1 and not node.keywords
                if bare_os or bare_path:
                    offenders.append(f"{relative.as_posix()}:{node.lineno}")
        self.assertEqual([], offenders)


if __name__ == "__main__":
    unittest.main()
