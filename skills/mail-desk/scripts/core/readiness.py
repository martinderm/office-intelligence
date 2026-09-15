"""Workspace-bound backend selection and minimal read-only readiness checks."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any, Callable, Mapping

from .envelope import build_error, build_success
from .himalaya import HimalayaInvocationError, run_himalaya


BACKEND_CONFIG_RELATIVE_PATH = Path(".agents") / "mail-desk-backend.json"
BACKEND_CONFIG_KEYS = {"schema_version", "backend", "account"}
SUPPORTED_BACKEND = "himalaya"
READINESS_ACTION = "mailbox_readiness"
READINESS_TIMEOUT_SECONDS = 10


def workspace_root_from_data_dir(data_dir: Path) -> Path:
    """Resolve the workspace root for the documented ``data/mail-desk`` layout."""
    resolved = data_dir.expanduser().resolve()
    if resolved.name != "mail-desk" or resolved.parent.name != "data":
        raise ValueError("data_dir must be the workspace-local data/mail-desk directory")
    return resolved.parent.parent


def load_workspace_backend_config(data_dir: Path) -> dict[str, Any]:
    """Load the sole credentials-free backend/account binding for a workspace."""
    workspace_root = workspace_root_from_data_dir(data_dir)
    config_path = workspace_root / BACKEND_CONFIG_RELATIVE_PATH
    if not config_path.is_file():
        raise ValueError(
            f"Workspace backend configuration is missing: {BACKEND_CONFIG_RELATIVE_PATH.as_posix()}"
        )
    try:
        config = json.loads(config_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError("Workspace backend configuration is not valid JSON.") from exc
    if not isinstance(config, dict) or set(config) != BACKEND_CONFIG_KEYS:
        raise ValueError(
            "Workspace backend configuration must contain exactly schema_version, backend, and account."
        )
    if config["schema_version"] != 1:
        raise ValueError("Workspace backend configuration schema_version must be 1.")
    if config["backend"] != SUPPORTED_BACKEND:
        raise ValueError(f"Workspace backend must be the supported adapter '{SUPPORTED_BACKEND}'.")
    account = config["account"]
    if account is not None and (not isinstance(account, str) or not account.strip()):
        raise ValueError("Workspace backend account must be null or a non-empty string.")
    return {
        "schema_version": 1,
        "backend": SUPPORTED_BACKEND,
        "account": account,
        "config_path": str(config_path),
    }


def bind_workspace_account(
    data_dir: Path,
    *,
    requested_account: str | None,
) -> dict[str, Any]:
    """Resolve the configured account and reject a differing caller request.

    ``None`` means no caller selection; the configured value (including an
    explicit ``null`` default account) is always returned as the effective one.
    """
    try:
        binding = load_workspace_backend_config(data_dir)
    except Exception as exc:  # Configuration failures are safe, reviewable stops.
        return {
            "ok": False,
            "reason_code": "workspace_config_invalid",
            "message": str(exc),
        }
    if requested_account is not None and requested_account != binding["account"]:
        return {
            "ok": False,
            "reason_code": "account_mismatch",
            "message": "Requested account does not match the workspace backend configuration.",
            "configured_account": binding["account"],
            "requested_account": requested_account,
        }
    return {"ok": True, **binding}


def binding_failure_envelope(binding: Mapping[str, Any]) -> dict[str, Any]:
    """Render a workspace binding failure as the same canonical readiness shape."""
    return build_error(
        READINESS_ACTION,
        str(binding.get("message") or "Workspace backend binding failed."),
        {
            key: binding[key]
            for key in ("configured_account", "requested_account")
            if key in binding
        },
        error_type="WorkspaceConfiguration",
        error_details={"reason_code": str(binding.get("reason_code") or "workspace_config_invalid")},
    )


def _parse_minimal_envelope_list(output: object) -> list[dict[str, Any]]:
    if not isinstance(output, str):
        raise ValueError("Adapter returned a non-text minimal response.")
    trimmed = output.strip()
    if "[" in trimmed:
        trimmed = trimmed[trimmed.find("[") :]
    parsed = json.loads(trimmed)
    if not isinstance(parsed, list) or not all(isinstance(item, dict) for item in parsed):
        raise ValueError("Adapter minimal response must be a JSON envelope list.")
    return parsed


def mailbox_readiness_preflight(
    binding: Mapping[str, Any],
    *,
    folder: str,
    run_himalaya_fn: Callable[..., str] = run_himalaya,
) -> dict[str, Any]:
    """Read at most one envelope to validate the selected adapter and account.

    The result itself is a canonical envelope, so callers can surface exactly why
    an execute or autonomous pipeline stopped before any mutation.
    """
    if binding.get("backend") != SUPPORTED_BACKEND:
        return build_error(
            READINESS_ACTION,
            "Workspace backend binding does not select a supported adapter.",
            {"folder": folder},
            error_type="UnsupportedBackend",
            error_details={"reason_code": "unsupported_backend"},
        )
    if not isinstance(folder, str) or not folder.strip():
        return build_error(
            READINESS_ACTION,
            "Readiness preflight requires a non-empty source folder.",
            {},
            error_type="InvalidFolder",
            error_details={"reason_code": "invalid_folder"},
        )
    try:
        output = run_himalaya_fn(
            ["-o", "json", "envelope", "list", "-f", folder, "-s", "1"],
            account=binding.get("account"),
            timeout=READINESS_TIMEOUT_SECONDS,
            max_retries=1,
        )
        envelopes = _parse_minimal_envelope_list(output)
    except subprocess.TimeoutExpired:
        return build_error(
            READINESS_ACTION,
            "Mailbox readiness timed out before any mutation.",
            {"backend": SUPPORTED_BACKEND, "account": binding.get("account"), "folder": folder},
            error_type="Timeout",
            error_details={"reason_code": "himalaya_timeout", "timeout_seconds": READINESS_TIMEOUT_SECONDS},
        )
    except FileNotFoundError:
        return build_error(
            READINESS_ACTION,
            "Configured mail adapter is unavailable before any mutation.",
            {"backend": SUPPORTED_BACKEND, "account": binding.get("account"), "folder": folder},
            error_type="AdapterUnavailable",
            error_details={"reason_code": "adapter_unavailable"},
        )
    except HimalayaInvocationError as exc:
        reason_code = exc.reason_code
        error_type = "AdapterUnavailable" if reason_code == "himalaya_unavailable" else "HimalayaBootstrap"
        return build_error(
            READINESS_ACTION,
            "Mailbox readiness stopped before any mutation.",
            {"backend": SUPPORTED_BACKEND, "account": binding.get("account"), "folder": folder},
            error_type=error_type,
            error_details={"reason_code": reason_code},
        )
    except ValueError as exc:
        return build_error(
            READINESS_ACTION,
            "Mailbox readiness returned an invalid minimal response.",
            {"backend": SUPPORTED_BACKEND, "account": binding.get("account"), "folder": folder},
            error_type="InvalidResponse",
            error_details={"reason_code": "invalid_response", "exception_type": type(exc).__name__},
        )
    except Exception as exc:  # Adapter errors must not fall through to mutation.
        return build_error(
            READINESS_ACTION,
            "Mailbox readiness failed before any mutation.",
            {"backend": SUPPORTED_BACKEND, "account": binding.get("account"), "folder": folder},
            error_type="ConnectivityFailure",
            error_details={"reason_code": "connectivity_failed", "exception_type": type(exc).__name__},
        )
    return build_success(
        READINESS_ACTION,
        "Mailbox readiness completed with a bounded read-only check.",
        {
            "backend": SUPPORTED_BACKEND,
            "account": binding.get("account"),
            "folder": folder,
            "envelope_count": len(envelopes),
            "timeout_seconds": READINESS_TIMEOUT_SECONDS,
            "config_path": binding.get("config_path"),
        },
    )
