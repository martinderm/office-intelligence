"""FR-17/MD-R8 canonical reply-heuristics owner.

``needs_reply`` is bound to a concrete request (question, ask, deadline, decision,
approval or contribution) per the desk contract.  This owner owns the closing/thank-you
detection used to downgrade an asserted reply requirement: a pure closing or thank-you
message without any concrete request is never reply-worthy.  The downgrade runs in the
facade after each classification pass (preview and full body), so a reply decision
derived from a preview can be corrected by the full body.  Remaining ambiguity keeps
the documented review semantics instead of a silent ``true``.
"""

from __future__ import annotations

import re


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


def is_closing_or_thanks(text: str) -> bool:
    """True when the text is a pure closing/thank-you message without any request."""
    normalized = str(text or "").strip()
    if not normalized:
        return False
    return not _contains_request_signal(normalized) and bool(
        any(marker.search(normalized) for marker in _CLOSING_MARKERS)
        or any(marker.search(normalized) for marker in _FAREWELL_MARKERS)
    )


def needs_reply_review(text: str) -> bool:
    """True when the text still carries a concrete request signal (review semantics)."""
    normalized = str(text or "").strip()
    if not normalized:
        return False
    return _contains_request_signal(normalized)


def downgrade_if_closing(decision: dict, notes: str) -> tuple[dict, str]:
    """Downgrade one asserted ``needs_reply`` to ``false`` for a pure closing mail.

    Returns the (possibly unchanged) decision and notes.  Only a currently asserted
    truthy ``needs_reply`` is affected; already-false values and review semantics
    stay untouched.
    """
    if not decision.get("needs_reply"):
        return decision, notes
    subject = str(decision.get("_closing_check_subject", "") or "")
    body = str(decision.get("_closing_check_body", "") or "")
    combined = f"{subject}\n{body}".strip()
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