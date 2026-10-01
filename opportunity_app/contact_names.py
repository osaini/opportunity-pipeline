"""The words and shapes that name a mailbox or a website, shared by contact finding, the reply reader and the senders.

A leaf module: standard library only. It holds the lists of mailbox names that are a team's rather than a person's
(``GENERIC_LOCAL_PARTS``, ``ROLE_INBOX_LOCAL_PARTS``, ``ROLE_INBOX_QUALIFIERS``), the pattern of a machine's From address
(``NO_REPLY_SENDER``) and ``website_domain``, so that ``outreach_identity`` can say whose mail something is without
loading outreach, the contact finder, the form sender or Gmail.
"""

from __future__ import annotations

import re
from urllib.parse import urlsplit

GENERIC_LOCAL_PARTS = {
    "info", "hello", "hi", "contact", "careers", "jobs", "team", "hr", "admin", "support", "sales",
    "press", "media", "inquiries", "enquiries", "office", "general", "recruiting", "talent",
    "internships", "people", "founders", "partners", "business",
}
# Beyond GENERIC_LOCAL_PARTS (which contact finding ranks and may write to), the local parts of a team,
# role or system inbox: never one person. is_shared_inbox reads both; contact finding is unchanged.
ROLE_INBOX_LOCAL_PARTS = frozenset({
    "recruitment", "recruiting", "recruiter", "recruiters", "careers", "career", "jobs", "job", "hiring", "hiring-team",
    "hiringteam", "talent", "talentacquisition", "acquisition", "ta", "hr", "people", "peopleops", "ops", "internships",
    "internship", "interns", "intern", "university", "universityrecruiting", "campus", "campusrecruiting", "early",
    "earlycareers", "apply", "applications", "application", "candidates", "candidate", "noreply", "no-reply", "donotreply",
    "do-not-reply", "notifications", "notification", "notify", "mailer", "support", "help", "helpdesk", "info", "hello",
    "contact", "team", "office", "admin", "service", "services", "relations", "staffing", "sourcing", "resourcing",
    "resources", "human", "operations", "graduate", "graduates", "student", "students", "joinus", "workwithus",
    "mailerdaemon", "postmaster", "bounce", "bounces",
})
# Words a role inbox adds for where or when it hires ("recruiting-us", "emea-recruiting", "internships2026" once its
# digits go): never a role on their own, and never enough without a role word beside them.
ROLE_INBOX_QUALIFIERS = frozenset({
    "us", "usa", "uk", "eu", "emea", "apac", "amer", "americas", "na", "latam", "anz", "global", "intl", "international",
})
# A From address that is a form or notification machine: the start of the address, then the separator after it.
NO_REPLY_SENDER = re.compile(r"^(no-?reply|do-?not-?reply|notifications?|mailer|forms?)[@+._-]", re.IGNORECASE)


def website_domain(url: str) -> str:
    """The host of a website URL, lowercased, without a leading www."""
    text = str(url or "").strip()
    if not text:
        return ""
    host = urlsplit(text if "//" in text else f"https://{text}").hostname or ""
    return host.lower().removeprefix("www.")
