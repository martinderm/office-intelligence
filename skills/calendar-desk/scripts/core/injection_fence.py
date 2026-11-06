"""Deterministic untrusted calendar fencing and prompt-injection scanner.

Provides fence constants, collision neutralization, envelope wrapping, and
multi-category threat scanning per spec.md §2.5 without external dependencies.
"""

from __future__ import annotations

import copy
import hashlib
import re
from typing import Any, Mapping

FENCE_TAG: str = "untrusted_calendar_content"
FENCE_VERSION: str = "1"
ESCAPED_TAG_PLACEHOLDER: str = "[ESCAPED_UNTRUSTED_TAG]"
TEXT_FIELDS_TO_FENCE: tuple[str, ...] = ("summary", "description", "location")

_TAG_COLLISION_RE = re.compile(
    r"</?(?:untrusted_calendar_content|untrusted_mail_content|untrusted_attachment_content)(?:\s+[^>]*>|>|\b)",
    re.IGNORECASE,
)

_TAG_BREAKOUT_RE = re.compile(
    r"</?(?:untrusted_calendar_content|untrusted_mail_content|untrusted_attachment_content)\b",
    re.IGNORECASE,
)

_PROMPT_INJECTION_PATTERNS = [
    re.compile(r"\bignore\s+(?:all\s+)?(?:previous|prior)\s+instructions\b", re.IGNORECASE),
    re.compile(r"\bsystem\s+prompt\b", re.IGNORECASE),
    re.compile(r"\byou\s+are\s+now\b", re.IGNORECASE),
    re.compile(r"\bignoriere\s+alle\s+bisherigen\s+anweisungen\b", re.IGNORECASE),
    re.compile(r"\bvergiss\s+alle\s+vorherigen\s+instruktionen\b", re.IGNORECASE),
    re.compile(r"\bneue\s+systemanweisung\b", re.IGNORECASE),
    re.compile(r"(?:^|\n)\s*System\s*:", re.IGNORECASE),
    re.compile(r"(?:^|\n)\s*Assistant\s*:", re.IGNORECASE),
    re.compile(r"<\|im_start\|>", re.IGNORECASE),
    re.compile(r"<\|im_end\|>", re.IGNORECASE),
    re.compile(r"\[/?INST\]", re.IGNORECASE),
]

_BIDI_OR_CONTROL_RE = re.compile(
    r"[\u202a-\u202e\u2066-\u2069\u200b-\u200d\ufeff]|[\x00-\x08\x0b\x0c\x0e-\x1f]"
)

_SUSPICIOUS_URL_PATTERNS = [
    re.compile(r"\bjavascript:\S*", re.IGNORECASE),
    re.compile(r"\bdata:\S*", re.IGNORECASE),
    re.compile(r"\bvbscript:\S*", re.IGNORECASE),
    re.compile(r"\bfile://\S*", re.IGNORECASE),
    re.compile(r"https?://[^/\s:@]+:[^/\s:@]+@\S*", re.IGNORECASE),
]


def sanitize_fence_collisions(text: str) -> tuple[str, bool]:
    """Replace spoofed or closing untrusted fence tags with ESCAPED_TAG_PLACEHOLDER."""
    if not text:
        return text, False
    sanitized, count = _TAG_COLLISION_RE.subn(ESCAPED_TAG_PLACEHOLDER, text)
    return sanitized, count > 0


def fence_calendar_text(text: str) -> dict[str, Any]:
    """Wrap raw calendar text in an immutable untrusted_calendar_content envelope.

    Normalizes line endings to LF, computes lowercase 64-hex SHA-256 of the raw
    normalized UTF-8 text, neutralizes fence collisions, and returns the envelope dict.
    """
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    sha256_digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest().lower()
    sanitized, escaped = sanitize_fence_collisions(normalized)
    fenced = (
        f'<{FENCE_TAG} fence_version="{FENCE_VERSION}" sha256="{sha256_digest}">\n'
        f"{sanitized}\n"
        f"</{FENCE_TAG}>"
    )
    return {
        "fenced": fenced,
        "sha256": sha256_digest,
        "escaped_tags": escaped,
    }


def scan_calendar_text(text: str, *, field_name: str = "text") -> list[dict[str, str]]:
    """Scan text for tag-breakouts, prompt-injections, Bidi/control characters, and URLs."""
    if not text:
        return []

    findings: list[dict[str, str]] = []

    # 1. Tag breakout
    m_tag = _TAG_BREAKOUT_RE.search(text)
    if m_tag:
        findings.append({
            "field": field_name,
            "category": "tag_breakout",
            "detail": f"Matched untrusted tag breakout pattern: {m_tag.group(0)}",
        })

    # 2. Prompt injection
    for pattern in _PROMPT_INJECTION_PATTERNS:
        m_inj = pattern.search(text)
        if m_inj:
            findings.append({
                "field": field_name,
                "category": "prompt_injection",
                "detail": f"Matched prompt injection indicator: {m_inj.group(0)}",
            })
            break

    # 3. Control or Bidi characters
    m_bidi = _BIDI_OR_CONTROL_RE.search(text)
    if m_bidi:
        char_hex = hex(ord(m_bidi.group(0)))
        findings.append({
            "field": field_name,
            "category": "control_or_bidi_char",
            "detail": f"Detected unexpected control or Bidi override character: {char_hex}",
        })

    # 4. Suspicious URLs
    for url_pattern in _SUSPICIOUS_URL_PATTERNS:
        m_url = url_pattern.search(text)
        if m_url:
            findings.append({
                "field": field_name,
                "category": "suspicious_url",
                "detail": f"Detected suspicious URL scheme or credentialed host: {m_url.group(0)}",
            })
            break

    return findings


def fence_calendar_record(record: Mapping[str, Any]) -> dict[str, Any]:
    """Scan and fence summary, description, location, and participant display names.

    Does not mutate the input mapping. Returns a result dict with the fenced record copy,
    flagged boolean, and all detected findings.
    """
    rec_copy = copy.deepcopy(dict(record))
    findings: list[dict[str, str]] = []
    fenced_content: dict[str, Any] = {}

    # Fence and scan core text fields
    for field in TEXT_FIELDS_TO_FENCE:
        val = str(rec_copy.get(field, "") or "")
        fenced_content[field] = fence_calendar_text(val)
        findings.extend(scan_calendar_text(val, field_name=field))

    # Scan organizer display name
    organizer = rec_copy.get("organizer")
    if isinstance(organizer, dict):
        cn = str(organizer.get("cn", "") or "")
        if cn:
            findings.extend(scan_calendar_text(cn, field_name="organizer.cn"))
            fenced_content["organizer_cn"] = fence_calendar_text(cn)

    # Scan attendee display names
    attendees = rec_copy.get("attendees")
    attendees_cn: list[dict[str, Any]] = []
    if isinstance(attendees, list):
        for idx, att in enumerate(attendees):
            if isinstance(att, dict):
                att_cn = str(att.get("cn", "") or "")
                if att_cn:
                    findings.extend(scan_calendar_text(att_cn, field_name=f"attendees[{idx}].cn"))
                    attendees_cn.append(fence_calendar_text(att_cn))
    if attendees_cn:
        fenced_content["attendees_cn"] = attendees_cn

    flagged = len(findings) > 0
    rec_copy["fenced_content"] = fenced_content
    rec_copy["injection_scan"] = {
        "flagged": flagged,
        "findings": findings,
    }

    return {
        "record": rec_copy,
        "flagged": flagged,
        "findings": findings,
    }
