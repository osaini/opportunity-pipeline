"""The thank-you workflow's callbacks, as the outreach records and the Gmail send path call them.

outreach.py (a company set aside, a reply pasted), outreach_inbox.py (a reply captured from Gmail) and outreach_gmail.py
(the claim for the one call that sends a thank-you) all have to tell outreach_thank_you what just happened, and
outreach_thank_you imports every one of them. These are the slots they call; outreach_thank_you.register() fills them,
through bootstrap.register_all(), when the process starts (see hooks.py: calling one that was not filled raises).

on_new_reply(conn, target_id, user_id)
    They wrote again: a thank-you that has not gone stops now. Inside the caller's transaction.
on_not_interested(conn, target_id, user_id)
    The student set the company aside: a thank-you that has not gone stops, as for a new reply. Inside the caller's transaction.
thank_you_problem_now(conn, target_id, user_id, thank_you, *, manual=False) -> (state, why) | None
    What stops this thank-you going now, from the app's own records (outreach_thank_you.problem_now).
"""

from __future__ import annotations

from .core.hooks import Hook

on_new_reply = Hook("outreach_callbacks.on_new_reply")
on_not_interested = Hook("outreach_callbacks.on_not_interested")
thank_you_problem_now = Hook("outreach_callbacks.thank_you_problem_now")
