"""Private, in-memory reuse of scan-time EML sources for one fresh draft scan.

Issue #103 P2: the fresh draft scan already exports each candidate's RFC-822
source for its attachment inventory.  That exact byte sequence may be reused
for the immediate draft processing (full-text escalation and attachment
evaluation) instead of a second export, but only under a closed identity and a
hard budget:

* identity is the internal scan id (one fresh scan), the effective account
  (including the default case), the exact folder, the canonical locator
  (envelope id) and the source's own, unique, normalized Message-ID; the bound
  ID must equal the Message-ID the EML itself carries (missing, duplicated,
  unparsable or deviating → no bind);
* any missing, duplicated, ambiguous or deviating binding yields ``None`` and
  the caller keeps its regular reader;
* per source at most :data:`MAX_RAW_EML_BYTES`, in total at most
  ``target_count * MAX_RAW_EML_BYTES`` and never more than ``target_count``
  held sources; an over-budget source is dropped, never truncated.  The scan
  export itself stays unbounded, so an oversized item keeps its normal
  attachment inventory and only its source is dropped;
* the lifetime is exactly one fresh scan plus its immediate draft processing.
  :meth:`ScanSourceContext.clear` drops every byte and every metadata entry, and
  nothing is persisted, serialized or shared through global state.

The context deliberately is not a mapping and refuses all further binds after
``clear`` so a finished scan can never hand a source to a later run.
"""

from __future__ import annotations

import secrets
from typing import Any, Iterable, Mapping

from .common import normalize_message_id
from .draft_raw_eml import MAX_RAW_EML_BYTES
from .himalaya import eml_own_message_id


def _normalize_account(account: str | None) -> str:
    """Return the effective account (``None``/blank is the default account case)."""
    return str(account or "").strip()


def _normalize_folder(folder: str | None) -> str:
    """Return the exact folder name; a blank folder means ``INBOX``."""
    return str(folder or "INBOX").strip() or "INBOX"


def _positive_int(value: Any, name: str, *, allow_zero: bool = False) -> int:
    minimum = 0 if allow_zero else 1
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ValueError(f"{name} must be an integer >= {minimum}")
    return value


class ScanSourceContext:
    """One private, non-serializable in-memory source store for a single scan.

    Only :meth:`lookup` returns bytes, and only for an exact, unambiguous
    identity match.  ``repr``/``str`` expose counts, never content.
    """

    __slots__ = (
        "_scan_id",
        "_max_eml_bytes",
        "_max_total_bytes",
        "_max_sources",
        "_sources",
        "_locators",
        "_message_ids",
        "_conflicted_locators",
        "_conflicted_messages",
        "_held_bytes",
        "_cleared",
    )

    def __init__(
        self,
        *,
        target_count: int,
        max_eml_bytes: int = MAX_RAW_EML_BYTES,
        max_total_bytes: int | None = None,
        max_sources: int | None = None,
        scan_id: str | None = None,
    ) -> None:
        count = _positive_int(target_count, "target_count")
        self._scan_id = scan_id or secrets.token_hex(16)
        self._max_eml_bytes = _positive_int(max_eml_bytes, "max_eml_bytes")
        self._max_total_bytes = _positive_int(
            count * self._max_eml_bytes if max_total_bytes is None else max_total_bytes,
            "max_total_bytes",
            allow_zero=True,
        )
        self._max_sources = _positive_int(
            count if max_sources is None else max_sources, "max_sources", allow_zero=True
        )
        self._sources: dict[tuple[str, str, str, str], bytes] = {}
        self._locators: dict[tuple[str, str, str], str] = {}
        self._message_ids: dict[tuple[str, str], tuple[str, str, str, str]] = {}
        self._conflicted_locators: set[tuple[str, str, str]] = set()
        self._conflicted_messages: set[tuple[str, str]] = set()
        self._held_bytes = 0
        self._cleared = False

    # ------------------------------------------------------------------ capture

    @property
    def scan_id(self) -> str:
        """Return the non-persisted identity of this one scan."""
        return self._scan_id

    @property
    def max_eml_bytes(self) -> int:
        """Return the configured per-source byte bound (10 MiB by default)."""
        return self._max_eml_bytes

    @property
    def held_count(self) -> int:
        """Return the number of currently held sources."""
        return len(self._sources)

    @property
    def held_bytes(self) -> int:
        """Return the number of currently held source bytes."""
        return self._held_bytes

    def __len__(self) -> int:
        return len(self._sources)

    def __repr__(self) -> str:
        return (
            f"<ScanSourceContext held={len(self._sources)} bytes={self._held_bytes} "
            f"cleared={self._cleared}>"
        )

    def bind(
        self,
        envelope_id: str | int | None,
        folder: str | None,
        account: str | None,
        message_id: str | None,
        eml: bytes,
    ) -> bool:
        """Hold one source for an exact identity; return whether it was accepted.

        A duplicate identity, the same locator with another Message-ID or the
        same Message-ID on another locator poisons every involved binding: the
        affected sources are dropped and later lookups return ``None``.
        """
        if self._cleared:
            return False
        locator, message_key, key, normalized_id = self._identity(
            envelope_id, folder, account, message_id
        )
        if locator is None or message_key is None or key is None or not normalized_id:
            return False
        if not isinstance(eml, (bytes, bytearray)) or not eml:
            return False
        payload = bytes(eml)
        if len(payload) > self._max_eml_bytes:
            return False
        # The source must carry exactly the Message-ID it is bound under; a
        # foreign, missing, duplicated or unparsable ID never binds.
        if eml_own_message_id(payload) != normalized_id:
            return False
        if locator in self._conflicted_locators or message_key in self._conflicted_messages:
            return False

        existing_id = self._locators.get(locator)
        if existing_id is not None and existing_id != normalized_id:
            self._conflict_locator(locator)
            self._conflict_message((locator[0], existing_id))
            self._conflict_message(message_key)
            return False
        existing_key = self._message_ids.get(message_key)
        if existing_key is not None and existing_key != key:
            self._conflict_message(message_key)
            self._conflict_locator(existing_key[:3])
            self._conflict_locator(locator)
            return False
        if key in self._sources:
            self._conflict_locator(locator)
            self._conflict_message(message_key)
            return False
        if len(self._sources) >= self._max_sources:
            return False
        if self._held_bytes + len(payload) > self._max_total_bytes:
            return False

        self._sources[key] = payload
        self._locators[locator] = normalized_id
        self._message_ids[message_key] = key
        self._held_bytes += len(payload)
        return True

    def lookup(
        self,
        envelope_id: str | int | None,
        folder: str | None,
        account: str | None,
        message_id: str | None,
    ) -> bytes | None:
        """Return the held source only for the exact, unambiguous identity."""
        if self._cleared:
            return None
        locator, message_key, key, normalized_id = self._identity(
            envelope_id, folder, account, message_id
        )
        if locator is None or message_key is None or key is None or not normalized_id:
            return None
        if locator in self._conflicted_locators or message_key in self._conflicted_messages:
            return None
        if self._locators.get(locator) != normalized_id:
            return None
        if self._message_ids.get(message_key) != key:
            return None
        return self._sources.get(key)

    # ----------------------------------------------------------------- lifecycle

    def discard(self, envelope_id: str | int | None, folder: str | None, account: str | None) -> None:
        """Drop the one source held for this locator (known/skipped/quarantined item)."""
        locator = (
            _normalize_account(account),
            _normalize_folder(folder),
            str(envelope_id or "").strip(),
        )
        for key in [key for key in self._sources if key[:3] == locator]:
            self._drop(key)

    def retain_selected(
        self,
        emails: Iterable[Mapping[str, Any]],
        *,
        account: str | None = None,
    ) -> None:
        """Keep only the sources bound to the final, clean selection of the scan."""
        selected: set[tuple[str, str, str, str]] = set()
        for email in emails:
            if not isinstance(email, Mapping):
                continue
            locator, _message_key, key, normalized_id = self._identity(
                email.get("envelope_id"), email.get("folder"), account, email.get("message_id")
            )
            if locator is not None and key is not None and normalized_id:
                selected.add(key)
        for key in [key for key in self._sources if key not in selected]:
            self._drop(key)

    def clear(self) -> None:
        """Drop every byte and every metadata entry; the context stays closed."""
        for key in list(self._sources):
            self._drop(key)
        self._sources.clear()
        self._locators.clear()
        self._message_ids.clear()
        self._conflicted_locators.clear()
        self._conflicted_messages.clear()
        self._held_bytes = 0
        self._cleared = True

    # ------------------------------------------------------------------ internal

    def _identity(
        self,
        envelope_id: str | int | None,
        folder: str | None,
        account: str | None,
        message_id: str | None,
    ) -> tuple[
        tuple[str, str, str] | None,
        tuple[str, str] | None,
        tuple[str, str, str, str] | None,
        str,
    ]:
        locator_value = str(envelope_id or "").strip()
        normalized_id = normalize_message_id(str(message_id or ""))
        if not locator_value:
            return None, None, None, ""
        normalized_account = _normalize_account(account)
        normalized_folder = _normalize_folder(folder)
        locator = (normalized_account, normalized_folder, locator_value)
        message_key = (normalized_account, normalized_id)
        key = (normalized_account, normalized_folder, locator_value, normalized_id)
        return locator, message_key, key, normalized_id

    def _conflict_locator(self, locator: tuple[str, str, str]) -> None:
        self._conflicted_locators.add(locator)
        normalized_id = self._locators.get(locator)
        if normalized_id is not None:
            self._drop((*locator, normalized_id))

    def _conflict_message(self, message_key: tuple[str, str]) -> None:
        self._conflicted_messages.add(message_key)
        key = self._message_ids.get(message_key)
        if key is not None:
            self._drop(key)

    def _drop(self, key: tuple[str, str, str, str]) -> None:
        payload = self._sources.pop(key, None)
        if payload is not None:
            self._held_bytes -= len(payload)
        locator = key[:3]
        if self._locators.get(locator) == key[3]:
            self._locators.pop(locator, None)
        message_key = (key[0], key[3])
        if self._message_ids.get(message_key) == key:
            self._message_ids.pop(message_key, None)


__all__ = ["ScanSourceContext"]
