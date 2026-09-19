"""Narrow context-bound receipt-class guard and internal machine-authorization factory.

FR-15 / MD-E1-T03 defines a deliberate new authorization form without turning the generic
MD-A2 review gate into a freely callable self-approval.  This module implements exactly two
seams:

* :func:`create_machine_authorization` -- the internal factory.  It mints the machine
  authorization (``receipt_class: "machine"``, ``receipt_type: "attachment_auto_evaluation"``)
  with the trusted bindings required by FR-15: a unique ``receipt_id``, a ``request_hash``
  equal to the canonical MD-A2 ``review_hash``, ``approved_at``, the fixed
  ``approved_by: "mail_desk_auto_evaluator"`` issuer, a policy revision and the
  account/message-ID/folder/envelope-ID/part-locator/inventory bindings.

* :func:`guard_context_authorization` -- the shared, context-aware guard.  The
  ``evaluation`` context accepts only the exact object instance issued by the factory; every
  Human-Approval context (filing, promotion, export, disposition, apply-discard and the
  direct/unrelated fetch path) rejects the machine class fail-closed.  The legacy, typeless
  FR-08 human MD-A2 receipts keep working unchanged, and no blanket schema migration is
  performed.

Trust boundary (read this before relying on the guard):

* The boundary is against external data -- caller arguments, mail content and manifest
  values.  Such data cannot forge evaluation authority: a structurally identical dictionary,
  a freshly constructed look-alike object, a ``copy`` or a pickled/unpickled structure are
  never accepted.
* This is deliberately **not** a Python access-control sandbox.  Arbitrary code that already
  executes inside this process and deliberately reaches into this module's private internals
  is outside the boundary, exactly like every other in-process capability.
* The factory is the only supported way to obtain a capability; it is the intended internal
  core API together with :func:`guard_context_authorization`.

Provenance design (security-relevant):

* The factory returns an opaque mapping object.  Authority is bound to the **exact issued
  object identity**, not to any value the object exposes.  A weak identity registry
  (``weakref.WeakKeyDictionary``) maps the issued object to the canonical content SHA-256
  recorded at mint time.  The registry cleans itself up when the capability is garbage
  collected, so there is no memory leak and no single-use requirement, and no token or secret
  is ever stored on the capability, serialized or logged.
* The guard first verifies that the registry record's weak reference ``is`` the exact object
  under test, then that the object's current content still hashes to the recorded value.
  Mutating any field (issuer, policy, request hash or binding) of a genuine object is
  therefore detected, and borrowing every exposed value to build a second object still fails.
* ``dict(apparent_receipt)``, ``copy.copy``/``copy.deepcopy`` and pickling all either drop the
  identity or raise, so none of them forge authority.
* The capability is intentionally process-internal and non-serializable.  It is minted and
  consumed in the same trusted control-plane process; later tickets (MD-E1-T05) that need to
  cross a process boundary must design an explicit, reviewed handoff rather than pickle this
  capability.

Validator reuse: the evaluation guard reuses
:func:`core.attachment_fetch.verify_approval_receipt` for the canonical request-hash and
receipt-structure check.  The disposition path's pre-existing
``attachment_disposition_log.validate_receipt_structure`` remains the downstream validator for
its own callsites and is **not** invoked by this guard.
"""

from __future__ import annotations

from collections.abc import Mapping
import hashlib
import json
import uuid
import weakref
from typing import Any

from core.attachment_quarantine_index import RFC3339_REGEX, SHA256_HEX_REGEX
from core.attachment_fetch import compute_review_hash, verify_approval_receipt
from core.common import normalize_message_id, utc_now_iso


# ==============================================================================
# Constants
# ==============================================================================

RECEIPT_CLASS_MACHINE = "machine"
RECEIPT_CLASS_HUMAN = "human"
RECEIPT_TYPE_ATTACHMENT_AUTO_EVALUATION = "attachment_auto_evaluation"
MACHINE_APPROVER_ID = "mail_desk_auto_evaluator"

CONTEXT_EVALUATION = "evaluation"
CONTEXT_FILING = "filing"
CONTEXT_PROMOTION = "promotion"
CONTEXT_EXPORT = "export"
CONTEXT_DISPOSITION = "disposition"
CONTEXT_APPLY = "apply_discard"
CONTEXT_DIRECT_FETCH = "direct_fetch"

#: Every Human-Approval context that must reject the machine class fail-closed.
HUMAN_APPROVAL_CONTEXTS = frozenset(
    {
        CONTEXT_FILING,
        CONTEXT_PROMOTION,
        CONTEXT_EXPORT,
        CONTEXT_DISPOSITION,
        CONTEXT_APPLY,
        CONTEXT_DIRECT_FETCH,
    }
)
ALLOWED_CONTEXTS = HUMAN_APPROVAL_CONTEXTS | {CONTEXT_EVALUATION}

MACHINE_BINDING_FIELDS: tuple[str, ...] = (
    "account",
    "message_id",
    "folder",
    "envelope_id",
    "part_locator",
    "inventory_sha256",
)
MACHINE_REQUIRED_FIELDS: tuple[str, ...] = (
    "receipt_id",
    "request_hash",
    "approved_at",
    "approved_by",
    "policy_revision",
    *MACHINE_BINDING_FIELDS,
)

_RECEIPT_ID_MAX_LENGTH = 128
_BINDING_MAX_LENGTH = 128


# ==============================================================================
# Exceptions
# ==============================================================================

class AttachmentAuthorizationError(ValueError):
    """Base exception for receipt-class / machine-authorization boundary errors."""


class UnknownAuthorizationContextError(AttachmentAuthorizationError):
    """Raised when the guard is asked to validate an unknown context."""


class ReceiptClassRejectedError(AttachmentAuthorizationError):
    """Raised when a Human-Approval context rejects the machine (or unknown) receipt class."""


class MachineAuthorizationRequiredError(AttachmentAuthorizationError):
    """Raised when the evaluation context is not given the exact internally issued capability."""


class MachineAuthorizationProvenanceError(MachineAuthorizationRequiredError):
    """Raised when an issued machine authorization was modified after it was minted."""


class MachineBindingError(AttachmentAuthorizationError):
    """Raised when a required trusted binding is missing, malformed, omitted or drifted."""


# ==============================================================================
# Opaque capability & weak identity registry
# ==============================================================================

class _MachineAuthorization:
    """Opaque mapping carrying a minted machine receipt.

    The class is private.  It is deliberately **not** a ``dict`` subclass, so it cannot leak
    through ``json``/``dict`` oriented serialization, and it carries no provenance value:
    authority is bound to the object identity through the module-level weak registry below.
    It hashes by identity (no ``__eq__`` override), which makes it usable as a weak
    dictionary key that only matches the exact issued object.
    """

    __slots__ = ("_receipt", "__weakref__")

    def __init__(self, receipt: Mapping[str, Any]) -> None:
        self._receipt: dict[str, Any] = {str(key): value for key, value in dict(receipt).items()}

    def __getitem__(self, key: str) -> Any:
        return self._receipt[key]

    def __setitem__(self, key: str, value: Any) -> None:
        self._receipt[key] = value

    def __delitem__(self, key: str) -> None:
        del self._receipt[key]

    def __contains__(self, key: object) -> bool:
        return key in self._receipt

    def __iter__(self):
        return iter(self._receipt)

    def __len__(self) -> int:
        return len(self._receipt)

    def keys(self):
        return self._receipt.keys()

    def items(self):
        return self._receipt.items()

    def values(self):
        return self._receipt.values()

    def get(self, key: str, default: Any = None) -> Any:
        return self._receipt.get(key, default)

    def to_dict(self) -> dict[str, Any]:
        """Return a plain snapshot (without authority) for inspection only."""
        return dict(self._receipt)

    def __repr__(self) -> str:
        # Bounded, non-secret shape only.
        return f"<MachineAuthorization keys={sorted(self._receipt)}>"

    def __copy__(self) -> "_MachineAuthorization":
        raise TypeError(
            "The machine authorization is a process-internal capability and cannot be copied."
        )

    def __deepcopy__(self, memo: Any) -> "_MachineAuthorization":
        raise TypeError(
            "The machine authorization is a process-internal capability and cannot be deep-copied."
        )

    def __reduce__(self) -> Any:
        raise TypeError(
            "The machine authorization is a process-internal capability and is not serializable."
        )

    def __reduce_ex__(self, protocol: int) -> Any:
        raise TypeError(
            "The machine authorization is a process-internal capability and is not serializable."
        )


# Expose the capability as a Mapping for generic receipt handling, without injecting
# ``__eq__``/``__hash__`` (virtual registration does not alter object identity semantics).
Mapping.register(_MachineAuthorization)


class _IssuedRecord:
    """Registry value that pins one issued capability and its mint-time content hash."""

    __slots__ = ("ref", "content_sha256")

    def __init__(self, capability: _MachineAuthorization, content_sha256: str) -> None:
        self.ref = weakref.ref(capability)
        self.content_sha256 = content_sha256


#: Identity registry: exact issued object -> its recorded content hash.  Weak keys guarantee
#: prompt cleanup once the capability is garbage collected.
_ISSUED_AUTHORIZATIONS: "weakref.WeakKeyDictionary[_MachineAuthorization, _IssuedRecord]" = (
    weakref.WeakKeyDictionary()
)


def _canonical_receipt_content_sha256(receipt: Mapping[str, Any]) -> str:
    """Deterministic 64-hex SHA-256 over the canonical receipt mapping (tamper binding)."""
    canonical_json = json.dumps(dict(receipt), sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical_json.encode("utf-8")).hexdigest()


# ==============================================================================
# Internal machine-authorization factory
# ==============================================================================

def _require_text(value: Any, field: str, *, max_length: int = _BINDING_MAX_LENGTH) -> str:
    if value is None:
        raise MachineBindingError(f"Machine authorization requires a non-empty '{field}' (got None).")
    text = str(value).strip()
    if not text:
        raise MachineBindingError(f"Machine authorization requires a non-empty '{field}'.")
    if len(text) > max_length:
        raise MachineBindingError(
            f"Machine authorization '{field}' exceeds maximum length {max_length}."
        )
    return text


def create_machine_authorization(
    *,
    request_hash: str,
    policy_revision: str,
    account: str,
    message_id: str,
    folder: str,
    envelope_id: str | int,
    part_locator: str,
    inventory_sha256: str,
    approved_at: str | None = None,
    receipt_id: str | None = None,
) -> _MachineAuthorization:
    """Mint the internal machine authorization for the bounded MD-E1 fetch/evaluate flow.

    The factory is the only supported way to obtain a capability the evaluation guard
    accepts.  It binds ``request_hash`` to the canonical MD-A2 ``review_hash`` recomputed from
    the trusted bindings, fixes the issuer to ``mail_desk_auto_evaluator`` and registers the
    exact issued object identity in the module-private weak registry.
    """
    norm_account = _require_text(account, "account")

    if message_id is None:
        raise MachineBindingError("Machine authorization requires a valid 'message_id' (got None).")
    norm_mid = normalize_message_id(str(message_id).strip())
    if not norm_mid:
        raise MachineBindingError("Machine authorization requires a valid 'message_id'.")

    norm_folder = _require_text(folder, "folder")
    norm_envelope = _require_text(envelope_id, "envelope_id")
    norm_locator = _require_text(part_locator, "part_locator")
    norm_policy = _require_text(policy_revision, "policy_revision")

    norm_inventory = _require_text(inventory_sha256, "inventory_sha256", max_length=64).lower()
    if not SHA256_HEX_REGEX.fullmatch(norm_inventory):
        raise MachineBindingError(
            "Machine authorization 'inventory_sha256' must be a 64-hex SHA-256."
        )

    norm_request_hash = _require_text(request_hash, "request_hash", max_length=64).lower()
    if not SHA256_HEX_REGEX.fullmatch(norm_request_hash):
        raise MachineBindingError("Machine authorization 'request_hash' must be a 64-hex SHA-256.")

    canonical_review_hash = compute_review_hash(
        account=norm_account,
        message_id=norm_mid,
        folder=norm_folder,
        envelope_id=norm_envelope,
        part_locator=norm_locator,
        inventory_sha256=norm_inventory,
    )
    if norm_request_hash != canonical_review_hash:
        raise MachineBindingError(
            "Machine authorization 'request_hash' must equal the canonical MD-A2 review_hash "
            f"recomputed from its bindings ({canonical_review_hash})."
        )

    norm_approved_at = utc_now_iso() if approved_at is None else str(approved_at).strip()
    if not RFC3339_REGEX.fullmatch(norm_approved_at):
        raise MachineBindingError(
            "Machine authorization 'approved_at' must be a valid RFC-3339 timestamp with timezone offset."
        )

    norm_receipt_id = (
        f"rcpt_machine_{uuid.uuid4().hex}" if receipt_id is None else str(receipt_id).strip()
    )
    if not norm_receipt_id or len(norm_receipt_id) > _RECEIPT_ID_MAX_LENGTH:
        raise MachineBindingError(
            f"Machine authorization 'receipt_id' must be 1-{_RECEIPT_ID_MAX_LENGTH} characters."
        )

    receipt: dict[str, Any] = {
        "receipt_class": RECEIPT_CLASS_MACHINE,
        "receipt_type": RECEIPT_TYPE_ATTACHMENT_AUTO_EVALUATION,
        "receipt_id": norm_receipt_id,
        "request_hash": norm_request_hash,
        "approved_at": norm_approved_at,
        "approved_by": MACHINE_APPROVER_ID,
        "policy_revision": norm_policy,
        "account": norm_account,
        "message_id": norm_mid,
        "folder": norm_folder,
        "envelope_id": norm_envelope,
        "part_locator": norm_locator,
        "inventory_sha256": norm_inventory,
    }

    capability = _MachineAuthorization(receipt)
    _ISSUED_AUTHORIZATIONS[capability] = _IssuedRecord(
        capability, _canonical_receipt_content_sha256(capability)
    )
    return capability


# ==============================================================================
# Shared context-aware guard
# ==============================================================================

def _is_machine_marked(receipt: Mapping[str, Any]) -> bool:
    receipt_class = receipt.get("receipt_class")
    receipt_type = receipt.get("receipt_type")
    approved_by = receipt.get("approved_by")
    if receipt_class is not None and str(receipt_class).strip().lower() == RECEIPT_CLASS_MACHINE:
        return True
    if receipt_type is not None and (
        str(receipt_type).strip().lower() == RECEIPT_TYPE_ATTACHMENT_AUTO_EVALUATION
    ):
        return True
    if approved_by is not None and str(approved_by).strip() == MACHINE_APPROVER_ID:
        return True
    return False


def _reject_non_human_receipt(receipt: Any, *, context: str) -> None:
    """Fail-closed if a Human-Approval context receives a machine/foreign receipt class.

    ``None`` and non-mapping receipts are left to the existing structure validators so the
    established ``ReceiptMissingError``/``ReceiptMalformedError`` contract is preserved.
    """
    if not isinstance(receipt, Mapping):
        return

    if _is_machine_marked(receipt):
        raise ReceiptClassRejectedError(
            f"The machine authorization class/type/issuer is not accepted as Human Approval "
            f"for context {context!r}."
        )

    receipt_class = receipt.get("receipt_class")
    if receipt_class is not None and str(receipt_class).strip().lower() not in ("", RECEIPT_CLASS_HUMAN):
        raise ReceiptClassRejectedError(
            f"Unknown receipt_class {receipt_class!r} is rejected for context {context!r}."
        )

    receipt_type = receipt.get("receipt_type")
    if receipt_type is not None and str(receipt_type).strip() != "":
        raise ReceiptClassRejectedError(
            f"Unknown receipt_type {receipt_type!r} is rejected for context {context!r}."
        )


def _guard_evaluation(
    receipt: Any,
    *,
    expected_request_hash: str | None,
    expected_policy_revision: str | None,
) -> dict[str, Any]:
    # The evaluation context must be driven by explicit trusted expectations.  Omitting either
    # one is a caller contract bug and never authorizes evaluation.
    if expected_request_hash is None or not str(expected_request_hash).strip():
        raise MachineBindingError(
            "Evaluation requires an explicit, non-empty trusted expected_request_hash."
        )
    if expected_policy_revision is None or not str(expected_policy_revision).strip():
        raise MachineBindingError(
            "Evaluation requires an explicit, non-empty trusted expected_policy_revision."
        )

    if not isinstance(receipt, _MachineAuthorization):
        raise MachineAuthorizationRequiredError(
            "Evaluation authorizes only the exact internally issued machine authorization; a "
            "caller/mail/manifest-supplied dictionary or look-alike object is not authority."
        )

    record = _ISSUED_AUTHORIZATIONS.get(receipt)
    if record is None or record.ref() is not receipt:
        raise MachineAuthorizationRequiredError(
            "Evaluation authorizes only the exact internally issued object instance; this "
            "capability was not issued by the internal factory."
        )
    if _canonical_receipt_content_sha256(receipt) != record.content_sha256:
        raise MachineAuthorizationProvenanceError(
            "Machine authorization content was modified after it was minted."
        )

    if str(receipt.get("receipt_class", "")).strip().lower() != RECEIPT_CLASS_MACHINE:
        raise MachineAuthorizationProvenanceError(
            "Machine authorization must carry receipt_class 'machine'."
        )
    if (
        str(receipt.get("receipt_type", "")).strip().lower()
        != RECEIPT_TYPE_ATTACHMENT_AUTO_EVALUATION
    ):
        raise MachineAuthorizationProvenanceError(
            "Machine authorization must carry receipt_type 'attachment_auto_evaluation'."
        )
    if str(receipt.get("approved_by", "")).strip() != MACHINE_APPROVER_ID:
        raise MachineAuthorizationProvenanceError(
            f"Machine authorization issuer must be {MACHINE_APPROVER_ID!r}."
        )

    missing = [field for field in MACHINE_REQUIRED_FIELDS if not str(receipt.get(field, "")).strip()]
    if missing:
        raise MachineBindingError(
            f"Machine authorization is missing required binding(s): {sorted(missing)}."
        )

    norm_inventory = str(receipt["inventory_sha256"]).strip().lower()
    if not SHA256_HEX_REGEX.fullmatch(norm_inventory):
        raise MachineBindingError(
            "Machine authorization 'inventory_sha256' must be a 64-hex SHA-256."
        )
    normalized_mid = normalize_message_id(str(receipt["message_id"]).strip())
    if not normalized_mid:
        raise MachineBindingError("Machine authorization 'message_id' is invalid.")

    canonical_review_hash = compute_review_hash(
        account=receipt["account"],
        message_id=normalized_mid,
        folder=receipt["folder"],
        envelope_id=receipt["envelope_id"],
        part_locator=receipt["part_locator"],
        inventory_sha256=norm_inventory,
    )
    if str(receipt["request_hash"]).strip().lower() != canonical_review_hash:
        raise MachineBindingError(
            "Machine authorization 'request_hash' does not equal the canonical review_hash "
            "recomputed from its bindings."
        )

    norm_expected = str(expected_request_hash).strip().lower()
    if canonical_review_hash != norm_expected:
        raise MachineBindingError(
            "Machine authorization request binding drift: the recomputed review_hash "
            f"'{canonical_review_hash}' does not match the expected '{norm_expected}'."
        )
    if str(receipt["policy_revision"]).strip() != str(expected_policy_revision).strip():
        raise MachineBindingError(
            "Machine authorization policy revision drift: "
            f"'{receipt['policy_revision']}' != expected '{expected_policy_revision}'."
        )

    # Reuse the existing hash/request validator instead of duplicating it.  It requires a
    # plain mapping, so hand it a snapshot of the already identity- and content-verified data.
    verify_approval_receipt(dict(receipt), expected_review_hash=canonical_review_hash)

    return {
        "class": "machine",
        "context": CONTEXT_EVALUATION,
        "authorization": "auto_evaluated",
        "request_hash": canonical_review_hash,
        "policy_revision": str(receipt["policy_revision"]).strip(),
        "receipt_id": str(receipt["receipt_id"]).strip(),
    }


def guard_context_authorization(
    receipt: Any,
    *,
    context: str,
    expected_request_hash: str | None = None,
    expected_policy_revision: str | None = None,
) -> dict[str, Any]:
    """Context-aware receipt-class guard shared by every callsite.

    * ``evaluation``: accepts only the exact object instance issued by
      :func:`create_machine_authorization`, and requires explicit non-empty
      ``expected_request_hash`` and ``expected_policy_revision``.
    * any Human-Approval context: rejects the machine class/type/issuer fail-closed and
      returns ``{"class": "human"}`` so the caller's existing validator runs unchanged.
      These contexts do not require (and ignore) the evaluation expectations.
    """
    if context not in ALLOWED_CONTEXTS:
        raise UnknownAuthorizationContextError(f"Unknown authorization context: {context!r}")

    if context == CONTEXT_EVALUATION:
        return _guard_evaluation(
            receipt,
            expected_request_hash=expected_request_hash,
            expected_policy_revision=expected_policy_revision,
        )

    _reject_non_human_receipt(receipt, context=context)
    return {"class": "human", "context": context}


__all__ = [
    "ALLOWED_CONTEXTS",
    "CONTEXT_APPLY",
    "CONTEXT_DIRECT_FETCH",
    "CONTEXT_DISPOSITION",
    "CONTEXT_EVALUATION",
    "CONTEXT_EXPORT",
    "CONTEXT_FILING",
    "CONTEXT_PROMOTION",
    "HUMAN_APPROVAL_CONTEXTS",
    "MACHINE_APPROVER_ID",
    "MACHINE_BINDING_FIELDS",
    "MACHINE_REQUIRED_FIELDS",
    "MachineAuthorizationProvenanceError",
    "MachineAuthorizationRequiredError",
    "MachineBindingError",
    "RECEIPT_CLASS_HUMAN",
    "RECEIPT_CLASS_MACHINE",
    "RECEIPT_TYPE_ATTACHMENT_AUTO_EVALUATION",
    "AttachmentAuthorizationError",
    "ReceiptClassRejectedError",
    "UnknownAuthorizationContextError",
    "create_machine_authorization",
    "guard_context_authorization",
]
