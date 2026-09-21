# A general bug-testing pipeline

A method for finding bugs that existing tests structurally cannot see. It is
project-independent; the worked examples come from this repository, where the
method found a P0 in the first twenty minutes.

It is written for an agent, but nothing in it depends on being one.

---

## The premise

A test suite with high pass rates tells you about the code paths someone thought
to write a test for. Bugs accumulate in the space between suites — where one
layer stops verifying and the next one assumes.

So the pipeline does not start by writing tests. It starts by finding the gap.

---

## Stage 0 — Map the coverage seams

Before writing anything, build a table: for each existing suite, what it sees and
what it is blind to. Be specific about *why* it is blind, because the reason
predicts the bug.

Worked example from this repo:

| Suite | Sees | Blind to | Why |
| --- | --- | --- | --- |
| 197 unittest tests | Routes, DB, auth, logic | All frontend behaviour | Uses an in-process `TestClient`; no browser, no `app.js` |
| `node --check app.js` | That the file parses | Whether any of it runs | It is a syntax check |

The seam is obvious once written down: **110KB of JavaScript that nothing
executed.** And the P0 was exactly there — `app.js` dropped the CSRF header on
every write, so Save and Pass were completely broken in the browser while every
API test passed.

Sharpen the seam with a second question: *what does the passing suite do
differently from a real user?* Here, the tests authenticated with a bearer token.
The CSRF check only applies to cookie-authenticated requests carrying an `Origin`
header — that is, only to real browsers. The suite could not have caught it no
matter how many cases were added.

Common seams worth checking:

- Server tests that never render the client
- Client tests that mock the server
- Anything authenticated differently in tests than in production
- Error paths, retries, and offline queues — usually only the happy path is driven
- Everything below the smallest viewport anyone opened
- Concurrency, ordering, and state that survives a reload

**Output of this stage:** one sentence naming the widest seam. Everything after
this is aimed at it.

## Stage 1 — Instrument passively before asserting actively

The highest-yield instrument is not a test. It is a listener that fails *any*
test whose run produced a runtime failure, whether or not the test looked for one.

For a web app: uncaught exceptions, console errors, failed requests, and any
4xx/5xx. Attach it to every test automatically.

This is what found the P0. No assertion asked "did the save request succeed?" —
a smoke test navigated a page, the listener saw a `403`, and the run turned red.

Two rules make it survivable:

- **Allow-list by cause, with the reason at the definition.** This project allows
  a `401` on the session bootstrap (the auth gate probing on load) and
  `net::ERR_ABORTED` (a navigation cancelling an in-flight fetch). Both are the
  system working. Neither is a blanket mute.
- **Give tests an explicit opt-out** for deliberately provoking errors, and make
  those tests assert on what they captured.

The analogue outside the browser: fail on any log at ERROR, any unhandled task
exception, any warning from the DB driver.

## Stage 2 — Make the environment deterministic before trusting a failure

An intermittent test cannot distinguish a bug from itself. Fix the environment
first, or you will spend the session debugging your instrument.

Four things to settle:

1. **Own data.** Build a throwaway database from a fixture. Never point tests at
   real data — not for reads either, because then results depend on it.
2. **Reset between tests.** State that leaks makes failures order-dependent. In
   this repo one uvicorn instance serves the whole session (a fixture-scoping
   constraint), so isolation comes from restoring a pristine copy of the database
   file before each test. Restoring an opaque snapshot beats enumerating tables:
   it does not rot when a migration adds one.
3. **Turn off defences that create noise, and say why.** The per-IP rate limiter
   here sees a whole test suite as one client and starts returning 429 partway
   through. It is raised for tests and covered separately. Document the trade —
   a silently disabled protection is how it stops being tested at all.
4. **Redirect side effects.** File uploads, caches, and temp storage should land
   in the temp tree, not the repo.

## Stage 3 — Drive the real thing

Now write tests that exercise the seam. Two principles carry most of the value:

**Assert what a user perceives, not what the DOM contains.** "The card says Saved"
is a real claim. "An element with class `is-active` exists" survives the feature
being broken.

**Test the honesty of failure, not just success.** The most valuable test written
here asserts that a rejected write never leaves a success label on screen. It
passes today, and it will keep passing only as long as nobody adds optimistic UI
that outruns the server.

Cover the axes that break silently: keyboard-only paths, the accessibility tree,
the smallest viewport, deep links and reload, and every state after a hard
refresh.

## Stage 4 — Generate input nobody would think of

Property-based fuzzing reaches the cases you cannot enumerate. If the app
publishes a schema — OpenAPI, GraphQL, protobuf — a fuzzer can derive inputs for
every operation without anyone writing a case.

**The whole difficulty is signal management.** First run here: 116 failures. Real
crashes: 2. The rest were undocumented status codes and schema-conformance gaps —
genuine work, but a different kind, and enough noise to bury the crashes.

The rule: **gate on the check that is unambiguously a defect, and make the rest
opt-in.** An unhandled exception is always a bug. A response the schema does not
document might be. So the default runs the crash check only; `--strict` adds
conformance.

Then exclude, with a written reason, what cannot produce a meaningful result:

- Routes that mutate global state and poison every later case (admin flags)
- Routes that answer `503` by design because a credential is absent — a fuzzer
  counts any 5xx as a crash, so these are permanent false positives

Suppress generator-side health checks. They report on the fuzzer's ability to
build data for a constrained schema, which says nothing about the code.

## Stage 5 — Triage: three explanations, in this order

Every red test has three possible causes. Check them in this order, because the
order is the reverse of how tempting they are.

**1. The test is wrong.** Assume this first. Two of my findings this session were
methodology errors, and both looked exactly like real bugs:

- *"15 controls have no focus indicator."* The stylesheet uses `:focus-visible`,
  which Chromium does not apply to programmatic `element.focus()`. Driving real
  Tab presses instead: zero offenders.
- *"The search input is a 22px tap target."* The input sits inside a label styled
  to `min-height: 54px`. The whole label is clickable. I was measuring the wrong
  box.

Both would have been reported as defects by anyone who trusted the first red run.
Before reporting, ask: *what would make this test fail on correct code?*

**2. The behaviour is deliberate.** Three "server errors" from the fuzzer turned
out to be `503`s from unconfigured OAuth credentials — the app failing closed,
correctly. Read the response body, not just the status. Then decide whether the
right fix is to the code or to the test's exclusions.

**3. It is a real bug.** Only now. Reproduce it minimally, in isolation, and
confirm the mechanism rather than the symptom. When a `403` appeared, the finding
was not "save is broken" — it was a captured request showing
`content-type: text/plain` and no `x-csrf-token`, traced to a spread operator on
one line. A finding with a file and a line is worth several without.

## Stage 6 — Pin every confirmed defect

A bug in a report gets forgotten. A bug in a test does not.

Write a test that **fails now and passes when fixed**, and mark it so the suite
stays green in the meantime:

- Use a **strict** expected-failure marker (`xfail(strict=True)` in pytest, or the
  equivalent). Strict means the build **fails when the test starts passing** —
  which is the signal to delete the marker. A non-strict marker can outlive the
  bug and quietly stop meaning anything.
- Put the cause in the marker's reason: file, line, mechanism.
- Keep it minimal and fast. Each of the `compare_digest` regressions here runs in
  well under a second and names one specific defect.

## Stage 7 — Quarantine known debt without going blind

Some backlogs are too large to fix now — an accessibility audit typically is. The
wrong responses are to gate on it (permanently red) or delete the check
(permanently blind).

Quarantine **by name**, not by disabling the test:

- List the specific rule ids or element ids that are accepted, each with its cause.
- Anything **not** on the list still fails immediately. New regressions are caught
  while old ones are tolerated.
- **Print the quarantined items at the end of every run.** Debt nobody sees again
  is debt that was deleted. This repo prints a "known UI defects" block after each
  run.
- Removing an entry is the whole fix workflow: fix the cause, delete the line.

## Anti-patterns

| Anti-pattern | What to do instead |
| --- | --- |
| Loosening a threshold until the test passes | Find out whether the measurement is wrong; fix the method, not the number |
| `try/except` around a flaky step | Make it deterministic, or delete the test |
| Reporting the first red run | Triage in the Stage 5 order first |
| One assertion per bug found | A passive gate catches what nobody thought to assert |
| Blanket-muting a whole error class | Allow-list by cause, with the reason at the definition |
| Quietly fixing the bug you were asked to test for | Report it; a fix is a separate decision with separate review |
| Counting tests as coverage | Name the seam each suite is blind to |

## Checklist

```
[ ] Named the widest coverage seam in one sentence
[ ] Passive failure gate attached to every test, allow-listed by cause
[ ] Throwaway data; reset between tests; side effects redirected
[ ] Noise-generating defences raised, with the trade documented
[ ] Tests assert what a user perceives, including how failure is presented
[ ] Fuzzer gated on crashes only; conformance opt-in; exclusions justified
[ ] Every red run triaged: test wrong -> deliberate -> real, in that order
[ ] Each confirmed defect pinned by a strict expected-failure test
[ ] Known debt quarantined by name and printed every run
[ ] Findings reported with file, line, and mechanism; nothing silently patched
```

---

## Applying this to a new project

1. Run the existing suite. Note what it does *not* touch.
2. Build the seam table (Stage 0). Usually one row is obviously empty.
3. Add the passive gate (Stage 1) and run the thinnest possible smoke test
   through the uncovered layer. In this repo that combination found the P0 before
   a single deliberate assertion existed.
4. Only then invest in breadth.

The ordering matters more than the tooling. A smoke test plus an error listener
in the right seam beats a hundred assertions in a layer that already works.
