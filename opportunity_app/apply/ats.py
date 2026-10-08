"""The ATSs Apply for me can read, and the one shape an ATS's browser adapter has.

``AtsSpec`` is one row of the registry: what the app knows about an applicant tracking system without opening a
browser. Its key (the value of ``apply_claims.ats`` and ``apply_runs.ats``), its name in a sentence, the adapter's
version, the modes it supports, how a saved role is recognised as one of its postings (``identify``), where the posting
lives (``canonical_url``), the client that reads its listing, how that listing becomes the form's fields
(``parse_schema``), whether a mail sender is its own (``is_confirmation_sender``), and its request policy
(``route_policy``: which hosts a page may reach, what counts as the submit POST and as the confirmation page; the rules in
``checks`` read it as an argument). Greenhouse and Lever are registered (docs/phase5-lever-handoff-spec.md, 5.2). Lever's form can be read and
planned, and its Finish in browser driver (``lever_adapter.LeverAdapter``) is connected (``adapter_built``): a spec whose driver is not
connected can be read and planned and no run of it starts.

``AtsAdapter`` is the set of methods ``ApplyAgent`` calls on a site's form, so the agent is typed to a shape and not to
Greenhouse. The adapters themselves live beside the agent (``agent.py``), which is a higher layer than this file.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Protocol

from . import greenhouse, lever
from .agent_types import BUILT_MODES
from .checks import GREENHOUSE_ROUTE_POLICY, LEVER_ROUTE_POLICY, RoutePolicy
from .policy import SchemaField, parse_schema as greenhouse_parse_schema, posting_difference as greenhouse_posting_difference
from .schema_client import (
    LeverListings, PageClient, SchemaClient, default_page_client_factory, default_schema_client_factory,
)


class Ident(tuple):
    """(board token, job id), and the host the posting lives on when the ATS has more than one ("" for Greenhouse).

    A tuple of exactly two, so everything that unpacks or compares an ATS's identity as (token, job id) keeps working.
    """

    host: str

    def __new__(cls, token: str, job_id: str, host: str = "") -> "Ident":
        found = super().__new__(cls, (token, job_id))
        found.host = host
        return found


@dataclass(frozen=True)
class AtsSpec:
    """What the app knows about one ATS before any browser opens."""

    key: str                                                              # "greenhouse"; the ats column of claims and runs
    display_name: str                                                     # how a sentence names it
    adapter_version: str                                                  # a rehearsal counts toward the gate only for the version in force
    supported_modes: tuple[str, ...]                                      # the agent modes built for it
    identify: Callable[[sqlite3.Connection, str], tuple[str, str] | None]  # a saved role -> (board token, job id) (an ``Ident`` when the ATS has hosts), or None
    canonical_url: Callable[..., str]                                     # (board token, job id[, host]) -> the posting's address
    schema_client: Callable[[], Any]                                      # a new client that reads the posting (Greenhouse's job board client, Lever's page client)
    parse_schema: Callable[[Mapping[str, Any]], list[SchemaField]]        # the listing -> the fields the form has
    is_confirmation_sender: Callable[[str], bool]                         # a sender domain is the ATS's own
    route_policy: RoutePolicy                                             # the hosts, endpoints and submit and confirmation rules of its form
    # The reading of a posting: from the clients the app was given (Greenhouse's job board client, Lever's page client) and the
    # posting's identity, the object that fetches its listing, or None when the app was given none for this ATS.
    listings: Callable[[SchemaClient | None, PageClient | None, tuple[str, str]], Any] = lambda schema, page, ident: schema
    # (company, title, listing, display name) -> why the listing does not look like the saved role, or "".
    posting_difference: Callable[..., str] = greenhouse_posting_difference
    # The modes a claim of this ATS may take (a claim's mode is one_click, handoff or unattended), apart from what is built.
    claim_modes: tuple[str, ...] = ("one_click", "handoff", "unattended")
    # The setting (an automation feature) that must be on before Apply for me reads this ATS's roles, besides apply_agent. "" for none.
    switch: str = ""
    # Whether the app starts a browser run for this ATS's form. False means the form can be read and planned and no window opens, in the app: the driver may
    # exist and be exercised by the tests before the milestone that connects it (Lever's did, until LV4).
    adapter_built: bool = True


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
    route_policy=GREENHOUSE_ROUTE_POLICY,
)

def lever_identify(conn: sqlite3.Connection, opportunity_id: str) -> Ident | None:
    found = lever.identify(conn, opportunity_id)
    return None if found is None else Ident(found.site, found.job_id, found.host)


def lever_canonical_url(site: str, job_id: str, host: str = lever.DEFAULT_HOST) -> str:
    return lever.canonical_url(site, job_id, host or lever.DEFAULT_HOST)


def lever_parse_schema(listing: Mapping[str, Any]) -> list[SchemaField]:
    """The fields of a Lever form (the page's own, then what it lists but the app cannot read), in the order ``parse_lever_form`` gives them.

    A question the page and its description disagree about, and a control the parser has no family for, become fields of a type the plan
    treats as the student's to answer in the window, with the reason in words. They are required when the page says so.
    """
    form = listing["lever_form"]
    fields = list(form.fields)
    for item in form.unreadable:
        fields.append(SchemaField(name=item.name, label=item.label, required=item.required, type=lever.UNREADABLE_TYPE, section="standard", description=item.reason))
    for item in form.unknown:
        reason = "the page has a control for it that the app does not know" + (" and has turned it off" if item.disabled else "")
        fields.append(SchemaField(name=item.name, label=item.label or item.name, required=item.required and not item.disabled, type=lever.UNKNOWN_TYPE, section="standard", description=reason))
    return fields


def lever_posting_difference(company: str, title: str, listing: Mapping[str, Any], *, ats_name: str) -> str:
    """Why the page does not look like the saved role, or "" when its title carries the saved company and the saved title (spec 5.4 item 8)."""
    form = listing["lever_form"]
    if form.posting.matches(company, title):
        return ""
    page = form.posting.company_title
    if not page:
        return f"{ats_name}'s page has no title the app can compare with {title} at {company}"
    return f"{ats_name}'s page is titled \"{page}\", not {title} at {company}"


LEVER = AtsSpec(
    key=lever.ATS_LEVER,
    display_name=lever.DISPLAY_NAME,
    adapter_version=lever.ADAPTER_VERSION,
    supported_modes=("handoff",),
    identify=lever_identify,
    canonical_url=lever_canonical_url,
    schema_client=default_page_client_factory,
    parse_schema=lever_parse_schema,
    is_confirmation_sender=lever.is_lever_sender,
    route_policy=LEVER_ROUTE_POLICY,
    listings=lambda schema, page, ident: LeverListings(page, getattr(ident, "host", "")) if page is not None else None,
    posting_difference=lever_posting_difference,
    claim_modes=("handoff",),
    switch="apply_agent_lever",
    adapter_built=True,
)

# In the order identify tries them. The first to recognise a role is the role's ATS.
REGISTRY: tuple[AtsSpec, ...] = (GREENHOUSE, LEVER)


class UnknownAts(KeyError):
    """An ATS key that no registered spec has."""


def keys() -> tuple[str, ...]:
    return tuple(spec.key for spec in REGISTRY)


def spec_for(key: str) -> AtsSpec:
    for spec in REGISTRY:
        if spec.key == key:
            return spec
    raise UnknownAts(key)


def name_of(key: str) -> str:
    """How a sentence names the ATS with this key: its spec's display name, or, for a row of an ATS this build no longer registers, the key in title case."""
    for spec in REGISTRY:
        if spec.key == key:
            return spec.display_name
    return key.title() if isinstance(key, str) else ""


def supported_names() -> str:
    """The display names of the registered ATSs as words: "Greenhouse", "Greenhouse and Lever", "A, B and C"."""
    names = [spec.display_name for spec in REGISTRY]
    return names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1] if names else ""


# How a mode is named to the student.
MODE_NAMES = {"lookup": "Look up options", "rehearse": "Rehearse in a window", "submit": "One-click submit", "handoff": "Finish in browser"}
CLAIM_MODE_NAMES = {"handoff": "Finish in browser", "one_click": "One-click submit", "unattended": "Unattended applying"}
CODE_MODE = "ats_mode"
CODE_NOT_BUILT = "ats_not_built"


def _words(names: list[str]) -> str:
    return names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1] if names else "nothing"


def mode_refusal(spec: AtsSpec, mode: str) -> tuple[str, str] | None:
    """(code, sentence) when the agent cannot be asked for ``mode`` (lookup, rehearse, submit or handoff) on this ATS, else None.

    Lever supports Finish in browser only (docs/phase5-lever-handoff-spec.md 6.1). An ATS whose driver is not connected (``adapter_built`` False) supports
    nothing that opens a window: the form is read and planned, and the student is told so.
    """
    if mode not in spec.supported_modes:
        return CODE_MODE, f"{spec.display_name} supports {_words([MODE_NAMES[m] for m in spec.supported_modes])} only, for now"
    if not spec.adapter_built:
        return CODE_NOT_BUILT, f"{MODE_NAMES[mode]} for {spec.display_name} postings is not available yet"
    return None


def claim_refusal(ats: str, mode: str) -> str:
    """The sentence when a claim of ``mode`` (one_click, handoff or unattended) is not one this ATS takes, else "". An ATS not registered is not judged."""
    spec = next((item for item in REGISTRY if item.key == ats), None)
    if spec is None or mode in spec.claim_modes:
        return ""
    return f"{spec.display_name} supports {_words([CLAIM_MODE_NAMES[m] for m in spec.claim_modes])} only, for now"


def canonical_url_of(spec: AtsSpec, ident: tuple[str, str]) -> str:
    """The address of the posting an ``identify`` found: the host it lives on goes with it when it has one."""
    host = getattr(ident, "host", "")
    token, job_id = ident
    return spec.canonical_url(token, job_id, host) if host else spec.canonical_url(token, job_id)


def identify(conn: sqlite3.Connection, opportunity_id: str) -> tuple[AtsSpec, tuple[str, str]] | None:
    """The ATS a saved role is posted on and its (board token, job id), or None when no registered ATS recognises it."""
    for spec in REGISTRY:
        found = spec.identify(conn, opportunity_id)
        if found is not None:
            return spec, found
    return None


class AtsAdapter(Protocol):
    """The methods ``ApplyAgent`` calls on a site's form. Reads only: whatever changes the page goes through the agent (``ops``).

    ``frame`` is a Playwright frame, ``page`` a Playwright page. ``loader_paths`` is a static method on the real adapters (it reads the page's HTML and its address).
    The agent also calls ``fill_react_select`` and ``react_values`` when ``is_react_select`` says a control is one; those
    two are an optional capability of an adapter, not part of this shape, and the agent does not yet check for them.
    There is no ``submit_control``: the student presses Submit, and nothing ever called one.

    ``ats`` is the key of the spec the adapter is for (``AtsSpec.key``); the agent reads its request policy from there.
    ``form_page_kind`` is what ``detect_page`` answers for a form the app fills. ``posting_ids``, ``lookup_token`` and
    ``confirmation_ids`` read the posting's address for the agent (the same-posting check, the lookup endpoints' ``{token}``, the
    confirmation rule). ``uploads_on_attach`` means the board uploads a file to a storage address as it is attached (the app cannot
    tell that upload from the application, so a handoff on such a board is refused); ``reads_on_attach`` means the page reads the
    file as it is attached (it leaves at once, and the student's setting governs whether the app attaches one).

    The rest is what ``AdapterBase`` (``agent_types``) answers for an ATS that needs nothing special, and Lever overrides: ``scan`` (the
    read of the form, when ``uses_engine`` is False), ``page_facts`` and ``page_managed`` (what the page carries for itself),
    ``is_typeahead`` (a list chosen from by typing), ``parse_state``, ``guessed_fields``, ``parser_values`` and ``cleared`` (a file reader's guesses, what
    they hold, and whether one is gone), ``owns`` and ``refuses`` (what the app never writes or presses). ``closed_on_404``, ``waits_for_challenge``,
    ``required_from_load``, ``page_sentences`` and ``press_selector`` (the form's Submit control, which the press listener watches) are its attributes.
    """

    ats: str
    form_page_kind: str
    uses_engine: bool
    closed_on_404: bool
    waits_for_challenge: bool
    required_from_load: bool
    page_sentences: dict[str, str]
    press_selector: str

    def form_frame(self, page: Any) -> Any: ...
    def detect_page(self, page: Any) -> str: ...
    def loader_paths(self, html: str, url: str = "") -> tuple[str, str, str]: ...
    def uploads_on_attach(self, frame: Any) -> bool: ...
    def reads_on_attach(self, frame: Any) -> bool: ...
    def posting_ids(self, url: str) -> tuple[str, str]: ...
    def lookup_token(self, url: str) -> str: ...
    def confirmation_ids(self, url: str) -> tuple[str, str]: ...
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
    def scan(self, frame: Any) -> list[dict[str, Any]]: ...
    def page_facts(self, frame: Any) -> dict[str, str]: ...
    def page_managed(self, frame: Any) -> dict[str, str]: ...
    def owns(self, name: str) -> bool: ...
    def plan_key(self, name: str) -> str: ...
    def hidden_mismatch(self, frame: Any) -> list[str]: ...
    def is_typeahead(self, frame: Any, key: str) -> bool: ...
    def parse_state(self, frame: Any) -> str: ...
    def guessed_fields(self, frame: Any) -> list[str]: ...
    def cleared(self, frame: Any, key: str) -> bool: ...
    def parser_values(self, frame: Any) -> dict[str, str]: ...
    def refuses(self, locator: Any) -> bool: ...
