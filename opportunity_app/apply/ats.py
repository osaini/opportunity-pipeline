"""The ATSs Apply for me can read, and the one shape an ATS's browser adapter has.

``AtsSpec`` is one row of the registry: what the app knows about an applicant tracking system without opening a
browser. Its key (the value of ``apply_claims.ats`` and ``apply_runs.ats``), its name in a sentence, the adapter's
version, the modes it supports, how a saved role is recognised as one of its postings (``identify``), where the posting
lives (``canonical_url``), the client that reads its listing, how that listing becomes the form's fields
(``parse_schema``), and whether a mail sender is its own (``is_confirmation_sender``). Greenhouse is the only one
registered. The request policy (which hosts a page may reach, what counts as the submit POST) is not here yet: the
modules that read it still use their Greenhouse constants (docs/phase5-lever-handoff-spec.md, 5.2 item 3).

``AtsAdapter`` is the set of methods ``ApplyAgent`` calls on a site's form, so the agent is typed to a shape and not to
Greenhouse. The adapters themselves live beside the agent (``agent.py``), which is a higher layer than this file.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Protocol

from . import greenhouse
from .agent_types import BUILT_MODES
from .policy import SchemaField, parse_schema as greenhouse_parse_schema
from .schema_client import SchemaClient, default_schema_client_factory


@dataclass(frozen=True)
class AtsSpec:
    """What the app knows about one ATS before any browser opens."""

    key: str                                                              # "greenhouse"; the ats column of claims and runs
    display_name: str                                                     # how a sentence names it
    adapter_version: str                                                  # a rehearsal counts toward the gate only for the version in force
    supported_modes: tuple[str, ...]                                      # the agent modes built for it
    identify: Callable[[sqlite3.Connection, str], tuple[str, str] | None]  # a saved role -> (board token, job id), or None
    canonical_url: Callable[[str, str], str]                              # (board token, job id) -> the posting's address
    schema_client: Callable[[], SchemaClient]                             # a new client that reads the posting's listing
    parse_schema: Callable[[Mapping[str, Any]], list[SchemaField]]        # the listing -> the fields the form has
    is_confirmation_sender: Callable[[str], bool]                         # a sender domain is the ATS's own


GREENHOUSE = AtsSpec(
    key=greenhouse.ATS_GREENHOUSE,
    display_name="Greenhouse",
    adapter_version=greenhouse.ADAPTER_VERSION,
    supported_modes=BUILT_MODES,
    identify=greenhouse.identify,
    canonical_url=greenhouse.canonical_url,
    schema_client=default_schema_client_factory,
    parse_schema=greenhouse_parse_schema,
    is_confirmation_sender=greenhouse.is_greenhouse_sender,
)

# In the order identify tries them. The first to recognise a role is the role's ATS.
REGISTRY: tuple[AtsSpec, ...] = (GREENHOUSE,)


class UnknownAts(KeyError):
    """An ATS key that no registered spec has."""


def keys() -> tuple[str, ...]:
    return tuple(spec.key for spec in REGISTRY)


def spec_for(key: str) -> AtsSpec:
    for spec in REGISTRY:
        if spec.key == key:
            return spec
    raise UnknownAts(key)


def identify(conn: sqlite3.Connection, opportunity_id: str) -> tuple[AtsSpec, tuple[str, str]] | None:
    """The ATS a saved role is posted on and its (board token, job id), or None when no registered ATS recognises it."""
    for spec in REGISTRY:
        found = spec.identify(conn, opportunity_id)
        if found is not None:
            return spec, found
    return None


class AtsAdapter(Protocol):
    """The methods ``ApplyAgent`` calls on a site's form. Reads only: whatever changes the page goes through the agent (``ops``).

    ``frame`` is a Playwright frame, ``page`` a Playwright page. ``loader_paths`` is a static method on the real adapters.
    The agent also calls ``fill_react_select`` and ``react_values`` when ``is_react_select`` says a control is one; those
    two are an optional capability of an adapter, not part of this shape, and the agent does not yet check for them.
    There is no ``submit_control``: the student presses Submit, and nothing ever called one.
    """

    def form_frame(self, page: Any) -> Any: ...
    def detect_page(self, page: Any) -> str: ...
    def loader_paths(self, html: str) -> tuple[str, str, str]: ...
    def uploads_on_attach(self, frame: Any) -> bool: ...
    def security_code_prompt(self, frame: Any) -> bool: ...
    def security_code_inputs(self, frame: Any) -> list[Any] | None: ...
    def captcha_widget(self, frame: Any) -> str: ...
    def control(self, frame: Any, key: str) -> Any: ...
    def control_kind(self, frame: Any, key: str) -> str: ...
    def is_react_select(self, frame: Any, key: str) -> bool: ...
    def field_container(self, frame: Any, key: str) -> Any: ...
    def choices(self, frame: Any, key: str) -> list[dict[str, Any]]: ...
    def fill_location(self, ops: Any, frame: Any, key: str, label: str) -> str: ...
    def read_options(self, ops: Any, frame: Any, key: str, *, typed: str | None = None, limit: int = ...) -> list[str]: ...
