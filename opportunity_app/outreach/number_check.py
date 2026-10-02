"""How the outreach checks read numbers: the word tokenizer, and the whole-number keys a text claims.

The quote check (quote_check), the email drafts (drafting), the call-prep notes (call_prep) and the decline thank-you
(thank_you_writing) all refuse a number no input supports. They share this one reading, so a number glued to letters
(H200, Q4) counts as a number everywhere, 1,500 and 1500 are one number, and a 45% is claimed as a percentage.
Standard library only; it knows nothing about agents, research or stored mail.
"""

from __future__ import annotations

import re
import unicodedata


def _normalized(text: str) -> str:
    text = unicodedata.normalize("NFKC", str(text or "")).casefold()
    text = text.replace("’", "'").replace("‘", "'")
    # "don't" is "do not", and 45% is "45 percent": the same words either way.
    return re.sub(r"n't", " not", text).replace("%", " percent ")


# A number keeps its decimals (0.1, 725.00); 1,500 and 1500 are the same number.
_TOKEN = re.compile(r"\d+(?:[.,]\d+)*|[^\W\d_]+")


def word_tokens(text: str) -> list[str]:
    return [token.replace(",", "") if token[0].isdigit() else token for token in _TOKEN.findall(_normalized(text))]


def number_tokens(tokens: list[str]) -> set[str]:
    return {token for token in tokens if token[0].isdigit()}


# What a number check takes out of the text first, on both sides (the draft and the inputs): email addresses,
# links with a scheme, and links without one (github.com/t/arm-2024, acme360.com), whose digits name a page,
# not a fact. A scheme-less link is a dotted host ending in a lowercase TLD of 2+ letters that ends the word,
# optionally followed by a path. The label before the TLD must hold a letter, so 3.5, U.S., Ph.D., e.g. and
# v2.0 are not hosts, nor is "2024.Then" (a missing space after a full stop). Call prep's number check shares this.
#
# A host whose labels hold a digit is a link only when it ends in a TLD from this list or carries a path. A
# lowercase word is not evidence enough: "40k.users" and "1.5x.overall" are a figure run into the next
# sentence, and "users" is no TLD, so their digits stay numbers to check. A link that is none of these
# (acme360.studio) keeps its digits as numbers, which only makes the check stricter. English words that are
# also TLDs (in, it, is, me, so, to, us) are left out so "40k.in" is not hidden either.
_LINK_TLDS = (
    "com|org|net|edu|gov|io|ai|co|dev|app|tech|xyz|info|biz|cloud|ly|uk|ca|de|fr|eu|jp|cn|nl|au"
)
_HOST_WITH_DIGITS = r"(?:[A-Za-z0-9-]+\.)*[A-Za-z0-9-]*[A-Za-z][A-Za-z0-9-]*\."
_HOST_WITHOUT_DIGITS = r"(?:[A-Za-z-]+\.)*[A-Za-z-]*[A-Za-z][A-Za-z-]*\."
ADDRESS_PATTERN = re.compile(
    r"\S+@\S+|https?://\S+"
    # A scheme-less host, which must start a word. Branch 1: any lowercase TLD, with a path. Branch 2: a known
    # TLD, with or without a path. Branch 3: a host with no digit at all, which hides nothing.
    rf"|(?<![\w@.-])(?:"
    rf"{_HOST_WITH_DIGITS}[a-z]{{2,}}(?![\w-])/\S*"
    rf"|{_HOST_WITH_DIGITS}(?:{_LINK_TLDS})(?![\w-])"
    rf"|{_HOST_WITHOUT_DIGITS}[a-z]{{2,}}(?![\w-])"
    rf")"
)


def _number_key(token: str) -> str:
    """The number as a comparison key: 09 and 9 are one number, 3.50 and 3.5 are one, 3.5 and 5 are not."""
    whole, point, fraction = token.partition(".")
    whole = whole.lstrip("0") or "0"
    fraction = fraction.rstrip("0")
    return f"{whole}.{fraction}" if fraction else whole


def number_keys(text: str) -> list[tuple[str, str]]:
    """Each number in the text as (its key, the key a draft needs to claim it).

    Whole numbers, as outreach_call_prep checks them, not pieces of text: the tokenizer
    is the quote check's (number_check.word_tokens), so 1,500 and 1500 are one number and a range such as
    2019-2023 is two. A number written as a percentage (45% or 45 percent) is claimed
    as a percentage, so it needs 45% in the inputs, not a headcount of 45.
    """
    tokens = word_tokens(text)
    keys = []
    for index, token in enumerate(tokens):
        if token[0].isdigit():
            key = _number_key(token)
            keys.append((key, key + "%" if tokens[index + 1:index + 2] == ["percent"] else key))
    return keys


def supported_numbers(pieces) -> set[str]:
    """What the pieces of the inputs' own words let a draft claim: a bare 45 may be a 45% too, a 45% is only a percentage."""
    return {key for piece in pieces for pair in number_keys(piece) for key in pair}
