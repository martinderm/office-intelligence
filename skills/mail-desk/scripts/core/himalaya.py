"""Himalaya CLI adapter and IMAP interaction utilities."""

from __future__ import annotations

import concurrent.futures
from contextlib import contextmanager
from contextvars import ContextVar
import base64
import datetime
import email
import email.header
import email.policy
import email.utils
import json
import math
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import tempfile
import threading
import time
from typing import Any, Callable

from .attachments import (
    find_mime_part_by_locator,
    inspect_mime_tree,
    validate_attachment_candidate_metadata,
)
from .common import normalize_message_id
from . import himalaya_normalize
from .himalaya_profile import (
    PROBE_TIMEOUT_SECONDS,
    PROFILE_V3,
    SEARCH_QUERY_UNSUPPORTED_REASON,
    UNAVAILABLE_REASON,
    V1_PROFILE,
    VERSION_UNSUPPORTED_REASON,
    HimalayaProfile,
    ProfileError,
    parse_version_output,
    translate_v3_arguments,
)


class HimalayaInvocationError(RuntimeError):
    """A fail-closed, classified Himalaya bootstrap or command failure.

    ``details`` is an optional, JSON-safe structured channel for bounded call
    metadata (command class, envelope id, duration, exit status).  It never
    carries mail content, headers or raw stderr.
    """

    def __init__(
        self,
        reason_code: str,
        message: str,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.reason_code = reason_code
        self.details = dict(details) if details else {}


_WINDOWS_DRIVE_PATH_REGEX = re.compile(r"^([A-Za-z]):[\\/](.*)$")


def normalize_himalaya_config_path(config_path: str | Path) -> Path:
    """Normalize local Windows drive paths to deterministic local UNC admin share paths.

    Himalaya 1.2.0 interprets Windows drive colons in '-c' arguments as path list
    separators. Under Windows, absolute local drive paths (e.g. 'C:\\...' or 'd:/...')
    are converted to local UNC admin share paths (e.g. '\\\\localhost\\C$\\...' or
    '\\\\localhost\\d$\\...'). Existing UNC paths, POSIX paths, and relative paths
    are preserved semantically.
    """
    raw_str = str(config_path).strip()
    if raw_str.startswith(("\\\\", "//")):
        normalized = raw_str.replace("/", "\\")
        return Path(normalized)
    m = _WINDOWS_DRIVE_PATH_REGEX.match(raw_str)
    if m:
        drive_letter = m.group(1)
        rest = m.group(2).replace("/", "\\")
        return Path(f"\\\\localhost\\{drive_letter}$\\{rest}")
    return Path(config_path)


_LOCAL_ADMIN_SHARE_UNC_REGEX = re.compile(
    r"^\\\\localhost\\([A-Za-z])\$(?:[\\/](.*))?$", re.IGNORECASE
)


def _native_himalaya_config_path(config_path: str | Path) -> Path:
    """Return the native spelling of one resolved config path for Himalaya v3.

    The v1 profile passes the deterministic local admin-share spelling
    (``\\\\localhost\\<drive>$\\<rest>``) that Himalaya 1.2.0 needs.  The v3
    profile receives the same file under its native spelling instead: a native
    drive path stays native, and only a local admin-share path is mapped back to
    ``<drive>:\\<rest>`` (case-insensitive host/drive, slash-tolerant).  A real
    remote UNC path has no native equivalent and is preserved unchanged.
    """
    raw_str = str(config_path).strip()
    match = _LOCAL_ADMIN_SHARE_UNC_REGEX.match(raw_str)
    if match is None:
        return Path(config_path)
    rest = (match.group(2) or "").replace("/", "\\")
    return Path(f"{match.group(1)}:\\{rest}")


def _default_himalaya_config_path() -> Path:
    """Return the platform's non-interactive Himalaya config location."""
    appdata = os.environ.get("APPDATA")
    if appdata:
        return Path(appdata) / "himalaya" / "config.toml"
    return Path.home() / ".config" / "himalaya" / "config.toml"


def _himalaya_config_candidate() -> Path:
    """Return the raw, expanduser-expanded config path before normalization.

    ``HIMALAYA_CONFIG`` is the only supported override.  It is intentionally a
    config *path*, not an arbitrary command hook: accepting a command string
    would turn workspace configuration into a shell-execution boundary.
    """
    configured = os.environ.get("HIMALAYA_CONFIG")
    if configured:
        return Path(configured).expanduser()
    return _default_himalaya_config_path()


def _validate_himalaya_config_path(config_path: Path) -> None:
    """Fail closed unless one config path is absolute and an existing file.

    Only existence is checked; the file is never opened, so no configuration
    content is read, logged or returned.
    """
    if not config_path.is_absolute():
        raise HimalayaInvocationError(
            "himalaya_config_invalid", "HIMALAYA_CONFIG must be an absolute configuration file path."
        )
    if not config_path.is_file():
        raise HimalayaInvocationError(
            "himalaya_config_missing", "Himalaya configuration is missing; refusing interactive setup."
        )


def _resolve_himalaya_executable() -> str:
    """Return the installed CLI path without invoking it."""
    executable = shutil.which("himalaya")
    if not executable:
        raise HimalayaInvocationError(
            "himalaya_unavailable", "Himalaya executable is unavailable; no mailbox command was started."
        )
    return executable


def resolve_himalaya_invocation() -> tuple[str, Path]:
    """Resolve an installed CLI and a readable config without invoking either.

    The config check runs on the normalized file (the deterministic v1
    admin-share spelling), exactly as before; the v3 command builder derives its
    native token from that resolved path instead of re-reading
    ``HIMALAYA_CONFIG``.
    """
    raw_config_path = _himalaya_config_candidate()
    config_path = normalize_himalaya_config_path(raw_config_path)
    _validate_himalaya_config_path(config_path)
    return _resolve_himalaya_executable(), config_path


class HimalayaOutput(str):
    """A CLI text result that carries the profile which produced it.

    Subclassing ``str`` keeps every existing caller, comparison and JSON parse
    unchanged; call sites that must interpret v3 JSON can read ``profile``
    without a second version resolution (and a test double that answers with a
    plain ``str`` keeps the pinned v1 interpretation).
    """

    __slots__ = ("profile",)

    def __new__(cls, text: str, profile: str | None = None) -> "HimalayaOutput":
        instance = super().__new__(cls, text)
        instance.profile = profile
        return instance


def profile_of(output: object) -> str | None:
    """Return the producing profile name of one CLI output, if it carries one."""
    return getattr(output, "profile", None)


#: Bounded profile cache keyed by ``(executable, mtime_ns, size)``.
_PROFILE_CACHE: dict[tuple[str, int, int], HimalayaProfile] = {}
_PROFILE_CACHE_LOCK = threading.Lock()
_MAX_PROFILE_CACHE_ENTRIES = 64

#: The profile selected by the most recent ``build_himalaya_command`` call in
#: this thread.  It is reset by ``run_himalaya`` before every build, so a patched
#: builder can never leak a stale profile into a later command.
_PROFILE_LOCAL = threading.local()


def _profile_cache_key(executable: str) -> tuple[str, int, int] | None:
    """Return the cache key of one executable, or ``None`` for a virtual path.

    Only ``FileNotFoundError``/``NotADirectoryError`` prove the documented
    injected/virtual invocation; any other ``stat`` failure is unprovable and
    fails closed as ``himalaya_unavailable``.
    """
    try:
        info = os.stat(executable)
    except (FileNotFoundError, NotADirectoryError):
        return None
    except OSError as exc:
        raise HimalayaInvocationError(
            UNAVAILABLE_REASON,
            "The Himalaya executable could not be inspected; no mailbox command was sent.",
        ) from exc
    return (str(executable), int(info.st_mtime_ns), int(info.st_size))


def _profile_for_executable(executable: str) -> HimalayaProfile:
    """Resolve the profile of one executable through at most one bound probe.

    The probe is a direct ``subprocess.run([exe, "--version"])`` with no
    ``-c``/account/mailbox and a short timeout; it never passes through
    ``run_himalaya`` and is therefore excluded from ``TransportStats``.  The
    result is cached per ``(executable, mtime, size)`` so one process probes one
    installation exactly once, even when threads race on the first use.

    An executable that does not exist is the pinned legacy invocation (an
    injected or virtual command path, as used by the bootstrap seams): it is
    never probed and keeps the byte-identical v1 argv.  Every real installation
    resolved through ``shutil.which`` is probed fail-closed.
    """
    key = _profile_cache_key(executable)
    if key is None:
        return V1_PROFILE
    with _PROFILE_CACHE_LOCK:
        cached = _PROFILE_CACHE.get(key)
        if cached is not None:
            return cached
        try:
            completed = subprocess.run(
                [str(executable), "--version"],
                capture_output=True,
                text=True,
                errors="replace",
                timeout=PROBE_TIMEOUT_SECONDS,
            )
        except UnicodeDecodeError as exc:
            raise HimalayaInvocationError(
                VERSION_UNSUPPORTED_REASON,
                "The Himalaya version probe output was not decodable; no mailbox command was sent.",
            ) from exc
        except (OSError, subprocess.SubprocessError) as exc:
            raise HimalayaInvocationError(
                UNAVAILABLE_REASON,
                "The Himalaya version probe could not be started; no mailbox command was sent.",
            ) from exc
        if completed.returncode != 0:
            raise HimalayaInvocationError(
                VERSION_UNSUPPORTED_REASON,
                "The Himalaya version probe failed; no mailbox command was sent.",
            )
        profile = parse_version_output(completed.stdout)
        if profile is None:
            raise HimalayaInvocationError(
                VERSION_UNSUPPORTED_REASON,
                "The installed Himalaya version is not a supported profile; no mailbox command was sent.",
            )
        if len(_PROFILE_CACHE) >= _MAX_PROFILE_CACHE_ENTRIES:
            _PROFILE_CACHE.pop(next(iter(_PROFILE_CACHE)), None)
        _PROFILE_CACHE[key] = profile
        return profile


def resolve_himalaya_profile_or_none() -> HimalayaProfile | None:
    """Resolve the executable's profile for a decision that precedes any write.

    The pre-write expunge guard needs the profile before any command can produce
    output.  This resolves the same executable as a real command and runs the
    same bounded, cached ``--version`` probe; the probe is not a mailbox command
    and never reaches ``TransportStats``/``process_starts``.  When the v1
    normalized path check fails, the raw, expanduser-expanded config path is
    validated instead, so a reachable native spelling does not depend on
    admin-share reachability.  An unknown, unparseable or unstartable version
    fails closed (``himalaya_version_unsupported``/``himalaya_unavailable``).  It
    returns ``None`` only when no CLI invocation exists at all (missing config,
    missing executable, invalid override); in that case the later command
    resolution raises before a child starts, so no write can happen either.

    Read-only seams do not call this: they read the profile that produced their
    own output (:class:`HimalayaOutput`), which is authoritative and starts no
    extra probe.
    """
    try:
        executable, _config_path = resolve_himalaya_invocation()
    except HimalayaInvocationError:
        # The v1 normalization is only an admin-share spelling of the same file.
        # Validate the raw path instead; the unchanged failure semantics apply.
        try:
            _validate_himalaya_config_path(_himalaya_config_candidate())
            executable = _resolve_himalaya_executable()
        except HimalayaInvocationError:
            return None
    return _profile_for_executable(executable)


def resolve_himalaya_downloads_dir(
    account: str | None = None,
    *,
    config_path: str | Path | None = None,
) -> str | None:
    """Return the proven Himalaya download directory, or ``None``.

    Reads only the ``downloads-dir`` key from the effective Himalaya config.
    The value comes from the selected account when that account carries one,
    otherwise from the top level.  An explicit ``account`` must match an
    ``[accounts.<name>]`` key exactly (case-sensitive); without one, exactly
    one account with ``default = true`` is required.  Every other selection,
    a missing/unreadable/invalid config, a non-string or blank value, or a
    relative result returns ``None``.

    Expansion mirrors the CLI: ``~`` and ``$VAR``/``${VAR}`` expand, the result
    must be absolute and is canonicalized.  ``%VAR%`` is rejected because
    ``shellexpand`` does not expand it; a remaining ``$``/``%``, a
    ``\\\\``-prefixed/UNC value (raw or canonical) and an unprovable path all
    return ``None``.  On Windows the ``\\\\?\\`` verbatim prefix is added only
    for an existing path.  No account segment is synthesized and there is no
    ``dirs::download_dir()`` fallback.

    The function is total: any unexpected exception returns ``None`` and no
    path or configuration value is logged.  The function never returns any
    other configuration value.
    """
    try:
        return _resolve_himalaya_downloads_dir(account, config_path=config_path)
    except Exception:  # noqa: BLE001 - an unprovable config never changes a read
        return None


def _resolve_himalaya_downloads_dir(
    account: str | None,
    *,
    config_path: str | Path | None,
) -> str | None:
    try:
        import tomllib
    except ImportError:
        return None

    if config_path is not None:
        candidate = Path(config_path)
    else:
        configured = os.environ.get("HIMALAYA_CONFIG")
        if configured:
            candidate = Path(configured).expanduser()
        else:
            candidate = _default_himalaya_config_path()

    try:
        if not candidate.is_file():
            return None
        with candidate.open("rb") as handle:
            document = tomllib.load(handle)
    except (OSError, ValueError, RecursionError):
        return None
    if not isinstance(document, dict):
        return None

    accounts = document.get("accounts")
    account_map = accounts if isinstance(accounts, dict) else {}
    if account is not None:
        if account not in account_map:
            return None
        selected_account: str | None = account
    elif account_map:
        defaults = [
            name
            for name, settings in account_map.items()
            if isinstance(settings, dict) and settings.get("default") is True
        ]
        if len(defaults) != 1:
            return None
        selected_account = defaults[0]
    else:
        # No account dimension is configured: the global key is the only value.
        selected_account = None

    account_settings = (
        account_map.get(selected_account) if selected_account is not None else None
    )
    if isinstance(account_settings, dict) and "downloads-dir" in account_settings:
        value = account_settings["downloads-dir"]
    else:
        value = document.get("downloads-dir")
    if not isinstance(value, str):
        return None

    raw_value = value.strip()
    # ``shellexpand`` leaves ``~user`` literal (only ``~`` and ``~/...``
    # expand); expanding it here would not match the CLI.
    if (
        raw_value.startswith("~")
        and raw_value != "~"
        and raw_value[1:2] not in ("/", "\\")
    ):
        return None
    # ``shellexpand`` does not expand %VAR%; doing so would not match the CLI.
    if "%" in raw_value:
        return None
    expanded = os.path.expandvars(os.path.expanduser(raw_value))
    if not expanded:
        return None
    if "$" in expanded or "%" in expanded:
        return None
    if expanded.replace("/", "\\").startswith("\\\\"):
        return None
    path = Path(expanded)
    if not path.is_absolute():
        return None
    try:
        resolved = path.resolve()
    except (OSError, RuntimeError, ValueError):
        return None
    resolved_text = str(resolved)
    if resolved_text.replace("/", "\\").startswith("\\\\"):
        return None
    if os.name == "nt" and path.exists() and not resolved_text.startswith("\\\\?\\"):
        resolved_text = "\\\\?\\" + resolved_text
    return resolved_text


def build_himalaya_command(args: list[str], account: str | None = None) -> list[str]:
    """Build a shell-free CLI command for the resolved, versioned profile.

    The v1 profile keeps the byte-identical legacy argv and ``-a`` placement,
    including the normalized admin-share ``-c`` token.  The v3 profile translates
    the same logical request into the documented v3 argv, places the global
    ``-a`` after ``-c <path>`` and before the subcommand, and passes the same
    config file under its native spelling (:func:`_native_himalaya_config_path`),
    because v3 does not need the v1 admin-share workaround.  The token is derived
    from the resolved path; ``HIMALAYA_CONFIG`` is never re-read here.  The
    selected profile is recorded for the caller's output interpretation (see
    :class:`HimalayaOutput`).
    """
    if not isinstance(args, list) or not args or any(not isinstance(arg, str) or not arg or "\x00" in arg for arg in args):
        raise ValueError("Himalaya arguments must be a non-empty list of non-empty text tokens.")
    executable, config_path = resolve_himalaya_invocation()
    profile = _profile_for_executable(executable)
    _PROFILE_LOCAL.profile = profile.name
    if profile.name == PROFILE_V3:
        try:
            translated = translate_v3_arguments(args)
        except ProfileError as exc:
            raise HimalayaInvocationError(exc.reason_code, str(exc)) from None
        account_args = ["-a", account] if account else []
        native_config = str(_native_himalaya_config_path(config_path))
        return [executable, "-c", native_config] + account_args + translated
    return [executable, "-c", str(config_path)] + _insert_account_arg(args, account)


def _insert_account_arg(args: list[str], account: str | None) -> list[str]:
    """Insert -a <account> into args at the correct position for Himalaya CLI v1.x."""
    if not account or "-a" in args or "--account" in args:
        return list(args)

    cmd_tokens = list(args)
    flags_with_val = {"-c", "--config", "-o", "--output"}
    boolean_flags = {"--quiet", "--debug", "--trace", "-h", "--help", "-V", "--version"}

    i = 0
    while i < len(cmd_tokens):
        token = cmd_tokens[i]
        if token in flags_with_val:
            i += 2
            continue
        if token in boolean_flags or token.startswith("-"):
            i += 1
            continue
        if i + 1 < len(cmd_tokens) and not cmd_tokens[i + 1].startswith("-"):
            return cmd_tokens[: i + 2] + ["-a", account] + cmd_tokens[i + 2 :]
        else:
            return cmd_tokens[: i + 1] + ["-a", account] + cmd_tokens[i + 1 :]

    return cmd_tokens + ["-a", account]


def _explicit_plain_output_requested(args: list[str]) -> bool:
    """Whether ``args`` carries exactly one explicit ``-o plain`` output format.

    Himalaya ``message save`` chooses its stdin branch only when stdin is not a
    TTY **and** the output format is not JSON (upstream rev
    ``1b70c4e0eaa72dee48353f0211e6cc0f0776fe98``); a JSON output format would
    silently fall back to the raw argv argument.  A payload command therefore
    has to pin ``-o plain`` explicitly.
    """
    found: list[str] = []
    index = 0
    while index < len(args):
        token = args[index]
        if token in ("-o", "--output"):
            if index + 1 < len(args):
                found.append(args[index + 1])
            index += 2
            continue
        if token.startswith("--output="):
            found.append(token.split("=", 1)[1])
        index += 1
    return found == ["plain"]


def _decode_process_stream(value: bytes | str | None) -> str:
    """Decode a captured subprocess stream to text without newline translation."""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value or ""


#: Upper bound on the v3 stdout error object that is parsed and kept.
_V3_ERROR_STDOUT_LIMIT_BYTES = 64 * 1024
#: Upper bound on each stored v3 error detail value.
_V3_ERROR_DETAIL_MAX_CHARS = 500


def _collapse_bounded_text(value: object) -> str:
    """Collapse whitespace and bound one detail value; non-strings become empty."""
    if not isinstance(value, str):
        return ""
    return re.sub(r"\s+", " ", value).strip()[:_V3_ERROR_DETAIL_MAX_CHARS]


def _parse_v3_stdout_error(stream: bytes | str | None) -> tuple[str, str] | None:
    """Parse the bounded v3 non-zero error object from child stdout.

    The v3 CLI writes ``{code, error, sources, backtrace}`` as JSON on stdout and
    leaves stderr empty.  At most 64 KiB are sliced before decoding (the payload
    path may hand over bytes); anything invalid, oversized, non-object or without
    a string ``code``/``error`` is ignored so the caller keeps the stderr
    fallback.  Only ``code``/``error`` survive, whitespace-collapsed and bounded;
    ``sources``/``backtrace`` and the raw JSON are never returned or stored.
    """
    if not isinstance(stream, (bytes, str)):
        return None
    text = _decode_process_stream(stream[:_V3_ERROR_STDOUT_LIMIT_BYTES])
    try:
        payload = json.loads(text)
    except (TypeError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    code = _collapse_bounded_text(payload.get("code"))
    message = _collapse_bounded_text(payload.get("error"))
    if not code and not message:
        return None
    return code, message


#: Reasons raised by the bounded diagnostic capture branch of ``run_himalaya``.
HIMALAYA_DIAGNOSTIC_OVERFLOW_REASON = "himalaya_diagnostic_overflow"
HIMALAYA_DIAGNOSTIC_OUTCOME_UNKNOWN_REASON = "himalaya_outcome_unknown"
HIMALAYA_DIAGNOSTIC_READER_FAILED_REASON = "himalaya_reader_failed"

#: Bounded post-kill reap/join budget (seconds) for the diagnostic branch.
_DIAGNOSTIC_REAP_BUDGET_SECONDS = 5.0

#: The process OS boundary for the bounded diagnostic branch.  It is a module
#: attribute so tests can fake *only* the process boundary without touching the
#: global ``subprocess.Popen`` used by the shared workspace-lock CLI.
_POPEN = subprocess.Popen


#: Canonical abort reason after two consecutive transport failures or an
#: exhausted start budget (issue #103, F6).
TRANSPORT_BLOCKED_REASON = "mailbox_transport_blocked"

#: Failure reasons that count as a *transport* failure.  A normal command or
#: authentication failure breaks the consecutive-error run instead of counting.
TRANSPORT_ERROR_REASON_CODES = frozenset(
    {
        "himalaya_transient",
        "himalaya_timeout",
        "himalaya_launch_failed",
        "himalaya_stdin_failed",
    }
)

#: Command classes that operate on exactly one specific item (envelope).  A
#: timeout of such a call is an item failure, not evidence that the transport
#: itself is down; every other transport failure keeps the unchanged F6
#: fail-closed behaviour.
ITEM_SCOPED_COMMAND_CLASSES = frozenset({"message read", "message export"})

#: Bounded number of durations kept per command class in ``transport_stats``.
TRANSPORT_DURATION_SAMPLES = 20

#: Several consecutive item timeouts *without any successful command between
#: them* mean no progress at all and abort fail-closed.  Two slow messages must
#: never end a run (the live defect behind #103); a run where every single item
#: times out must not grind through the whole mailbox either.
ITEM_FAILURE_MAX_CONSECUTIVE = 5

#: Block reason when item failures accumulate without any mailbox progress.
ITEM_FAILURE_BLOCK_REASON = "consecutive_item_failures_without_progress"

#: Explicit, documented process-start budget per transport-stats session.  It is
#: a runaway guard, not a batch-shaping limit: the documented fallback scan can
#: read a 2,500-envelope window and may use two processes per envelope (raw-MIME
#: export plus header/read), plus list tiers and the thread map.  10,000 keeps
#: roughly 2x headroom above that documented worst case.
DEFAULT_TRANSPORT_START_BUDGET = 10_000

#: The second consecutive transport failure aborts the run immediately.
TRANSPORT_MAX_CONSECUTIVE_ERRORS = 2

#: Hard, documented upper bound for the optional inter-start pacing (1 hour).
#: Pacing holds the spawn lock while waiting, so an unchecked operator value could
#: stall every start for an unbounded time; 3,600,000 ms still covers any
#: deliberate throttle while keeping ``pacing_ms / 1000.0`` finite.  The pilot
#: value is 250 ms; the operator owns the wait below this ceiling.
MAX_TRANSPORT_PACING_MS = 3_600_000

#: Closed purpose list for process-start telemetry (Issue #103, D1 first slice).
#: Every successful child creation is counted under exactly one of these labels;
#: ``other`` is the default for callers that do not bind a purpose.
TRANSPORT_PURPOSES = (
    "thread_map",
    "list",
    "scan_preview",
    "scan_inventory",
    "full_read",
    "full_inventory",
    "evaluation_raw",
    "id_fallback",
    "other",
)
_TRANSPORT_PURPOSES = frozenset(TRANSPORT_PURPOSES)

#: Inventory export purpose derived from the read purpose of the same helper call.
_INVENTORY_PURPOSES = {
    "scan_preview": "scan_inventory",
    "full_read": "full_inventory",
}


def _validate_purpose(purpose: str) -> str:
    """Reject an unknown purpose before any child creation or accounting."""
    if not isinstance(purpose, str) or purpose not in _TRANSPORT_PURPOSES:
        raise ValueError(
            "purpose must be one of the documented transport purposes: "
            + ", ".join(sorted(_TRANSPORT_PURPOSES))
        )
    return purpose


def _inventory_purpose(purpose: str) -> str:
    """Return the inventory-export purpose for one read purpose."""
    return _INVENTORY_PURPOSES.get(purpose, purpose)


_ACTIVE_TRANSPORT_STATS: ContextVar["TransportStats | None"] = ContextVar(
    "mail_desk_active_transport_stats", default=None
)
_LAST_TRANSPORT_STATS: dict[str, Any] | None = None


def _positional_words(args: list[str], limit: int) -> list[str]:
    """Return up to ``limit`` positional tokens after skipping flags and values."""
    tokens = list(args)
    flags_with_value = {
        "-a", "--account", "-c", "--config", "-o", "--output", "-f", "--folder",
        "-s", "--page-size", "-H", "--header", "-d", "--destination", "-F",
        "--preview-lines", "-t", "--target",
    }
    words: list[str] = []
    index = 0
    while index < len(tokens) and len(words) < limit:
        token = tokens[index]
        if token in flags_with_value:
            index += 2
            continue
        if token.startswith("-"):
            index += 1
            continue
        words.append(token)
        index += 1
    return words


def classify_himalaya_command(args: list[str]) -> str:
    """Return the stable command class of a Himalaya argv (for counters only).

    Only structural tokens are inspected: flags with a value are skipped and
    the first two positional words are joined (``envelope list``,
    ``message read``, ``folder list``).  No argument content is stored.
    """
    words = _positional_words(args, 2)
    return " ".join(words) if words else "unknown"


def item_envelope_id(args: list[str]) -> str | None:
    """Return the envelope id of one item-scoped command, if the argv binds one.

    Only ``message read`` and ``message export`` are item-scoped.  The id is
    the third positional token and must be a decimal envelope id; anything
    else is not treated as item-bound.
    """
    words = _positional_words(args, 3)
    if len(words) < 3:
        return None
    if " ".join(words[:2]) not in ITEM_SCOPED_COMMAND_CLASSES:
        return None
    candidate = words[2].strip()
    return candidate if candidate.isascii() and candidate.isdigit() else None


class TransportStats:
    """Process-local counters and the fail-closed transport guard.

    Counters are recorded by :func:`run_himalaya` for the active session:
    process starts, retries, errors and bounded durations per command class.
    A transport failure without item relation (network, launch, process
    communication) aborts with :data:`TRANSPORT_BLOCKED_REASON` on the second
    consecutive occurrence, unchanged from F6.  A timeout of one item-scoped
    command (``message read``/``message export`` for one envelope) is an item
    failure instead: it is counted, does not abort, and only
    :data:`ITEM_FAILURE_MAX_CONSECUTIVE` item failures *without any successful
    command in between* abort the run.  An exhausted ``start_budget`` blocks
    every further process start.
    """

    def __init__(
        self,
        *,
        start_budget: int | None = DEFAULT_TRANSPORT_START_BUDGET,
        max_consecutive_errors: int = TRANSPORT_MAX_CONSECUTIVE_ERRORS,
        max_consecutive_item_failures: int = ITEM_FAILURE_MAX_CONSECUTIVE,
        pacing_ms: int = 0,
        transient_backoff_s: float = 2.0,
        clock: Callable[[], float] | None = None,
        sleep: Callable[[float], None] | None = None,
    ) -> None:
        if start_budget is not None and (
            isinstance(start_budget, bool)
            or not isinstance(start_budget, int)
            or start_budget < 1
        ):
            raise ValueError("start_budget must be a positive integer or None")
        # Issue #103 P5: pacing and transient backoff are explicit, validated
        # bounds.  The documented defaults (0 ms / 2.0 s) keep the unchanged
        # timing; activation is config-only.
        if (
            isinstance(pacing_ms, bool)
            or not isinstance(pacing_ms, int)
            or pacing_ms < 0
            or pacing_ms > MAX_TRANSPORT_PACING_MS
        ):
            raise ValueError(
                "pacing_ms must be an integer between 0 and "
                f"{MAX_TRANSPORT_PACING_MS}"
            )
        if (
            isinstance(transient_backoff_s, bool)
            or not isinstance(transient_backoff_s, (int, float))
        ):
            raise ValueError("transient_backoff_s must be a positive finite number")
        try:
            backoff_seconds = float(transient_backoff_s)
        except (TypeError, ValueError, OverflowError):
            raise ValueError(
                "transient_backoff_s must be a positive finite number"
            ) from None
        if not math.isfinite(backoff_seconds) or backoff_seconds <= 0:
            raise ValueError("transient_backoff_s must be a positive finite number")
        if clock is not None and not callable(clock):
            raise ValueError("clock must be callable or None")
        if sleep is not None and not callable(sleep):
            raise ValueError("sleep must be callable or None")
        self.start_budget = start_budget
        self.max_consecutive_errors = max(1, int(max_consecutive_errors))
        self.max_consecutive_item_failures = max(1, int(max_consecutive_item_failures))
        self.pacing_ms = pacing_ms
        self.transient_backoff_s = backoff_seconds
        # ``None`` keeps the module functions resolved at call time, so patching
        # ``himalaya.time.sleep`` (or a test clock) stays effective.
        self._clock = clock
        self._sleep = sleep
        self.pacing_waits = 0
        self.pacing_wait_ms_total = 0
        # Reference of the last real (paced) start; payload/diagnostic and failed
        # starts never update it.
        self._last_started_at: float | None = None
        self._lock = threading.Lock()
        # Serializes budget/block checks with the actual child creation.  The
        # lock is never held while the child runs; only until Popen returns.
        self._spawn_lock = threading.Lock()
        self.process_starts = 0
        self.retries = 0
        self.errors = 0
        self.consecutive_transport_errors = 0
        self.consecutive_item_failures = 0
        self.blocked_reason: str | None = None
        self.block_trigger: dict[str, Any] | None = None
        # The stable F6 counters map keeps its exact three-key shape; the
        # detailed per-class telemetry is additive under ``command_telemetry``.
        self.commands: dict[str, dict[str, int]] = {}
        self.command_telemetry: dict[str, dict[str, Any]] = {}
        self._starts_by_purpose: dict[str, int] = {}

    def _bucket(self, command_class: str) -> dict[str, int]:
        return self.commands.setdefault(
            command_class, {"starts": 0, "retries": 0, "errors": 0}
        )

    def _timing_bucket(self, command_class: str) -> dict[str, Any]:
        return self.command_telemetry.setdefault(
            command_class,
            {
                "duration_ms_total": 0,
                "durations_ms": [],
                "last_duration_ms": None,
                "last_exit_code": None,
                "last_reason_code": None,
            },
        )

    def _record_timing_locked(
        self,
        command_class: str,
        duration_ms: int | None,
        exit_code: int | None,
        reason_code: str | None,
    ) -> None:
        if duration_ms is None:
            return
        bucket = self._timing_bucket(command_class)
        clamped = max(0, int(duration_ms))
        bucket["duration_ms_total"] += clamped
        samples = bucket["durations_ms"]
        samples.append(clamped)
        if len(samples) > TRANSPORT_DURATION_SAMPLES:
            del samples[: len(samples) - TRANSPORT_DURATION_SAMPLES]
        bucket["last_duration_ms"] = clamped
        bucket["last_exit_code"] = exit_code
        bucket["last_reason_code"] = reason_code

    @staticmethod
    def _trigger(
        command_class: str,
        reason_code: str,
        duration_ms: int | None,
        exit_code: int | None,
        envelope_id: str | None,
    ) -> dict[str, Any]:
        return {
            "command_class": command_class,
            "envelope_id": envelope_id,
            "duration_ms": None if duration_ms is None else max(0, int(duration_ms)),
            "exit_code": exit_code,
            "reason_code": reason_code,
        }

    def start_process(
        self,
        command_class: str,
        factory: Callable[[], Any],
        *,
        retry: bool = False,
        purpose: str = "other",
        pace: bool = True,
    ) -> Any:
        """Create one child atomically against the budget, the F6 block and pacing.

        Budget check, pacing wait, the repeated budget/block check and the actual
        ``Popen`` happen under one spawn lock.  Two workers can therefore never
        reserve the same last budget slot, a block published by
        :meth:`record_error` cannot slip into the window between the check and
        child creation, and two real starts honour the configured minimum gap.
        The lock is released as soon as the child exists; waiting for its output
        is not serialized.

        ``purpose`` is validated before the budget check and the child creation
        and counted only after ``factory()`` returned successfully: a failed
        launch produces neither a start nor a purpose count and never updates the
        pacing reference.  ``pace=False`` (payload/APPEND and the bounded
        diagnostic spawn) never waits and never updates that reference.
        """
        _validate_purpose(purpose)
        with self._spawn_lock:
            with self._lock:
                reason = self._admission_reason_locked()
            if reason is not None:
                raise self.blocked_error(command_class, reason=reason)
            if pace and self.pacing_ms > 0:
                with self._lock:
                    last_started_at = self._last_started_at
                if last_started_at is not None:
                    wait_s = (self.pacing_ms / 1000.0) - (
                        self._now() - last_started_at
                    )
                    if wait_s > 0:
                        self._wait(wait_s)
                        with self._lock:
                            self.pacing_waits += 1
                            self.pacing_wait_ms_total += int(round(wait_s * 1000))
                        # The wait may have published a block or consumed the
                        # last budget slot elsewhere; re-check before the start.
                        with self._lock:
                            reason = self._admission_reason_locked()
                        if reason is not None:
                            raise self.blocked_error(command_class, reason=reason)
            process = factory()
            with self._lock:
                if pace:
                    self._last_started_at = self._now()
                self.process_starts += 1
                self._bucket(command_class)["starts"] += 1
                self._starts_by_purpose[purpose] = (
                    self._starts_by_purpose.get(purpose, 0) + 1
                )
                if retry:
                    self.retries += 1
                    self._bucket(command_class)["retries"] += 1
            return process

    def _admission_reason_locked(self) -> str | None:
        """Return the blocking reason for the next start, latching a budget stop."""
        if self.blocked_reason:
            return self.blocked_reason
        if self.start_budget is not None and self.process_starts >= self.start_budget:
            self.blocked_reason = "start_budget_exhausted"
            return self.blocked_reason
        return None

    def _now(self) -> float:
        """Return the current monotone time (resolved at call time by default)."""
        return self._clock() if self._clock is not None else time.monotonic()

    def _wait(self, seconds: float) -> None:
        """Sleep ``seconds`` through the injected sleep or ``time.sleep``."""
        if self._sleep is not None:
            self._sleep(seconds)
        else:
            time.sleep(seconds)

    def retry_backoff_seconds(self, attempt: int) -> float:
        """Return the configured transient backoff for one zero-based attempt."""
        return self.transient_backoff_s * (attempt + 1)

    def sleep_retry_backoff(self, attempt: int) -> None:
        """Wait the configured transient backoff through the resolved sleep."""
        self._wait(self.retry_backoff_seconds(attempt))

    def blocked_error(
        self, command_class: str, *, reason: str | None = None
    ) -> HimalayaInvocationError:
        """Return the one structured abort error used for every F6 stop."""
        with self._lock:
            reason = reason or self.blocked_reason or "transport_blocked"
            trigger = dict(self.block_trigger) if self.block_trigger else {
                "command_class": command_class,
                "envelope_id": None,
                "duration_ms": None,
                "exit_code": None,
                "reason_code": reason,
            }
        return HimalayaInvocationError(
            TRANSPORT_BLOCKED_REASON,
            f"Himalaya transport blocked ({reason}); no further process was started for '{command_class}'.",
            details={"block_reason": reason, "trigger": trigger},
        )

    def raise_if_blocked(self, command_class: str) -> None:
        """Raise immediately once the threshold is reached (never swallow it)."""
        with self._lock:
            reason = self.blocked_reason
        if reason:
            raise self.blocked_error(command_class, reason=reason)

    def record_success(
        self,
        *,
        command_class: str | None = None,
        duration_ms: int | None = None,
        exit_code: int | None = 0,
    ) -> None:
        """Record one successful command as progress for both guard dimensions."""
        with self._lock:
            self.consecutive_transport_errors = 0
            self.consecutive_item_failures = 0
            if command_class is not None:
                self._record_timing_locked(command_class, duration_ms, exit_code, None)

    def record_error(
        self,
        command_class: str,
        reason_code: str,
        *,
        duration_ms: int | None = None,
        exit_code: int | None = None,
        item_scoped: bool = False,
        envelope_id: str | None = None,
    ) -> bool:
        """Record one error and return whether this error reached an abort.

        The threshold is latched here, not only checked before the next start:
        the abort-triggering failure itself is returned as the structured
        ``mailbox_transport_blocked`` error, so a caller that swallows a
        transient error cannot turn the run into a silent success.

        An item-scoped timeout is an item failure: it is counted and reported,
        but it only aborts when several item failures occur without any
        successful command in between (no progress at all).
        """
        trigger = self._trigger(
            command_class, reason_code, duration_ms, exit_code, envelope_id
        )
        with self._spawn_lock:
            with self._lock:
                self.errors += 1
                self._bucket(command_class)["errors"] += 1
                self._record_timing_locked(
                    command_class, duration_ms, exit_code, reason_code
                )
                if item_scoped and reason_code == "himalaya_timeout":
                    self.consecutive_item_failures += 1
                    if (
                        self.blocked_reason is None
                        and self.consecutive_item_failures
                        >= self.max_consecutive_item_failures
                    ):
                        self.blocked_reason = ITEM_FAILURE_BLOCK_REASON
                        self.block_trigger = trigger
                    return self.blocked_reason is not None
                if reason_code not in TRANSPORT_ERROR_REASON_CODES:
                    self.consecutive_transport_errors = 0
                    return False
                self.consecutive_transport_errors += 1
                if (
                    self.blocked_reason is None
                    and self.consecutive_transport_errors >= self.max_consecutive_errors
                ):
                    self.blocked_reason = "two_consecutive_transport_errors"
                    self.block_trigger = trigger
                return self.blocked_reason is not None

    def snapshot(self) -> dict[str, Any]:
        """Return a stable, JSON-safe counter snapshot for the result envelope."""
        with self._lock:
            telemetry: dict[str, dict[str, Any]] = {}
            for command in sorted(set(self.commands) | set(self.command_telemetry)):
                counts = self.commands.get(command, {"starts": 0, "retries": 0, "errors": 0})
                timing = self.command_telemetry.get(command, {})
                telemetry[command] = {
                    "starts": counts["starts"],
                    "retries": counts["retries"],
                    "errors": counts["errors"],
                    "duration_ms_total": timing.get("duration_ms_total", 0),
                    "durations_ms": list(timing.get("durations_ms", ())),
                    "last_duration_ms": timing.get("last_duration_ms"),
                    "last_exit_code": timing.get("last_exit_code"),
                    "last_reason_code": timing.get("last_reason_code"),
                }
            return {
                "process_starts": self.process_starts,
                "retries": self.retries,
                "errors": self.errors,
                "consecutive_transport_errors": self.consecutive_transport_errors,
                "consecutive_item_failures": self.consecutive_item_failures,
                "start_budget": self.start_budget,
                "pacing_ms": self.pacing_ms,
                "transient_backoff_s": self.transient_backoff_s,
                "pacing_waits": self.pacing_waits,
                "pacing_wait_ms_total": self.pacing_wait_ms_total,
                "blocked": self.blocked_reason is not None,
                "block_reason": self.blocked_reason,
                "block_trigger": (
                    dict(self.block_trigger) if self.block_trigger else None
                ),
                "starts_by_purpose": {
                    purpose: int(self._starts_by_purpose.get(purpose, 0))
                    for purpose in sorted(TRANSPORT_PURPOSES)
                },
                "duration_samples_per_command": TRANSPORT_DURATION_SAMPLES,
                "commands": {
                    command: dict(counts) for command, counts in sorted(self.commands.items())
                },
                "command_telemetry": telemetry,
            }


@contextmanager
def transport_stats_session(
    start_budget: int | None = DEFAULT_TRANSPORT_START_BUDGET,
    *,
    pacing_ms: int = 0,
    transient_backoff_s: float = 2.0,
    clock: Callable[[], float] | None = None,
    sleep: Callable[[float], None] | None = None,
):
    """Activate one counter/guard session; nested sessions reuse the active one.

    ``pacing_ms`` and ``transient_backoff_s`` default to the documented values
    (0 ms / 2.0 s) and are validated by :class:`TransportStats` before any
    process.  ``clock``/``sleep`` exist for deterministic tests; ``None`` keeps
    ``time.monotonic``/``time.sleep``.
    """
    active = _ACTIVE_TRANSPORT_STATS.get()
    if active is not None:
        yield active
        return
    if start_budget is None:
        start_budget = DEFAULT_TRANSPORT_START_BUDGET
    stats_kwargs: dict[str, Any] = {
        "pacing_ms": pacing_ms,
        "transient_backoff_s": transient_backoff_s,
    }
    if clock is not None:
        stats_kwargs["clock"] = clock
    if sleep is not None:
        stats_kwargs["sleep"] = sleep
    stats = TransportStats(start_budget=start_budget, **stats_kwargs)
    token = _ACTIVE_TRANSPORT_STATS.set(stats)
    global _LAST_TRANSPORT_STATS
    try:
        yield stats
    finally:
        _LAST_TRANSPORT_STATS = stats.snapshot()
        _ACTIVE_TRANSPORT_STATS.reset(token)


def current_transport_stats() -> TransportStats | None:
    """Return the active transport-stats session, if one is active."""
    return _ACTIVE_TRANSPORT_STATS.get()


def last_transport_stats() -> dict[str, Any] | None:
    """Return the snapshot of the most recently completed session."""
    return _LAST_TRANSPORT_STATS


def reset_transport_stats() -> None:
    """Clear the stored last snapshot (test isolation only)."""
    global _LAST_TRANSPORT_STATS
    _LAST_TRANSPORT_STATS = None


def _write_all_binary(stream: Any, payload: bytes) -> None:
    """Write the full payload, looping over short/no-progress writes.

    A single ``stream.write`` call may deliver only part of the buffer (or return
    ``None`` for a non-blocking stream).  The loop guarantees the whole payload is
    delivered or raises, so a ``message save`` payload is never silently
    truncated; the caller treats any error as an unknown APPEND outcome.
    """
    view = memoryview(payload)
    total = 0
    while total < len(view):
        written = stream.write(view[total:])
        if written is None or written <= 0:
            raise OSError("stdin write made no progress")
        total += int(written)


def _run_himalaya_diagnostic(
    cmd: list[str],
    *,
    stdin_bytes: bytes,
    timeout: float,
    capture: Any,
    spawn: Callable[..., Any] | None = None,
) -> str:
    """Run one bounded diagnostic child through the caller's capture object.

    The capture owns the fixed child environment, the resource bounds and the
    bounded stderr parser.  Only fixed, structured data is recorded; no raw
    stderr or stdout is ever stored on the capture.

    All shared mutable state (raw chunk buffers and flags) is written under one
    lock and snapshotted ("frozen") under a bounded acquire after the single
    cleanup deadline.  Background reader threads can therefore never mutate the
    finalized evidence, and any thread that is still alive at freeze time forces
    an *unknown* outcome with ``cleanup_incomplete`` set.
    """
    env = capture.child_environment(os.environ)
    read_chunk = capture.read_chunk_bytes
    stderr_cap = capture.stderr_cap_bytes
    stdout_cap = capture.stdout_cap_bytes

    lock = threading.Lock()
    state: dict[str, Any] = {
        "stderr_chunks": [],
        "stderr_bytes": 0,
        "stderr_overflow": False,
        "stdout_chunks": [],
        "stdout_bytes": 0,
        "stdout_truncated": False,
        "stdout_overflow": False,
        "reader_error": False,
        "stdin_error": False,
        "frozen": False,
    }

    def _kill() -> None:
        try:
            process.kill()
        except (OSError, ValueError):
            pass

    try:
        process = (spawn or _POPEN)(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            bufsize=0,
        )
    except OSError:
        raise HimalayaInvocationError(
            "himalaya_launch_failed",
            "Himalaya could not be launched; the diagnostic APPEND outcome is unknown and it was not retried.",
        ) from None

    def _record(mutator) -> None:
        with lock:
            if not state["frozen"]:
                mutator()

    def _write_stdin() -> None:
        stream = process.stdin
        if stream is None:
            _record(lambda: state.__setitem__("stdin_error", True))
            _kill()
            return
        try:
            _write_all_binary(stream, stdin_bytes)
            stream.flush()
        except (BrokenPipeError, OSError, ValueError):
            _record(lambda: state.__setitem__("stdin_error", True))
            _kill()
        finally:
            try:
                stream.close()
            except (OSError, ValueError):
                pass

    def _append_bounded(key_chunks: str, key_bytes: str, key_overflow: str, cap: int, chunk: bytes) -> bool:
        overflow = False
        with lock:
            if state["frozen"]:
                return False
            remaining = cap - state[key_bytes]
            if remaining <= 0:
                state[key_overflow] = True
                return True
            state[key_chunks].append(bytes(chunk[:remaining]))
            state[key_bytes] += min(len(chunk), remaining)
            if len(chunk) > remaining:
                state[key_overflow] = True
                overflow = True
        return overflow

    def _drain_stdout() -> None:
        stream = process.stdout
        if stream is None:
            _record(lambda: state.__setitem__("reader_error", True))
            _kill()
            return
        try:
            while True:
                chunk = stream.read(read_chunk)
                if not chunk:
                    break
                if _append_bounded("stdout_chunks", "stdout_bytes", "stdout_overflow", stdout_cap, chunk):
                    _record(lambda: state.__setitem__("stdout_truncated", True))
                    _kill()
                    break
        except (OSError, ValueError):
            _record(lambda: state.__setitem__("reader_error", True))
            _kill()

    def _drain_stderr() -> None:
        stream = process.stderr
        if stream is None:
            _record(lambda: state.__setitem__("reader_error", True))
            _kill()
            return
        try:
            while True:
                chunk = stream.read(read_chunk)
                if not chunk:
                    break
                if _append_bounded("stderr_chunks", "stderr_bytes", "stderr_overflow", stderr_cap, chunk):
                    _kill()
                    break
        except (OSError, ValueError):
            # A swallowed reader error would hide an unknown outcome; record it.
            _record(lambda: state.__setitem__("reader_error", True))
            _kill()

    threads = [
        threading.Thread(target=_write_stdin, name="diagnostic-stdin", daemon=True),
        threading.Thread(target=_drain_stdout, name="diagnostic-stdout", daemon=True),
        threading.Thread(target=_drain_stderr, name="diagnostic-stderr", daemon=True),
    ]
    for thread in threads:
        thread.start()

    timed_out = False
    killed = False
    waited = False
    exit_code: int | None = None
    run_deadline = time.monotonic() + float(timeout)
    try:
        exit_code = process.wait(timeout=max(0.0, run_deadline - time.monotonic()))
        waited = True
    except subprocess.TimeoutExpired:
        timed_out = True
        killed = True
        _kill()

    # One single bounded cleanup budget (not one per join/wait).
    cleanup_deadline = time.monotonic() + max(0.0, _DIAGNOSTIC_REAP_BUDGET_SECONDS)
    if killed:
        try:
            exit_code = process.wait(timeout=max(0.0, cleanup_deadline - time.monotonic()))
            waited = True
        except Exception:  # noqa: BLE001 - reaping must never mask the timeout
            exit_code = None
    for thread in threads:
        thread.join(max(0.0, cleanup_deadline - time.monotonic()))
    alive = [thread for thread in threads if thread.is_alive()]

    # Freeze and snapshot under a bounded acquire; a still-running thread must
    # never mutate the evidence we are about to finalize.
    acquired = lock.acquire(timeout=max(0.001, cleanup_deadline - time.monotonic()))
    snapshot: dict[str, Any] | None = None
    if acquired:
        state["frozen"] = True
        snapshot = {
            "stderr_chunks": list(state["stderr_chunks"]),
            "stderr_overflow": bool(state["stderr_overflow"]),
            "stdout_chunks": list(state["stdout_chunks"]),
            "stdout_bytes": int(state["stdout_bytes"]),
            "stdout_truncated": bool(state["stdout_truncated"]),
            "stdout_overflow": bool(state["stdout_overflow"]),
            "reader_error": bool(state["reader_error"] or state["stdin_error"]),
        }
        lock.release()

    cleanup_incomplete = bool(alive) or snapshot is None
    child_reaped = bool(waited and exit_code is not None)

    if not alive and not cleanup_incomplete:
        for stream_name in ("stdin", "stdout", "stderr"):
            stream = getattr(process, stream_name, None)
            if stream is None:
                continue
            try:
                stream.close()
            except (OSError, ValueError):
                pass

    if snapshot is None:
        stderr_result = {
            "stderr_bytes": 0,
            "safe_events": [],
            "event_count": 0,
            "dropped_unknown": 0,
            "dropped_malformed": 0,
            "events_overflow": True,
        }
        stdout_data = b""
    else:
        stderr_result = capture.parse_stderr(snapshot["stderr_chunks"])
        stdout_data = b"".join(snapshot["stdout_chunks"])

    over_budget = bool(
        cleanup_incomplete
        or stderr_result["events_overflow"]
        or timed_out
        or (snapshot is not None and (snapshot["stderr_overflow"] or snapshot["stdout_overflow"]))
    )
    result = {
        "exit_code": exit_code,
        "timed_out": timed_out,
        "stderr": stderr_result,
        "stderr_overflow": bool(snapshot["stderr_overflow"]) if snapshot else True,
        "stdout_overflow": bool(snapshot["stdout_overflow"]) if snapshot else False,
        "stdout_bytes": int(snapshot["stdout_bytes"]) if snapshot else 0,
        "stdout_truncated": bool(snapshot["stdout_truncated"]) if snapshot else False,
        "reader_error": bool(snapshot["reader_error"]) if snapshot else True,
        "cleanup_incomplete": cleanup_incomplete,
        "child_reaped": child_reaped,
        "over_budget": over_budget,
    }
    capture.record_run(result)

    if cleanup_incomplete:
        raise HimalayaInvocationError(
            HIMALAYA_DIAGNOSTIC_READER_FAILED_REASON,
            "The diagnostic stream cleanup did not complete; the APPEND outcome is unknown and it was not retried.",
        ) from None
    if timed_out:
        raise HimalayaInvocationError(
            "himalaya_timeout",
            "Himalaya timed out; the diagnostic APPEND outcome is unknown and it was not retried.",
        ) from None
    if stderr_result["events_overflow"] or result["stderr_overflow"]:
        raise HimalayaInvocationError(
            HIMALAYA_DIAGNOSTIC_OVERFLOW_REASON,
            "The diagnostic stderr exceeded its bound; the APPEND outcome is unknown and it was not retried.",
        ) from None
    if result["stdout_overflow"]:
        raise HimalayaInvocationError(
            HIMALAYA_DIAGNOSTIC_OVERFLOW_REASON,
            "The diagnostic stdout exceeded its bound; the APPEND outcome is unknown and it was not retried.",
        ) from None
    if result["reader_error"]:
        raise HimalayaInvocationError(
            HIMALAYA_DIAGNOSTIC_READER_FAILED_REASON,
            "The diagnostic pipe stream failed; the APPEND outcome is unknown and it was not retried.",
        ) from None
    if exit_code is None:
        raise HimalayaInvocationError(
            HIMALAYA_DIAGNOSTIC_OUTCOME_UNKNOWN_REASON,
            "The diagnostic child exit status is unknown; the APPEND outcome is unknown and it was not retried.",
        ) from None
    if exit_code != 0:
        raise HimalayaInvocationError(
            "himalaya_command_failed",
            "Himalaya failed; the diagnostic APPEND outcome was recorded without raw output.",
        ) from None
    return stdout_data.decode("utf-8", errors="replace")


def _run_process(
    cmd: list[str],
    *,
    stdin_bytes: bytes | None,
    timeout: int,
    env_vars: dict[str, str],
    stats: "TransportStats | None",
    command_class: str,
    retry: bool,
    purpose: str = "other",
) -> Any:
    """Run one child either through the atomic stats gate or the legacy path.

    The stats-active path creates the child with :meth:`TransportStats.start_process`
    so budget and block checks share one lock with ``Popen``.  The no-session
    path keeps ``subprocess.run`` unchanged; it has no budget or F6 guard to
    make atomic.  Both branches return a ``CompletedProcess``-compatible object.
    """
    if stats is None:
        if stdin_bytes is None:
            return subprocess.run(
                cmd,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=env_vars,
                timeout=timeout,
            )
        return subprocess.run(
            cmd,
            input=stdin_bytes,
            capture_output=True,
            env=env_vars,
            timeout=timeout,
        )

    if stdin_bytes is None:
        process = stats.start_process(
            command_class,
            lambda: _POPEN(
                cmd,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=env_vars,
            ),
            retry=retry,
            purpose=purpose,
            pace=True,
        )
    else:
        process = stats.start_process(
            command_class,
            lambda: _POPEN(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=env_vars,
            ),
            retry=retry,
            purpose=purpose,
            pace=False,
        )
    try:
        if stdin_bytes is None:
            stdout, stderr = process.communicate(timeout=timeout)
        else:
            stdout, stderr = process.communicate(input=stdin_bytes, timeout=timeout)
    except subprocess.TimeoutExpired:
        # Mirror subprocess.run: kill and reap before surfacing the timeout.
        try:
            process.kill()
        except (OSError, ValueError):
            pass
        try:
            process.communicate()
        except (OSError, ValueError):
            pass
        raise
    return subprocess.CompletedProcess(cmd, process.returncode, stdout, stderr)


def run_himalaya(
    args: list[str],
    account: str | None = None,
    timeout: int = 35,
    max_retries: int = 5,
    *,
    stdin_bytes: bytes | None = None,
    stdin_v1_terminator: bytes | None = None,
    diagnostic_capture: Any | None = None,
    purpose: str = "other",
) -> str:
    """Execute a non-interactive Himalaya command, retrying transient failures only.

    ``stdin_bytes`` is the optional binary payload channel for commands that
    consume stdin instead of argv (Himalaya 1.2.0 ``message save``).  It is
    keyword-only and defaults to ``None``, so every existing non-payload caller
    keeps the unchanged ``stdin=DEVNULL``/``text=True`` path.  When a payload is
    supplied it is validated as strict UTF-8 (upstream would silently truncate on
    an invalid byte), written to the process pipe in binary mode (no newline
    translation) and the command must carry an explicit ``-o plain`` output
    format so the JSON branch cannot bypass stdin; a payload command is executed
    exactly once (an APPEND must never be retried).  Launch/communication
    ``OSError`` becomes ``himalaya_launch_failed`` for both the payload and the
    non-payload path; its outcome may be unknown and it is never retried.

    ``stdin_v1_terminator`` (keyword-only, default ``None``) marks the trailing
    bytes a caller appended for the pinned v1 ``message save`` transport: v1
    reads stdin with ``lines()`` and drops the final line terminator, so one
    extra CRLF compensates it.  A v3 ``message add`` stores the piped message as
    given and makes its line endings CRLF, so the same compensator would add an
    empty line.  Under the v3 profile exactly this suffix is removed before the
    child starts; the v1 payload stays byte-identical.  The declared suffix must
    actually end the payload or the call fails closed with ``ValueError``.

    ``diagnostic_capture`` is an optional, typed, keyword-only internal object
    (see ``core.diagnostics``) that switches a payload command onto the bounded
    diagnostic branch: exactly one child, bounded concurrent pipe drain and no
    retry.  It defaults to ``None``, so the original path stays byte-identical.

    ``purpose`` is the additive process-start label (Issue #103, D1 first
    slice).  It is validated before any executable/config resolution or child
    creation, is forwarded unchanged through every retry attempt and defaults
    to ``other``.  The bounded diagnostic spawn is always counted as ``other``.
    """
    if not isinstance(timeout, int) or timeout <= 0:
        raise ValueError("timeout must be a positive integer.")
    if not isinstance(max_retries, int) or max_retries < 1:
        raise ValueError("max_retries must be at least one.")
    _validate_purpose(purpose)
    if stdin_bytes is not None:
        if not isinstance(stdin_bytes, bytes) or not stdin_bytes:
            raise ValueError("stdin_bytes must be a non-empty bytes object.")
        if stdin_v1_terminator is not None and (
            not isinstance(stdin_v1_terminator, bytes) or not stdin_v1_terminator
        ):
            raise ValueError("stdin_v1_terminator must be a non-empty bytes object.")
        # Himalaya 1.2.0 reads stdin with ``lines().map_while(Result::ok)`` and
        # would silently truncate at the first invalid byte, so the payload must
        # be validated as strict UTF-8 before any executable/config resolution
        # or subprocess.  The validation message never contains the payload.
        try:
            stdin_bytes.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError(
                "stdin_bytes must be valid UTF-8; the payload was not sent."
            ) from exc
        if stdin_v1_terminator is not None and not stdin_bytes.endswith(stdin_v1_terminator):
            raise ValueError(
                "stdin_v1_terminator must end the payload; the payload was not sent."
            )
        if not _explicit_plain_output_requested(args):
            raise ValueError(
                "stdin_bytes requires exactly one explicit '-o plain' output format; "
                "JSON output would bypass stdin."
            )
    elif stdin_v1_terminator is not None:
        raise ValueError("stdin_v1_terminator requires a stdin_bytes payload.")
    _PROFILE_LOCAL.profile = None
    cmd = build_himalaya_command(args, account)
    profile_name = getattr(_PROFILE_LOCAL, "profile", None)
    if (
        stdin_bytes is not None
        and stdin_v1_terminator is not None
        and profile_name == PROFILE_V3
    ):
        # v3 ``message add`` keeps the piped message as given (line endings made
        # CRLF) and has no ``lines()``/join step, so the v1 compensator would add
        # an empty line.  Remove exactly the declared suffix.
        stdin_bytes = stdin_bytes[: -len(stdin_v1_terminator)]
    command_class = classify_himalaya_command(args)
    envelope_id = item_envelope_id(args)
    item_scoped = envelope_id is not None
    stats = current_transport_stats()
    if diagnostic_capture is not None:
        # Opt-in bounded diagnostic branch: exactly one child, bounded drain, no
        # retry.  ``stdin_bytes`` is required (a payload command); validation
        # above already ran.
        if stdin_bytes is None:
            raise ValueError("diagnostic_capture requires a payload stdin_bytes.")
        spawn = None
        if stats is not None:
            def spawn(*args: Any, **kwargs: Any) -> Any:
                return stats.start_process(
                    command_class, lambda: _POPEN(*args, **kwargs), purpose="other", pace=False
                )
        started = time.monotonic()
        try:
            output = _run_himalaya_diagnostic(
                cmd,
                stdin_bytes=stdin_bytes,
                timeout=float(timeout),
                capture=diagnostic_capture,
                spawn=spawn,
            )
        except HimalayaInvocationError as exc:
            duration_ms = int((time.monotonic() - started) * 1000)
            if exc.reason_code == TRANSPORT_BLOCKED_REASON:
                raise
            if stats is not None and stats.record_error(
                command_class,
                exc.reason_code,
                duration_ms=duration_ms,
                item_scoped=item_scoped,
                envelope_id=envelope_id,
            ):
                raise stats.blocked_error(command_class) from exc
            raise
        if stats is not None:
            stats.record_success(
                command_class=command_class,
                duration_ms=int((time.monotonic() - started) * 1000),
            )
        return HimalayaOutput(output, profile_name)
    env_vars = os.environ.copy()
    env_vars["PAGER"] = "cat"

    # A payload command is never retried: the caller's attempt budget cannot
    # turn one draft into a duplicate APPEND.
    attempts = 1 if stdin_bytes is not None else max_retries
    last_err = None
    for attempt in range(attempts):
        started = time.monotonic()
        try:
            res = _run_process(
                cmd,
                stdin_bytes=stdin_bytes,
                timeout=timeout,
                env_vars=env_vars,
                stats=stats,
                command_class=command_class,
                retry=attempt > 0,
                purpose=purpose,
            )
            duration_ms = int((time.monotonic() - started) * 1000)
            if res.returncode != 0:
                err_msg = _decode_process_stream(res.stderr).strip()
                failure_details: dict[str, Any] = {}
                if profile_name == PROFILE_V3:
                    # v3 writes its structured error as JSON on stdout and leaves
                    # stderr empty; only the bounded code/error survive.
                    v3_error = _parse_v3_stdout_error(res.stdout)
                    if v3_error is not None:
                        v3_code, v3_message = v3_error
                        if v3_code:
                            failure_details["himalaya_error_code"] = v3_code
                        if v3_message:
                            failure_details["himalaya_error_message"] = v3_message
                        failure_details["exit_code"] = res.returncode
                        err_msg = ": ".join(part for part in (v3_code, v3_message) if part)
                # Check for transient connection errors
                if _is_transient_himalaya_error(err_msg):
                    if stats is not None and stats.record_error(
                        command_class,
                        "himalaya_transient",
                        duration_ms=duration_ms,
                        exit_code=res.returncode,
                        item_scoped=item_scoped,
                        envelope_id=envelope_id,
                    ):
                        # The abort-triggering failure is the structured abort
                        # itself; retry/no retry cannot swallow it.
                        raise stats.blocked_error(command_class)
                    last_err = HimalayaInvocationError(
                        "himalaya_transient",
                        f"Himalaya transient error: {err_msg}",
                        failure_details or None,
                    )
                    if attempt < attempts - 1:
                        # Issue #103 P5: the configured first transient backoff;
                        # the default 2.0 keeps the unchanged timing, and the
                        # session sleep lets tests inject a fake clock.
                        if stats is not None:
                            stats.sleep_retry_backoff(attempt)
                        else:
                            time.sleep(2.0 * (attempt + 1))
                        continue
                    break
                if stats is not None:
                    stats.record_error(
                        command_class,
                        "himalaya_command_failed",
                        duration_ms=duration_ms,
                        exit_code=res.returncode,
                        item_scoped=item_scoped,
                        envelope_id=envelope_id,
                    )
                raise HimalayaInvocationError(
                    "himalaya_command_failed",
                    f"Himalaya failed: {err_msg}",
                    failure_details or None,
                )
            if stats is not None:
                stats.record_success(
                    command_class=command_class,
                    duration_ms=duration_ms,
                    exit_code=res.returncode,
                )
            return HimalayaOutput(_decode_process_stream(res.stdout), profile_name)
        except subprocess.TimeoutExpired as te:
            duration_ms = int((time.monotonic() - started) * 1000)
            if stats is not None and stats.record_error(
                command_class,
                "himalaya_timeout",
                duration_ms=duration_ms,
                item_scoped=item_scoped,
                envelope_id=envelope_id,
            ):
                raise stats.blocked_error(command_class) from te
            raise HimalayaInvocationError(
                "himalaya_timeout",
                "Himalaya timed out; refusing to retry the mailbox command.",
                {
                    "timeout_seconds": timeout,
                    "duration_ms": duration_ms,
                    "stdout_chars": len(_decode_process_stream(te.stdout)),
                    "stderr_chars": len(_decode_process_stream(te.stderr)),
                },
            ) from te
        except BrokenPipeError as bp:
            duration_ms = int((time.monotonic() - started) * 1000)
            if stats is not None and stats.record_error(
                command_class,
                "himalaya_stdin_failed",
                duration_ms=duration_ms,
                item_scoped=item_scoped,
                envelope_id=envelope_id,
            ):
                raise stats.blocked_error(command_class) from bp
            raise HimalayaInvocationError(
                "himalaya_stdin_failed",
                "Himalaya closed its stdin before the message payload was fully written; "
                "the command was not retried.",
            ) from bp
        except OSError as exc:
            duration_ms = int((time.monotonic() - started) * 1000)
            if stats is not None and stats.record_error(
                command_class,
                "himalaya_launch_failed",
                duration_ms=duration_ms,
                item_scoped=item_scoped,
                envelope_id=envelope_id,
            ):
                raise stats.blocked_error(command_class) from exc
            raise HimalayaInvocationError(
                "himalaya_launch_failed",
                "Himalaya could not be launched or communication failed; "
                "the command outcome may be unknown and it was not retried.",
            ) from exc

    raise last_err or HimalayaInvocationError("himalaya_timeout", "Himalaya timed out.")


def _is_transient_himalaya_error(stderr: str) -> bool:
    """Keep retries narrowly limited to transport failures."""
    message = stderr.lower()
    return any(
        token in message
        for token in (
            "10054",
            "tls stream",
            "cannot connect",
            "broken pipe",
            "connection reset",
            "connection refused",
            "connection aborted",
            "network is unreachable",
            "network unreachable",
            "no route to host",
            "temporary failure in name resolution",
            "name or service not known",
            "nodename nor servname",
            "no address associated with hostname",
            "could not resolve host",
            "getaddrinfo",
            "timed out",
        )
    )


def _decode_v3_json(output: object) -> object:
    try:
        return json.loads(str(output))
    except (TypeError, ValueError):
        raise HimalayaInvocationError(
            himalaya_normalize.JSON_INCOMPATIBLE_REASON,
            "The v3 JSON payload is not valid JSON; no partial result was returned.",
        ) from None


def decode_v3_envelope_output(output: object) -> tuple[list[dict[str, Any]], str | None]:
    """Decode one v3 envelope page into normalized entries plus ``nextPage``."""
    payload = _decode_v3_json(output)
    try:
        return himalaya_normalize.decode_envelope_page(payload)
    except himalaya_normalize.JsonIncompatible as exc:
        raise HimalayaInvocationError(exc.reason_code, str(exc)) from None


def decode_v3_mailbox_output(output: object) -> list[dict[str, Any]]:
    """Decode one ``mailbox list --json`` payload into its mailbox dicts."""
    payload = _decode_v3_json(output)
    try:
        return himalaya_normalize.decode_mailbox_list(payload)
    except himalaya_normalize.JsonIncompatible as exc:
        raise HimalayaInvocationError(exc.reason_code, str(exc)) from None


def collect_v3_envelope_pages(
    entries: list[dict[str, Any]],
    next_page: str | None,
    fetch: Callable[[str], object],
) -> list[dict[str, Any]]:
    """Follow ``nextPage`` through ``--page-token`` with a hard, fail-closed bound.

    A repeated token, an empty token or an exhausted page bound is a bounded
    ``himalaya_*`` refusal; a malformed page is ``himalaya_json_incompatible``.
    The live array form carries no token and returns after the first page.
    """
    collected = list(entries)
    seen: set[str] = set()
    pages = 0
    token = next_page
    while token is not None:
        if not isinstance(token, str) or not token.strip():
            raise HimalayaInvocationError(
                himalaya_normalize.PAGE_TOKEN_INVALID_REASON,
                "The v3 nextPage token is empty or invalid; paging stopped.",
            )
        if token in seen:
            raise HimalayaInvocationError(
                himalaya_normalize.PAGING_NOT_PROGRESSING_REASON,
                "The v3 nextPage token repeated without progress; paging stopped.",
            )
        if pages >= himalaya_normalize.MAX_V3_PAGES:
            raise HimalayaInvocationError(
                himalaya_normalize.PAGING_NOT_PROGRESSING_REASON,
                "The v3 listing exceeded the hard page bound; paging stopped.",
            )
        seen.add(token)
        pages += 1
        collected_page, token = decode_v3_envelope_output(fetch(token))
        collected.extend(collected_page)
    return collected


def fetch_raw_message_eml(
    env_id: str | int,
    folder: str = "INBOX",
    account: str | None = None,
    timeout: int = 30,
    max_bytes: int | None = None,
    *,
    purpose: str = "other",
) -> bytes:
    """Fetch raw RFC 822 .eml bytes via himalaya message export -F.

    The v3 profile reads ``message read --raw --json`` and decodes the base64
    payload instead of an exported file.  ``max_bytes`` (default ``None``:
    unchanged unbounded behaviour) makes the read bounded: an exported file or a
    decoded payload larger than the bound is rejected before it is used
    (``HimalayaInvocationError`` / ``himalaya_export_too_large``).

    ``purpose`` is the additive start label of this export (Issue #103, D1).
    """
    with tempfile.TemporaryDirectory() as tmp_dir:
        dest_eml = Path(tmp_dir) / f"msg_{env_id}.eml"
        args = [
            "message", "export", str(env_id),
            "-f", folder,
            "-F",
            "-d", str(dest_eml),
        ]
        output = run_himalaya(args, account=account, timeout=timeout, max_retries=2, purpose=purpose)
        if profile_of(output) == PROFILE_V3:
            payload = _decode_v3_json(output)
            if not isinstance(payload, dict):
                raise HimalayaInvocationError(
                    himalaya_normalize.JSON_INCOMPATIBLE_REASON,
                    "The v3 raw message payload is not an object.",
                )
            data = payload.get("data")
            size = payload.get("size")
            if not isinstance(data, str) or isinstance(size, bool) or not isinstance(size, int):
                raise HimalayaInvocationError(
                    himalaya_normalize.JSON_INCOMPATIBLE_REASON,
                    "The v3 raw message payload is missing data/size.",
                )
            try:
                decoded = base64.b64decode(data.encode("ascii"), validate=True)
            except (UnicodeEncodeError, ValueError):
                raise HimalayaInvocationError(
                    himalaya_normalize.JSON_INCOMPATIBLE_REASON,
                    "The v3 raw message payload is not valid base64.",
                ) from None
            if len(decoded) != size:
                raise HimalayaInvocationError(
                    himalaya_normalize.JSON_INCOMPATIBLE_REASON,
                    "The v3 raw message size does not match the decoded payload.",
                )
            if max_bytes is not None and len(decoded) > max_bytes:
                raise HimalayaInvocationError(
                    "himalaya_export_too_large",
                    "The exported message exceeds the bounded read-back size; it was not loaded.",
                )
            return decoded
        if max_bytes is not None:
            return _read_export_bounded(dest_eml, max_bytes)
        if not dest_eml.exists() or dest_eml.stat().st_size == 0:
            raise RuntimeError(f"Himalaya export produced empty or missing .eml for envelope {env_id}")
        return dest_eml.read_bytes()


def _read_export_bounded(path: Path, max_bytes: int) -> bytes:
    """Read an exported message through ONE regular-file handle with a hard byte bound.

    The CLI has already written the complete export to disk before this runs, so this bound limits
    what Python loads, not what the backend transfers or stores.  The path must be a regular,
    non-link file; the opened handle must be that same file (device/inode/size), is read in blocks
    up to ``min(size, max_bytes) + 1`` bytes and must end exactly at the announced size (growth or
    truncation while reading is rejected).
    """
    invalid = HimalayaInvocationError(
        "himalaya_export_invalid", "The exported message file is not a stable regular file; it was not loaded."
    )
    try:
        before = os.lstat(path)
    except OSError:
        raise invalid from None
    if (
        not stat.S_ISREG(before.st_mode)
        or stat.S_ISLNK(before.st_mode)
        or getattr(before, "st_file_attributes", 0) & 0x400
    ):
        raise invalid
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError:
        raise invalid from None
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or (info.st_dev, info.st_ino, info.st_size) != (
            before.st_dev, before.st_ino, before.st_size
        ):
            raise invalid
        if info.st_size == 0:
            raise invalid
        if info.st_size > max_bytes:
            raise HimalayaInvocationError(
                "himalaya_export_too_large",
                "The exported message exceeds the bounded read-back size; it was not loaded.",
            )
        chunks: list[bytes] = []
        total = 0
        remaining = info.st_size + 1  # one extra byte detects growth
        while remaining > 0:
            block = os.read(descriptor, min(1024 * 1024, remaining))
            if not block:
                break
            chunks.append(block)
            total += len(block)
            remaining -= len(block)
        if total != info.st_size:
            raise invalid
        return b"".join(chunks)
    except OSError:
        raise invalid from None
    finally:
        os.close(descriptor)


#: Newest-first envelope window requested for an unambiguous Message-ID lookup.
UNIQUE_LOOKUP_ENVELOPE_WINDOW = 500

#: Hard budget of header reads (= envelopes that can be proven) for one unambiguous lookup.
UNIQUE_LOOKUP_MAX_HEADER_READS = 50


def find_envelopes_by_message_id(
    folder: str,
    message_id: str,
    *,
    subject: str = "",
    account: str | None = None,
    reserve: int = 0,
) -> tuple[list[str], bool]:
    """Return ``(matching envelope ids, complete)`` for one Message-ID in ``folder``.

    Unlike ``verify_in_target_folder`` this does not stop at the first match and has no subject
    shortcut: the ``Message-ID`` header of EVERY envelope of the listing is read.  The budget is
    ``UNIQUE_LOOKUP_MAX_HEADER_READS`` header reads; ``complete`` is ``True`` only when the whole
    folder fits into it (``len(envelopes) + reserve <= budget`` -- ``reserve`` leaves room for a
    message that is about to be appended), every header read succeeded and no envelope carries more
    than one ``Message-ID`` header.  Otherwise ``complete`` is ``False`` (the first budget-many
    newest envelopes are still examined and reported) and a caller needing uniqueness must fail
    closed.  ``subject`` is accepted for call compatibility and deliberately not used.
    """
    wanted = normalize_message_id(message_id)
    out = run_himalaya(
        ["-o", "json", "envelope", "list", "-f", folder, "-s", str(UNIQUE_LOOKUP_ENVELOPE_WINDOW)],
        account=account, timeout=30, max_retries=2,
    )
    if profile_of(out) == PROFILE_V3:
        # v3 publishes the Message-ID as the listing's ``messageId``: no
        # per-envelope header reads are needed (or started) here.
        entries, next_page = decode_v3_envelope_output(out)
        complete = (
            next_page is None
            and len(entries) < UNIQUE_LOOKUP_ENVELOPE_WINDOW
            and len(entries) + reserve <= UNIQUE_LOOKUP_MAX_HEADER_READS
        )
        matches = [
            str(entry["id"])
            for entry in entries[:UNIQUE_LOOKUP_MAX_HEADER_READS]
            if entry.get("message_id") == wanted
        ]
        return matches, complete
    if "[" in out:
        out = out[out.find("["):]
    envelopes = json.loads(out)
    complete = len(envelopes) < UNIQUE_LOOKUP_ENVELOPE_WINDOW and len(envelopes) + reserve <= UNIQUE_LOOKUP_MAX_HEADER_READS
    matches: list[str] = []
    for env in envelopes[:UNIQUE_LOOKUP_MAX_HEADER_READS]:
        candidate = str(env["id"])
        try:
            header = run_himalaya(
                ["message", "read", "--preview", "-H", "Message-Id", "-f", folder, candidate],
                account=account, timeout=10, max_retries=1,
            )
        except Exception as exc:  # noqa: BLE001 - an unreadable envelope makes absence unprovable
            raise_if_transport_blocked(exc)
            complete = False
            continue
        values = [
            line.split(":", 1)[1] for line in header.splitlines() if line.lower().startswith("message-id:")
        ]
        if len(values) > 1:
            complete = False  # ambiguous identity: never a clean single hit
        if any(normalize_message_id(value) == wanted for value in values):
            matches.append(candidate)
    return matches, complete


def raise_if_transport_blocked(error: BaseException) -> None:
    """Re-raise the structured F6 abort instead of degrading it to fallback data."""
    if isinstance(error, HimalayaInvocationError) and error.reason_code == TRANSPORT_BLOCKED_REASON:
        raise error


def _read_failure_reason(error: object) -> str | None:
    """Return the bounded item-level reason for one failed read."""
    if error is None:
        return None
    if isinstance(error, HimalayaInvocationError):
        return "timeout" if error.reason_code == "himalaya_timeout" else "read_failed"
    if isinstance(error, (TimeoutError, subprocess.TimeoutExpired)):
        return "timeout"
    return "read_failed"


#: Content charsets accepted for a declared ``Content-Type`` parameter.
_EML_ACCEPTED_CHARSETS = frozenset({"us-ascii", "ascii", "utf-8"})

#: Transfer encodings whose decoded text equals the raw body bytes.
_EML_ACCEPTED_TRANSFER_ENCODINGS = frozenset({"", "7bit", "8bit"})

#: Headers that must occur at most once before the text parity can be claimed.
_EML_SINGLETON_HEADERS = (
    "message-id",
    "subject",
    "from",
    "to",
    "cc",
    "date",
    "in-reply-to",
    "references",
    "content-type",
    "content-transfer-encoding",
    "content-disposition",
    "mime-version",
)


def _is_well_formed_message_id(raw: str) -> bool:
    """Return whether ``raw`` has one legal raw bracket form and a sane msg-id body.

    The raw value may carry exactly one surrounding ``<...>`` pair or no
    brackets at all.  One final raw-form check covers both branches, so nested,
    multiple or unbalanced brackets are rejected before normalization.
    """
    if not isinstance(raw, str):
        return False
    value = raw.strip()
    if not value:
        return False
    if "<" in value or ">" in value:
        if not (value.startswith("<") and value.endswith(">")):
            return False
        value = value[1:-1]
    if "<" in value or ">" in value:
        return False
    normalized = normalize_message_id(value)
    if not normalized or normalized.count("@") != 1:
        return False
    local, domain = normalized.split("@", 1)
    if not local or not domain or domain == "generated":
        return False
    return not any(character.isspace() or ord(character) < 32 for character in normalized)


def _fallback_display_fields(
    fallback_envelope: dict[str, Any] | None,
) -> tuple[str, str, str, str]:
    """Return ``(subject, from, to, date)`` exactly as the CLI read path shows them."""
    fb_from = ""
    fb_subj = ""
    fb_to = ""
    fb_date = ""
    if fallback_envelope:
        fb_subj = fallback_envelope.get("subject", "")
        f_info = fallback_envelope.get("from", {})
        fb_from = f"{f_info.get('name', '')} <{f_info.get('addr', '')}>".strip() if isinstance(f_info, dict) else str(f_info)
        t_info = fallback_envelope.get("to", {})
        fb_to = f"{t_info.get('name', '')} <{t_info.get('addr', '')}>".strip() if isinstance(t_info, dict) else str(t_info)
        fb_date = fallback_envelope.get("date", "")
    return fb_subj, fb_from, fb_to, fb_date


def _collapse_header_whitespace(value: str) -> str:
    """Collapse every whitespace run to one space and strip (CLI display form)."""
    return " ".join(value.split())


#: One RFC 2047 encoded word: charset, encoding (B/Q) and payload.
_ENCODED_WORD_PATTERN = re.compile(r"=\?([^?\s]+)\?([bBqQ])\?([^?]*)\?=")

#: The base64 alphabet with optional padding.
_BASE64_PAYLOAD = re.compile(r"[A-Za-z0-9+/]*={0,2}")

#: Closed reference form for a P1 ``Date`` header.
_P1_DATE_PATTERN = re.compile(
    r"^(Mon|Tue|Wed|Thu|Fri|Sat|Sun), "
    r"(0[1-9]|[12][0-9]|3[01]) "
    r"(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec) "
    r"([0-9]{4}) "
    r"([01][0-9]|2[0-3]):([0-5][0-9]):([0-5][0-9]) "
    r"([+-])([01][0-9]|2[0-3])([0-5][0-9])$"
)

_P1_WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")

_P1_MONTHS = {
    "Jan": 1,
    "Feb": 2,
    "Mar": 3,
    "Apr": 4,
    "May": 5,
    "Jun": 6,
    "Jul": 7,
    "Aug": 8,
    "Sep": 9,
    "Oct": 10,
    "Nov": 11,
    "Dec": 12,
}


def _is_p1_reference_date(value: str) -> bool:
    """Return whether ``value`` is exactly the closed P1 ``Date`` reference form.

    The anchored form is ``Thu, 01 Jan 2026 00:00:00 +0000``: weekday from the
    closed list, two-digit day, closed month, four-digit year, ``HH:MM:SS`` and
    a numeric ``[+-]HHMM`` zone.  The weekday must match the date (deterministic
    arithmetic, locale-independent); every other form — including an invalid
    calendar date, ``Foo,``, a one-digit day or ``GMT`` — is not proven.
    """
    match = _P1_DATE_PATTERN.fullmatch(value)
    if match is None:
        return False
    weekday, day, month, year = match.group(1, 2, 3, 4)
    try:
        expected = _P1_WEEKDAYS[
            datetime.date(int(year), _P1_MONTHS[month], int(day)).weekday()
        ]
    except ValueError:
        return False
    return weekday == expected


def _encoded_words_are_well_formed(value: str) -> bool:
    """Return whether every ``=?`` in a header value is a well-formed encoded word.

    The stdlib decoder tolerates broken payloads (it may decode them to an empty
    string), so the strict shape is checked here before decoding: a base64
    payload must use the base64 alphabet with a valid length, a Q payload must
    not carry a broken ``=XX`` escape, and no literal ``=?`` may remain outside
    a matched word.
    """
    if "=?" not in value:
        return True
    matches = list(_ENCODED_WORD_PATTERN.finditer(value))
    if not matches:
        return False
    for match in matches:
        _charset, encoding, payload = match.groups()
        if encoding.lower() == "b":
            if not payload or len(payload) % 4 or not _BASE64_PAYLOAD.fullmatch(payload):
                return False
        else:
            if re.search(r"=(?![0-9A-Fa-f]{2})", payload):
                return False
    starts = {match.start() for match in matches}
    index = value.find("=?")
    while index != -1:
        if index not in starts:
            return False
        index = value.find("=?", index + 1)
    return True


def _decode_mime_header(value: str) -> str | None:
    """Decode RFC 2047 encoded words strictly; ``None`` when not provable.

    Every encoded word's charset must be known and the bytes must decode
    strictly.  A malformed word or a leftover ``=?`` is ambiguous and also
    yields ``None``.
    """
    if not isinstance(value, str):
        return None
    if not _encoded_words_are_well_formed(value):
        return None
    try:
        chunks = email.header.decode_header(value)
    except Exception:  # noqa: BLE001 - a malformed encoding never proves parity
        return None
    decoded: list[str] = []
    for chunk, charset in chunks:
        if isinstance(chunk, bytes):
            try:
                decoded.append(chunk.decode(charset or "ascii", errors="strict"))
            except (LookupError, UnicodeDecodeError):
                return None
        else:
            decoded.append(chunk)
    result = "".join(decoded)
    if "=?" in result:
        # A malformed encoded word stayed literal; never claim parity for it.
        return None
    return result


def _split_address_elements(value: str) -> list[str] | None:
    """Split a comma list on top-level commas, quote- and bracket-aware.

    Returns ``None`` for group/comment syntax (``:``/``;``/``(``/``)`` outside
    quotes), unbalanced quotes or angle brackets, a comma inside an angle block
    (``Name <a@x,b@y>`` is not a separator), and an empty element (leading,
    trailing or doubled comma).  Quoted display names and quoted special
    characters stay inside one element.
    """
    elements: list[str] = []
    current: list[str] = []
    in_quotes = False
    in_angle = False
    index = 0
    while index < len(value):
        character = value[index]
        if in_quotes:
            current.append(character)
            if character == "\\" and index + 1 < len(value):
                current.append(value[index + 1])
                index += 2
                continue
            if character == '"':
                in_quotes = False
        elif character == '"':
            in_quotes = True
            current.append(character)
        elif character == "<":
            if in_angle:
                return None
            in_angle = True
            current.append(character)
        elif character == ">":
            if not in_angle:
                return None
            in_angle = False
            current.append(character)
        elif character == ",":
            if in_angle:
                # Only a top-level comma separates addresses.
                return None
            elements.append("".join(current))
            current = []
        elif character in ":;()":
            # Group and comment syntax is not proven for the CLI anchor.
            return None
        else:
            current.append(character)
        index += 1
    if in_quotes or in_angle:
        return None
    elements.append("".join(current))
    if any(not element.strip() for element in elements):
        return None
    return elements


def _canonical_address_header(value: str) -> str | None:
    """Return the CLI display form of one address header, or ``None``.

    The canonical form is documented as: RFC 2047 decoded, whitespace runs
    collapsed, rendered per parsed address — ``Name <addr>`` when a display name
    exists, and the bare ``addr`` (no angle brackets) when it does not.  Group
    or comment syntax, an empty list element, a silently dropped entry, a
    case-insensitive duplicate address, a quoted local part (``"a,b"@x.test``)
    and any unparsable or ambiguous input return ``None``.  Every parsed address
    must carry exactly one ``@`` and no whitespace.
    """
    decoded = _decode_mime_header(value)
    if decoded is None:
        return None
    collapsed = _collapse_header_whitespace(decoded)
    if not collapsed:
        return ""
    elements = _split_address_elements(collapsed)
    if elements is None:
        return None
    try:
        parsed = email.utils.getaddresses([collapsed])
    except Exception:  # noqa: BLE001 - an unparsable list is never canonical
        return None
    if len(parsed) != len(elements):
        return None
    rendered: list[str] = []
    seen_addresses: set[str] = set()
    for name, address in parsed:
        if not address:
            return None
        if address.count("@") != 1 or any(character.isspace() for character in address):
            return None
        if '"' in address:
            # A quoted local part keeps its quotes in the parsed address; the
            # CLI rendering for it is unproven; never render an ambiguous list.
            return None
        folded = address.casefold()
        if folded in seen_addresses:
            # Unproven dedupe semantics; never render an ambiguous list.
            return None
        seen_addresses.add(folded)
        display_name = _collapse_header_whitespace(name).replace('"', "")
        rendered.append(f"{display_name} <{address}>" if display_name else address)
    return ", ".join(rendered)


def _canonical_references_header(value: str) -> str | None:
    """Return the CLI display form of a ``References`` header, or ``None``.

    The CLI keeps exactly one bracketed ID unchanged (the recorded 43-character
    form) and renders several whitespace-separated IDs without any angle
    brackets; a single unbracketed ID is already in CLI form and stays as is.
    Adjacent ``><`` (IDs glued together), unbalanced or nested brackets, comma
    lists and undecodable encoded words are not proven and return ``None``;
    ``In-Reply-To`` is deliberately not handled here and keeps its single-ID
    brackets.
    """
    decoded = _decode_mime_header(value)
    if decoded is None:
        return None
    stripped = decoded.strip()
    if not stripped:
        return ""
    if "," in stripped:
        return None
    depth = 0
    previous = ""
    for character in stripped:
        if character == "<":
            if depth:
                return None
            if previous == ">":
                return None
            depth = 1
        elif character == ">":
            if not depth:
                return None
            depth = 0
        previous = character
    if depth:
        return None
    bare = stripped.replace("<", "").replace(">", "").strip()
    single_bracketed_id = (
        len(stripped) > 2
        and stripped.startswith("<")
        and stripped.endswith(">")
        and stripped.count("<") == 1
        and stripped.count(">") == 1
        and not any(character.isspace() for character in stripped[1:-1])
    )
    return stripped if single_bracketed_id else bare


def _part_is_attachment(part: "email.message.Message") -> bool:
    """Return whether a MIME part is an attachment for the canonical inventory.

    The inventory classifies a named part (``Content-Type name=`` or
    ``Content-Disposition filename=``) as an attachment, as well as any
    disposition other than absent/``inline``.  Such a part is never the body.
    """
    if part.get_filename():
        return True
    return part.get_content_disposition() not in (None, "inline")


def _select_plain_text_part(
    message: "email.message.Message",
) -> "email.message.Message | None":
    """Return the one unambiguous non-attachment ``text/plain`` body part.

    A non-multipart message must itself be ``text/plain``.  ``multipart/
    alternative`` needs exactly one direct ``text/plain`` child;
    ``multipart/mixed`` and ``multipart/related`` need exactly one
    non-attachment ``text/plain`` part across the tree.  Everything else
    (HTML-only, no candidate, several candidates, attachment-only) returns
    ``None``.
    """
    if not message.is_multipart():
        if message.get_content_type() != "text/plain":
            return None
        if _part_is_attachment(message):
            return None
        return message
    subtype = message.get_content_subtype()
    payload = message.get_payload()
    if not isinstance(payload, list):
        return None
    if subtype == "alternative":
        direct = [
            part
            for part in payload
            if isinstance(part, email.message.Message)
            and not part.is_multipart()
            and part.get_content_type() == "text/plain"
            and not _part_is_attachment(part)
        ]
        return direct[0] if len(direct) == 1 else None
    if subtype in ("mixed", "related"):
        candidates: list[email.message.Message] = []

        def _walk(container: "email.message.Message") -> None:
            children = container.get_payload()
            if not isinstance(children, list):
                return
            for child in children:
                if not isinstance(child, email.message.Message):
                    continue
                if child.is_multipart():
                    _walk(child)
                    continue
                if child.get_content_type() == "text/plain" and not _part_is_attachment(child):
                    candidates.append(child)

        _walk(message)
        return candidates[0] if len(candidates) == 1 else None
    return None


#: Charsets accepted for a P1 text part (closed list).  P2 keeps its own,
#: narrower ``_EML_ACCEPTED_CHARSETS``; this list never widens it.
_P1_TEXT_CHARSETS = frozenset(
    {
        "us-ascii",
        "ascii",
        "utf-8",
        "iso-8859-1",
        "latin-1",
        "windows-1252",
        "cp1252",
        "iso-8859-15",
    }
)


def _decode_part_text(part: "email.message.Message") -> str | None:
    """Decode one text part strictly with its declared charset, or ``None``.

    Without a declared charset only pure US-ASCII is provable.  The charset must
    be on the closed P1 list; a duplicated ``charset`` parameter or a
    ``format=flowed``/``delsp`` body is ambiguous (the raw text is not the
    rendered CLI text) and returns ``None``.
    """
    try:
        params = part.get_params(header="content-type", failobj=[]) or []
        names = [str(name).strip().lower() for name, _value in params]
        if names.count("charset") > 1:
            return None
        if "format" in names or "delsp" in names:
            return None
        payload = part.get_payload(decode=True)
    except Exception:  # noqa: BLE001 - a broken transfer encoding is not provable
        return None
    if not isinstance(payload, bytes):
        return None
    charset = part.get_content_charset()
    try:
        if charset is None:
            return payload.decode("ascii", errors="strict")
        normalized = str(charset).strip().lower()
        if normalized not in _P1_TEXT_CHARSETS:
            return None
        return payload.decode(normalized, errors="strict")
    except (LookupError, UnicodeDecodeError):
        return None


def _p1_attachment_line(
    parsed: "email.message.Message",
    attachments: list[dict[str, Any]],
    attachment_status: str,
    attachment_error: str | None,
    downloads_dir: str | None,
) -> str | None:
    """Return the proven CLI attachment preview line, or ``None``.

    Only the closed single-file class qualifies.  Every missing proof returns
    ``None`` so the caller keeps the unchanged CLI read.
    """
    if attachment_status != "available" or attachment_error is not None:
        return None
    if len(attachments) != 1:
        return None
    if not downloads_dir:
        return None
    entry = attachments[0]
    if not isinstance(entry, dict):
        return None
    if entry.get("is_inline") is not False:
        return None
    mime_type = entry.get("mime_type")
    if not isinstance(mime_type, str) or not mime_type:
        return None
    normalized_mime = mime_type.lower()
    if (
        normalized_mime.startswith("image/")
        or normalized_mime == "application/octet-stream"
    ):
        return None
    filename = entry.get("filename")
    original_filename = entry.get("original_filename")
    if not isinstance(filename, str) or not filename:
        return None
    if filename != original_filename:
        return None
    if "/" in filename or "\\" in filename or filename in {".", ".."}:
        return None
    if any(
        ord(character) < 32
        or ord(character) == 0x7F
        or character in {"\x85", "\u2028", "\u2029"}
        for character in filename
    ):
        return None
    locator = entry.get("part_locator")
    if not isinstance(locator, str) or not locator:
        return None
    part = find_mime_part_by_locator(parsed, locator)
    if part is None:
        return None
    # The inventory normalizes a missing disposition to "attachment"; only the
    # raw header is proof that the CLI renders an attachment line.
    content_type_header = part.get("Content-Type")
    if content_type_header is None or not str(content_type_header).strip():
        return None
    if part.get_content_disposition() != "attachment":
        return None
    if part.get_content_type().lower() != normalized_mime:
        return None
    if part.get_filename() != filename:
        return None
    separator = "" if downloads_dir.endswith(("\\", "/")) else "\\"
    return (
        f'<#part type={mime_type} filename="{downloads_dir}{separator}{filename}">'
        "<#/part>"
    )


def _details_from_eml(
    env_id: str | int,
    folder: str,
    raw_eml: bytes,
    *,
    preview_lines: int,
    fallback_envelope: dict[str, Any] | None,
    attachments: list[dict[str, Any]],
    attachment_status: str,
    attachment_error: str | None,
    downloads_dir: str | None = None,
) -> dict[str, Any] | None:
    """Return the details dict only for the extended, closed EML input class.

    The function is pure: no process, file, network or hidden state.  The class
    covers real mail shapes (Issue #103 P1 extension): RFC-5322 unfolding,
    RFC 2047 decoding for Subject/From/To/Cc/References (Date/In-Reply-To are
    unfolded and stripped only, never decoded), canonical address display
    values (a display-name-less address is rendered without ``<>``),
    ``References`` keeping exactly one bracketed ID unchanged and rendering
    several IDs without ``<>`` (``In-Reply-To`` keeps its brackets), one
    unambiguous non-attachment ``text/plain`` body part (single-part,
    ``multipart/alternative``, ``multipart/mixed``/``multipart/related``), the
    part's declared charset from the closed P1 list
    (``us-ascii``/``ascii``/``utf-8``/``iso-8859-1``/``latin-1``/
    ``windows-1252``/``cp1252``/``iso-8859-15``), the CLI stdout preview
    semantics (a body that ends with a line break gains the process-terminator
    empty line) and a preview truncated to ``preview_lines``.  A ``Date`` is
    only accepted in the anchored reference form ``Thu, 01 Jan 2026 00:00:00
    +0000`` with a weekday consistent with the date; every other form is
    excluded.  ``References`` keeps a single bracketed ID in its recorded
    43-character CLI form and renders several whitespace-separated IDs without
    angle brackets (adjacent brackets, unbalanced brackets and commas are
    excluded); ``In-Reply-To`` keeps its single-ID brackets.

    Items with a canonical inventory attachment are accepted only for the
    closed single-file class when ``downloads_dir`` is proven: exactly one
    entry, ``attachment_status == "available"`` and ``attachment_error is
    None``; its ``part_locator`` resolves to a leaf part whose raw headers carry
    an explicit ``Content-Type`` and an actual ``Content-Disposition:
    attachment``; ``is_inline is False``; the MIME type is neither ``image/*``
    nor ``application/octet-stream``; ``filename`` is non-empty, has no path
    component and equals ``original_filename``.  The CLI attachment line is
    then appended after the body content lines and before the
    process-terminator empty line, prior to the ``preview_lines`` cut.  Every
    other attachment shape returns ``None`` and keeps the unchanged CLI read.
    Items without attachments keep the previous behaviour.
    Ambiguous input — duplicate relevant headers, raw 8-bit header bytes,
    malformed encoded words, undecodable charsets, HTML-only, several text
    candidates, a named/attachment body part, defective Message-ID, mixed line
    endings, bare CR/NUL — returns ``None`` and the caller keeps its existing
    CLI read without a new export.

    The comparison anchor is the CLI display form: unfolded with exactly one
    space, values stripped, header keys lowercase, RFC 2047 decoded and address
    double quotes removed.  It performs **no** independent email-policy defect
    scan and is only called after the canonical inventory check
    (``inspect_mime_tree``) already succeeded; the inventory passes through
    unchanged.  ``has_attachment is False`` items never export and therefore
    never reach this function (documented structural limit).
    """
    try:
        if not isinstance(raw_eml, bytes) or not raw_eml:
            return None
        if b"\x00" in raw_eml:
            return None
        if raw_eml.replace(b"\r\n", b"").find(b"\r") >= 0:
            return None
        crlf_index = raw_eml.find(b"\r\n\r\n")
        lf_index = raw_eml.find(b"\n\n")
        if crlf_index == -1 and lf_index == -1:
            return None
        if crlf_index == -1:
            separator_index = lf_index
        elif lf_index == -1:
            separator_index = crlf_index
        else:
            # The earlier separator ends the header block; the later one is body.
            separator_index = min(crlf_index, lf_index)
        head_bytes = raw_eml[:separator_index]
        if not head_bytes:
            return None
        # Raw 8-bit header bytes outside an encoded word are ambiguous (Mojibake).
        if any(byte > 126 for byte in head_bytes):
            return None
        has_crlf = b"\r\n" in head_bytes
        has_bare_lf = b"\n" in head_bytes.replace(b"\r\n", b"")
        if has_crlf and has_bare_lf:
            return None
        header_block = head_bytes.decode("ascii").replace("\r\n", "\n")
        if not header_block.strip():
            return None

        headers: dict[str, list[str]] = {}
        current_key: str | None = None
        for line in header_block.split("\n"):
            if not line:
                continue
            if line[0] in " \t":
                if current_key is None:
                    return None
                headers[current_key][-1] = (
                    headers[current_key][-1] + " " + line.strip()
                ).strip()
                continue
            if ":" not in line:
                return None
            name, _, value = line.partition(":")
            key = name.strip().lower()
            if not key:
                return None
            if any(ord(character) < 32 and character != "\t" for character in value):
                return None
            headers.setdefault(key, []).append(value.strip())
            current_key = key

        for key in _EML_SINGLETON_HEADERS:
            if len(headers.get(key, ())) > 1:
                return None

        content_type_values = headers.get("content-type")
        if content_type_values is not None and not content_type_values[0]:
            return None
        mime_versions = headers.get("mime-version")
        if mime_versions is not None and mime_versions[0] != "1.0":
            return None

        message_ids = headers.get("message-id", [])
        if len(message_ids) != 1 or not _is_well_formed_message_id(message_ids[0]):
            return None
        raw_message_id = message_ids[0]
        normalized_message_id = normalize_message_id(raw_message_id)

        parsed = email.message_from_bytes(raw_eml, policy=email.policy.default)
        attachment_line: str | None = None
        if attachments or attachment_status != "none":
            attachment_line = _p1_attachment_line(
                parsed,
                attachments,
                attachment_status,
                attachment_error,
                downloads_dir,
            )
            if attachment_line is None:
                return None
        body_part = _select_plain_text_part(parsed)
        if body_part is None:
            return None
        body_text = _decode_part_text(body_part)
        if body_text is None:
            return None
        if body_text.endswith("\n"):
            # The CLI preview is the stdout body plus the process terminator
            # line: a body that ends with a line break therefore gains one empty
            # final line before the preview selection.  A trailing bare CR alone
            # is not a line break here.
            body_text = body_text + "\n"
        body_lines = body_text.splitlines()
        if not any(line.strip() for line in body_lines):
            return None
        if attachment_line is not None:
            insertion_index = len(body_lines)
            if body_lines and body_lines[-1] == "":
                # Keep the process-terminator empty line after the attachment.
                insertion_index -= 1
            body_lines.insert(insertion_index, attachment_line)
        preview = "\n".join(body_lines[:preview_lines])

        fb_subj, fb_from, fb_to, fb_date = _fallback_display_fields(fallback_envelope)

        def _display(key: str, *, addresses: bool = False) -> str | None:
            values = headers.get(key)
            if not values:
                return ""
            if addresses:
                return _canonical_address_header(values[0])
            decoded = _decode_mime_header(values[0])
            if decoded is None:
                return None
            # Subject/References keep their internal whitespace; only the
            # unfolding space is added and the value is stripped (CLI anchor).
            return decoded.strip()

        def _plain(key: str) -> str:
            """Unfold and strip only; Date/In-Reply-To are not RFC 2047 decoded."""
            values = headers.get(key)
            return values[0].strip() if values else ""

        subject = _display("subject")
        from_value = _display("from", addresses=True)
        to_value = _display("to", addresses=True)
        cc_value = _display("cc", addresses=True)
        date_value = _plain("date")
        raw_references = headers.get("references", [""])[0]
        references = _canonical_references_header(raw_references)
        in_reply_to = _plain("in-reply-to")
        if None in (subject, from_value, to_value, cc_value, references):
            return None
        # Date/In-Reply-To are not RFC 2047 decoded; an encoded word there would
        # be shown raw, which the CLI anchor does not prove.
        if "=?" in date_value or "=?" in in_reply_to:
            return None
        # Only the closed reference form is proven as the CLI passthrough; any
        # other Date value falls back and is never rendered.
        if date_value and not _is_p1_reference_date(date_value):
            return None

        return {
            "envelope_id": str(env_id),
            "folder": folder,
            "message_id": normalized_message_id,
            "raw_message_id": raw_message_id,
            "subject": subject or fb_subj,
            "from": from_value or fb_from,
            "to": to_value or fb_to,
            "cc": cc_value,
            "headers_complete": True,
            "date": date_value or fb_date,
            "in_reply_to": in_reply_to,
            "references": references,
            "preview": preview,
            "attachments": attachments,
            "attachment_status": attachment_status,
            "attachment_error": attachment_error,
            "error": None,
            "read_error_reason": None,
            "read_level": "preview",
        }
    except Exception:  # noqa: BLE001 - unproven parity is always the safe answer
        return None


def _details_from_raw_eml(
    env_id: str | int,
    folder: str,
    eml_b: bytes,
    *,
    preview_lines: int,
    full_body: bool,
    fallback_envelope: dict[str, Any] | None,
    attachments_list: list[dict[str, Any]],
    attachment_status: str,
    attachment_error: str | None,
) -> dict[str, Any]:
    """Derive one result record from already validated RFC-822 bytes.

    This is the single ``raw_eml`` derivation: the caller supplies bytes whose
    inventory it already validated, and the display fields are read from the
    parsed message.  It starts no process and reads no file.  Exceptions stay
    with the caller so its existing containment (fall through to the CLI read)
    is unchanged.
    """
    msg_obj = email.message_from_bytes(eml_b, policy=email.policy.default)
    raw_mid = str(msg_obj.get("Message-ID", "") or "")
    norm_mid = normalize_message_id(raw_mid) if raw_mid else ""
    subj = str(msg_obj.get("Subject", "") or "")
    from_hdr = str(msg_obj.get("From", "") or "")
    to_hdr = str(msg_obj.get("To", "") or "")
    cc_hdr = str(msg_obj.get("Cc", "") or "")
    date_hdr = str(msg_obj.get("Date", "") or "")
    in_reply_to = str(msg_obj.get("In-Reply-To", "") or "")
    references = str(msg_obj.get("References", "") or "")

    body_text = ""
    try:
        body_part = msg_obj.get_body(preferencelist=("plain", "html"))
        if body_part:
            content = body_part.get_content()
            body_text = content if isinstance(content, str) else str(content or "")
    except Exception:
        pass
    if not body_text:
        for part in msg_obj.walk():
            if not part.is_multipart() and part.get_content_type().lower() in ("text/plain", "text/html"):
                payload = part.get_payload(decode=True)
                if isinstance(payload, bytes):
                    body_text = payload.decode(part.get_content_charset() or "utf-8", errors="replace")
                    break

    body_lines = body_text.splitlines()
    preview = "\n".join(body_lines if full_body else body_lines[:preview_lines])

    return {
        "envelope_id": str(env_id),
        "folder": folder,
        "message_id": norm_mid,
        "raw_message_id": raw_mid,
        "subject": subj or (fallback_envelope.get("subject", "") if fallback_envelope else ""),
        "from": from_hdr,
        "to": to_hdr,
        "cc": cc_hdr,
        "headers_complete": True,
        "date": date_hdr,
        "in_reply_to": in_reply_to,
        "references": references,
        "preview": preview,
        "attachments": attachments_list,
        "attachment_status": attachment_status,
        "attachment_error": attachment_error,
        "error": None,
        "read_error_reason": None,
        "read_level": "full_body" if full_body else "preview",
    }


def _full_body_from_own_export(
    env_id: str | int,
    folder: str,
    eml_data: bytes,
    *,
    preview_lines: int,
    fallback_envelope: dict[str, Any] | None,
    attachments_list: list[dict[str, Any]],
    attachment_status: str,
    attachment_error: str | None,
) -> dict[str, Any] | None:
    """Return the full-body record from the item's own export, or ``None``.

    Issue #103 P2 (gated): the completed inventory export may replace the CLI
    read only when its structure belongs to the modelled full-text class and its
    own Message-ID proves the expected item.  Every doubt — missing/deviating
    identity, parity rejection, derivation error — returns ``None`` so the
    caller keeps the unchanged CLI read and the successful inventory.
    """
    try:
        expected = normalize_message_id(
            str((fallback_envelope or {}).get("message_id") or "")
        )
        if not expected or not eml_full_text_parity(eml_data):
            return None
        if eml_own_message_id(eml_data) != expected:
            return None
        return _details_from_raw_eml(
            env_id,
            folder,
            eml_data,
            preview_lines=preview_lines,
            full_body=True,
            fallback_envelope=fallback_envelope,
            attachments_list=attachments_list,
            attachment_status=attachment_status,
            attachment_error=attachment_error,
        )
    except Exception:  # noqa: BLE001 - an unproven reuse keeps the CLI read
        return None


def eml_own_message_id(eml: bytes) -> str:
    """Return the EML's own normalized Message-ID, or ``""`` when not provable.

    A missing, duplicated or unparsable Message-ID proves nothing and therefore
    never binds a source or an inventory merge.
    """
    try:
        message = email.message_from_bytes(eml, policy=email.policy.default)
        values = message.get_all("Message-ID") or []
        if len(values) != 1:
            return ""
        return normalize_message_id(str(values[0]))
    except Exception:  # noqa: BLE001 - an unprovable identity never binds
        return ""


def complete_scan_inventory(
    details: dict[str, Any],
    env_id: str | int,
    folder: str = "INBOX",
    account: str | None = None,
    *,
    fallback_envelope: dict[str, Any] | None = None,
    purpose: str = "scan_preview",
    raw_eml_sink: Callable[[bytes], None] | None = None,
) -> tuple[bytes | None, bool]:
    """Complete a preview-only result with exactly one inventory export.

    Returns ``(exported_eml, drift)``.  ``drift`` is ``True`` when the exported
    message's own Message-ID differs from the preview's (or is unprovable):
    nothing is merged, the sink is not called and the caller must take the
    regular export+read path for this item.  Without drift the canonical
    inventory (same validation as the regular read) is merged into ``details``
    in place and the export is returned for the caller's source binding.  This
    function performs **one** export and **no** read; a failed export leaves the
    preview valid and marks the inventory unavailable, exactly like the regular
    path.  ``fallback_envelope.has_attachment is False`` needs no export at all.
    """
    _validate_purpose(purpose)
    if raw_eml_sink is not None and not callable(raw_eml_sink):
        raise ValueError("raw_eml_sink must be callable or None")
    if fallback_envelope and fallback_envelope.get("has_attachment") is False:
        details["attachments"] = []
        details["attachment_status"] = "none"
        details["attachment_error"] = None
        return None, False
    try:
        eml_data = fetch_raw_message_eml(
            env_id,
            folder=folder,
            account=account,
            purpose=_inventory_purpose(purpose),
        )
    except Exception as exc:  # noqa: BLE001 - one bounded item-local export failure
        raise_if_transport_blocked(exc)
        details["attachments"] = []
        details["attachment_status"] = "attachment_inventory_unavailable"
        details["attachment_error"] = f"Failed to export or inspect message MIME: {exc}"
        return None, False
    expected = str(details.get("message_id") or "")
    own = eml_own_message_id(eml_data)
    if not own or own != expected:
        return None, True
    if raw_eml_sink is not None:
        try:
            raw_eml_sink(eml_data)
        except Exception:  # noqa: BLE001 - a capture sink never changes the merge
            pass
    try:
        attachments_list = inspect_mime_tree(eml_data)
        inv_reasons = []
        for candidate in attachments_list:
            valid, reason = validate_attachment_candidate_metadata(candidate)
            if not valid:
                inv_reasons.append(reason)
        if inv_reasons:
            details["attachments"] = []
            details["attachment_status"] = "attachment_inventory_unavailable"
            details["attachment_error"] = (
                f"Incomplete attachment inventory in MIME: {'; '.join(inv_reasons)}"
            )
        else:
            details["attachments"] = attachments_list
            details["attachment_status"] = "available" if attachments_list else "none"
            details["attachment_error"] = None
    except Exception as exc:  # noqa: BLE001 - one bounded item-local inventory failure
        raise_if_transport_blocked(exc)
        details["attachments"] = []
        details["attachment_status"] = "attachment_inventory_unavailable"
        details["attachment_error"] = f"Failed to export or inspect message MIME: {exc}"
    return eml_data, False


def eml_full_text_parity(raw_eml: bytes) -> bool:
    """Return whether the EML belongs to the closed, modelled full-text class.

    The class is modelled from the existing ``raw_eml`` derivation and the CLI
    read shape; it is **not live-proven**.  It may only be used behind the
    draft's explicit full-text gate (``allow_scan_source_full_text``, default
    ``False``).

    Issue #103 P2: a held scan source may supply the full text only when its
    structure proves that the existing ``raw_eml`` derivation shows the same
    fields as the regular CLI read for this input class.  The class is: one
    non-multipart ``text/plain`` body (no HTML-only, no multipart, no
    ``message/*``); ``Content-Type`` absent (ASCII body only) or exactly
    ``text/plain`` with exactly one ``charset`` parameter without whitespace
    around ``=`` from ``us-ascii``/``ascii``/``utf-8``; a non-ASCII body only
    with ``charset=utf-8``; 7bit/8bit or no transfer encoding; no
    ``Content-Disposition``; ``MIME-Version`` absent or ``1.0``; no header
    folding, no RFC-2047 encoding and printable-ASCII header values; exactly one
    well-formed Message-ID; a header/body separator; and a non-empty body.

    This is a structural predicate only: it starts no process, reads no file and
    performs no email-policy defect scan.  Every other input returns ``False``
    and the caller keeps its regular full read.  It is intentionally separate
    from :func:`_details_from_eml` (the P1 preview path).
    """
    try:
        if not isinstance(raw_eml, bytes) or not raw_eml:
            return False
        text = raw_eml.decode("utf-8")
        if "\x00" in text or "\r" in text.replace("\r\n", ""):
            return False
        normalized = text.replace("\r\n", "\n")
        header_block, separator, body_block = normalized.partition("\n\n")
        if not separator or not header_block.strip():
            return False

        headers: dict[str, list[str]] = {}
        for line in header_block.split("\n"):
            if not line or line[0] in " \t":
                return False
            if ":" not in line:
                return False
            name, _, value = line.partition(":")
            key = name.strip().lower()
            value = value.strip()
            if not key or "=?" in value:
                return False
            if any(ord(character) < 32 or ord(character) > 126 for character in value):
                return False
            headers.setdefault(key, []).append(value)

        for key in _EML_SINGLETON_HEADERS:
            if len(headers.get(key, ())) > 1:
                return False

        message_ids = headers.get("message-id", [])
        if len(message_ids) != 1 or not _is_well_formed_message_id(message_ids[0]):
            return False

        declared_charset: str | None = None
        content_types = headers.get("content-type")
        if content_types is not None:
            content_type_header = content_types[0]
            if not content_type_header:
                return False
            segments = content_type_header.split(";")
            if len(segments) != 2 or segments[0] != "text/plain":
                return False
            parameter = segments[1].lstrip(" \t")
            if not parameter.startswith("charset="):
                return False
            raw_charset = parameter[len("charset="):]
            if raw_charset.startswith('"') or raw_charset.endswith('"'):
                if not (
                    raw_charset.startswith('"')
                    and raw_charset.endswith('"')
                    and len(raw_charset) >= 2
                ):
                    return False
                raw_charset = raw_charset[1:-1]
            if not raw_charset or '"' in raw_charset:
                return False
            if any(character.isspace() for character in raw_charset):
                return False
            charset = raw_charset.lower()
            if charset not in _EML_ACCEPTED_CHARSETS:
                return False
            declared_charset = charset

        transfer_encoding = headers.get("content-transfer-encoding", [""])[0]
        if transfer_encoding.lower() not in _EML_ACCEPTED_TRANSFER_ENCODINGS:
            return False
        if "content-disposition" in headers:
            return False
        if headers.get("mime-version") and headers["mime-version"][0] != "1.0":
            return False
        if any(ord(character) > 126 for character in body_block):
            if declared_charset != "utf-8":
                return False
        if not any(line.strip() for line in body_block.splitlines()):
            return False
        return True
    except Exception:  # noqa: BLE001 - an unproven class is never reused
        return False


def _v3_address_string(value: object) -> str:
    """Render one v3 ``{name,email}[]`` address list as the v1 header string."""
    if not isinstance(value, list):
        return ""
    rendered: list[str] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "")
        email = str(item.get("email") or "")
        if name and email:
            rendered.append(f"{name} <{email}>")
        elif email:
            rendered.append(email)
        elif name:
            rendered.append(name)
    return ", ".join(rendered)


def _v3_reply_reference(value: object) -> str:
    """Render one v3 ``inReplyTo``/``references`` array as the v1 bracketed string."""
    if not isinstance(value, list):
        return ""
    parts: list[str] = []
    for item in value:
        if not isinstance(item, str):
            continue
        normalized = normalize_message_id(item)
        if normalized:
            parts.append(f"<{normalized}>")
    return " ".join(parts)


#: Required typed fields of the v3 ``message read --json`` ``headers`` object.
_V3_HEADER_ADDRESS_FIELDS = ("from", "to", "cc", "bcc", "replyTo", "sender")
_V3_HEADER_ID_LIST_FIELDS = ("references", "inReplyTo")
_V3_HEADER_TEXT_FIELDS = ("subject", "date", "messageId")


def _v3_headers_typed(headers: object) -> bool:
    """Whether ``headers`` is the complete, typed v3 ``MessageHeaders`` object.

    The schema requires every address list (``from``/``to``/``cc``/``bcc``/
    ``replyTo``/``sender``) as ``{name:string|null,email:string}[]``, both id
    lists (``references``/``inReplyTo``) as ``string[]`` and ``subject``/``date``/
    ``messageId`` as ``string|null``.  Only this shape justifies
    ``headers_complete``; a string map or a partial object is not the v3
    contract.
    """
    if not isinstance(headers, dict):
        return False
    for field in _V3_HEADER_ADDRESS_FIELDS:
        value = headers.get(field)
        if not isinstance(value, list):
            return False
        for item in value:
            if not isinstance(item, dict):
                return False
            name = item.get("name")
            if name is not None and not isinstance(name, str):
                return False
            if not isinstance(item.get("email"), str):
                return False
    for field in _V3_HEADER_ID_LIST_FIELDS:
        value = headers.get(field)
        if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
            return False
    for field in _V3_HEADER_TEXT_FIELDS:
        if field not in headers:
            return False
        value = headers[field]
        if value is not None and not isinstance(value, str):
            return False
    return True


def _details_from_v3_view(
    env_id: str | int,
    folder: str,
    payload: dict[str, Any],
    *,
    preview_lines: int,
    full_body: bool,
    fallback_envelope: dict[str, Any] | None,
    attachments_list: list[dict[str, Any]],
    attachment_status: str,
    attachment_error: str | None,
) -> dict[str, Any]:
    """Map one v3 ``message read --json`` view onto the existing detail fields.

    ``headers`` is the typed v3 ``MessageHeaders`` object, not a header-name map:
    ``from``/``to``/``cc`` are address arrays, ``references``/``inReplyTo`` are
    bracket-less id arrays and ``subject``/``date``/``messageId`` are strings.
    ``cc`` and ``references`` therefore survive into the existing detail fields
    (the Sent indexer and the handoff consume them), and ``headers_complete`` is
    true only for that complete, typed shape.
    """
    fb_subj, fb_from, fb_to, fb_date = _fallback_display_fields(fallback_envelope)
    headers = payload.get("headers")
    header_map = headers if isinstance(headers, dict) else {}
    headers_complete = _v3_headers_typed(headers)

    def _header_text(field: str) -> str:
        value = header_map.get(field)
        return value if isinstance(value, str) else ""

    raw_mid = payload.get("messageId")
    if not (isinstance(raw_mid, str) and raw_mid.strip()):
        raw_mid = _header_text("messageId")
    norm_mid = normalize_message_id(raw_mid) if raw_mid else ""
    text = payload.get("text")
    body_text = text if isinstance(text, str) else ""
    body_lines = body_text.splitlines()
    subject = payload.get("subject") if isinstance(payload.get("subject"), str) else ""
    date = payload.get("date") if isinstance(payload.get("date"), str) else ""
    return {
        "envelope_id": str(env_id),
        "folder": folder,
        "message_id": norm_mid,
        "raw_message_id": f"<{norm_mid}>" if norm_mid else "",
        "subject": subject or _header_text("subject") or fb_subj,
        "from": _v3_address_string(payload.get("from"))
        or _v3_address_string(header_map.get("from"))
        or fb_from,
        "to": _v3_address_string(payload.get("to"))
        or _v3_address_string(header_map.get("to"))
        or fb_to,
        "cc": _v3_address_string(header_map.get("cc")),
        "headers_complete": headers_complete,
        "date": date or _header_text("date") or fb_date,
        "in_reply_to": _v3_reply_reference(payload.get("inReplyTo"))
        or _v3_reply_reference(header_map.get("inReplyTo")),
        "references": _v3_reply_reference(header_map.get("references")),
        "preview": "\n".join(body_lines if full_body else body_lines[:preview_lines]),
        "attachments": attachments_list,
        "attachment_status": attachment_status,
        "attachment_error": attachment_error,
        "error": None,
        "read_error_reason": None,
        "read_level": "full_body" if full_body else "preview",
    }


def get_single_email_details(
    env_id: str | int,
    folder: str = "INBOX",
    account: str | None = None,
    preview_lines: int = 30,
    fallback_envelope: dict[str, Any] | None = None,
    full_body: bool = False,
    inspect_attachments: bool = True,
    raw_eml: bytes | None = None,
    *,
    purpose: str = "other",
    allow_eml_details: bool = False,
    raw_eml_sink: Callable[[bytes], None] | None = None,
    preview_only: bool = False,
    allow_full_body_from_export: bool = False,
) -> dict[str, Any]:
    """Fetch headers, preview/body, and structured MIME attachments for one envelope.

    ``purpose`` binds the additive process-start label of this read (Issue #103,
    D1).  A scan/full read also labels its inventory export: ``scan_preview``
    exports as ``scan_inventory``, ``full_read`` as ``full_inventory``; every
    other purpose forwards unchanged.  The ID-only fallback read is always
    ``id_fallback``.

    ``allow_eml_details`` (helper default ``False``; the draft passes ``True``
    since the 2026-11-25 owner release) lets a
    successful inventory export supply the preview details instead of a second
    CLI read.  It applies only inside the ``inspect_attachments`` branch, only
    for ``not full_body`` and only when :func:`_details_from_eml` proves full
    text parity; otherwise the unchanged CLI read runs without a new export.
    The download directory is resolved only for a non-empty inventory; a
    resolver failure is contained and never changes the successful inventory.

    ``raw_eml_sink`` (Issue #103 P2, default ``None``) receives the exact bytes
    of a successful inventory export once, after the export and before the
    inventory/extraction work.  The sink is observability only: it never changes
    the result and a sink error is swallowed so no capture failure reaches the
    read.  The export itself stays unbounded (unchanged behaviour); a consumer
    that must bound what it holds enforces its own budget.

    ``preview_only`` (Issue #103 P3, default ``False``) performs the CLI read and
    starts **no** export and no inventory work; the result carries an empty
    attachment list and ``attachment_status: "none"``.  It is the first step of a
    preview-first scan, where :func:`complete_scan_inventory` adds exactly one
    export afterwards.

    ``allow_full_body_from_export`` (Issue #103 P2, default ``False``) lets the
    already completed inventory export supply a ``full_body`` result instead of
    the following CLI read.  It applies only when ``full_body is True``, the
    inventory is complete, :func:`eml_full_text_parity` proves the modelled
    full-text class and the export's own Message-ID equals the normalized
    ``fallback_envelope["message_id"]``.  The result comes from the same
    :func:`_details_from_raw_eml` derivation as the ``raw_eml`` branch with
    ``read_level: "full_body"``.  A missing or deviating identity, a parity
    rejection, an unavailable inventory or any exception keeps the unchanged CLI
    read; a transport block is re-raised, never swallowed.
    """
    _validate_purpose(purpose)
    if not isinstance(allow_eml_details, bool):
        raise ValueError("allow_eml_details must be a boolean")
    if not isinstance(allow_full_body_from_export, bool):
        raise ValueError("allow_full_body_from_export must be a boolean")
    if not isinstance(preview_only, bool):
        raise ValueError("preview_only must be a boolean")
    if raw_eml_sink is not None and not callable(raw_eml_sink):
        raise ValueError("raw_eml_sink must be callable or None")
    inventory_purpose = _inventory_purpose(purpose)
    attachments_list: list[dict[str, Any]] = []
    attachment_status: str = "none"
    attachment_error: str | None = None

    if raw_eml is not None:
        try:
            attachments_list = inspect_mime_tree(raw_eml)
            inv_reasons = []
            for a in attachments_list:
                v, r = validate_attachment_candidate_metadata(a)
                if not v:
                    inv_reasons.append(r)
            if inv_reasons:
                attachments_list = []
                attachment_status = "attachment_inventory_unavailable"
                attachment_error = f"Incomplete attachment inventory in MIME: {'; '.join(inv_reasons)}"
            else:
                attachment_status = "available" if attachments_list else "none"
            eml_b = raw_eml if isinstance(raw_eml, bytes) else raw_eml.encode("utf-8", errors="replace")
            return _details_from_raw_eml(
                env_id,
                folder,
                eml_b,
                preview_lines=preview_lines,
                full_body=full_body,
                fallback_envelope=fallback_envelope,
                attachments_list=attachments_list,
                attachment_status=attachment_status,
                attachment_error=attachment_error,
            )
        except Exception as exc:
            attachments_list = []
            attachment_status = "attachment_inventory_unavailable"
            attachment_error = f"Failed to parse MIME tree: {exc}"
    elif preview_only:
        # Issue #103 P3 preview-first: the CLI read is the only start here; the
        # inventory is completed later by ``complete_scan_inventory``.
        attachments_list = []
        attachment_status = "none"
        attachment_error = None
    elif fallback_envelope and fallback_envelope.get("has_attachment") is False:
        attachments_list = []
        attachment_status = "none"
        attachment_error = None
    elif inspect_attachments:
        try:
            eml_data = fetch_raw_message_eml(
                env_id,
                folder=folder,
                account=account,
                purpose=inventory_purpose,
            )
            if raw_eml_sink is not None:
                try:
                    raw_eml_sink(eml_data)
                except Exception:  # noqa: BLE001 - a capture sink never changes the read
                    pass
            attachments_list = inspect_mime_tree(eml_data)
            inv_reasons = []
            for a in attachments_list:
                v, r = validate_attachment_candidate_metadata(a)
                if not v:
                    inv_reasons.append(r)
            if inv_reasons:
                attachments_list = []
                attachment_status = "attachment_inventory_unavailable"
                attachment_error = f"Incomplete attachment inventory in MIME: {'; '.join(inv_reasons)}"
            else:
                attachment_status = "available" if attachments_list else "none"
                attachment_error = None
                if allow_eml_details and not full_body:
                    # Issue #103 P1: the completed export may replace the
                    # following preview read, but only on proven text parity.
                    # The download directory is resolved lazily, only for a
                    # non-empty inventory, outside the pure derivation.  A
                    # resolver failure is contained here so the successful
                    # inventory stays intact and the unchanged CLI read runs.
                    downloads_dir: str | None = None
                    if attachments_list:
                        try:
                            downloads_dir = resolve_himalaya_downloads_dir(account)
                        except Exception:  # noqa: BLE001 - never changes the inventory
                            downloads_dir = None
                    derived = _details_from_eml(
                        env_id,
                        folder,
                        eml_data,
                        preview_lines=preview_lines,
                        fallback_envelope=fallback_envelope,
                        attachments=attachments_list,
                        attachment_status=attachment_status,
                        attachment_error=attachment_error,
                        downloads_dir=downloads_dir,
                    )
                    if derived is not None:
                        return derived
                elif allow_full_body_from_export and full_body:
                    # Issue #103 P2: the same export may supply the full body
                    # when its structure proves the modelled full-text class and
                    # its own Message-ID proves this exact item.  Any doubt
                    # (missing/deviating id, parity rejection, derivation error)
                    # keeps the unchanged CLI read and the successful inventory;
                    # no new export starts here.
                    reused_full_body = _full_body_from_own_export(
                        env_id,
                        folder,
                        eml_data,
                        preview_lines=preview_lines,
                        fallback_envelope=fallback_envelope,
                        attachments_list=attachments_list,
                        attachment_status=attachment_status,
                        attachment_error=attachment_error,
                    )
                    if reused_full_body is not None:
                        return reused_full_body
        except Exception as exc:
            raise_if_transport_blocked(exc)
            attachments_list = []
            attachment_status = "attachment_inventory_unavailable"
            attachment_error = f"Failed to export or inspect message MIME: {exc}"
    elif fallback_envelope and "attachments" in fallback_envelope:
        # Fallback attachments without live MIME inspection cannot be trusted as 'available'
        attachments_list = []
        attachment_status = "attachment_inventory_unavailable"
        attachment_error = "Fallback envelope attachments cannot be trusted without verified MIME inspection"

    args = [
        "message", "read",
        "-H", "Message-Id", "-H", "In-Reply-To", "-H", "References",
        "-H", "From", "-H", "To", "-H", "Cc", "-H", "Date", "-H", "Subject",
        "-f", folder, str(env_id),
    ]
    if not full_body:
        args.insert(2, "--preview")
    stdout = None
    last_err = None
    read_timeout = 30 if full_body else 12
    try:
        stdout = run_himalaya(args, account=account, timeout=read_timeout, max_retries=2, purpose=purpose)
    except Exception as e:
        raise_if_transport_blocked(e)
        last_err = e

    fb_subj, fb_from, fb_to, fb_date = _fallback_display_fields(fallback_envelope)

    if stdout is None:
        if full_body:
            return {
                "envelope_id": str(env_id),
                "folder": folder,
                "message_id": "",
                "raw_message_id": "",
                "subject": fb_subj,
                "from": fb_from,
                "to": fb_to,
                "cc": "",
                "headers_complete": False,
                "date": fb_date,
                "in_reply_to": "",
                "references": "",
                "preview": "",
                "attachments": attachments_list,
                "attachment_status": attachment_status,
                "attachment_error": attachment_error,
                "error": str(last_err) if last_err else "Full message read failed.",
                "read_error_reason": _read_failure_reason(last_err) or "read_failed",
                "read_level": "full_body",
            }
        # Lightweight fallback: try reading just Message-ID
        raw_mid = ""
        try:
            mid_out = run_himalaya(["message", "read", "-H", "Message-Id", "-f", folder, str(env_id)], account=account, timeout=10, purpose="id_fallback")
            if profile_of(mid_out) == PROFILE_V3:
                mid_payload = _decode_v3_json(mid_out)
                if isinstance(mid_payload, dict) and isinstance(mid_payload.get("messageId"), str):
                    raw_mid = mid_payload["messageId"]
            else:
                for line in mid_out.splitlines():
                    if line.lower().startswith("message-id:"):
                        raw_mid = line.split(":", 1)[1].strip()
                        break
        except Exception as exc:
            raise_if_transport_blocked(exc)

        norm_mid = normalize_message_id(raw_mid) if raw_mid else ""
        return {
            "envelope_id": str(env_id),
            "folder": folder,
            "message_id": norm_mid,
            "raw_message_id": raw_mid,
            "subject": fb_subj,
            "from": fb_from,
            "to": fb_to,
            "cc": "",
            "headers_complete": False,
            "date": fb_date,
            "in_reply_to": "",
            "references": "",
            "preview": "",
            "attachments": attachments_list,
            "attachment_status": attachment_status,
            "attachment_error": attachment_error,
            "error": None if norm_mid else str(last_err),
            "read_error_reason": _read_failure_reason(last_err),
            "read_level": "preview",
        }

    if profile_of(stdout) == PROFILE_V3:
        payload = _decode_v3_json(stdout)
        if not isinstance(payload, dict):
            raise HimalayaInvocationError(
                himalaya_normalize.JSON_INCOMPATIBLE_REASON,
                "The v3 message view is not an object; no partial result was returned.",
            )
        return _details_from_v3_view(
            env_id,
            folder,
            payload,
            preview_lines=preview_lines,
            full_body=full_body,
            fallback_envelope=fallback_envelope,
            attachments_list=attachments_list,
            attachment_status=attachment_status,
            attachment_error=attachment_error,
        )

    try:
        headers: dict[str, str] = {}
        body_lines: list[str] = []
        in_headers = True
        cur_header: str | None = None

        for line in stdout.splitlines():
            if in_headers:
                if not line.strip():
                    in_headers = False
                    continue
                if line.startswith(" ") or line.startswith("\t"):
                    if cur_header:
                        headers[cur_header] += " " + line.strip()
                elif ":" in line:
                    k, v = line.split(":", 1)
                    cur_header = k.strip().lower()
                    headers[cur_header] = v.strip()
            else:
                body_lines.append(line)

        raw_mid = headers.get("message-id", "")
        norm_mid = normalize_message_id(raw_mid) if raw_mid else ""
        read_error = None
        if not headers:
            read_error = f"Envelope {env_id} in folder '{folder}' returned no message headers."

        return {
            "envelope_id": str(env_id),
            "folder": folder,
            "message_id": norm_mid,
            "raw_message_id": raw_mid,
            "subject": headers.get("subject", "") or fb_subj,
            "from": headers.get("from", "") or fb_from,
            "to": headers.get("to", "") or fb_to,
            "cc": headers.get("cc", ""),
            "headers_complete": read_error is None,
            "date": headers.get("date", "") or fb_date,
            "in_reply_to": headers.get("in-reply-to", ""),
            "references": headers.get("references", ""),
            "preview": "\n".join(body_lines if full_body else body_lines[:preview_lines]),
            "attachments": attachments_list,
            "attachment_status": attachment_status,
            "attachment_error": attachment_error,
            "error": read_error,
            "read_error_reason": None,
            "read_level": "full_body" if full_body else "preview",
        }
    except Exception as e:
        # Issue #103/F6: never degrade the structured transport abort into a
        # per-envelope result record, even on an unexpected path.
        raise_if_transport_blocked(e)
        return {
            "envelope_id": str(env_id),
            "folder": folder,
            "message_id": "",
            "raw_message_id": "",
            "subject": fb_subj,
            "from": fb_from,
            "to": fb_to,
            "cc": "",
            "headers_complete": False,
            "date": fb_date,
            "in_reply_to": "",
            "references": "",
            "preview": "",
            "attachments": attachments_list,
            "attachment_status": attachment_status,
            "attachment_error": attachment_error,
            "error": str(e),
            "read_error_reason": "read_failed",
            "read_level": "full_body" if full_body else "preview",
        }


def verify_in_target_folder(
    target_folder: str,
    target_msg_id: str,
    subject: str = "",
    from_addr: str = "",
    date_str: str = "",
    account: str | None = None,
    candidate_env_id: str | None = None,
    diagnostics: dict[str, Any] | None = None,
) -> str | None:
    """Verify presence of a message in target folder and return its new envelope_id.

    ``diagnostics`` is an optional caller-owned collector. When supplied, the
    listing facts and at most six ``header_reads`` of this invocation are written
    into it. Header entries contain only phase, elapsed_ms and a fixed outcome;
    no raw headers, message IDs or exception text. It never changes the
    ``str | None`` return contract or the timeout/retry policy.
    """
    norm_target = normalize_message_id(target_msg_id)
    if diagnostics is not None:
        diagnostics["header_reads"] = []

    def _header_matches(cid: str, phase: str) -> bool:
        started = time.monotonic() if diagnostics is not None else None
        outcome = "error"
        try:
            h_out = run_himalaya(
                ["message", "read", "--preview", "-H", "Message-Id", "-f", target_folder, cid],
                account=account, timeout=10, max_retries=1,
            )
            outcome = "missing_message_id"
            if profile_of(h_out) == PROFILE_V3:
                payload = _decode_v3_json(h_out)
                value = payload.get("messageId") if isinstance(payload, dict) else None
                if isinstance(value, str) and value.strip():
                    outcome = "mismatch"
                    if normalize_message_id(value) == norm_target:
                        outcome = "match"
                        return True
            else:
                for line in h_out.splitlines():
                    if line.lower().startswith("message-id:"):
                        outcome = "mismatch"
                        if normalize_message_id(line.split(":", 1)[1]) == norm_target:
                            outcome = "match"
                            return True
        except Exception as exc:
            # The same failures were previously swallowed at each call site.
            # Classify only known timeouts; never serialize untrusted messages.
            raise_if_transport_blocked(exc)
            if (isinstance(exc, (TimeoutError, subprocess.TimeoutExpired))
                    or isinstance(exc, HimalayaInvocationError) and exc.reason_code == "himalaya_timeout"):
                outcome = "timeout"
            else:
                outcome = "error"
        finally:
            if diagnostics is not None:
                diagnostics["header_reads"].append({
                    "phase": phase,
                    "elapsed_ms": max(0, int((time.monotonic() - started) * 1000)),
                    "outcome": outcome,
                })
        return False

    def _record_listing(envelopes_seen: int, candidate_count: int) -> None:
        if diagnostics is not None:
            diagnostics["envelopes_seen"] = envelopes_seen
            diagnostics["candidate_count"] = candidate_count

    # Fast-path: if a specific candidate envelope is known (e.g. retained in-place), test it directly
    if candidate_env_id:
        if _header_matches(str(candidate_env_id), "fast_path"):
            return str(candidate_env_id)

    try:
        out = run_himalaya(["-o", "json", "envelope", "list", "-f", target_folder, "-s", "150"], account=account, timeout=30, max_retries=2)
        if profile_of(out) == PROFILE_V3:
            entries, _next_page = decode_v3_envelope_output(out)
            for entry in entries:
                if entry.get("message_id") != norm_target:
                    continue
                if subject and str(entry.get("subject") or "").strip().lower() != subject.strip().lower():
                    continue
                return str(entry["id"])
            return None
        if "[" in out:
            out = out[out.find("["):]
        envelopes = json.loads(out)
    except Exception:
        return None

    exact_candidates: list[str] = []
    prefix_candidates: list[str] = []

    # 1. Filter candidates by matching subject.  Exact subject matches win;
    # prefix matches are only a fallback.  Envelope listings are already in
    # the backend's newest-first order, so keep that order instead of reversing.
    for env in envelopes:
        env_subj = env.get("subject", "").strip()
        if subject:
            wanted = subject.strip()
            if env_subj.lower() == wanted.lower():
                exact_candidates.append(str(env["id"]))
            elif len(wanted) > 15 and env_subj.lower().startswith(wanted[:30].lower()):
                prefix_candidates.append(str(env["id"]))
        elif not subject and not env_subj:
            prefix_candidates.append(str(env["id"]))

    candidates = exact_candidates + prefix_candidates

    # 1b. Fallback to date only if no subject candidate found
    if not candidates and date_str:
        from .classifier import parse_date_to_year_month
        _, ymd = parse_date_to_year_month(date_str)
        for env in envelopes:
            env_date = env.get("date", "").strip()
            if ymd in env_date or date_str[:10] in env_date:
                candidates.append(str(env["id"]))

    _record_listing(len(envelopes), len(candidates))

    # 2. Check candidate headers in listing order (backend newest first, exact
    # subject matches first, then prefix matches), limited to the top 5
    for cid in candidates[:5]:
        if _header_matches(cid, "candidate"):
            return cid

    # 3. Quick fallback: check the newest five envelopes in listing order if no
    # candidates were found (never the last five)
    if not candidates:
        for env in envelopes[:5]:
            cid = str(env["id"])
            if _header_matches(cid, "fallback"):
                return cid

    return None


#: Default overall wall-clock budget (seconds) for a multi-folder mailbox sweep.
DEFAULT_SEARCH_OVERALL_DEADLINE_SECONDS: float = 120.0

#: Bounded reason raised when a multi-folder sweep exhausts its overall budget.
SEARCH_DEADLINE_REASON = "Himalaya search overall deadline exceeded; aborting the folder sweep."

#: Bounded reason raised when the supplied overall sweep budget is not a usable bound.
SEARCH_DEADLINE_INVALID_REASON = (
    "overall_deadline_seconds must be a finite positive number of seconds, or None to disable the bound."
)


def _validate_overall_deadline_seconds(value: float | None) -> None:
    """Fail closed on a non-finite, non-positive or non-numeric sweep budget.

    ``None`` is the documented disable.  Every other invalid value (NaN, +/-Inf,
    zero, negative, or a non-numeric type) is rejected before the first mailbox
    command so that a caller -- including a JSON manifest -- can never silently
    neuter or disable the wall-clock bound and trigger an unbounded sweep.
    """
    if value is None:
        return
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value <= 0
    ):
        raise ValueError(SEARCH_DEADLINE_INVALID_REASON)


def search_mailbox(
    query: str = "",
    message_ids: list[str] | None = None,
    folders: list[str] | None = None,
    page_size: int = 50,
    threads: int = 4,
    account: str | None = None,
    overall_deadline_seconds: float | None = DEFAULT_SEARCH_OVERALL_DEADLINE_SECONDS,
) -> list[dict[str, Any]]:
    """Search for messages across specified folders by query or message_ids.

    ``overall_deadline_seconds`` bounds the whole folder sweep in wall-clock
    seconds.  Exhausting it raises ``HimalayaInvocationError`` with reason code
    ``himalaya_timeout`` and a reason naming the deadline, distinct from a
    per-call timeout.  ``None`` disables the overall budget.  A per-call
    ``himalaya_timeout`` stops the sweep fail-closed.  An invalid budget
    (non-finite, non-positive or non-numeric) raises a bounded ``ValueError``
    before the first mailbox command, so the bound can never be disabled
    implicitly.
    """
    _validate_overall_deadline_seconds(overall_deadline_seconds)
    query_str = query.strip().lower()
    target_mids = set(normalize_message_id(m) for m in message_ids) if message_ids else set()
    started_at = time.monotonic()

    def deadline_exhausted() -> bool:
        """Whether the overall sweep budget is spent."""
        return (
            overall_deadline_seconds is not None
            and (time.monotonic() - started_at) >= overall_deadline_seconds
        )

    def ensure_within_deadline() -> None:
        """Fail closed once the overall sweep budget is spent."""
        if deadline_exhausted():
            raise HimalayaInvocationError("himalaya_timeout", SEARCH_DEADLINE_REASON)

    if not folders:
        try:
            f_out = run_himalaya(["folder", "list", "-o", "json"], account=account, timeout=30)
        except Exception:
            folders = ["INBOX", "Junk", "Trash", "Newsletter"]
        else:
            if profile_of(f_out) == PROFILE_V3:
                folders = [mailbox["name"] for mailbox in decode_v3_mailbox_output(f_out)]
            else:
                try:
                    if "[" in f_out:
                        f_out = f_out[f_out.find("["):]
                    folders = [f["name"] for f in json.loads(f_out)]
                except Exception:
                    folders = ["INBOX", "Junk", "Trash", "Newsletter"]

    matches: list[dict[str, Any]] = []

    def check_env_header(fld: str, env: dict[str, Any]) -> dict[str, Any] | None:
        eid = str(env.get("id", ""))
        subj = str(env.get("subject") or "")
        from_info = env.get("from", {})
        from_str = (
            f"{str(from_info.get('name') or '')} {str(from_info.get('addr') or '')}"
            if isinstance(from_info, dict)
            else str(from_info or "")
        )
        date_str = str(env.get("date") or "")

        try:
            h = run_himalaya(["message", "read", "--preview", "-H", "Message-Id", "-f", fld, eid], account=account, timeout=15)
            env_mid = ""
            for line in h.splitlines():
                if line.lower().startswith("message-id:"):
                    env_mid = normalize_message_id(line.split(":", 1)[1])
                    break

            if target_mids and env_mid in target_mids:
                return {
                    "folder": fld,
                    "envelope_id": eid,
                    "message_id": env_mid,
                    "subject": subj,
                    "from": from_str.strip(),
                    "date": date_str,
                }
            if query_str and (query_str in subj.lower() or query_str in from_str.lower()):
                return {
                    "folder": fld,
                    "envelope_id": eid,
                    "message_id": env_mid,
                    "subject": subj,
                    "from": from_str.strip(),
                    "date": date_str,
                }
        except HimalayaInvocationError as exc:
            if exc.reason_code == "himalaya_timeout":
                raise
        except Exception:
            pass
        return None

    def search_single_folder(fld: str) -> list[dict[str, Any]]:
        found_in_folder: list[dict[str, Any]] = []
        ensure_within_deadline()
        try:
            out = run_himalaya(["-o", "json", "envelope", "list", "-f", fld, "-s", str(page_size)], account=account, timeout=30)
            if profile_of(out) == PROFILE_V3:
                # v3 answers with the Message-ID in the listing.  The ID lookup
                # is one server-side ``envelope search mid:<id>`` per folder;
                # no per-envelope header read starts.
                decode_v3_envelope_output(out)  # fail closed on an invalid page
                if not target_mids:
                    if query_str:
                        raise HimalayaInvocationError(
                            SEARCH_QUERY_UNSUPPORTED_REASON,
                            "The v3 adapter has no documented translation for a free-text search; the sweep stopped.",
                        )
                    return found_in_folder
                for wanted_mid in sorted(target_mids):
                    ensure_within_deadline()
                    search_out = run_himalaya(
                        ["-o", "json", "envelope", "list", "-f", fld, "-s", str(page_size), "mid", wanted_mid],
                        account=account,
                        timeout=30,
                    )
                    entries, _next_page = decode_v3_envelope_output(search_out)
                    for env in entries:
                        from_info = env.get("from")
                        from_str = (
                            f"{str(from_info.get('name') or '')} {str(from_info.get('addr') or '')}"
                            if isinstance(from_info, dict)
                            else ""
                        )
                        found_in_folder.append(
                            {
                                "folder": fld,
                                "envelope_id": str(env.get("id", "")),
                                "message_id": str(env.get("message_id") or ""),
                                "subject": str(env.get("subject") or ""),
                                "from": from_str.strip(),
                                "date": str(env.get("date") or ""),
                            }
                        )
                return found_in_folder
            if "[" in out:
                out = out[out.find("["):]
            envelopes = json.loads(out)
        except HimalayaInvocationError as exc:
            if exc.reason_code in (
                "himalaya_timeout",
                SEARCH_QUERY_UNSUPPORTED_REASON,
                himalaya_normalize.JSON_INCOMPATIBLE_REASON,
            ):
                # The overall deadline, a refused v3 query and an incompatible
                # v3 page are fail-closed refusals, never an empty partial
                # result.  Everything else keeps the established containment.
                raise
            return found_in_folder
        except Exception:
            return found_in_folder

        if query_str and not target_mids:
            candidates = [
                e for e in envelopes
                if query_str in str(e.get("subject") or "").lower() or query_str in str(e.get("from") or "").lower()
            ]
        else:
            candidates = envelopes

        with concurrent.futures.ThreadPoolExecutor(max_workers=5) as exec_env:
            futs = [exec_env.submit(check_env_header, fld, env) for env in candidates]
            for f in concurrent.futures.as_completed(futs):
                res = f.result()
                if res:
                    found_in_folder.append(res)

        return found_in_folder

    if threads <= 1:
        for fld in folders:
            ensure_within_deadline()
            matches.extend(search_single_folder(fld))
        return matches

    with concurrent.futures.ThreadPoolExecutor(max_workers=threads) as executor:
        futures: dict[concurrent.futures.Future[list[dict[str, Any]]], str] = {}
        for fld in folders:
            ensure_within_deadline()
            futures[executor.submit(search_single_folder, fld)] = fld
        for fut in concurrent.futures.as_completed(futures):
            try:
                res = fut.result()
            except HimalayaInvocationError:
                for pending in futures:
                    pending.cancel()
                raise
            if res:
                matches.extend(res)

    return matches
