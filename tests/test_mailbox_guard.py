"""The Claude Code hook that reminds an agent which Gmail mailbox is the pipeline's (.claude/hooks/mailbox-guard.mjs)."""

import json
import os
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HOOK = ROOT / ".claude" / "hooks" / "mailbox-guard.mjs"
NODE = shutil.which("node")
GMAIL = "mcp__claude_ai_Gmail__search_threads"
START = 1_800_000_000_000


@unittest.skipUnless(NODE, "node is not on PATH")
class MailboxGuardTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)

    def run_hook(self, payload, *args, now=START, raw=None):
        env = {**os.environ, "TMPDIR": self.tempdir.name, "TEMP": self.tempdir.name, "TMP": self.tempdir.name,
               "PIPELINE_MAILBOX_GUARD_NOW_MS": str(now)}
        stdin = raw if raw is not None else json.dumps(payload)
        done = subprocess.run([NODE, str(HOOK), *args], input=stdin, capture_output=True, text=True, env=env, timeout=30)
        self.assertEqual(done.returncode, 0, done.stderr)
        return done.stdout

    def call(self, session="session-1", tool=GMAIL, now=START, **extra):
        return self.run_hook({"session_id": session, "tool_name": tool, **extra}, now=now)

    def assertDenied(self, output):
        decision = json.loads(output)["hookSpecificOutput"]
        self.assertEqual((decision["hookEventName"], decision["permissionDecision"]), ("PreToolUse", "deny"))
        return decision["permissionDecisionReason"]

    def test_the_first_gmail_call_is_denied_with_where_the_pipeline_mailbox_is(self):
        reason = self.assertDenied(self.call())
        for words in ("scripts/pipeline_mailbox.py whoami", "pipeline's mailbox", "say so and ask", "naming the mailbox", "once per session and agent", "first emails included", "all three counts 0"):
            self.assertIn(words, reason)

    def test_a_call_in_the_same_batch_is_denied_too(self):
        first = self.assertDenied(self.call())
        again = self.assertDenied(self.call(now=START + 59_000))
        # It is told it is held and what to do next, not that the reminder "appears once".
        self.assertNotEqual(again, first)
        self.assertIn("held for 1 more second after", again)
        self.assertNotIn("1 more seconds", again)
        self.assertIn("call it again after that", again)
        self.assertIn("scripts/pipeline_mailbox.py", again)
        self.assertIn("more seconds", self.assertDenied(self.call(now=START + 10_000)))

    def test_after_a_minute_the_tool_goes_through_with_no_output(self):
        self.assertDenied(self.call())
        self.assertEqual(self.call(now=START + 61_000), "")
        self.assertEqual(self.call(now=START + 3_600_000), "")

    def test_a_subagent_gets_its_own_reminder(self):
        self.assertDenied(self.call())
        self.assertEqual(self.call(now=START + 61_000), "")
        self.assertDenied(self.call(now=START + 62_000, agent_id="agent-7"))
        self.assertEqual(self.call(now=START + 130_000, agent_id="agent-7"), "")

    def test_another_session_is_reminded_separately(self):
        self.assertDenied(self.call())
        self.assertDenied(self.call(session="session-2"))

    def test_a_tool_that_is_not_gmail_is_ignored(self):
        self.assertEqual(self.call(tool="Bash"), "")
        self.assertEqual(self.call(tool="mcp__claude_ai_Google_Drive__search_files"), "")
        # It did not use up the reminder.
        self.assertDenied(self.call())

    def test_unreadable_input_lets_the_call_through(self):
        self.assertEqual(self.run_hook(None, raw="not json"), "")
        self.assertEqual(self.run_hook(None, raw=""), "")
        self.assertEqual(self.run_hook(None, raw="[1, 2]"), "")

    def test_forgetting_the_session_reminds_again(self):
        self.assertDenied(self.call())
        self.assertEqual(self.call(now=START + 61_000), "")
        self.assertEqual(self.run_hook({"session_id": "session-1"}, "--forget-session", now=START + 90_000), "")
        self.assertDenied(self.call(now=START + 91_000))

    def test_forgetting_one_session_leaves_the_others(self):
        self.assertDenied(self.call(session="other"))
        self.run_hook({"session_id": "session-1"}, "--forget-session")
        self.assertEqual(self.call(session="other", now=START + 61_000), "")

    def test_old_session_records_are_pruned(self):
        guard = Path(self.tempdir.name) / "opportunity-pipeline-mailbox-guard"
        old = guard / "0123456789abcdef"
        old.mkdir(parents=True)
        (old / "main").write_text("1")
        past = time.time() - 8 * 24 * 3600
        os.utime(old, (past, past))
        self.assertDenied(self.call())
        self.assertFalse(old.exists())


class SettingsTests(unittest.TestCase):
    def test_settings_wire_the_hook_to_gmail_tools_and_to_session_restarts(self):
        settings = json.loads((ROOT / ".claude" / "settings.json").read_text(encoding="utf-8"))
        [pre] = settings["hooks"]["PreToolUse"]
        self.assertEqual(pre["matcher"], "[Gg]mail")
        [command] = pre["hooks"]
        self.assertEqual((command["type"], command["command"], command["timeout"]), ("command", "node", 10))
        self.assertEqual(command["args"], ["${CLAUDE_PROJECT_DIR}/.claude/hooks/mailbox-guard.mjs"])
        [start] = settings["hooks"]["SessionStart"]
        self.assertEqual(start["matcher"], "compact|resume")
        [forget] = start["hooks"]
        self.assertEqual(forget["command"], "node")
        self.assertEqual(forget["args"], ["${CLAUDE_PROJECT_DIR}/.claude/hooks/mailbox-guard.mjs", "--forget-session"])
        self.assertTrue(HOOK.is_file())


if __name__ == "__main__":
    unittest.main()
