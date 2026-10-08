# Sources and discovery

Where postings come from, what each source needs, and how an agent session hands postings to the pipeline.

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

What the board adapters keep of a posting, so the score has something to read:

- **Lever** keeps the bulleted sections ("What you'll do", "What we require") as well as the opening paragraph and
  the closing note, because years of experience and sponsorship limits are usually stated in the bullets. The posting
  date is Lever's creation time.
- **Ashby** skips a posting its board marks as not listed (`isListed` false), since the company keeps it off its
  public page. When Ashby gives a structured USD salary paid by the year or the hour, the description gains a
  sentence ("Pay listed on the Ashby posting: $211,400 - $290,600 per year.") so the pay filter can read it. When
  Ashby's summary says the posting has several ranges, the sentence keeps its words ("... per year (Multiple
  Ranges)."). A posting whose only text is that sentence still counts as having no description. Other
  currencies, other periods, equity, bonus and commission are not written out.

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
credentials. The alerts may sit in a different Gmail account from the pipeline
mailbox (the one the app connected to), so confirm with the person which account
holds them before searching. Instead, a Claude Code session (with Gmail access) extracts
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

Back to the [README](../../README.md) index.
