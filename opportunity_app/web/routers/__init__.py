"""The route table, built once per process: every router, in the order the app registers them.

Registration order is part of the public contract (tests/test_route_contract.py pins it): Starlette answers with the first route
that matches, so a static path such as /api/v1/outreach/export must stay ahead of /api/v1/outreach/{target_id}. A feature that
was registered in two places therefore has two routers here, listed where each one belongs. The /assets mount sits between the
two lists, where create_app mounts it.
"""

from __future__ import annotations

from . import (
    account,
    admin,
    agent,
    applications,
    apply_agent,
    apply_sessions,
    automation,
    captures,
    connections,
    dossier,
    employer,
    extension,
    market,
    opportunities,
    outreach_contacts,
    outreach_delivery,
    outreach_drafting,
    outreach_research,
    outreach_settings,
    outreach_targets,
    pages,
    preparation,
    resumes,
    session,
    system,
    typesafe,
    urgent,
)

# The ops surface sits together near the end: employer and admin authenticate with their own role tokens (require_employer,
# require_admin) and are served by ops.html and ops.js; market adds the public archive that market.html reads.
ROUTERS_BEFORE_ASSETS = (
    system.health_router,
    session.router,
    system.router,
    session.sign_out_router,
    opportunities.router,
    apply_agent.router,
    urgent.router,
    typesafe.router,
    opportunities.review_router,
    applications.router,
    outreach_targets.router,
    outreach_research.discovery_router,
    outreach_settings.automation_router,
    automation.router,
    outreach_settings.router,
    outreach_research.recontact_router,
    outreach_drafting.router,
    outreach_research.router,
    outreach_drafting.history_router,
    outreach_delivery.router,
    outreach_contacts.router,
    outreach_targets.detail_router,
    account.router,
    resumes.router,
    captures.router,
    preparation.router,
    agent.router,
    extension.router,
    apply_sessions.router,
    connections.router,
    dossier.router,
    market.router,
    employer.router,
    admin.router,
    opportunities.facets_router,
)

# The page routes come after the /assets mount.
ROUTERS_AFTER_ASSETS = (
    pages.router,
)
