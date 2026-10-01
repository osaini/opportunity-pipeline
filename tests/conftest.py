"""pytest configuration shared by the unit run (pytest-unit.ini) and the browser suite (pytest.ini, tests/ui).

This file sits above tests/ui, so pytest loads it for both. `python -m unittest discover` does not read it; that path is guarded
by tests/test_real_data_guard.py and tests/helpers_platform.py importing the same module.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import realdata_guard  # noqa: E402

# Before any test module is collected: from here on, no test in this process can open a database file under data/.
realdata_guard.install()
