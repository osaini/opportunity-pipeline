"""Opportunity list, detail, company tags, deadlines, resume picks, Jev review, intent actions, facets and stats."""

from __future__ import annotations

import sqlite3
from typing import Annotated, Any, Literal

from fastapi import Depends, HTTPException, Header, Query, Response, status

from pipeline_core import MAX_PER_COMPANY, OpportunityFilters, OpportunityRepository
from ..overrides import shared_router
from ...student import resume_variants
from ...applications.actions import OpportunityNotFoundError, record_intent
from ...core.company_tags import CompanyNotFoundError, decorate_with_tags, set_company_tag, tag_facets_for_keys
from ...applications.urgent import (
    DeadlineNotFoundError,
    clear_user_deadline,
    set_user_deadline,
    user_deadline,
    user_deadlines_for,
    visible_opportunity,
)
from ...student.profile import is_personalized
from ...core.profile_store import read_stored_profile
from ...integrations.typesafe_decisions import (
    TypeSafeError,
    TypeSafeNotConfigured,
    review_opportunity as review_opportunity_with_typesafe,
)
from ..context import AppContext
from ..dependencies import get_ctx, repository, require_auth, writable_connection
from ..models.opportunities import (
    CompanyTagRequest,
    DeadlineRequest,
    IntentRequest,
    OpportunityListResponse,
    ResumePickRequest,
)


router = shared_router()
review_router = shared_router()
facets_router = shared_router()


@router.get("/api/v1/opportunities", response_model=OpportunityListResponse)
def list_opportunities(
    repo: OpportunityRepository = Depends(repository),
    q: str = Query(default="", max_length=200),
    role_type: str = Query(default="", max_length=80),
    pipeline_status: str = Query(default="", alias="status", max_length=80),
    intent_state: Literal["", "undecided", "saved", "passed"] = "",
    exclude_passed: bool = False,
    region: str = Query(default="", max_length=120),
    source: str = Query(default="", max_length=160),
    term: str = Query(default="", max_length=80),
    graduation_year: int | None = Query(default=None, ge=2024, le=2100),
    remote_mode: Literal["", "remote", "hybrid", "onsite", "unknown"] = "",
    min_hourly_pay: float | None = Query(default=None, ge=0, le=1000),
    posted_since: str = Query(default="", max_length=40),
    deadline_before: str = Query(default="", max_length=40),
    tag: str = Query(default="", max_length=25),
    company: str = Query(default="", max_length=200),
    per_company: int = Query(default=0, ge=0, le=MAX_PER_COMPANY),
    sort: Literal["score", "newest", "discovered", "company", "deadline"] = "score",
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    include_inactive: bool = False,
    include_duplicates: bool = False,
) -> OpportunityListResponse:
    filters = OpportunityFilters(
        query=q,
        role_type=role_type,
        status=pipeline_status,
        intent_state=intent_state,
        exclude_passed=exclude_passed,
        region=region,
        source=source,
        term=term,
        graduation_year=graduation_year,
        remote_mode=remote_mode,
        min_hourly_pay=min_hourly_pay,
        posted_since=posted_since,
        deadline_before=deadline_before,
        tag=tag,
        company=company,
        per_company=per_company,
        sort=sort,
        active_only=not include_inactive,
        unique_only=not include_duplicates,
        limit=limit,
        offset=offset,
    ).normalized()
    items, total = repo.list(filters)
    if repo.user_id:
        # Decorated here, not in pipeline_core, so the CLI's contract is unchanged.
        deadlines = user_deadlines_for(
            repo.connection, [item["id"] for item in items], user_id=repo.user_id
        )
        picks = resume_variants.picks_for(repo.connection, [item["id"] for item in items], user_id=repo.user_id)
        items = [
            {**item, "user_deadline_on": deadlines.get(item["id"]), "resume_pick": picks.get(item["id"])}
            for item in items
        ]
        items = decorate_with_tags(repo.connection, items, user_id=repo.user_id)
    return OpportunityListResponse(
        items=items,
        total=total,
        limit=filters.limit,
        offset=filters.offset,
        sort=filters.sort,
        per_company=0 if filters.company else filters.per_company,
        personalized=is_personalized(repo.connection, user_id=repo.user_id) if repo.user_id else True,
    )


@router.get("/api/v1/opportunities/{opportunity_id}")
def get_opportunity(
    opportunity_id: str,
    repo: OpportunityRepository = Depends(repository),
) -> dict[str, Any]:
    item = repo.get(opportunity_id)
    if not item:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Opportunity not found")
    if repo.user_id:
        item = {
            **item,
            "user_deadline": user_deadline(repo.connection, opportunity_id, user_id=repo.user_id),
            "can_set_user_deadline": visible_opportunity(repo.connection, repo.user_id, opportunity_id),
            "resume_pick": resume_variants.stored_pick(repo.connection, repo.user_id, opportunity_id),
        }
        item = decorate_with_tags(repo.connection, [item], user_id=repo.user_id)[0]
    return item


@router.put("/api/v1/company-tags")
def put_company_tag(
    payload: CompanyTagRequest,
    conn: sqlite3.Connection = Depends(writable_connection),
    user_id: str = Depends(require_auth),
) -> dict[str, Any]:
    """Add or remove one tag on a company, for the caller only."""
    try:
        return set_company_tag(conn, payload.company, payload.tag, user_id=user_id, present=payload.present)
    except CompanyNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Company not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc


@router.put("/api/v1/opportunities/{opportunity_id}/deadline")
def put_user_deadline(
    opportunity_id: str,
    payload: DeadlineRequest,
    conn: sqlite3.Connection = Depends(writable_connection),
    user_id: str = Depends(require_auth),
) -> dict[str, Any]:
    """Record the student's own deadline for a role (never used by the purge)."""
    try:
        return set_user_deadline(
            conn, opportunity_id, user_id=user_id, deadline_on=payload.deadline_on, note=payload.note
        )
    except DeadlineNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Opportunity not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc


@router.delete("/api/v1/opportunities/{opportunity_id}/deadline", status_code=status.HTTP_204_NO_CONTENT)
def delete_user_deadline(
    opportunity_id: str,
    conn: sqlite3.Connection = Depends(writable_connection),
    user_id: str = Depends(require_auth),
) -> Response:
    clear_user_deadline(conn, opportunity_id, user_id=user_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/api/v1/opportunities/{opportunity_id}/resume-pick")
def get_resume_pick(
    opportunity_id: str,
    conn: sqlite3.Connection = Depends(writable_connection),
    user_id: str = Depends(require_auth),
) -> dict[str, Any]:
    """The résumé variant for this role: the pick in force, what the words suggest, and the résumés to choose from."""
    try:
        return resume_variants.pick_view(conn, user_id, opportunity_id)
    except OpportunityNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Opportunity not found") from exc


@router.put("/api/v1/opportunities/{opportunity_id}/resume-pick")
def put_resume_pick(
    opportunity_id: str,
    payload: ResumePickRequest,
    conn: sqlite3.Connection = Depends(writable_connection),
    user_id: str = Depends(require_auth),
) -> dict[str, Any]:
    """The student's own pick for this role. It wins, and automation never overwrites it."""
    try:
        return resume_variants.set_student_pick(conn, user_id, opportunity_id, payload.resume_file_id)
    except OpportunityNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Opportunity not found") from exc
    except resume_variants.ResumePickError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc


@router.get("/api/v1/opportunities/{opportunity_id}/resume-check")
def get_resume_check(
    opportunity_id: str,
    conn: sqlite3.Connection = Depends(writable_connection),
    user_id: str = Depends(require_auth),
) -> dict[str, Any]:
    """Confirmed profile skills this posting names that the chosen résumé never mentions. Informational only."""
    try:
        return resume_variants.resume_check(conn, user_id, opportunity_id)
    except OpportunityNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Opportunity not found") from exc


@review_router.post("/api/v1/opportunities/{opportunity_id}/jev-review")
def opportunity_jev_review(
    opportunity_id: str,
    repo: OpportunityRepository = Depends(repository),
    ctx: AppContext = Depends(get_ctx),
) -> dict[str, Any]:
    """Run an explicit, non-persisted Jev review over bounded fields."""
    item = repo.get(opportunity_id)
    if not item:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Opportunity not found")
    profile = read_stored_profile(repo.connection, repo.user_id)
    try:
        return review_opportunity_with_typesafe(
            ctx.services.typesafe_client_factory(), item, profile
        )
    except TypeSafeNotConfigured as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)
        ) from exc
    except TypeSafeError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)
        ) from exc


@review_router.post("/api/v1/opportunities/{opportunity_id}/actions")
def opportunity_action(
    opportunity_id: str,
    payload: IntentRequest,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
    conn: sqlite3.Connection = Depends(writable_connection),
    user_id: str = Depends(require_auth),
) -> dict[str, Any]:
    if idempotency_key is not None and (not idempotency_key.strip() or len(idempotency_key) > 200):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Idempotency-Key must contain 1 to 200 characters",
        )
    try:
        return record_intent(
            conn,
            opportunity_id,
            payload.action,
            idempotency_key=idempotency_key, user_id=user_id)
    except OpportunityNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Opportunity not found",
        ) from exc
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(exc),
        ) from exc


@facets_router.get("/api/v1/facets")
def facets(repo: OpportunityRepository = Depends(repository)) -> dict[str, list[Any]]:
    facet_values, company_keys = repo.facets_with_company_keys()
    result: dict[str, list[Any]] = dict(facet_values)
    if repo.user_id:
        # The same active, visible rows the facets were just collected from, so the view is scanned once.
        result["tags"] = tag_facets_for_keys(repo.connection, company_keys, user_id=repo.user_id)
    return result


@facets_router.get("/api/v1/stats")
def stats(repo: OpportunityRepository = Depends(repository)) -> dict[str, int]:
    # "tracked" counts any status past discovered, shortlisted roles included;
    # "applications" matches what the Applications view lists.
    applications = repo.connection.execute("SELECT COUNT(*) FROM applications WHERE user_id=?", (repo.user_id,)).fetchone()[0]
    return {**repo.stats(), "applications": int(applications)}
