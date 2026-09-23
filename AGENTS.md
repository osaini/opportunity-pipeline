# AGENTS.md — orientation for coding agents

Read this first. It is loaded automatically by OpenCode and is short on purpose;
everything deeper is linked.

Companion documents:

- [`SETUP.md`](SETUP.md) — **setting up a new student's copy**. If `config/profile.json`
  does not exist, or you are asked to install or personalize the pipeline, follow it.
- [`docs/ui-testing.md`](docs/ui-testing.md) — full reference for this project's bug-testing pipeline
- [`docs/bug-testing-playbook.md`](docs/bug-testing-playbook.md) — the general method, portable to any project

---

## 1. What this project is

A private opportunity workspace: one student
tracking internship, co-op, and research applications. Local-first, single-user,
loopback-only.

| Piece | Where | Notes |
| --- | --- | --- |
| Legacy pipeline + CLI | `pipeline.py` | No third-party dependencies. See rule 4 for the two allowed non-stdlib imports. |
| Shared read model | `pipeline_core/read_model.py` | Framework-neutral read model shared by CLI parity tests and the web app; standard library only. |
| Web API | `opportunity_app/` | FastAPI. `api.py` is ~2,300 lines and holds every route. |
| Frontend | `opportunity_app/static/` | Vanilla JS. No framework, no build step. `app.js` is ~110KB. |
| Browser extension | `apps/extension/` | Tested by `node tests/extension/run_tests.mjs`. |
| Schema | `migrations/*.sql` | SQLite by default; PostgreSQL supported. |

**The product's core promise is source integrity.** Every opportunity keeps its
source, its freshness, and an honest explanation of its score. Anything that
presents an inferred value as confirmed is the most serious class of bug here —
weight it above crashes.

## 2. Hard rules

1. **Never touch `data/platform.db`.** It holds real application history. Tests
   and sandboxes build their own throwaway copies. Never point a test, a fuzzer,
   or a browser at real data.
2. **`.env`, `config/resume.json`, `config/profile.json`, and
   `config/sources.local.json` are personal.** All are gitignored. Do not read
   `.env` or the resume into output, and do not commit any of them.
   `.githooks/` (enabled by `setup init`) refuses commits and pushes that repeat
   any of their values; do not bypass it with `--no-verify`. Everything under
   `data/` and `output/` is ignored by default.
3. **Loopback only.** Nothing in this repo should bind beyond `127.0.0.1`. The
   sandbox server refuses to.
4. **The legacy pipeline stays dependency-free.** `pipeline.py` and
   `pipeline_core/` run with no third-party packages installed. Beyond the
   standard library they may import only the stdlib-only
   `opportunity_app.opportunity_metadata` (lazily, for deadline parsing) and
   Playwright (optional PDF export, inside an `ImportError` guard).
   `tests/test_dependency_boundary.py` enforces this. Web dependencies belong in
   `opportunity_app/`.
5. **Do not silently fix a documented defect.** Section 5 lists known bugs, each
   pinned by a test. If you fix one, remove its marker in the same change and say
   so. If you are only testing, report and move on.

### Product invariants

- Never fabricate jobs, dates, eligibility, compensation, or deadlines.
- Never auto-apply. External delivery, such as the SMTP path in
  `notifications.py`, happens only through an explicitly configured live
  provider, never as a side effect an agent adds.
- Never bypass authentication or disable TLS verification for source fetching.
- Preserve existing application state and manual data.
- Keep scoring inspectable: every adjustment is recorded as a reason, and the
  final score is clamped to 0–100, so reasons account for the raw score and do
  not necessarily sum to the clamped score.
- Treat employer and university source pages as authoritative over aggregators.
- Make uncertainty visible instead of making consequential assumptions on the
  user's behalf.
- Keep the system inexpensive and maintainable for a single student.
- Personalize every feature. Several students run their own copies, so nothing
  about one student's situation, such as class year, school, programs, or tab
  names, is hardcoded in shipped code or copy. It lives in that student's
  gitignored config (`config/profile.json` or a `config/*.local.json`), the UI
  wording is driven by it, a missing file gets an honest empty state, and
  `SETUP.md` gains a step that produces it with the student. The commit and
  push hooks refuse the student's own school, degree, and
  `private/situation-terms.txt` terms in shipped code. That catches a leak, not
  a hardcoded assumption, so the rule still needs judgment.

## 3. Running the test suites

Four suites. The first is the fast default; run it after any change.

```bash
py -3 -m unittest discover -s tests
```

531 tests, API-level via `TestClient`. No browser. Needs `requirements-web.lock`
installed. `tests/ui/` is skipped automatically — it is not an importable
package, so `unittest discover` does not recurse into it.

This is the primary, always-supported command. On a multi-core machine the same
tests also run in parallel, which takes roughly three minutes down to under one:

```bash
py -3 -m pip install -r requirements-test.txt   # once
py -3 -m pytest -c pytest-unit.ini -n auto
```

`pytest-unit.ini` is deliberately separate from `pytest.ini` so a unit run never
picks up the browser suite's options. It skips `tests/test_postgres.py`, which
resets a shared schema in `setUpClass` and so cannot be split across workers;
It also skips `tests/test_scheduled_tasks.py`, which kills real processes on a timer and flakes under parallel load. Both files still run under the `unittest` command above.

```bash
.venv-ui/Scripts/python -m pytest tests/ui -q
```

203 browser tests: page smoke, student journey, outreach journey, axe accessibility, keyboard and
focus, responsive matrix. Needs the separate `.venv-ui` environment. Call the
interpreter directly rather than the PowerShell wrapper — it works in any shell
and sidesteps execution policy.

```bash
py -3 scripts/run_api_fuzz.py
```

Property-based fuzzing of all 148 OpenAPI operations, looking for unhandled
exceptions. Starts and stops its own sandbox server.

```bash
node tests/extension/run_tests.mjs
```

Browser-extension unit tests.

**First-time setup** for the browser and fuzz suites is in
[`docs/ui-testing.md`](docs/ui-testing.md#setup). They need two separate
virtualenvs because schemathesis pins `starlette<1` while this project pins
`starlette==1.3.1` — they cannot share a resolver.

## 4. Where each suite can and cannot see

This is the part worth internalising, because it explains where bugs hide.

| Suite | Sees | Blind to |
| --- | --- | --- |
| `tests/` (unittest) | Routes, DB, auth, business logic | Anything in `app.js` or `styles.css`. Authenticates with a bearer token, so it never exercises the CSRF path, which only applies to cookie-authenticated browser requests. |
| `tests/ui/` (Playwright) | Real rendering, real event handlers, real cookies, console and network | Server internals; anything behind a feature flag or credential it does not have |
| `scripts/run_api_fuzz.py` | Every operation in the schema, with generated input | Anything requiring a valid multi-step sequence; connector routes and the outreach draft/find-contacts routes are excluded |
| `node --check` in CI | That `app.js` parses | Whether any of it runs |

A P0 bug lived in the gap between rows one and two from the day the platform
landed (`d592e85`, 2026-08-10) until the browser suite was added: `app.js` dropped
the CSRF header on every write, so Save and Pass were completely broken in the
browser while every API test passed. The CSRF middleware and the helper that
defeats it were written in the same commit. When you are asked "is this covered?",
answer with the row, not the test count.

## 5. Defect regression status

The phase-verification pass on 2026-08-23 fixed the previously documented CSRF,
Unicode credential, deep-link, mobile sign-out, accessibility, CLI provider
boundary, and sensitive-field extension defects. Their tests are now ordinary
live regression guards; there are no expected-failure or named known-defect pins.
Historical reproductions remain in
[`docs/ui-testing.md`](docs/ui-testing.md#defects-found-and-fixed).

If a future defect is pinned with a strict `xfail`, `expectedFailure`, or named
quarantine, remove the marker in the same change as the fix. A stale pin should
fail the build.

## 6. Exploratory testing

For hunting what the written assertions did not anticipate, start the sandbox:

```bash
py -3 scripts/serve_for_testing.py
```

It seeds a throwaway database from the same fixture the unittest suite uses,
prints fixed tokens, and serves `http://127.0.0.1:8799`. Sign in by pasting
`sandbox-owner-token` into the **Owner invitation** field. Two opportunities are
seeded: Acme Robotics (already saved) and Orbit Systems (unsaved).

Drive it with whatever browser automation your harness provides. For Claude Code
that is the `playwright` MCP server in `.mcp.json`, scoped to that origin; a
`ui-reviewer` subagent in `.claude/agents/` is set up for it. For OpenCode,
configure a Playwright MCP server the same way:

```json
{
  "mcp": {
    "playwright": {
      "type": "local",
      "command": ["node", "scripts/playwright-mcp.mjs", "--browser", "chromium",
                  "--isolated", "--allowed-origins", "http://127.0.0.1:8799"],
      "enabled": true
    }
  }
}
```

`@playwright/mcp` is pinned in `package.json`. Launch it through
`scripts/playwright-mcp.mjs`, never bare `npx`: the launcher installs the pinned
packages and Chromium when a fresh clone or worktree lacks them, where `npx`
would download at editor launch and time out.
Keep `--allowed-origins` set — an exploring agent should not be able to navigate
off the sandbox.

## 7. Method

When you are hunting bugs rather than building, follow
[`docs/bug-testing-playbook.md`](docs/bug-testing-playbook.md). The short version:

1. Find the **coverage seam** — the layer where one suite stops and the next has
   not started. That is where bugs survive.
2. Reproduce before diagnosing. A failing test you can run beats a theory.
3. Verify your instrument before trusting its output. A test that reports a bug
   is a claim about the test as much as the code.
4. Pin every confirmed defect with a test that fails now and passes when fixed.
5. Report; do not quietly patch.
