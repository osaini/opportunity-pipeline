"""Startup: fill every registry the app reads at call time, once.

Some modules cannot import the module that finishes their work, because it imports them: the automation ledger asks
a handler to make each change, and the handlers need the outreach records the ledger must not depend on; the outreach
records tell the thank-you workflow when a reply arrives, and that workflow sends through the scheduler, which hands
a thank-you back to it. Each of those is a registry or a callback the lower module reads, and the higher module fills in
here, by name, instead of by an import that happens to run first:

  automation.HANDLERS, BREAKER_GROUPS, CORRECTIONS, REQUIREMENTS   automation_handlers, application_inbox,
                                                                    outreach_thank_you, auto_triage, resume_variants,
                                                                    apply_runs
  outreach_schedule's kinds                                         outreach_thank_you
  outreach_callbacks (a reply arrived, not interested, ...)         outreach_thank_you

Every process that runs any of that calls register_all() once as it starts: create_app for the web app (and so for
the launcher, uvicorn and the sandbox), and the entry points that open the same database from outside it (the worker
and the outreach CLI). Nothing registers at import time. A registry that was never filled fails loudly where it is read
(an unknown action type, a requirement that errors, a callback that says it was not registered) rather than acting as if
there were nothing to do. Calling it again is harmless, so a test that wants the app's registries calls it too.

This module is an entry-point module (L5): it imports the workflow modules whose register() it calls.
"""

from __future__ import annotations

import threading

from . import application_inbox, apply_runs, auto_triage, automation_handlers, outreach_thank_you, resume_variants

# In the order they are filled. Each is a module with a ``register()`` function.
_REGISTRANTS = (automation_handlers, application_inbox, outreach_thank_you, auto_triage, resume_variants, apply_runs)

_LOCK = threading.Lock()
_registered = False


def register_all() -> None:
    """Fill the registries. The first call does the work; later calls return at once."""
    global _registered
    with _LOCK:
        if _registered:
            return
        for module in _REGISTRANTS:
            module.register()
        _registered = True
