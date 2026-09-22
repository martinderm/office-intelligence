"""Structural contract for the ``core.quarantine`` package (FR-13 / MD-M2-T01).

The approved target structure groups the six cohesive quarantine/attachment
modules under ``core/quarantine/`` while the six former ``core/attachment_*.py``
paths become thin re-export shims.  Every shim must alias the canonical owner
module in ``sys.modules`` so that legacy-path consumers, monkeypatch seams and
``core`` package re-exports keep resolving to the identical module and symbol
objects.

These tests describe that structure only; they add no production behaviour and
touch no production module.
"""

from __future__ import annotations

import importlib
import sys
import unittest
from pathlib import Path

MAIL_DESK_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(MAIL_DESK_ROOT / "scripts"))

import core as core_package  # noqa: E402

_PACKAGE = "core.quarantine"

# Canonical owner module name -> legacy facade module name.
_OWNER_TO_LEGACY = {
    "quarantine_index": "attachment_quarantine_index",
    "attachment_fetch": "attachment_fetch",
    "attachment_extract": "attachment_extract",
    "attachment_filing": "attachment_filing",
    "attachment_policy": "attachment_policy",
    "attachment_handoff": "attachment_handoff",
}

# The 33 symbols re-exported by ``scripts/core/__init__.py`` from the legacy
# ``attachment_quarantine_index`` path, in declaration order.
_QUARANTINE_INDEX_REEXPORTS = (
    "INDEX_FILENAME",
    "SCHEMA_VERSION",
    "LIFECYCLE_STATE_QUARANTINED",
    "ANALYSIS_STATUS_COMPLETED",
    "ANALYSIS_COMPLETENESS_FULL",
    "ANALYSIS_COMPLETENESS_TRUNCATED",
    "ANALYSIS_COMPLETENESS_PARTIAL",
    "ANALYSIS_COMPLETENESS_UNAVAILABLE",
    "ANALYSIS_COMPLETENESS_UNKNOWN",
    "ALLOWED_ANALYSIS_COMPLETENESS",
    "TRUNCATION_STAGE_NONE",
    "TRUNCATION_STAGE_EXTRACTION",
    "TRUNCATION_STAGE_HANDOFF_PER_ATTACHMENT",
    "TRUNCATION_STAGE_HANDOFF_CUMULATIVE_MAIL",
    "ALLOWED_TRUNCATION_STAGES",
    "QuarantineIndexError",
    "WorkspaceLockRequiredError",
    "AttachmentIndexDriftError",
    "AttachmentIndexSchemaError",
    "ForbiddenContentError",
    "PhysicalVerificationError",
    "compute_attachment_id",
    "resolve_quarantine_index_path",
    "load_quarantine_index",
    "save_quarantine_index_atomic",
    "validate_quarantine_index_entry",
    "record_quarantine_entry",
    "reconcile_quarantine_index",
    "lookup_quarantine_entry",
    "get_quarantine_index_stats",
    "canonical_index_entry_sha256",
    "remove_quarantine_entry",
    "update_quarantine_entry_disposition_ref",
)

# (legacy module name, symbol, canonical owner module name).  Representative
# intra-group consumer symbols whose object identity must survive the move.
_SYMBOL_IDENTITY = (
    ("attachment_quarantine_index", "compute_attachment_id", "quarantine_index"),
    ("attachment_quarantine_index", "load_quarantine_index", "quarantine_index"),
    ("attachment_quarantine_index", "validate_quarantine_index_entry", "quarantine_index"),
    ("attachment_quarantine_index", "resolve_quarantine_index_path", "quarantine_index"),
    ("attachment_quarantine_index", "verify_workspace_lock", "attachment_fetch"),
    ("attachment_quarantine_index", "check_quarantine_path_security", "attachment_fetch"),
    ("attachment_quarantine_index", "is_valid_run_id", "attachment_fetch"),
    ("attachment_quarantine_index", "validate_attachment_filename", "attachment_fetch"),
    ("attachment_quarantine_index", "verify_quarantine_attachment_artifact", "attachment_fetch"),
    ("attachment_quarantine_index", "sanitize_attachment_filename", "attachment_policy"),
    ("attachment_quarantine_index", "INVENTORY_FILENAME", "attachment_fetch"),
    ("attachment_fetch", "compute_review_hash", "attachment_fetch"),
    ("attachment_fetch", "op_attachment_fetch", "attachment_fetch"),
    ("attachment_fetch", "verify_workspace_lock", "attachment_fetch"),
    ("attachment_fetch", "check_quarantine_path_security", "attachment_fetch"),
    ("attachment_fetch", "DEFAULT_ATTACHMENT_POLICY", "attachment_policy"),
    ("attachment_fetch", "sanitize_attachment_filename", "attachment_policy"),
    ("attachment_extract", "extract_attachment_content", "attachment_extract"),
    ("attachment_extract", "validate_mda2_fetch_result", "attachment_extract"),
    ("attachment_extract", "run_with_timeout", "attachment_extract"),
    ("attachment_extract", "check_quarantine_path_security", "attachment_fetch"),
    ("attachment_extract", "is_valid_run_id", "attachment_fetch"),
    ("attachment_extract", "verify_workspace_lock", "attachment_fetch"),
    ("attachment_extract", "_atomic_no_clobber_promote", "attachment_fetch"),
    ("attachment_extract", "WorkspaceLockError", "attachment_fetch"),
    ("attachment_extract", "DEFAULT_ATTACHMENT_POLICY", "attachment_policy"),
    ("attachment_filing", "propose_attachment_filing", "attachment_filing"),
    ("attachment_filing", "validate_mda2_attachment", "attachment_filing"),
    ("attachment_filing", "resolve_catalog_storage", "attachment_filing"),
    ("attachment_filing", "is_valid_run_id", "attachment_fetch"),
    ("attachment_filing", "compute_review_hash", "attachment_fetch"),
    ("attachment_filing", "verify_approval_receipt", "attachment_fetch"),
    ("attachment_filing", "verify_quarantine_attachment_artifact", "attachment_fetch"),
    ("attachment_filing", "validate_attachment_handoff", "attachment_handoff"),
    ("attachment_filing", "HandoffDriftError", "attachment_handoff"),
    ("attachment_filing", "sanitize_attachment_filename", "attachment_policy"),
    ("attachment_policy", "sanitize_attachment_filename", "attachment_policy"),
    ("attachment_policy", "check_attachment_policy", "attachment_policy"),
    ("attachment_policy", "DEFAULT_ATTACHMENT_POLICY", "attachment_policy"),
    ("attachment_handoff", "validate_attachment_handoff", "attachment_handoff"),
    ("attachment_handoff", "build_attachment_analysis_handoff", "attachment_handoff"),
    ("attachment_handoff", "compute_handoff_hash", "attachment_handoff"),
    ("attachment_handoff", "apply_attachment_handoff_to_item", "attachment_handoff"),
)


def _owner_module(owner: str):
    """Import a canonical owner module lazily so the missing package is the failure."""
    return importlib.import_module(f"{_PACKAGE}.{owner}")


def _legacy_module(legacy: str):
    return importlib.import_module(f"core.{legacy}")


class QuarantinePackageStructureTests(unittest.TestCase):
    """``core.quarantine`` is a package holding exactly the six owner modules."""

    def test_package_imports_and_declares_six_owner_modules(self) -> None:
        package = importlib.import_module(_PACKAGE)
        self.assertTrue(hasattr(package, "__path__"))
        package_dir = Path(list(package.__path__)[0])
        self.assertEqual(
            set(_OWNER_TO_LEGACY),
            {path.stem for path in package_dir.glob("*.py") if path.name != "__init__.py"},
        )

    def test_each_owner_module_file_exists_and_imports(self) -> None:
        package = importlib.import_module(_PACKAGE)
        package_dir = Path(list(package.__path__)[0])
        for owner in _OWNER_TO_LEGACY:
            with self.subTest(owner=owner):
                self.assertTrue((package_dir / f"{owner}.py").is_file())
                module = _owner_module(owner)
                self.assertEqual(f"{_PACKAGE}.{owner}", module.__name__)
                self.assertIs(module, sys.modules[f"{_PACKAGE}.{owner}"])


class LegacyModuleIdentityTests(unittest.TestCase):
    """Every legacy facade path aliases the identical canonical owner module."""

    def test_legacy_paths_resolve_to_the_same_module_object_as_owners(self) -> None:
        for owner, legacy in _OWNER_TO_LEGACY.items():
            with self.subTest(legacy=legacy, owner=owner):
                legacy_module = _legacy_module(legacy)
                owner_module = _owner_module(owner)
                self.assertIs(legacy_module, owner_module)
                self.assertIs(sys.modules[f"core.{legacy}"], owner_module)
                self.assertEqual(f"{_PACKAGE}.{owner}", owner_module.__name__)


class IntraGroupSymbolIdentityTests(unittest.TestCase):
    """Consumer symbols keep one object identity across old and new paths."""

    def test_symbol_identity_table_is_unique_and_covers_every_module(self) -> None:
        keys = [(legacy, symbol) for legacy, symbol, _ in _SYMBOL_IDENTITY]
        self.assertEqual(len(keys), len(set(keys)))
        self.assertEqual(set(_OWNER_TO_LEGACY.values()), {legacy for legacy, _, _ in _SYMBOL_IDENTITY})

    def test_representative_symbols_are_identical_across_paths(self) -> None:
        for legacy, symbol, owner in _SYMBOL_IDENTITY:
            with self.subTest(legacy=legacy, symbol=symbol, owner=owner):
                legacy_module = _legacy_module(legacy)
                owner_module = _owner_module(owner)
                self.assertTrue(hasattr(legacy_module, symbol))
                self.assertTrue(hasattr(owner_module, symbol))
                self.assertIs(getattr(legacy_module, symbol), getattr(owner_module, symbol))


class CoreReExportIdentityTests(unittest.TestCase):
    """The 33 ``core`` re-exports stay identity-bound through the owner module."""

    def test_reexport_table_covers_all_33_symbols(self) -> None:
        self.assertEqual(33, len(_QUARANTINE_INDEX_REEXPORTS))
        self.assertEqual(len(_QUARANTINE_INDEX_REEXPORTS), len(set(_QUARANTINE_INDEX_REEXPORTS)))

    def test_core_reexports_resolve_to_the_owner_module_objects(self) -> None:
        owner_module = _owner_module("quarantine_index")
        legacy_module = _legacy_module("attachment_quarantine_index")
        for name in _QUARANTINE_INDEX_REEXPORTS:
            with self.subTest(symbol=name):
                self.assertTrue(hasattr(core_package, name))
                self.assertTrue(hasattr(owner_module, name))
                self.assertIs(getattr(core_package, name), getattr(owner_module, name))
                self.assertIs(getattr(legacy_module, name), getattr(owner_module, name))


class CloudAtlasDiscoveryTests(unittest.TestCase):
    """Cloud-Atlas script discovery still resolves from the deeper owner parents."""

    def test_gen_filemap_is_discoverable_from_owner_module_parents(self) -> None:
        owner_module = _owner_module("attachment_filing")
        parents = list(Path(owner_module.__file__).resolve().parents)
        relative_candidates = (
            Path("skills") / "cloud-atlas" / "scripts" / "gen_filemap.py",
            Path("cloud-atlas") / "scripts" / "gen_filemap.py",
        )
        discovered = [
            base / relative
            for base in parents
            for relative in relative_candidates
            if (base / relative).is_file()
        ]
        self.assertTrue(
            discovered,
            "skills/cloud-atlas/scripts/gen_filemap.py must remain discoverable "
            "from the attachment_filing owner parents",
        )


class PackageImportSmokeTests(unittest.TestCase):
    """Importing the package and every legacy facade path stays cycle-free."""

    def test_smoke_imports_bind_legacy_aliases_to_owner_modules(self) -> None:
        for owner, legacy in _OWNER_TO_LEGACY.items():
            with self.subTest(legacy=legacy):
                legacy_module = _legacy_module(legacy)
                owner_module = _owner_module(owner)
                self.assertIs(legacy_module, owner_module)
        self.assertIs(sys.modules["core"], core_package)


if __name__ == "__main__":
    unittest.main()
