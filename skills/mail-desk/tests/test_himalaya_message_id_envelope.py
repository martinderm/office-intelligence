"""HMI-1 behavior tests for the additive normalized ``message_id`` on ``envelope list``.

Evidence mode ``tdd``, risk tier ``low``.  The ``Target...`` classes pin the new
contract and are Red before the production change in
``mail_desk_himalaya_client.op_list_envelopes``; the ``...Characterization``
class pins the 1.2.0/default output that must stay green before and after.

A newer Himalaya version exposes ``Envelope.message_id`` as the camelCase JSON
field ``messageId`` on ``envelope list -o json`` entries.  The documented list
seam is ``op_list_envelopes`` (``skills/mail-desk/scripts/
mail_desk_himalaya_client.py``), which today returns the raw parsed list.

Intended HMI-1 contract encoded here (additive only, issue #103):
  * when an entry carries a usable camelCase ``messageId`` (or the snake_case
    ``message_id`` variant), the returned entry gains a normalized
    ``message_id``: lowercase, no angle brackets, produced through
    ``core.common.normalize_message_id`` and well formed per
    ``core.known_skip.is_well_formed_message_id`` (exactly one ``@``, no
    whitespace/control characters, no angle brackets, non-empty local/domain
    parts, no ``@generated`` domain);
  * absent, ``null``, empty/whitespace, non-string and invalid values leave the
    entry untouched and raise nothing;
  * when both keys are present they must agree after normalization; a conflict
    leaves the entry untouched;
  * duplicate envelope ids keep a messageId only when every occurrence yields
    the identical normalized value, conflicting duplicates drop it from every
    occurrence, and the same messageId on distinct ids is not a conflict;
  * a non-list JSON root passes through unchanged;
  * the 1.2.0/default output (no such field) stays byte-identical, including key
    order, no new key appears, and no entry object is destructively mutated.

Everything here is hermetic: ``client.run_himalaya`` is patched with the
fixture's ``envelope_list_json``; no Himalaya process, mailbox, network, temp
file or file mutation is involved.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
import sys
import unittest
from typing import Any
from unittest.mock import patch

MAIL_DESK_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(MAIL_DESK_ROOT / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import mail_desk_himalaya_client as client  # noqa: E402
from himalaya_fixtures import envelope_list_json  # noqa: E402

#: A realistic 1.2.0 ``envelope list -o json`` entry: no ``messageId`` field.
OLD_ENVELOPE: dict[str, Any] = {
    "id": "4711",
    "subject": "Prüfung Unterlagen",
    "from": {"name": "Ada Lovelace", "addr": "ada@example.org"},
    "date": "2026-11-27T09:00:00Z",
    "flags": ["seen"],
}

#: A second default entry proving order and multiple elements stay intact.
OLD_SECOND_ENVELOPE: dict[str, Any] = {
    "id": "4712",
    "subject": "Re: Prüfung Unterlagen",
    "from": {"name": "Grace Hopper", "addr": "grace@example.org"},
    "date": "2026-11-27T09:05:00Z",
    "flags": [],
}


def _list_envelopes(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Run ``op_list_envelopes`` once against a faked ``envelope list`` payload."""
    with patch.object(
        client, "run_himalaya", return_value=envelope_list_json(entries)
    ) as runner:
        result = client.op_list_envelopes(folder="INBOX")
    if runner.call_count != 1:
        raise AssertionError(
            f"op_list_envelopes must issue exactly one list command, "
            f"observed {runner.call_count}"
        )
    return result


def _list_raw_payload(payload: Any) -> Any:
    """Run ``op_list_envelopes`` once against an arbitrary JSON root."""
    with patch.object(client, "run_himalaya", return_value=json.dumps(payload)):
        return client.op_list_envelopes(folder="INBOX")


class CharacterizationDefaultOutputTests(unittest.TestCase):
    """Green now and after: 1.2.0 output without the field is untouched."""

    def test_old_json_is_byte_identical_including_key_order(self) -> None:
        """Default entries serialize exactly as before, key order included."""
        entries = [copy.deepcopy(OLD_ENVELOPE), copy.deepcopy(OLD_SECOND_ENVELOPE)]
        result = _list_envelopes(entries)
        self.assertEqual(
            json.dumps(entries, ensure_ascii=False),
            json.dumps(result, ensure_ascii=False),
        )

    def test_old_json_entries_gain_no_new_key(self) -> None:
        """No additive key leaks into an entry that never carried the field."""
        entries = [copy.deepcopy(OLD_ENVELOPE), copy.deepcopy(OLD_SECOND_ENVELOPE)]
        result = _list_envelopes(entries)
        for source, returned in zip(entries, result):
            self.assertEqual(list(source), list(returned))
            self.assertNotIn("message_id", returned)
            self.assertEqual(set(source), set(returned))

    def test_old_json_entries_are_not_destructively_mutated(self) -> None:
        """The source entry objects passed to the seam stay byte-identical."""
        entries = [copy.deepcopy(OLD_ENVELOPE), copy.deepcopy(OLD_SECOND_ENVELOPE)]
        snapshot = copy.deepcopy(entries)
        _list_envelopes(entries)
        self.assertEqual(snapshot, entries)

    def test_empty_envelope_list_stays_empty(self) -> None:
        """An empty list stays an empty list."""
        self.assertEqual([], _list_envelopes([]))


class TargetCamelCaseMessageIdTests(unittest.TestCase):
    """Red now: a usable camelCase ``messageId`` yields a normalized ``message_id``."""

    def test_bracketed_and_mixed_case_values_are_normalized(self) -> None:
        """Brackets, whitespace and case are removed through normalization."""
        cases = (
            ("bracketed-mixed-case", "<ABC@Example.COM>", "abc@example.com"),
            ("bare-mixed-case", "Plain@Example.COM", "plain@example.com"),
            ("padded-brackets", "  <Pad@Example.COM>  ", "pad@example.com"),
        )
        for label, raw, expected in cases:
            with self.subTest(case=label):
                entry = {
                    "id": "42",
                    "subject": "Subject",
                    "messageId": raw,
                }
                result = _list_envelopes([dict(entry)])
                self.assertEqual(expected, result[0].get("message_id"))

    def test_normalization_is_additive_and_preserves_the_raw_field(self) -> None:
        """Only ``message_id`` is added; original keys and raw values survive."""
        entry = {
            "id": "42",
            "subject": "Subject",
            "messageId": "<ABC@Example.COM>",
        }
        result = _list_envelopes([dict(entry)])
        self.assertEqual({"message_id"}, set(result[0]) - set(entry))
        for key, value in entry.items():
            self.assertIn(key, result[0])
            self.assertEqual(value, result[0][key])
        self.assertEqual("abc@example.com", result[0]["message_id"])


class TargetSnakeCaseMessageIdTests(unittest.TestCase):
    """Red now: the snake_case ``message_id`` variant is recognized as well."""

    def test_snake_case_variant_is_normalized_in_place(self) -> None:
        """A snake_case value is normalized rather than left raw."""
        entry = {
            "id": "42",
            "subject": "Subject",
            "message_id": "<Snake@Example.COM>",
        }
        result = _list_envelopes([dict(entry)])
        self.assertEqual("snake@example.com", result[0].get("message_id"))
        self.assertEqual("42", result[0]["id"])
        self.assertEqual("Subject", result[0]["subject"])
        self.assertEqual(set(entry), set(result[0]))

    def test_both_keys_agreeing_yield_one_normalized_value(self) -> None:
        """Two agreeing spellings produce exactly one normalized ``message_id``."""
        entry = {
            "id": "42",
            "subject": "Subject",
            "messageId": "<Agree@Example.COM>",
            "message_id": "<agree@example.com>",
        }
        result = _list_envelopes([dict(entry)])
        self.assertEqual("agree@example.com", result[0].get("message_id"))
        self.assertEqual(set(entry), set(result[0]))
        self.assertEqual("<Agree@Example.COM>", result[0]["messageId"])


class TargetConflictingKeysTests(unittest.TestCase):
    """Red now: a camelCase/snake_case conflict never guesses."""

    def test_conflicting_keys_leave_the_entry_untouched(self) -> None:
        """Conflicting raw values add no normalized field and change nothing."""
        entry = {
            "id": "42",
            "subject": "Subject",
            "messageId": "<One@Example.COM>",
            "message_id": "<Two@Example.COM>",
        }
        result = _list_envelopes([dict(entry)])
        self.assertEqual(entry, result[0])
        self.assertNotEqual("one@example.com", result[0].get("message_id"))
        self.assertNotEqual("two@example.com", result[0].get("message_id"))


class TargetUnusableValuesTests(unittest.TestCase):
    """Red now: absent/null/empty/invalid/non-string values stay silent."""

    #: Values that must never produce a normalized ``message_id``.
    UNUSABLE_VALUES: tuple[tuple[str, Any], ...] = (
        ("null", None),
        ("empty", ""),
        ("whitespace", "   \t"),
        ("no-at-sign", "no-at-sign"),
        ("multiple-at-signs", "a@b@c"),
        ("inner-whitespace", "a b@c"),
        ("control-character", "a@b\u0000c"),
        ("surviving-left-bracket", "a<b@c"),
        ("surviving-right-bracket", "a@b>"),
        ("empty-local-part", "@example.org"),
        ("empty-domain-part", "foo@"),
        ("generated-domain", "<foo@generated>"),
        ("boolean", True),
        ("integer", 42),
        ("list", ["<a@b>"]),
        ("object", {"value": "<a@b>"}),
    )

    def test_unusable_camel_case_values_leave_the_entry_untouched(self) -> None:
        """Every unusable value adds no field and raises nothing."""
        for label, value in self.UNUSABLE_VALUES:
            with self.subTest(case=label):
                entry = {"id": "42", "subject": "Subject", "messageId": value}
                result = _list_envelopes([copy.deepcopy(entry)])
                self.assertEqual(entry, result[0])
                self.assertNotIn("message_id", result[0])

    def test_unusable_snake_case_values_leave_the_entry_untouched(self) -> None:
        """The snake_case variant is equally fail-closed for unusable values."""
        for label, value in self.UNUSABLE_VALUES:
            with self.subTest(case=label):
                entry = {"id": "42", "subject": "Subject", "message_id": value}
                result = _list_envelopes([copy.deepcopy(entry)])
                self.assertEqual(entry, result[0])


class TargetDuplicateEnvelopeIdTests(unittest.TestCase):
    """Red now: duplicate ids resolve per the documented duplicate rule."""

    def test_duplicate_id_with_same_normalized_value_keeps_it(self) -> None:
        """Two spellings of one value on the same id keep the normalized field."""
        entries = [
            {"id": "7", "subject": "A", "messageId": "<Dup@Example.COM>"},
            {"id": "7", "subject": "B", "messageId": "dup@example.com"},
        ]
        result = _list_envelopes(entries)
        self.assertEqual("dup@example.com", result[0].get("message_id"))
        self.assertEqual("dup@example.com", result[1].get("message_id"))
        self.assertEqual("A", result[0]["subject"])
        self.assertEqual("B", result[1]["subject"])

    def test_duplicate_id_with_conflicting_values_drops_it_everywhere(self) -> None:
        """A conflicting duplicate yields no normalized field on any occurrence."""
        entries = [
            {"id": "7", "subject": "A", "messageId": "<Left@Example.COM>"},
            {"id": "7", "subject": "B", "messageId": "<Right@Example.COM>"},
        ]
        result = _list_envelopes(entries)
        for source, returned in zip(entries, result):
            self.assertNotIn("message_id", returned)
            self.assertEqual(source, returned)

    def test_duplicate_id_with_one_unusable_occurrence_drops_it_everywhere(
        self,
    ) -> None:
        """An unusable occurrence of a repeated id voids the value on all of them.

        The duplicate rule requires *every* occurrence to agree on a usable
        value; one occurrence without a usable value must therefore not be
        outvoted by the other, in either list order.
        """
        unusable_cases: tuple[tuple[str, Any], ...] = (
            ("null", None),
            ("invalid", "no-at-sign"),
        )
        for label, unusable in unusable_cases:
            usable = {"id": "7", "subject": "A", "messageId": "<Dup@Example.COM>"}
            missing = {"id": "7", "subject": "B", "messageId": unusable}
            for order, entries in (
                ("usable-then-unusable", [usable, missing]),
                ("unusable-then-usable", [missing, usable]),
            ):
                with self.subTest(case=label, order=order):
                    snapshot = copy.deepcopy(entries)
                    result = _list_envelopes(entries)
                    for source, returned in zip(snapshot, result):
                        self.assertNotIn("message_id", returned)
                        self.assertEqual(source, returned)

    def test_same_value_on_distinct_ids_is_not_a_conflict(self) -> None:
        """Distinct envelope ids may share one normalized messageId."""
        entries = [
            {"id": "7", "subject": "A", "messageId": "<Shared@Example.COM>"},
            {"id": "8", "subject": "B", "messageId": "<Shared@Example.COM>"},
        ]
        result = _list_envelopes(entries)
        self.assertEqual("shared@example.com", result[0].get("message_id"))
        self.assertEqual("shared@example.com", result[1].get("message_id"))
        self.assertEqual("7", result[0]["id"])
        self.assertEqual("8", result[1]["id"])


class TargetNonListRootTests(unittest.TestCase):
    """Red now: a non-list JSON root is returned unchanged."""

    def test_non_list_roots_pass_through_unchanged(self) -> None:
        """Dict, string, integer and null roots are not treated as envelopes."""
        payloads: tuple[tuple[str, Any], ...] = (
            ("object", {"error": "mailbox_unavailable", "code": 7}),
            ("string", "not-a-list"),
            ("integer", 42),
            ("null", None),
        )
        for label, payload in payloads:
            with self.subTest(root=label):
                result = _list_raw_payload(payload)
                self.assertEqual(payload, result)
                self.assertEqual(
                    json.dumps(payload, ensure_ascii=False),
                    json.dumps(result, ensure_ascii=False),
                )


if __name__ == "__main__":
    unittest.main()
