# Contributing

Thanks for improving the pipeline. Every copy of it holds one student's job
search, so the rules below are mostly about keeping that private and honest.

## Before your first commit

1. Fork, clone, and follow [`SETUP.md`](SETUP.md) through step 2. Running
   `python -m opportunity_app.setup init` turns on the git hooks in
   `.githooks/`, which refuse any commit or push that repeats your name,
   contact details, or keys from your own `config/` and `.env`. If you skip
   setup, turn them on yourself:

   ```bash
   git config core.hooksPath .githooks
   ```

2. Read [`AGENTS.md`](AGENTS.md), sections 1 and 2. They are short, and they
   are the rules a pull request is reviewed against.

## Keep personal data out

- Your `.env`, `config/profile.json`, `config/resume.json`,
  `config/sources.local.json`, everything under `data/` and `output/`, and
  `private/` are gitignored. Never force one in with `git add -f`; CI refuses
  it anyway.
- Test data is invented: `example.com` and `example.edu` addresses, `555-01xx`
  phone numbers, made-up names and employers. Don't paste a real posting's
  recruiter, a real resume, or a real screenshot of your dashboard.
- Don't bypass the hooks with `--no-verify` to get a commit through. If they
  flag something that isn't personal, reword it and say so in the PR.

## Making the change

- Keep a PR to one change. Explain what a student sees differently.
- Run `python -m unittest discover -s tests` before pushing. For frontend
  changes, also try it in the browser; the unit tests can't see `app.js`.
  The other suites are in [`AGENTS.md`](AGENTS.md#3-running-the-test-suites).
- `pipeline.py` and `pipeline_core/` must stay free of third-party
  dependencies. Web dependencies belong in `opportunity_app/`.
- Add a test that fails without your change.

## What review looks for

A PR runs on other students' machines, next to their tokens, their Gmail
connection, and their application history. Expect questions about:

- any new network destination, subprocess, file outside the project, or
  dependency;
- anything that sends, applies, or contacts someone without the student
  acting;
- anything that shows an inferred date, deadline, eligibility, or pay as
  confirmed;
- changes to authentication, `.githooks/`, `scripts/check_personal_data.py`,
  or `.github/workflows/`.

## Security problems

Don't open a public issue for a vulnerability or for personal data you find in
the repository. Report it privately through the repository's **Security** tab
(*Report a vulnerability*).
