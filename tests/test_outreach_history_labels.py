"""Every outreach event the server records has words in the pane's history.

The Replies and history tab names each event from OUTREACH_EVENT_LABELS (app-outreach-send.js). An event type missing from
it showed as its raw key ("email search", "location recorded"), lower case, beside the labelled ones. This reads every
log_event call in opportunity_app/ and fails on an event type without a label, so a new event cannot ship unnamed.

An event type must be a string literal, a module-level constant, or a local set from those (a conditional, or a dict's
values looked up with .get): anything else fails here as unreadable rather than passing unchecked.
"""

import ast
import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from helpers_source import python_modules, static_script_text

try:
    import realdata_guard
except ImportError:  # imported as tests.test_outreach_history_labels, with tests/ not on sys.path
    from tests import realdata_guard
realdata_guard.install()

# Drawn from OUTREACH_STATUS_LABELS ("Sent → Replied"), not named on its own.
UNLABELLED = {"status"}
LABELS_BLOCK = re.compile(r"const OUTREACH_EVENT_LABELS = \{(.*?)\n  \};", re.S)
LABEL_KEY = re.compile(r"^\s*([a-z_]+):", re.M)


def module_constants(modules):
    """{name: {values}} for every module-level string constant, across all modules (a name reused keeps every value)."""
    constants = {}
    for tree in modules.values():
        for node in tree.body:
            if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        constants.setdefault(target.id, set()).add(node.value.value)
    return constants


def local_value(function, name):
    """The value last assigned to `name` in the function, or None."""
    found = None
    for node in ast.walk(function):
        if isinstance(node, ast.Assign) and any(isinstance(target, ast.Name) and target.id == name for target in node.targets):
            found = node.value
    return found


def event_types(expr, function, constants):
    """The event types an argument can be, or None when it cannot be read."""
    if isinstance(expr, ast.Constant) and isinstance(expr.value, str):
        return {expr.value}
    if isinstance(expr, ast.IfExp):
        body, orelse = event_types(expr.body, function, constants), event_types(expr.orelse, function, constants)
        return None if body is None or orelse is None else body | orelse
    name = expr.id if isinstance(expr, ast.Name) else expr.attr if isinstance(expr, ast.Attribute) else None
    if name in constants:
        return set(constants[name])
    if isinstance(expr, ast.Name) and function is not None:
        value = local_value(function, expr.id)
        if isinstance(value, ast.Call) and isinstance(value.func, ast.Attribute) and value.func.attr == "get" and isinstance(value.func.value, ast.Dict):
            choices = list(value.func.value.values) + list(value.args[1:])
        elif isinstance(value, ast.Dict):
            choices = list(value.values)
        else:
            return None
        found = set()
        for choice in choices:
            types = event_types(choice, None, constants)
            if types is None:
                return None
            found |= types
        return found
    return None


def recorded_event_types():
    """({event type: [where]}, [unreadable call sites]) for every log_event call in opportunity_app/."""
    modules = {path: ast.parse(text) for path, text in python_modules("*.py").items()}
    constants = module_constants(modules)
    found, unreadable = {}, []
    for path, tree in modules.items():
        functions = [node for node in ast.walk(tree) if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))]
        for function in functions:
            for node in ast.walk(function):
                if not isinstance(node, ast.Call) or len(node.args) < 4:
                    continue
                callee = node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", "")
                if callee != "log_event":
                    continue
                types = event_types(node.args[3], function, constants)
                if types is None:
                    unreadable.append(f"{path}:{node.lineno} {ast.unparse(node.args[3])}")
                    continue
                for kind in types:
                    found.setdefault(kind, []).append(f"{path}:{node.lineno}")
    return found, unreadable


def history_labels():
    match = LABELS_BLOCK.search(static_script_text())
    if match is None:
        raise AssertionError("OUTREACH_EVENT_LABELS was not found in the static scripts")
    return set(LABEL_KEY.findall(match.group(1)))


class HistoryLabelTests(unittest.TestCase):
    def test_every_recorded_event_type_has_a_history_label(self):
        found, unreadable = recorded_event_types()
        self.assertEqual(unreadable, [], "name the event type with a literal or a module-level constant so it can be checked")
        self.assertGreater(len(found), 40, "the scan found too few log_event calls to mean anything")
        missing = {kind: places for kind, places in found.items() if kind not in history_labels() | UNLABELLED}
        self.assertEqual(missing, {}, "add these to OUTREACH_EVENT_LABELS in app-outreach-send.js")

    def test_the_scan_reads_each_way_an_event_type_is_named(self):
        found, _ = recorded_event_types()
        for kind in ("created", "follow_up_generated", "partly_bounced", "form_not_sent", "thank_you_held", "auto_follow_up_draft_failed"):
            self.assertIn(kind, found)


if __name__ == "__main__":
    unittest.main()
