"""Start, open, and schedule the local app on Windows, macOS, or Linux.

    python -m opportunity_app.launch open       start if needed, then sign the browser in
    python -m opportunity_app.launch start      start the server in the background
    python -m opportunity_app.launch stop       stop a server this launcher started
    python -m opportunity_app.launch restart    stop, then start (picks up code edits)
    python -m opportunity_app.launch status
    python -m opportunity_app.launch install-autostart | install-daily | install-outreach
    python -m opportunity_app.launch uninstall  remove every scheduled job this installs

``open`` is the one-click sign-in: it reads PIPELINE_WEB_TOKEN from .env on
this machine, trades it for a one-time ticket (POST /api/v1/auth/launch-ticket),
and opens the browser at ``#launch=<ticket>``. The token itself is never
printed, put in a URL, or written to a log.

Scheduling uses what each system ships with. On Windows it is Task Scheduler,
through the existing scripts/install-*-task.ps1. On macOS it is a launchd
agent in ~/Library/LaunchAgents, and on Linux a systemd user unit.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import shutil
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path
from typing import Any
from xml.sax.saxutils import escape

from . import ROOT

HOST = "127.0.0.1"
DEFAULT_PORT = 8765
DATA_DIR = ROOT / "data"
PID_PATH = DATA_DIR / "web.pid"
WEB_LOG = DATA_DIR / "web.log"
ENV_PATH = ROOT / ".env"
LABEL_PREFIX = "com.opportunity-pipeline"
UNIT_PREFIX = "opportunity-pipeline"
WINDOWS_TASKS = {
    "autostart": ("install-web-task.ps1", "internship-pipeline-web"),
    "daily": ("install-daily-task.ps1", "internship-pipeline"),
    "outreach": ("install-outreach-task.ps1", "internship-pipeline-outreach"),
}
# Lines the server prints that carry a credential. They never reach web.log.
SECRET_LINE = re.compile(r"(access token|api token)\s*:", re.IGNORECASE)


# -- server lifecycle ------------------------------------------------------------


def health(port: int = DEFAULT_PORT, timeout: float = 2.0) -> bool:
    try:
        with urllib.request.urlopen(f"http://{HOST}:{port}/api/v1/health", timeout=timeout) as response:
            return bool(json.loads(response.read().decode("utf-8")).get("ok"))
    except (OSError, ValueError):
        return False


def _read_env_token() -> str:
    from pipeline import load_env_file

    load_env_file(ENV_PATH)
    return os.environ.get("PIPELINE_WEB_TOKEN", "")


def _python_for_background() -> str:
    # pythonw has no console window on Windows; elsewhere the plain interpreter.
    if sys.platform == "win32":
        candidate = Path(sys.executable).with_name("pythonw.exe")
        if candidate.exists():
            return str(candidate)
    return sys.executable


def start(port: int = DEFAULT_PORT, wait_seconds: float = 30.0) -> bool:
    if health(port):
        return True
    if not _read_env_token():
        raise SystemExit(
            "PIPELINE_WEB_TOKEN is not set in .env, so the launcher could not sign you in. "
            "Run `python -m opportunity_app.setup init` first."
        )
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    command = [_python_for_background(), "-m", "opportunity_app.launch", "serve", "--port", str(port)]
    options: dict[str, Any] = {
        "cwd": str(ROOT),
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "close_fds": True,
    }
    if sys.platform == "win32":
        options["creationflags"] = (
            subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
        )
    else:
        options["start_new_session"] = True
    subprocess.Popen(command, **options)
    deadline = time.monotonic() + wait_seconds
    while time.monotonic() < deadline:
        if health(port):
            return True
        time.sleep(0.5)
    return False


def launch_url(port: int = DEFAULT_PORT, path: str = "/") -> str:
    request = urllib.request.Request(
        f"http://{HOST}:{port}/api/v1/auth/launch-ticket",
        method="POST",
        headers={"Authorization": f"Bearer {_read_env_token()}"},
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            ticket = json.loads(response.read().decode("utf-8"))["ticket"]
    except urllib.error.HTTPError as exc:
        if exc.code == 401:
            raise SystemExit(
                "The running server did not accept the token in .env. It was probably started "
                "with a different token; run `python -m opportunity_app.launch restart`."
            ) from exc
        raise
    return f"http://{HOST}:{port}{path}#launch={ticket}"


def open_browser(port: int = DEFAULT_PORT, path: str = "/") -> int:
    if not start(port):
        print(f"The server did not come up; see {WEB_LOG}.", file=sys.stderr)
        return 1
    url = launch_url(port, path)
    if not webbrowser.open(url):
        # No browser could be opened (a headless session): the ticket still works
        # for the next 60 seconds, and it is single use.
        print(f"Open this link within a minute: {url}")
    return 0


def _pid_alive(pid: int) -> bool:
    if sys.platform == "win32":
        output = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/NH"], capture_output=True, text=True, check=False
        ).stdout
        return str(pid) in output
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def stop(port: int = DEFAULT_PORT) -> bool:
    try:
        pid = int(PID_PATH.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        pid = 0
    if not pid or not _pid_alive(pid):
        PID_PATH.unlink(missing_ok=True)
        if health(port):
            print(
                "A server is running, but not one this launcher started (for example the "
                "Windows scheduled task). Stop it from there, or end its python process."
            )
        return False
    if sys.platform == "win32":
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True, check=False)
    else:
        os.kill(pid, signal.SIGTERM)
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and health(port, timeout=0.5):
        time.sleep(0.3)
    PID_PATH.unlink(missing_ok=True)
    return True


class _RedactingLog:
    """A text stream that appends to web.log and drops credential lines."""

    def __init__(self, path: Path) -> None:
        self._handle = path.open("a", encoding="utf-8", buffering=1)
        self._pending = ""

    def write(self, text: str) -> int:
        self._pending += text
        *lines, self._pending = self._pending.split("\n")
        for line in lines:
            if not SECRET_LINE.search(line):
                self._handle.write(line + "\n")
        return len(text)

    def flush(self) -> None:
        self._handle.flush()

    def isatty(self) -> bool:
        return False


def serve(port: int = DEFAULT_PORT) -> int:
    """Run the server in this process, logging to data/web.log. launchd and systemd call this."""
    import uvicorn

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    stream = _RedactingLog(WEB_LOG)
    sys.stdout = sys.stderr = stream  # type: ignore[assignment]
    logging.getLogger("opportunity_app").addFilter(lambda record: not SECRET_LINE.search(record.getMessage()))
    PID_PATH.write_text(str(os.getpid()), encoding="utf-8")
    try:
        from .api import LOOPBACK_HOSTS, create_app

        app = create_app(allowed_hosts=list(LOOPBACK_HOSTS))
        print(f"Serving http://{HOST}:{port} (PID {os.getpid()})")
        uvicorn.run(app, host=HOST, port=port, log_level="info")
    finally:
        try:
            if PID_PATH.read_text(encoding="utf-8").strip() == str(os.getpid()):
                PID_PATH.unlink()
        except OSError:
            pass
    return 0


# -- scheduling ------------------------------------------------------------------


def job_command(job: str, port: int = DEFAULT_PORT) -> list[str]:
    python = sys.executable
    if job == "autostart":
        return [python, "-m", "opportunity_app.launch", "serve", "--port", str(port)]
    if job == "daily":
        return [python, "-m", "opportunity_app.daily", "--scheduled"]
    if job == "outreach":
        return [python, "-m", "opportunity_app.launch", "outreach-scheduled"]
    raise ValueError(job)


def launchd_plist(job: str, command: list[str], root: Path = ROOT) -> str:
    """A launchd agent for one job. The daily run polls every 30 minutes, like the
    Windows task's retry trigger, and its --scheduled mode makes that idempotent."""
    schedule = {
        "autostart": "  <key>RunAtLoad</key><true/>\n  <key>KeepAlive</key><true/>\n",
        "daily": "  <key>RunAtLoad</key><true/>\n  <key>StartInterval</key><integer>1800</integer>\n",
        "outreach": (
            "  <key>StartCalendarInterval</key>\n  <array>\n"
            + "".join(
                f"    <dict><key>Weekday</key><integer>{day}</integer>"
                "<key>Hour</key><integer>7</integer><key>Minute</key><integer>0</integer></dict>\n"
                for day in (1, 4)
            )
            + "  </array>\n"
        ),
    }[job]
    arguments = "".join(f"    <string>{escape(part)}</string>\n" for part in command)
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
        '<plist version="1.0">\n<dict>\n'
        f"  <key>Label</key><string>{LABEL_PREFIX}.{job}</string>\n"
        f"  <key>ProgramArguments</key>\n  <array>\n{arguments}  </array>\n"
        f"  <key>WorkingDirectory</key><string>{escape(str(root))}</string>\n"
        f"{schedule}"
        "  <key>StandardOutPath</key><string>/dev/null</string>\n"
        "  <key>StandardErrorPath</key><string>/dev/null</string>\n"
        "</dict>\n</plist>\n"
    )


def systemd_units(job: str, command: list[str], root: Path = ROOT) -> dict[str, str]:
    """A systemd user service for one job, plus a timer for the scheduled ones."""
    name = f"{UNIT_PREFIX}-{job}"
    exec_start = " ".join(f'"{part}"' if " " in part else part for part in command)
    service = (
        f"[Unit]\nDescription=Opportunity pipeline: {job}\n\n"
        f"[Service]\nWorkingDirectory={root}\nExecStart={exec_start}\n"
    )
    if job == "autostart":
        service += "Restart=on-failure\n\n[Install]\nWantedBy=default.target\n"
        return {f"{name}.service": service}
    service += "Type=oneshot\n"
    calendar = "*:0/30" if job == "daily" else "Mon,Thu 07:00"
    timer = (
        f"[Unit]\nDescription=Opportunity pipeline: {job} schedule\n\n"
        f"[Timer]\nOnCalendar={calendar}\nPersistent=true\n\n[Install]\nWantedBy=timers.target\n"
    )
    return {f"{name}.service": service, f"{name}.timer": timer}


def _run(command: list[str]) -> int:
    return subprocess.run(command, check=False).returncode


def install(job: str) -> int:
    command = job_command(job)
    if sys.platform == "win32":
        script, _task = WINDOWS_TASKS[job]
        return _run([
            "powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass",
            "-File", str(ROOT / "scripts" / script),
        ])
    if sys.platform == "darwin":
        agents = Path.home() / "Library" / "LaunchAgents"
        agents.mkdir(parents=True, exist_ok=True)
        plist = agents / f"{LABEL_PREFIX}.{job}.plist"
        domain = f"gui/{os.getuid()}"
        subprocess.run(["launchctl", "bootout", domain, str(plist)], capture_output=True, check=False)
        plist.write_text(launchd_plist(job, command), encoding="utf-8")
        code = _run(["launchctl", "bootstrap", domain, str(plist)])
        if code == 0:
            print(f"Installed {plist}")
        return code
    units_dir = Path.home() / ".config" / "systemd" / "user"
    units_dir.mkdir(parents=True, exist_ok=True)
    units = systemd_units(job, command)
    for name, text in units.items():
        (units_dir / name).write_text(text, encoding="utf-8")
    _run(["systemctl", "--user", "daemon-reload"])
    enabled = next(name for name in units if name.endswith(".timer")) if len(units) > 1 else next(iter(units))
    return _run(["systemctl", "--user", "enable", "--now", enabled])


def uninstall() -> int:
    if sys.platform == "win32":
        for _script, task in WINDOWS_TASKS.values():
            _run([
                "powershell.exe", "-NoProfile", "-Command",
                f"Unregister-ScheduledTask -TaskName '{task}' -Confirm:$false -ErrorAction SilentlyContinue",
            ])
        return 0
    if sys.platform == "darwin":
        agents = Path.home() / "Library" / "LaunchAgents"
        for job in WINDOWS_TASKS:
            plist = agents / f"{LABEL_PREFIX}.{job}.plist"
            if plist.exists():
                subprocess.run(["launchctl", "bootout", f"gui/{os.getuid()}", str(plist)], capture_output=True, check=False)
                plist.unlink()
        return 0
    units_dir = Path.home() / ".config" / "systemd" / "user"
    for job in WINDOWS_TASKS:
        for suffix in (".timer", ".service"):
            unit = units_dir / f"{UNIT_PREFIX}-{job}{suffix}"
            if unit.exists():
                subprocess.run(["systemctl", "--user", "disable", "--now", unit.name], capture_output=True, check=False)
                unit.unlink()
    _run(["systemctl", "--user", "daemon-reload"])
    return 0


def outreach_scheduled() -> int:
    """The Monday/Thursday deep search, then the location backfill (run-outreach-discovery.ps1)."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    log = DATA_DIR / "outreach-discovery.log"
    env = dict(os.environ, PYTHONUTF8="1")
    with log.open("a", encoding="utf-8") as handle:
        handle.write(f"=== {time.strftime('%Y-%m-%dT%H:%M:%S')} deep search ===\n")
        handle.flush()
        code = subprocess.call(
            [sys.executable, "-m", "opportunity_app.outreach_cli", "discover", "--trigger", "scheduled"],
            cwd=str(ROOT), env=env, stdout=handle, stderr=subprocess.STDOUT,
        )
        handle.write(f"=== finished with exit code {code} ===\n")
        handle.flush()
        subprocess.call(
            [sys.executable, "-m", "opportunity_app.outreach_cli", "enrich", "--limit", "15"],
            cwd=str(ROOT), env=env, stdout=handle, stderr=subprocess.STDOUT,
        )
    # 75 means another deep search was already running, which is not a failure.
    return 0 if code == 75 else code


def status(port: int = DEFAULT_PORT) -> int:
    running = health(port)
    print(f"Server: {'running' if running else 'stopped'} on http://{HOST}:{port}")
    if PID_PATH.exists():
        print(f"Started by this launcher: PID {PID_PATH.read_text(encoding='utf-8').strip()}")
    print(f"Sign-in token in .env: {'yes' if _read_env_token() else 'MISSING (run setup init)'}")
    return 0 if running else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    sub = parser.add_subparsers(dest="command", required=True)
    opened = sub.add_parser("open", help="Start if needed and open a signed-in browser tab")
    opened.add_argument("--path", default="/", help="Page to open, e.g. /saved")
    for name in ("start", "stop", "restart", "status", "install-autostart", "install-daily",
                 "install-outreach", "uninstall"):
        sub.add_parser(name)
    serve_parser = sub.add_parser("serve", help=argparse.SUPPRESS)
    serve_parser.add_argument("--port", type=int, default=DEFAULT_PORT, dest="serve_port")
    sub.add_parser("outreach-scheduled", help=argparse.SUPPRESS)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "open":
        return open_browser(args.port, args.path)
    if args.command == "start":
        return 0 if start(args.port) else 1
    if args.command == "stop":
        stop(args.port)
        return 0
    if args.command == "restart":
        stop(args.port)
        return 0 if start(args.port) else 1
    if args.command == "status":
        return status(args.port)
    if args.command == "serve":
        return serve(args.serve_port)
    if args.command == "outreach-scheduled":
        return outreach_scheduled()
    if args.command == "uninstall":
        return uninstall()
    return install(args.command.removeprefix("install-"))


if __name__ == "__main__":
    raise SystemExit(main())
