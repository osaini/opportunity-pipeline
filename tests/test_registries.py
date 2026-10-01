"""The startup registries: what bootstrap.register_all fills, and that an empty one fails loudly instead of doing nothing.

Three things used to be wired by whichever module happened to be imported first: the automation ledger's handlers, the
scheduler's thank-you branches, and the callbacks the outreach records make to the thank-you workflow. They are filled in
once, by bootstrap.register_all(), as a process starts (create_app, the worker, the outreach CLI). These tests pin what is
filled, that filling twice changes nothing, and that a slot nobody filled raises where it is read.
"""

import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

from opportunity_app import bootstrap, outreach_callbacks, outreach_schedule, outreach_thank_you
from opportunity_app.hooks import Hook, NotRegistered

from helpers_platform import build_and_migrate  # noqa: F401  (also registers, as a test without an app needs)


class HookTests(unittest.TestCase):
    def test_a_hook_nobody_filled_raises_and_names_itself(self):
        hook = Hook("example.slot")
        self.assertFalse(hook.registered())
        with self.assertRaisesRegex(NotRegistered, "example.slot was never registered"):
            hook(1, key=2)

    def test_a_filled_hook_calls_through_with_its_arguments_and_the_last_fill_wins(self):
        hook = Hook("example.slot")
        hook.register(lambda *args, **kwargs: ("first", args, kwargs))
        self.assertEqual(hook(1, key=2), ("first", (1,), {"key": 2}))
        hook.register(lambda *args, **kwargs: "second")
        self.assertEqual(hook(), "second")
        self.assertTrue(hook.registered())


class OutreachCallbackTests(unittest.TestCase):
    NAMES = ("on_new_reply", "on_not_interested", "thank_you_problem_now")

    def test_register_all_fills_every_callback_with_the_thank_you_workflow(self):
        bootstrap.register_all()
        self.assertEqual(
            [getattr(outreach_callbacks, name).registered() for name in self.NAMES], [True, True, True],
        )
        with mock.patch.object(outreach_thank_you, "_stop_open") as stop:
            outreach_callbacks.on_new_reply("conn", "t1", "u1")
        stop.assert_called_once_with("conn", "t1", "u1", outreach_thank_you.WROTE_AGAIN)

    def test_an_unfilled_callback_raises_where_a_record_calls_it(self):
        for name in self.NAMES:
            hook = getattr(outreach_callbacks, name)
            with self.subTest(name=name), mock.patch.object(hook, "_target", None), self.assertRaises(NotRegistered):
                hook("conn", "t1", "u1", {})


class ScheduledKindTests(unittest.TestCase):
    def test_register_all_hands_the_scheduler_the_thank_you_hooks(self):
        bootstrap.register_all()
        hooks = outreach_schedule._kind_hooks(outreach_thank_you.THANK_YOU_KIND)
        self.assertIs(hooks.settle, outreach_thank_you.settle_in)
        self.assertIs(hooks.gate, outreach_thank_you.gate)
        self.assertIs(hooks.in_window, outreach_thank_you.in_window)
        self.assertIs(hooks.hand_over_stop, outreach_thank_you.hand_over_stop)
        self.assertIs(hooks.back_in_line, outreach_thank_you.back_in_line)
        self.assertIs(hooks.recover_stuck, outreach_thank_you.recover_stuck)

    def test_ordinary_emails_need_no_hooks_but_a_thank_you_without_them_is_an_error(self):
        with mock.patch.dict(outreach_schedule._KINDS, clear=True):
            for kind in ("initial", "follow_up"):
                self.assertIsNone(outreach_schedule._kind_hooks(kind), kind)
            with self.assertRaisesRegex(outreach_schedule.KindNotRegistered, "thank_you"):
                outreach_schedule._kind_hooks(outreach_thank_you.THANK_YOU_KIND)
            with self.assertRaises(outreach_schedule.KindNotRegistered):
                outreach_schedule.finish_send(mock.MagicMock(), {"target_id": "t", "user_id": "u", "kind": "thank_you"}, "sent")

    def test_the_worker_pass_refuses_to_run_without_the_thank_you_hooks(self):
        with mock.patch.dict(outreach_schedule._KINDS, clear=True), self.assertRaises(outreach_schedule.KindNotRegistered):
            conn = mock.MagicMock()
            conn.execute.return_value.fetchall.return_value = []
            outreach_schedule.run_due_sends(conn, client_factory=lambda: None)


if __name__ == "__main__":
    unittest.main()
