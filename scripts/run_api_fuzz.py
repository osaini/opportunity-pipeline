"""Fuzz every endpoint in the generated OpenAPI schema and report 5xx responses.

FastAPI publishes a complete schema at /openapi.json, so schemathesis can derive
inputs for every route without anyone writing a case. It is looking for the
class of bug unit tests rarely reach: a value that is valid per the schema but
crashes the handler, or a response that contradicts the declared type.

Schemathesis pins ``starlette<1`` and this project pins ``starlette==1.3.1``, so
the two cannot share an environment. Schemathesis only needs an HTTP URL, never
the application object, so it lives in its own virtualenv and this script wires
the two together: start the sandbox server with the app's interpreter, wait for
health, run the fuzzer with the other, then shut down.

    py -3 scripts/run_api_fuzz.py

Override the interpreters with APP_PYTHON and FUZZ_PYTHON when the layout
differs, as it does in CI.
"""

from __future__ import annotations

import argparse
import os
import platform
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
IS_WINDOWS = platform.system() == "Windows"
BIN = "Scripts" if IS_WINDOWS else "bin"
EXE = ".exe" if IS_WINDOWS else ""

DEFAULT_APP_PYTHON = REPO_ROOT / ".venv-ui" / BIN / f"python{EXE}"
DEFAULT_FUZZ_PYTHON = REPO_ROOT / ".venv-fuzz" / BIN / f"python{EXE}"

OWNER_TOKEN = "sandbox-owner-token"

# Endpoints excluded from fuzzing, with the reason. Keep this list honest: an
# entry is a route nobody is checking.
# Routes left out of the fuzz run, and why. schemathesis honours only the last
# --exclude-path-regex it is given, so these are combined into one alternation
# rather than passed as separate flags.
EXCLUDED = {
    "/api/v1/admin/.*": (
        "Mutating admin flags mid-run makes every later case meaningless rather "
        "than finding anything."
    ),
    "/api/v1/connections.*": (
        "The connector routes deliberately answer 503 until Google/Microsoft "
        "credentials and a webhook secret are configured, which a sandbox never "
        "has. schemathesis counts any 5xx as a crash, so these would be permanent "
        "false positives. tests/test_live_connectors.py covers them behind real "
        "credentials."
    ),
    "/api/v1/outreach/[^/]+/(draft|call-prep|find-contacts)$": (
        "Drafting and call prep run a model and finding contacts reads the target's website; "
        "fuzzed input would spend model quota and send requests to arbitrary "
        "domains. tests/test_outreach_drafting.py, tests/test_outreach_call_prep.py, and "
        "tests/test_outreach_discovery.py cover them against fakes."
    ),
}
EXCLUDED_PATTERN = "|".join(f"({pattern})" for pattern in EXCLUDED)

# Variables hypothesis.settings.is_in_ci() checks (hypothesis/_settings.py).
CI_ENVIRONMENT_MARKERS = (
    "CI", "__TOX_ENVIRONMENT_VARIABLE_ORIGINAL_CI", "TF_BUILD", "bamboo.buildKey",
    "BUILDKITE", "CIRCLECI", "CIRRUS_CI", "CODEBUILD_BUILD_ID", "GITHUB_ACTIONS",
    "GITLAB_CI", "HEROKU_TEST_RUN_ID", "TEAMCITY_VERSION",
)


def _wait_for_health(url: str, timeout: float = 60.0) -> None:
    deadline = time.monotonic() + timeout
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(f"{url}/api/v1/health", timeout=2) as response:
                if response.status == 200:
                    return
        except (urllib.error.URLError, OSError, TimeoutError) as exc:  # not up yet
            last_error = exc
        time.sleep(0.25)
    raise RuntimeError(f"sandbox server never became healthy at {url}: {last_error}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--port", type=int, default=8799)
    parser.add_argument(
        "--max-examples",
        type=int,
        default=25,
        help="Generated cases per operation. Raise for a deeper nightly run.",
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=int(os.environ.get("FUZZ_SEED", "20260823")),
        help=(
            "Generator seed. Fixed by default because schemathesis's filter_too_much "
            "health check is not suppressible and fails at random on unconstrained path "
            "parameters; pass a different seed to explore new inputs."
        ),
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help=(
            "Run every schemathesis check, not just the crash check. Adds response-schema "
            "and status-code conformance, which currently report a large backlog of "
            "undocumented responses — useful to work through, too noisy to gate on."
        ),
    )
    parser.add_argument(
        "--app-python",
        type=Path,
        default=Path(os.environ.get("APP_PYTHON", DEFAULT_APP_PYTHON)),
    )
    parser.add_argument(
        "--fuzz-python",
        type=Path,
        default=Path(os.environ.get("FUZZ_PYTHON", DEFAULT_FUZZ_PYTHON)),
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    # schemathesis 4 has no runnable __main__; it must be invoked through the
    # console script installed next to its interpreter.
    schemathesis = args.fuzz_python.parent / f"schemathesis{EXE}"
    for label, executable in (
        ("--app-python", args.app_python),
        ("--fuzz-python", args.fuzz_python),
        ("schemathesis", schemathesis),
    ):
        if not executable.exists():
            raise SystemExit(f"{label} does not exist: {executable}\nSee docs/ui-testing.md for setup.")

    url = f"http://127.0.0.1:{args.port}"
    server = subprocess.Popen(
        [str(args.app_python), str(REPO_ROOT / "scripts" / "serve_for_testing.py"), "--port", str(args.port)],
        cwd=REPO_ROOT,
    )
    try:
        _wait_for_health(url)
        command = [
            str(schemathesis), "run",
            f"{url}/openapi.json",
            "--url", url,
            "--header", f"Authorization: Bearer {OWNER_TOKEN}",
            "--max-examples", str(args.max_examples),
            "--seed", str(args.seed),
            # A local example database makes runs pass that fail on a fresh CI
            # runner; disable it so local and CI results agree.
            "--generation-database", "none",
            # Default to the crash check only. An unhandled exception is
            # unambiguously a bug; the conformance checks mostly report responses
            # the OpenAPI schema does not document, which is real but separate
            # work and would drown the signal. --strict turns the rest on.
            "--checks", "all" if args.strict else "not_a_server_error",
            # Hypothesis health checks report on the generator's ability to produce
            # data for a schema, not on the API's behaviour. They fail the run for
            # heavily-constrained request bodies, which says nothing about defects.
            "--suppress-health-check", "all",
            "--report", "junit",
            "--report-junit-path", str(REPO_ROOT / "data" / "api-fuzz-report.xml"),
        ]
        command += ["--exclude-path-regex", EXCLUDED_PATTERN]

        print("running:", " ".join(command), flush=True)
        # schemathesis derives its output encoding from the stream and produces
        # "utf-8:surrogateescape" — not a real codec name — when stdout is a pipe
        # on Windows. Pinning the variable sidesteps it on every platform.
        environment = {**os.environ, "PYTHONIOENCODING": "utf-8"}
        # Hypothesis loads its derandomized "ci" profile when it sees CI markers,
        # which trips an unsuppressible filter_too_much health check on
        # single-string-path operations. --seed already makes runs reproducible,
        # so hide the markers and get identical results locally and in CI.
        for marker in CI_ENVIRONMENT_MARKERS:
            environment.pop(marker, None)
        return subprocess.call(command, cwd=REPO_ROOT, env=environment)
    finally:
        server.terminate()
        try:
            server.wait(timeout=15)
        except subprocess.TimeoutExpired:
            server.kill()


if __name__ == "__main__":
    sys.exit(main())
