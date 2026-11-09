"""Raw-text, single-entry merge of an existing Cloud Atlas filemap (Issue #22).

A full scan rebuilds ``files`` and deletes orphans.  The single-file mode must do
the opposite: change exactly one ``files`` member and leave every other byte of
the document alone.  Re-serializing the parsed document cannot guarantee that
(foreign formatting, key escapes, number spelling), so this module locates the
raw character spans of the members and splices only the one member.
"""

from __future__ import annotations

import json
import os
import re
import stat
import tempfile
import unicodedata
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any, Callable

from .metadata import validate_cloud_artifact_metadata


FILEMAP_SCHEMA_URI = (
    "https://raw.githubusercontent.com/martinderm/office-intelligence/main/"
    "skills/cloud-atlas/references/filemap.schema.json"
)
FILEMAP_SCHEMA_VERSION = 1
_CONTAINER_REQUIRED = frozenset({
    "$schema", "schema_version", "kind", "scope", "storage_id", "project",
    "project_title", "scan_dir", "output_dir", "updated_at", "files",
})
_TIMESTAMP = re.compile(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}")
_SIZE = re.compile(r"\d+(?:\.\d+)? (?:B|KB|MB)")
_SHA256 = re.compile(r"[a-fA-F0-9]{64}")
_JSON_WHITESPACE = " \t\n\r"
_DECODER = json.JSONDecoder()


class FilemapError(ValueError):
    """The existing filemap cannot be merged safely (fail-closed)."""


class DuplicateSourceError(FilemapError):
    """More than one member (or a case variant) names the requested source."""


class DuplicateKeyError(FilemapError):
    """A JSON object repeats a key; the second value would silently win."""

    def __init__(self, key):
        super().__init__(f"filemap contains a duplicate JSON key: {key!r}")
        self.key = key


@dataclass(frozen=True)
class Member:
    key: str
    key_start: int
    key_end: int
    value_start: int
    value_end: int
    prefix_start: int  # first character after the preceding delimiter


@dataclass(frozen=True)
class Filemap:
    text: str
    data: dict
    top_members: list
    files_member: Member
    files_open: int
    files_close: int
    members: list


def normalize_relative(value: Any) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    raw = value.strip().replace("\\", "/")
    if raw.startswith("/") or re.match(r"^[A-Za-z]:/", raw):
        return None
    path = PurePosixPath(raw)
    if ".." in path.parts:
        return None
    return path.as_posix()


def _within(path: str, parent: str) -> bool:
    try:
        PurePosixPath(path).relative_to(PurePosixPath(parent))
        return True
    except ValueError:
        return False


def source_fold(key: str) -> str:
    """Comparison key for a source path: separators, NFC and casefold."""
    normalized = PurePosixPath(key.replace("\\", "/")).as_posix()
    return unicodedata.normalize("NFC", normalized).casefold()


def _no_duplicate_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise DuplicateKeyError(key)
        result[key] = value
    return result


def _reject_constant(name):
    raise FilemapError(f"filemap contains the non-JSON constant {name}")


def _skip_ws(text: str, index: int) -> int:
    while index < len(text) and text[index] in _JSON_WHITESPACE:
        index += 1
    return index


def _scan_object(text: str, start: int):
    """Return ``(members, close_index)`` for the JSON object opened at ``start``."""
    if start >= len(text) or text[start] != "{":
        raise FilemapError("expected a JSON object")
    members: list[Member] = []
    prefix_start = start + 1
    index = _skip_ws(text, prefix_start)
    if index < len(text) and text[index] == "}":
        return members, index
    while True:
        key_start = index
        try:
            key, key_end = _DECODER.raw_decode(text, key_start)
        except json.JSONDecodeError as exc:
            raise FilemapError(f"invalid JSON object key: {exc}") from exc
        if not isinstance(key, str):
            raise FilemapError("JSON object key is not a string")
        index = _skip_ws(text, key_end)
        if index >= len(text) or text[index] != ":":
            raise FilemapError("expected ':' after JSON object key")
        value_start = _skip_ws(text, index + 1)
        try:
            _value, value_end = _DECODER.raw_decode(text, value_start)
        except json.JSONDecodeError as exc:
            raise FilemapError(f"invalid JSON value: {exc}") from exc
        members.append(Member(key, key_start, key_end, value_start, value_end, prefix_start))
        index = _skip_ws(text, value_end)
        if index < len(text) and text[index] == ",":
            prefix_start = index + 1
            index = _skip_ws(text, prefix_start)
            continue
        if index < len(text) and text[index] == "}":
            return members, index
        raise FilemapError("expected ',' or '}' in JSON object")


def parse_filemap(text: str) -> Filemap:
    """Parse strictly (no duplicate keys, no NaN) and locate raw member spans."""
    if text.startswith("﻿"):
        raise FilemapError("filemap must not start with a byte order mark")
    try:
        data = json.loads(
            text, object_pairs_hook=_no_duplicate_keys, parse_constant=_reject_constant,
        )
    except json.JSONDecodeError as exc:
        raise FilemapError(f"filemap is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise FilemapError("filemap must be a JSON object")
    if not isinstance(data.get("files"), dict):
        raise FilemapError("filemap.files must be an object")
    top_start = _skip_ws(text, 0)
    top_members, top_close = _scan_object(text, top_start)
    if _skip_ws(text, top_close + 1) != len(text):
        raise FilemapError("filemap has trailing content after the JSON document")
    files_member = next((m for m in top_members if m.key == "files"), None)
    if files_member is None:
        raise FilemapError("filemap.files is missing")
    members, files_close = _scan_object(text, files_member.value_start)
    if len(members) != len(data["files"]):
        raise FilemapError("internal span scan disagrees with the parsed filemap")
    return Filemap(
        text, data, top_members, files_member, files_member.value_start,
        files_close, members,
    )


def locate_source(filemap: Filemap, source: str) -> Member | None:
    """Return the one member for ``source``; reject case/duplicate variants."""
    folded = source_fold(source)
    matches = [member for member in filemap.members if source_fold(member.key) == folded]
    if len(matches) > 1:
        raise DuplicateSourceError(
            f"filemap names the source more than once: {[m.key for m in matches]!r}"
        )
    if matches and matches[0].key != source:
        raise DuplicateSourceError(
            f"filemap names the source with a different spelling: {matches[0].key!r}"
        )
    return matches[0] if matches else None


# ---------------------------------------------------------------------------
# Validation (in-memory subset of gen_filemap.validate_filemap)
# ---------------------------------------------------------------------------

def validate_file_entry(source: str, entry: Any, *, scan_dir: str, output_dir: str) -> str | None:
    """Validate one entry; return its folded mirror path, if any."""
    normalized = normalize_relative(source)
    if not normalized or not _within(normalized, scan_dir):
        raise FilemapError(f"filemap.files contains a source outside scan_dir: {source!r}")
    if not isinstance(entry, dict):
        raise FilemapError(f"filemap.files[{source!r}] must be an object")
    missing = sorted({"version", "mtime", "size", "sha256", "description"} - set(entry))
    if missing:
        raise FilemapError(f"filemap.files[{source!r}] is missing keys: {missing}")
    if not isinstance(entry["version"], str) or not isinstance(entry["description"], str):
        raise FilemapError(f"filemap.files[{source!r}] has invalid text fields")
    if not isinstance(entry["mtime"], str) or _TIMESTAMP.fullmatch(entry["mtime"]) is None:
        raise FilemapError(f"filemap.files[{source!r}].mtime has an invalid timestamp")
    if not isinstance(entry["size"], str) or _SIZE.fullmatch(entry["size"]) is None:
        raise FilemapError(f"filemap.files[{source!r}].size has an invalid format")
    if not isinstance(entry["sha256"], str) or _SHA256.fullmatch(entry["sha256"]) is None:
        raise FilemapError(f"filemap.files[{source!r}].sha256 has an invalid format")

    mirror = None
    if "markdown_mirror" in entry:
        mirror = normalize_relative(entry["markdown_mirror"])
        if not mirror or not _within(mirror, output_dir):
            raise FilemapError(f"filemap.files[{source!r}].markdown_mirror is outside output_dir")
    status = entry.get("conversion_status")
    if "conversion_status" in entry and (
        not isinstance(status, str) or status not in {"converted", "conversion_required"}
    ):
        raise FilemapError(f"filemap.files[{source!r}].conversion_status is invalid")
    if status == "converted" and not mirror:
        raise FilemapError(f"filemap.files[{source!r}] is converted without a markdown_mirror")
    if "ocr_applied" in entry and not isinstance(entry["ocr_applied"], bool):
        raise FilemapError(f"filemap.files[{source!r}].ocr_applied must be boolean")
    if "ocr_policy" in entry and (
        not isinstance(entry["ocr_policy"], str)
        or entry["ocr_policy"] not in {"enrich_source", "local_derivative", "disabled"}
    ):
        raise FilemapError(f"filemap.files[{source!r}].ocr_policy is invalid")
    if "conversion_error" in entry and not isinstance(entry["conversion_error"], str):
        raise FilemapError(f"filemap.files[{source!r}].conversion_error must be a string")
    if "derivative" in entry:
        derivative = entry["derivative"]
        if not isinstance(derivative, dict):
            raise FilemapError(f"filemap.files[{source!r}].derivative must be an object")
        for field in ("path", "sha256", "format"):
            if field not in derivative:
                raise FilemapError(f"filemap.files[{source!r}].derivative is missing {field}")
        deriv_path = normalize_relative(derivative["path"])
        if not deriv_path or not _within(deriv_path, output_dir):
            raise FilemapError(f"filemap.files[{source!r}].derivative.path is outside output_dir")
        if not isinstance(derivative["sha256"], str) or _SHA256.fullmatch(derivative["sha256"]) is None:
            raise FilemapError(f"filemap.files[{source!r}].derivative.sha256 has an invalid format")
        if not isinstance(derivative["format"], str) or not derivative["format"].strip():
            raise FilemapError(f"filemap.files[{source!r}].derivative.format is invalid")
    if "artifact_metadata" in entry:
        if not mirror:
            raise FilemapError(f"filemap.files[{source!r}] has artifact_metadata without a mirror")
        try:
            validate_cloud_artifact_metadata(entry["artifact_metadata"], expected_source_uri=normalized)
        except ValueError as exc:
            raise FilemapError(f"filemap.files[{source!r}].artifact_metadata: {exc}") from exc
    return mirror.casefold() if mirror else None


def validate_filemap_document(data: dict, *, scan_dir: str, output_dir: str) -> dict[str, str]:
    """Validate container + every entry before any mutation.

    Returns the folded-mirror index ``{folded mirror: source}``.  A container
    with ``$schema`` must satisfy the full generator contract; a legacy
    converter-written map only needs a ``files`` object.
    """
    if not isinstance(data, dict):
        raise FilemapError("filemap must be an object")
    if "$schema" in data:
        missing = sorted(_CONTAINER_REQUIRED - set(data))
        if missing:
            raise FilemapError(f"filemap is missing required keys: {missing}")
        unknown = sorted(set(data) - _CONTAINER_REQUIRED - {"exclude_dirs"})
        if unknown:
            raise FilemapError(f"filemap has unknown container keys: {unknown}")
        if data["$schema"] != FILEMAP_SCHEMA_URI:
            raise FilemapError("filemap.$schema does not identify the bundled Filemap schema")
        if data["schema_version"] != FILEMAP_SCHEMA_VERSION or isinstance(data["schema_version"], bool):
            raise FilemapError(f"filemap.schema_version must be {FILEMAP_SCHEMA_VERSION}")
        if data["kind"] != "cloud-filemap":
            raise FilemapError("filemap.kind must be 'cloud-filemap'")
        if data["scope"] not in ("project", "topic"):
            raise FilemapError("filemap.scope is invalid")
        for field in ("project", "project_title", "storage_id"):
            if not isinstance(data[field], str) or not data[field].strip():
                raise FilemapError(f"filemap.{field} must be a non-empty string")
        for field, expected in (("scan_dir", scan_dir), ("output_dir", output_dir)):
            if normalize_relative(data[field]) != expected:
                raise FilemapError(
                    f"filemap.{field} does not match the resolved storage ({expected!r})"
                )
    if "updated_at" in data and (
        not isinstance(data["updated_at"], str) or _TIMESTAMP.fullmatch(data["updated_at"]) is None
    ):
        raise FilemapError("filemap.updated_at has an invalid timestamp")
    if "exclude_dirs" in data and not (
        isinstance(data["exclude_dirs"], list) and all(isinstance(v, str) for v in data["exclude_dirs"])
    ):
        raise FilemapError("filemap.exclude_dirs must be a list of strings")

    mirrors: dict[str, str] = {}
    for source, entry in data["files"].items():
        mirror = validate_file_entry(source, entry, scan_dir=scan_dir, output_dir=output_dir)
        if mirror is not None:
            previous = mirrors.get(mirror)
            if previous is not None:
                raise FilemapError(
                    "filemap.files has duplicate markdown_mirror targets under case-insensitive "
                    f"comparison: {previous!r} and {source!r}"
                )
            mirrors[mirror] = source
    return mirrors


# ---------------------------------------------------------------------------
# Splice
# ---------------------------------------------------------------------------

def _indent_after_newline(whitespace: str) -> str:
    return whitespace.rsplit("\n", 1)[1]


def _render_value(entry: dict, unit: str | None, member_indent: str, newline: str) -> str:
    if unit is None:
        return json.dumps(entry, ensure_ascii=False)
    dumped = json.dumps(entry, indent=unit, ensure_ascii=False)
    return (newline + member_indent).join(dumped.split("\n"))


def _style(filemap: Filemap, sample: Member | None):
    """Derive (item_prefix, unit, member_indent, newline, colon) from the file."""
    text = filemap.text
    top_sample = filemap.files_member
    top_ws = text[top_sample.prefix_start:top_sample.key_start]
    pretty = "\n" in top_ws
    newline = "\r\n" if "\r\n" in top_ws else "\n"
    parent_indent = _indent_after_newline(top_ws) if pretty else ""
    if sample is not None:
        prefix = text[sample.prefix_start:sample.key_start]
        colon = text[sample.key_end:sample.value_start]
        if "\n" in prefix:
            member_indent = _indent_after_newline(prefix)
            newline = "\r\n" if "\r\n" in prefix else "\n"
            unit = member_indent[len(parent_indent):] if member_indent.startswith(parent_indent) else ""
            return prefix, (unit or "  "), member_indent, newline, colon
        return prefix, None, "", newline, colon
    if pretty:
        first_top = filemap.top_members[0]
        top_indent = _indent_after_newline(text[first_top.prefix_start:first_top.key_start])
        unit = top_indent or "  "
        member_indent = parent_indent + unit
        return newline + member_indent, unit, member_indent, newline, ": "
    return "", None, "", newline, ": "


def _keys_sorted(members: list) -> bool:
    keys = [member.key for member in members]
    return keys == sorted(keys)


def splice_entry(filemap: Filemap, source: str, entry: dict) -> str:
    """Return the filemap text with exactly one ``files`` member created/replaced."""
    text = filemap.text
    existing = locate_source(filemap, source)
    if existing is not None:
        prefix, unit, member_indent, newline, _colon = _style(filemap, existing)
        value = _render_value(entry, unit, member_indent, newline)
        return text[:existing.value_start] + value + text[existing.value_end:]

    members = filemap.members
    key_text_of = lambda: json.dumps(source, ensure_ascii=False)
    if not members:
        prefix, unit, member_indent, newline, colon = _style(filemap, None)
        member = key_text_of() + colon + _render_value(entry, unit, member_indent, newline)
        if unit is None:
            return text[:filemap.files_open + 1] + member + text[filemap.files_close:]
        top_ws = text[filemap.files_member.prefix_start:filemap.files_member.key_start]
        parent_indent = _indent_after_newline(top_ws) if "\n" in top_ws else ""
        return (
            text[:filemap.files_open + 1] + prefix + member + newline + parent_indent
            + text[filemap.files_close:]
        )
    insert_before = None
    if _keys_sorted(members):
        insert_before = next((m for m in members if m.key > source), None)
    if insert_before is not None:
        prefix, unit, member_indent, newline, colon = _style(filemap, insert_before)
        member = key_text_of() + colon + _render_value(entry, unit, member_indent, newline)
        position = insert_before.key_start
        return text[:position] + member + "," + prefix + text[position:]
    last = members[-1]
    prefix, unit, member_indent, newline, colon = _style(filemap, last)
    member = key_text_of() + colon + _render_value(entry, unit, member_indent, newline)
    return text[:last.value_end] + "," + prefix + member + text[last.value_end:]


def verify_splice(before: Filemap, after_text: str, source: str, entry: dict) -> Filemap:
    """Prove that only the one member differs; raise before anything is written."""
    after = parse_filemap(after_text)
    for old, new in zip(
        [m for m in before.top_members if m.key != "files"],
        [m for m in after.top_members if m.key != "files"],
    ):
        if (
            before.text[old.key_start:old.value_end] != after.text[new.key_start:new.value_end]
        ):
            raise FilemapError("splice verification failed: a top-level member changed")
    old_foreign = [m for m in before.members if m.key != source]
    new_foreign = [m for m in after.members if m.key != source]
    if len(old_foreign) != len(new_foreign):
        raise FilemapError("splice verification failed: member count changed")
    for old, new in zip(old_foreign, new_foreign):
        if old.key != new.key or (
            before.text[old.key_start:old.value_end] != after.text[new.key_start:new.value_end]
        ):
            raise FilemapError(f"splice verification failed: {old.key!r} is not byte-identical")
    if after.data["files"].get(source) != json.loads(json.dumps(entry)):
        raise FilemapError("splice verification failed: target entry differs from the merge result")
    return after


def write_text_atomic(
    path: str, text: str, *, mode: int | None = None,
    safe_path: Callable[[str], str] = lambda value: value,
) -> None:
    """Replace ``path`` through a flushed sibling temporary file and ``os.replace``."""
    absolute = os.path.abspath(path)
    directory, name = os.path.split(absolute)
    fd, temp_name = tempfile.mkstemp(prefix=f".{name}.", suffix=".tmp", dir=directory)
    try:
        with os.fdopen(fd, "wb") as handle:
            fd = None
            handle.write(text.encode("utf-8"))
            handle.flush()
            os.fsync(handle.fileno())
        if mode is not None:
            os.chmod(temp_name, stat.S_IMODE(mode))
        os.replace(safe_path(temp_name), safe_path(absolute))
    finally:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
        try:
            os.unlink(safe_path(temp_name))
        except OSError:
            pass
