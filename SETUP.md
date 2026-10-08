# Setting up your own copy

This guide is written for a **coding agent** (Claude Code, Codex, OpenCode, and
so on) to follow with you. Open this folder in your agent and say:

> Set this pipeline up for me by following SETUP.md.

It works on Windows 10, Windows 11, macOS, and Linux. Nothing is shared with
the person who gave you the code: your copy has its own database, profile,
sources, and keys, and it runs only on your computer at `http://127.0.0.1:8765`.

Everything below that says "the agent" is an instruction to the agent.

---

## Rules for the agent

1. **Ask; don't assume.** The profile drives every score and eligibility check.
   Never guess the student's school, major, graduation year, work authorization,
   sponsorship needs, or availability. If they don't know or would rather not
   say, leave the field `null` or `[]`. The app shows that as unanswered, which
   is honest; a guess is not.
2. **Never invent sources.** Only add a job board that `pipeline.py discover-ats`
   confirmed, or that the student gave you a working link for. Token guessing
   produces impostors, such as `greenhouse/archer`, which is a veterinary clinic.
3. **Keep secrets out of the chat.** Never print `.env`, and never ask the
   student to paste an API key into the conversation. Have them run
   `python -m opportunity_app.setup set-key NAME` themselves, which reads the
   value with the input hidden. In Claude Code they can type
   `! python -m opportunity_app.setup set-key NAME`.
4. **Everything is optional except Python.** The pipeline runs on the public
   employer job boards with no keys at all. Offer each integration, explain what
   it unlocks, and let the student skip it. In particular, **Jev (TypeSafe) is
   waitlisted**. Skipping it loses only an optional second-opinion panel and
   Jev inbox suggestions, whose keyword-rule fallback keeps working.
5. **Stay on this machine.** Never bind the server beyond `127.0.0.1`, and never
   commit `.env`, `config/profile.json`, `config/sources.local.json`,
   `config/early_programs.local.json`, `config/resume.json`, or anything in
   `data/`. All of these are gitignored already.
6. **Personalize every feature.** Anything that depends on who the student is,
   such as their class year, field, school, programs, or the labels they see,
   comes from their own answers and research. Never copy another student's
   list or wording into this copy.

Use `python` below. On macOS or Linux, if `python` is missing, use `python3`.
On Windows, `py -3` also works.

---

## 1. Python and dependencies

Python 3.11 or newer is required; 3.12 or later is recommended. Check it:

```bash
python --version
```

If it's missing or too old, the student installs it from
<https://www.python.org/downloads/> (on macOS `brew install python` also
works). On Windows, tick **Add python.exe to PATH** in the installer.

Create a virtual environment in the project folder and install into it. The
launchers and scheduled jobs look for `.venv` first.

```bash
python -m venv .venv
# Windows:        .venv\Scripts\activate
# macOS / Linux:  source .venv/bin/activate
python -m pip install -r requirements-web.lock
```

If the lock file fails to install, which happens on Intel Macs because the
pinned `cryptography` has no wheel there, use the version ranges instead:

```bash
python -m pip install -r requirements-web.txt
```

From here on, run every command with the virtualenv active.

## 2. Create the local files

```bash
python -m opportunity_app.setup init
```

This creates:

- `.env`, with a fresh sign-in token and encryption keys;
- `config/profile.json`, from the empty template;
- `config/sources.local.json`, an empty overlay;
- the two SQLite databases in `data/`.
- a git setting that turns on `.githooks/`, which refuses any commit or push
  that repeats your name, contact details, or keys (see below).

It detects Claude Code or Codex CLI, and records whichever it finds for the
outreach deep search. If only Codex is installed, it warns that the deep search
and company research stay off until the student agrees to
`PIPELINE_OUTREACH_RESEARCH_ALLOW_CODEX=1` in `.env` (see step 6, AI). It is safe
to run again, and never overwrites anything already set.

## 3. Interview the student and write the profile

Ask these questions conversationally, a few at a time, then write the answers
into `config/profile.json`. The field names are the keys in
`config/profile.example.json`.

| Ask | Field | Notes |
| --- | --- | --- |
| Name, school, degree | `name`, `school`, `degree` | e.g. "B.S. Chemical Engineering". Start with the level (B.S., M.S., Ph.D., MBA): a posting whose title asks only for another level, such as "MS/PhD", scores lower. |
| How is their name written on an application? | `name_parts` | `{"first": ..., "last": ..., "preferred": ...}`. Apply for me (see 7c) types `first` and `last` into an employer's two name boxes, and `preferred` only where a form has a preferred-name box. It never splits a longer name itself, so ask for it whenever the name has more than two words. Editable on the Profile page as **Name for applications**. |
| Mailing address (optional) | `contact`: `address_line1`, `address_line2`, `city`, `state`, `postal_code`, `country` | Some company contact forms require an address box. The app types this into a form's address boxes only when the form requires one, only once the student has confirmed it, and nowhere else (never a home, permanent, nationality or birth box, nor any box on a form that asks for an address twice, such as one with a reference's address block): when a form requires the street address, the rest of the confirmed address goes into its other address boxes; a form that requires only a country, state, city or ZIP gets only that; with none on file such a form waits for the student. Ask whether they are happy for that, and if so have them enter it on the Profile page (**About you › Mailing address**) rather than writing the file, because saving the page is what confirms it. Never guess one from a résumé or a school. |
| Graduation year | `graduation_year` | a number |
| Words that name their field in a posting | `degree_keywords` | e.g. `["chemical engineering", "process engineering"]`. Postings that match rank higher. |
| Kinds of roles they want | `preferred_role_types` | from `internship`, `externship`, `co-op`, `research`, `part_time`, `early_career` |
| Terms they're available | `available_terms` | `["summer 2027", "fall 2027"]`, always "season year" |
| Hours a week during classes | `hours_per_week` | a number or `null` |
| Where they'd work | `regions` | see below |
| Remote OK? Would they relocate? | `remote_ok`, `willing_to_relocate` | `true`, `false`, or `null` for unsure |
| Tools and skills they can honestly claim | `skills` | factual only; put aspirations in interests |
| Fields that interest them | `interest_keywords` | e.g. `["batteries", "catalysis", "process control"]` |
| Titles to push down | `deprioritize_title_keywords` | disciplines they don't want, e.g. `["sales", "software"]` |
| Authorized to work in the US? US citizen? Need sponsorship? | `work_authorized_us`, `us_citizen`, `requires_sponsorship` | `true` / `false` / `null`. Never infer these. |
| Home during breaks and summers | `break_location` | "City, ST", or the name of one of their `regions`. Used only for the outreach "(live in …)" note (see below) |
| Pay expectations | `compensation_preferences` | `null`, or an object: `paid_only` (`true`, `false` or `null`), `minimum_hourly` (dollars an hour as a number, or `null`) and `currency` (text; pay is compared only when it is blank or `USD`). A string here is refused by the Profile page and ignored by the score. |
| How they open an email | `greeting_word`, `unnamed_greeting` | Their word before a name (`"Hi"`, `"Hello"`, `"Dear"`), and how they greet a shared inbox with no name: `"{company} team"`, `"there"`, or `"{company} hiring team"`. Drafts and contact changes use these; left out, they are `"Hi"` and `"{company} team"`. Editable later on the Profile page. |

**Regions** decide which locations score up. Each is a metro area with a
bonus. There is no geocoding: a region is the list of towns it covers, and a
town only matches when the posting also names one of the region's state
markers. That keeps "Dublin, Ireland" out of a Bay Area region.

```json
"regions": [
  {
    "name": "Atlanta",
    "radius": "close",
    "bonus": 15,
    "state_markers": ["ga", "georgia"],
    "aliases": ["metro atlanta"],
    "places": ["atlanta", "marietta", "decatur", "sandy springs", "alpharetta", "smyrna"]
  }
],
"out_of_region_penalty": 40
```

A region may also carry an optional `"phrase"`: how an outreach email should
name it when the name alone reads oddly, e.g. `"name": "NorCal", "phrase":
"Northern California"`. Without one, the email uses the name ("the Bay Area"
style for names ending in "Area").

**Home and outreach.** Every cold email to a company where the student lives
says so right after the school's name ("student at [school] (live in Seattle)"), and a
draft that leaves it out cannot be approved. When `break_location` falls in one
of their regions, the whole metro counts. Otherwise only the exact city and
state count, so a Bellevue company is not near a "Seattle, WA" home until the
student adds a Seattle region that lists Bellevue. A bare town with no state
("Portland") matches nothing, because it could be anywhere. When home and
school are in the same region, it says "(live in Seattle year-round)".

Build the `places` list from your own knowledge of the metro and confirm it
with the student. Use a bigger bonus for their first choice. If they are open
to anywhere, leave `regions` empty: nothing is then penalised for location.

Then check the file:

```bash
python -m opportunity_app.setup validate
```

Fix every error it reports. Missing fields are fine if the student chose not
to answer.

## 4. Sources for their field

`config/sources.json` is the shared catalog: about 125 verified employer
boards, leaning toward engineering, hardware, aerospace, and medical devices.
The student's own additions go in `config/sources.local.json`.
`config/sources.local.example.json` shows every option.

1. Ask which companies they'd most like to work for. Resolve them to boards:

   ```bash
   python pipeline.py discover-ats "Company One" "Company Two"
   ```

   It previews without writing. Check each hit, then rerun with `--write` to
   append the confirmed ones to `config/sources.local.json`. Companies that
   don't resolve go on the student's manual list instead.
2. If the shared catalog is far from their field, switch off whole parts of it
   with `"disabled_sources": ["Company Name", ...]`, or drop it entirely with
   `"include_base_catalog": false`.
3. Add their school's career portal, and anything else they check by hand, to
   `manual_check_sources`: name, URL, and how often to look.
4. For LinkedIn and Exa discovery (`agent_discovery`), set keywords and
   locations that fit their field and regions.
5. For the outreach deep search, you may replace a scope's brief under
   `outreach_scopes`. For example, name the incubators in their city under
   `local-accelerators`. Only name programs you have confirmed exist.

## 5. Programs for your stage

The **Programs** tab lists internships, research programs, scholarships, and
externships that take a student at *this* student's stage. Most postings
quietly assume a later class year, so these are worth finding by hand. Nothing
ships in the repo: the agent researches the list with the student and writes
it to `config/early_programs.local.json`, which is gitignored. Until that file
exists, the tab shows an empty state pointing here.

1. **Ask; don't infer.** Their current class year and the first term they
   could start. Their field, from `degree_keywords`. Which kinds they want:
   paid internships, research (such as NSF REUs), scholarships with
   internships, job shadowing or externships, and programs for particular
   groups. Include identity-restricted programs only if the student asks for
   them. Take citizenship and work authorization from the profile only. If
   they are `null`, keep a program that requires them and say so in
   `eligibility`.
2. **Research on the web.** Cover companies in their field, government and
   national labs, research programs, their own school's offerings (career
   center externships, undergraduate research, co-op rules), and cross-company
   programs. Include a program only if it names their class year or states no
   class-year limit. Confirm that on the host's own page. If that page won't
   open, include the program only as `unverified`.
3. **Write each entry honestly.** Record `evidence`:
   - `explicit` if the host names their class year;
   - `not_named` if no class-year limit is stated;
   - `unverified` if the official page could not be checked, with the actual
     source in `source_note`.

   Use only dates the host published (`YYYY-MM-DD`). Otherwise leave
   `deadline_on` as `null` and explain in `deadline_note`, for example
   "Rolling" or "2027 dates not posted". Never estimate a date, a pay figure,
   or eligibility. If a program is already filled for this cycle, say so in
   `closed_note`.
4. **Name the tab for them.** `label` is the short tab name, such as
   `"First-year"` or `"Sophomore"`. `audience` is the plural the labels use,
   such as `"first-years"`, so a badge reads "Names first-years".

   ```json
   {
     "checked_on": "2026-09-21",
     "label": "First-year",
     "audience": "first-years",
     "programs": [
       {
         "id": "short-unique-slug",
         "name": "Program name as the host writes it",
         "host": "Company, lab, or university",
         "url": "https://official page",
         "evidence": "explicit",
         "kind": "Paid internship",
         "sector": "Aerospace and defense",
         "eligibility": "Class year, majors, GPA, citizenship, as published",
         "pay": "As published, or leave empty",
         "opens_on": null,
         "deadline_on": "2026-12-13",
         "deadline_note": "",
         "source_note": "Official posting",
         "closed_note": "",
         "notes": ""
       }
     ]
   }
   ```

   Only `id`, `name`, `host`, `url`, and `evidence` are required. The full
   format is in `opportunity_app/opportunities/early_programs.py`.
5. **Check it:**

   ```bash
   python -m opportunity_app.setup programs
   ```

   Fix every problem it lists; an invalid entry is left out of the tab.
6. Walk the student through the list, soonest deadline first. The tab re-reads
   the file each time it opens, so edits need no restart. Offer to redo the
   research each application cycle and when their class year changes, and
   update `checked_on`.

## 6. Optional keys

Show the student what's configured and what each integration unlocks:

```bash
python -m opportunity_app.setup status
```

For each one they want, they run `python -m opportunity_app.setup set-key NAME`
themselves (rule 3). The details:

- **USAJOBS** (`USAJOBS_API_KEY`, `USAJOBS_CONTACT_EMAIL`): free. Afterwards,
  add `"usajobs:federal-engineering"` to `enabled_sources`.
- **Adzuna** (`ADZUNA_APP_ID`, `ADZUNA_APP_KEY`): free. Copy the adzuna entry
  from the example overlay, set their city, and set `"enabled": true`.
- **AI**: Claude Code or Codex CLI signed in on their own subscription covers
  everything: the deep search, company research, drafts, call prep, the
  follow-up reviewer, and the career agent. `ANTHROPIC_API_KEY` or `OPENAI_API_KEY` works too, pay per
  use. One is enough. Show them Outreach → Settings, where each AI feature has
  its own choice listing only what is set up; with two (say Claude Code and
  Codex), the follow-up reviewer picks the one that did not write the email.
  **Codex and the web**: Codex has no web-search-only mode. The one way to give it a
  web tool also leaves it a file-patching tool that a page it reads could steer
  (the sandbox blocks the write, but not testing what a local file contains). So
  the deep search, contact searches and company research use Codex only when the
  student has agreed to `PIPELINE_OUTREACH_RESEARCH_ALLOW_CODEX=1` in `.env`. Explain
  that in a sentence and ask; do not set it for them. Without it, Claude Code runs
  those searches when it is installed (and says so), and with only Codex they
  refuse. Drafts, reviews and the career agent run Codex with no tools at all and
  need nothing. Codex is started without `~/.codex/config.toml`, so its `model` and
  `model_reasoning_effort` are carried over by the app; `PIPELINE_CODEX_MODEL` and
  `PIPELINE_CODEX_REASONING_EFFORT` override them.
  **Who writes thank-yous after a decline** (`PIPELINE_OUTREACH_THANK_YOU_PROVIDER`)
  is there too; left on "Same as first-email drafts", the draft writer writes them.
- **Jev** (`TYPESAFE_API_KEY`): optional and waitlisted; skip it freely. If they
  set it, tell them Jev inbox suggestions are a separate switch under Outreach →
  Outreach settings, off until they turn it on, because it sends reply and email
  text to TypeSafe.
- **Gmail drafts**: the student needs their own Google Cloud OAuth client. Walk
  them through docs/guide/gmail.md → "Gmail drafts setup". They also set
  `PIPELINE_OUTREACH_ACCOUNT` to their address and `PIPELINE_OUTREACH_COMPOSE=gmail`.
  `PIPELINE_CONNECTION_KEY` was already generated in step 2.
  Once Gmail is connected the app catches bounces and logs replies on its own.
  Ask whether their Google Cloud project is still in **Testing** or was
  **published to production**. Google ends a Testing project's Gmail grant
  after about 7 days, so the app warns a day ahead that Gmail will likely ask
  them to reconnect; that is `PIPELINE_GMAIL_TOKEN_DAYS`, 7 by default. For a
  project published to production there is no such limit, so they set it to 0
  and the warning never shows: `python -m opportunity_app.setup set-key
  PIPELINE_GMAIL_TOKEN_DAYS`, then type `0`. The warning is only an estimate,
  and it retires itself once Gmail keeps answering past the date.
  Connect Gmail asks for **three** permissions: compose, read, and a third that
  Google words as "Read, compose, and send emails from your Gmail account".
  Tell the student to **tick it**; without it the app cannot label outreach.
  The app uses it to add one label to every outreach thread, the emails they
  send to companies (first emails included) and the replies, and
  nothing else: it never deletes, archives, moves, or marks mail read.
  **Outreach label step.** Ask the student what Gmail label they want on their
  outreach threads, sent emails and replies, or none. The default is `opportunities`; they set it under
  Outreach → Outreach settings → "Gmail label for replies" (empty turns it off;
  letters, digits, spaces, hyphens, underscores and slashes only).
  A connection made before this existed needs one **Reconnect Gmail** to add the
  permission. The app refuses a connection when Google signs in as an account
  other than `PIPELINE_OUTREACH_ACCOUNT`. Pausing automation pauses labelling.
  **The pipeline mailbox versus an AI harness's Gmail.** The pipeline mailbox is
  the account the app connected to. A Gmail tool their AI harness provides (for
  example a claude.ai connector) may be signed into a different account, so an
  agent must not use it to look at outreach mail. `scripts/pipeline_mailbox.py`
  (`whoami`, `search`, `thread`) reads the pipeline mailbox read-only, from the
  main checkout when run in a worktree. Tell the student that anything it prints
  goes into the agent's conversation and to that agent's model provider. In
  Claude Code, `.claude/hooks/mailbox-guard.mjs` reminds the agent once per
  session before a Gmail tool runs; it needs Node (`node --version`). Without
  Node the AGENTS.md rule is the only guard.
  Ask whether they want the **Automation** switches under Outreach → Outreach
  settings: writing drafts automatically, finding a new contact after a bounce,
  sending on the recipient's weekday morning, having a second model check
  each follow-up (any model set up here works; with only one, it says the
  reviewer is from the same company as the writer), and sending through
  contact forms.
  All are off until they turn them on. Every email still waits for their approval; the scheduling switch only
  changes when an approved, confirmed email goes out.
  **Contact forms** reach companies that publish no email: the crawl notes the
  form on the company's contact page, and the approved first email goes in
  through it as the student, filled only from their confirmed profile (name,
  and `PIPELINE_OUTREACH_ACCOUNT` as the reply address; school, phone, a
  link, and the mailing address from step 3 only when a form insists). It runs in Playwright's Chromium, so install
  it if this computer does not have it yet: `pip install -r requirements-optional.txt`
  then `python -m playwright install chromium`. Tell them it is their name on every
  form it sends, and that it sends only a draft they approved: the switch sends
  approved ones on its own; without it, the card's **Send through contact form**
  asks them to confirm first. A form wanting a picture CAPTCHA waits for them
  under **Finish in browser**.
- **Update applications from job emails** (optional). Ask whether they want the
  app to read job-system and assessment emails (Greenhouse, Lever, Workday,
  HackerRank and the like) and keep their applications up to date from them. It
  needs Gmail connected as above, with the read permission Connect Gmail asks for;
  a connection made before the app asked to read mail must be reconnected first.
  The switch is under Profile → Automation, and it is off until they choose.
  Walk them through it:
  - They start it in **Shadow**. For at least 48 hours it only logs what it
    would have done, under **Would have done**, and changes nothing. They mark
    each entry right or wrong. **On** unlocks only after 48 hours and at least
    five entries, every one reviewed and none marked wrong; a wrong mark means
    switching it off and back to shadow to start the 48 hours again.
  - When on, an email that clearly confirms an application, rejects it, or
    invites them to interview moves the application forward on its own (never
    backward, and never over a change they made after the email), and adds a
    task or a deadline. Each change shows on the application's timeline with
    Undo, and the email is listed on the application. An application they
    archived stays archived; one the app archived itself after no reply
    (**Archive applications that never answered**) is reopened by an email
    about it that the archive did not know of.
  - Anything unclear waits under **Waiting for you**, with the reason and a
    picker to choose the right application: an offer (always), an email that
    could be about two applications, a forwarded email, a newsletter, or an
    email from a company's own domain until they trust that domain under
    **Trusted company mail domains** (suggested from their job links and
    outreach records; nothing is trusted without their click). **Stop
    trusting** puts a domain back to a suggestion, so its mail still only
    proposes; **Dismiss** stops the app reading that domain's mail at all.
  - The first time it runs it also looks back 60 days. What it finds there
    only ever waits for them ("Found 14 updates from the last 60 days").
    **Approve all** approves only those that waited just because they arrived
    before the switch was on; an offer, a sender Gmail could not verify, a
    guessed application, or a role not in their tracker stays for one by one.
  - Email excerpts kept as evidence are dropped after 180 days
    (`PIPELINE_MAIL_EVIDENCE_DAYS`). With Jev inbox suggestions on, the text of
    these emails goes to TypeSafe too, and Jev's answer acts on its own only
    when the keyword rules agree with it.
- **Send a thank-you when someone declines** (optional; needs Jev). Ask whether
  they want the app to answer a plain "no" to their cold outreach with a short
  thank-you, sent on its own in the same thread. It is the one email the app
  sends without their approval, so explain it before they choose:
  - It needs Jev inbox suggestions on (and so `TYPESAFE_API_KEY`),
    `PIPELINE_OUTREACH_ACCOUNT` set to the Gmail address they send from (the
    switch cannot be turned on without it, and a scheduled one is held if it
    is cleared), Gmail connected with read access, and a model set up to
    review it (the AI step above; Outreach → Settings → **Who reviews
    follow-ups and thank-yous**).
    Without a reviewer every thank-you is held on the card for them. It acts
    only when both the keyword rules and Jev read the reply as a plain decline;
    with Jev off, paused, or unavailable nothing goes, and turning Jev or the
    switch off holds one already scheduled for them to send or dismiss. A reply about a call, an offer, a question, a referral, or
    "maybe later" is always left for them, and so is a rejection from a job
    system (those come from no-reply addresses). The check fails closed: a
    reply that says anything beyond a stock "no" with thanks and good wishes,
    a thread where anyone there said more than no, or an answer typed into
    the quoted email is left for them too, so some plain declines will still
    be theirs to answer.
  - It also reads the reply's own email headers, and leaves it for them unless
    all of these hold: it is in the thread of their email or from the address
    they wrote to (not just someone at the company), it was found within a day
    of arriving, it was addressed to them in To or Cc (so check that
    `PIPELINE_OUTREACH_ACCOUNT` is their address; a Bcc'd blast never counts),
    a person wrote it (no auto-reply or mailing-list headers), no job system,
    job board or applicant-tracking system sent, relayed, signed or linked it
    (a link from their own email, quoted back, does not count), it came from
    one person rather than a shared inbox (careers@, recruitingteam@, info@,
    the company's own name), and Gmail's own sender check passed. Headers or
    links it cannot read leave it for them too. These are read again just before it goes; one that
    no longer passes is not sent, and the card says why in plain words ("Not
    thanked automatically: sent by an automated system").
  - A decline that arrives before 5 PM on a weekday in the recipient's time
    zone is answered after a normal delay the same day; otherwise the next
    weekday morning. There is no shadow period: the switch under Profile →
    Automation is off until they turn it on, and it cannot be turned on while
    Jev is off.
  - The words are theirs: it greets the person who wrote with the greeting from
    step 3 (`greeting_word`, `unnamed_greeting`) and signs with their confirmed
    name. Plain rules refuse anything but thanks (no question, no ask, no
    number, no dash), and a second model reads it just before it goes; anything
    unclear holds it on the card with **Send it anyway** and **Dismiss**. While
    it waits the card offers **Cancel** and **Edit** (which puts it in their
    Gmail Drafts instead). Pausing automation holds it, and a new message from
    the contact, or one of theirs to the contact, stops it.

## 7. Resume (optional, recommended)

Copy `config/resume.example.json` to `config/resume.json` and fill it in from
the student's resume, if they share one. Copy facts exactly; never embellish.
The resume and cover-letter commands only reformulate what is in that file.

**Résumé variants (optional).** Ask: *"Which kinds of roles do you apply to?
Do you keep a different résumé for each?"* Many students keep two or three
they designed themselves, one per field (for example one for hardware roles
and one for software roles). If they do:

1. They upload each one on the Profile page, under Resume versions, then type
   a label for it and press **Use as a variant**. That confirms it as a
   document to send without copying anything into the profile; profile facts
   still come from the one résumé confirmed normally.
2. Ask for the words that mark each kind of posting, and write them into
   `config/profile.json` with the same labels:

```json
"resume_variants": [
  {"label": "Hardware", "keywords": ["CAD", "PCB", "embedded", "mechanical"]},
  {"label": "Software", "keywords": ["Python", "React", "backend", "APIs"]}
],
"default_variant": "Software"
```

Labels match the ones typed in the app, ignoring case and spaces at the ends.
The switch can be turned on only once at least one listed label is on a
confirmed résumé; the Profile page says which labels are ready and which are
not listed. With the **Pick the résumé variant for each saved role** switch on
(Profile › Automation), every role they save from then on gets the variant whose words the posting
names most: a word in the title counts three times, one in the description
once, and the winner needs at least 2 points and 1.5 times the next variant.
Otherwise the pick is marked unsure and `default_variant` is used. The role
shows the pick, which they can change with one click, and the browser
extension preselects it. Nothing is rewritten. Leave `resume_variants` empty
(the default) if they keep one résumé; the app then uses the confirmed résumé,
as before.

**What the app may do on its own.** Every switch under Profile › Automation is
off until the student turns it on, and each change can be undone. A few read
settings from `config/profile.json`; ask before setting them:

| Ask | Field | Notes |
| --- | --- | --- |
| After how many days with no reply should an application show up in Urgent? | `application_follow_up_days` | default 21; used by **Flag applications with no reply** |
| After how many days should a silent application be archived? | `archive_after_days` | default 60; used by **Archive applications that never answered** |
| From what score should new roles be saved for you? Below what score passed? | `automation.auto_save_at`, `automation.auto_pass_below` | scores from 0 to 100, with no defaults: left out, the switch can't be turned on. Keep `auto_pass_below` at or under `auto_save_at`. |

With **Update applications from job emails** on (not in shadow), a job email
linked to an application that says something happened (a confirmation, an
interview invite, a scheduling link, an assessment, a deadline, a rejection or
an offer) counts as a reply: both day counts above start again from the latest
one. A job alert or newsletter does not count, nor does an email whose changes
the student turned down or ignored. An application the app archived after no
reply is reopened by a job email about it that the archive did not know of: an
interview invite moves it to Interview, a rejection to Rejected, and a
confirmation, assessment or scheduling email back to Applied, even when that
email reached Gmail before the archive and was read later. The follow-up
reminder the archive cancelled comes back with it. If the email names only the
company and the student has another open application there, it waits for them
to pick. One the student archived is never reopened, and one with a job email's
change waiting for their approval is not archived at all. An archive the
student undoes stays undone until a new job email or a new applied date; one a
job email reopened is archived again only after the full count of silent days
from that email.

Auto-save and auto-pass run after each daily sync, only on roles first seen
since the switch was turned on, and never pass a posting that has no
description. Known limit: the daily sync keeps scores only for the main account
on the computer, so these two switches work only for that account. Roles passed
this way are listed for a week under Auto-passed this week, each with Restore.

```json
"application_follow_up_days": 21,
"archive_after_days": 60,
"automation": {"auto_save_at": 85, "auto_pass_below": 30}
```

Run `python -m opportunity_app.setup validate` again after editing; it checks
these fields too.

## 7b. Call prep (optional)

When a company replies, call prep researches the company and the person the
student will talk to, and writes questions meant to get that person talking
about their own work. Two things make it the student's own.

**Their standing questions.** Ask: *"What do you want to ask on every call?
Anything from your own work you'd lead with?"* Copy
`config/call_prep.local.example.json` to `config/call_prep.local.json` and write
their questions in their words: `ask` is the question, `lead_in` (optional) is
what they say first, `research` names the research to have ready for it
(`customers`, `product`, `growth`, `hiring`, `engineering`, and the other
sections in `opportunity_app/outreach/quote_check.py`), and `blank` is a line to
write the answer on. The file is gitignored. Without it, call prep asks four
plain questions with no lead-ins.

**LinkedIn, for notes on the interviewer.** Optional, and only with a separate
LinkedIn test account, never the one in their everyday browser. The interviewer
is found from their outreach inbox (who sent the calendar invitation, or who
wrote last); their profile is then read through `mcp-server-linkedin` run by
`mcporter`, signed in as the test account. Set it up with the student at the
keyboard (they type the password; never ask for it):

```bash
npm install -g mcporter
mcporter config add linkedin-scraper --scope home --stdio "uvx mcp-server-linkedin==4.26.1 --no-auto-import" --env AUTO_IMPORT_FROM_BROWSER=false
AUTO_IMPORT_FROM_BROWSER=false uvx mcp-server-linkedin==4.26.1 --login --no-auto-import
```

The version is pinned on purpose: `@latest` would run whatever was published
last, with the LinkedIn sign-in in reach, every time it starts. Use the same
number in both commands. To upgrade, choose the new version yourself after
reading its release notes, then run `mcporter config remove linkedin-scraper`
and both commands again with it. `--scope home` keeps the entry in mcporter's
own folder in the home directory, so no `config/mcporter.json` appears in the
project (it would be committed by accident).

Both `--no-auto-import` and `AUTO_IMPORT_FROM_BROWSER=false` must stay: without
them the server copies the browser's LinkedIn sign-in. Then, in Outreach →
Settings, put the test account's profile link under *LinkedIn test account*.
Before every read the app checks that the server is signed in as exactly that
account with browser import off, and reads nothing otherwise. It only reads, a
few calls per interviewer, spaced 20 seconds apart. If `mcporter` is not on the
web app's PATH, set `PIPELINE_MCPORTER` in `.env` to its full path.

## 7c. Apply for me (optional)

**Apply for me** reads a saved Greenhouse role's public application form and
shows the student what it would fill from their confirmed facts and saved
answers, and which questions it cannot answer yet. The student answers a
missing question once, on the role, and it is saved for that company only: Apply
for me never carries an answer from one company to another, so there is no "use
for any company" tick there. A **rehearsal** opens a Chromium window and fills the
form to check it, sends nothing (the app blocks every request that could submit
the form), and takes a picture of the filled form with the sensitive fields
covered; the pictures are kept 90 days. A form that does not look like the saved
role (another company or title) is rehearsed, or filled for **Finish in browser**,
only after the student ticks "This is the right posting". Tell the student to turn
a VPN off before a rehearsal or Finish in browser, since a form can refuse a visit
that comes through one. Every application goes out under the student's own name and
is their own act: the app only fills the form, and they press Submit. It is off
until they turn it on under Profile › Automation. Ask before turning it on for
them, and set up these things with the student:

1. **Their name on an application.** Ask how they write it and set
   `name_parts` (see step 3), or fill in **Name for applications** on the
   Profile page. A confirmed name of exactly two words works without it; a
   longer name does not, on purpose, and the switch says so until it is set.
2. **A confirmed email and a confirmed résumé** (step 7). The email is
   `contact.email`, confirmed on the Profile page in **Email for applications**
   (the phone box under it is optional).
3. **Playwright and Chromium**, the same install as PDF export and contact
   forms: `python -m playwright install chromium`. On Linux the app also needs
   a display: run `systemctl --user import-environment DISPLAY WAYLAND_DISPLAY`
   and restart the dashboard. The switch names whichever of these is missing.
4. **Limits (optional).** The defaults are cautious: an employer can mark an
   applicant as spam for good, so the app spaces applications out and applies to
   one company at most once a month. Change one only if the student asks. Write
   the ones they want under `apply_agent` in `config/profile.json`, each a whole
   number:

| Ask | Field | Default |
| --- | --- | --- |
| Minutes between two applications | `apply_agent.spacing_minutes` | 10 |
| Applications a day | `apply_agent.daily_cap` | 5 |
| Days before applying again to the same company | `apply_agent.company_days` | 30 |
| Rehearsals and option lookups a day | `apply_agent.rehearsals_per_day` | 20 |
| Clean rehearsals at different companies before a one click submit | `apply_agent.rehearsals_before_submit` | 3 |

```json
"apply_agent": {"daily_cap": 3, "company_days": 60}
```

The limits in force are listed, read only, under Profile › Automation › Apply
for me settings. That page also keeps the **exact options** the student picks
for lists only the form knows (school, location, degree): the app uses such an
option word for word and never guesses one. Screenshots of a filled form
are deleted after 90 days
(`PIPELINE_APPLY_EVIDENCE_DAYS` in `.env` changes that). Run
`python -m opportunity_app.setup validate` after editing; it checks these
fields too.

5. **Sensitive answers (optional; ask, never decide for them).** Questions about
   work authorization, sponsorship, demographics, consent and salary are never
   answered from the profile or the saved answers. The app lists them and leaves
   them for the student. A student who wants the app to type a few of them can
   allow that under Profile › Automation › Apply for me settings › **Answers for
   sensitive questions**. Ask which kinds they want, if any: work authorization,
   visa sponsorship, 18 or older, voluntary self-identification (EEO), legal
   acknowledgments, data-processing consents. Nothing is on until they switch it
   on. Then they add each answer themselves, either there (the question exactly
   as the form shows it) or on a role, where the app lists the question and the
   form's own options. Say plainly:

   - each answer needs their own tick on the wording that it is used only to
     fill in application forms, and it is kept with the time they ticked it;
   - for voluntary self-identification the app keeps only a decline answer such
     as "Decline To Self Identify", never a real one, and a form that words the
     decline differently needs its own entry;
   - a legal acknowledgment or consent is ticked only when the form's statement
     (its heading, its option and any description under it) is word for word the
     stored one and links the same documents. Every such statement is saved for
     one company only, however it is worded, because no list of words can prove
     that a statement names no document (a plain "I certify that the information I
     have provided is accurate" is one company's too), and a short option ("I
     agree") or a pointer to "the above terms" is also filed under the question
     above it. So is any question that depends on the company ("this company", a
     follow-up, a bare heading). A Yes/No question that asks
     for agreement is matched the same way, on its question (with the question
     above it when it is short or a follow-up) and its description, and only a
     box or a Yes/No question is ever ticked: a text field or a list of several
     options is left for them. A statement of fewer than three words is left
     for them too;
   - a box that states a fact about them (work authorization, sponsorship, 18 or
     older) is matched on its heading too, since the heading is the question, and
     an answer added in settings for such a box is stored as ticked when they
     tell the form it is a tick box. A tick box, a typed answer, and a choice
     that also agrees to something are saved for one company only; only a choice
     from the form's own option list ("Yes", "No") for those three kinds, and an
     EEO decline, may be kept for any company;
   - a box, a group of boxes, a choice whose options or heading agree to
     something and a typed signature or initials are never filled from the
     ordinary saved answers, at any
     company. Only a stored statement that matches word for word ticks them;
   - export control, citizenship, security clearance and salary questions, and
     personal ones such as age or birth date, are never answered.

   Nothing about their situation is written into the app: the kinds, the
   answers and the consent all live in their own copy. It works from the
   student's browser session: a request that sends the access token as an
   Authorization header is refused, and a write needs the session's CSRF header.
   That is a guard against a stray script, not a lock against anyone who holds
   the access token (it can sign in a browser session), and the account export
   includes the stored answers. Say so if they share the machine or the token.

6. **Finish in browser.** On a saved role a **Finish in browser** button sits next to
   the rehearsal. It opens a Chromium window and fills the form; what the app
   cannot fill (a cover letter with none approved, any CAPTCHA box, a field it could not read back)
   is listed as **Left for you**, and every consent box it ticked is listed with
   the addresses the statement links to. If the student has approved a cover letter
   for the role, the app attaches that one to the form's cover letter field after a
   last check that it is still the latest approved version. The student finishes the form in the
   window and presses **Submit application** there themselves. Tell them: the
   application is not sent until they press it (to find the options for typeahead
   fields such as location and school, the app sends the text typed there to
   Greenhouse's lookup service, and it does not watch what they type in the window
   themselves); **Stop**, closing the window, or 20
   minutes without a press closes it and sends nothing; if the window doesn't
   come forward, click Chromium in the taskbar; and the app never moves the
   tracker by itself: when Greenhouse shows its confirmation page the card asks
   **Mark as applied?**, and only their click does it. If Greenhouse asks for its
   emailed security code, the app reads it from Gmail (read-only, after they
   press Submit) and types it into the window, and they press Submit again; that
   needs the email on their applications to be the Gmail account the app reads
   (Profile › Email for applications), otherwise they type the code themselves.
   The confirmation-email watch is optional for Finish in browser: without Gmail
   connected the card says the app isn't checking for a confirmation email.
   Pausing automation does not close a window they opened; Stop does.

## 8. First run and daily use

```bash
python pipeline.py doctor                   # confirms the profile and keys
python pipeline.py run                      # fetch, score, write the shortlist (a few minutes)
python -m opportunity_app.migrate           # load the results into the web app
python -m opportunity_app.launch open       # opens the dashboard, already signed in
```

To keep it running and fresh without thinking about it:

```bash
python -m opportunity_app.launch install-autostart   # server starts at login
python -m opportunity_app.launch install-daily       # fetch every morning, resumes after sleep
python -m opportunity_app.launch install-outreach    # optional: Mon/Thu deep search (needs Claude Code or Codex)
```

These use Task Scheduler on Windows, launchd on macOS, and systemd on Linux.
`python -m opportunity_app.launch uninstall` removes them all.

After that, the student opens the app with **`Open Pipeline.vbs`** (Windows) or
**`Open Pipeline.command`** (macOS). Either can be double-clicked or pinned, and
both sign in automatically, with no token to copy. The first time on macOS,
right-click the `.command` file and choose **Open**.

Ask the student whether anyone else uses this computer. If nobody does and they
would rather open the app from a bookmark too, run
`echo 1 | python -m opportunity_app.setup set-key PIPELINE_SKIP_SIGN_IN`, then
`python -m opportunity_app.launch restart`. Leave it unset on a shared computer
([The web app](docs/guide/web-app.md) explains the trade).

## 9. Updating later

```bash
git pull
python -m pip install -r requirements-web.lock
python -m opportunity_app.setup init        # adds any new settings; keeps everything else
python -m opportunity_app.launch restart
```

Personal files are gitignored, so a pull never touches them. If an update
brings a feature that needs the student's own data, its step above says so;
`python -m opportunity_app.setup status` lists what is still missing.

If you commit changes of your own, the hooks from step 2 check each commit and
push against your resume, profile, and `.env`, and refuse if any of it would be
published. Add employers, contacts, or anything else to keep private, one per
line, in `private/blocked-terms.txt`.

The hooks also refuse commits that write your school or degree from
`config/profile.json` into the code and setup docs every student runs, because
each copy personalizes those from the student's own config. List short forms,
such as your school's abbreviation or a campus program, one per line in
`private/situation-terms.txt`. Tests, fixtures, and the shared source catalog
may still use them. Audit the whole history at any time with
`python scripts/check_personal_data.py --all`.

## Troubleshooting

| Symptom | Fix |
| --- | --- |
| `Missing config/profile.json` | `python -m opportunity_app.setup init` |
| Browser shows the sign-in page | Open the app through the launcher, not a bookmark. Or paste `PIPELINE_WEB_TOKEN` from `.env` into **Owner token**. On a computer nobody else uses, `PIPELINE_SKIP_SIGN_IN=1` turns sign-in off. |
| "did not accept the token in .env" | The running server was started with another token: `python -m opportunity_app.launch restart` |
| A source errors every run | `python -m opportunity_app.setup status` shows sources that need keys; add the key or disable the source. |
| Nothing scores high | Check `regions`, `degree_keywords`, and `interest_keywords`. Every score comes with its reasons in the dashboard. |
