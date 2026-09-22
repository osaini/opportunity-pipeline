---
name: setup-pipeline
description: Set up a fresh copy of this opportunity pipeline for a new student - virtualenv, .env, profile interview, sources, researched programs for their stage, optional keys, first run, and one-click launcher. Use when someone asks to set up, install, configure, or personalize the pipeline, or when config/profile.json does not exist yet.
---

Follow [SETUP.md](../../../SETUP.md) from the top, in order, with the student.

Its "Rules for the agent" section is binding. In short:
- Ask about eligibility, availability, and field. Never infer them.
- Add only job boards that `pipeline.py discover-ats` confirmed.
- Never read `.env` into the chat or ask for a key in the chat. The student runs
  `! python -m opportunity_app.setup set-key NAME` themselves.
- Everything but Python is optional. Jev is waitlisted; skipping it is fine.
- Personalize every feature. Step 5 researches the Programs tab's list for this
  student's class year and field. Confirm each program on its official page
  (mark it `unverified` if the page won't open), never estimate a date, and
  check the file with
  `python -m opportunity_app.setup programs`.

Use `python -m opportunity_app.setup status --json` to see where things stand
at any point, and `python -m opportunity_app.setup validate` after every edit
to `config/profile.json`.
