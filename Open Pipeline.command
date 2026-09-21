#!/bin/sh
# Double-click launcher for macOS (and any POSIX desktop): starts the local
# Opportunity Pipeline if needed and opens a signed-in browser tab.
cd "$(dirname "$0")" || exit 1
if [ -x .venv/bin/python ]; then PYTHON=.venv/bin/python; else PYTHON=python3; fi
exec "$PYTHON" -m opportunity_app.launch open
