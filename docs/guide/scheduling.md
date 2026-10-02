# Running it unattended

Keeping the local web app up, and running the daily pipeline on a schedule. The twice-weekly outreach deep search has its own schedule: see [Running the deep search on a schedule](outreach.md#running-the-deep-search-on-a-schedule).

## Keep the local web dashboard running

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

Back to the [README](../../README.md) index.
