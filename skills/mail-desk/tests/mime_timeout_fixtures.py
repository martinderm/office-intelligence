"""Stateful MIME-export mocks and hermetic draft inputs for the MD-R3 tests.

The module is deliberately *not* named ``test_*.py`` so the unittest discovery pattern
does not collect it as a suite.  It provides a single stateful raw-MIME export mock that
can be injected both as the classifier's message-export path (through the real
``get_single_email_details`` MIME inspection) and as the MD-E2 ``read_raw_mime`` boundary,
so one call counter pins exactly how many canonical exports one item caused in one run.

Every helper is deterministic, reads no mailbox and writes only inside a caller-provided
temporary workspace.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
import sys
from typing import Any, Callable, Mapping
from unittest.mock import patch

_SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from core import attachments as _attachments  # noqa: E402
from core import himalaya as _himalaya  # noqa: E402
from core.modes import draft as _draft_mode  # noqa: E402

TIMEOUT_REASON = "himalaya_timeout"
TIMEOUT_MESSAGE = "Himalaya timed out; refusing to retry the mailbox command."

ACCOUNT = "primary"
FOLDER = "INBOX"
AMBIGUOUS_ENVELOPE_ID = "9392"
AMBIGUOUS_MESSAGE_ID = "mdr3-timeout@example.test"

ATTACHMENT_FILENAME = "clue.txt"
ATTACHMENT_TEXT = "MD-R3 hermetic attachment without a routing signal."
ATTACHMENT_SHA256 = hashlib.sha256(ATTACHMENT_TEXT.encode("utf-8")).hexdigest()


class FlakyMimeReader:
    """Stateful raw-MIME export mock: fail the first ``fail_first_n`` calls, then succeed.

    ``call_count`` is the single observation point for "how many canonical MIME exports did
    this run perform".  The same instance can be injected at both the classifier export and
    the MD-E2 export boundary, so a second call proves a same-run double export.
    """

    def __init__(
        self,
        raw_eml: bytes,
        *,
        fail_first_n: int = 1,
        fail_forever: bool = False,
    ) -> None:
        self._raw_eml = raw_eml
        self._fail_first_n = fail_first_n
        self._fail_forever = fail_forever
        self.call_count = 0

    def __call__(
        self,
        env_id: str | int,
        folder: str = FOLDER,
        account: str | None = None,
        timeout: int = 30,
        **kwargs: Any,
    ) -> bytes:
        self.call_count += 1
        if self._fail_forever or self.call_count <= self._fail_first_n:
            raise _himalaya.HimalayaInvocationError(TIMEOUT_REASON, TIMEOUT_MESSAGE)
        return self._raw_eml


def fixed_raw_eml() -> bytes:
    """Return one deterministic RFC 822 message with a signal-free text attachment."""
    return _attachments.build_test_eml(
        subject="Kurzfrage",
        message_id=f"<{AMBIGUOUS_MESSAGE_ID}>",
        body_text="Kurze Rueckfrage ohne weitere Details.",
        attachments=[
            {
                "filename": ATTACHMENT_FILENAME,
                "mime_type": "text/plain",
                "data": ATTACHMENT_TEXT.encode("utf-8"),
            }
        ],
    )


def ambiguous_email(
    *,
    envelope_id: str = AMBIGUOUS_ENVELOPE_ID,
    message_id: str = AMBIGUOUS_MESSAGE_ID,
    subject: str = "Kurzfrage",
    preview: str = "Kurze Rueckfrage ohne weitere Details.",
    from_addr: str = "sender@example.test",
    to_addr: str = "desk@example.test",
    date: str = "Fri, 18 Sep 2026 09:00:00 +0200",
    folder: str = FOLDER,
) -> dict[str, Any]:
    """Return a hermetic preview email carrying no routing signal."""
    return {
        "envelope_id": envelope_id,
        "folder": folder,
        "message_id": message_id,
        "subject": subject,
        "from": from_addr,
        "to": to_addr,
        "cc": "",
        "date": date,
        "preview": preview,
    }


def _canned_message_read(email: Mapping[str, Any]) -> str:
    """Render the header/body text the real ``get_single_email_details`` read expects."""
    return (
        f"Message-Id: <{email['message_id']}>\n"
        f"Subject: {email['subject']}\n"
        f"From: {email['from']}\n"
        f"To: {email['to']}\n"
        f"Date: {email['date']}\n"
        f"\n{email['preview']}\n"
    )


def preview_email_from_export(
    email: Mapping[str, Any], reader: Callable[..., bytes], account: str
) -> dict[str, Any]:
    """Return an inspected preview email produced through the real MIME export path.

    Only the raw export boundary is replaced by ``reader``; the production
    ``get_single_email_details`` therefore turns a transient timeout into the canonical
    ``attachment_inventory_unavailable``/``attachment_error`` fields itself.
    """
    with patch.object(_himalaya, "fetch_raw_message_eml", reader), patch.object(
        _himalaya, "run_himalaya", return_value=_canned_message_read(email)
    ):
        return _himalaya.get_single_email_details(
            email["envelope_id"], email["folder"], account
        )


def make_full_reader(email: Mapping[str, Any]) -> Callable[..., dict[str, Any]]:
    """Return a hermetic Full Read that observes the same inventory state as the preview."""

    def _full_reader(
        env_id: str | int,
        folder: str = FOLDER,
        account: str | None = None,
        fallback_envelope: Mapping[str, Any] | None = None,
        full_body: bool = False,
        **kwargs: Any,
    ) -> dict[str, Any]:
        return dict(fallback_envelope if fallback_envelope is not None else email)

    return _full_reader


def still_ambiguous_evaluation() -> dict[str, Any]:
    """Return the bounded MD-E1 outcome a same-run MD-E2 fetch would install."""
    files = [
        {
            "filename": ATTACHMENT_FILENAME,
            "sha256": ATTACHMENT_SHA256,
            "mime_type": "text/plain",
            "chars": len(ATTACHMENT_TEXT),
            "coverage": "full",
            "run_id": "run-mdr3",
        }
    ]
    return {
        "attachment_evaluation": {
            "status": "completed",
            "reason": "still_ambiguous",
            "authorization": "auto_evaluated",
            "files": files,
            "used_for_classification": False,
            "classifier_revision": None,
        }
    }


def make_evaluate_stub(
    calls: list[dict[str, Any]] | None = None,
) -> Callable[..., dict[str, Any]]:
    """Return an ``attachment_evaluate`` stand-in that records its keyword arguments."""

    def _evaluate(**kwargs: Any) -> dict[str, Any]:
        if calls is not None:
            calls.append(dict(kwargs))
        return still_ambiguous_evaluation()

    return _evaluate


class _NullTracker:
    """Inert progress tracker so no run writes progress artifacts."""

    def __init__(self, **kwargs: Any) -> None:
        pass

    def step(self, *args: Any, **kwargs: Any) -> None:
        pass

    def advance_item(self, *args: Any, **kwargs: Any) -> None:
        pass

    def complete(self, *args: Any, **kwargs: Any) -> None:
        pass


def _no_sent_index(*args: Any, **kwargs: Any) -> dict[str, Any]:
    return {}


def make_get_unprocessed(
    email: Mapping[str, Any], reader: Callable[..., bytes], account: str
) -> Callable[..., tuple[list[dict[str, Any]], int]]:
    """Return the message-listing boundary that inspects one mail through ``reader``."""

    def _get_unprocessed(**kwargs: Any) -> tuple[list[dict[str, Any]], int]:
        return [preview_email_from_export(email, reader, account)], 0

    return _get_unprocessed


def run_draft_with_reader(
    *,
    workspace: Path,
    reader: Callable[..., bytes],
    email: Mapping[str, Any],
    account: str = ACCOUNT,
    evaluate: Callable[..., dict[str, Any]] | None = None,
    config: Mapping[str, Any] | None = None,
) -> tuple[dict[str, Any], Path]:
    """Run ``run_draft_mode`` hermetically with the stateful MIME reader at both boundaries.

    The real two-pass classifier, the real ``draft_manifest``, the real MD-E2 installation
    and the real canonical rule fingerprint run unchanged.  Fakes exist only at the true
    external boundaries: message listing, the Full Read, the raw RFC-822 export and the
    MD-E1 backend.
    """
    data_dir = workspace / "data" / "mail-desk"
    data_dir.mkdir(parents=True, exist_ok=True)
    output_path = data_dir / "batch-manifest.json"
    merged: dict[str, Any] = {"count": 1, "output_file": str(output_path)}
    merged.update(dict(config or {}))
    dependencies = {
        "BatchProgressTracker": _NullTracker,
        "get_unprocessed_emails": make_get_unprocessed(email, reader, account),
        "get_single_email_details": make_full_reader(email),
        "load_sent_index": _no_sent_index,
        "attachment_evaluate": evaluate or make_evaluate_stub(),
        "fetch_raw_message_eml": reader,
    }
    result = _draft_mode.run_draft_mode(
        merged, account=account, data_dir=data_dir, dependencies=dependencies
    )
    return result, output_path
