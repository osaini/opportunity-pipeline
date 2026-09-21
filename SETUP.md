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
   waitlisted**. Skipping it loses only an optional second-opinion panel.
5. **Stay on this machine.** Never bind the server beyond `127.0.0.1`, and never
   commit `.env`, `config/profile.json`, `config/sources.local.json`,
   `config/resume.json`, or anything in `data/`. All of these are gitignored
   already.

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
outreach deep search. It is safe to run again, and never overwrites anything
already set.

## 3. Interview the student and write the profile

Ask these questions conversationally, a few at a time, then write the answers
into `config/profile.json`. The field names are the keys in
`config/profile.example.json`.

| Ask | Field | Notes |
| --- | --- | --- |
| Name, school, degree | `name`, `school`, `degree` | e.g. "B.S. Chemical Engineering" |
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
| Home during breaks and summers | `break_location` | "City, ST"; used only for the outreach "I'm based in…" line |
| Pay expectations | `compensation_preferences` | free text or `null` |

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

## 5. Optional keys

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
  the deep search and drafting. `ANTHROPIC_API_KEY` or `OPENAI_API_KEY` is only
  for the in-app career agent, and is pay per use.
- **Jev** (`TYPESAFE_API_KEY`): optional and waitlisted; skip it freely.
- **Gmail drafts**: the student needs their own Google Cloud OAuth client. Walk
  them through README.md → "Gmail drafts with an attachment". They also set
  `PIPELINE_OUTREACH_ACCOUNT` to their address and `PIPELINE_OUTREACH_COMPOSE=gmail`.
  `PIPELINE_CONNECTION_KEY` was already generated in step 2.

## 6. Resume (optional, recommended)

Copy `config/resume.example.json` to `config/resume.json` and fill it in from
the student's resume, if they share one. Copy facts exactly; never embellish.
The resume and cover-letter commands only reformulate what is in that file.

## 7. First run and daily use

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

## 8. Updating later

```bash
git pull
python -m pip install -r requirements-web.lock
python -m opportunity_app.setup init        # adds any new settings; keeps everything else
python -m opportunity_app.launch restart
```

Personal files are gitignored, so a pull never touches them.

If you commit changes of your own, the hooks from step 2 check each commit and
push against your resume, profile, and `.env`, and refuse if any of it would be
published. Add employers, contacts, or anything else to keep private, one per
line, in `private/blocked-terms.txt`. Audit the whole history at any time with
`python scripts/check_personal_data.py --all`.

## Troubleshooting

| Symptom | Fix |
| --- | --- |
| `Missing config/profile.json` | `python -m opportunity_app.setup init` |
| Browser shows the sign-in page | Open the app through the launcher, not a bookmark. Or paste `PIPELINE_WEB_TOKEN` from `.env` into **Owner token**. |
| "did not accept the token in .env" | The running server was started with another token: `python -m opportunity_app.launch restart` |
| A source errors every run | `python -m opportunity_app.setup status` shows sources that need keys; add the key or disable the source. |
| Nothing scores high | Check `regions`, `degree_keywords`, and `interest_keywords`. Every score comes with its reasons in the dashboard. |
