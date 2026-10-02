"""HTTP errors several routes raise in exactly the same words.

Only blocks that were written identically in every route are here. The other exception-to-status mappings differ on purpose (which
class maps to which status, and whether a RuntimeError keeps its own message), so they stay in the routes, in the order their
`except` clauses need. No app-wide exception handlers are registered: an unmapped domain error is a 500 in some routes and a
404 or 422 in others, and a global handler would change that.
"""

from __future__ import annotations

from fastapi import HTTPException, status

from ..outreach.gmail import SendNeedsCheckError


def outreach_not_found() -> HTTPException:
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Outreach target not found")


def send_needs_check(exc: SendNeedsCheckError) -> HTTPException:
    """428: the same request succeeds once it carries the named check."""
    return HTTPException(
        status_code=status.HTTP_428_PRECONDITION_REQUIRED, detail={"msg": str(exc), "check": exc.check},
    )
