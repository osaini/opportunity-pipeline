"""Per-app dependency overrides for routes that every app shares.

The route table is built once per process (see web.app), so a route cannot point at one app. FastAPI asks a route's
dependency_overrides_provider for overrides on every request. The routers here are made with one provider for all apps, which
answers with the dependency_overrides of the app serving the current request. SharedRouteApp records itself in a context variable
for each request it serves. That keeps ``app.dependency_overrides`` working per app, as it did when each app built its own routes.
"""

from __future__ import annotations

from contextvars import ContextVar
from typing import Any

from fastapi import APIRouter, FastAPI

_SERVING_APP: ContextVar[Any] = ContextVar("serving_app", default=None)


class _ServingAppOverrides:
    """The overrides of the app serving this request; no app, no overrides."""

    @property
    def dependency_overrides(self) -> dict[Any, Any]:
        app = _SERVING_APP.get()
        return getattr(app, "dependency_overrides", {}) if app is not None else {}


SERVING_APP_OVERRIDES = _ServingAppOverrides()


def shared_router() -> APIRouter:
    """An APIRouter whose routes read overrides from the app serving each request."""
    return APIRouter(dependency_overrides_provider=SERVING_APP_OVERRIDES)


class SharedRouteApp(FastAPI):
    """A FastAPI app that records itself as the app serving each request, for SERVING_APP_OVERRIDES.

    Done in __call__ rather than as middleware so the middleware stack (pinned in tests/fixtures/route_table.json) is unchanged.
    """

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        token = _SERVING_APP.set(self)
        try:
            await super().__call__(scope, receive, send)
        finally:
            _SERVING_APP.reset(token)
