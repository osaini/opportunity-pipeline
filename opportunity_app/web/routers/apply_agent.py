"""Apply for me: the check, the answers it asks for, its settings and the sensitive-answers store."""

from __future__ import annotations

import sqlite3
from typing import Any, Callable, Literal

from fastapi import Depends, HTTPException, Query, Response, status
from fastapi.responses import FileResponse

from ..overrides import shared_router
from ...automation import ledger as automation_core
from ...applications.actions import OpportunityNotFoundError
from ...apply import (
    ats as apply_ats,
    classify as apply_classify,
    policy as apply_policy,
    preflight as apply_preflight,
    runner as apply_runner,
    runs as apply_runs,
    sensitive as apply_sensitive,
    watch as apply_watch,
)
from ...apply.schema_client import PageClient, SchemaClient
from ..context import AppContext
from ..dependencies import get_ctx, require_auth, require_browser_session, writable_connection
from ..models.apply_agent import (
    ApplyAnswerRequest,
    ApplyClaimResolveRequest,
    ApplyHandoffRequest,
    ApplyLabelRequest,
    ApplyLookupRequest,
    ApplyRehearsalRequest,
    ApplyReviewRequest,
    ApplySensitiveAnswerRequest,
    ApplySensitiveCategoriesRequest,
    ApplySensitiveEntryRequest,
)


router = shared_router()


def _live_runs(ctx: AppContext) -> Callable[[str], bool]:
    """Whether this server is working on a run: the runner's own slot, or a run whose supervisor thread is up."""
    runner = ctx.runtime.apply_runner

    def live(run_id: str) -> bool:
        return run_id in apply_runs.RUNNING_RUNS or runner.busy() == run_id

    return live


def apply_schema_client(ctx: AppContext) -> SchemaClient:
    """The client the read-only Apply for me check uses, or 503 in an app that has none (a test, the fuzz sandbox)."""
    if ctx.services.apply_schema_client_factory is None or ctx.services.apply_agent_factory is None:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=apply_runs.NOT_HERE)
    return ctx.services.apply_schema_client_factory()


def apply_page_client(ctx: AppContext) -> PageClient | None:
    """The client that reads a Lever posting's application page, or None in an app that was given none (the check then says Lever did not answer)."""
    factory = ctx.services.apply_page_client_factory
    return factory() if factory is not None else None


def require_apply_agent(conn: sqlite3.Connection, user_id: str) -> None:
    """6.0 step 1: Apply for me must be on. The answer is what it still needs, or how to turn it on."""
    if automation_core.mode(conn, user_id, "apply_agent") != "on":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=apply_runs.setup_requirement(conn, user_id) or "Apply for me is off. Turn it on under Automation",
        )


def sensitive_answers_payload(conn: sqlite3.Connection, user_id: str) -> dict[str, Any]:
    allowed = apply_sensitive.allowed_categories(conn, user_id)
    return {
        "groups": [
            {"key": key, "label": label, "categories": list(categories), "on": all(category in allowed for category in categories)}
            for key, label, categories in apply_sensitive.CATEGORY_GROUPS
        ],
        "categories": [
            {"category": category, "label": apply_sensitive.LABELS[category], "statement": category in apply_classify.STATEMENT_CATEGORIES,
             "decline_only": category in apply_sensitive.EEO_CATEGORIES,
             "tickable": category in apply_classify.TICKABLE}
            for category in apply_sensitive.STORABLE
        ],
        "entries": apply_sensitive.list_entries(conn, user_id),
        "consent_text": apply_sensitive.CONSENT_TEXT,
        "decline_examples": list(apply_sensitive.DECLINE_EXAMPLES),
    }


@router.get("/api/v1/apply-agent/opportunities/{opportunity_id}/check")
def apply_agent_check(
    opportunity_id: str,
    conn: sqlite3.Connection = Depends(writable_connection),
    user_id: str = Depends(require_auth),
    ctx: AppContext = Depends(get_ctx),
) -> dict[str, Any]:
    """What Apply for me would fill for this Greenhouse role, and what it still needs from the student.

    Read-only: it opens no browser and writes nothing (no application, no event, no run). It asks Greenhouse's
    public listing once an hour per role and keeps the answer in memory.
    """
    client = apply_schema_client(ctx)
    require_apply_agent(conn, user_id)
    try:
        return apply_preflight.check(
            conn, user_id, opportunity_id, client=client, cache=ctx.runtime.apply_schema_cache, resume_root=ctx.config.resume_storage,
            page_client=apply_page_client(ctx),
        )
    except OpportunityNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Opportunity not found") from exc


@router.post("/api/v1/apply-agent/opportunities/{opportunity_id}/answers")
def apply_agent_answer(
    opportunity_id: str,
    payload: ApplyAnswerRequest,
    conn: sqlite3.Connection = Depends(writable_connection),
    user_id: str = Depends(require_auth),
    ctx: AppContext = Depends(get_ctx),
) -> dict[str, Any]:
    """Answer one question the check listed as missing. Saved for this company, without field ids, so it carries over."""
    client = apply_schema_client(ctx)
    require_apply_agent(conn, user_id)
    try:
        return apply_preflight.answer_missing(
            conn, user_id, opportunity_id, key=payload.key, answer=payload.answer, reusable=payload.reusable, posting_confirmed=payload.posting_confirmed,
            client=client, cache=ctx.runtime.apply_schema_cache, resume_root=ctx.config.resume_storage, page_client=apply_page_client(ctx),
        )
    except OpportunityNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Opportunity not found") from exc
    except apply_preflight.AnswerRefused as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc


@router.get("/api/v1/apply-agent/settings")
def apply_agent_settings(
    conn: sqlite3.Connection = Depends(writable_connection),
    user_id: str = Depends(require_auth),
) -> dict[str, Any]:
    """The Apply for me settings in force: the switch and what it needs, the limits, and the option labels confirmed."""
    values = apply_runs.limits(conn, user_id)
    lever_on = automation_core.mode(conn, user_id, "apply_agent_lever") == "on"
    sets = [{"ats": apply_ats.GREENHOUSE.key, "name": apply_ats.GREENHOUSE.display_name,
             "fields": list(apply_policy.label_fields_for(apply_ats.GREENHOUSE.key)),
             "labels": apply_runs.list_ats_labels(conn, user_id, apply_ats.GREENHOUSE.key)}]
    if lever_on:
        sets.append({"ats": apply_ats.LEVER.key, "name": apply_ats.LEVER.display_name,
                     "fields": list(apply_policy.label_fields_for(apply_ats.LEVER.key)),
                     "labels": apply_runs.list_ats_labels(conn, user_id, apply_ats.LEVER.key)})
    return {
        "mode": automation_core.mode(conn, user_id, "apply_agent"),
        "requirement": apply_runs.setup_requirement(conn, user_id),
        "limits": [
            {"key": key, "value": values[key], "default": default, "overridden": values[key] != default}
            for key, default in apply_runs.DEFAULT_LIMITS.items()
        ],
        "ats_labels": apply_runs.list_ats_labels(conn, user_id),
        "label_fields": list(apply_policy.ALLOWED_ATS_LABEL_FIELDS),
        # The same for every ATS whose form has lists of its own (Lever's only once its switch is on), each with its own saved options.
        "ats_label_sets": sets,
        # ``window``: whether Finish in browser can open a Lever form at all (its driver is built), which the settings page says in plain words.
        "lever": {"mode": "on" if lever_on else "off", "resume_upload": automation_core.mode(conn, user_id, "apply_lever_resume_upload"),
                  "window": apply_ats.spec_for(apply_ats.LEVER.key).adapter_built},
        "evidence_days": apply_runs.evidence_days(),
        "ats_statistics": [apply_watch.ats_statistics(conn, user_id)],
    }


@router.put("/api/v1/apply-agent/ats-labels/{field}")
def put_apply_ats_label(
    field: str,
    payload: ApplyLabelRequest,
    conn: sqlite3.Connection = Depends(writable_connection),
    user_id: str = Depends(require_auth),
) -> dict[str, str]:
    """Save the exact text of an option the student picked in a typeahead list (school, location, degree)."""
    if payload.ats not in apply_ats.keys():
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail="Unknown ATS")
    try:
        return apply_runs.set_ats_label(conn, user_id, field, payload.label, ats=payload.ats)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc


@router.delete("/api/v1/apply-agent/ats-labels/{field}", status_code=status.HTTP_204_NO_CONTENT)
def delete_apply_ats_label(
    field: str,
    ats: str = Query(default="greenhouse", min_length=1, max_length=40),
    conn: sqlite3.Connection = Depends(writable_connection),
    user_id: str = Depends(require_auth),
) -> Response:
    if ats not in apply_ats.keys():
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail="Unknown ATS")
    if not apply_runs.delete_ats_label(conn, user_id, field, ats=ats):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No confirmed option for that list")
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# The sensitive-answers store. Every route here needs the student's own browser session (require_browser_session): an
# access token, which scripts hold, is refused. Reading is refused too, since an entry is the student's own answer.
@router.get("/api/v1/apply-agent/sensitive-answers")
def list_stored_sensitive_answers(
    user_id: str = Depends(require_browser_session),
    conn: sqlite3.Connection = Depends(writable_connection),
) -> dict[str, Any]:
    """The kinds of sensitive answer the student switched on, and the answers they stored, each with its consent."""
    return sensitive_answers_payload(conn, user_id)


@router.put("/api/v1/apply-agent/sensitive-categories")
def put_apply_sensitive_categories(
    payload: ApplySensitiveCategoriesRequest,
    user_id: str = Depends(require_browser_session),
    conn: sqlite3.Connection = Depends(writable_connection),
) -> dict[str, Any]:
    """Switch kinds of sensitive answer on or off. Export control, citizenship, clearance and salary cannot be switched on."""
    try:
        apply_sensitive.set_allowed_categories(conn, user_id, payload.categories)
    except apply_sensitive.StoreRefused as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc
    return sensitive_answers_payload(conn, user_id)


@router.post("/api/v1/apply-agent/sensitive-answers")
def add_apply_sensitive_answer(
    payload: ApplySensitiveEntryRequest,
    user_id: str = Depends(require_browser_session),
    conn: sqlite3.Connection = Depends(writable_connection),
) -> dict[str, Any]:
    """Store an answer the app may type into a form, with the consent ticked. The same question for the same company is replaced."""
    try:
        return apply_sensitive.add_entry(
            conn, user_id, category=payload.category, question=payload.question, answer=payload.answer, answer_kind=payload.answer_kind,
            company=payload.company, links=payload.links, consent=payload.consent,
        )
    except apply_sensitive.StoreRefused as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc


@router.delete("/api/v1/apply-agent/sensitive-answers/{entry_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_apply_sensitive_answer(
    entry_id: str,
    user_id: str = Depends(require_browser_session),
    conn: sqlite3.Connection = Depends(writable_connection),
) -> Response:
    """Remove a stored answer at once. A plan that used it no longer matches."""
    if not apply_sensitive.delete_entry(conn, user_id, entry_id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No stored answer with that id")
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/api/v1/apply-agent/opportunities/{opportunity_id}/sensitive-answers")
def apply_agent_sensitive_answer(
    opportunity_id: str,
    payload: ApplySensitiveAnswerRequest,
    user_id: str = Depends(require_browser_session),
    conn: sqlite3.Connection = Depends(writable_connection),
    ctx: AppContext = Depends(get_ctx),
) -> dict[str, Any]:
    """Needs you: store the student's answer to one sensitive question the check listed, with the consent ticked."""
    client = apply_schema_client(ctx)
    require_apply_agent(conn, user_id)
    try:
        return apply_preflight.answer_sensitive(
            conn, user_id, opportunity_id, key=payload.key, answer=payload.answer, consent=payload.consent, any_company=payload.any_company,
            posting_confirmed=payload.posting_confirmed, client=client, cache=ctx.runtime.apply_schema_cache, resume_root=ctx.config.resume_storage,
            page_client=apply_page_client(ctx),
        )
    except OpportunityNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Opportunity not found") from exc
    except apply_preflight.AnswerRefused as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc


# --- Rehearsals and option lookups (M5a). Neither ever submits: the agent's request policy aborts anything that could. ---
# --- Finish in browser (M5b): the app fills the form and the student presses Submit in the window themselves. ---


def _view(conn: sqlite3.Connection, ctx: AppContext, row: dict[str, Any]) -> dict[str, Any]:
    """A run as the page shows it; the window can be brought forward only for the run this app is running."""
    return apply_runner.run_view(conn, row, _live_runs(ctx), local=ctx.runtime.apply_runner.busy())


def _own_run(conn: sqlite3.Connection, user_id: str, run_id: str) -> dict[str, Any]:
    row = apply_runs.get_run(conn, run_id, user_id=user_id)
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=apply_runner.NO_RUN)
    return row


def _start_run(
    kind: str, opportunity_id: str, conn: sqlite3.Connection, user_id: str, ctx: AppContext, *, key: str = "", text: str = "",
    acknowledged: tuple[str, ...] = (), posting_confirmed: bool = False,
) -> dict[str, Any]:
    """Start a rehearsal, a lookup or a Finish in browser run and answer with its (running) run view."""
    client = apply_schema_client(ctx)
    if ctx.config.apply_storage is None:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=apply_runs.NOT_HERE)
    require_apply_agent(conn, user_id)
    factory = ctx.services.apply_agent_factory
    missing = str(factory.available() or "") if callable(getattr(factory, "available", None)) else ""
    if missing:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=missing)
    try:
        run_id = ctx.runtime.apply_runner.start(
            conn, database_target=ctx.config.database_target, user_id=user_id, opportunity_id=opportunity_id, kind=kind,
            agent_factory=factory, schema_client=client, apply_root=ctx.config.apply_storage, resume_root=ctx.config.resume_storage,
            lookup_key=key, lookup_text=text, acknowledged=acknowledged, posting_confirmed=posting_confirmed, page_client=apply_page_client(ctx),
        )
    except OpportunityNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Opportunity not found") from exc
    except apply_runner.RunnerBusy as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except apply_runner.RunRefused as exc:
        # A refusal with a code carries it (the page adds the tick the code names, or the posting's confirmation); one without is the sentence.
        detail: Any = {"message": exc.message, "code": exc.code, "ask": exc.ask} if exc.code else exc.message
        raise HTTPException(status_code=exc.status_code, detail=detail) from exc
    return _view(conn, ctx, _own_run(conn, user_id, run_id))


@router.post("/api/v1/apply-agent/opportunities/{opportunity_id}/rehearsals", status_code=status.HTTP_202_ACCEPTED)
def start_apply_rehearsal(
    opportunity_id: str,
    payload: ApplyRehearsalRequest | None = None,
    conn: sqlite3.Connection = Depends(writable_connection),
    user_id: str = Depends(require_auth),
    ctx: AppContext = Depends(get_ctx),
) -> dict[str, Any]:
    """Fill this Greenhouse form in a window the student can watch, to check it. Nothing is sent: the app never presses Submit, and it refuses every request it can see that could send the form.

    A form that does not look like the saved role is refused (409) until the body says ``posting_confirmed``.
    """
    return _start_run("rehearsal", opportunity_id, conn, user_id, ctx, posting_confirmed=bool(payload and payload.posting_confirmed))


@router.post("/api/v1/apply-agent/opportunities/{opportunity_id}/lookups", status_code=status.HTTP_202_ACCEPTED)
def start_apply_lookup(
    opportunity_id: str,
    payload: ApplyLookupRequest,
    conn: sqlite3.Connection = Depends(writable_connection),
    user_id: str = Depends(require_auth),
    ctx: AppContext = Depends(get_ctx),
) -> dict[str, Any]:
    """Type this text into one of the form's lists and read the options Greenhouse offers. The text goes to Greenhouse's lookup service."""
    return _start_run("lookup", opportunity_id, conn, user_id, ctx, key=payload.key, text=payload.text)


@router.post("/api/v1/apply-agent/opportunities/{opportunity_id}/handoffs", status_code=status.HTTP_202_ACCEPTED)
def start_apply_handoff(
    opportunity_id: str,
    payload: ApplyHandoffRequest,
    conn: sqlite3.Connection = Depends(writable_connection),
    user_id: str = Depends(require_browser_session),
    ctx: AppContext = Depends(get_ctx),
) -> dict[str, Any]:
    """Finish in browser: fill this Greenhouse form in a window and leave the Submit button to the student.

    Needs the student's own browser session: starting one is the student's act, and the application goes out under their
    name only when they press Submit application in the window. The ticks the student gave (an earlier application, a
    released attempt, an old row) are named by their codes, and ``posting_confirmed`` says they checked a posting that
    does not look like the saved role.
    """
    return _start_run(
        "handoff", opportunity_id, conn, user_id, ctx, acknowledged=tuple(payload.acknowledged), posting_confirmed=payload.posting_confirmed,
    )


@router.get("/api/v1/apply-agent/opportunities/{opportunity_id}/runs")
def list_apply_runs(
    opportunity_id: str,
    kind: Literal["lookup", "rehearsal", "handoff"] | None = None,
    run_id: str | None = Query(default=None, max_length=80),
    limit: int = Query(default=5, ge=1, le=20),
    conn: sqlite3.Connection = Depends(writable_connection),
    user_id: str = Depends(require_auth),
    ctx: AppContext = Depends(get_ctx),
) -> dict[str, Any]:
    """This role's lookups, rehearsals and Finish in browser runs, newest first (or the one ``run_id``), and whether the app is busy."""
    try:
        apply_preflight.require_opportunity(conn, user_id, opportunity_id)
    except OpportunityNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Opportunity not found") from exc
    local = ctx.runtime.apply_runner.busy()
    live = _live_runs(ctx)
    if run_id:
        row = apply_runs.get_run(conn, run_id, user_id=user_id)
        runs = [apply_runner.run_view(conn, row, live, local=local)] if row is not None and row["opportunity_id"] == opportunity_id else []
    else:
        runs = apply_runner.run_views(conn, user_id, opportunity_id, kind=kind or "", limit=limit, live=live, local=local)
    return {"runs": runs, "busy": local is not None}


@router.get("/api/v1/apply-agent/runs/{run_id}")
def get_apply_run(
    run_id: str,
    conn: sqlite3.Connection = Depends(writable_connection),
    user_id: str = Depends(require_auth),
    ctx: AppContext = Depends(get_ctx),
) -> dict[str, Any]:
    return _view(conn, ctx, _own_run(conn, user_id, run_id))


@router.post("/api/v1/apply-agent/runs/{run_id}/cancel")
def cancel_apply_run(
    run_id: str,
    user_id: str = Depends(require_browser_session),
    conn: sqlite3.Connection = Depends(writable_connection),
    ctx: AppContext = Depends(get_ctx),
) -> dict[str, Any]:
    """Stop: the run is asked to end, and its browser is killed if it does not.

    A Finish in browser run's claim is asked first, in the database (``request_cancel``): from then on the hand-over is
    refused, so a Submit the student presses while the stop travels to the window goes nowhere. Once the student has pressed
    Submit the application is out of the app's hands, and the answer says so.
    """
    row = _own_run(conn, user_id, run_id)
    if row["status"] == "finished":
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=apply_runner.FINISHED_ALREADY)
    token = str(row["claim_token"] or "")
    if token and not apply_runs.request_cancel(conn, token, user_id=user_id):
        claim = apply_runs.get_claim(conn, token, user_id=user_id)
        handed = claim is not None and claim["state"] in ("clicking", "submitted", "unconfirmed")
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=apply_runner.HANDED_OVER if handed else apply_runner.FINISHED_ALREADY,
        )
    if not ctx.runtime.apply_runner.cancel(run_id):
        if not apply_runner.orphaned(row, _live_runs(ctx)):
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=apply_runner.NOT_RUNNING)
        # A server that was stopped mid-run left this row 'running', and nothing here is working on it: end it now, instead of
        # showing a Stop button that cannot work until its heartbeat goes stale.
        apply_runs.finish_run(conn, run_id, outcome="failed", clean=False, reasons=[apply_runner.SERVER_STOPPED])
    return _view(conn, ctx, _own_run(conn, user_id, run_id))


@router.post("/api/v1/apply-agent/runs/{run_id}/front")
def front_apply_run(
    run_id: str,
    user_id: str = Depends(require_browser_session),
    conn: sqlite3.Connection = Depends(writable_connection),
    ctx: AppContext = Depends(get_ctx),
) -> dict[str, Any]:
    """Bring the Finish in browser window to the front (the student could not find it behind other windows)."""
    row = _own_run(conn, user_id, run_id)
    if row["kind"] != "handoff":
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=apply_runner.NOT_HANDOFF)
    if row["status"] != "running" or not ctx.runtime.apply_runner.front(run_id):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=apply_runner.NOT_RUNNING)
    return _view(conn, ctx, _own_run(conn, user_id, run_id))


@router.get("/api/v1/apply-agent/runs/{run_id}/values")
def get_apply_run_values(
    run_id: str,
    response: Response,
    user_id: str = Depends(require_browser_session),
    conn: sqlite3.Connection = Depends(writable_connection),
    ctx: AppContext = Depends(get_ctx),
) -> dict[str, Any]:
    """What the app would use (a rehearsal) or filled (Finish in browser) for each field of this run, for the student's own browser only.

    The values are today's source for each stored plan entry: they are read here, from the student's data, and never stored on the
    run. Each says whether it ``changed`` since the run and, for a Finish in browser run, is shown only where it provably equals
    what was filled. Never cached.
    """
    row = _own_run(conn, user_id, run_id)
    root = ctx.config.apply_storage
    if root is None:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=apply_runs.NOT_HERE)
    response.headers["Cache-Control"] = "no-store"
    return {"values": apply_policy.preview_values(
        conn, user_id, row, key=apply_policy.mac_key(root), storage_root=ctx.config.resume_storage,
    )}


@router.post("/api/v1/apply-agent/runs/{run_id}/review")
def review_apply_run(
    run_id: str,
    payload: ApplyReviewRequest,
    user_id: str = Depends(require_browser_session),
    conn: sqlite3.Connection = Depends(writable_connection),
    ctx: AppContext = Depends(get_ctx),
) -> dict[str, Any]:
    """The student marks a finished rehearsal right or wrong. Marks open the gate and trip the breaker, so they need the browser session."""
    row = _own_run(conn, user_id, run_id)
    if not apply_runner.reviewable(row):
        # The run page offers a mark only on these (can_review); a 'wrong' on a failed run would count toward the breaker.
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=apply_runner.NOT_REVIEWABLE)
    try:
        marked = apply_runs.mark_review(conn, run_id, user_id=user_id, verdict=payload.verdict, note=payload.note)
    except LookupError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=apply_runner.NO_RUN) from exc
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    return {**_view(conn, ctx, _own_run(conn, user_id, run_id)), "breaker_tripped": marked["breaker_tripped"]}


@router.get("/api/v1/apply-agent/runs/{run_id}/screenshots/{index}")
def get_apply_screenshot(
    run_id: str,
    index: int,
    conn: sqlite3.Connection = Depends(writable_connection),
    user_id: str = Depends(require_browser_session),
    ctx: AppContext = Depends(get_ctx),
) -> FileResponse:
    """One picture of a filled form, with sensitive fields covered. Served only from the student's own folder, never cached.

    The covering hides every sensitive answer, not the rest: the picture shows each other value the form holds, so it needs the
    student's own browser session (the same protection as ``values``), not an access token a script holds.
    """
    row = _own_run(conn, user_id, run_id)
    root = ctx.config.apply_storage
    path = apply_runner.screenshot_path(root, user_id, row, index) if root is not None else None
    if path is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=apply_runner.NO_PICTURE)
    return FileResponse(path, media_type="image/png", headers={"Cache-Control": "no-store"})

# The card's two answers and its Mark as applied? button. They need the student's own browser session, like the sensitive
# answers: an attempt that may have reached Greenhouse is settled by the student looking, never by a script. They do not need
# Apply for me to be on: a student who turned it off still has attempts to settle.
@router.post("/api/v1/apply-agent/claims/{token}/resolve")
def resolve_apply_claim(
    token: str,
    payload: ApplyClaimResolveRequest,
    user_id: str = Depends(require_browser_session),
    conn: sqlite3.Connection = Depends(writable_connection),
) -> dict[str, Any]:
    """It went through / It didn't go through, for an attempt that may have reached Greenhouse."""
    try:
        return {"claim": apply_watch.resolve_by_student(conn, token, user_id=user_id, went_through=payload.went_through)}
    except apply_runs.ClaimNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="This attempt was not found") from exc
    except apply_runs.ClaimHeldError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="This application is still being submitted") from exc
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="This attempt does not need an answer") from exc


@router.post("/api/v1/apply-agent/claims/{token}/mark-applied")
def mark_apply_claim_applied(
    token: str,
    user_id: str = Depends(require_browser_session),
    conn: sqlite3.Connection = Depends(writable_connection),
) -> dict[str, Any]:
    """Mark as applied?: move the application forward after a Finish in browser submission, which never moves it by itself."""
    try:
        return {"claim": apply_watch.mark_applied(conn, token, user_id=user_id)}
    except apply_runs.ClaimNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="This attempt was not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="This attempt has nothing to mark") from exc
