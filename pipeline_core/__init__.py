"""Framework-neutral read model shared by the legacy CLI and web app.

Legacy pipeline functions live in :mod:`pipeline`.
"""

from .read_model import OpportunityFilters, OpportunityRepository

__all__ = ["OpportunityFilters", "OpportunityRepository"]
