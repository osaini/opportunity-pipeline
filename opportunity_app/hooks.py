"""A named callback that a lower module calls and a higher module supplies at startup.

Some workflows are called back from code below them: an outreach record learns of a reply and the thank-you workflow
must hear of it, the Gmail send path asks that workflow whether a thank-you may still go. The lower module cannot import
the workflow (it imports the lower module), so it calls a Hook, and the workflow fills it in once, from its register(),
which bootstrap.register_all() calls as the process starts (see bootstrap.py).

A Hook nobody filled in raises NotRegistered where it is called, naming itself, instead of doing nothing: a reply that
was not passed on is a thank-you that goes to someone who wrote back.

Standard library only.
"""

from __future__ import annotations

from typing import Any, Callable


class NotRegistered(RuntimeError):
    """A hook was called before the module that supplies it registered (bootstrap.register_all was not called)."""


class Hook:
    """One callback slot. register() fills it (again, if it is already filled: the last one wins); calling it calls it."""

    __slots__ = ("name", "_target")

    def __init__(self, name: str) -> None:
        self.name = name
        self._target: Callable[..., Any] | None = None

    def register(self, target: Callable[..., Any]) -> None:
        self._target = target

    def registered(self) -> bool:
        return self._target is not None

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        target = self._target
        if target is None:
            raise NotRegistered(f"{self.name} was never registered: call bootstrap.register_all() as the process starts")
        return target(*args, **kwargs)
