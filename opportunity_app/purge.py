"""python -m opportunity_app.purge, run by the daily task. The implementation is opportunity_app/opportunities/purge.py."""

from .opportunities.purge import main

if __name__ == "__main__":
    raise SystemExit(main())
