# scripts/

An index, not a plan to reorganise. Several of these paths are baked into things outside the repository (a student's
installed scheduled tasks, an agent's documented command, the CI workflow), so **do not move or rename a file here**
without a forwarding stub at the old path. Moving a `.vbs` file silently breaks every installed task until the student
reinstalls it.

Run Python scripts with `py -3` on Windows and `python3` on macOS and Linux.

## Scheduled-task launchers (Windows)

`python -m opportunity_app.launch install-autostart | install-daily | install-outreach` runs the matching installer
below on Windows (`launch.WINDOWS_TASKS`). On macOS and Linux the same commands write a launchd agent or a systemd user
unit instead, and those run `python -m opportunity_app.*` directly, so none of the files in this group are used there.

| Installed task | Installer | Runs (hidden, through the `.vbs` shim) | What it does |
| --- | --- | --- | --- |
| `internship-pipeline-web` | `install-web-task.ps1` | `start-web.vbs` then `start-web.ps1` | Starts the local web app at sign-in, checks every minute, restarts it up to three times after a failure, and creates the private role tokens in `.env` if they are missing. |
| `internship-pipeline` | `install-daily-task.ps1` | `run-daily.vbs` then `run-daily.ps1` | The unattended daily run: `pipeline.py run`, a bounded liveness check, expired-posting purges, the platform sync and outreach reminders. Checkpoints to `data/daily-run.json`, so a run survives the laptop closing. `python -m opportunity_app.daily` is the same run for macOS and Linux. |
| `internship-pipeline-outreach` | `install-outreach-task.ps1` | `run-outreach-discovery.vbs` then `run-outreach-discovery.ps1` | The twice-weekly outreach deep search (`python -m opportunity_app.outreach_cli discover`). It never sends email. `-DryRun` tries it without changing anything. Also starts at sign-in, unlock and wake with `-Days Monday,Thursday -At 07:00`; `outreach_cli` exits 76 (silently, no backfill) when the newest slot already has its run, so a slot missed with the computer off is searched once, when the computer is opened. A task registered without those arguments still works. |

The `.vbs` files exist because registering `powershell.exe` as a task action shows a blank console window on every run,
and closing it kills the run. Each shim starts the PowerShell script hidden and passes its exit code through.

## Launchers a person double-clicks

| File | What it does |
| --- | --- |
| `open-web.ps1` | Run by `Open Pipeline.vbs` in the project root: installs or starts the web task, waits for the health endpoint, and opens the browser already signed in. (`Open Pipeline.command` does the same on macOS through `python -m opportunity_app.launch open`.) |

## Agent and developer tools

| File | What it does | Used by |
| --- | --- | --- |
| `pipeline_mailbox.py` | Reads the pipeline's Gmail mailbox, read-only (`whoami`, `search`, `thread`). The sanctioned way for an agent to look at outreach mail; the reading itself is `opportunity_app/pipeline_mailbox.py`. | [`AGENTS.md`](../AGENTS.md) hard rule 6; `tests/test_pipeline_mailbox.py` |
| `serve_for_testing.py` | Serves the app on `127.0.0.1:8799` against a disposable, seeded database. | Exploratory testing, the fuzzer; `AGENTS.md` section 6 |
| `run_api_fuzz.py` | Starts the sandbox server and fuzzes every operation in the OpenAPI schema with schemathesis, looking for unhandled exceptions. Needs the second virtualenv in [`docs/ui-testing.md`](../docs/ui-testing.md). | CI job `api-fuzz` |
| `ui-test.ps1` | Wraps pytest for the browser suite with the `.venv-ui` interpreter; `-Setup` builds the environment. Anything scripted should call `.venv-ui/Scripts/python -m pytest tests/ui` directly. | [`docs/ui-testing.md`](../docs/ui-testing.md) |
| `playwright-mcp.mjs` | Starts the pinned Playwright MCP server, installing the pinned packages and Chromium first when a fresh clone or worktree lacks them. Never launch the MCP server with a bare `npx`. | `.mcp.json`; `tests/test_mcp_config.py` |
| `check-js-syntax.mjs` | Runs `node --check` over every browser, extension, Node-test, script and hook file by directory, so a new file needs no edit. | CI job `test`; `npm run check` |
| `check_personal_data.py` | Refuses a commit or push that repeats this student's own details or secrets, and files that must never be tracked. | `.githooks/` (turned on by `setup init`); CI job `secrets` (`--all`) |
| `eval_second_read.py` | Runs fake pages and a real model through the research check's second read and prints how each fact was decided. Calls a model, so it is not part of any suite. | By hand, after changing the research check's instructions |

## Why these stay put

- The three installers, the three `.vbs` files and the three `.ps1` scripts they run are named in installed Task Scheduler
  entries and in `launch.WINDOWS_TASKS`. `tests/test_scheduled_tasks.py` and `tests/test_entry_points.py` check them.
- `pipeline_mailbox.py` is the path `AGENTS.md`, the student's notes and an error string in `tests/test_pipeline_mailbox.py`
  all name.
- `playwright-mcp.mjs` is named in `.mcp.json`, and `check_personal_data.py` in `.githooks/_run-check` and CI.
- This folder is a shipped path for the personal-data check, so nothing here may carry one student's details.
