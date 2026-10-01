#!/usr/bin/env python3
"""Local, source-linked internship discovery and application pipeline.

The code lives in pipeline_core/ (paths, config, http, text, sources, store, liveness, retention, discovery, fetch,
importers, scoring, reports, artifacts and cli). This file is only the entry point that `python pipeline.py ...`, the
scheduled tasks and the web app's subprocess calls run by path.
"""

from __future__ import annotations

from pipeline_core.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
