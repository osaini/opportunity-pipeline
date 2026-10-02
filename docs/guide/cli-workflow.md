# The command-line workflow

The dependency-free `pipeline.py`: the static dashboard, the daily and weekly routine, resumes and cover letters, finding job boards, and keeping the posting list honest. The command list is in the [README](../../README.md#commands).

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

## Daily and weekly workflow

- Daily during peak recruiting: run `python3 pipeline.py run`, inspect the top
  ten, and verify the posting on the source page.
- Twice weekly: ask an agent session for an [agent-reach sweep](sources.md#agent-reached-channels)
  — LinkedIn and Exa for the `agent_discovery` queries, then
  `import-discovered` → `enrich` → `score` → `report`. Public ATS feeds in
  `ats_sources` are already covered by `run`, so this pass is for what those
  feeds miss.
- Twice weekly: check the login-only portals listed in your
  `manual_check_sources` (your school's career portal, Handshake, and so on).
  Copy good results into `data/manual_jobs.csv`. A school portal on public
  Workday can often be fetched automatically instead — see
  [Sources](sources.md#sources-and-boundaries).
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
source's latest batch is marked inactive on the next `fetch`, but only when that
fetch proved it read the board. An answer listing nothing retires nothing until
the board has answered empty three fetches running, and a listing that was cut
short (a page cap, Workday's search stopping early) or shrank to under half of
the previous fetch's retires only postings unseen for 48 hours. `fetch` prints
a "Kept N unlisted posting(s) open" line whenever it holds rows back. Rows that arrive
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

Back to the [README](../../README.md) index.
