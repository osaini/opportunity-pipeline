"""A frozen copy of the student-facing sentences that name Greenhouse, as they were before the per-ATS sentences (LV1b).

The constants are copied by value from the commit before the sentences took the ATS's display name (80ac76c); the sentences the code built in
f-strings are copied as functions with the same text. tests/test_apply_ats_seam.py compares each with the new sentence for Greenhouse, old
against new (AGENTS.md section 8 rule 14). Do not edit it, and do not import anything but strings into it.
"""

# opportunity_app/apply/agent_types.py
PROGRESS_STEPS = {'start': 'Starting the browser',
 'open': 'Opening the Greenhouse form',
 'read': 'Reading the form',
 'lookup': 'Looking up options for {question}',
 'fill': 'Filling {n} fields',
 'check': 'Checking every required field',
 'picture': 'Taking a picture of the filled form',
 'your_turn': 'Your turn: complete the form in the window, then press Submit application there',
 'submitting': 'Submitting to Greenhouse…',
 'security_code': 'Greenhouse emailed you a security code. The app is looking for it in your Gmail; you can also type it into the window '
                  'yourself',
 'form_elsewhere': "The form tried to send a request to {host}, which the app doesn't recognize, so the app stopped that request. If the "
                   'form shows an error, fix it and press Submit application again, or press Stop and apply from the posting instead',
 'code_typed': 'The app typed the security code from your email. Press Submit application in the window',
 'code_yours': 'Type the security code Greenhouse emailed you into the window, then press Submit application',
 'challenge': 'Greenhouse showed a check in the window. Finish it there'}

# opportunity_app/apply/agent_types.py
WINDOW_UNCONFIRMED = ("The app couldn't confirm the Chromium window closed, so it can't be sure nothing was sent. Check your email for a confirmation from "
 'Greenhouse.')

# opportunity_app/apply/agent.py
NOT_BOARD = "The app only opens Greenhouse's own job boards"

# opportunity_app/apply/agent.py
HTTP_STATUS = 'Greenhouse answered HTTP {status}'

# opportunity_app/apply/agent.py
LEGACY = "This is Greenhouse's older form, which the app does not fill yet"

# opportunity_app/apply/agent.py
UNKNOWN_PAGE = 'The page did not look like a Greenhouse application form'

# opportunity_app/apply/agent.py
NO_ENDPOINT = "The app has not confirmed Greenhouse's lookup service for this list yet, so it did not ask it"

# opportunity_app/apply/agent.py
OPEN_FAILED = 'The app could not open the Greenhouse form'

# opportunity_app/apply/agent.py
DIFFERENT_POSTING = 'Greenhouse opened a different posting from the one the app was asked to open'

# opportunity_app/apply/preflight.py
NOT_GREENHOUSE = 'Apply for me works with Greenhouse postings only, for now'

# opportunity_app/apply/preflight.py
NOT_FOUND = "The app couldn't find this posting on Greenhouse. It may be closed"

# opportunity_app/apply/preflight.py
NO_ANSWER = 'Greenhouse did not answer. Try again later'

# opportunity_app/apply/runs.py
SETTLED_BY = {'page': ('apply_agent:confirmation_page', 'confirmation_page', 'Greenhouse showed its confirmation page'),
 'email': ('apply_agent:confirmation_email', 'confirmation_email', "Greenhouse's confirmation email arrived"),
 'student': ('apply_agent:student_confirmed', 'student_confirmed', 'you said it went through')}

# opportunity_app/apply/runs.py
STOPPED_BEFORE = 'The app stopped before handing your application to Greenhouse. Nothing was sent.'

# --- sentences the old code built in f-strings (copied from the code at 80ac76c)

def refused_form(status):  # checks.decide_outcome row 4
    return f"Greenhouse refused the form (HTTP {status})"


def marked_wrong(question):  # checks.decide_outcome rows 4 and 5
    return f'. Greenhouse marked "{question}" as wrong'


def listing_mismatch(label):  # checks.join
    return f"The form does not match what Greenhouse's own listing describes ({label})"


def wording_mismatch(heard):  # checks.join
    return f"The form's wording differs from Greenhouse's listing ({heard})"


def required_not_seen(label):  # checks.check_required
    return f"Greenhouse lists \"{label}\" as required but the check did not find it on the form"


def posting_other_company(theirs_title, theirs_company, company):  # policy.posting_difference
    return f"Greenhouse's form is for {theirs_title or 'a posting'} at {theirs_company}, not {company}"


def posting_other_title(theirs_title, title):  # policy.posting_difference
    return f"Greenhouse's form is for {theirs_title}, not {title}"


def duplicate_tick(company, date):  # preflight.handoff rows
    return f"I know Apply for me handed an application to {company} to Greenhouse on {date} (it may not have gone through). Apply anyway."


def left_for_you(count):  # preflight.check
    return f". {count} more {'is' if count == 1 else 'are'} left for you to answer on the Greenhouse form"


def yours_to_answer(count):  # preflight.check
    return f"The app has everything it can fill. {count} question{'s are' if count != 1 else ' is'} yours to answer on the Greenhouse form"


def confirmed_by_email(day):  # runs.duplicate_block
    return f"Greenhouse already confirmed an application from you on {day}"


def released_job_ask(day):  # runs.duplicate_block
    return f"You said the attempt on {day} didn't go through. " "Greenhouse may still have it. Send it again anyway."


def other_copy(title):  # runs._other_copy
    return f"This Greenhouse job already has an attempt from another saved copy of the role ({title}). Finish or release that one first."


def late_confirmation(company):  # runs._after_missed_settle
    return f"Greenhouse showed its confirmation page for {company}, after this attempt was marked as not sent. Check it."


def result_submitted(title, company):  # runs.settle
    return f"Greenhouse showed its confirmation page for your application to {title} at {company}"


def email_after_release(company):  # watch._held_back
    return f"Greenhouse confirmed an application to {company} by email, after an attempt was marked as not sent. Check it."


def email_confirmed(company):  # watch._resolve_by_email
    return f"Greenhouse confirmed your application to {company} by email"


def confirmation_shown(ask):  # runner._summary, a submitted handoff
    return "Greenhouse showed its confirmation page. Mark as applied?" if ask else "Greenhouse showed its confirmation page."


def options_listed(count_words):  # runner._summary, a lookup
    return f"Greenhouse listed {count_words} for what you typed. Pick the one that is yours."


def measured(count_words, typed_words, listed_words):  # runner._measured
    text = (
        f"During the rehearsal the app blocked {count_words} that could have submitted the form or carried a filled-in answer, "
        "to Greenhouse or anywhere else. Sensitive answers were not put in the page; they go in only if you choose Finish in browser, "
        "before you press Submit application."
    )
    if typed_words:
        text += f" To find the options for {typed_words}, the app sent the text typed into those fields to Greenhouse's lookup service."
    if listed_words:
        text += f" For {listed_words}, the app fetched Greenhouse's whole list; nothing you typed went with it."
    if typed_words or listed_words:
        text += " The app saw nothing else you entered leave the browser."
    return text


FEATURE_APPLY_AGENT = ("Fill a Greenhouse application from your confirmed facts and saved answers, show you the result, and send it only "
                       "when you press Submit")
