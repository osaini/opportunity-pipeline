# Opportunity Pipeline

A local, inspectable pipeline for finding and tracking internships, externships,
co-ops, undergraduate research, and part-time work. Every opportunity retains its
source URL, fetch time, and score explanation. It never auto-applies.

It has two halves that share one codebase: a dependency-free command-line
pipeline (`pipeline.py`) that fetches, scores and reports, and a local web app
(`opportunity_app/`) with a student workspace, an Outreach tab, an Apply Mode
browser extension, and a durable worker. Everything runs on your own computer and
listens only on `127.0.0.1`.

## Quick start

**Setting up your own copy?** Open this folder in your coding agent and ask it
to follow [`SETUP.md`](SETUP.md). It interviews you for your profile, sets up
your sources and optional keys, and leaves you with a one-click launcher. That
works on Windows, macOS, and Linux.

The command-line pipeline alone needs only Python 3.11+ and no third-party
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

To use the web app as well:

```bash
python -m pip install -r requirements-web.lock
python -m opportunity_app.setup init
python -m opportunity_app.launch open
```

`launch open` starts the server in the background if it isn't running, then
opens your browser already signed in. See [The web app](docs/guide/web-app.md)
for what it does, and [Running it unattended](docs/guide/scheduling.md) to keep
it running.

## What it never does

These statements are policy, not description, and changing one is a decision for
the owner (see [`AGENTS.md`](AGENTS.md), "Product invariants").

- It never auto-applies. Apply Mode in `apps/extension` uses transient `activeTab`
  access, inventories Workday/Greenhouse/Lever/Ashby/SmartRecruiters/generic
  fields, requires review for sensitive fields, and has no final-submit
  capability. (The same sentence sits in the Apply Mode bullet of
  [The web app](docs/guide/web-app.md#full-opportunity-platform).)
- `pipeline.py` itself never authenticates to anything, submits an application, or
  guesses a missing fact, and it still has no third-party dependencies. Stale and
  inactive postings remain in the database for history but disappear from the
  active shortlist.
- Outreach mail you send starts from a draft you approved: it opens in your own
  email account, or goes out when you press **Send** in the app and confirm the
  recipient. Three opt-in automations send that approved text later without
  another click (a scheduled send, the resend after a bounce, contact-form
  submission). One writes its own: the short thank-you after a plain decline is
  the only email the app composes and sends without your approval. Each is a
  switch under Outreach settings → Automation, and pausing automation stops them
  all (see [Cold outreach](docs/guide/outreach.md) and [Gmail](docs/guide/gmail.md)).
- Every posting keeps its source, its fetch time, and an honest explanation of its
  score; an inferred value is never shown as confirmed.

## Where to read next

The manual lives in `docs/guide/`, one file per topic. Each heading below is a
section that used to be in this file.

| If you want to... | Read | Sections |
| --- | --- | --- |
| Run the web app, turn on the AI agent or Jev reviews, or host it | [The web app](docs/guide/web-app.md) | Full opportunity platform; Enable the AI career agent; Enable on-demand Jev reviews; Hosting, the worker and backups |
| Keep the dashboard running, or run the pipeline daily without a terminal | [Running it unattended](docs/guide/scheduling.md) | Keep the local web dashboard running; Running it on a schedule |
| Tune the ranking to your situation | [Ranking and eligibility](docs/guide/ranking.md) | Personalize ranking and eligibility review; Target regions; How scores are computed |
| Use the command line day to day | [The command-line workflow](docs/guide/cli-workflow.md) | Unified dashboard; Daily and weekly workflow; Resume and cover letter; Finding new ATS boards; Checking whether a posting is still open; Deleting expired postings |
| Add sources, API keys, or agent-found postings | [Sources and discovery](docs/guide/sources.md) | Sources and boundaries; API keys; Federal postings (USAJOBS); Aggregator postings (Adzuna); LinkedIn job-alert emails; Two traps in raw LinkedIn output; Agent-reached channels; Import discovered postings; Enrich thin postings |
| Understand duplicate linking | [Duplicates and re-listed roles](docs/guide/duplicates.md) | Duplicate handling; Re-listed roles |
| Run cold outreach | [Cold outreach](docs/guide/outreach.md) | Cold outreach pipeline; Company research and call prep; Company locations and SEC Form D; Running the deep search on a schedule |
| Connect Gmail for sending, replies and labels | [Gmail](docs/guide/gmail.md) | Sending through Gmail, with an attachment; Gmail drafts setup |

Other documents:

- [`SETUP.md`](SETUP.md): set up a new student's copy, step by step.
- [`AGENTS.md`](AGENTS.md): orientation for coding agents (layout, rules, test suites).
- [`CONTRIBUTING.md`](CONTRIBUTING.md): how to contribute.
- [`docs/`](docs): the threat model, runbook, privacy and accessibility notes,
  the Apply for me specification, the assisted-apply guide, and the testing
  reference. [`docs/known-defects.md`](docs/known-defects.md) lists defects found
  and not yet fixed.
- [`scripts/README.md`](scripts/README.md): what each script does and which are
  installed as scheduled tasks.
- [`apps/extension/README.md`](apps/extension/README.md): the Apply Mode browser extension.

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
