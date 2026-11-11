"""Fail-closed known-envelope skip accelerator for one mailbox scan.

The accelerator reads one ``himalaya envelope thread -o json`` per scan and
derives a process-local mapping ``envelope id -> normalized Message-ID``.  It
only ever *suppresses* a per-envelope read; it never produces mail data.  The
final-location index remains the sole skip authority (issue #66), so a mapping
hit must also be present there.

Every acceptance rule is fail-closed (issue #103, F1-F3):

* F1: the map comes from exactly one successful process with valid JSON that is
  a JSON array of ``[source, target, weight]`` triples (Himalaya's serialized
  ``ThreadedEnvelopes`` graph edges).  The whole root is discarded when any
  element is not a three-element list, either envelope position is not an
  object, or the weight is not an integer in ``0..255``.
* F2: an id is decimal and not ``0``, with no surrounding whitespace; a node
  carries exactly one well-formed Message-ID.  Well-formedness is checked on ``normalize_message_id`` output
  (one ``@``, no whitespace/control characters, no angle brackets, non-empty
  local/domain parts, domain not ``generated``).  A repeated id is kept only
  when every occurrence is valid and identical; a conflicting or invalid
  duplicate drops the id entirely.
* F3: a mapping is bound only when the ``envelope list`` envelope has the same
  id **and** the identical subject; otherwise the envelope is read individually.
* F5: a missing id is unknown, never "new" or "known"; it is read individually.
"""

from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass
from typing import Any, Callable

from .common import normalize_message_id
from .himalaya import TRANSPORT_BLOCKED_REASON, HimalayaInvocationError

#: One process per scan; the observed Gate-0 duration was well below this bound.
THREAD_MAP_TIMEOUT_SECONDS = 60

#: ``ThreadedEnvelopes`` serializes its graph edges as ``(source, target, weight)``
#: with a ``u8`` weight; anything outside that range is not this contract.
MAX_THREAD_EDGE_WEIGHT = 255

_DECIMAL_ID = re.compile(r"[0-9]+")


@dataclass(frozen=True)
class EnvelopeThreadEntry:
    """One validated ``id -> Message-ID`` binding with its subject."""

    message_id: str
    subject: str


def is_well_formed_message_id(value: Any) -> bool:
    """Return whether ``value`` is a well-formed Message-ID after normalization.

    The check deliberately runs on the normalized form: ``message read`` emits
    the header without angle brackets while ``envelope thread`` wraps it in
    ``<...>``.  Both spellings are therefore accepted, and only the normalized
    value must be well formed (issue #103 follow-up).
    """
    if not isinstance(value, str):
        return False
    normalized = normalize_message_id(value)
    if not normalized or normalized.count("@") != 1:
        return False
    if any(
        char in "<>" or char.isspace() or unicodedata.category(char).startswith("C")
        for char in normalized
    ):
        return False
    local, domain = normalized.split("@")
    if not local or not domain:
        return False
    return domain != "generated"


def canonical_envelope_id(value: Any) -> str | None:
    """Return the validated decimal envelope id (zero variants excluded) or ``None``.

    F2 accepts only a decimal id without surrounding whitespace, and F3 binds
    only on the *identical* id from both CLI outputs.  The string is therefore
    preserved verbatim (no ``strip()``, no leading-zero rewrite): ``" 7"`` is
    invalid, ``"0007"`` never binds to ``"7"``.  No integer conversion is used,
    so IDs longer than Python's 4300-digit ``int()`` limit stay safe.  All-zero
    spellings (``"0"``, ``"00"``) are rejected fail-closed.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        try:
            text = str(value)
        except ValueError:
            # A Python int beyond the str-conversion digit limit is not a
            # usable CLI locator; fail closed instead of raising mid-scan.
            return None
    elif isinstance(value, str):
        text = value
    else:
        return None
    if not text or not _DECIMAL_ID.fullmatch(text):
        return None
    if not text.strip("0"):
        return None
    return text


def _node_entry(node: dict[str, Any]) -> EnvelopeThreadEntry | None:
    """Return the validated identity of one graph node, or ``None``.

    Only typed fields contribute.  A node object is not searched for nested
    lookalikes; the edge structure is already validated separately (F1).
    """
    raw_message_id = node.get("message-id")
    subject = node.get("subject")
    if not is_well_formed_message_id(raw_message_id) or not isinstance(subject, str):
        return None
    return EnvelopeThreadEntry(normalize_message_id(raw_message_id), subject)


def _record_node(
    node: dict[str, Any],
    mapping: dict[str, EnvelopeThreadEntry],
    seen: dict[str, EnvelopeThreadEntry | None],
) -> None:
    envelope_id = canonical_envelope_id(node.get("id"))
    if envelope_id is None:
        return
    if envelope_id in seen and seen[envelope_id] is None:
        # Already ambiguous or seen only in invalid form: stay excluded.
        return
    entry = _node_entry(node)
    if envelope_id not in seen:
        seen[envelope_id] = entry
        if entry is not None:
            mapping[envelope_id] = entry
        return
    previous = seen[envelope_id]
    if previous is None or entry is None or previous != entry:
        # Ambiguous identity (including an invalid duplicate): drop the id,
        # never resolve it by traversal order.
        seen[envelope_id] = None
        mapping.pop(envelope_id, None)


def _validated_edges(payload: Any) -> list[list[Any]] | None:
    """Return the triple edges of a valid root, or ``None`` (F1)."""
    if not isinstance(payload, list):
        return None
    for edge in payload:
        if not isinstance(edge, list) or len(edge) != 3:
            return None
        source, target, weight = edge
        if not isinstance(source, dict) or not isinstance(target, dict):
            return None
        if isinstance(weight, bool) or not isinstance(weight, int):
            return None
        if not 0 <= weight <= MAX_THREAD_EDGE_WEIGHT:
            return None
    return payload


def parse_envelope_thread_map(payload: Any) -> dict[str, EnvelopeThreadEntry]:
    """Validate ``envelope thread -o json`` output and return the skip map.

    F1: the root must be a list of ``[source, target, weight]`` triples; a
    malformed root discards the whole map.  F2: invalid nodes are skipped and
    any repeated id is kept only when all occurrences are valid and identical.
    An empty map is valid and simply disables the accelerator.
    """
    edges = _validated_edges(payload)
    if edges is None:
        return {}
    mapping: dict[str, EnvelopeThreadEntry] = {}
    seen: dict[str, EnvelopeThreadEntry | None] = {}
    for source, target, _weight in edges:
        _record_node(source, mapping, seen)
        _record_node(target, mapping, seen)
    return mapping


def load_envelope_thread_map(
    folder: str,
    *,
    account: str | None,
    run_himalaya_fn: Callable[..., str],
    timeout: int = THREAD_MAP_TIMEOUT_SECONDS,
) -> dict[str, EnvelopeThreadEntry]:
    """Run exactly one bounded ``envelope thread`` process and build the map.

    A non-zero exit, timeout, malformed JSON or invalid structure discards the
    whole map (F1).  A ``mailbox_transport_blocked`` abort is never swallowed:
    it ends the scan without another process start (F6).
    """
    try:
        raw = run_himalaya_fn(
            ["envelope", "thread", "-f", folder, "-o", "json"],
            account=account,
            timeout=timeout,
            max_retries=1,
        )
    except HimalayaInvocationError as exc:
        if exc.reason_code == TRANSPORT_BLOCKED_REASON:
            raise
        return {}
    except Exception:  # noqa: BLE001 - any map failure only disables the accelerator
        return {}
    if not isinstance(raw, str):
        return {}
    try:
        payload = json.loads(raw.strip())
    except (TypeError, ValueError):
        return {}
    return parse_envelope_thread_map(payload)


def thread_map_hit(
    thread_map: dict[str, EnvelopeThreadEntry],
    envelope: dict[str, Any],
) -> EnvelopeThreadEntry | None:
    """Return the bound map entry for one listed envelope, or ``None``.

    Binding requires the canonical decimal id and the identical subject from
    both CLI outputs (F3).  A missing id or subject mismatch is unknown and the
    caller must read the envelope individually (F5).
    """
    entry = thread_map.get(canonical_envelope_id(envelope.get("id")))
    if entry is None:
        return None
    subject = envelope.get("subject")
    if not isinstance(subject, str) or subject != entry.subject:
        return None
    return entry
