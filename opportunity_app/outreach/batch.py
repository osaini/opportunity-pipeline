"""Matching a model's reply about a batch of companies back to the batch.

The location search and the email search ask a model about several companies at once and get
{"companies": [{"company": "...", ...}]} back. Each then checks its own kind of claim and shapes its own
results; what they share is reading the reply and pairing each answer with the target it names.
Standard library plus agent_providers only.
"""

from __future__ import annotations

from typing import Any

from ..integrations.agent_providers import CliAgentProvider


def answers_by_target(
    raw: str, targets: list[dict[str, Any]], what: str,
) -> tuple[list[tuple[dict[str, Any], dict[str, Any]]], list[dict[str, Any]]]:
    """(answer, target) pairs in the order the model answered, and the targets it did not answer for.

    ``what`` names the search in the error when the reply has no companies list ("location search"). An answer
    that is not an object, names no target of the batch, or names one a second time is skipped. A target is found
    by its company name case-folded as stored, an answer by its company name with whitespace collapsed and
    case-folded: the two keys are deliberately not normalised alike, as before, so a stored name with a double
    space is never matched. Two targets whose names case-fold alike collapse to the later one. The unanswered
    come back in the batch's order.
    """
    parsed = CliAgentProvider.extract_json(raw)
    answers = parsed.get("companies")
    if not isinstance(answers, list):
        raise ValueError(f"The {what} reply had no companies list")
    by_name = {target["company"].casefold(): target for target in targets}
    seen: set[str] = set()
    matched: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for answer in answers:
        if not isinstance(answer, dict):
            continue
        key = " ".join(str(answer.get("company") or "").split()).casefold()
        target = by_name.get(key)
        if target is None or key in seen:
            continue
        seen.add(key)
        matched.append((answer, target))
    return matched, [target for key, target in by_name.items() if key not in seen]
