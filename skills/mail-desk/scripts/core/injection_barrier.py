"""Deterministic, local injection barrier for untrusted mail ingress (Issue #62).

The barrier is a *pre-filter*: it runs before mail content is handed to an
agent context. It never calls the network, never uses a model and has no
runtime dependency outside the Python standard library.

It provides three separable guarantees:

1. **Fencing** -- body/preview text is wrapped in fixed
   ``<untrusted_mail_content>`` delimiters and every attempt to close or spoof
   that delimiter is neutralised. Fencing is not a permission to execute
   content; it is a deterministic data boundary.
2. **Deterministic scan** -- instruction-like phrases, fake role markers,
   active URLs, dangerous attachment patterns, control/bidi/zero-width
   characters and mixed-script homoglyphs are detected with fixed rules.
   Workspace catalogs may only *add* signatures; they can never disable a
   default rule or lower the fixed quarantine threshold.
3. **Quarantine evidence** -- flagged mail is written to the operational
   evidence zone ``data/mail-desk/injection-quarantine/`` as an append-only
   record, a fenced content file and index/observation JSONL lines. No raw
   credentials, no raw MIME and no absolute host paths are persisted.

The module is deliberately independent of ``classifier.py`` and the matching
modules so the ``classifier_revision`` fingerprint does not rotate.
"""

from __future__ import annotations

import hashlib
import html
import ipaddress
import json
import os
import re
import unicodedata
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterable, Mapping
from urllib.parse import urlsplit

from .common import atomic_write_json, atomic_write_text, normalize_message_id, utc_now_iso


INJECTION_BARRIER_SCHEMA_VERSION = 1
FENCE_VERSION = 1
POLICY_VERSION = 1
POLICY_CATALOG_RELATIVE_PATH = Path("memory") / "references" / "mail-desk" / "injection-barrier.json"
QUARANTINE_DIR_NAME = "injection-quarantine"
QUARANTINE_ROOT_RELATIVE_PATH = Path("data") / "mail-desk" / QUARANTINE_DIR_NAME

#: Fixed threshold. Workspace catalogs cannot lower it.
QUARANTINE_THRESHOLD = "medium"
SEVERITY_RANK = {"low": 1, "medium": 2, "high": 3, "critical": 4}

#: Bounds keep the scan deterministic and cheap on adversarial input.
MAX_SCAN_CHARS = 256 * 1024
MAX_FENCE_CHARS = 1024 * 1024
MAX_URLS_PER_FIELD = 200
MAX_ATTACHMENTS = 64
MAX_SAMPLES_PER_RULE_FIELD = 3
MAX_SAMPLE_CHARS = 160
MAX_CATALOG_BYTES = 256 * 1024
MAX_CATALOG_ENTRIES = 200
MAX_CATALOG_ENTRY_CHARS = 200
MAX_RECORD_SUBJECT_CHARS = 500
MAX_RECORD_ADDRESS_CHARS = 320

FENCE_TAG = "untrusted_mail_content"
FENCE_OPEN_PREFIX = f"<{FENCE_TAG} "
FENCE_CLOSE = f"</{FENCE_TAG}>"

#: Existing attachment-handoff delimiters that mail text must never spoof.
_FENCE_SPOOF_RE = re.compile(
    r"</?\s*untrusted_(?:mail|attachment)_content\b[^>]*>|\[ESCAPED_UNTRUSTED_TAG\]",
    re.IGNORECASE,
)
_OWN_FENCE_RE = re.compile(
    rf"\A<{FENCE_TAG}\b(?P<attrs>[^>]*)>\n(?P<body>.*)\n</{FENCE_TAG}>\Z",
    re.DOTALL,
)
_FENCE_SHA_RE = re.compile(r'\bsha256="(?P<sha>[0-9a-f]{64})"', re.IGNORECASE)
_FENCE_INNER_SHA_RE = re.compile(r'\binner_sha256="(?P<sha>[0-9a-f]{64})"', re.IGNORECASE)

_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]")
_BIDI_CHARS = frozenset("\u061c\u200e\u200f\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069")
_ZERO_WIDTH_CHARS = frozenset("\u200b\u200c\u200d\u2060\ufeff")
_TAG_CHAR_RANGE = (0xE0000, 0xE007F)
_LETTER_TOKEN_RE = re.compile(r"[^\W\d_]{2,}", re.UNICODE)

_URL_RE = re.compile(r"(?i)\b(?:https?|ftp)://[^\s<>\"'`]+")
_ACTIVE_SCHEME_RE = re.compile(r"(?i)\b(?:javascript|vbscript|data|file)\s*:")
_MARKDOWN_LINK_RE = re.compile(r"\[([^\]\n]{1,160})\]\((https?://[^)\s]{1,2048})\)", re.IGNORECASE)
_DOMAIN_RE = re.compile(
    r"(?i)\b((?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,24})\b"
)
_IPV4_LIKE_RE = re.compile(r"^\d{1,3}(?:\.\d{1,3}){3}$")
_HTML_ACTIVE_RE = re.compile(
    r"(?i)<\s*(?:script|iframe|object|embed|form)\b"
    r"|\bon[a-z]{3,}\s*="
    r"|javascript\s*:"
    r"|meta\s+http-equiv\s*=\s*[\"']?refresh"
)
_HTML_HIDDEN_RE = re.compile(
    r"(?i)(?:display\s*:\s*none"
    r"|visibility\s*:\s*hidden"
    r"|font-size\s*:\s*0(?:px|pt|em|%)?"
    r"|color\s*:\s*(?:#fff(?:fff)?|white)\b)"
)
_BASE64_BLOB_RE = re.compile(r"[A-Za-z0-9+/]{200,}={0,2}")
_HEX_BLOB_RE = re.compile(r"\b[0-9a-fA-F]{200,}\b")

_TEXT_FIELDS = ("subject", "from", "to", "cc", "preview")

_DEFAULT_SHORTENER_DOMAINS = (
    "bit.ly", "buff.ly", "cutt.ly", "goo.gl", "is.gd", "lnkd.in", "ow.ly",
    "rb.gy", "rebrand.ly", "s.id", "shorturl.at", "t.co", "tinyurl.com",
)

_DEFAULT_DANGEROUS_EXTENSIONS = (
    "apk", "app", "bat", "chm", "class", "cmd", "com", "cpl", "dll", "dmg",
    "exe", "hta", "iso", "img", "jar", "js", "jse", "lnk", "msi", "msp",
    "pif", "ps1", "psm1", "reg", "scr", "sys", "vbe", "vbs", "vhd", "vhdx",
    "wsf", "wsh",
)
_DEFAULT_MACRO_EXTENSIONS = ("docm", "dotm", "potm", "pptm", "xlam", "xlsb", "xlsm", "xltm")
_DEFAULT_HTML_EXTENSIONS = ("htm", "html", "mht", "mhtml", "rtf")
_DEFAULT_ARCHIVE_EXTENSIONS = ("7z", "ace", "bz2", "cab", "gz", "rar", "tar", "tgz", "xz", "zip")
_DANGEROUS_MIME_TYPES = frozenset(
    {
        "application/javascript",
        "application/x-dosexec",
        "application/x-executable",
        "application/x-msdownload",
        "application/x-msdos-program",
        "application/x-sh",
        "application/x-shellscript",
        "text/javascript",
    }
)

#: Fixed default instruction patterns. Each entry is
#: ``(rule_id, severity, category, pattern)``. Workspace catalogs can only add
#: literal phrases; they cannot replace or remove these.
_DEFAULT_INSTRUCTION_PATTERNS: tuple[tuple[str, str, str, str], ...] = (
    (
        "IB-INSTR-001", "high", "instruction",
        r"\bignore\s+(?:all\s+)?(?:the\s+)?(?:previous|prior|above|earlier)\s+"
        r"(?:instructions?|prompts?|rules?|directions?)\b",
    ),
    (
        "IB-INSTR-001", "high", "instruction",
        r"\bdisregard\s+(?:all\s+)?(?:the\s+)?(?:previous|prior|above|earlier)\s+"
        r"(?:instructions?|prompts?|rules?)\b",
    ),
    (
        "IB-INSTR-001", "high", "instruction",
        r"\bforget\s+(?:all\s+)?(?:your\s+)?(?:previous\s+|prior\s+)?"
        r"(?:instructions?|rules?|prompt)\b",
    ),
    (
        "IB-INSTR-001", "high", "instruction",
        r"\b(?:new|updated|revised)\s+(?:system\s+)?(?:instructions?|prompt|rules?)\s*[:\-]",
    ),
    (
        "IB-INSTR-001", "high", "instruction",
        r"\b(?:system|developer|hidden|initial)\s+prompt\b",
    ),
    (
        "IB-INSTR-001", "high", "instruction",
        r"\b(?:reveal|print|show|repeat|output|dump)\s+(?:me\s+)?(?:your\s+)?"
        r"(?:system\s+prompt|hidden\s+instructions?|initial\s+instructions?|"
        r"instructions?\s+above|text\s+above)\b",
    ),
    (
        "IB-INSTR-001", "high", "instruction",
        r"\b(?:do\s+not|don't|never)\s+(?:tell|inform|notify|mention(?:\s+this)?\s+to)\s+"
        r"(?:the\s+)?(?:user|human|operator)\b",
    ),
    (
        "IB-INSTR-001", "high", "instruction",
        r"\bwithout\s+(?:asking|confirmation|approval|telling)\s+"
        r"(?:the\s+)?(?:user|human|operator)\b",
    ),
    (
        "IB-INSTR-001", "high", "instruction",
        r"\b(?:jailbreak|developer\s+mode|dan\s+mode)\b",
    ),
    (
        "IB-INSTR-003", "high", "instruction",
        r"\byou\s+are\s+(?:now\s+)?(?:a|an|the)\s+"
        r"(?:ai|assistant|system|developer|administrator|jailbroken|dan)\b",
    ),
    (
        "IB-INSTR-003", "high", "instruction",
        r"\bact\s+as\s+(?:a|an|the)\s+"
        r"(?:ai|assistant|system|developer|administrator|jailbroken|dan)\b",
    ),
    (
        "IB-INSTR-002", "high", "instruction",
        r"\bignoriere\s+(?:alle\s+)?(?:vorherigen|bisherigen|obigen)\s+"
        r"(?:anweisungen|regeln|befehle)\b",
    ),
    (
        "IB-INSTR-002", "high", "instruction",
        r"\bvergiss\s+(?:alle\s+)?(?:deine\s+)?(?:vorherigen\s+)?(?:anweisungen|regeln)\b",
    ),
    (
        "IB-INSTR-002", "high", "instruction",
        r"\bneue\s+(?:system\s*)?anweisungen\s*[:\-]",
    ),
    (
        "IB-INSTR-002", "high", "instruction",
        r"\b(?:system\s*prompt|systemanweisung)\b",
    ),
    (
        "IB-INSTR-002", "high", "instruction",
        r"\b(?:verrate|zeige|drucke|gib)\s+(?:mir\s+)?(?:deine\s+)?"
        r"(?:anweisungen|systemprompt)\b",
    ),
    (
        "IB-INSTR-002", "high", "instruction",
        r"\b(?:lösche|entferne)\s+(?:alle|sämtliche)\s+"
        r"(?:mails?|nachrichten|dateien|daten)\b",
    ),
    (
        "IB-INSTR-002", "high", "instruction",
        r"\bdu\s+bist\s+(?:jetzt|ab\s+jetzt)\b",
    ),
    (
        "IB-INSTR-004", "medium", "instruction",
        r"\b(?:run|execute)\s+(?:the\s+)?(?:following\s+)?"
        r"(?:command|script|code|shell|powershell|bash)\b",
    ),
    (
        "IB-INSTR-004", "medium", "instruction",
        r"\b(?:send|forward|upload|post|exfiltrate)\s+(?:all|every|the|these|this)\s+"
        r"(?:mails?|e-?mails?|messages?|data|files?|credentials?|passwords?|tokens?)\b"
        r"[^\n]{0,60}\b(?:to|at)\b",
    ),
    (
        "IB-INSTR-004", "medium", "instruction",
        r"\b(?:delete|remove|wipe|erase)\s+(?:all|every)\s+"
        r"(?:messages?|mails?|emails?|files?|data)\b",
    ),
    (
        "IB-INSTR-004", "medium", "instruction",
        r"\b(?:create|add|set\s+up)\s+(?:a\s+)?(?:rule|filter|forwarding)\b",
    ),
    (
        "IB-INSTR-004", "medium", "instruction",
        r"\b(?:mark|move)\s+(?:all|every)\b[^\n]{0,40}\b(?:read|spam|trash)\b",
    ),
    (
        "IB-INSTR-004", "medium", "instruction",
        r"\bohne\s+(?:rückfrage|nachfrage|bestätigung|freigabe)\b[^\n]{0,80}\b"
        r"(?:sende|senden|leite|leiten|lösche|löschen|ändere|ändern|erstelle|erstellen|"
        r"führe|führen)\b",
    ),
    (
        "IB-INSTR-004", "medium", "instruction",
        r"\b(?:sende|senden|leite|leiten|lösche|löschen|ändere|ändern|erstelle|erstellen|"
        r"führe|führen)\b[^\n]{0,80}\bohne\s+"
        r"(?:rückfrage|nachfrage|bestätigung|freigabe)\b",
    ),
    (
        "IB-INSTR-004", "medium", "instruction",
        r"\b(?:sende|leite|schicke)\s+(?:alle|die|diese)\s+"
        r"(?:mails?|e-?mails?|nachrichten|daten|dateien|zugangsdaten|passwörter)\b"
        r"[^\n]{0,60}\b(?:an|weiter)\b",
    ),
    (
        "IB-INSTR-004", "medium", "instruction",
        r"\bführe\s+(?:den\s+)?(?:folgenden\s+)?(?:befehl|code|skript)\s+aus\b",
    ),
    (
        "IB-INSTR-005", "medium", "credential",
        r"\b(?:send|share|provide|paste|reveal|tell)\s+(?:me\s+)?(?:the\s+|your\s+)?"
        r"(?:api[\s_-]?key|password|credentials?|access\s+token|secret)\b",
    ),
    (
        "IB-INSTR-005", "medium", "credential",
        r"\b(?:sende|teile|nenne)\s+(?:mir\s+)?(?:das\s+)?"
        r"(?:passwort|kennwort|zugangsdaten|api[\s_-]?schlüssel)\b",
    ),
    (
        "IB-ROLE-001", "high", "role",
        r"(?im)^\s*(?:<\|?\s*(?:system|assistant|developer)\s*\|?>"
        r"|\[\s*(?:system|assistant|developer)\s*\]"
        r"|###\s*(?:system|assistant|developer))\s*$",
    ),
)

_COMPILED_INSTRUCTION_PATTERNS = tuple(
    (rule_id, severity, category, re.compile(pattern, re.IGNORECASE))
    for rule_id, severity, category, pattern in _DEFAULT_INSTRUCTION_PATTERNS
)


class InjectionBarrierError(ValueError):
    """Raised when the barrier cannot produce a deterministic safe result."""


class InjectionPolicyError(InjectionBarrierError):
    """Raised when a workspace injection-barrier catalog is malformed."""


@dataclass(frozen=True)
class InjectionPolicy:
    """Additive workspace policy; default rules are never removable."""

    version: int = POLICY_VERSION
    source: str = "bundle_defaults"
    catalog_path: str | None = None
    catalog_sha256: str | None = None
    extra_instruction_phrases: tuple[str, ...] = ()
    extra_url_shortener_domains: tuple[str, ...] = ()
    extra_dangerous_extensions: tuple[str, ...] = ()
    extra_macro_extensions: tuple[str, ...] = ()


@dataclass(frozen=True)
class Assessment:
    """Deterministic result of assessing one untrusted mail object."""

    message_key: str
    message_id: str
    envelope_id: str
    folder: str
    subject: str
    from_addr: str
    date: str
    findings: tuple[dict[str, Any], ...]
    max_severity: str | None
    quarantined: bool
    fenced_preview: str
    content_sha256: str
    content_bytes: int
    fenced_sha256: str
    fenced_bytes: int
    transformations: dict[str, int | bool]
    scan_truncated: bool
    policy_version: int
    policy_source: str
    policy_catalog_sha256: str | None

    @property
    def quarantine_id(self) -> str:
        return quarantine_id_for(self.message_key, self.policy_version)

    def as_output_block(self) -> dict[str, Any]:
        """Return the agent-visible per-mail block (no raw content)."""
        block: dict[str, Any] = {
            "schema_version": INJECTION_BARRIER_SCHEMA_VERSION,
            "status": "quarantined" if self.quarantined else "fenced",
            "max_severity": self.max_severity,
            "findings_count": len(self.findings),
            "findings": [dict(finding) for finding in self.findings],
            "content_sha256": self.content_sha256,
            "policy_version": self.policy_version,
        }
        if self.quarantined:
            block["quarantine_id"] = self.quarantine_id
        return block


def default_policy() -> InjectionPolicy:
    """Return the immutable bundle default policy."""
    return InjectionPolicy()


def _dedupe(values: Iterable[str]) -> tuple[str, ...]:
    seen: set[str] = set()
    ordered: list[str] = []
    for value in values:
        if value not in seen:
            seen.add(value)
            ordered.append(value)
    return tuple(ordered)


def _catalog_error(detail: str) -> InjectionPolicyError:
    return InjectionPolicyError(
        f"injection-barrier catalog is invalid: {detail}"
    )


def _catalog_string_list(
    raw: object,
    field: str,
    *,
    normalize: str,
) -> tuple[str, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise _catalog_error(f"{field} must be a list of non-empty strings")
    if len(raw) > MAX_CATALOG_ENTRIES:
        raise _catalog_error(f"{field} exceeds {MAX_CATALOG_ENTRIES} entries")
    values: list[str] = []
    for entry in raw:
        if not isinstance(entry, str):
            raise _catalog_error(f"{field} entries must be strings")
        value = entry.strip()
        if not value:
            raise _catalog_error(f"{field} entries must be non-empty")
        if len(value) > MAX_CATALOG_ENTRY_CHARS:
            raise _catalog_error(f"{field} entries exceed {MAX_CATALOG_ENTRY_CHARS} characters")
        if "\n" in value or "\r" in value:
            raise _catalog_error(f"{field} entries must be single-line")
        if normalize == "domain":
            value = value.lower().lstrip(".")
        elif normalize == "extension":
            value = value.lower().lstrip(".")
        values.append(value)
    return _dedupe(values)


def load_injection_policy(workspace_root: Path | str | None = None) -> InjectionPolicy:
    """Load and strictly validate the optional workspace catalog.

    A missing catalog is the documented neutral case and yields the bundle
    defaults. A present catalog is never partially applied: every schema,
    type, key or size drift raises ``InjectionPolicyError``. The catalog is
    additive only; it cannot disable a default rule or lower the threshold.
    """
    if workspace_root is None:
        return default_policy()
    path = Path(workspace_root) / POLICY_CATALOG_RELATIVE_PATH
    try:
        stat = path.stat()
    except FileNotFoundError:
        return default_policy()
    except OSError as exc:
        raise InjectionPolicyError(
            f"injection-barrier catalog cannot be inspected: {type(exc).__name__}"
        ) from exc
    if not path.is_file():
        raise _catalog_error("path exists but is not a regular file")
    if stat.st_size > MAX_CATALOG_BYTES:
        raise _catalog_error(f"file exceeds {MAX_CATALOG_BYTES} bytes")
    try:
        raw_bytes = path.read_bytes()
    except OSError as exc:
        raise InjectionPolicyError(
            f"injection-barrier catalog cannot be read: {type(exc).__name__}"
        ) from exc
    if len(raw_bytes) > MAX_CATALOG_BYTES:
        raise _catalog_error(f"file exceeds {MAX_CATALOG_BYTES} bytes")
    try:
        document = json.loads(raw_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise _catalog_error("file is not valid UTF-8 JSON") from exc
    if not isinstance(document, dict):
        raise _catalog_error("top-level document must be an object")
    allowed_keys = {
        "schema_version",
        "extra_instruction_phrases",
        "extra_url_shortener_domains",
        "extra_dangerous_extensions",
        "extra_macro_extensions",
    }
    unknown = sorted(set(document) - allowed_keys)
    if unknown:
        raise _catalog_error(f"unknown keys: {', '.join(unknown)}")
    schema_version = document.get("schema_version")
    if isinstance(schema_version, bool) or not isinstance(schema_version, int):
        raise _catalog_error("schema_version must be the integer 1")
    if schema_version != INJECTION_BARRIER_SCHEMA_VERSION:
        raise _catalog_error(f"schema_version must be {INJECTION_BARRIER_SCHEMA_VERSION}")

    policy = InjectionPolicy(
        version=POLICY_VERSION,
        source="workspace_catalog",
        catalog_path=POLICY_CATALOG_RELATIVE_PATH.as_posix(),
        catalog_sha256=hashlib.sha256(raw_bytes).hexdigest(),
        extra_instruction_phrases=_catalog_string_list(
            document.get("extra_instruction_phrases"),
            "extra_instruction_phrases",
            normalize="phrase",
        ),
        extra_url_shortener_domains=_catalog_string_list(
            document.get("extra_url_shortener_domains"),
            "extra_url_shortener_domains",
            normalize="domain",
        ),
        extra_dangerous_extensions=_catalog_string_list(
            document.get("extra_dangerous_extensions"),
            "extra_dangerous_extensions",
            normalize="extension",
        ),
        extra_macro_extensions=_catalog_string_list(
            document.get("extra_macro_extensions"),
            "extra_macro_extensions",
            normalize="extension",
        ),
    )
    return policy


@lru_cache(maxsize=16)
def _literal_phrase_pattern(phrases: tuple[str, ...]) -> re.Pattern[str] | None:
    if not phrases:
        return None
    alternation = "|".join(re.escape(phrase) for phrase in phrases)
    return re.compile(rf"(?:{alternation})", re.IGNORECASE)


def _severity_at_least(severity: str | None, threshold: str) -> bool:
    if severity is None:
        return False
    return SEVERITY_RANK.get(severity, 0) >= SEVERITY_RANK[threshold]


def _sanitize_sample(text: str) -> str:
    normalized = unicodedata.normalize("NFKC", text)
    cleaned = "".join(
        " " if (ord(ch) < 0x20 or ord(ch) == 0x7F or ch in _BIDI_CHARS or ch in _ZERO_WIDTH_CHARS) else ch
        for ch in normalized
    )
    cleaned = " ".join(cleaned.split())
    if len(cleaned) > MAX_SAMPLE_CHARS:
        cleaned = cleaned[: MAX_SAMPLE_CHARS - 1] + "…"
    return cleaned


def _add_finding(
    collected: dict[tuple[str, str], dict[str, Any]],
    *,
    rule_id: str,
    severity: str,
    category: str,
    field: str,
    sample: str,
) -> None:
    key = (rule_id, field)
    entry = collected.get(key)
    if entry is None:
        entry = {
            "rule_id": rule_id,
            "severity": severity,
            "category": category,
            "field": field,
            "count": 0,
            "samples": [],
        }
        collected[key] = entry
    entry["count"] += 1
    if len(entry["samples"]) < MAX_SAMPLES_PER_RULE_FIELD:
        cleaned = _sanitize_sample(sample)
        if cleaned:
            entry["samples"].append(cleaned)


def _sorted_findings(collected: Mapping[tuple[str, str], dict[str, Any]]) -> tuple[dict[str, Any], ...]:
    ordered = sorted(
        collected.values(),
        key=lambda item: (-SEVERITY_RANK.get(str(item["severity"]), 0), str(item["rule_id"]), str(item["field"])),
    )
    return tuple(dict(item) for item in ordered)


def _iter_attachment_metadata(email: Mapping[str, Any]) -> list[dict[str, str]]:
    raw = email.get("attachments")
    if not isinstance(raw, list):
        return []
    entries: list[dict[str, str]] = []
    for candidate in raw[:MAX_ATTACHMENTS]:
        if not isinstance(candidate, Mapping):
            continue
        filename = str(candidate.get("filename") or candidate.get("name") or "").strip()
        mime_type = str(
            candidate.get("mime_type") or candidate.get("content_type") or ""
        ).strip().lower()
        if filename or mime_type:
            entries.append({"filename": filename[:512], "mime_type": mime_type[:256]})
    return entries


def _char_script(character: str) -> str | None:
    name = unicodedata.name(character, "")
    if name.startswith("LATIN"):
        return "latin"
    if name.startswith("CYRILLIC"):
        return "cyrillic"
    if name.startswith("GREEK"):
        return "greek"
    return None


def _mixed_script_tokens(text: str) -> list[str]:
    suspicious: list[str] = []
    for match in _LETTER_TOKEN_RE.finditer(text):
        token = match.group(0)
        scripts = {_char_script(character) for character in token}
        scripts.discard(None)
        if "latin" in scripts and ({"cyrillic", "greek"} & scripts):
            suspicious.append(token)
            if len(suspicious) >= MAX_SAMPLES_PER_RULE_FIELD:
                break
    return suspicious


def _has_dangerous_characters(text: str) -> bool:
    for character in text:
        codepoint = ord(character)
        if codepoint < 0x20:
            return True
        if 0x7F <= codepoint <= 0x9F:
            return True
        if character in _BIDI_CHARS or character in _ZERO_WIDTH_CHARS:
            return True
        if _TAG_CHAR_RANGE[0] <= codepoint <= _TAG_CHAR_RANGE[1]:
            return True
    return False


def _scan_control_characters(
    raw: str,
    field: str,
    collected: dict[tuple[str, str], dict[str, Any]],
) -> None:
    for match in _CONTROL_RE.finditer(raw):
        _add_finding(
            collected,
            rule_id="IB-CTRL-001",
            severity="medium",
            category="control",
            field=field,
            sample=f"U+{ord(match.group(0)):04X}",
        )
    for character in raw:
        if character in _BIDI_CHARS:
            _add_finding(
                collected,
                rule_id="IB-CTRL-002",
                severity="high",
                category="control",
                field=field,
                sample=f"U+{ord(character):04X}",
            )
    zero_width_count = sum(1 for character in raw if character in _ZERO_WIDTH_CHARS)
    tag_count = sum(
        1
        for character in raw
        if _TAG_CHAR_RANGE[0] <= ord(character) <= _TAG_CHAR_RANGE[1]
    )
    if zero_width_count:
        _add_finding(
            collected,
            rule_id="IB-CTRL-003",
            severity="medium" if zero_width_count >= 8 else "low",
            category="control",
            field=field,
            sample=f"{zero_width_count} zero-width characters",
        )
    if tag_count:
        _add_finding(
            collected,
            rule_id="IB-CTRL-003",
            severity="high",
            category="control",
            field=field,
            sample=f"{tag_count} Unicode tag characters",
        )


def _scan_instruction_patterns(
    haystack: str,
    field: str,
    policy: InjectionPolicy,
    collected: dict[tuple[str, str], dict[str, Any]],
) -> None:
    for rule_id, severity, category, pattern in _COMPILED_INSTRUCTION_PATTERNS:
        for match in pattern.finditer(haystack):
            _add_finding(
                collected,
                rule_id=rule_id,
                severity=severity,
                category=category,
                field=field,
                sample=match.group(0),
            )
    extra_pattern = _literal_phrase_pattern(policy.extra_instruction_phrases)
    if extra_pattern is not None:
        for match in extra_pattern.finditer(haystack):
            _add_finding(
                collected,
                rule_id="IB-INSTR-006",
                severity="medium",
                category="instruction",
                field=field,
                sample=match.group(0),
            )


def _url_host(url: str) -> str:
    try:
        parsed = urlsplit(url)
        host = parsed.hostname or ""
    except ValueError:
        return ""
    return host.lower().strip(".")


def _url_port_problem(url: str) -> bool:
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError:
        return True
    if port is None:
        return False
    return port not in (21, 80, 443)


def _shortener_match(host: str, shorteners: tuple[str, ...]) -> bool:
    for domain in shorteners:
        if host == domain or host.endswith("." + domain):
            return True
    return False


def _scan_urls(
    normalized: str,
    field: str,
    policy: InjectionPolicy,
    collected: dict[tuple[str, str], dict[str, Any]],
) -> bool:
    """Scan URLs; return ``True`` when the per-field URL cap was exceeded."""
    active_scheme = _ACTIVE_SCHEME_RE.search(normalized)
    if active_scheme:
        _add_finding(
            collected,
            rule_id="IB-URL-001",
            severity="high",
            category="url",
            field=field,
            sample=active_scheme.group(0),
        )
    urls: list[str] = []
    urls_truncated = False
    for match in _URL_RE.finditer(normalized):
        if len(urls) >= MAX_URLS_PER_FIELD:
            urls_truncated = True
            break
        urls.append(match.group(0).rstrip(".,;:!?)]}'\"»”"))
    if urls_truncated:
        _add_finding(
            collected,
            rule_id="IB-SCAN-001",
            severity="low",
            category="scan",
            field=field,
            sample=f"URL scan cap reached ({MAX_URLS_PER_FIELD})",
        )
    shorteners = _dedupe(_DEFAULT_SHORTENER_DOMAINS + policy.extra_url_shortener_domains)
    for url in urls:
        host = _url_host(url)
        if "@" in url.split("://", 1)[-1].split("/", 1)[0]:
            _add_finding(
                collected,
                rule_id="IB-URL-002",
                severity="high",
                category="url",
                field=field,
                sample=url,
            )
        if not host:
            continue
        is_ip = False
        try:
            ipaddress.ip_address(host.strip("[]"))
            is_ip = True
        except ValueError:
            is_ip = bool(_IPV4_LIKE_RE.match(host))
        if is_ip or host.startswith("xn--") or ".xn--" in host or _url_port_problem(url):
            _add_finding(
                collected,
                rule_id="IB-URL-003",
                severity="medium",
                category="url",
                field=field,
                sample=url,
            )
        if _shortener_match(host, shorteners):
            _add_finding(
                collected,
                rule_id="IB-URL-004",
                severity="low",
                category="url",
                field=field,
                sample=url,
            )
    if len(urls) > 20:
        _add_finding(
            collected,
            rule_id="IB-URL-006",
            severity="low",
            category="url",
            field=field,
            sample=f"{len(urls)} URLs",
        )
    for match in _MARKDOWN_LINK_RE.finditer(normalized):
        label, target = match.group(1), match.group(2)
        label_domains = {item.lower() for item in _DOMAIN_RE.findall(label)}
        target_host = _url_host(target)
        if not label_domains or not target_host:
            continue
        if not any(
            target_host == domain or target_host.endswith("." + domain)
            for domain in label_domains
        ):
            _add_finding(
                collected,
                rule_id="IB-URL-005",
                severity="high",
                category="url",
                field=field,
                sample=f"{label} -> {target}",
            )
    return urls_truncated


def _filename_extension(filename: str) -> str:
    cleaned = filename.strip().rstrip(" .")
    if "." not in cleaned:
        return ""
    return cleaned.rsplit(".", 1)[-1].lower()


def _attachment_double_extension(filename: str, dangerous: set[str]) -> str:
    cleaned = filename.strip().rstrip(" .")
    parts = cleaned.lower().split(".")
    if len(parts) < 3:
        return ""
    suffix = parts[-1]
    if suffix in dangerous:
        return f".{parts[-2]}.{suffix}"
    return ""


def _scan_attachments(
    attachments: list[dict[str, str]],
    policy: InjectionPolicy,
    collected: dict[tuple[str, str], dict[str, Any]],
) -> None:
    dangerous = set(_DEFAULT_DANGEROUS_EXTENSIONS) | set(policy.extra_dangerous_extensions)
    macro = set(_DEFAULT_MACRO_EXTENSIONS) | set(policy.extra_macro_extensions)
    for attachment in attachments:
        filename = attachment["filename"]
        mime_type = attachment["mime_type"]
        if not filename and not mime_type:
            continue
        extension = _filename_extension(filename)
        if extension in dangerous:
            _add_finding(
                collected,
                rule_id="IB-ATT-001",
                severity="high",
                category="attachment",
                field="attachments",
                sample=filename or f".{extension}",
            )
        double = _attachment_double_extension(filename, dangerous)
        if double:
            _add_finding(
                collected,
                rule_id="IB-ATT-001",
                severity="high",
                category="attachment",
                field="attachments",
                sample=f"{filename} ({double})",
            )
        if extension in macro:
            _add_finding(
                collected,
                rule_id="IB-ATT-002",
                severity="medium",
                category="attachment",
                field="attachments",
                sample=filename,
            )
        if extension in _DEFAULT_HTML_EXTENSIONS:
            _add_finding(
                collected,
                rule_id="IB-ATT-003",
                severity="medium",
                category="attachment",
                field="attachments",
                sample=filename,
            )
        if extension in _DEFAULT_ARCHIVE_EXTENSIONS:
            _add_finding(
                collected,
                rule_id="IB-ATT-007",
                severity="low",
                category="attachment",
                field="attachments",
                sample=filename,
            )
        if _has_dangerous_characters(filename):
            _add_finding(
                collected,
                rule_id="IB-ATT-004",
                severity="high",
                category="attachment",
                field="attachments",
                sample=filename,
            )
        for token in _mixed_script_tokens(filename):
            _add_finding(
                collected,
                rule_id="IB-ATT-005",
                severity="high",
                category="attachment",
                field="attachments",
                sample=token,
            )
        if mime_type in _DANGEROUS_MIME_TYPES:
            _add_finding(
                collected,
                rule_id="IB-ATT-006",
                severity="high",
                category="attachment",
                field="attachments",
                sample=f"{filename} ({mime_type})" if filename else mime_type,
            )


def _scan_html(
    normalized: str,
    field: str,
    collected: dict[tuple[str, str], dict[str, Any]],
) -> None:
    active = _HTML_ACTIVE_RE.search(normalized)
    if active:
        _add_finding(
            collected,
            rule_id="IB-HTML-001",
            severity="medium",
            category="html",
            field=field,
            sample=active.group(0),
        )
    hidden = _HTML_HIDDEN_RE.search(normalized)
    if hidden:
        _add_finding(
            collected,
            rule_id="IB-HTML-002",
            severity="medium",
            category="html",
            field=field,
            sample=hidden.group(0),
        )


def _scan_encoded(
    normalized: str,
    field: str,
    collected: dict[tuple[str, str], dict[str, Any]],
) -> None:
    base64_match = _BASE64_BLOB_RE.search(normalized)
    if base64_match:
        _add_finding(
            collected,
            rule_id="IB-ENC-001",
            severity="low",
            category="encoded",
            field=field,
            sample=f"{len(base64_match.group(0))}-char base64-like blob",
        )
    hex_match = _HEX_BLOB_RE.search(normalized)
    if hex_match:
        _add_finding(
            collected,
            rule_id="IB-ENC-001",
            severity="low",
            category="encoded",
            field=field,
            sample=f"{len(hex_match.group(0))}-char hex-like blob",
        )


def _scan_text(
    raw: str,
    field: str,
    policy: InjectionPolicy,
    collected: dict[tuple[str, str], dict[str, Any]],
    *,
    allow_escaped_marker: bool = False,
) -> bool:
    """Scan one field; return ``True`` when the scanned window was truncated."""
    truncated = len(raw) > MAX_SCAN_CHARS
    window = raw[:MAX_SCAN_CHARS] if truncated else raw
    try:
        normalized = unicodedata.normalize("NFKC", window)
    except (TypeError, ValueError):
        normalized = window
    haystack = html.unescape(normalized)
    _scan_control_characters(window, field, collected)
    _scan_instruction_patterns(haystack, field, policy, collected)
    spoofs = [match.group(0) for match in _FENCE_SPOOF_RE.finditer(normalized)]
    if allow_escaped_marker:
        # A verified own fence already replaced every spoof with this inert
        # marker; re-scanning the fenced preview must not quarantine it again.
        spoofs = [sample for sample in spoofs if sample != "[ESCAPED_UNTRUSTED_TAG]"]
    if spoofs:
        _add_finding(
            collected,
            rule_id="IB-DELIM-001",
            severity="high",
            category="delimiter",
            field=field,
            sample=spoofs[0],
        )
    for token in _mixed_script_tokens(window):
        _add_finding(
            collected,
            rule_id="IB-HOMO-001",
            severity="high",
            category="homoglyph",
            field=field,
            sample=token,
        )
    urls_truncated = _scan_urls(haystack, field, policy, collected)
    _scan_html(haystack, field, collected)
    _scan_encoded(haystack, field, collected)
    return truncated or urls_truncated


def _unwrap_own_fence(text: str) -> tuple[str, str | None, str | None]:
    """Return ``(inner, raw sha256, inner sha256)`` for a well-formed own fence."""
    match = _OWN_FENCE_RE.match(text)
    if not match:
        return text, None, None
    attributes = match.group("attrs") or ""
    sha_match = _FENCE_SHA_RE.search(attributes)
    inner_sha_match = _FENCE_INNER_SHA_RE.search(attributes)
    return (
        match.group("body"),
        sha_match.group("sha").lower() if sha_match else None,
        inner_sha_match.group("sha").lower() if inner_sha_match else None,
    )


def _neutralize_for_fence(text: str) -> tuple[str, dict[str, int | bool]]:
    counters: dict[str, int | bool] = {
        "nul_removed": 0,
        "control_replaced": 0,
        "bidi_replaced": 0,
        "zero_width_replaced": 0,
        "tag_replaced": 0,
        "tag_spoofs_escaped": 0,
        "nfkc_changed": False,
        "truncated": False,
    }
    cleaned_parts: list[str] = []
    for character in text:
        codepoint = ord(character)
        if character == "\x00":
            counters["nul_removed"] = int(counters["nul_removed"]) + 1
            continue
        if character in _BIDI_CHARS:
            cleaned_parts.append("[BIDI]")
            counters["bidi_replaced"] = int(counters["bidi_replaced"]) + 1
            continue
        if character in _ZERO_WIDTH_CHARS:
            cleaned_parts.append("[ZW]")
            counters["zero_width_replaced"] = int(counters["zero_width_replaced"]) + 1
            continue
        if _TAG_CHAR_RANGE[0] <= codepoint <= _TAG_CHAR_RANGE[1]:
            cleaned_parts.append("[TAG]")
            counters["tag_replaced"] = int(counters["tag_replaced"]) + 1
            continue
        if codepoint < 0x20 and character not in "\n\t":
            cleaned_parts.append("[CTRL]")
            counters["control_replaced"] = int(counters["control_replaced"]) + 1
            continue
        if 0x7F <= codepoint <= 0x9F:
            cleaned_parts.append("[CTRL]")
            counters["control_replaced"] = int(counters["control_replaced"]) + 1
            continue
        cleaned_parts.append(character)
    cleaned = "".join(cleaned_parts)
    try:
        normalized = unicodedata.normalize("NFKC", cleaned)
    except (TypeError, ValueError):
        normalized = cleaned
    if normalized != cleaned:
        counters["nfkc_changed"] = True
    if len(normalized) > MAX_FENCE_CHARS:
        normalized = normalized[:MAX_FENCE_CHARS] + "\n[TRUNCATED]"
        counters["truncated"] = True
    escaped, count = _FENCE_SPOOF_RE.subn("[ESCAPED_UNTRUSTED_TAG]", normalized)
    counters["tag_spoofs_escaped"] = count
    return escaped, counters


def fence_untrusted_text(
    text: str,
    *,
    sha256: str | None = None,
) -> tuple[str, dict[str, int | bool]]:
    """Wrap untrusted text in fixed delimiters and neutralise spoofs.

    The returned transformation counters describe the deterministic
    normalisation (control/bidi/zero-width/tag characters, NFKC and delimiter
    spoofs). The input is never executed, truncated silently or escaped by a
    caller-supplied format.
    """
    raw = str(text or "")
    digest = sha256 or hashlib.sha256(raw.encode("utf-8")).hexdigest()
    inner, counters = _neutralize_for_fence(raw)
    inner_digest = hashlib.sha256(inner.encode("utf-8")).hexdigest()
    fenced = (
        f'<{FENCE_TAG} fence_version="{FENCE_VERSION}" sha256="{digest}" '
        f'inner_sha256="{inner_digest}">\n'
        f"{inner}\n"
        f"{FENCE_CLOSE}"
    )
    return fenced, counters


def _message_key(email: Mapping[str, Any]) -> tuple[str, str]:
    raw_message_id = email.get("message_id") or email.get("raw_message_id") or ""
    normalized = normalize_message_id(str(raw_message_id))
    if normalized:
        return normalized, normalized
    folder = str(email.get("folder", "INBOX") or "INBOX")
    envelope_id = str(email.get("envelope_id", "") or "")
    fallback = hashlib.sha256(
        "\x00".join(
            (
                folder,
                envelope_id,
                str(email.get("subject", "") or ""),
                str(email.get("date", "") or ""),
            )
        ).encode("utf-8")
    ).hexdigest()[:32]
    return f"fallback:{fallback}", ""


def quarantine_id_for(message_key: str, policy_version: int = POLICY_VERSION) -> str:
    """Return the deterministic 16-hex quarantine id for one message key."""
    return hashlib.sha256(
        f"oi62:{policy_version}:{message_key}".encode("utf-8")
    ).hexdigest()[:16]


def assess_email(
    email: Mapping[str, Any],
    policy: InjectionPolicy | None = None,
    *,
    extra_text: str | None = None,
) -> Assessment:
    """Scan one mail object and return a deterministic assessment.

    ``preview`` may already be wrapped in this module's own fence (for example
    after an ``inspect`` run): the wrapper is unwrapped before scanning and is
    not treated as a delimiter spoof. Every other field is scanned verbatim.
    """
    active_policy = policy or default_policy()
    message_key, message_id = _message_key(email)
    envelope_id = str(email.get("envelope_id", "") or "")
    folder = str(email.get("folder", "INBOX") or "INBOX")
    subject = str(email.get("subject", "") or "")
    from_addr = str(email.get("from", "") or "")
    date = str(email.get("date", "") or "")

    raw_preview = str(email.get("preview", "") or "")
    preview_inner, fenced_sha, fenced_inner_sha = _unwrap_own_fence(raw_preview)
    already_fenced = preview_inner != raw_preview
    actual_inner_sha = (
        hashlib.sha256(preview_inner.encode("utf-8")).hexdigest()
        if already_fenced
        else None
    )
    # A fence is only "ours" when the inner digest matches the attribute. A
    # sender-crafted wrapper must never bind an attacker-chosen content hash.
    fence_valid = bool(
        already_fenced
        and fenced_inner_sha
        and actual_inner_sha
        and fenced_inner_sha == actual_inner_sha
    )

    collected: dict[tuple[str, str], dict[str, Any]] = {}
    prior_block = email.get("injection_barrier")
    if isinstance(prior_block, Mapping) and prior_block.get("status") == "quarantined":
        # A cached inspect result keeps its fail-closed quarantine signal even
        # when fencing neutralised the original delimiter/control finding.
        _add_finding(
            collected,
            rule_id="IB-PRIOR-001",
            severity="high",
            category="cache",
            field="injection_barrier",
            sample="prior quarantine annotation",
        )
    if already_fenced and not fence_valid:
        _add_finding(
            collected,
            rule_id="IB-DELIM-001",
            severity="high",
            category="delimiter",
            field="preview",
            sample="forged or unverifiable fence hash",
        )

    truncated = False
    for field in _TEXT_FIELDS:
        value = email.get(field)
        if not isinstance(value, str) or not value:
            continue
        if field == "preview" and already_fenced:
            value = preview_inner
        truncated = _scan_text(
            value,
            field,
            active_policy,
            collected,
            allow_escaped_marker=(field == "preview" and fence_valid),
        ) or truncated
    if extra_text:
        truncated = _scan_text(str(extra_text), "untrusted_external", active_policy, collected) or truncated
    raw_attachments = email.get("attachments")
    attachments = _iter_attachment_metadata(email)
    if isinstance(raw_attachments, list) and len(raw_attachments) > MAX_ATTACHMENTS:
        _add_finding(
            collected,
            rule_id="IB-SCAN-001",
            severity="low",
            category="scan",
            field="attachments",
            sample=f"attachment scan cap reached ({MAX_ATTACHMENTS})",
        )
        truncated = True
    if attachments:
        _scan_attachments(attachments, active_policy, collected)

    findings = _sorted_findings(collected)
    max_severity: str | None = None
    for finding in findings:
        severity = str(finding["severity"])
        if max_severity is None or SEVERITY_RANK[severity] > SEVERITY_RANK[max_severity]:
            max_severity = severity
    quarantined = _severity_at_least(max_severity, QUARANTINE_THRESHOLD)

    if already_fenced:
        content_bytes = len(preview_inner.encode("utf-8"))
        if fence_valid:
            fenced_preview = raw_preview
            content_sha256 = fenced_sha or actual_inner_sha or hashlib.sha256(
                preview_inner.encode("utf-8")
            ).hexdigest()
            transformations: dict[str, int | bool] = {
                "nul_removed": 0,
                "control_replaced": 0,
                "bidi_replaced": 0,
                "zero_width_replaced": 0,
                "tag_replaced": 0,
                "tag_spoofs_escaped": 0,
                "nfkc_changed": False,
                "truncated": False,
            }
        else:
            # Re-fence an unverifiable wrapper from its scanned inner content;
            # the record must never bind an attacker-chosen hash attribute.
            content_sha256 = actual_inner_sha or hashlib.sha256(
                preview_inner.encode("utf-8")
            ).hexdigest()
            fenced_preview, transformations = fence_untrusted_text(
                preview_inner, sha256=content_sha256
            )
    else:
        content_sha256 = hashlib.sha256(raw_preview.encode("utf-8")).hexdigest()
        content_bytes = len(raw_preview.encode("utf-8"))
        if raw_preview:
            fenced_preview, transformations = fence_untrusted_text(
                raw_preview, sha256=content_sha256
            )
        else:
            fenced_preview = ""
            transformations = {
                "nul_removed": 0,
                "control_replaced": 0,
                "bidi_replaced": 0,
                "zero_width_replaced": 0,
                "tag_replaced": 0,
                "tag_spoofs_escaped": 0,
                "nfkc_changed": False,
                "truncated": False,
            }
    fenced_sha256 = (
        hashlib.sha256(fenced_preview.encode("utf-8")).hexdigest()
        if fenced_preview
        else hashlib.sha256(b"").hexdigest()
    )
    fenced_bytes = len(fenced_preview.encode("utf-8"))

    return Assessment(
        message_key=message_key,
        message_id=message_id,
        envelope_id=envelope_id,
        folder=folder,
        subject=subject,
        from_addr=from_addr,
        date=date,
        findings=findings,
        max_severity=max_severity,
        quarantined=quarantined,
        fenced_preview=fenced_preview,
        content_sha256=content_sha256,
        content_bytes=content_bytes,
        fenced_sha256=fenced_sha256,
        fenced_bytes=fenced_bytes,
        transformations=transformations,
        scan_truncated=truncated,
        policy_version=active_policy.version,
        policy_source=active_policy.source,
        policy_catalog_sha256=active_policy.catalog_sha256,
    )


def annotate_email_for_output(
    email: Mapping[str, Any],
    assessment: Assessment,
) -> dict[str, Any]:
    """Return a copy with fenced preview and an additive barrier block."""
    annotated = dict(email)
    if str(email.get("preview", "") or ""):
        annotated["preview"] = assessment.fenced_preview
    if assessment.findings:
        annotated["injection_barrier"] = assessment.as_output_block()
    return annotated


def _bounded_record_text(value: str, limit: int) -> str:
    cleaned = unicodedata.normalize("NFKC", str(value or ""))
    cleaned = "".join(
        " " if (ord(ch) < 0x20 or ord(ch) == 0x7F or ch in _BIDI_CHARS or ch in _ZERO_WIDTH_CHARS) else ch
        for ch in cleaned
    )
    cleaned = " ".join(cleaned.split())
    return cleaned[:limit]


def _relative_to_workspace(path: Path, workspace_root: Path) -> str:
    try:
        return path.resolve().relative_to(workspace_root.resolve()).as_posix()
    except (OSError, ValueError):
        return path.name


def _append_jsonl(path: Path, entry: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(
        dict(entry), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ) + "\n"
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(line)
        handle.flush()
        os.fsync(handle.fileno())


def _write_once(path: Path, content: str) -> bool:
    if path.exists():
        return False
    atomic_write_text(path, content)
    return True


def write_quarantine_evidence(
    assessment: Assessment,
    email: Mapping[str, Any],
    *,
    data_dir: Path,
    mode: str,
    observed_at: str | None = None,
) -> dict[str, Any]:
    """Persist one quarantine observation append-only and return its envelope.

    ``data_dir`` is the desk's ``data/mail-desk`` directory. The store is
    deterministic per message key:

    ``<data_dir>/injection-quarantine/<id>/record.json``  (write-once)
    ``<data_dir>/injection-quarantine/<id>/fenced.txt``   (write-once)
    ``<data_dir>/injection-quarantine/<id>/observations.jsonl`` (append-only)
    ``<data_dir>/injection-quarantine/index.jsonl``       (append-only)
    """
    if mode not in {"inspect", "draft", "pipeline"}:
        raise InjectionBarrierError(f"unsupported quarantine mode: {mode}")
    if not assessment.quarantined:
        raise InjectionBarrierError("quarantine evidence requires a quarantined assessment")
    timestamp = (observed_at or utc_now_iso()).strip()
    if not timestamp:
        raise InjectionBarrierError("observed_at must be a non-empty timestamp")
    data_root = Path(data_dir)
    workspace_root = data_root.parent.parent
    quarantine_root = data_root / QUARANTINE_DIR_NAME
    entry_dir = quarantine_root / assessment.quarantine_id
    entry_dir.mkdir(parents=True, exist_ok=True)

    record_path = entry_dir / "record.json"
    record_exists = record_path.exists()
    if not record_exists:
        rule_ids = sorted({str(finding["rule_id"]) for finding in assessment.findings})
        record = {
            "schema_version": INJECTION_BARRIER_SCHEMA_VERSION,
            "quarantine_id": assessment.quarantine_id,
            "status": "quarantined",
            "mode": mode,
            "message_key": _bounded_record_text(assessment.message_key, 320),
            "message_id": _bounded_record_text(assessment.message_id, 320),
            "envelope_id": _bounded_record_text(assessment.envelope_id, 128),
            "folder": _bounded_record_text(assessment.folder, 128),
            "subject": _bounded_record_text(assessment.subject, MAX_RECORD_SUBJECT_CHARS),
            "from": _bounded_record_text(assessment.from_addr, MAX_RECORD_ADDRESS_CHARS),
            "date": _bounded_record_text(assessment.date, 64),
            "content": {
                "sha256": assessment.content_sha256,
                "bytes": assessment.content_bytes,
                "fence_version": FENCE_VERSION,
                "fenced_sha256": assessment.fenced_sha256,
                "fenced_bytes": assessment.fenced_bytes,
                "transformations": dict(assessment.transformations),
                "scan_truncated": assessment.scan_truncated,
            },
            "findings": [dict(finding) for finding in assessment.findings],
            "rule_ids": rule_ids,
            "max_severity": assessment.max_severity,
            "policy": {
                "version": assessment.policy_version,
                "source": assessment.policy_source,
                "catalog_sha256": assessment.policy_catalog_sha256,
            },
            "first_observed_at": timestamp,
        }
        atomic_write_json(record_path, record)

    fenced_path = entry_dir / "fenced.txt"
    if not fenced_path.exists():
        # Write exactly the fenced representation so its bytes match
        # ``content.fenced_sha256``. A header/attachment-only quarantine has an
        # empty fenced file; the record and index still name it.
        _write_once(fenced_path, assessment.fenced_preview)

    rule_ids = sorted({str(finding["rule_id"]) for finding in assessment.findings})
    observation = {
        "schema_version": INJECTION_BARRIER_SCHEMA_VERSION,
        "quarantine_id": assessment.quarantine_id,
        "observed_at": timestamp,
        "mode": mode,
        "message_key": _bounded_record_text(assessment.message_key, 320),
        "content_sha256": assessment.content_sha256,
        "max_severity": assessment.max_severity,
        "findings_count": len(assessment.findings),
        "rule_ids": rule_ids,
        "policy_version": assessment.policy_version,
    }
    _append_jsonl(entry_dir / "observations.jsonl", observation)
    index_entry = dict(observation)
    index_entry["record"] = _relative_to_workspace(record_path, workspace_root)
    index_entry["fenced"] = (
        _relative_to_workspace(fenced_path, workspace_root)
        if fenced_path.exists()
        else None
    )
    _append_jsonl(quarantine_root / "index.jsonl", index_entry)

    return {
        "quarantine_id": assessment.quarantine_id,
        "status": "quarantined",
        "max_severity": assessment.max_severity,
        "rule_ids": rule_ids,
        "record": _relative_to_workspace(record_path, workspace_root),
        "fenced": (
            _relative_to_workspace(fenced_path, workspace_root)
            if fenced_path.exists()
            else None
        ),
        "observed_at": timestamp,
        "record_created": not record_exists,
    }


def manifest_block(assessments: Iterable[Assessment]) -> dict[str, Any]:
    """Return the deterministic hash-bindable manifest summary block."""
    items = list(assessments)
    quarantined = [item for item in items if item.quarantined]
    findings_by_rule: dict[str, int] = {}
    for item in items:
        for finding in item.findings:
            rule_id = str(finding["rule_id"])
            findings_by_rule[rule_id] = findings_by_rule.get(rule_id, 0) + int(finding["count"])
    quarantined_rows = [
        {
            "quarantine_id": item.quarantine_id,
            "message_key": item.message_key,
            "message_id": item.message_id,
            "envelope_id": item.envelope_id,
            "severity": item.max_severity,
            "rule_ids": sorted({str(finding["rule_id"]) for finding in item.findings}),
            "content_sha256": item.content_sha256,
        }
        for item in sorted(quarantined, key=lambda entry: entry.quarantine_id)
    ]
    return {
        "schema_version": INJECTION_BARRIER_SCHEMA_VERSION,
        "fence_version": FENCE_VERSION,
        "policy_version": POLICY_VERSION,
        "scanned_count": len(items),
        "quarantined_count": len(quarantined),
        "findings_count": sum(int(finding["count"]) for item in items for finding in item.findings),
        "findings_by_rule": dict(sorted(findings_by_rule.items())),
        "quarantined": quarantined_rows,
        "quarantine_root": QUARANTINE_ROOT_RELATIVE_PATH.as_posix(),
    }


def assess_and_quarantine(
    emails: list[dict[str, Any]],
    *,
    policy: InjectionPolicy,
    data_dir: Path,
    mode: str,
) -> tuple[list[Assessment], list[dict[str, Any]], dict[str, Any]]:
    """Assess a batch, persist evidence for flagged mail and return its block.

    Returns ``(assessments, quarantine_results, manifest_block)``. Evidence is
    written before the caller builds any manifest, so a failure to persist it
    stops the run fail-loud instead of silently dropping the quarantine.
    """
    assessments = [assess_email(email, policy) for email in emails]
    quarantine_results: list[dict[str, Any]] = []
    for email, assessment in zip(emails, assessments):
        if assessment.quarantined:
            quarantine_results.append(
                write_quarantine_evidence(
                    assessment,
                    email,
                    data_dir=data_dir,
                    mode=mode,
                )
            )
    return assessments, quarantine_results, manifest_block(assessments)
