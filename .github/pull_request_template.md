## What this changes

<!-- One or two sentences: what a student sees differently, and why. -->

## How you checked it

<!-- Commands you ran and what you clicked through. Paste failures honestly. -->

- [ ] `python -m unittest discover -s tests` passes
- [ ] If you touched `opportunity_app/static/`, I tried it in the browser (or ran `tests/ui`)

## Personal data

- [ ] No real names, emails, phone numbers, resumes, contacts, or keys, mine or anyone else's. Test data is invented (`example.com`, `555-01xx` numbers).
- [ ] I ran `python -m opportunity_app.setup init` (or `git config core.hooksPath .githooks`), so the personal-data hooks checked my commits.

## Product rules (AGENTS.md)

- [ ] Nothing presents an inferred date, deadline, eligibility, or pay as confirmed.
- [ ] Nothing applies, sends email, or contacts anyone without the student acting.
- [ ] No new network destination, background job, or dependency. If there is one, it's named and explained here.
