"""The HTML pages the browser opens."""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends
from fastapi.responses import HTMLResponse

from ..assets import versioned_page
from ..context import AppContext
from ..dependencies import get_ctx


router = APIRouter()


@router.get("/employer", include_in_schema=False)
@router.get("/admin", include_in_schema=False)
def role_workspace(ctx: AppContext = Depends(get_ctx)) -> HTMLResponse:
    return versioned_page(ctx, "ops.html")


@router.get("/connections/oauth/{provider}/callback", include_in_schema=False)
def oauth_callback_page(provider: Literal["google", "microsoft", "gmail_drafts"], ctx: AppContext = Depends(get_ctx)) -> HTMLResponse:
    return versioned_page(ctx, "oauth-callback.html")


@router.get("/market", include_in_schema=False)
def public_market_page(ctx: AppContext = Depends(get_ctx)) -> HTMLResponse:
    return versioned_page(ctx, "market.html")


@router.get("/", include_in_schema=False)
@router.get("/urgent", include_in_schema=False)
@router.get("/programs", include_in_schema=False)
@router.get("/saved", include_in_schema=False)
@router.get("/applications", include_in_schema=False)
@router.get("/outreach", include_in_schema=False)
@router.get("/prepare", include_in_schema=False)
@router.get("/agent", include_in_schema=False)
@router.get("/profile", include_in_schema=False)
@router.get("/opportunities/{opportunity_id}", include_in_schema=False)
def web_app(opportunity_id: str | None = None, ctx: AppContext = Depends(get_ctx)) -> HTMLResponse:
    return versioned_page(ctx, "index.html")
