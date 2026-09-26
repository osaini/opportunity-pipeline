# Opportunity Pipeline

A local, inspectable pipeline for finding and tracking internships, externships,
co-ops, undergraduate research, and part-time work. Every opportunity retains its
source URL, fetch time, and score explanation. It never auto-applies.

## Quick start

**Setting up your own copy?** Open this folder in your coding agent and ask it
to follow [`SETUP.md`](SETUP.md). It interviews you for your profile, sets up
your sources and optional keys, and leaves you with a one-click launcher. That
works on Windows, macOS, and Linux.

The command-line pipeline alone needs only Python 3.10+ and no third-party
packages:

```bash
python -m opportunity_app.setup init --no-migrate   # or copy config/profile.example.json to config/profile.json
python3 pipeline.py doctor
python3 pipeline.py run
open output/shortlist.md
open output/dashboard.html
```

`run` fetches public employer job-board APIs, imports any rows in
`data/manual_jobs.csv`, recomputes scores, and writes the Markdown/CSV
shortlists plus a sortable/filterable `output/dashboard.html`. The first
network run can take a minute because each matching posting is retrieved
from its official job-board API.

## Unified dashboard

`output/dashboard.html` is a single self-contained file — no server, no
external dependencies, works fully offline. Open it directly in a browser
(Chrome or Firefox recommended) after any `report` or `run`. It supports:

- Sorting by score, company, most recently posted, or most recently discovered
- Filtering by region, role type, status, and source, plus free-text search
- Stat tiles for how many postings are showing, how many are new, how many sit
  in a target region, and the best score in the current slice — all of which
  respond to the active filters
- A region chip on every posting (`Austin`, `Bay Area`, `Remote`, `Other`,
  `Unknown`). Each region keeps its colour no matter how you filter or sort,
  and the chip always carries its text label, so the colour is never the only
  thing distinguishing one region from another.
- The same "why ranked here" score transparency as the Markdown report
- **New/read tracking**: a listing is badged `NEW` if it wasn't there the
  last time you opened the dashboard; clicking a listing marks it read.
  This is tracked client-side via `localStorage` in your browser, keyed to
  the file's path — it persists across pipeline reruns as long as you keep
  opening the same `output/dashboard.html` in the same browser. On the very
  first-ever open, nothing is flagged NEW (flagging your whole existing
  backlog as new on day one isn't a meaningful signal). If your browser
  blocks `localStorage` for local files (some strict Safari privacy
  settings do this), the dashboard still works — new/read status just
  won't persist between opens.
- Control the number of postings included with `--dashboard-limit` (default
  300) on `report`/`run`. Unlike the Markdown shortlist, the dashboard does
  not exclude rejected/withdrawn postings — it's meant as a fuller
  "everything I've seen" view, with status as a filter rather than a
  pre-filter.

## Full opportunity platform

The repository also implements a clean-room, UTern-inspired
product: a separate product database, versioned role-scoped API, responsive student
workspace, employer/admin workspaces, durable worker, and Apply Mode extension.
The original CLI remains dependency-free and continues to own ingestion and scoring.

Install the web dependencies, create the local files, and open the app:

```bash
python -m pip install -r requirements-web.lock
python -m opportunity_app.setup init
python -m opportunity_app.launch open
```

`launch open` starts the server in the background if it isn't running, then
opens your browser already signed in. It trades `PIPELINE_WEB_TOKEN` from `.env`
for a one-time ticket, so there is no token to copy; the session lasts 30 days.
`launch stop`, `restart`, and `status` manage the server, and
`launch install-autostart` starts it at login on Windows, macOS, or Linux.
Running `python -m opportunity_app.api` directly still works, and prints the
token for the sign-in page's **Owner token** field.

Important properties:

- `data/pipeline.db` is opened read-only and is never changed by the migration.
- Migrated data is written to gitignored `data/platform.db`.
- The migration is idempotent and checks active-unique counts plus the first 200
  ranked IDs against the legacy pipeline before it reports success.
- `/api/v1/*` data routes require the local access token or the signed session
  cookie created by the login screen.
- The web UI reads, filters, and searches opportunities; displays source and fit
  evidence; persists reversible save/pass actions; records an explicit Apply
  click without submitting the employer form; and provides an application
  tracker with audited stage changes.
- Profile includes onboarding completeness, editable goals/availability/location/
  compensation/authorization fields, and explicit confirmation records. Use
  **Export scoring profile** to download the exact JSON schema accepted by the
  legacy deterministic scorer.
- Resume ingestion accepts content-sniffed PDF/DOCX files up to 5 MB. Files are
  kept in gitignored private storage, scanned for test malware and active or
  embedded document payloads, parsed into drafts, and never copied into the
  profile until individual suggestions are selected and confirmed. Original
  files can be downloaded or permanently deleted from Profile.
- Rerunning the legacy migration refreshes opportunities but never overwrites a
  profile already edited in the web app or its confirmed-fact provenance.
- Preparation includes evidence-linked resume/cover-letter versions, reusable
  answers, and typed or visibly transcribed mock interviews. The student agent
  uses durable threads, auditable tools, budgets, abstention, and explicit
  approval cards for consequential actions.
- Apply Mode in `apps/extension` uses transient `activeTab` access, inventories
  Workday/Greenhouse/Lever/Ashby/SmartRecruiters/generic fields, requires review
  for sensitive fields, and has no final-submit capability.
- Connections default to sandbox suppression. Optional Google/Microsoft OAuth
  uses PKCE, least-privilege read scopes, encrypted tokens, signed webhooks,
  preview/confirm tracker updates, quiet hours, verified phone state, and STOP.
- The private career dossier has classified evidence, pause/retention/export/
  deletion controls, granular expiring shares, revocation, and access logs.
- Market issues use hashed dated snapshots and an editorial publish gate.
- Outreach tracks cold outreach to companies with no posting (startup incubator
  portfolios, student accelerators). Targets live in their own tables, never in
  `opportunities` or `applications`, so they cannot pass as sourced postings.
  Each keeps its research source URLs, research date, and a contact confidence
  label (confirmed, unverified, unknown). Import CSV or JSON adds new companies
  and never overwrites existing ones (a matching name or website domain counts
  as existing); `data/outreach-*.json` is gitignored for personal research lists.
  See [Cold outreach pipeline](#cold-outreach-pipeline).
- `/employer` and `/admin` use separate role tokens. Employer evidence access is
  consent-gated, protected/proxy rubric criteria are rejected, agent summaries
  cannot decide, and recruiter messages require approval. Admin controls expose
  verification, moderation, source health/control, flags, queue state, metrics,
  aggregate school reporting, and audit history.
- Account registration is invitation-gated; email/password login and sandbox
  recovery are available. Full account export and confirmed deletion are API
  operations, with private-file download/delete kept explicit.
- Interactive API documentation is available at `http://127.0.0.1:8765/docs`;
  the data endpoints still require authorization.

### Enable the AI career agent

The Agent tab supports OpenAI and Anthropic through the same audited tool and
approval runtime. Add either provider to the gitignored `.env` file, restart the
server, and select it when starting a thread:

```dotenv
OPENAI_API_KEY=your-key
OPENAI_AGENT_MODEL=gpt-5.4

# Or:
ANTHROPIC_API_KEY=your-key
ANTHROPIC_AGENT_MODEL=claude-sonnet-5
```

API keys remain server-side. The browser receives only provider/model names and
configuration status. Read-only questions can search the student's opportunities,
profile, applications, deadlines, and tasks. Saves, stage changes, new tasks, and
preparation documents appear as approval cards and execute only after approval.
Every model turn and tool run records its provider, model, status, token usage,
and request identifier. The Prepare tab can also use a configured provider for a
draft, while the no-AI grounded template remains the default.

### Enable on-demand Jev reviews

[TypeSafe Jev](https://docs.typesafe.ai/introduction) is used as a narrow
decision model, not as another career agent or a replacement scorer. Add the
credential to `.env` and restart the server:

```dotenv
TYPESAFE_API_KEY=your-key
TYPESAFE_MODEL=jev-1.13.0
```

Opportunity details then offer **Run Jev review**. One batched request evaluates
role and skill alignment, education, experience, authorization, and term
compatibility, plus deadline and compensation clarity. The result exposes each
choice or score, its probability distribution, confidence, resolved model, and
token usage. It is visibly labeled an unconfirmed AI suggestion, is not stored,
and never changes the deterministic 0–100 score or performs an action.

The click is also the privacy boundary: no TypeSafe request happens in the
background. The request includes the posting fields and only a fixed set of
non-contact profile fields needed for matching. It excludes the student's name,
school, contact details, notes, application history, and resume prose. See
[`docs/typesafe-jev.md`](docs/typesafe-jev.md) for the full contract, limitations,
and test strategy.

**Jev inbox suggestions** are a separate, per-student switch under Outreach →
Outreach settings, off by default. When on, the text of a reply you paste and of
an application email a connector delivers is sent to Jev to suggest its outcome.
Without a key, with the switch off, when TypeSafe errors, or when Jev is less than
50% sure, the keyword rules suggest instead, and each suggestion says which one
made it. Either way you confirm every change.

Keep the direct development server bound to `127.0.0.1`. For hosted use, the API
supports `DATABASE_URL=postgresql://...`, HTTPS-only cookies in
`PIPELINE_ENV=production`, a separately deployed worker, and the container stack
in `infra/docker-compose.yml`. See `docs/THREAT_MODEL.md`, `docs/RUNBOOK.md`, and
`docs/PRIVACY_ACCESSIBILITY.md` before enabling live provider credentials.

Run the worker and encrypted backup drill locally with:

```powershell
py -3 -m opportunity_app.worker --once
py -3 -m opportunity_app.ops_cli backup backups/platform.enc
py -3 -m opportunity_app.ops_cli drill backups/platform.enc
```

### Keep the local web dashboard running

On macOS, double-click **`Open Pipeline.command`**; the first time, right-click
it and choose **Open**. On any system,
`python -m opportunity_app.launch install-autostart` starts the server at login:
a launchd agent on macOS, a systemd user unit on Linux, and the scheduled task
below on Windows.

On Windows, the easiest option is to double-click **`Open Pipeline.vbs`** in the project
folder. It runs without a terminal window, installs or starts the private
per-user background task as needed, waits for `http://127.0.0.1:8765` to become
healthy, and opens the dashboard in your default browser, signed in. You can create a
normal Windows shortcut to that file and pin the shortcut wherever convenient.

The first launch performs the same one-time setup as the command below. Later
launches simply confirm the background service is running and open the page.

Install the per-user Scheduled Task once from the project root:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\install-web-task.ps1
```

The task starts the dashboard invisibly at sign-in, checks every minute that it
is still running, restarts it up to three times after a failure, and keeps
stable private role tokens in the gitignored `.env` file. Open
`http://127.0.0.1:8765` at any time while this computer is on and the user is
signed in. Server output is appended to `data/web.log`.

Manage it with:

```powershell
Get-ScheduledTask -TaskName internship-pipeline-web
Stop-ScheduledTask -TaskName internship-pipeline-web
Start-ScheduledTask -TaskName internship-pipeline-web
Unregister-ScheduledTask -TaskName internship-pipeline-web
```

## Personalize ranking and eligibility review

Edit `config/profile.json` (gitignored; setup creates it from
`config/profile.example.json`, which starts empty). These eight answers
materially improve ranking, eligibility checks, or the decisions you make from
the shortlist:

1. What year do you graduate?
2. Which terms are you available: fall, spring, summer, and what year?
3. Which locations are acceptable, and will you relocate?
4. How many hours per week can you work during classes?
5. What tools and methods can you honestly claim (for example Python, MATLAB,
   CAD, lab techniques, statistics, a framework you have shipped with)?
6. Which fields interest you most, in your discipline's own terms (for example
   robotics, data, energy, biotech, finance, controls, product design)?
7. Are you authorized to work in the U.S., and will you require sponsorship?
8. Are unpaid or for-credit research/externships acceptable, or only paid work?

Keep `skills` factual. Put aspirations in `interest_keywords`.

### Target regions

`regions` in `config/profile.json` is what makes the pipeline geographically
picky. The template ships with none. For example, a region can target
**Austin on a close radius** (the metro and its immediate commuter towns) or the
**Bay Area on a medium-large radius** (the whole nine-county spread, Santa Rosa
and Gilroy included); `tests/fixtures/profile_student.json` has both.

There is no geocoding here, so a "radius" is just how long that region's
`places` list is — widen a region by adding towns, tighten it by removing them.
Each region takes:

| Field | Meaning |
|---|---|
| `name` | Label shown on the dashboard chip |
| `radius` | Free text, quoted back in the score explanation |
| `bonus` | Points added when a posting matches |
| `state_markers` | Required state tokens, e.g. `["ca", "california"]` |
| `places` | Cities/counties, matched only alongside a state marker |
| `aliases` | Phrases unambiguous on their own, e.g. `"bay area"` |

The state marker is load-bearing: Newark, Dublin, Richmond, Concord and
Berkeley all name a Bay Area city *and* a well-known city elsewhere, so a bare
city name is never enough on its own.

Anything that matches no region takes `out_of_region_penalty` (default 40),
which is heavy enough to sink it below every genuine match without hiding it —
scores clamp at 0, so out-of-area postings collect at the bottom rather than
disappearing. Remote roles still score `+8` while `remote_ok` is true. A
posting whose location names no place at all — blank, or a Workday-style
`"3 Locations"` placeholder — is left alone rather than penalised, since a
sparse location field is not evidence the role is elsewhere.

Delete `regions` entirely to fall back to the older, gentler
`preferred_locations` keyword scoring.

## Daily and weekly workflow

- Daily during peak recruiting: run `python3 pipeline.py run`, inspect the top
  ten, and verify the posting on the source page.
- Twice weekly: ask an agent session for an [agent-reach sweep](#agent-reached-channels)
  — LinkedIn and Exa for the `agent_discovery` queries, then
  `import-discovered` → `enrich` → `score` → `report`. Public ATS feeds in
  `ats_sources` are already covered by `run`, so this pass is for what those
  feeds miss.
- Twice weekly: check the login-only portals listed in your
  `manual_check_sources` (your school's career portal, Handshake, and so on).
  Copy good results into `data/manual_jobs.csv`. A school portal on public
  Workday can often be fetched automatically instead — see
  [Sources](#sources-and-boundaries).
- When a company keeps surfacing through `exa` or `linkedin`, promote it into
  `ats_sources` with its real ATS `kind`. A direct feed is cheaper and more
  complete than rediscovering it every sweep.
- Weekly: run `python3 pipeline.py liveness` so imported postings that have
  since closed stop occupying shortlist slots. ATS rows retire themselves; these
  don't. See [Checking whether a posting is still open](#checking-whether-a-posting-is-still-open).
- Weekly: check your school's undergraduate research listings and NSF REU,
  and contact one relevant lab at your school. Many research roles are
  relationship-driven rather than posted.
- Track movement with:

```bash
python3 pipeline.py update <ID> shortlisted
python3 pipeline.py update <ID> applied --follow-up 2026-08-05
python3 pipeline.py update <ID> interview --notes "Phone screen Aug 8"
python3 pipeline.py status
```

Valid statuses are `discovered`, `shortlisted`, `applying`, `applied`,
`interview`, `offer`, `rejected`, and `withdrawn`.

## Sources and boundaries

`config/sources.json` lists employers/APIs to fetch automatically, all public
and no-login. Add or disable entries there. Supported `kind` values and their
config fields:

| `kind` | Required fields | Notes |
|---|---|---|
| `greenhouse` | `token` | from an employer's Greenhouse job-board URL |
| `lever` | `site` (`region` optional) | from `jobs.lever.co/<site>` |
| `ashby` | `board` | from `jobs.ashbyhq.com/<board>` |
| `smartrecruiters` | `company_id` | from an employer's SmartRecruiters postings URL |
| `workday` | `tenant`, `datacenter`, `site` | an employer's own *public* Workday career site (e.g. `nvidia.wd5.myworkdayjobs.com/NVIDIAExternalCareerSite` → `tenant=nvidia, datacenter=wd5, site=NVIDIAExternalCareerSite`). |
| `usajobs` | none beyond credentials; see [Federal postings](#federal-postings-usajobs) | federal internships (DoD, DOE national labs, USACE, etc.) across every agency in one source. Needs a free API key. |
| `adzuna` | `queries`, each with `what`/`where`/`distance_km`; see [Aggregator postings](#aggregator-postings-adzuna) | aggregator reaching employers with no public ATS feed. Needs a free key pair. |

### API keys

Only two sources need a key; every other entry in `ats_sources` is public and
keyless. Copy `.env.example` to `.env` and fill in what you have. A missing key
is not fatal — that one source reports an error and every other source in the
run still works — and `doctor` tells you which are missing.

| Key | Free tier | Register at | What it buys |
|---|---|---|---|
| `USAJOBS_API_KEY` + `USAJOBS_CONTACT_EMAIL` | free | [developer.usajobs.gov](https://developer.usajobs.gov/apirequest/) | Every federal agency in one source: DoD labs, the DOE national labs, USACE. Note that **NASA is not among them** — see below. |
| `ADZUNA_APP_ID` + `ADZUNA_APP_KEY` | free, rate limited (~25 calls/min) | [developer.adzuna.com](https://developer.adzuna.com/) | The only source with a true **radius** filter, which is how you reach small local manufacturers, labs, and suppliers that never appear on a modern ATS. |

Sources that were probed and deliberately **not** adopted are recorded under
`rejected_sources` in `config/sources.json`, each with the measurement that
ruled it out, so the same ground isn't re-covered every few months.

**Verify a board's identity before adding it.** Guessing an ATS token produces
convincing impostors: `greenhouse/archer` is Archer Veterinary Clinic, not Archer
Aviation; `greenhouse/apex` is Apex Eye; `greenhouse/axiom` is a consultancy in
Atlanta and Belfast, not Axiom Space; `ashby/sierra` is Sierra AI, not Sierra
Space. Confirm from the board's own metadata or the locations on its postings —
`https://boards-api.greenhouse.io/v1/boards/<token>` returns the company `name`
directly — rather than trusting that the endpoint returned JSON.

`discovery_title_terms` are matched as **stems on word boundaries**, so `intern`
matches "Intern", "Interns", and "Internship" but not "Internal Medicine". Bare
substring matching had been pulling in every internal-medicine physician role
the VA posts; that was invisible on employer boards and swamped USAJOBS.

`pipeline.py` itself never authenticates to anything, submits an application, or
guesses a missing fact, and it still has no third-party dependencies. Stale and
inactive postings remain in the database for history but disappear from the
active shortlist.

### Federal postings (USAJOBS)

USAJOBS is one source covering every federal agency — NASA centers, DoD labs,
DOE national labs, USACE — rather than one employer, so it is configured as a
set of queries instead of a board token.

Setup, once:

```bash
cp .env.example .env       # then fill in both values
python3 pipeline.py doctor # confirms the credentials are visible
```

`.env` is gitignored and read on startup; a real environment variable still wins
over it, so `USAJOBS_API_KEY=... python3 pipeline.py fetch` works for a one-off.
Both values are required — the API authenticates on the key *and* rejects any
request whose `User-Agent` is not the email address the key was registered with
(https://developer.usajobs.gov/apirequest/). `doctor` flags either one missing.

The source holds a list of `queries`, each with its own filters:

| Field | Effect |
|---|---|
| `keywords` | searches to run for this query; omit entirely to issue one unkeyworded request |
| `hiring_paths` | `student`, `graduates`, `public`, … |
| `job_category_codes` | occupational series (`0899` engineering student trainee, `0830` mechanical) |
| `location_names` | `"Austin, Texas"` style names, optionally with `radius` |
| `organizations` | agency subelement codes |
| `posted_within_days` | `DatePosted`, 0–60 |
| `fields` | `Full` (default) pulls the job summary and duties, which is most of the scoring signal; `Min` returns only the qualification blurb |

**Keep the hiring-path sweep and the keyword sweep as separate queries.** The
API ANDs its filters and the two annihilate each other — measured 2026-07-26,
`Keyword=mechanical engineering` returned 255 rows alone and **4** once
`HiringPath=student` was added. Federal titles also rarely say "intern"
(Pathways roles are titled `Student Trainee (…)` or just the occupation), which
is why the hiring-path query runs with no keyword at all.

Each query is paged to the API's documented ceiling (500 rows per page, 10,000
per query), so a large result set is not silently truncated to its first page.
Postings keep their own hiring agency as `company`; the source's `company` is
only a fallback label.

Two things to expect from this source:

- **It is strongly seasonal.** Of the 106 open student-path postings on
  2026-07-26, 33 were published in September — federal Pathways and summer
  internships post in roughly Sep–Jan. A mid-summer run legitimately returns a
  few dozen postings, mostly legal, clerical, and administrative, with almost
  no mechanical engineering. That is the federal hiring calendar, not a broken
  fetch. Re-check yield in the fall before retuning the queries.
- **NASA is not in it.** NASA had 0 student-path postings on USAJOBS against 14
  postings total; its internships run through
  [intern.nasa.gov](https://intern.nasa.gov/) instead, which is in
  `manual_check_sources`.

### Aggregator postings (Adzuna)

Employer boards only reach employers big enough to run a modern ATS. Adzuna
covers the rest — small manufacturers, machine shops, suppliers, staffing
firms — and is the only configured source with a real radius filter, so it can
answer "within 60 km of Austin" rather than "matches a city name we listed".

Like `usajobs`, it is a set of `queries` rather than a board token:

| Field | Effect |
|---|---|
| `what` | search terms |
| `what_exclude` | terms that disqualify a posting |
| `where` | a place name; required for `distance_km` to do anything |
| `distance_km` | radius around `where`, **in kilometres** regardless of country |
| `posted_within_days` | maximum posting age |
| `max_pages` | page cap for this query (default 5, 50 results per page) |

Two things to expect. The free tier is rate limited, which is why page depth is
capped. And Adzuna's search results carry a **truncated description snippet**,
not the full posting — so these rows score on title and location much like
job-alert emails do, and are the best candidates for
[`enrich`](#enrich-thin-postings).

**Handshake and 12twenty are not automated.** Check them by hand on the cadence
in `manual_check_sources` and copy good results into `data/manual_jobs.csv`.

**University student-job portals on Workday can often be automated.** Many
answer on a public Workday CXS endpoint without any login, so they run as an
ordinary `workday` source in that student's `config/sources.local.json`. *Applying* still requires
signing in. Expect a low hit rate: most listings are graders, work-study, and
administrative student roles, and the scorer sinks them accordingly.

**LinkedIn is automated, by explicit owner decision (2026-07-26).** An earlier
version of this file ruled LinkedIn out on the same footing as Handshake; that
was overridden deliberately, and LinkedIn search now runs through an
authenticated session — see [Agent-reached channels](#agent-reached-channels).
Two things did not stop being true and are recorded here so the trade-off looks
like a decision rather than an oversight:

- LinkedIn's terms prohibit automated access even for a legitimate account
  holder, so this is a terms violation, not a technical grey area.
- Enforcement lands on the **account**, not the tool, so restriction or ban is
  the live risk. A dedicated account limits the blast radius.

### LinkedIn job-alert emails

LinkedIn's own job-alert emails (from `jobs-listings@linkedin.com`) are a
sanctioned channel you already opted into — reading your own inbox isn't
scraping LinkedIn. There's a dedicated intake path for these:

```bash
python3 pipeline.py import-emails [path]   # defaults to data/linkedin_emails.json
```

This does not read your inbox itself — `pipeline.py` has no standing Gmail
credentials. Instead, a Claude Code session (with Gmail access) extracts
matching alert emails into a JSON file matching this contract, then runs the
import:

```json
[
  {
    "title": "Mechanical Design Intern",
    "company": "Acme Robotics",
    "location": "Austin, TX",
    "url": "https://www.linkedin.com/comm/jobs/view/...",
    "posted_hint": "Posted on 7/20/2026",
    "match_note": "High skills match"
  }
]
```

These postings carry no job description (LinkedIn's alert emails don't
include one), so their score leans on title/location keyword hits only —
that's expected, not a bug. `import-emails` is deliberately not part of
`run`, since the extraction step happens in a separate agent session. Thin rows
like these are good candidates for [`enrich`](#enrich-thin-postings).

## Agent-reached channels

Channels an agent session reaches through [Agent Reach](https://github.com/Panniantong/agent-reach),
then hands to the pipeline. `pipeline.py` does **not** call any of them — it stays
stdlib-only and offline-testable, and the agent does the network work. This is the
same seam `import-emails` already used, generalized.

Configure which channels and queries are in play under `agent_discovery` in
`config/sources.json`. Channels and the tool behind each:

| `channel` | Reached with | Good for |
|---|---|---|
| `linkedin` | `mcporter call 'linkedin-scraper.search_jobs(...)'` | postings that never reach a public ATS feed |
| `exa` | `mcporter call 'exa.web_search_exa(...)'` | finding employers not yet in `ats_sources` |
| `github` | `gh api repos/<repo>/contents/README.md` | community internship lists |
| `jina` | `curl -s https://r.jina.ai/<url>` | reading a public posting or career page as text |
| `rss` | `feedparser` | lab and society feeds |

### Import discovered postings

```bash
python3 pipeline.py import-discovered [path]   # defaults to data/discovered_jobs.json
```

Each record needs `channel`, `company`, `title`, and an `http(s)` `url`.
`location`, `description`, and `posted_at` (ISO) or `posted_hint`
(`"Posted on 7/21/2026"`) are optional:

```json
[
  {
    "channel": "exa",
    "title": "Thermal Systems Intern",
    "company": "Redwood Materials",
    "location": "Fremont, California",
    "url": "https://example.com/careers/thermal-intern",
    "posted_at": "2026-07-18T00:00:00+00:00",
    "description": "Support thermal modeling for battery pack design."
  }
]
```

For `channel: "linkedin"` you can skip the split-out fields and pass the raw
`get_job_details` text as `raw_posting` instead — company, title, location, and
description are parsed from it. Any explicit field still wins, so a bad parse can
be corrected without editing the blob:

```json
[
  { "channel": "linkedin", "url": "/jobs/view/4416596272/", "raw_posting": "Neuralink\n\n…" }
]
```

Behaviour worth knowing:

- **Each channel gets its own `source_key`** (`agent:linkedin`, `agent:exa`, …).
  Like every other source, an import retires rows that batch didn't contain — so
  scoping per channel is what stops a LinkedIn-only run from retiring everything
  Exa found last week.
- **A channel that found nothing needs the envelope form.** A bare list can only
  name channels that returned something, so a channel going quiet would keep its
  old rows active forever. Wrap the batch to say what you actually searched:

  ```json
  {
    "searched_channels": ["linkedin", "exa"],
    "postings": [
      { "channel": "linkedin", "company": "…", "title": "…", "url": "https://…" }
    ]
  }
  ```

  Every channel in `searched_channels` is processed even with zero postings, so
  `exa` above retires its stale rows. The bare list form still works unchanged.
- **Unknown channels are skipped, not invented.** Provenance is the one field a
  human reviewer can't reconstruct later, so `channel` is an allowlist.
- **Malformed rows are skipped with a warning** rather than aborting the batch,
  matching `import-emails` — the input is best-effort agent extraction.
- **Two LinkedIn artifacts are normalized on the way in**, both observed in real
  output: site-relative URLs (`/jobs/view/123/`) are absolutized, and the
  verification-badge text LinkedIn appends to scraped titles
  (`"… Intern with verification"`) is stripped.

### Enrich thin postings

```bash
python3 pipeline.py enrich [path] [--force]   # defaults to data/enrichment.json
python3 pipeline.py score                     # enrich does not rescore
```

Postings that arrive from job-alert emails or search results have no description,
so they score on title and location alone. An agent session can read the public
posting page and supply the text here, giving them the same scoring surface as an
ATS-sourced posting. Records match on `url` or `id`:

```json
[
  { "url": "https://example.com/careers/thermal-intern", "description": "Full posting text…" }
]
```

Only thin descriptions (under 200 characters) are replaced, so re-running never
clobbers richer ATS text; `--force` overrides that. HTML is stripped, and a blank
`location` or missing `posted_at` is backfilled when supplied.

Enrichment survives later imports: a sweep that rediscovers an enriched posting
without a description leaves the enriched text in place, while a source that does
supply a full description still wins. Backfilling a blank `location` also
refreshes the posting's fingerprint and re-runs deduplication — a blank location
is treated as compatible with every city, so naming the city can reveal a posting
that was hidden behind a copy it actually contradicts.

### Two traps in raw LinkedIn output

Both are handled in code, but they explain why the raw tool output isn't imported
as-is:

1. **`search_jobs` returns no company or location** — only a title and a relative
   URL. You need `get_job_details(job_id)` per result to get a usable record.
2. **`get_job_details` text ends with a "More jobs" carousel of *other
   companies'* postings.** Left in, an unrelated employer's keywords would score
   this posting. `parse_linkedin_job_posting()` cuts at the first chrome marker
   after `About the job`. Its relevance filtering is also loose — the same query
   returned "Lead Principal Firmware Engineer" — so keep filtering on
   `discovery_title_terms` rather than trusting the result set.

Scores are deliberately simple: preferred role type (+18), degree match (up to
+15), interests (+20), demonstrated skills (+15), preferred location (+10),
term availability (+8), recency (+10), and penalties for seniority, experience,
relocation, discipline, or explicit availability mismatches. Matches in a title
count more than incidental words in a long description. List
disciplines you don't want in `deprioritize_title_keywords` if their titles are
ranking too high, and remove entries there if they are ranking too low. Citizenship and sponsorship language is
flagged for human verification; a sponsorship penalty is applied only when the
profile explicitly says sponsorship is required.

## Duplicate handling

The same opportunity often arrives through several channels. Duplicates are
linked rather than deleted — one row is canonical and the rest point at it via
`duplicate_of`, so nothing is lost and the shortlist doesn't repeat itself.
Matching runs in three passes:

1. Identical company + title + location.
2. Same company and title across sources whose locations don't contradict each
   other.
3. Near-identical description bodies across *different* sources.

The second pass exists because channels format locations differently: Greenhouse
packs several cities into one field
(`"Austin, Texas, United States; South San Francisco, California, United States"`)
where LinkedIn gives `"Austin, TX"`. Comparing city tokens links those, while
genuinely different cities — the same title in Austin and in Boston — stay
separate. A blank location counts as unknown rather than conflicting, the same
way the scorer declines to penalise an uninformative location.

The third pass exists because the first two both key on the company name, so
neither can reconcile a posting that arrives from the employer's own board *and*
from a channel that restyled the company and rewrote the title. Employers rarely
rewrite the requirements text, so the body is the reliable key: each description
gets a 64-bit SimHash fingerprint over 3-token shingles, and two postings are
linked when at least 92% of those bits agree (at most 5 of 64 differ —
near-verbatim only). Descriptions under 200 characters carry too little signal
and are never fingerprinted, so a thin posting is never falsely merged.

That pass only ever links across *different* sources, and only where locations
don't contradict: one employer legitimately posts several requisitions off the
same JD template, and the same JD used for two cities is two opportunities.

**What it does and doesn't catch (measured 2026-08-05).** Across 217
fingerprintable postings and 369 comparable cross-source pairs, this pass linked
nothing — and the measurement is the reason the threshold stays at 0.92 rather
than being relaxed:

| Pair | Similarity |
| --- | --- |
| Same Figure job, Greenhouse vs Adzuna (a true duplicate) | 0.781 |
| Two *different* Figure roles, sharing company boilerplate | 0.719 |
| Neuralink vs Base Power, unrelated roles | 0.703 |

Only 0.06 separates a true match from a false one, so any threshold low enough
to catch the real pair would also merge unrelated companies' postings. The
reason the true pair scores so low is that **Adzuna truncates every description
to exactly 500 characters** — its copy is a prefix of the real body, not a
near-verbatim mirror. That case is already handled by pass 2 anyway, since the
company and title match. Pass 3 earns its keep on sources that carry full
bodies, which is what `enrich` gives agent-discovered rows.

Canonical is the furthest-along copy by status, then an ATS source over an
`agent:`/`manual:` one, then the longest description — so a tracked application
is never demoted to a duplicate of an untracked row.

### Re-listed roles

Separately from duplicate linking, scoring adds an informational note when a
role went away and came back at a *different* URL within 90 days:

> FLAG: this role has been listed under 2 different URLs since 2026-06-14 — may
> be an evergreen or re-listed req

Cohort markers are ignored when comparing roles, so "Mechanical Intern (Summer
2027)" and "Mechanical Intern [Fall 2026]" count as the same role.

Two conditions must both hold: an earlier posting of that role must have been
*retired*, and the live one must be at a *different URL*. So terms advertised
side by side are not flagged, and neither is the same URL going inactive and
coming back.

What this does flag, correctly, is a role an employer re-posts each cycle — the
12 hits on the current data are all SpaceX seasonal requisitions, where "Fall
2026 Engineering Internship/Co-op" closes and "Spring 2027" opens at a new URL.
That is what the "evergreen" half of the wording refers to; it is a recurring
pipeline posting rather than a live opening created for you, which is worth
knowing before you spend effort on it.

**The flag never changes the score.** A re-listed requisition is often just an
evergreen posting or an ATS migration — this is information for you, not a
verdict on the employer.

## Resume and cover letter

```bash
cp config/resume.example.json config/resume.json   # then fill it in
python3 pipeline.py resume
python3 pipeline.py resume --job <ID>              # emphasised for one posting
python3 pipeline.py cover-letter --job <ID>
python3 pipeline.py resume --job <ID> --pdf
```

Output lands in `output/applications/` (gitignored).

**`config/resume.json` is the only source of factual claims.** Tailoring
*reorders and emphasises* what is already there — it never adds a skill, a
number, or an experience. Against a posting, matching skills move to the front
of their group and are bolded; a matching project moves up; matching coursework
leads. Matching is whole-word, so "CAD" is not found inside "Cadence". If a
posting names nothing you listed, nothing is emphasised — which is itself a
useful signal about fit.

The cover letter is a **scaffold, not a finished letter**. Everything the tool
cannot legitimately know — why this company, what you actually built — is
emitted as a highlighted `TODO` you have to replace. It also lists the posting's
requirement-shaped sentences as a checklist to answer, which you delete before
sending. There is no model in this pipeline inventing prose on your behalf.

PDF output is optional and needs Playwright:

```bash
pip install -r requirements-optional.txt
python3 -m playwright install chromium
```

Without it, `--pdf` still writes the HTML and tells you so — printing that to PDF
from a browser gives the same document. The pipeline itself stays dependency-free;
nothing in discovery, scoring, or reporting touches this.

Both templates are ATS-safe by construction: one column, no tables, no text
boxes, no images, standard section headings, and real selectable text. They use
system fonts deliberately — an embedded webfont has no ATS benefit and can garble
the extracted text layer in some parsers.

## Finding new ATS boards

`discover-ats` turns a list of company names into `ats_sources` entries. It
probes the Greenhouse, Ashby, and Lever public APIs (in that order, first hit
wins) and resolves a company only when a board exists *and* currently lists
postings.

```bash
python3 pipeline.py discover-ats "Firefly Aerospace" "Base Power"
python3 pipeline.py discover-ats --in companies.json
python3 pipeline.py discover-ats "Firefly Aerospace" --write
```

**It previews by default and writes nothing.** With `--write`, confirmed
entries are appended to your own `config/sources.local.json`; add `--shared` to
append to the tracked shared catalog `config/sources.json` instead.

The identity check is the important part. As `_source_verification_note` in
`config/sources.json` records, guessing tokens produces convincing impostors —
`greenhouse/archer` is Archer Veterinary Clinic, not Archer Aviation. A board
returning JSON proves nothing about whose board it is, so each hit is graded:

- **confirmed** — Greenhouse exposes the board's own `name` and it matches the
  company you asked for (corporate suffixes like "Inc." are ignored). Only these
  are written.
- **review** — Greenhouse gave a name and it *doesn't* match. The real name is
  printed so the mismatch is obvious. Never written.
- **unverified** — Ashby and Lever expose no company name at all, so this can't
  be settled automatically. A sample posting title is printed as evidence; pass
  `--include-unverified` to write these once you've checked them yourself.

Workday is deliberately not probed: it needs a tenant, datacenter, *and* site,
and site names are unguessable (`NVIDIAExternalCareerSite` versus
`External_Career_Site`), so a company name alone cannot resolve one. Companies
that don't resolve are listed for manual follow-up rather than dropped — a
JS-rendered portal or a non-standard slug lands there too.

## Checking whether a posting is still open

Postings fetched from an ATS board retire themselves: whatever is missing from a
source's latest batch is marked inactive on the next `fetch`. Rows that arrive
without a batch behind them have no such mechanism — `import-discovered` and
`import-emails` only run when you invoke them, and neither is part of `run` — so
those go stale silently.

```bash
python3 pipeline.py liveness              # check agent:/manual: rows
python3 pipeline.py liveness --dry-run    # report verdicts, change nothing
python3 pipeline.py liveness --limit 40   # least recently seen first
python3 pipeline.py liveness --all        # include ATS rows too
```

Each posting page gets one of three verdicts, and **only `expired` retires a
row**:

- **active** — a visible apply control was found.
- **expired** — HTTP 404/410, a closure banner, or a page with nothing left on it.
- **uncertain** — anything ambiguous: an anti-bot challenge, a 403/5xx, or a
  redirect that landed somewhere other than the posting.

The three-way split is the point. A false "expired" removes a real opportunity
for good, which is much worse than carrying a dead row for another week. This is
not hypothetical: SpaceX's board sits behind Cloudflare, and its challenge page
is short and has no apply button — read naively that looks exactly like a dead
posting, and would retire every live SpaceX internship at once.

Postings you've already engaged with (`applying`, `applied`, `interview`,
`offer`) are reported but never retired. Losing sight of a submitted application
is worse than keeping a stale row.

### Deleting expired postings

Retiring hides a row; `purge-expired` deletes it. A posting counts as expired
when it is retired (`active=0`, from a source batch or a liveness verdict) or
when its description states a deadline (`Apply by ...`, `Deadline: ...`,
`Applications close ...`) that is before today. The deadline day itself is still
open, and a posting with no stated deadline is never guessed at.

```bash
python3 pipeline.py purge-expired --dry-run   # counts only
python3 pipeline.py purge-expired             # pipeline.db
python3 -m opportunity_app.purge [--dry-run]  # platform.db (or --db URL)
```

Postings with an application (`applying` through `offer`, plus `rejected` and
`withdrawn`) are kept in both databases. Everything else about a deleted posting
goes with it, including saves and notes. If a source lists it again, it returns
as a new row.

Before anything is deleted, each purge snapshots its database to a `backups/`
folder beside it (`data/backups/pipeline-<UTC time>.db` and
`data/backups/platform-<UTC time>.db`) and keeps the newest 14 of each. Dry
runs and purges with nothing to delete write no snapshot. If the snapshot fails,
nothing is deleted. To undo a purge, stop the dashboard and copy a snapshot back
over the database. PostgreSQL targets have no file to snapshot, so back them up
with `opportunity_app.ops_cli backup` first and pass `--no-backup`.

## Running it on a schedule

`run` is idempotent and read-only against the outside world, so it's safe
unattended. On any system, `python -m opportunity_app.launch install-daily`
schedules it. On macOS and Linux that runs `python -m opportunity_app.daily`,
a Python port of the script below with the same steps, checkpoints, and state
file.

On Windows, `scripts/run-daily.ps1` wraps `run`, a bounded liveness pass, and
`purge-expired` on both databases, and appends to `data/run.log`. Register it with Task Scheduler (once, from the
project root; rerunning replaces the existing task):

```powershell
.\scripts\install-daily-task.ps1
```

The task runs through `scripts/run-daily.vbs`, so no console window appears.
Registering `powershell.exe` as the action directly shows a blank window on
every run, and closing that window kills the run.

Runs survive the laptop being closed. Progress is checkpointed to
`data/daily-run.json` after each step, and besides the daily time the task also
fires at sign-in, on unlock, on wake from sleep, and every 30 minutes. A start
after the day's run is done exits immediately without logging; a start that
finds an unfinished run resumes it from the step it stopped at, and the fetch
skips sources that already succeeded in that run. If some sources were
unreachable (the network is usually still reconnecting right after waking),
`run` exits 75 and the fetch is retried, up to four attempts, before the day's
results are kept as they are. A run left unfinished for 20 hours is dropped in
favour of a fresh one. The dashboard task already recovers on its own: its
one-minute watchdog trigger restarts it if sleep or shutdown ended it.

The script resolves `py`/`python` explicitly, because Task Scheduler runs with a
minimal environment where a bare `py` often isn't on `PATH`.

## Cold outreach pipeline

The Outreach tab runs cold email from research to reply. **Nothing sends on its
own**: an approved draft opens in your own email account, where you press Send,
or, with Gmail connected, goes out when you press **Send** in the app and then
confirm the recipient.

1. **Find companies.** The deep search runs on Monday and Thursday mornings (or
   **Run deep search now**). Claude Code searches for accelerator startups
   near your target regions, US startups in your field, and recently funded
   companies,
   using only web search and fetch, outside the project directory. Each kind of
   company is its own search of up to 10 companies, run one after another, so
   the broadest one does not crowd out the local search; a later search is told
   what an earlier one found, and one that fails does not lose the others (its
   error shows on the Deep search panel). Python checks
   every proposal before importing it: the website and at least one source URL
   must load. Rejected companies are listed with the reason. Each run writes
   `data/outreach-discovered-<date>-<UTC time>-<run id>.json`.
   A company is never proposed twice. Names are compared without case,
   punctuation, or legal forms ("Acme Robotics, Inc." is "Acme Robotics"), and
   websites by domain. Companies you deleted from the Outreach tab, companies
   you have an application with, and companies with an open posting in your
   feed are rejected with that reason. Recent rejections go back into the next
   prompt, so a company that failed a check is only proposed again with a fix.
2. **Find contacts.** **Find contacts** reads a few pages of the company's own
   site, honoring robots.txt. Addresses published there are *confirmed*.
   Addresses guessed from a named person (following the site's own pattern when
   one is visible) are *unverified*, and are only made when the domain accepts
   mail. Every candidate links its evidence page. The same pages record where
   the company is based when they say so (see **Company locations** below).
3. **Draft.** **Generate draft** writes from your *confirmed* profile facts and
   the target's research only. The model must cite a basis for each claim; a
   draft citing anything else, or stating a number found in neither source, is
   retried once and then refused. Without a model, a template draft uses only
   confirmed facts.
4. **Approve and send yourself.** **Approve draft** refuses unfilled
   `[placeholders]` or a missing recipient, and asks you to accept any other
   warnings (dashes, length). Approval unlocks **Open in Gmail**, a prefilled
   compose window in your account. Editing the draft or changing the recipient
   withdraws the approval. Click **I sent it** after sending; that sets a
   seven-day follow-up.
5. **Follow up and log replies.** Due follow-ups get an in-app reminder (from
   the daily run and the worker) and a **Generate follow-up** draft with the same
   approval step. Paste a reply into **Log a reply** to get a suggested status
   (call, declined, come back later); nothing changes until you click it. After
   a follow-up goes unanswered for 14 days, the card suggests No response.

Settings in `.env`:

```text
PIPELINE_OUTREACH_COMPOSE=gmail            # gmail or mailto (default mailto)
PIPELINE_OUTREACH_ACCOUNT=you@school.edu   # the Google account compose opens in
PIPELINE_OUTREACH_PROVIDER=claude-code     # optional draft model; default is the agent default
PIPELINE_OUTREACH_DISCOVERY_PROVIDER=claude-code  # or codex-cli for the deep search
PIPELINE_OUTREACH_ATTACHMENT=data/outreach-attachments/resume.pdf  # attached to Gmail drafts
PIPELINE_SEC_USER_AGENT="Your Name you@example.com"  # enables SEC Form D lookups
```

### Company locations and SEC Form D

Each target's location shows where it came from, with a link: your own entry,
the company's site, an SEC Form D filing, or the deep search. A location only
the deep search reported is marked *not yet checked*, and a draft does not say
you are nearby until the site or a filing agrees or you confirm the research.
Your own entry is never overwritten; the company's site outranks a filing.

- **Company site.** Structured data with a postal address, a sentence like
  "headquartered in Austin, TX", or a street address with a ZIP code. A site
  that lists several places at the same level sets none. Failing those, a site
  that names exactly one place (a footer that just says "Austin, TX") gives it
  as *the only place it names, not yet checked*: the site never says the
  company is based there, so a draft does not rely on it until you click
  **Confirm location**. A site whose pages are empty without JavaScript is
  read again in headless Chromium when Playwright is installed
  (`pip install -r requirements-optional.txt`, then
  `python -m playwright install chromium`). That browser may only load public
  addresses, never this machine or your network, and skips images and styles.
- **Needs a location** in the Outreach rail lists companies you have not
  contacted whose location is missing or not yet checked. Typing a location
  under Research, or **Confirm location** on one shown, settles it.
- **SEC Form D.** A US startup files one after selling shares in a private
  round. EDGAR full-text search finds filings by an issuer with exactly the
  target's name, and the Outreach tab shows the amount sold, the filing date,
  and a link to the filing. Two issuers with the name are reported as
  ambiguous. A filing from a different place than the target's location is
  shown as a possible match and changes nothing, and a filing older than five
  years does not set a location. SEC requires automated clients to identify
  themselves, so set `PIPELINE_SEC_USER_AGENT` to your name and email.

New deep search companies are checked as they are added. To fill in companies
already on the list:

```powershell
py -3 -m opportunity_app.outreach_cli enrich            # targets with no sourced location or Form D yet
py -3 -m opportunity_app.outreach_cli enrich --limit 10 --no-sec
```

It prints how many targets lacked a location before and after. A target is
rechecked at most every 30 days (`--force` overrides that), and the scheduled
deep search runs `enrich --limit 15` after each search.

- **A web search**, for the companies neither their own site nor a filing
  placed. A young startup often states its city nowhere on its site and has
  filed nothing, while one search finds it on its accelerator's page or in a
  funding story. The same headless CLI the deep search uses does the searching,
  and nothing it says is taken on its word: the app opens the page the search
  cites and keeps the location only when that page loads, names the company,
  and states the place. The result is shown as *from a web search* with a link
  to that page, and it ranks below the company's own site and a filing, so
  either one later overrides it. A location you typed is never touched.

```powershell
py -3 -m opportunity_app.outreach_cli locate             # every company nothing else placed
py -3 -m opportunity_app.outreach_cli locate --limit 10 --batch 4
```

A deep search does this for its own new companies; `discover --no-locate`
skips it. Each run reports what it refused and why, so a company with no
sourced location stays visibly empty rather than getting a guess.

### Sending through Gmail, with an attachment

A compose link cannot attach a file. To attach your resume, connect Gmail. An
approved draft then shows two buttons:

- **Send with resume.pdf** sends it from your Gmail without leaving the app.
  The first click only asks: the button becomes **Send to jane@company.com?**,
  and a second click sends. Escape, clicking elsewhere, or waiting eight seconds
  cancels. A successful send marks the company Sent and sets the follow-up a
  week out, the same as **I sent it**. Each email goes out at most once, and
  nothing is sent if the draft changed after you confirmed it.
- **Open in Gmail with resume.pdf** creates the draft in your Gmail Drafts
  folder and opens it, for when you want to edit it there first. Clicking again
  for the same approved words reopens the same draft. Once a draft exists, send
  it from Gmail and press **I sent it**: the app never sends a Gmail draft, since
  it may have been edited there, and it refuses to send its own copy while the
  draft is still in Drafts.

The app sends only the words you approved, and does not send an email a second
time on its own:

- Two clicks, two tabs, or a retry cannot send twice. Only one send or draft of
  an email runs at a time.
- If Gmail does not confirm a send (a timeout or a Google error), the email may
  have gone out. The app then asks you to check your Gmail Sent folder before it
  sends again. The button becomes **Checked Gmail — send again**, and that
  covers one attempt. The app cannot read your Sent folder, so this check is
  yours: if the email is there, press **I sent it** instead.
- If a Gmail draft of the email has left your Drafts, it may have been sent from
  Gmail, so the app asks the same question.
- Once an email was sent, or marked sent by hand, no new Gmail draft of it is
  made.

The app requests only the `gmail.compose` scope, which covers both drafts and
sending, and calls only `drafts.create`, `drafts.get`, `messages.send`, and
`profile`. The connection is refused if Google signs in as
an account other than `PIPELINE_OUTREACH_ACCOUNT`.

One-time setup:

1. In the [Google Cloud console](https://console.cloud.google.com/), create a
   project, enable the **Gmail API**, and configure the OAuth consent screen.
   Add your sending account as a test user if the app is External.
2. Create an OAuth client of type **Web application** with these authorized
   redirect URIs:
   `http://127.0.0.1:8765/connections/oauth/gmail_drafts/callback` and
   `http://localhost:8765/connections/oauth/gmail_drafts/callback`.
3. In `.env`, set `GOOGLE_OAUTH_CLIENT_ID`, `GOOGLE_OAUTH_CLIENT_SECRET`,
   `PIPELINE_OUTREACH_ACCOUNT` (your address), `PIPELINE_OUTREACH_COMPOSE=gmail`,
   and `PIPELINE_OUTREACH_ATTACHMENT`. `setup init` already generated
   `PIPELINE_CONNECTION_KEY`, the Fernet key the tokens are stored encrypted
   with. Restart the app (`python -m opportunity_app.launch restart`).
4. Click **Connect Gmail** on the Outreach tab.

An External app left in Testing status gets refresh tokens that expire after
seven days; the tab then offers **Reconnect Gmail**. A Google Workspace school
account may also block unverified apps from Gmail scopes.

Register the twice-weekly deep search (Claude Code or Codex CLI must be signed
in; run `claude` once):

```bash
python -m opportunity_app.launch install-outreach   # any system
```

On Windows that registers the task below, `.\scripts\install-outreach-task.ps1`.

It runs through `scripts/run-outreach-discovery.vbs`, so no console window
appears. A run missed while the laptop was off starts when it is next on, and
a scheduled run within 48 hours of a successful one is skipped. The log is
`data/outreach-discovery.log`. To try it without changing anything:

```powershell
.\scripts\run-outreach-discovery.ps1 -DryRun
```

## Commands

```text
python3 pipeline.py fetch
python3 pipeline.py import-csv [path]
python3 pipeline.py import-emails [path]
python3 pipeline.py import-discovered [path]
python3 pipeline.py enrich [path] [--force]
python3 pipeline.py score
python3 pipeline.py report --limit 30 --dashboard-limit 300
python3 pipeline.py run --limit 30 --dashboard-limit 300
python3 pipeline.py resume [--job ID] [--pdf]
python3 pipeline.py cover-letter --job ID [--pdf]
python3 pipeline.py discover-ats [names...] [--in file] [--write] [--include-unverified]
python3 pipeline.py liveness [--limit N] [--all] [--dry-run]
python3 pipeline.py purge-expired [--dry-run]
python3 pipeline.py doctor
python3 pipeline.py status
```

Run tests with:

```bash
python3 -m unittest discover -s tests -v
```
