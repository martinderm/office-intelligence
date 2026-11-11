"""Issue #103: CLI batch skip over ``envelope thread`` + transport counters.

Covers the fail-closed rules F1-F6 and the offline acceptance points AK1-AK4:

* F1  one successful thread process with valid JSON array of 3-tuples
* F2  decimal id != 0, exactly one well-formed Message-ID (normalized check)
* F3  binding requires the identical id and subject from both CLI outputs
* F4  a map hit never produces ``collected`` data
* F5  a missing id is unknown and is read individually
* F6  two consecutive transport failures abort without another process start

All tests are hermetic: ``run_himalaya``/``get_single_email_details`` are
patched at the runner seam, and the transport guard is driven through the real
``run_himalaya`` with only the process boundary faked.
"""

from __future__ import annotations

import concurrent.futures
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

MAIL_DESK_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(MAIL_DESK_ROOT / "scripts"))

from core import himalaya  # noqa: E402
from core import classifier  # noqa: E402
from core.common import normalize_message_id  # noqa: E402
from core.himalaya import HimalayaInvocationError  # noqa: E402
from core.known_skip import (  # noqa: E402
    canonical_envelope_id,
    is_well_formed_message_id,
    load_envelope_thread_map,
    parse_envelope_thread_map,
    thread_map_hit,
)
from core.modes import inspect as inspect_mode  # noqa: E402
import mail_desk_batch_runner as runner  # noqa: E402


def _node(envelope_id: str, message_id: str, subject: str) -> dict:
    return {"id": envelope_id, "message-id": message_id, "subject": subject}


def _thread_payload(nodes: list[dict]) -> str:
    """Render a real ``envelope thread -o json`` root.

    ``ThreadedEnvelopes`` serializes its graph as a JSON array of
    ``[source, target, weight]`` triples.  Each non-reply envelope is a target
    of the synthetic ``id: "0"`` root, which is exactly the shape this helper
    produces.
    """
    return json.dumps([[_node("0", "0", ""), node, 0] for node in nodes])


def _edges(*edges: list) -> str:
    """Render an explicit edge list for structural/ambiguity fixtures."""
    return json.dumps(list(edges))


def _envelopes(count: int, *, subject_prefix: str = "message") -> list[dict]:
    return [
        {"id": str(number), "subject": f"{subject_prefix} {number}"}
        for number in range(1, count + 1)
    ]


def _known_items(count: int, *, lower_bound: int = 1) -> dict:
    return {
        f"{number}@example.test": {"message_id": f"{number}@example.test", "final_folder": "Archive"}
        for number in range(lower_bound, count + 1)
    }


class _FakeProcess:
    """Minimal Popen stand-in for the stats-active process gate."""

    def __init__(self, response: subprocess.CompletedProcess) -> None:
        self._response = response
        self.returncode = response.returncode
        self.killed = False

    def communicate(self, input=None, timeout=None):  # noqa: A002 - Popen signature
        if self.killed:
            return self._response.stdout, self._response.stderr
        return self._response.stdout, self._response.stderr

    def kill(self) -> None:
        self.killed = True


class MessageIdWellFormednessTests(unittest.TestCase):
    """F2: well-formedness is checked on the normalized form (both spellings)."""

    def test_angle_bracketed_and_bare_spellings_are_both_valid(self) -> None:
        self.assertTrue(is_well_formed_message_id("<A@Example.test>"))
        self.assertTrue(is_well_formed_message_id("A@Example.test"))
        self.assertTrue(is_well_formed_message_id("  <a.b+c@sub.example.test>  "))

    def test_invalid_normalized_values_are_rejected(self) -> None:
        for value in (
            "",
            "a@@b",
            "a b@c",
            "a@b\x00",
            "a@b\tc",
            "<a>@b",
            "a@generated",
            "a@Generated",
            "@b",
            "a@",
            "<>",
        ):
            with self.subTest(value=value):
                self.assertFalse(is_well_formed_message_id(value))
        for value in (None, 42, ["a@b"], {"id": "a@b"}):
            with self.subTest(value=value):
                self.assertFalse(is_well_formed_message_id(value))

    def test_thread_and_read_spellings_differ_only_by_angle_brackets(self) -> None:
        # Gate-0 offline rule: the thread command wraps the value in <...>, the
        # read command does not; both must normalize to the same valid form.
        thread_value = "<A@Example.test>"
        read_value = "A@Example.test"
        self.assertEqual(2, len(thread_value) - len(read_value))
        self.assertEqual(normalize_message_id(thread_value), normalize_message_id(read_value))
        self.assertTrue(is_well_formed_message_id(thread_value))
        self.assertTrue(is_well_formed_message_id(read_value))

    def test_canonical_envelope_id_is_decimal_and_not_zero(self) -> None:
        self.assertEqual("7", canonical_envelope_id("7"))
        # F3 requires the identical id: leading zeros are significant.
        self.assertEqual("007", canonical_envelope_id("007"))
        self.assertEqual("7", canonical_envelope_id(7))
        for value in (
            "0", "00", "000", 0, "-1", "1.0", "", " ", "abc", True, None,
            " 7", "7 ", " 7 ", "\t7\n", "\u00a07",
        ):
            with self.subTest(value=value):
                self.assertIsNone(canonical_envelope_id(value))

    def test_whitespace_ids_never_enter_the_thread_map(self) -> None:
        mapping = parse_envelope_thread_map(json.loads(_thread_payload([
            _node(" 7", "<seven@example.test>", "Seven"),
            _node("8 ", "<eight@example.test>", "Eight"),
            _node("9", "<nine@example.test>", "Nine"),
        ])))
        self.assertEqual({"9"}, set(mapping))

    def test_extremely_long_decimal_ids_never_use_int_conversion(self) -> None:
        # Python 3.12 raises ValueError above 4300 digits for int(); an envelope
        # id is an opaque decimal locator and must stay fail-closed instead.
        huge = "9" * 10_000
        self.assertEqual(huge, canonical_envelope_id(huge))
        prefixed = "0" * 10_000 + "42"
        self.assertEqual(prefixed, canonical_envelope_id(prefixed))
        self.assertIsNone(canonical_envelope_id("0" * 10_000))
        self.assertIsNone(canonical_envelope_id("9" * 10_000 + "x"))

    def test_thread_map_and_lookup_require_the_identical_id(self) -> None:
        # F3: "0007" and "7" are different CLI ids; never bind them.
        mapping = parse_envelope_thread_map(json.loads(_thread_payload([
            _node("0007", "<seven@example.test>", "Seven"),
        ])))
        self.assertEqual({"0007"}, set(mapping))
        self.assertIsNotNone(thread_map_hit(mapping, {"id": "0007", "subject": "Seven"}))
        self.assertIsNone(thread_map_hit(mapping, {"id": "7", "subject": "Seven"}))


class EnvelopeThreadMapTests(unittest.TestCase):
    """F1/F2: only a valid root and valid nodes contribute entries."""

    def test_valid_root_and_nodes_build_the_map(self) -> None:
        mapping = parse_envelope_thread_map(json.loads(_thread_payload([
            _node("1", "<One@Example.test>", "Subject one"),
            _node("2", "two@example.test", "Subject two"),
        ])))

        self.assertEqual(
            {"1": ("one@example.test", "Subject one"), "2": ("two@example.test", "Subject two")},
            {key: (entry.message_id, entry.subject) for key, entry in mapping.items()},
        )

    def test_root_must_be_an_array_of_three_element_graph_edges(self) -> None:
        node = _node("1", "<one@example.test>", "One")
        self.assertEqual({}, parse_envelope_thread_map({"edges": []}))
        self.assertEqual({}, parse_envelope_thread_map([[], []]))
        self.assertEqual({}, parse_envelope_thread_map([[[], []]]))
        self.assertEqual({}, parse_envelope_thread_map([[node, node]]))
        self.assertEqual({}, parse_envelope_thread_map([[node, node, "0"]]))
        self.assertEqual({}, parse_envelope_thread_map([[node, node, True]]))
        self.assertEqual({}, parse_envelope_thread_map([[node, node, 256]]))
        self.assertEqual({}, parse_envelope_thread_map([[node, [], 0]]))
        self.assertEqual({}, parse_envelope_thread_map([[node, node, 0, 1]]))
        self.assertEqual({}, parse_envelope_thread_map("[]"))

    def test_upstream_source_target_weight_shape_is_accepted(self) -> None:
        def upstream_node(envelope_id: str, message_id: str, subject: str) -> dict:
            # Exact serde shape of email-lib 0.27.0 ThreadedEnvelope.
            return {
                "id": envelope_id,
                "message-id": message_id,
                "from": "Alice <alice@example.test>",
                "subject": subject,
                "date": "2026-11-11T10:00:00+00:00",
            }

        mapping = parse_envelope_thread_map(json.loads(_edges(
            [upstream_node("0", "0", ""), upstream_node("1", "<one@example.test>", "One"), 0],
            [
                upstream_node("1", "<one@example.test>", "One"),
                upstream_node("2", "<two@example.test>", "Two"),
                1,
            ],
        )))
        self.assertEqual({"1", "2"}, set(mapping))
        self.assertEqual("one@example.test", mapping["1"].message_id)

    def test_nested_lookalike_nodes_do_not_contribute(self) -> None:
        node = _node("1", "<one@example.test>", "One")
        node["meta"] = {"id": "9", "message-id": "<nine@example.test>", "subject": "Nine"}
        mapping = parse_envelope_thread_map(json.loads(_thread_payload([node])))
        self.assertEqual({"1"}, set(mapping))

    def test_invalid_nodes_are_skipped_not_guessed(self) -> None:
        mapping = parse_envelope_thread_map(json.loads(_thread_payload([
            _node("1", "<valid@example.test>", "ok"),
            _node("0", "<zero@example.test>", "zero"),
            _node("2", "<a@@b>", "two ats"),
            _node("3", "<a@generated>", "generated"),
            {"id": "4", "message-id": "<four@example.test>"},
            _node("5", "<five@example.test>", "five"),
        ])))

        self.assertEqual({"1", "5"}, set(mapping))

    def test_duplicate_id_with_different_identity_is_dropped(self) -> None:
        mapping = parse_envelope_thread_map(json.loads(_thread_payload([
            _node("1", "<first@example.test>", "Subject"),
            _node("1", "<second@example.test>", "Subject"),
            _node("2", "<two@example.test>", "Two"),
        ])))

        self.assertEqual({"2"}, set(mapping))

    def test_duplicate_id_with_identical_identity_is_kept(self) -> None:
        mapping = parse_envelope_thread_map(json.loads(_thread_payload([
            _node("1", "<same@example.test>", "Subject"),
            _node("1", "<SAME@example.test>", "Subject"),
        ])))

        self.assertEqual("same@example.test", mapping["1"].message_id)

    def test_invalid_duplicate_drops_the_id_fail_closed(self) -> None:
        cases = (
            # valid first, invalid duplicate second
            [_node("1", "<one@example.test>", "One"), _node("1", "<a@@b>", "One")],
            # invalid first, valid duplicate second
            [_node("1", "<a@@b>", "One"), _node("1", "<one@example.test>", "One")],
            # subject of the duplicate is not a string
            [_node("1", "<one@example.test>", "One"), {"id": "1", "message-id": "<one@example.test>", "subject": 7}],
        )
        for nodes in cases:
            with self.subTest(nodes=nodes):
                self.assertEqual({}, parse_envelope_thread_map(json.loads(_thread_payload(nodes))))

    def test_thread_map_loader_discards_map_on_any_process_or_json_failure(self) -> None:
        folder = "INBOX"

        def raises(error):
            def call(*_args, **_kwargs):
                raise error
            return call

        cases = (
            raises(RuntimeError("boom")),
            raises(HimalayaInvocationError("himalaya_command_failed", "failed")),
            Mock(return_value="not json"),
            Mock(return_value=json.dumps({"not": "a list"})),
            Mock(return_value=json.dumps([{"id": "1"}])),
        )
        for run_himalaya in cases:
            with self.subTest(case=run_himalaya):
                self.assertEqual(
                    {},
                    load_envelope_thread_map(folder, account=None, run_himalaya_fn=run_himalaya),
                )

    def test_thread_map_loader_uses_exactly_one_process_without_retry(self) -> None:
        run_himalaya = Mock(return_value=_thread_payload([_node("1", "<a@b.test>", "s")]))
        mapping = load_envelope_thread_map("INBOX", account="demo", run_himalaya_fn=run_himalaya)

        self.assertEqual({"1"}, set(mapping))
        run_himalaya.assert_called_once()
        args, kwargs = run_himalaya.call_args
        self.assertEqual(["envelope", "thread", "-f", "INBOX", "-o", "json"], args[0])
        self.assertEqual(1, kwargs["max_retries"])
        self.assertEqual(60, kwargs["timeout"])

    def test_blocked_transport_is_never_swallowed(self) -> None:
        blocked = HimalayaInvocationError("mailbox_transport_blocked", "blocked")
        with self.assertRaises(HimalayaInvocationError) as raised:
            load_envelope_thread_map(
                "INBOX", account=None, run_himalaya_fn=Mock(side_effect=blocked)
            )
        self.assertEqual("mailbox_transport_blocked", raised.exception.reason_code)


class SkipScanTests(unittest.TestCase):
    """AK1/AK2 and F3/F4/F5 at the ``get_unprocessed_emails`` seam."""

    def _scan(
        self,
        *,
        envelopes: list[dict],
        thread_output: str,
        known: dict,
        target: int = 10,
        reads: Mock | None = None,
        folder: str = "INBOX",
    ):
        reads = reads or Mock(
            side_effect=lambda eid, *_a, **_k: {
                "envelope_id": str(eid),
                "message_id": f"{eid}@example.test",
                "subject": "",
            }
        )

        def run_himalaya(args, **_kwargs):
            if args[:2] == ["envelope", "thread"]:
                return thread_output
            if "envelope" in args and "list" in args:
                return json.dumps(envelopes)
            raise AssertionError(f"unexpected command: {args}")

        with tempfile.TemporaryDirectory() as temporary, \
                patch.object(runner, "load_final_index", return_value={"items": known}), \
                patch.object(runner, "build_signature_index", return_value={}), \
                patch.object(runner, "run_himalaya", side_effect=run_himalaya), \
                patch.object(runner, "get_single_email_details", reads), \
                patch.object(runner.time, "sleep"):
            collected, known_count, skipped_known = runner._scan_unprocessed_emails(
                folder, target, data_dir=Path(temporary),
            )
        return collected, known_count, skipped_known, reads

    def test_ak1_full_map_skips_known_without_reads_and_bounded_starts(self) -> None:
        envelopes = [
            {**envelope, "has_attachment": False}
            for envelope in _envelopes(300)
        ]
        known = _known_items(182)
        thread_output = _thread_payload([
            _node(str(number), f"<{number}@example.test>", f"message {number}")
            for number in range(1, 301)
        ])

        def fake_run(cmd, **_kwargs):
            if "envelope" in cmd and "thread" in cmd:
                stdout = thread_output
            elif "envelope" in cmd and "list" in cmd:
                stdout = json.dumps(envelopes)
            elif "message" in cmd and "read" in cmd:
                envelope_id = cmd[-1]
                stdout = (
                    f"Message-Id: <{envelope_id}@example.test>\n"
                    f"Subject: message {envelope_id}\n\n"
                )
            else:
                raise AssertionError(f"unexpected command: {cmd}")
            return _FakeProcess(subprocess.CompletedProcess(cmd, 0, stdout, ""))

        with tempfile.TemporaryDirectory() as temporary, \
                patch.object(runner, "load_final_index", return_value={"items": known}), \
                patch.object(runner, "build_signature_index", return_value={}), \
                patch.object(
                    himalaya, "build_himalaya_command",
                    side_effect=lambda command, account=None: ["himalaya", *command],
                ), \
                patch.object(himalaya, "_POPEN", side_effect=fake_run) as run_mock, \
                patch.object(runner.time, "sleep"), \
                himalaya.transport_stats_session() as stats:
            collected, known_count = runner.get_unprocessed_emails(
                "INBOX", 10, data_dir=Path(temporary),
            )

        self.assertEqual([str(number) for number in range(183, 193)], [item["envelope_id"] for item in collected])
        self.assertEqual(182, known_count)
        # Exactly one thread process, four list tiers (25/50/150/300 until the
        # first 182 known envelopes are covered) and ten real reads were
        # started.  This is the actual process-start acceptance point (AK1),
        # not a tautology over a mocked read seam.
        self.assertEqual(15, run_mock.call_count)
        self.assertEqual(15, stats.process_starts)
        self.assertEqual(
            {"envelope thread": 1, "envelope list": 4, "message read": 10},
            {command: counts["starts"] for command, counts in stats.snapshot()["commands"].items()},
        )

    def test_ak2_differential_result_is_identical_with_and_without_map(self) -> None:
        envelopes = _envelopes(300)
        known = _known_items(182)
        thread_output = _thread_payload([
            _node(str(number), f"<{number}@example.test>", f"message {number}")
            for number in range(1, 301)
        ])

        with_map, known_with, skipped_with, _ = self._scan(
            envelopes=envelopes, thread_output=thread_output, known=known, target=10,
        )
        without_map, known_without, skipped_without, _ = self._scan(
            envelopes=envelopes, thread_output="[]", known=known, target=10,
        )

        # Norm audit: compare the complete collected records (all fields and
        # nested values), not only envelope ids or counts.
        self.assertEqual(
            (with_map, known_with, skipped_with),
            (without_map, known_without, skipped_without),
        )
        self.assertEqual(10, len(with_map))
        self.assertEqual(182, known_with)
        self.assertEqual(182, skipped_with)
        for record in with_map:
            self.assertFalse(record["is_known"])
            self.assertIsNone(record["known_location"])

    def test_f3_subject_mismatch_forces_a_read(self) -> None:
        envelopes = [{"id": "1", "subject": "Actual subject"}]
        thread_output = _thread_payload([_node("1", "<known@example.test>", "Different subject")])

        collected, known_count, skipped_known, reads = self._scan(
            envelopes=envelopes,
            thread_output=thread_output,
            known={"known@example.test": {}},
            target=1,
            reads=Mock(return_value={"envelope_id": "1", "message_id": "known@example.test"}),
        )

        reads.assert_called_once()
        self.assertEqual([], collected)
        self.assertEqual(1, known_count)
        self.assertEqual(1, skipped_known)

    def test_f3_leading_zero_ids_never_bind_and_force_a_read(self) -> None:
        # A thread id "0007" must not bind to the list id "7"; fail closed.
        envelopes = [{"id": "7", "subject": "Same subject"}]
        thread_output = _thread_payload([
            _node("0007", "<known@example.test>", "Same subject"),
        ])

        collected, known_count, skipped_known, reads = self._scan(
            envelopes=envelopes,
            thread_output=thread_output,
            known={"known@example.test": {}},
            target=1,
            reads=Mock(return_value={"envelope_id": "7", "message_id": "known@example.test"}),
        )

        reads.assert_called_once()
        self.assertEqual([], collected)
        self.assertEqual(1, known_count)
        self.assertEqual(1, skipped_known)

    def test_f3_surrounding_whitespace_never_binds_and_forces_a_read(self) -> None:
        # F2 accepts only a bare decimal id; surrounding whitespace is invalid
        # on either F3 side and must fall back to a single read.
        cases = (
            # thread id invalid, list id valid
            ({"id": "7", "subject": "Same subject"}, " 7", "7"),
            # thread id valid, list id invalid
            ({"id": " 7", "subject": "Same subject"}, "7", " 7"),
        )
        for envelope, thread_id, read_id in cases:
            with self.subTest(thread_id=thread_id, list_id=envelope["id"]):
                thread_output = _thread_payload([
                    _node(thread_id, "<known@example.test>", "Same subject"),
                ])
                collected, known_count, skipped_known, reads = self._scan(
                    envelopes=[envelope],
                    thread_output=thread_output,
                    known={"known@example.test": {}},
                    target=1,
                    reads=Mock(return_value={
                        "envelope_id": read_id,
                        "message_id": "known@example.test",
                    }),
                )
                reads.assert_called_once()
                self.assertEqual([], collected)
                self.assertEqual(1, known_count)
                self.assertEqual(1, skipped_known)

    def test_f4_map_hit_never_produces_collected_data(self) -> None:
        envelopes = [{"id": "1", "subject": "Known subject"}]
        thread_output = _thread_payload([_node("1", "<known@example.test>", "Known subject")])

        collected, known_count, skipped_known, reads = self._scan(
            envelopes=envelopes,
            thread_output=thread_output,
            known={"known@example.test": {"final_folder": "Archive"}},
            target=1,
        )

        reads.assert_not_called()
        self.assertEqual([], collected)
        self.assertEqual(1, known_count)
        self.assertEqual(1, skipped_known)

    def test_f5_missing_id_is_unknown_and_read_individually(self) -> None:
        envelopes = [
            {"id": "1", "subject": "First"},
            {"id": "2", "subject": "Second"},
        ]
        thread_output = _thread_payload([_node("2", "<second@example.test>", "Second")])

        def details(envelope_id, *_args, **_kwargs):
            return {
                "envelope_id": str(envelope_id),
                "message_id": {"1": "first@example.test", "2": "second@example.test"}[str(envelope_id)],
            }

        collected, known_count, skipped_known, reads = self._scan(
            envelopes=envelopes,
            thread_output=thread_output,
            known={},
            target=2,
            reads=Mock(side_effect=details),
        )

        self.assertEqual(["1", "2"], [call.args[0] for call in reads.call_args_list])
        self.assertEqual(["1", "2"], [item["envelope_id"] for item in collected])
        self.assertEqual(0, known_count)
        self.assertEqual(0, skipped_known)

    def test_map_hit_outside_the_index_is_read_not_skipped(self) -> None:
        envelopes = [{"id": "1", "subject": "Subject"}]
        thread_output = _thread_payload([_node("1", "<not-indexed@example.test>", "Subject")])

        collected, known_count, skipped_known, reads = self._scan(
            envelopes=envelopes,
            thread_output=thread_output,
            known={},
            target=1,
            reads=Mock(return_value={"envelope_id": "1", "message_id": "not-indexed@example.test"}),
        )

        reads.assert_called_once()
        self.assertEqual(1, len(collected))
        self.assertEqual(0, known_count)
        self.assertEqual(0, skipped_known)

    def test_f1_invalid_map_behaves_exactly_like_no_map(self) -> None:
        envelopes = _envelopes(5)
        known = _known_items(2)
        invalid_outputs = (
            "not json",
            json.dumps({"not": "a list"}),
            json.dumps([[], []]),
        )

        baseline, baseline_known, baseline_skipped, _ = self._scan(
            envelopes=envelopes, thread_output="[]", known=known, target=2,
        )
        for invalid in invalid_outputs:
            with self.subTest(invalid=invalid):
                collected, known_count, skipped_known, _ = self._scan(
                    envelopes=envelopes, thread_output=invalid, known=known, target=2,
                )
                self.assertEqual(
                    ([item["envelope_id"] for item in baseline], baseline_known, baseline_skipped),
                    ([item["envelope_id"] for item in collected], known_count, skipped_known),
                )

    def test_skip_known_disabled_never_starts_the_thread_map(self) -> None:
        envelopes = [{"id": "1", "subject": "Subject"}]
        seen: list[list[str]] = []

        def run_himalaya(args, **_kwargs):
            seen.append(list(args))
            return json.dumps(envelopes)

        with tempfile.TemporaryDirectory() as temporary, \
                patch.object(runner, "load_final_index", return_value={"items": {}}), \
                patch.object(runner, "build_signature_index", return_value={}), \
                patch.object(runner, "run_himalaya", side_effect=run_himalaya), \
                patch.object(
                    runner, "get_single_email_details",
                    return_value={"envelope_id": "1", "message_id": "one@example.test"},
                ), \
                patch.object(runner.time, "sleep"):
            collected, _known = runner.get_unprocessed_emails(
                "INBOX", 1, data_dir=Path(temporary), skip_known=False,
            )

        self.assertEqual(["1"], [item["envelope_id"] for item in collected])
        self.assertFalse(any(args[:2] == ["envelope", "thread"] for args in seen))

    def test_thread_map_skip_progress_stays_one_aggregated_line(self) -> None:
        class _Tracker:
            def __init__(self) -> None:
                self.steps: list[str] = []

            def step(self, step_name: str, **_kwargs: object) -> None:
                self.steps.append(step_name)

            def advance_item(self, **_kwargs: object) -> None:
                self.steps.append("advance_item")

        tracker = _Tracker()
        envelopes = _envelopes(6)
        known = _known_items(5)
        thread_output = _thread_payload([
            _node(str(number), f"<{number}@example.test>", f"message {number}")
            for number in range(1, 7)
        ])

        def run_himalaya(args, **_kwargs):
            if args[:2] == ["envelope", "thread"]:
                return thread_output
            return json.dumps(envelopes)

        with tempfile.TemporaryDirectory() as temporary, \
                patch.object(runner, "load_final_index", return_value={"items": known}), \
                patch.object(runner, "build_signature_index", return_value={}), \
                patch.object(runner, "run_himalaya", side_effect=run_himalaya), \
                patch.object(
                    runner, "get_single_email_details",
                    side_effect=lambda eid, *_a, **_k: {
                        "envelope_id": str(eid),
                        "message_id": f"{eid}@example.test",
                    },
                ), \
                patch.object(runner.time, "sleep"):
            emails, known_count = runner.get_unprocessed_emails(
                "INBOX", 1, data_dir=Path(temporary), tracker=tracker,
            )

        self.assertEqual(5, known_count)
        self.assertEqual(["6"], [email["envelope_id"] for email in emails])
        self.assertEqual(
            ["skipped 5 known envelopes"],
            [step for step in tracker.steps if "known envelopes" in step],
        )
        self.assertEqual(1, tracker.steps.count("listing_envelope_thread_map"))
        self.assertNotIn("skipped_known_envelope", tracker.steps)

    def test_thread_map_hit_helper_requires_identical_subject(self) -> None:
        mapping = parse_envelope_thread_map(json.loads(_thread_payload([
            _node("1", "<a@example.test>", "Subject"),
        ])))
        self.assertIsNotNone(thread_map_hit(mapping, {"id": "1", "subject": "Subject"}))
        self.assertIsNone(thread_map_hit(mapping, {"id": "1", "subject": "subject"}))
        self.assertIsNone(thread_map_hit(mapping, {"id": "2", "subject": "Subject"}))
        self.assertIsNone(thread_map_hit(mapping, {"subject": "Subject"}))


class SentSyncTransportBlockTests(unittest.TestCase):
    """F6: Sent-Sync re-raises the structured abort instead of a generic failure."""

    def test_date_candidates_never_rewrite_the_transport_block(self) -> None:
        from core import sent_indexer

        blocked = HimalayaInvocationError("mailbox_transport_blocked", "blocked")
        with patch.object(sent_indexer, "run_himalaya", side_effect=blocked):
            with self.assertRaises(HimalayaInvocationError) as raised:
                sent_indexer._collect_date_candidates(
                    "2026-11-10",
                    folder="Sent Items",
                    account="demo",
                    known_mids=set(),
                    known_eids=set(),
                    max_pages=1,
                )
        self.assertEqual("mailbox_transport_blocked", raised.exception.reason_code)

    def test_sync_sent_items_never_degrades_the_block_to_a_partial_failure(self) -> None:
        from core import sent_indexer

        blocked = HimalayaInvocationError("mailbox_transport_blocked", "blocked")
        with tempfile.TemporaryDirectory() as temporary:
            with patch.object(
                sent_indexer, "_collect_date_candidates", side_effect=blocked,
            ):
                with self.assertRaises(HimalayaInvocationError) as raised:
                    sent_indexer.sync_sent_items(
                        count=1,
                        dates=["2026-11-10"],
                        account="demo",
                        data_dir=Path(temporary),
                    )
        self.assertEqual("mailbox_transport_blocked", raised.exception.reason_code)

    def test_inspect_proposal_sent_sync_block_writes_no_files(self) -> None:
        blocked = HimalayaInvocationError("mailbox_transport_blocked", "blocked")
        email = {
            "envelope_id": "1",
            "folder": "INBOX",
            "message_id": "one@example.test",
            "subject": "hello",
            "from": "sender@example.test",
            "date": "2026-11-10T09:00:00+00:00",
            "preview": "body",
        }
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary) / "data" / "mail-desk"
            data_dir.mkdir(parents=True)
            output_file = data_dir / "inspect-output.json"
            manifest_file = data_dir / "inspect-manifest.json"
            with patch.object(
                runner, "_bind_mailbox_mode",
                return_value=(data_dir, {"account": "demo"}, None),
            ), patch.object(
                runner, "get_unprocessed_emails", return_value=([email], 0),
            ), patch.object(
                classifier, "sync_sent_items", side_effect=blocked,
            ):
                with self.assertRaises(HimalayaInvocationError) as raised:
                    runner.run_inspect_mode(
                        {
                            "count": 1,
                            "skip_known": True,
                            "output_file": str(output_file),
                            "propose_manifest": True,
                            "manifest_file": str(manifest_file),
                        },
                        data_dir=data_dir,
                    )
            self.assertEqual("mailbox_transport_blocked", raised.exception.reason_code)
            self.assertFalse(output_file.exists())
            self.assertFalse(manifest_file.exists())


class TransportGuardTests(unittest.TestCase):
    """F6 and the transport counters at the real ``run_himalaya`` boundary."""

    def setUp(self) -> None:
        himalaya.reset_transport_stats()

    def _run(self, responses, *, max_retries=1):
        remaining = list(responses)

        def fake_popen(cmd, **_kwargs):
            response = remaining.pop(0)
            if isinstance(response, BaseException):
                raise response
            return _FakeProcess(response)

        return patch.object(himalaya, "build_himalaya_command", return_value=["himalaya"]), \
            patch.object(himalaya, "_POPEN", side_effect=fake_popen), \
            patch.object(himalaya.time, "sleep")

    def test_transport_error_tokens_cover_refused_unreachable_and_dns(self) -> None:
        for stderr in (
            "connect: Connection refused (os error 111)",
            "connect: Network is unreachable",
            "connect: Network unreachable",
            "No route to host (os error 113)",
            "Temporary failure in name resolution",
            "Name or service not known",
            "nodename nor servname provided, or not known",
            "No address associated with hostname",
            "could not resolve host: imap.example.test",
            "getaddrinfo failed",
        ):
            with self.subTest(stderr=stderr):
                self.assertTrue(himalaya._is_transient_himalaya_error(stderr))
        for stderr in (
            "authentication failed",
            "mailbox does not exist",
            "invalid argument",
            "permission denied",
        ):
            with self.subTest(stderr=stderr):
                self.assertFalse(himalaya._is_transient_himalaya_error(stderr))

    def test_connection_refused_counts_toward_f6(self) -> None:
        refused = subprocess.CompletedProcess([], 1, "", "Connection refused (os error 111)")
        build, run, sleep = self._run([refused, refused, refused])

        with build, run as run_mock, sleep, himalaya.transport_stats_session() as stats:
            with self.assertRaises(HimalayaInvocationError) as first:
                himalaya.run_himalaya(["folder", "list"], max_retries=1)
            with self.assertRaises(HimalayaInvocationError) as second:
                himalaya.run_himalaya(["folder", "list"], max_retries=1)
            with self.assertRaises(HimalayaInvocationError):
                himalaya.run_himalaya(["folder", "list"], max_retries=1)

        self.assertEqual("himalaya_transient", first.exception.reason_code)
        self.assertEqual("mailbox_transport_blocked", second.exception.reason_code)
        self.assertEqual(2, run_mock.call_count)
        self.assertEqual("two_consecutive_transport_errors", stats.snapshot()["block_reason"])

    def test_no_route_to_host_counts_toward_f6(self) -> None:
        no_route = subprocess.CompletedProcess([], 1, "", "No route to host (os error 113)")
        build, run, sleep = self._run([no_route, no_route, no_route])

        with build, run as run_mock, sleep, himalaya.transport_stats_session() as stats:
            with self.assertRaises(HimalayaInvocationError) as first:
                himalaya.run_himalaya(["folder", "list"], max_retries=1)
            with self.assertRaises(HimalayaInvocationError) as second:
                himalaya.run_himalaya(["folder", "list"], max_retries=1)
            with self.assertRaises(HimalayaInvocationError):
                himalaya.run_himalaya(["folder", "list"], max_retries=1)

        self.assertEqual("himalaya_transient", first.exception.reason_code)
        self.assertEqual("mailbox_transport_blocked", second.exception.reason_code)
        self.assertEqual(2, run_mock.call_count)
        self.assertEqual("two_consecutive_transport_errors", stats.snapshot()["block_reason"])

    def test_two_consecutive_transport_failures_abort_on_the_second_error(self) -> None:
        transient = subprocess.CompletedProcess([], 1, "", "TLS stream reset")
        build, run, sleep = self._run([transient, transient, transient])

        with build, run as run_mock, sleep, himalaya.transport_stats_session() as stats:
            with self.assertRaises(HimalayaInvocationError) as first:
                himalaya.run_himalaya(["folder", "list"], max_retries=1)
            with self.assertRaises(HimalayaInvocationError) as second:
                himalaya.run_himalaya(["folder", "list"], max_retries=1)
            with self.assertRaises(HimalayaInvocationError) as third:
                himalaya.run_himalaya(["folder", "list"], max_retries=1)

        self.assertEqual("himalaya_transient", first.exception.reason_code)
        # The second failure itself is the structured abort; it is never
        # swallowed and never degraded to another transient error.
        self.assertEqual("mailbox_transport_blocked", second.exception.reason_code)
        self.assertEqual("mailbox_transport_blocked", third.exception.reason_code)
        self.assertEqual(2, run_mock.call_count)
        snapshot = stats.snapshot()
        self.assertTrue(snapshot["blocked"])
        self.assertEqual("two_consecutive_transport_errors", snapshot["block_reason"])
        self.assertEqual(2, snapshot["errors"])

    def test_thread_worker_inherits_the_transport_guard(self) -> None:
        transient = subprocess.CompletedProcess([], 1, "", "TLS stream reset")
        build, run, sleep = self._run([transient, transient])

        with build, run as run_mock, sleep, himalaya.transport_stats_session() as stats:
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
                # A bare submit starts with an empty context and would bypass
                # the guard; submit_with_mail_context must keep the session.
                visible = inspect_mode.submit_with_mail_context(
                    executor, himalaya.current_transport_stats
                ).result()
                first = inspect_mode.submit_with_mail_context(
                    executor, himalaya.run_himalaya, ["folder", "list"], max_retries=1
                )
                with self.assertRaises(HimalayaInvocationError) as first_error:
                    first.result()
                second = inspect_mode.submit_with_mail_context(
                    executor, himalaya.run_himalaya, ["folder", "list"], max_retries=1
                )
                with self.assertRaises(HimalayaInvocationError) as second_error:
                    second.result()

        self.assertIs(stats, visible)
        self.assertEqual("himalaya_transient", first_error.exception.reason_code)
        self.assertEqual("mailbox_transport_blocked", second_error.exception.reason_code)
        self.assertEqual(2, run_mock.call_count)
        self.assertEqual(2, stats.snapshot()["process_starts"])

    def test_success_breaks_the_consecutive_error_run(self) -> None:
        transient = subprocess.CompletedProcess([], 1, "", "TLS stream reset")
        success = subprocess.CompletedProcess([], 0, "[]", "")
        build, run, sleep = self._run([transient, success, success])

        with build, run as run_mock, sleep, himalaya.transport_stats_session() as stats:
            with self.assertRaises(HimalayaInvocationError):
                himalaya.run_himalaya(["folder", "list"], max_retries=1)
            self.assertEqual("[]", himalaya.run_himalaya(["folder", "list"], max_retries=1))
            self.assertEqual("[]", himalaya.run_himalaya(["folder", "list"], max_retries=1))

        self.assertEqual(3, run_mock.call_count)
        self.assertFalse(stats.snapshot()["blocked"])

    def test_start_budget_config_is_validated_and_forwarded(self) -> None:
        self.assertEqual(2, runner._transport_start_budget({"transport_start_budget": 2}))
        self.assertIsNone(runner._transport_start_budget({}))
        for invalid in (0, -1, True, False, "10", 1.5):
            with self.subTest(invalid=invalid):
                with self.assertRaises(ValueError):
                    runner._transport_start_budget({"transport_start_budget": invalid})

        parser = runner._build_parser()
        args = parser.parse_args(["--draft", "2", "--transport-start-budget", "7"])
        config = runner._direct_mode_config(args, Path("data/mail-desk"))
        self.assertEqual(7, config["transport_start_budget"])
        with self.assertRaises(runner.ArgumentParseError):
            runner._direct_mode_config(
                parser.parse_args(["--reconcile", "--transport-start-budget", "7"]),
                Path("data/mail-desk"),
            )
        with self.assertRaises(runner.ArgumentParseError):
            runner._direct_mode_config(
                parser.parse_args(["--draft", "2", "--transport-start-budget", "0"]),
                Path("data/mail-desk"),
            )

    def test_start_budget_blocks_the_next_process(self) -> None:
        success = subprocess.CompletedProcess([], 0, "[]", "")
        build, run, sleep = self._run([success, success])

        with build, run as run_mock, sleep, himalaya.transport_stats_session(start_budget=1) as stats:
            self.assertEqual("[]", himalaya.run_himalaya(["folder", "list"], max_retries=1))
            with self.assertRaises(HimalayaInvocationError) as raised:
                himalaya.run_himalaya(["folder", "list"], max_retries=1)

        self.assertEqual("mailbox_transport_blocked", raised.exception.reason_code)
        self.assertEqual(1, run_mock.call_count)
        self.assertEqual("start_budget_exhausted", stats.snapshot()["block_reason"])

    def test_budget_reservation_is_atomic_under_parallel_workers(self) -> None:
        stats = himalaya.TransportStats(start_budget=5)
        workers = 20
        barrier = threading.Barrier(workers)
        created: list[int] = []
        created_lock = threading.Lock()
        blocked: list[str] = []
        blocked_lock = threading.Lock()

        def factory() -> object:
            with created_lock:
                created.append(1)
            return object()

        def worker() -> None:
            barrier.wait(timeout=5)
            try:
                stats.start_process("envelope list", factory)
            except HimalayaInvocationError as exc:
                with blocked_lock:
                    blocked.append(exc.reason_code)

        threads = [threading.Thread(target=worker) for _ in range(workers)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=5)
            self.assertFalse(thread.is_alive(), "worker deadlocked")

        self.assertEqual(5, len(created))
        self.assertEqual(5, stats.process_starts)
        self.assertEqual(["mailbox_transport_blocked"] * (workers - 5), blocked)
        self.assertEqual("start_budget_exhausted", stats.snapshot()["block_reason"])

    def test_block_cannot_overtake_an_inflight_process_start(self) -> None:
        stats = himalaya.TransportStats(start_budget=10)
        factory_entered = threading.Event()
        allow_factory = threading.Event()
        blocker_started = threading.Event()
        blocker_done = threading.Event()
        events: list[str] = []
        events_lock = threading.Lock()
        created: list[int] = []

        def factory() -> object:
            factory_entered.set()
            self.assertTrue(allow_factory.wait(timeout=5), "factory release timed out")
            with events_lock:
                events.append("spawn_created")
            created.append(1)
            return object()

        def spawner() -> None:
            stats.start_process("message read", factory)

        def blocker() -> None:
            blocker_started.set()
            stats.record_error("message read", "himalaya_transient")
            stats.record_error("message read", "himalaya_transient")
            with events_lock:
                events.append("block_published")
            blocker_done.set()

        spawn_thread = threading.Thread(target=spawner)
        blocker_thread = threading.Thread(target=blocker)
        spawn_thread.start()
        self.assertTrue(factory_entered.wait(timeout=5), "factory never entered")
        blocker_thread.start()
        self.assertTrue(blocker_started.wait(timeout=5), "blocker never started")
        # While the spawn lock is held, the blocker must not publish the block.
        self.assertFalse(blocker_done.wait(timeout=0.05))
        allow_factory.set()
        spawn_thread.join(timeout=5)
        blocker_thread.join(timeout=5)
        self.assertFalse(spawn_thread.is_alive(), "spawner deadlocked")
        self.assertFalse(blocker_thread.is_alive(), "blocker deadlocked")

        self.assertEqual(["spawn_created", "block_published"], events)
        self.assertEqual(1, len(created))
        self.assertTrue(stats.snapshot()["blocked"])

        def late_factory() -> object:
            raise AssertionError("no process may start after the block is published")

        with self.assertRaises(HimalayaInvocationError) as raised:
            stats.start_process("message read", late_factory)
        self.assertEqual("mailbox_transport_blocked", raised.exception.reason_code)

    def test_default_budget_covers_the_documented_fallback_window(self) -> None:
        # 2,500-envelope fallback window x 2 processes per envelope (raw-MIME
        # export + read) plus list tiers, thread map and readiness headroom.
        self.assertGreaterEqual(himalaya.DEFAULT_TRANSPORT_START_BUDGET, 2 * 2_500 + 100)
        stats = himalaya.TransportStats()
        for _ in range(5_100):
            stats.start_process("message read", lambda: object())
        self.assertFalse(stats.snapshot()["blocked"])
        self.assertEqual(5_100, stats.process_starts)

    def test_classifier_full_read_never_swallows_f6(self) -> None:
        blocked = HimalayaInvocationError("mailbox_transport_blocked", "blocked")
        preview = {"decision": {"kind": "unknown", "confidence": "low"}, "notes": ""}
        email = {"envelope_id": "1", "folder": "INBOX", "subject": "x", "from": "a@b.test"}
        with patch.object(classifier, "classify_email", return_value=preview), \
                patch.object(classifier, "full_body_triggers", return_value=["low_confidence"]):
            with self.assertRaises(HimalayaInvocationError) as raised:
                classifier.classify_email_two_pass(
                    email,
                    full_reader=Mock(side_effect=blocked),
                    sent_lookup={},
                    final_index={},
                )
        self.assertEqual("mailbox_transport_blocked", raised.exception.reason_code)

    def test_inspect_f6_writes_no_output_or_manifest(self) -> None:
        blocked = HimalayaInvocationError("mailbox_transport_blocked", "blocked")
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary) / "data" / "mail-desk"
            data_dir.mkdir(parents=True)
            output_file = data_dir / "inspect-output.json"
            manifest_file = data_dir / "inspect-manifest.json"
            with patch.object(
                runner, "_bind_mailbox_mode",
                return_value=(data_dir, {"account": "demo"}, None),
            ), patch.object(runner, "get_unprocessed_emails", side_effect=blocked):
                with self.assertRaises(HimalayaInvocationError):
                    runner.run_inspect_mode(
                        {
                            "count": 1,
                            "skip_known": True,
                            "output_file": str(output_file),
                            "propose_manifest": True,
                            "manifest_file": str(manifest_file),
                        },
                        data_dir=data_dir,
                    )
            self.assertFalse(output_file.exists())
            self.assertFalse(manifest_file.exists())

    def test_draft_f6_writes_no_manifest(self) -> None:
        blocked = HimalayaInvocationError("mailbox_transport_blocked", "blocked")
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary) / "data" / "mail-desk"
            data_dir.mkdir(parents=True)
            output_file = data_dir / "batch-manifest.json"
            with patch.object(
                runner, "_bind_mailbox_mode",
                return_value=(data_dir, {"account": "demo"}, None),
            ), patch.object(runner, "get_unprocessed_emails", side_effect=blocked):
                with self.assertRaises(HimalayaInvocationError):
                    runner.run_draft_mode(
                        {"count": 1, "output_file": str(output_file)},
                        data_dir=data_dir,
                    )
            self.assertFalse(output_file.exists())

    def test_draft_full_read_f6_writes_no_manifest(self) -> None:
        blocked = HimalayaInvocationError("mailbox_transport_blocked", "blocked")
        email = {
            "envelope_id": "1",
            "folder": "INBOX",
            "message_id": "one@example.test",
            "subject": "hello",
            "from": "sender@example.test",
            "preview": "body",
        }
        preview_item = {
            "decision": {"kind": "unknown", "confidence": "low", "needs_reply": False},
            "notes": "",
        }
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary) / "data" / "mail-desk"
            data_dir.mkdir(parents=True)
            output_file = data_dir / "batch-manifest.json"
            with patch.object(
                runner, "_bind_mailbox_mode",
                return_value=(data_dir, {"account": "demo"}, None),
            ), patch.object(
                runner, "get_unprocessed_emails", return_value=([email], 0),
            ), patch.object(
                runner, "load_sent_index", return_value={},
            ), patch.object(
                classifier, "classify_email", return_value=preview_item,
            ), patch.object(
                classifier, "full_body_triggers", return_value=["low_confidence"],
            ), patch.object(
                runner, "get_single_email_details", side_effect=blocked,
            ):
                with self.assertRaises(HimalayaInvocationError) as raised:
                    runner.run_draft_mode(
                        {"count": 1, "output_file": str(output_file)},
                        data_dir=data_dir,
                    )
            self.assertEqual("mailbox_transport_blocked", raised.exception.reason_code)
            self.assertFalse(output_file.exists())

    def test_transport_budget_schema_is_scoped_to_non_execute_modes(self) -> None:
        doc = (MAIL_DESK_ROOT / "references" / "batch-runner.md").read_text(encoding="utf-8")
        inspect_start = doc.index('"title": "MailDeskInspectRequest"')
        execute_start = doc.index('"title": "MailDeskExecuteRequest"')
        verify_start = doc.index('"title": "MailDeskBatchVerifyRequest"')
        inspect_block = doc[inspect_start:execute_start]
        execute_block = doc[execute_start:verify_start]
        self.assertIn('"transport_start_budget"', inspect_block)
        self.assertNotIn('"transport_start_budget"', execute_block)

    def test_counters_are_reported_per_command_class_including_retries(self) -> None:
        transient = subprocess.CompletedProcess([], 1, "", "TLS stream reset")
        success = subprocess.CompletedProcess([], 0, "[]", "")
        build, run, sleep = self._run([transient, success, success])

        with build, run, sleep, himalaya.transport_stats_session() as stats:
            self.assertEqual("[]", himalaya.run_himalaya(["envelope", "list"], max_retries=2))
            self.assertEqual("[]", himalaya.run_himalaya(["message", "read", "-H", "Message-Id"], max_retries=1))

        snapshot = stats.snapshot()
        self.assertEqual(3, snapshot["process_starts"])
        self.assertEqual(1, snapshot["retries"])
        self.assertEqual(1, snapshot["errors"])
        self.assertEqual(
            {"starts": 2, "retries": 1, "errors": 1},
            snapshot["commands"]["envelope list"],
        )
        self.assertEqual(
            {"starts": 1, "retries": 0, "errors": 0},
            snapshot["commands"]["message read"],
        )

    def test_mode_result_carries_transport_stats(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary) / "data" / "mail-desk"
            data_dir.mkdir(parents=True)
            with patch.object(runner, "get_unprocessed_emails", return_value=([], 0)), \
                    patch.object(runner, "draft_manifest", return_value={"items": []}):
                result = runner.run_draft_mode({"count": 1}, data_dir=data_dir)

        self.assertIn("transport_stats", result)
        self.assertEqual(0, result["transport_stats"]["process_starts"])
        self.assertEqual(10_000, result["transport_stats"]["start_budget"])

    def test_non_scoped_mode_keeps_its_previous_result_shape(self) -> None:
        stdout = io.StringIO()
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary)
            with patch.object(runner, "resolve_data_dir", return_value=data_dir), \
                    patch.object(
                        runner, "_load_configuration",
                        return_value=({"mode": "execute"}, None),
                    ), \
                    patch.object(
                        runner, "resolve_final_index_path",
                        return_value=data_dir / "index.json",
                    ), \
                    patch.object(
                        runner, "_dispatch",
                        return_value=({"ok": True, "mode": "execute"}, "execute"),
                    ), \
                    patch.object(sys, "argv", ["mail_desk_batch_runner.py", "--input", "x.json"]), \
                    patch("sys.stdout", stdout):
                code = runner.main()

        envelope = json.loads(stdout.getvalue())
        self.assertEqual(0, code)
        self.assertNotIn("transport_stats", envelope["data"])

    def test_swallowed_blocked_flag_still_aborts_the_facade(self) -> None:
        def swallow(*_args, **_kwargs):
            stats = himalaya.current_transport_stats()
            self.assertIsNotNone(stats)
            stats.record_error("envelope list", "himalaya_transient")
            stats.record_error("envelope list", "himalaya_transient")
            return {"ok": True, "mode": "draft"}

        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary) / "data" / "mail-desk"
            data_dir.mkdir(parents=True)
            with patch.object(
                runner, "_bind_mailbox_mode",
                return_value=(data_dir, {"account": "demo"}, None),
            ), patch.object(runner, "_run_draft_mode", side_effect=swallow):
                with self.assertRaises(HimalayaInvocationError) as raised:
                    runner.run_draft_mode({"count": 1}, data_dir=data_dir)

        self.assertEqual("mailbox_transport_blocked", raised.exception.reason_code)

    def test_blocked_abort_has_a_structured_envelope(self) -> None:
        blocked = HimalayaInvocationError("mailbox_transport_blocked", "blocked")

        def blocked_mode(*_args, **_kwargs):
            # Mirror the facade: the session snapshot is published before the
            # structured abort leaves the mode handler.
            with himalaya.transport_stats_session() as stats:
                stats.record_error("envelope list", "himalaya_transient")
                stats.record_error("envelope list", "himalaya_transient")
                raise blocked

        stdout = io.StringIO()
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary)
            with patch.object(runner, "resolve_data_dir", return_value=data_dir), \
                    patch.object(runner, "resolve_final_index_path", return_value=data_dir / "index.json"), \
                    patch.object(runner, "run_draft_mode", side_effect=blocked_mode), \
                    patch.object(
                        sys, "argv", ["mail_desk_batch_runner.py", "--draft", "1"],
                    ), \
                    patch("sys.stdout", stdout):
                code = runner.main()

        envelope = json.loads(stdout.getvalue())
        self.assertEqual(1, code)
        self.assertFalse(envelope["success"])
        self.assertEqual("TransportBlocked", envelope["error"]["type"])
        self.assertEqual("mailbox_transport_blocked", envelope["error"]["details"]["reason_code"])
        self.assertIsNotNone(envelope["error"]["details"]["transport_stats"])


if __name__ == "__main__":
    unittest.main()
