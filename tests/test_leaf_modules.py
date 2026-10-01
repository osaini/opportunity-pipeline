"""The shared leaf modules: what each may import, and what each guarantees.

A leaf is a module other modules import freely, so it must not import them back. The tests here parse each leaf with
ast and fail when it imports anything outside its allowlist, then pin the behaviour the callers rely on.

The sections are named after the workstream that created the leaves. Workstream B (background workers, the AI CLI
runner, outreach leaves) is below.
"""

import ast
import json
import logging
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from contextlib import closing
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

from opportunity_app import ROOT, agent_providers, background, inbox_watcher, outreach_batch, outreach_config, outreach_review, web_fetch
from opportunity_app.outreach import create_target, get_target, latest_event_stamp, log_event, withdraw_auto_approval
from opportunity_app.schema import connect_product

from helpers_platform import build_and_migrate

USER = "local-user"
PACKAGE = "opportunity_app"


def imports_of(path: Path) -> tuple[set[str], set[str]]:
    """(module-level imports, function-level imports) of a source file, as dotted names.

    ``from .x import y`` in opportunity_app/m.py is opportunity_app.x; ``from . import x`` is opportunity_app.x as
    well; ``import a.b`` is a.b.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    top: set[str] = set()
    lazy: set[str] = set()

    def record(node: ast.AST, depth: int) -> None:
        found: set[str] = set()
        if isinstance(node, ast.Import):
            found = {alias.name for alias in node.names}
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                base = [PACKAGE] + ([node.module] if node.module else [])
                found = {".".join(base)} if node.module else {f"{PACKAGE}.{alias.name}" for alias in node.names}
            else:
                found = {node.module or ""}
        (top if depth == 0 else lazy).update(found)

    def walk(node: ast.AST, depth: int) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.Import, ast.ImportFrom)):
                record(child, depth)
            nested = depth + 1 if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)) else depth
            walk(child, nested)

    walk(tree, 0)
    return top, lazy


def stdlib_only(names: set[str], also: set[str] = frozenset()) -> set[str]:
    """What is left of ``names`` once the standard library and ``also`` are taken out."""
    return {name for name in names if name.split(".")[0] not in sys.stdlib_module_names and name not in also and name != "__future__"}


class WorkstreamBLeafImportTests(unittest.TestCase):
    """Workstream B leaves: top-level imports stay inside the allowlist; lazy ones are named too."""

    # leaf -> (allowed top-level imports besides the standard library, allowed function-level imports)
    LEAVES = {
        # Not a pure leaf: two lazy imports, kept out of the top so importing it loads neither module. schema supplies
        # utc_now (drop it from this list once the clock moves to timestamps); automation supplies record_health.
        "background": (set(), {f"{PACKAGE}.schema", f"{PACKAGE}.automation"}),
        "web_fetch": ({"httpx", "httpcore"}, set()),
        "outreach_config": ({f"{PACKAGE}.agent_providers"}, set()),
        "outreach_batch": ({f"{PACKAGE}.agent_providers"}, set()),
        "daily_lock": ({f"{PACKAGE}.ROOT"}, set()),
        # The two API SDKs are imported where a provider is built, so a missing one fails only that provider.
        "agent_providers": (set(), {"openai", "anthropic"}),
        "__init__": (set(), set()),
        # Storage over outreach, not a pure leaf: it may import only outreach and the schema's clock.
        "outreach_versions": ({f"{PACKAGE}.outreach", f"{PACKAGE}.schema"}, set()),
    }

    def test_each_leaf_imports_only_what_it_is_allowed_to(self):
        for name, (allowed, lazy_allowed) in self.LEAVES.items():
            with self.subTest(leaf=name):
                top, lazy = imports_of(ROOT / PACKAGE / f"{name}.py")
                self.assertEqual(stdlib_only(top, allowed), set(), f"{name} imports outside its allowlist at the top")
                self.assertEqual(stdlib_only(lazy, allowed | lazy_allowed), set(), f"{name} imports outside its allowlist in a function")

    def test_the_scheduled_daily_run_does_not_load_the_pipeline_or_the_automation_stack(self):
        code = (
            "import sys\n"
            "import opportunity_app.daily\n"
            "print(sorted(m for m in ('pipeline', 'opportunity_app.refresh', 'opportunity_app.automation', "
            "'opportunity_app.schema') if m in sys.modules))\n"
        )
        done = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, cwd=ROOT, timeout=120)
        self.assertEqual(done.returncode, 0, done.stderr[-500:])
        self.assertEqual(done.stdout.strip(), "[]")


class QuietWorker(background.PollingWorker):
    thread_name = "test-quiet-worker"
    failure_message = "The quiet worker's pass failed"
    logger = logging.getLogger("test_leaf_modules.quiet")

    def __init__(self, interval_seconds, fail=False):
        super().__init__(interval_seconds)
        self.passes = 0
        self.started = []
        self.fail = fail
        self.ran = threading.Event()

    def before_start(self):
        self.started.append(self._stop.is_set())

    def _run_pass(self):
        self.passes += 1
        self.ran.set()
        if self.fail:
            raise RuntimeError("boom")


class WorkstreamBBackgroundTests(unittest.TestCase):
    def test_a_polling_worker_runs_a_pass_then_waits_for_the_interval_or_a_wake(self):
        worker = QuietWorker(60)
        try:
            worker.start()
            self.assertTrue(worker.ran.wait(5), "the first pass runs at once")
            self.assertEqual(worker.started, [False], "before_start runs once, after the stop flag was cleared")
            worker.ran.clear()
            self.assertFalse(worker.ran.wait(0.3), "it then sleeps for the interval")
            worker.wake()
            self.assertTrue(worker.ran.wait(5), "wake() ends the sleep")
        finally:
            worker.stop()
        self.assertEqual(worker.passes, 2)
        self.assertFalse(worker._thread.is_alive())

    def test_start_while_running_is_a_no_op_and_stop_ends_the_thread_quickly(self):
        worker = QuietWorker(60)
        worker.start()
        first = worker._thread
        worker.start()
        self.assertIs(worker._thread, first)
        self.assertEqual(len(worker.started), 1)
        began = time.monotonic()
        worker.stop()
        self.assertLess(time.monotonic() - began, 4, "stop() wakes the sleeping thread rather than waiting out the interval")
        worker.stop()

    def test_a_failed_pass_is_logged_where_the_worker_says_and_the_thread_goes_on(self):
        worker = QuietWorker(0.01, fail=True)
        try:
            with self.assertLogs("test_leaf_modules.quiet", "ERROR") as logged:
                worker.start()
                deadline = time.monotonic() + 5
                while worker.passes < 2 and time.monotonic() < deadline:
                    time.sleep(0.01)
        finally:
            worker.stop()
        self.assertGreaterEqual(worker.passes, 2, "one bad pass does not end the thread")
        self.assertIn("The quiet worker's pass failed", logged.output[0])

    def test_the_inbox_watcher_waits_before_its_first_pass_and_stop_ends_that_wait(self):
        watcher = inbox_watcher.InboxWatcher(
            "unused", client_factory=lambda: None, decisions_for=lambda conn, user_id: None, interval_seconds=3600,
        )
        with mock.patch.object(watcher, "run_once") as run_once:
            watcher.start()
            time.sleep(0.2)
            began = time.monotonic()
            watcher.stop()
            self.assertLess(time.monotonic() - began, 4)
        run_once.assert_not_called()

    def test_a_single_flight_manager_runs_one_job_and_reports_its_status(self):
        class Busy(RuntimeError):
            pass

        class Manager(background.SingleFlightManager):
            busy_error = Busy
            busy_message = "A test job is already running"
            idle_extra = {"mode": None}

        manager = Manager()
        idle = manager.status()
        self.assertEqual((idle["state"], idle["mode"], idle["result"]), ("idle", None, None))
        release = threading.Event()
        running = manager._launch("test-job", lambda: (release.wait(5), {"done": 1})[1], mode="report")
        self.assertEqual((running["state"], running["mode"]), ("running", "report"))
        with self.assertRaises(Busy) as raised:
            manager._launch("test-job-2", lambda: None, mode="apply")
        self.assertEqual(str(raised.exception), "A test job is already running")
        running["state"] = "tampered"
        self.assertEqual(manager.status()["state"], "running", "status() is a copy")
        release.set()
        manager.wait(5)
        done = manager.status()
        self.assertEqual((done["state"], done["result"], done["error"], done["mode"]), ("succeeded", {"done": 1}, None, "report"))
        self.assertTrue(done["finished_at"])

    def test_a_job_that_raises_is_failed_with_a_trimmed_message_and_the_next_job_may_start(self):
        manager = background.SingleFlightManager()

        def fail():
            raise ValueError("x" * 2_000)

        manager._launch("test-failing", fail)
        manager.wait(5)
        failed = manager.status()
        self.assertEqual(failed["state"], "failed")
        self.assertEqual(len(failed["error"]), 1_000)
        self.assertIsNone(failed["result"])
        manager._launch("test-after", lambda: "fine")
        manager.wait(5)
        self.assertEqual(manager.status()["result"], "fine")

    def test_step_error_names_the_failure_and_never_an_address_or_a_query_string(self):
        error = background.step_error(RuntimeError("could not read the draft to greg@bovi.example via https://x.test/a?token=abc&b=1"))
        self.assertEqual(error, "RuntimeError: could not read the draft to [address] via https://x.test/a")
        self.assertEqual(len(background.step_error(ValueError("y" * 500))), len("ValueError: ") + 200)

    def test_discard_open_transaction_rolls_back_only_an_open_one_and_never_raises(self):
        class Conn:
            def __init__(self, in_transaction, fails=False):
                self.in_transaction, self.fails, self.rolled = in_transaction, fails, 0

            def rollback(self):
                self.rolled += 1
                if self.fails:
                    raise RuntimeError("gone")

        open_one, idle, broken = Conn(True), Conn(False), Conn(True, fails=True)
        for conn in (open_one, idle, broken):
            background.discard_open_transaction(conn)
        self.assertEqual((open_one.rolled, idle.rolled, broken.rolled), (1, 0, 1))


class WorkstreamBCliRunnerTests(unittest.TestCase):
    def test_run_headless_sends_the_prompt_on_stdin_with_the_shared_flags(self):
        done = subprocess.CompletedProcess(["claude"], 0, "out", "")
        with mock.patch.object(agent_providers.subprocess, "run", return_value=done) as run:
            result = agent_providers.run_headless(["claude", "-p"], "the prompt", timeout=12.5, cwd="somewhere")
        self.assertIs(result, done)
        run.assert_called_once_with(
            ["claude", "-p"], input="the prompt", capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=12.5, cwd="somewhere", creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )

    def test_run_headless_lets_a_timeout_and_a_missing_binary_propagate(self):
        for failure in (subprocess.TimeoutExpired("claude", 1), FileNotFoundError("claude")):
            with self.subTest(failure=type(failure).__name__), mock.patch.object(agent_providers.subprocess, "run", side_effect=failure):
                with self.assertRaises(type(failure)):
                    agent_providers.run_headless(["claude"], "p", timeout=1, cwd=".")

    def test_failure_detail_prefers_stderr_and_falls_back_to_stdout(self):
        self.assertEqual(agent_providers.failure_detail(subprocess.CompletedProcess([], 1, "out ", " err\n")), "err")
        self.assertEqual(agent_providers.failure_detail(subprocess.CompletedProcess([], 1, " out\n", "")), "out")
        self.assertEqual(agent_providers.failure_detail(subprocess.CompletedProcess([], 1, "", "")), "")

    def test_every_headless_call_uses_the_shared_no_tools_and_read_only_flags(self):
        self.assertEqual(agent_providers.CLAUDE_NO_TOOLS, ["-p", "--output-format", "text", "--tools", "", "--strict-mcp-config"])
        self.assertEqual(agent_providers.CODEX_READ_ONLY, ["exec", "--skip-git-repo-check", "--sandbox", "read-only"])
        seen = []

        def fake(command, prompt, **kwargs):
            seen.append(command)
            return subprocess.CompletedProcess(command, 0, "answer", "")

        with mock.patch.dict("os.environ", {"PIPELINE_OUTREACH_REVIEW_PROVIDER": "claude-code"}), \
                mock.patch.object(agent_providers, "provider_catalog", return_value=[
                    {"id": "claude-code", "display_name": "Claude Code", "model": "m", "configured": True, "setup_hint": ""},
                ]), mock.patch.object(outreach_review, "run_headless", fake):
            name, run = outreach_review.review_runner()
            run("review this")
        self.assertEqual(name, "claude-code")
        self.assertEqual(seen[0][1:], agent_providers.CLAUDE_NO_TOOLS)

    def test_the_cli_provider_command_is_the_shared_flags(self):
        claude = agent_providers.CliAgentProvider("claude-code", "m")
        codex = agent_providers.CliAgentProvider("codex-cli", "m")
        self.assertEqual(claude._command()[1:], agent_providers.CLAUDE_NO_TOOLS)
        self.assertEqual(codex._command()[1:], [*agent_providers.CODEX_READ_ONLY, "-"])

    def test_the_binary_name_and_availability_are_public(self):
        with mock.patch.dict("os.environ", {"PIPELINE_CODEX_BIN": "/opt/codex"}):
            self.assertEqual(agent_providers.cli_binary("codex-cli"), "/opt/codex")
        with mock.patch.object(agent_providers.shutil, "which", return_value=None):
            self.assertFalse(agent_providers.cli_available("claude"))
        with mock.patch.object(agent_providers.shutil, "which", return_value="/bin/claude"):
            self.assertTrue(agent_providers.cli_available("claude"))


class WorkstreamBOutreachLeafTests(unittest.TestCase):
    def test_gmail_web_url_opens_the_outreach_account_or_the_first_signed_in(self):
        self.assertEqual(outreach_config.gmail_web_url("all/abc", "me+x@gmail.com"), "https://mail.google.com/mail/?authuser=me%2Bx%40gmail.com#all/abc")
        self.assertEqual(outreach_config.gmail_web_url("all/abc", ""), "https://mail.google.com/mail/?authuser=0#all/abc")
        with mock.patch.dict("os.environ", {"PIPELINE_OUTREACH_ACCOUNT": " me@gmail.com "}):
            self.assertEqual(outreach_config.gmail_web_url("drafts"), "https://mail.google.com/mail/?authuser=me%40gmail.com#drafts")
        with mock.patch.dict("os.environ", {"PIPELINE_OUTREACH_ACCOUNT": ""}):
            self.assertEqual(outreach_config.gmail_web_url("drafts"), "https://mail.google.com/mail/?authuser=0#drafts")

    def test_the_sending_address_and_the_discovery_provider_read_the_environment_each_time(self):
        with mock.patch.dict("os.environ", {"PIPELINE_OUTREACH_ACCOUNT": "  a@b.test "}):
            self.assertEqual(outreach_config.sender_account(), "a@b.test")
        with mock.patch.dict("os.environ", {"PIPELINE_OUTREACH_DISCOVERY_PROVIDER": ""}):
            self.assertEqual(outreach_config.discovery_provider(), "claude-code")
        with mock.patch.dict("os.environ", {"PIPELINE_OUTREACH_DISCOVERY_PROVIDER": "codex-cli"}):
            self.assertEqual(outreach_config.discovery_provider(), "codex-cli")

    def test_the_writer_settings_names_are_the_ones_the_settings_page_writes(self):
        self.assertEqual(outreach_config.PURPOSE_ENV, {
            "follow_up": "PIPELINE_OUTREACH_FOLLOW_UP_PROVIDER",
            "call_prep": "PIPELINE_OUTREACH_CALL_PREP_PROVIDER",
            "thank_you": "PIPELINE_OUTREACH_THANK_YOU_PROVIDER",
        })

    def test_answers_are_matched_to_targets_by_name_and_the_unanswered_come_back_in_batch_order(self):
        targets = [{"id": "1", "company": "Acme Robotics"}, {"id": "2", "company": "Bovi"}, {"id": "3", "company": "Cyclo"}]
        reply = json.dumps({"companies": [
            {"company": "  bovi  "}, "not an object", {"company": "Nobody"}, {"company": "BOVI"}, {"company": "acme   robotics"},
        ]})
        matched, unanswered = outreach_batch.answers_by_target(reply, targets, "location search")
        self.assertEqual([(answer["company"], target["id"]) for answer, target in matched], [("  bovi  ", "2"), ("acme   robotics", "1")])
        self.assertEqual([target["id"] for target in unanswered], ["3"])

    def test_a_stored_name_with_a_double_space_is_never_matched(self):
        targets = [{"id": "1", "company": "Acme  Robotics"}]
        matched, unanswered = outreach_batch.answers_by_target(json.dumps({"companies": [{"company": "Acme Robotics"}]}), targets, "x")
        self.assertEqual((matched, [t["id"] for t in unanswered]), ([], ["1"]))

    def test_a_reply_without_a_companies_list_names_the_search_in_its_error(self):
        with self.assertRaisesRegex(ValueError, "The email search reply had no companies list"):
            outreach_batch.answers_by_target(json.dumps({"companies": "none"}), [], "email search")

    def test_close_browser_closes_in_order_swallows_errors_and_leaves_nothing_held(self):
        order = []

        class Owner:
            pass

        owner = Owner()
        owner._context = mock.Mock(close=lambda: order.append("context") or (_ for _ in ()).throw(RuntimeError("dead")))
        owner._browser = mock.Mock(close=lambda: order.append("browser"))
        owner._playwright = mock.Mock(stop=lambda: order.append("playwright"))
        web_fetch.close_browser(owner)
        self.assertEqual(order, ["context", "browser", "playwright"])
        self.assertEqual((owner._context, owner._browser, owner._playwright), (None, None, None))
        web_fetch.close_browser(owner)

    def test_ask_reviewer_fails_closed_and_each_caller_names_what_it_catches(self):
        held = {"send": False, "reviewer": "r"}
        good = json.dumps({"send": True, "problems": []})

        def raises(error):
            def run(prompt):
                raise error
            return run

        answer, hold = outreach_review.ask_reviewer(lambda prompt: good, "p", held, catch=(RuntimeError,))
        self.assertEqual((answer["send"], hold), (True, None))
        _, hold = outreach_review.ask_reviewer(raises(RuntimeError("down")), "p", held, catch=(RuntimeError,))
        self.assertEqual(hold, {**held, "problems": ["The reviewer could not run: down"]})
        with self.assertRaises(KeyError):
            outreach_review.ask_reviewer(raises(KeyError("odd")), "p", held, catch=(RuntimeError,))
        _, hold = outreach_review.ask_reviewer(raises(KeyError("odd")), "p", held, catch=(Exception,))
        self.assertTrue(hold["problems"][0].startswith("The reviewer could not run: "))
        for bad in ("not json", json.dumps({"send": "yes", "problems": []}), json.dumps({"send": True, "problems": [1]}),
                    json.dumps({"send": True, "problems": "none"}), json.dumps([1])):
            with self.subTest(reply=bad):
                answer, hold = outreach_review.ask_reviewer(lambda prompt, bad=bad: bad, "p", held, catch=(Exception,))
                self.assertEqual((answer, hold), (None, {**held, "problems": ["The reviewer's answer could not be read"]}))

    def test_review_log_detail_says_who_passed_or_held_it_and_is_trimmed(self):
        self.assertEqual(outreach_review.review_log_detail("codex-cli", {"send": True, "problems": []}), "Passed by codex-cli")
        self.assertEqual(outreach_review.review_log_detail("codex-cli", {"send": False, "problems": ["a", "b"]}), "Held by codex-cli: a; b")
        self.assertEqual(len(outreach_review.review_log_detail("r", {"send": False, "problems": ["z" * 3_000]})), 1_000)


class WorkstreamBOutreachStorageTests(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.TemporaryDirectory()
        self.addCleanup(self.root.cleanup)
        _, self.path = build_and_migrate(Path(self.root.name))
        self.conn = connect_product(self.path)
        self.addCleanup(self.conn.close)
        self.target = create_target(self.conn, {"company": "Bovi", "contact_email": "greg@bovi.example", "website": "https://bovi.example"}, user_id=USER)

    def test_latest_event_stamp_is_the_newest_stored_stamp_of_that_type_or_none(self):
        self.assertIsNone(latest_event_stamp(self.conn, self.target["id"], USER, "bounced"))
        with self.conn:
            for stamp in ("2026-01-01T10:00:00+00:00", "2026-03-01T10:00:00+00:00", "2026-02-01T10:00:00+00:00"):
                self.conn.execute(
                    "INSERT INTO outreach_events(id, target_id, user_id, event_type, detail, created_at) VALUES(?, ?, ?, 'bounced', '', ?)",
                    (f"event-{stamp}", self.target["id"], USER, stamp),
                )
        self.assertEqual(latest_event_stamp(self.conn, self.target["id"], USER, "bounced"), "2026-03-01T10:00:00+00:00")
        self.assertIsNone(latest_event_stamp(self.conn, self.target["id"], USER, "sent"))
        self.assertIsNone(latest_event_stamp(self.conn, self.target["id"], "someone-else", "bounced"))

    def test_withdraw_auto_approval_touches_only_an_approved_draft_and_says_why(self):
        target_id = self.target["id"]
        with self.conn:
            withdraw_auto_approval(self.conn, target_id, USER, "not approved, so nothing happens")
        before = get_target(self.conn, target_id, user_id=USER, include_events=True)["events"]
        self.assertEqual([event for event in before if event["event_type"] == "approval_withdrawn"], [])
        with self.conn:
            self.conn.execute("UPDATE outreach_targets SET draft_status='approved' WHERE id=?", (target_id,))
            withdraw_auto_approval(self.conn, target_id, USER, "the automatic resend could not be queued")
        after = get_target(self.conn, target_id, user_id=USER, include_events=True)
        self.assertEqual(after["draft_status"], "generated")
        withdrawn = [event for event in after["events"] if event["event_type"] == "approval_withdrawn"]
        self.assertEqual([event["detail"] for event in withdrawn], ["the automatic resend could not be queued"])

    def test_log_event_is_the_public_name_for_writing_history(self):
        with self.conn:
            log_event(self.conn, self.target["id"], USER, "note", detail="hello")
        events = get_target(self.conn, self.target["id"], user_id=USER, include_events=True)["events"]
        self.assertIn(("note", "hello"), [(event["event_type"], event["detail"]) for event in events])


if __name__ == "__main__":
    unittest.main()
