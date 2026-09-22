"""Contract test binding the documented execute ``action.type`` enum to production.

The accepted execute vocabulary is the union of the action types the production
code can emit and the stable legacy retention aliases the execute handler's
generic retention branch tolerates. The documented schema enum must match that
set exactly.
"""

from __future__ import annotations

from pathlib import Path
import ast
import json
import re
import sys
import unittest


MAIL_DESK_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = MAIL_DESK_ROOT / "scripts"
BATCH_RUNNER_DOC = MAIL_DESK_ROOT / "references" / "batch-runner.md"
EXECUTE_HANDLER = SCRIPTS / "core" / "modes" / "execute.py"

LEGACY_RETENTION_ACTION_TYPES = frozenset({"move", "copy", "none", "archive"})

ACTION_EMITTERS = (
    SCRIPTS / "core" / "classifier.py",
    SCRIPTS / "core" / "attachment_reclassification.py",
    SCRIPTS / "core" / "quarantine" / "attachment_handoff.py",
)

_JSON_BLOCK = re.compile(r"```json\s*\n(.*?)```", re.DOTALL)


def _execute_schema() -> dict:
    text = BATCH_RUNNER_DOC.read_text(encoding="utf-8")
    for block in _JSON_BLOCK.findall(text):
        try:
            schema = json.loads(block)
        except json.JSONDecodeError:
            continue
        if isinstance(schema, dict) and schema.get("title") == "MailDeskExecuteRequest":
            return schema
    raise AssertionError("execute schema not found in batch-runner.md")


def _documented_action_types() -> frozenset[str]:
    schema = _execute_schema()
    enum = schema["properties"]["items"]["items"]["properties"]["action"]["properties"]["type"]["enum"]
    return frozenset(enum)


def _literal_action_types(value: ast.AST) -> set[str]:
    if isinstance(value, ast.Constant) and isinstance(value.value, str):
        return {value.value}
    if isinstance(value, ast.IfExp):
        return _literal_action_types(value.body) | _literal_action_types(value.orelse)
    return set()


def _emitted_action_types() -> set[str]:
    emitted: set[str] = set()
    for path in ACTION_EMITTERS:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Dict):
                for key, value in zip(node.keys, node.values):
                    if isinstance(key, ast.Constant) and key.value == "type":
                        emitted |= _literal_action_types(value)
    return emitted


def _contract_action_types() -> frozenset[str]:
    return frozenset(_emitted_action_types()) | LEGACY_RETENTION_ACTION_TYPES


def _handler_action_type_literals() -> set[str]:
    literals: set[str] = set()
    tree = ast.parse(EXECUTE_HANDLER.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Compare):
            for operator, comparator in zip(node.ops, node.comparators):
                if isinstance(operator, (ast.Eq, ast.NotEq)) and isinstance(comparator, ast.Constant):
                    if isinstance(comparator.value, str):
                        literals.add(comparator.value)
    return literals


class ExecuteActionTypeContractTests(unittest.TestCase):
    def test_documented_enum_exactly_matches_execute_contract(self):
        self.assertEqual(_contract_action_types(), _documented_action_types())

    def test_production_emits_only_documented_action_types(self):
        emitted = _emitted_action_types()
        self.assertEqual({"copy_as_move", "keep_in_folder"}, emitted)
        self.assertTrue(emitted.issubset(_documented_action_types()))

    def test_execute_handler_recognizes_documented_routing_actions(self):
        handler_literals = _handler_action_type_literals()
        self.assertIn("copy_as_move", handler_literals)
        self.assertIn("keep_in_folder", handler_literals)


if __name__ == "__main__":
    unittest.main()
