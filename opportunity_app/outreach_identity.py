"""Which company a mail address, a host or a name belongs to, and whether an address is a person's.

The rules the reply reader (outreach_inbox) uses to say whose mail something is,
and that the interviewer and company research share: who is "anyone at the
company" (its own website's domain, never a platform's, a university's or the
student's own), whether an address is one person's or a shared inbox or a machine,
and whether a From name and its address belong together. Pure functions over
strings, plus the shipped sender lists of mail_trust and the student's own
sending address. No database, no Gmail.

Two things that look alike stay apart. ``domain_of`` here folds and strips a
sender's domain the way the reply reader needs; application_inbox's
``sender_domain`` takes an already lowercased address, outreach_labels' ``_host``
takes the text after the @ as written, and outreach_thank_you's
``_job_system`` strips angle brackets first. And ``mailbox_key`` (mail_message)
decides whether two addresses are one mailbox; ``university_alias`` decides
whether two mailboxes at one university are one person.
"""

from __future__ import annotations

import re
import unicodedata
from urllib.parse import urlsplit

from pipeline_core.identity import identity_tokens, normalized

from .mail_trust import FREEMAIL, not_an_employer, registrable_domain
from .outreach import website_domain
from .outreach_contacts import GENERIC_LOCAL_PARTS
from .outreach_drafting import sender_account
from .outreach_forms import NO_REPLY_SENDER


# Senders that are a machine rather than a person or a shared inbox.
_MACHINE_SENDER = re.compile(
    r"^(no-?reply|do-?not-?reply|donotreply|notifications?|notify|alerts?|mailer|bounces?|news(letters?)?|"
    r"marketing|updates?|digest|calendar(-notification)?|invitations?|billing|invoices?|receipts?|"
    r"system|robot|bot|automated|auto|wordpress|forms?)([+._-]|$)|(no-?reply|noreply|donotreply)",
    re.IGNORECASE,
)
# The first part of a website host that names a section, not the company (careers.acme.com is acme.com).
_SECTION_LABELS = {"www", "careers", "jobs", "about", "en", "home", "go", "get", "app", "blog", "team", "info", "corp", "company"}
# Hosts where a website is one tenant of many (a LinkedIn page, a Google Site,
# a store builder, an applicant system): anyone at the host is not the company.
# A company's own big domain (microsoft.com) is never here.
PLATFORM_HOSTS = {
    "sites.google.com", "linkedin.com", "facebook.com", "instagram.com", "x.com", "twitter.com", "youtube.com",
    "tiktok.com", "medium.com", "substack.com", "github.io", "gitlab.io", "notion.site", "notion.so", "wixsite.com",
    "squarespace.com", "wordpress.com", "weebly.com", "godaddysites.com", "myshopify.com", "carrd.co", "linktr.ee",
    "crunchbase.com", "angel.co", "wellfound.com", "ycombinator.com", "producthunt.com", "webflow.io", "framer.website",
    "framer.ai", "canva.site", "hs-sites.com", "hubspotpagebuilder.com", "about.me", "bit.ly", "atlassian.net",
    "teamtailor.com", "personio.de", "personio.com", "rippling.com", "dover.com", "gem.com", "pinpointhq.com",
    "jazz.co", "applytojob.com", "eightfold.ai", "oraclecloud.com", "csod.com", "zohorecruit.com", "workable.com",
    "recruitee.com", "breezy.hr", "bamboohr.com", "jobvite.com", "ashbyhq.com", "lever.co", "greenhouse.io",
    "github.com", "gitlab.com", "bitbucket.org", "huggingface.co", "kaggle.com", "discord.com", "discord.gg",
    "slack.com", "meetup.com", "eventbrite.com", "devpost.com", "behance.net", "dribbble.com", "angellist.com",
}
# Registrable domains that stand for a whole university or government: never "anyone at the domain".
_INSTITUTION = re.compile(r"(^|\.)(edu|gov|mil)$|(^|\.)(ac|edu|gov|mil|govt|gob|gouv)\.[a-z]{2}$")


def is_machine_local(local: str) -> bool:
    return bool(_MACHINE_SENDER.search(local)) or bool(NO_REPLY_SENDER.search(f"{local}@"))


# Words that make an address a team's or a function's (recruitment@, hiring-team@, bovi.careers@): a part of the
# address that starts with a long one, or is exactly a short one (so hrishi@ and stafford@ stay people).
_ROLE_PREFIX = re.compile(
    r"recruit|hiring|talent|career|campus|universit|internship|student|people|communit|research|admission|feedback|"
    r"educat|sponsor|welcome|onboard|partner|investor|founder|contact|inquir|enquir|support|operations|marketing",
    re.IGNORECASE,
)
_ROLE_EXACT = {
    "jobs", "job", "team", "help", "info", "hello", "hi", "sales", "admin", "ops", "hr", "staff", "group", "lab", "labs",
    "press", "media", "store", "shop", "event", "events", "office", "intern", "interns", "people", "careers", "billing",
}


def role_word(token: str) -> bool:
    return token in _ROLE_EXACT or bool(_ROLE_PREFIX.match(token))


def is_person(address: str, company: str = "") -> bool:
    """Whether an address looks like one person's (dana@, d.reyes@), not a shared inbox (careers@, recruitment@), a
    company-named one (bovi@ for Bovi), or a machine (noreply@)."""
    local = str(address or "").split("@", 1)[0].casefold().split("+", 1)[0]
    if not local or local in GENERIC_LOCAL_PARTS or is_machine_local(local):
        return False
    tokens = [token for token in re.split(r"[._\-]+", local) if token]
    if any(role_word(token) for token in tokens):
        return False
    stem = _letters(domain_of(address).split(".")[0]) if "@" in str(address) else ""
    names = set(company_words(company).split()) if company else set()
    return not ((stem and _letters(local) == stem) or (names and set(tokens) <= names))


def _letters(text: str) -> str:
    return re.sub(r"[^a-z]", "", unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode().casefold())


def name_matches_address(display: str, address: str, company: str = "") -> bool:
    """Whether the From name and address are one person's: Dana Reyes as dana@, d.reyes@, dreyes@ or reyes.d@.

    Words of the company's name or a role ("Bovi Careers") are not a person's name, and one must remain.
    """
    ignore = set(company_words(company).split()) if company else set()
    words = [_letters(word) for word in re.split(r"[\s,.'\-]+", display) if _letters(word)]
    words = [word for word in words if word not in ignore and not role_word(word)]
    local = _letters(str(address).split("@", 1)[0].split("+", 1)[0])
    if not words or not local:
        return False
    if local in words or any(len(word) >= 3 and word in local for word in words):
        return True
    first, last = words[0], words[-1]
    return len(words) >= 2 and local in {first[0] + last, first + last[0], last + first[0], "".join(word[0] for word in words)}


# --- Who each company is ------------------------------------------------------------


def domain_of(address: str) -> str:
    return address.rsplit("@", 1)[-1].casefold().strip().rstrip(".>") if "@" in address else ""


def is_platform_host(host: str) -> bool:
    return any(host == shared or host.endswith(f".{shared}") for shared in PLATFORM_HOSTS) or not_an_employer(host)


def is_institution(host: str) -> bool:
    return bool(_INSTITUTION.search(registrable_domain(host) or host))


def own_domains() -> set[str]:
    """The student's own sending domain and its registrable domain: their school's or employer's mail is never a company's."""
    domain = domain_of(sender_account())
    return {item for item in (domain, registrable_domain(domain) or "") if item}


def is_own(host: str, own: set[str]) -> bool:
    return bool(own) and (host in own or (registrable_domain(host) or host) in own or any(host.endswith(f".{item}") for item in own))


def names_host(company: str, host: str) -> bool:
    """Whether a company's name carries a host's name: Rippling for rippling.com, never Acme for linkedin.com."""
    stem = re.sub(r"[^a-z0-9]", "", (registrable_domain(host) or host).split(".")[0])
    words = [re.sub(r"[^a-z0-9]", "", word) for word in str(company or "").casefold().split()]
    return len(stem) >= 3 and (stem in words or stem == "".join(words))


def website_strength(url: str, host: str, company: str) -> bool:
    """Whether a website's domain is as good as proof: its root, or a page on a host its name carries (not uwaterloo.ca/bovi-lab)."""
    text = str(url or "").strip()
    try:
        path = urlsplit(text if "//" in text else f"https://{text}").path
    except ValueError:
        return False
    return path in {"", "/"} or names_host(company, host) or len([part for part in path.split("/") if part]) == 1 and not _INSTITUTION.search(host)


def site_domain(url: str, own: set[str], company: str = "") -> str:
    """The domain a company's website stands for, or '' when it says nothing (a page on a platform, a university, the student's own).

    A platform's own site (www.rippling.com for Rippling) is its company's
    domain; a page on it (linkedin.com/company/acme, sites.google.com/view/acme,
    acme.wixsite.com) is not.
    """
    host = website_domain(url)
    if not host or "." not in host or host in FREEMAIL or is_institution(host) or is_own(host, own):
        return ""
    if is_platform_host(host) and not names_host(company, host):
        text = str(url or "").strip()
        try:
            path = urlsplit(text if "//" in text else f"https://{text}").path
        except ValueError:
            path = "/x"
        if host != (registrable_domain(host) or host) or path not in {"", "/"}:
            return ""
    labels = host.split(".")
    # careers.acme.com is acme.com: one label naming a section of the site is dropped, no more.
    if len(labels) > 2 and labels[0] in _SECTION_LABELS:
        rest = ".".join(labels[1:])
        if registrable_domain(host) is None or registrable_domain(rest) == registrable_domain(host):
            host = rest
    return host


def _stem(domain: str) -> str:
    return (registrable_domain(domain) or domain).split(".")[0]


_STEM_SUFFIXES = {"", "inc", "hq", "co", "corp", "labs", "lab", "ai", "io", "tech", "group", "global", "us", "usa", "mail", "team", "app"}


def contact_domain(address: str, site: str, own: set[str], company: str = "") -> tuple[str, bool]:
    """The domain of a contact's address as a company domain, and whether it is as good as the website's.

    Only when it shares the website's name (bovirobotics.us for
    bovirobotics.com), or there is no website: a contact at an ISP, a VC, an
    agency or a platform stands for nobody else there. A contact at a
    university stands for their own department's host (cs.stateu.edu, never
    stateu.edu), and only weakly: what comes from there is at most a possible reply.
    """
    domain = domain_of(address)
    if not domain or domain in FREEMAIL or (registrable_domain(domain) or domain) in FREEMAIL or is_own(domain, own):
        return "", False
    if site and (domain == site or domain.endswith(f".{site}")):
        return domain, True
    if is_institution(domain):
        return (domain, False) if not site else ("", False)
    if is_platform_host(domain):
        # A platform's own people (jane@rippling.com for Rippling, with no website on file) are its company's.
        return (domain, False) if not site and names_host(company, domain) and domain == (registrable_domain(domain) or domain) \
            else ("", False)
    if site:
        stem, theirs = _stem(site), _stem(domain)
        # The same name, or the name and a corporate word (bovirobotics.us, bovirobotics-inc.com), never another company's.
        rest = re.sub(r"[^a-z0-9]", "", theirs[len(stem):]) if theirs.startswith(stem) else None
        return (domain, True) if len(stem) >= 4 and rest in _STEM_SUFFIXES else ("", False)
    return domain, False


def university_alias(address: str) -> str:
    """At a university, one person's mailboxes on its hosts (jkim@stateu.edu, jkim@cs.stateu.edu) as one; else ''."""
    domain = domain_of(address)
    if not domain or not is_institution(domain):
        return ""
    return f"{address.split('@', 1)[0].casefold().split('+', 1)[0]}@{registrable_domain(domain) or domain}"


def company_words(company: str) -> str:
    """A company's name as it is written in mail, without its legal suffix: 'Bovi Robotics, Inc.' is 'bovi robotics'."""
    tokens = identity_tokens(company)
    return " ".join(word for word in normalized(company).split() if word in tokens)


def is_distinctive(company: str) -> bool:
    """Whether a company's name is specific enough to search mail for: two words, or one of six letters or more."""
    words = company_words(company).split()
    return len(words) >= 2 or any(len(word) >= 6 for word in words)
