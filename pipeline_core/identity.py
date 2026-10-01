"""How the product names and folds an employer: one home for the rules, standard library only.

Three different folds exist on purpose and must never be merged, because each is
persisted or matched against stored values:

* ``sort_key``: the casefold ``opportunities.company_sort_key`` and
  ``title_sort_key`` are stored with, and the key company tags are stored under.
* ``employer_key``: the sorted identity tokens that the apply run, apply answer
  and employer-domain tables store. A change to the identity rule orphans those
  rows, so ``tests/test_leaf_modules.py`` pins it.
* ``opportunity_app.outreach_identity.company_key``: a third, different rule (NFKC, "&"
  becomes "and", drops a leading "The" and trailing legal suffixes, keeps word
  order) that outreach targets are matched on. It is not here and must not be
  folded into ``employer_key``: that would change which outreach targets match.
"""

from __future__ import annotations

import re
from typing import Any


def normalized(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", value.lower()).strip()


def normalized_text(text: Any) -> str:
    """``normalized`` for a value that may be None or not a string: ``None`` reads as empty.

    The apply policy and its answer store compare question text through this.
    It tests ``is not None`` rather than truthiness, so ``0`` and ``False``
    read as "0" and "false".
    """
    return normalized(str(text if text is not None else ""))


def sort_key(value: str | None) -> str:
    """The stored fold used to order company and title.

    One function, applied once at write time, so every backend orders by the
    same bytes. `casefold` rather than `lower` because it is the fold the
    tenant path has always used -- `lower` leaves U+00DF alone and would change
    which of 'Straße' and 'Strasse' comes first.

    ``opportunity_app.schema`` writes the ``company_sort_key`` column with this
    and ``pipeline_core.read_model`` filters on it, so both import this one
    definition. It is also the key ``company_tags`` stores tags under.
    """

    return str(value or "").casefold()


# Dropped before comparing a queried name against a board's own name, so
# "Firefly Aerospace Inc." and "Firefly Aerospace" are the same employer.
CORPORATE_SUFFIXES = {
    "inc",
    "incorporated",
    "llc",
    "ltd",
    "limited",
    "corp",
    "corporation",
    "co",
    "company",
    "group",
    "holdings",
    "the",
}


def identity_tokens(name: str) -> frozenset[str]:
    """The words that identify an employer, without corporate suffixes: "Acme Robotics Inc." -> {acme, robotics}.

    Public because the web app's application-email matching compares company
    names by the same rule the board identity check uses.
    """
    return frozenset(normalized(name).split()) - CORPORATE_SUFFIXES


def employer_key(name: str) -> str:
    """The words that identify an employer, sorted and joined: "Acme Robotics Inc." and "ACME robotics" match.

    Stored as the ``company_key`` of the apply run, apply answer and (through
    ``mail_trust.company_key``) employer-domain tables, so the output must stay
    byte-identical. It raises ``AttributeError`` on None, as every apply-side
    caller always has: in the apply stores an empty key means "any company", so a
    missing company must not silently become one.
    """
    return " ".join(sorted(identity_tokens(name)))
