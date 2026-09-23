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
workspace's desk-signals catalog (``memory/references/mail-desk/mail-desk.json``).
A missing catalog yields a **neutral** fallback with an EMPTY ``reply_triggers``
tuple -- "ohne Katalog keine Anrede-Trigger": no personal name is ever an expected
fallback.  ``needs_reply`` stays trigger-false in that case.  An invalid catalog
fails loud with ``ValueError`` instead of silently falling back.

FR-22/MD-ID1 introduces catalog **schema 2**, which removes the identity-bearing
fallback entirely:

* Schema 1 stays valid legacy (BOKU shape): ``reply_triggers`` is required,
  ``owner_address`` optional.
* Schema 2 accepts an ``owner_address`` without an explicit ``reply_triggers`` and
  derives the generic greeting-trigger class from the owner's local part
  (lowercased, first dot-segment; the whole local part when it has no dot) via
  :func:`derive_greeting_triggers`.  Explicit ``reply_triggers`` are used verbatim
  and never mixed with derived ones.
* Schema 2 adds the optional desk-signals fields ``sent_sender_domain_whitelist``,
  ``sent_subject_stopwords``, ``internal_domains`` and ``spam_sender_allowlist``
  (each a list of non-empty strings); absent fields fall back to the documented
  defaults that mirror today's hardcoded constants.
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

#: Greeting-trigger derivation class (FR-22/MD-ID1).  ``{X}`` is replaced by the
#: owner local part's first dot-segment (the whole local part when it has no dot).
#: This is a **documentation-only** template for the owner-derived triggers -- it is
#: deliberately NOT a runtime fallback trigger list, so no personal name remains an
#: expected fallback.  A missing catalog yields an EMPTY ``reply_triggers`` tuple.
GREETING_TRIGGER_TEMPLATES: tuple[str, ...] = (
    "{X} bitte",
    "bitte {X}",
    "frage an {X}",
    "hallo {X}",
    "lieber {X}",
    "{X} kannst du",
    "{X} ?",
    "{X}, bitte",
    "@{X}",
)

DEFAULT_NO_REPLY_SENDER_TOKENS: tuple[str, ...] = (
    "no-reply",
    "do_not_reply",
    "quarantine",
    "mailer-daemon",
)

#: Schema-2 optional desk-signals defaults, mirroring today's hardcoded constants
#: (``core/sent_indexer.py``: sender-domain set at the sent-candidate check and the
#: subject stopword set used for keyword extraction).
DEFAULT_SENT_SENDER_DOMAIN_WHITELIST: tuple[str, ...] = (
    "boku.ac.at",
    "gmail.com",
    "yahoo.com",
    "hotmail.com",
)
DEFAULT_SENT_SUBJECT_STOPWORDS: tuple[str, ...] = (
    "antwort",
    "betreff",
    "anfrage",
    "update",
    "fwd",
    "wtrlt",
    "2025",
    "2026",
    "boku",
    "mail",
)
DEFAULT_INTERNAL_DOMAINS: tuple[str, ...] = ("boku.ac.at",)
DEFAULT_SPAM_SENDER_ALLOWLIST: tuple[str, ...] = ()

#: Desk-signals catalog location, relative to the consuming workspace root.
REPLY_HEURISTICS_CATALOG_RELATIVE_PATH = Path("memory") / "references" / "mail-desk" / "mail-desk.json"

#: Desk-signals catalog schema revisions.  Schema 1 is the legacy BOKU shape
#: (``reply_triggers`` required); schema 2 makes ``reply_triggers`` optional when an
#: ``owner_address`` is present and adds the optional desk-signals fields.
REPLY_HEURISTICS_SCHEMA_VERSION = 1
REPLY_HEURISTICS_SCHEMA_VERSION_OWNER_DERIVED = 2
SUPPORTED_REPLY_HEURISTICS_SCHEMA_VERSIONS: tuple[int, ...] = (
    REPLY_HEURISTICS_SCHEMA_VERSION,
    REPLY_HEURISTICS_SCHEMA_VERSION_OWNER_DERIVED,
)

_WORD_CHARACTER: re.Pattern[str] = re.compile(r"\w")


@dataclass(frozen=True)
class ReplyHeuristicsConfig:
    """Immutable workspace reply-heuristics catalog (FR-18/MD-S1, FR-22/MD-ID1).

    ``reply_triggers`` is empty for the neutral missing-catalog fallback and holds
    either the explicit catalog triggers or the owner-derived greeting class.
    The four schema-2 desk-signals fields default to the documented constants that
    mirror today's hardcoded values and are parsed on schema 1 as well.
    """

    reply_triggers: tuple[str, ...]
    no_reply_sender_tokens: tuple[str, ...]
    owner_address: str | None = None
    sent_sender_domain_whitelist: tuple[str, ...] = DEFAULT_SENT_SENDER_DOMAIN_WHITELIST
    sent_subject_stopwords: tuple[str, ...] = DEFAULT_SENT_SUBJECT_STOPWORDS
    internal_domains: tuple[str, ...] = DEFAULT_INTERNAL_DOMAINS
    spam_sender_allowlist: tuple[str, ...] = DEFAULT_SPAM_SENDER_ALLOWLIST


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


def derive_greeting_triggers(owner_address: str) -> tuple[str, ...]:
    """Derive the generic greeting-trigger class from an owner mail address (FR-22/MD-ID1).

    The owner's local part is lowercased and reduced to its first dot-segment (the
    whole local part when it contains no dot); that name fills the
    :data:`GREETING_TRIGGER_TEMPLATES` ``{X}`` placeholder.  The result is the
    schema-2 owner-derived trigger class -- it is never mixed with explicit
    ``reply_triggers``.  An address without a usable local part fails loud.
    """
    local_part = str(owner_address).split("@", 1)[0].strip().lower()
    name = local_part.split(".", 1)[0]
    if not name:
        raise ValueError(
            f"owner_address {owner_address!r} has no usable local part for trigger derivation"
        )
    return tuple(template.replace("{X}", name) for template in GREETING_TRIGGER_TEMPLATES)


def load_reply_heuristics(workspace_root: Path) -> ReplyHeuristicsConfig:
    """Load the workspace desk-signals reply-heuristics catalog, or the neutral fallback.

    A missing catalog file yields the **neutral** fallback: an EMPTY
    ``reply_triggers`` tuple ("ohne Katalog keine Anrede-Trigger") plus the
    documented token/schema-2 defaults.  No personal name is ever an expected
    fallback.

    An existing catalog must be schema-1 or schema-2 JSON.  Schema 1 keeps the
    legacy BOKU shape (a non-empty ``reply_heuristics.reply_triggers`` list is
    required).  Schema 2 makes ``reply_triggers`` optional when an ``owner_address``
    is set -- the greeting-trigger class is then derived from the owner's local part
    (see :func:`derive_greeting_triggers`) -- and fails loud when neither is present.
    Explicit triggers are used verbatim and never mixed with derived ones.  Any
    schema drift or malformed value raises ``ValueError`` -- never a silent fallback.
    """
    path = Path(workspace_root) / REPLY_HEURISTICS_CATALOG_RELATIVE_PATH
    if not path.exists():
        return ReplyHeuristicsConfig(
            reply_triggers=(),
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
    # silently masquerading as a supported revision (FR-18/FR-22 drift invariant).
    if (
        isinstance(schema_version, bool)
        or not isinstance(schema_version, int)
        or schema_version not in SUPPORTED_REPLY_HEURISTICS_SCHEMA_VERSIONS
    ):
        raise _catalog_error(
            path,
            f"unsupported schema_version {schema_version!r}; "
            f"expected one of {list(SUPPORTED_REPLY_HEURISTICS_SCHEMA_VERSIONS)}",
        )
    block = data.get("reply_heuristics")
    if not isinstance(block, dict):
        raise _catalog_error(path, "'reply_heuristics' must be a JSON object")

    owner_address = block.get("owner_address")
    if owner_address is not None and not isinstance(owner_address, str):
        raise _catalog_error(path, "'owner_address' must be a string or null")

    if "reply_triggers" in block:
        raw_triggers = block.get("reply_triggers")
        if not isinstance(raw_triggers, list) or not raw_triggers:
            raise _catalog_error(
                path,
                "'reply_heuristics.reply_triggers' must be a non-empty list of strings",
            )
        reply_triggers = _string_tuple(path, raw_triggers, "reply_triggers")
    elif schema_version == REPLY_HEURISTICS_SCHEMA_VERSION:
        raise _catalog_error(
            path,
            "'reply_heuristics.reply_triggers' is required and must be a non-empty list of strings",
        )
    elif owner_address is not None:
        reply_triggers = derive_greeting_triggers(owner_address)
    else:
        raise _catalog_error(
            path,
            "schema 2 requires either 'reply_heuristics.reply_triggers' or "
            "'reply_heuristics.owner_address'",
        )

    if "no_reply_sender_tokens" in block:
        no_reply_sender_tokens = _string_tuple(
            path, block["no_reply_sender_tokens"], "no_reply_sender_tokens"
        )
    else:
        no_reply_sender_tokens = DEFAULT_NO_REPLY_SENDER_TOKENS

    optional_fields: dict[str, tuple[str, ...]] = {}
    for field, default in (
        ("sent_sender_domain_whitelist", DEFAULT_SENT_SENDER_DOMAIN_WHITELIST),
        ("sent_subject_stopwords", DEFAULT_SENT_SUBJECT_STOPWORDS),
        ("internal_domains", DEFAULT_INTERNAL_DOMAINS),
        ("spam_sender_allowlist", DEFAULT_SPAM_SENDER_ALLOWLIST),
    ):
        if field in block:
            optional_fields[field] = _string_tuple(path, block[field], field)
        else:
            optional_fields[field] = default

    return ReplyHeuristicsConfig(
        reply_triggers=reply_triggers,
        no_reply_sender_tokens=no_reply_sender_tokens,
        owner_address=owner_address,
        sent_sender_domain_whitelist=optional_fields["sent_sender_domain_whitelist"],
        sent_subject_stopwords=optional_fields["sent_subject_stopwords"],
        internal_domains=optional_fields["internal_domains"],
        spam_sender_allowlist=optional_fields["spam_sender_allowlist"],
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
