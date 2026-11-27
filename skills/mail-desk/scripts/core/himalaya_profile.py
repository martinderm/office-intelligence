"""Version-aware Himalaya CLI profiles and the central v1/v3 argument map.

This module is deliberately free of process, configuration and mailbox access:
it only parses a ``himalaya --version`` line, selects the supported profile and
translates the adapter's logical (v1-shaped) argv into the documented v3 argv.
The transport boundary (probing, caching, account placement) lives in
``core.himalaya``.

Supported profiles (fail-closed for everything else):

* ``1.2.x`` -> ``v1``: the pinned, byte-identical legacy argv.
* ``3.x``   -> ``v3``: ``--json``, ``-m/--mailbox``, ``mailbox list``,
  ``envelope search`` (paging ``-p/--page``), ``message read --json``/``--raw --json``,
  ``message add`` with stdin, ``message copy -f/-t``, ``message delete -m`` and
  ``flag add ... -m``.
"""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Sequence

PROFILE_V1 = "v1"
PROFILE_V3 = "v3"

#: Bounded wall-clock budget (seconds) of the single ``--version`` probe.
PROBE_TIMEOUT_SECONDS = 5

#: Bounded reason codes raised by this module and mapped to
#: ``HimalayaInvocationError`` by the transport boundary.
VERSION_UNSUPPORTED_REASON = "himalaya_version_unsupported"
UNAVAILABLE_REASON = "himalaya_unavailable"
EXPUNGE_UNSUPPORTED_REASON = "himalaya_expunge_unsupported"
COMMAND_UNSUPPORTED_REASON = "himalaya_command_unsupported"
SEARCH_QUERY_UNSUPPORTED_REASON = "himalaya_search_query_unsupported"

#: ``himalaya v3.0.0 +gmail ... build: ...`` carries trailing build tokens that
#: must be ignored; an optional leading ``v`` is the only variant seen live.
_VERSION_LINE = re.compile(r"^\s*himalaya\s+v?(\d+)\.(\d+)\.(\d+)", re.IGNORECASE)


@dataclass(frozen=True)
class HimalayaProfile:
    """One selected, supported CLI profile."""

    name: str
    major: int
    minor: int
    patch: int

    @property
    def is_v3(self) -> bool:
        """Whether this is the v3 CLI profile."""
        return self.name == PROFILE_V3


#: The pinned legacy profile (used for injected/virtual command paths).
V1_PROFILE = HimalayaProfile(PROFILE_V1, 1, 2, 0)

#: The verified v3 profile.
V3_PROFILE = HimalayaProfile(PROFILE_V3, 3, 0, 0)


class ProfileError(Exception):
    """A bounded, fail-closed profile or command-mapping refusal."""

    def __init__(self, reason_code: str, message: str) -> None:
        super().__init__(message)
        self.reason_code = reason_code


def parse_version_output(text: object) -> HimalayaProfile | None:
    """Return the supported profile of one ``himalaya --version`` stdout, or ``None``.

    Only ``1.2.x`` and ``3.x`` are supported.  Any other major/minor, a missing
    line or unparseable text returns ``None`` so the caller can fail closed.
    """
    if not isinstance(text, str):
        return None
    match = _VERSION_LINE.match(text)
    if match is None:
        return None
    major, minor, patch = (int(group) for group in match.groups())
    if major == 1 and minor == 2:
        return HimalayaProfile(PROFILE_V1, major, minor, patch)
    if major == 3:
        return HimalayaProfile(PROFILE_V3, major, minor, patch)
    return None


# ---------------------------------------------------------------------------
# Argument map
# ---------------------------------------------------------------------------

_OUTPUT_FORMAT_FLAGS = frozenset({"-o", "--output"})
_HEADER_FLAGS = frozenset({"-H", "--header"})
_FOLDER_FLAGS = ("-f", "--folder")
_PAGE_SIZE_FLAGS = ("-s", "--page-size")
_PAGE_FLAGS = ("-p", "--page")
_PAGE_TOKEN_FLAGS = ("--page-token",)
_SORT_FLAGS = ("--sort", "-S")
_SEARCH_FIELDS = frozenset({"subject", "from", "to", "mid", "is", "date"})


def _strip_output_format(tokens: list[str]) -> list[str]:
    """Remove a v1 ``-o <format>`` / ``--output=<format>`` pair."""
    cleaned: list[str] = []
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if token in _OUTPUT_FORMAT_FLAGS:
            index += 2
            continue
        if token.startswith("--output="):
            index += 1
            continue
        cleaned.append(token)
        index += 1
    return cleaned


def _pop_option(tokens: list[str], flags: Sequence[str]) -> tuple[str | None, list[str]]:
    """Return the first value of ``flags`` and the token list without that pair."""
    for index, token in enumerate(tokens):
        if token.startswith("--") and "=" in token:
            key, _, value = token.partition("=")
            if key in flags:
                return value, tokens[:index] + tokens[index + 1 :]
        if token in flags:
            if index + 1 < len(tokens):
                return tokens[index + 1], tokens[:index] + tokens[index + 2 :]
            return None, tokens[:index] + tokens[index + 1 :]
    return None, list(tokens)


def _single_option(tokens: list[str], flags: Sequence[str]) -> str | None:
    """Return the first value of ``flags`` in ``tokens`` without rewriting them."""
    for index, token in enumerate(tokens):
        if token.startswith("--") and "=" in token:
            key, _, value = token.partition("=")
            if key in flags:
                return value
        if token in flags and index + 1 < len(tokens):
            return tokens[index + 1]
    return None


def _positionals(tokens: list[str], *, value_flags: Sequence[str]) -> list[str]:
    """Collect positional tokens while skipping known flag/value pairs."""
    flags = frozenset(value_flags)
    positionals: list[str] = []
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if token.startswith("--") and "=" in token and token.partition("=")[0] in flags:
            index += 1
            continue
        if token in flags:
            index += 2
            continue
        if token.startswith("-"):
            index += 1
            continue
        positionals.append(token)
        index += 1
    return positionals


def _split_composed_sort(token: str) -> tuple[str, str] | None:
    """Split a trailing ``order by date asc|desc`` phrase out of one query token.

    Callers such as the batch runner build one composed query token
    (``date 2026-01-01 order by date asc``).  The phrase is mapped to the v3
    ``--sort`` value and the remaining query stays translatable; an exact
    phrase-only token yields an empty remainder.
    """
    lowered = token.lower()
    for phrase, direction in (
        ("order by date asc", "oldest-first"),
        ("order by date desc", "newest-first"),
    ):
        position = lowered.find(phrase)
        if position >= 0:
            remainder = (token[:position] + " " + token[position + len(phrase) :]).strip()
            return remainder, direction
    return None


def _translate_query(positionals: Sequence[str]) -> tuple[list[str], str | None]:
    """Translate the documented v1 query forms into v3 ``envelope search`` tokens.

    Supported: ``subject <q>``/``subject:<q>``, ``date <D>``/``date:D``,
    ``mid|from|to|is <value>``/``<field>:<value>`` and
    ``order by date asc|desc`` (as separate tokens, as one composed token or as a
    trailing phrase of a single composed query token).  Anything else is
    fail-closed: the v3 adapter never silently drops a query.
    """
    search: list[str] = []
    sort: str | None = None
    index = 0
    tokens = [str(token) for token in positionals]
    while index < len(tokens):
        token = tokens[index]
        composed = _split_composed_sort(token)
        if composed is not None:
            token, sort = composed
            if not token:
                index += 1
                continue
        lowered = token.strip().lower()
        if lowered in ("order by date asc", "order by date desc"):
            sort = "oldest-first" if lowered.endswith("asc") else "newest-first"
            index += 1
            continue
        if lowered == "order" and index + 3 < len(tokens):
            by, field, direction = (tokens[index + offset].strip().lower() for offset in (1, 2, 3))
            if by == "by" and field == "date" and direction in ("asc", "desc"):
                sort = "oldest-first" if direction == "asc" else "newest-first"
                index += 4
                continue
        if lowered.startswith("order"):
            raise ProfileError(
                SEARCH_QUERY_UNSUPPORTED_REASON,
                "The v3 adapter translates only 'order by date asc|desc'; the v1 query was not sent.",
            )
        field, separator, value = token.partition(":")
        if separator and field.strip().lower() in _SEARCH_FIELDS:
            search.append(f"{field.strip().lower()}:{value}")
            index += 1
            continue
        if " " in token:
            head, _, tail = token.partition(" ")
            if head.strip().lower() in _SEARCH_FIELDS and tail.strip():
                search.append(f"{head.strip().lower()}:{tail.strip()}")
                index += 1
                continue
        if token.strip().lower() in _SEARCH_FIELDS and index + 1 < len(tokens):
            search.append(f"{token.strip().lower()}:{tokens[index + 1]}")
            index += 2
            continue
        raise ProfileError(
            SEARCH_QUERY_UNSUPPORTED_REASON,
            "The v3 adapter has no documented translation for this query; it was not sent.",
        )
    return search, sort


def _translate_folder(tokens: list[str]) -> list[str]:
    subcommand = tokens[1] if len(tokens) > 1 else ""
    if subcommand == "list":
        return ["--json", "mailbox", "list"]
    if subcommand == "expunge":
        raise ProfileError(
            EXPUNGE_UNSUPPORTED_REASON,
            "Himalaya v3 has no in-place expunge; no mailbox command was sent.",
        )
    raise ProfileError(COMMAND_UNSUPPORTED_REASON, "This 'folder' command has no v3 mapping.")


def _translate_envelope(tokens: list[str]) -> list[str]:
    subcommand = tokens[1] if len(tokens) > 1 else ""
    if subcommand == "thread":
        folder, _rest = _pop_option(tokens, _FOLDER_FLAGS)
        return ["--json", "envelope", "list", "-m", folder or "INBOX"]
    if subcommand != "list":
        raise ProfileError(COMMAND_UNSUPPORTED_REASON, "This 'envelope' command has no v3 mapping.")

    rest = tokens[2:]
    folder, rest = _pop_option(rest, _FOLDER_FLAGS)
    page_size, rest = _pop_option(rest, _PAGE_SIZE_FLAGS)
    page, rest = _pop_option(rest, _PAGE_FLAGS)
    page_token, rest = _pop_option(rest, _PAGE_TOKEN_FLAGS)
    explicit_sort, rest = _pop_option(rest, _SORT_FLAGS)
    search, sort = _translate_query(_positionals(rest, value_flags=()))
    sort = sort or explicit_sort

    mapped = ["--json", "envelope", "search" if (search or sort is not None) else "list"]
    mapped.extend(search)
    mapped.extend(["-m", folder or "INBOX"])
    if page_size is not None:
        mapped.extend(["-s", page_size])
    if page is not None:
        mapped.extend(["-p", page])
    if page_token is not None:
        mapped.extend(["--page-token", page_token])
    if sort is not None:
        mapped.extend(["--sort", sort])
    return mapped


def _translate_message(tokens: list[str]) -> list[str]:
    subcommand = tokens[1] if len(tokens) > 1 else ""
    if subcommand == "read":
        rest = tokens[2:]
        while _single_option(rest, _HEADER_FLAGS) is not None:
            _value, rest = _pop_option(rest, _HEADER_FLAGS)
        rest = [
            token
            for token in rest
            if token not in _HEADER_FLAGS and not token.startswith("--header=")
        ]
        folder, rest = _pop_option(rest, _FOLDER_FLAGS)
        raw = "--raw" in rest
        rest = [token for token in rest if token not in ("--raw", "--preview")]
        envelope_ids = _positionals(rest, value_flags=())
        if not envelope_ids:
            raise ProfileError(COMMAND_UNSUPPORTED_REASON, "A v3 'message read' needs one envelope id.")
        mapped = ["--json", "message", "read", envelope_ids[-1], "-m", folder or "INBOX"]
        if raw:
            mapped.append("--raw")
        return mapped
    if subcommand == "export":
        rest = tokens[2:]
        folder, rest = _pop_option(rest, _FOLDER_FLAGS)
        envelope_ids = _positionals(rest, value_flags=("-d", "--destination"))
        if not envelope_ids:
            raise ProfileError(COMMAND_UNSUPPORTED_REASON, "A v3 raw read needs one envelope id.")
        return ["--json", "message", "read", envelope_ids[0], "-m", folder or "INBOX", "--raw"]
    if subcommand == "save":
        folder, _rest = _pop_option(tokens[2:], _FOLDER_FLAGS)
        return ["--json", "message", "add", "--mailbox", folder or "INBOX", "--flag", "draft"]
    if subcommand == "copy":
        rest = tokens[2:]
        source, rest = _pop_option(rest, _FOLDER_FLAGS)
        positionals = _positionals(rest, value_flags=_TRANSFER_VALUE_FLAGS)
        if len(positionals) < 2:
            raise ProfileError(COMMAND_UNSUPPORTED_REASON, "A v3 'message copy' needs target and id.")
        target, envelope_id = positionals[0], positionals[1]
        return [
            "--json", "message", "copy",
            "-f", source or "INBOX",
            "-t", target,
            envelope_id,
        ]
    if subcommand == "delete":
        rest = tokens[2:]
        folder, rest = _pop_option(rest, _FOLDER_FLAGS)
        positionals = _positionals(rest, value_flags=())
        if not positionals:
            raise ProfileError(COMMAND_UNSUPPORTED_REASON, "A v3 'message delete' needs one envelope id.")
        return ["--json", "message", "delete", "-m", folder or "INBOX", positionals[0]]
    raise ProfileError(COMMAND_UNSUPPORTED_REASON, "This 'message' command has no v3 mapping.")


def _translate_flag(tokens: list[str]) -> list[str]:
    if len(tokens) < 4 or tokens[1] != "add":
        raise ProfileError(COMMAND_UNSUPPORTED_REASON, "Only 'flag add' has a v3 mapping.")
    rest = tokens[2:]
    folder, rest = _pop_option(rest, _FOLDER_FLAGS)
    positionals = _positionals(rest, value_flags=())
    if len(positionals) < 2:
        raise ProfileError(COMMAND_UNSUPPORTED_REASON, "A v3 'flag add' needs a flag and an id.")
    return ["--json", "flag", "add", positionals[0], positionals[1], "-m", folder or "INBOX"]


_TRANSFER_VALUE_FLAGS = ("-t", "--target", "-f", "--folder")


def translate_v3_arguments(args: Sequence[str]) -> list[str]:
    """Translate the adapter's logical argv into the documented v3 argv.

    An argv that already starts with the v3 global ``--json`` marker is passed
    through unchanged: the profile-aware builders may emit final v3 tokens
    directly (for example an injected thread-map runner).  Every other argv is
    the v1-shaped logical request; an unmapped family raises a bounded
    :class:`ProfileError` instead of sending a wrong command.
    """
    tokens = [str(token) for token in args]
    if not tokens:
        raise ProfileError(COMMAND_UNSUPPORTED_REASON, "An empty argv has no v3 mapping.")
    if tokens[0] == "--json":
        return tokens
    tokens = _strip_output_format(tokens)
    if not tokens:
        raise ProfileError(COMMAND_UNSUPPORTED_REASON, "An output-only argv has no v3 mapping.")
    head = tokens[0]
    if head == "folder":
        return _translate_folder(tokens)
    if head == "envelope":
        return _translate_envelope(tokens)
    if head == "message":
        return _translate_message(tokens)
    if head == "flag":
        return _translate_flag(tokens)
    raise ProfileError(
        COMMAND_UNSUPPORTED_REASON,
        "This Himalaya command family has no documented v3 mapping; it was not sent.",
    )
