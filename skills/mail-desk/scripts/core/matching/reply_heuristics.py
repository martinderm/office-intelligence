"""FR-17/MD-R8 canonical reply-heuristics owner.

``needs_reply`` is bound to a concrete request (question, ask, deadline, decision,
approval or contribution) per the desk contract.  This owner owns the closing/thank-you
detection used to downgrade an asserted reply requirement: a pure closing or thank-you
message without any concrete request is never reply-worthy.  The downgrade runs in the
facade after each classification pass (preview and full body), so a reply decision
derived from a preview can be corrected by the full body.  Remaining ambiguity keeps
the documented review semantics instead of a silent ``true``.

FR-18/MD-S1 additionally owns the workspace-bound reply-trigger catalog: the trigger
list and the no-reply sender tokens move out of the facade into the consuming
workspace's desk-signals catalog (``memory/references/mail-desk/mail-desk.json``,
schema 1).  A missing catalog keeps the documented compatibility defaults; an invalid
catalog fails loud with ``ValueError`` instead of silently falling back.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path


#: Closing/thank-you markers.  Only matched when the message carries no concrete
#: request signal -- the presence of a request keeps review semantics intact.
_CLOSING_MARKERS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\bvielen\s+dank\b", re.IGNORECASE),
    re.compile(r"\bdankesch[öo]n\b", re.IGNORECASE),
    re.compile(r"\bherzlichen\s+dank\b", re.IGNORECASE),
    re.compile(r"\bthanks?\b", re.IGNORECASE),
    re.compile(r"\bthank\s+you\b", re.IGNORECASE),
    re.compile(r"\bpasst\s+f(?:ü|ue)r\s+mich\b", re.IGNORECASE),
    re.compile(r"\bsieht\s+gut\s+aus\b", re.IGNORECASE),
)

#: Explicit courtesy-only closings with no informational content beyond farewell.
_FAREWELL_MARKERS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\bliebe\s+gr(?:ü|ue)(?:ß|ss)e\b", re.IGNORECASE),
    re.compile(r"\bbeste\s+gr(?:ü|ue)(?:ß|ss)e\b", re.IGNORECASE),
    re.compile(r"\bkind\s+regards\b", re.IGNORECASE),
    re.compile(r"\bbest\s+regards\b", re.IGNORECASE),
)

#: A concrete request signal: any question, ask, deadline, decision, approval or
#: contribution.  Matched over the same text; a hit forbids the downgrade.
_REQUEST_SIGNAL: re.Pattern[str] = re.compile(
    r"\b(?:"
    r"please|kindly|could\s+you|can\s+you|would\s+you|"
    r"bitte|kannst\s+du|k(?:ö|oe)nnen\s+sie|kann\s+sie|"
    r"frage|question|"
    r"bis\s+(?:zum\s+)?\d{1,2}\.\d{1,2}\.|"
    r"deadline|frist|bis\s+(?:ende|freitag|montag|dienstag|mittwoch|donnerstag|samstag|sonntag|morgen|nächste|kommende)|"
    r"entscheidung|decision|freigabe|approval|zustimmung|"
    r"beitrag|contribution|feedback|kommentar|comment|"
    r"rückmeldung|antwort|response|senden|schicken|liefern|ergänzen|bestätigen"
    r")\b",
    re.IGNORECASE,
)


def _contains_request_signal(text: str) -> bool:
    return bool(_REQUEST_SIGNAL.search(text))


#: Start of a quoted predecessor thread.  Everything from the first matching line
#: on is history and never a request of the current mail.
_QUOTE_BOUNDARY: tuple[re.Pattern[str], ...] = (
    re.compile(r"^\s*>"),
    re.compile(r"^\s*_{10,}\s*$"),
    re.compile(r"^\s*-{5,}\s*(?:original|urspr(?:ü|ue)ngliche)", re.IGNORECASE),
    re.compile(r"^\s*(?:am|on)\s+.{3,120}?(?:schrieb|wrote)\b.*:\s*$", re.IGNORECASE),
)


def _strip_quoted_history(text: str) -> str:
    """Return only the newly written part of a plain-text mail body.

    The closing/request decision must consider the mail's own content.  Quoted
    ``>``/``>>>`` blocks, header separators and ``Am ... schrieb`` blocks are
    predecessor history and must not keep an asserted reply requirement alive.
    """
    kept: list[str] = []
    for line in str(text or "").splitlines():
        if any(marker.search(line) for marker in _QUOTE_BOUNDARY):
            break
        kept.append(line)
    return "\n".join(kept).strip()


def is_closing_or_thanks(text: str) -> bool:
    """True when the text is a pure closing/thank-you message without any request.

    Only the newly written part counts: a quoted predecessor with a request or a
    closing stays history.
    """
    normalized = _strip_quoted_history(text)
    if not normalized:
        return False
    return not _contains_request_signal(normalized) and bool(
        any(marker.search(normalized) for marker in _CLOSING_MARKERS)
        or any(marker.search(normalized) for marker in _FAREWELL_MARKERS)
    )


def needs_reply_review(text: str) -> bool:
    """True when the text still carries a concrete request signal (review semantics).

    Quoted predecessor history is ignored, matching ``is_closing_or_thanks``.
    """
    normalized = _strip_quoted_history(text)
    if not normalized:
        return False
    return _contains_request_signal(normalized)


def downgrade_if_closing(
    decision: dict,
    notes: str,
    *,
    subject: str = "",
    body: str = "",
) -> tuple[dict, str]:
    """Downgrade one asserted ``needs_reply`` to ``false`` for a pure closing mail.

    Returns the (possibly unchanged) decision and notes.  Only a currently asserted
    truthy ``needs_reply`` is affected; already-false values and review semantics
    stay untouched.  ``subject``/``body`` carry the closing-check text of the
    effective source (supplied explicitly by the facade; they never enter the
    decision dict).
    """
    if not decision.get("needs_reply"):
        return decision, notes
    combined = f"{str(subject or '')}\n{str(body or '')}".strip()
    if not combined or not is_closing_or_thanks(combined):
        return decision, notes
    downgraded = dict(decision)
    downgraded["needs_reply"] = False
    downgraded["reply_downgrade"] = {
        "reason": "closing_or_thanks",
        "rule_revision": "md-r8",
    }
    suffix = " (MD-R8: Abschluss-/Dankesmail ohne konkrete Anforderung; needs_reply herabgestuft)"
    if suffix not in notes:
        notes = f"{notes}{suffix}"
    return downgraded, notes


# ---------------------------------------------------------------------------
# FR-18/MD-S1: workspace-bound reply-trigger catalog
# ---------------------------------------------------------------------------

#: Pre-MD-S1 hardcoded compatibility defaults, kept verbatim for workspaces that
#: have no desk-signals catalog yet.
DEFAULT_REPLY_TRIGGERS: tuple[str, ...] = (
    "martin bitte",
    "bitte martin",
    "frage an martin",
    "hallo martin",
    "lieber martin",
    "martin kannst du",
    "martin ?",
    "martin, bitte",
    "@martin",
)

DEFAULT_NO_REPLY_SENDER_TOKENS: tuple[str, ...] = (
    "no-reply",
    "do_not_reply",
    "quarantine",
    "mailer-daemon",
)

#: Desk-signals catalog location, relative to the consuming workspace root.
REPLY_HEURISTICS_CATALOG_RELATIVE_PATH = Path("memory") / "references" / "mail-desk" / "mail-desk.json"

#: The only supported desk-signals catalog schema revision.
REPLY_HEURISTICS_SCHEMA_VERSION = 1

_WORD_CHARACTER: re.Pattern[str] = re.compile(r"\w")


@dataclass(frozen=True)
class ReplyHeuristicsConfig:
    """Immutable workspace reply-heuristics catalog (FR-18/MD-S1)."""

    reply_triggers: tuple[str, ...]
    no_reply_sender_tokens: tuple[str, ...]
    owner_address: str | None = None


def _catalog_error(path: Path, detail: str) -> ValueError:
    return ValueError(f"Invalid mail-desk reply-heuristics catalog at {path}: {detail}")


def _string_tuple(path: Path, value: object, field: str) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise _catalog_error(path, f"'{field}' must be a list of non-empty strings")
    items: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise _catalog_error(path, f"'{field}' must contain only non-empty strings")
        items.append(item)
    return tuple(items)


def load_reply_heuristics(workspace_root: Path) -> ReplyHeuristicsConfig:
    """Load the workspace desk-signals reply-heuristics catalog, or the defaults.

    A missing catalog file yields the documented pre-MD-S1 compatibility defaults.  An
    existing catalog must be schema-1 JSON with a non-empty ``reply_heuristics.reply_triggers``
    list; any schema drift or malformed value raises ``ValueError`` -- never a silent
    fallback.  ``no_reply_sender_tokens`` and ``owner_address`` are optional.
    """
    path = Path(workspace_root) / REPLY_HEURISTICS_CATALOG_RELATIVE_PATH
    if not path.exists():
        return ReplyHeuristicsConfig(
            reply_triggers=DEFAULT_REPLY_TRIGGERS,
            no_reply_sender_tokens=DEFAULT_NO_REPLY_SENDER_TOKENS,
            owner_address=None,
        )
    try:
        with path.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise _catalog_error(path, f"unreadable or malformed JSON ({exc})") from exc
    if not isinstance(data, dict):
        raise _catalog_error(path, "top-level value must be a JSON object")
    schema_version = data.get("schema_version")
    # Strict type gate: ``True == 1`` and ``1.0 == 1`` would pass a loose ``==``
    # comparison, so a bool/float/str/None schema_version must fail loud instead of
    # silently masquerading as schema 1 (FR-18 drift invariant).
    if (
        isinstance(schema_version, bool)
        or not isinstance(schema_version, int)
        or schema_version != REPLY_HEURISTICS_SCHEMA_VERSION
    ):
        raise _catalog_error(
            path,
            f"unsupported schema_version {schema_version!r}; "
            f"expected {REPLY_HEURISTICS_SCHEMA_VERSION}",
        )
    block = data.get("reply_heuristics")
    if not isinstance(block, dict):
        raise _catalog_error(path, "'reply_heuristics' must be a JSON object")
    raw_triggers = block.get("reply_triggers")
    if not isinstance(raw_triggers, list) or not raw_triggers:
        raise _catalog_error(
            path,
            "'reply_heuristics.reply_triggers' is required and must be a non-empty list of strings",
        )
    reply_triggers = _string_tuple(path, raw_triggers, "reply_triggers")
    if "no_reply_sender_tokens" in block:
        no_reply_sender_tokens = _string_tuple(
            path, block["no_reply_sender_tokens"], "no_reply_sender_tokens"
        )
    else:
        no_reply_sender_tokens = DEFAULT_NO_REPLY_SENDER_TOKENS
    owner_address = block.get("owner_address")
    if owner_address is not None and not isinstance(owner_address, str):
        raise _catalog_error(path, "'owner_address' must be a string or null")
    return ReplyHeuristicsConfig(
        reply_triggers=reply_triggers,
        no_reply_sender_tokens=no_reply_sender_tokens,
        owner_address=owner_address,
    )


@lru_cache(maxsize=256)
def _compile_trigger(trigger: str) -> re.Pattern[str]:
    leading = r"(?<!\w)" if _WORD_CHARACTER.match(trigger[0]) else ""
    trailing = r"(?!\w)" if _WORD_CHARACTER.match(trigger[-1]) else ""
    return re.compile(f"{leading}{re.escape(trigger)}{trailing}", re.IGNORECASE)


def matches_reply_trigger(text: str, triggers: Iterable[str]) -> bool:
    """Case-insensitive, word-boundary aware reply-trigger predicate (FR-18/MD-S1).

    A word-character edge of a trigger requires a word boundary, so a bare substring
    inside a longer word (``"Smartin bitte"``) never matches.  A trigger with a
    non-word edge (``"@martin"``, ``"martin ?"``) keeps that literal edge instead.
    """
    haystack = str(text or "")
    if not haystack:
        return False
    for trigger in triggers or ():
        if isinstance(trigger, str) and trigger and _compile_trigger(trigger).search(haystack):
            return True
    return False
