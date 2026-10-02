# Plan: claudex-loop bug fix + user-agnostic sweep (2026-09-21)
_Locked via claudex-loop — by Claude + owner (owner away; every open decision locked at Claude's recommendation via the escape hatch, as the owner instructed)_

## Goal
Fix the confirmed defects found by three recon passes (backend, API↔frontend seam, user-agnostic audit), each with a regression test that fails before and passes after, without changing product behaviour beyond the fix. Remove the remaining places where one student's situation is baked into shipped code, prompts or docs. Keep the legacy pipeline dependency-free (rule 4). Finish with the full unit, browser, and extension suites plus a Playwright-driven visual sweep of every tab at desktop and phone widths.

## Approach

### A. Source integrity (highest weight)
1. `opportunity_metadata.YEARLY_PAY_RE`: make the period suffix required (`(?:/|per\s+)?(?:year|yr|annually|annual)` non-optional, or a `a year`/`per year` form). A bare "$15,000 stipend" or "$120,000,000 in funding" must not become `pay_period='year'`. Test: stipend/funding text → no yearly pay; "$80,000 - $95,000 per year" and "$90,000/yr" still parse.
2. `pipeline.score_job` experience regex: require an experience word, `(\d{1,2})\+?\s+years?\s+(?:of\s+)?(?:[\w/+-]+\s+){0,3}?experience`. "at least 18 years of age" / "4 year degree" → no penalty; "3+ years of professional experience" still penalised.
3. `score_job` seniority: match whole words — `(?:\b(?:senior|staff|principal|manager|director|lead)\b|\bsr\.(?=\W|$))` (the period breaks `\b`, so `sr.` is handled separately; "Sr. Engineer" must still match) and do not apply the seniority penalty when the title itself is an intern/co-op/apprentice title (e.g. "Technical Program Manager Intern"). Reason text unchanged otherwise.
4. Urgent program deadlines (`urgent._program_rows`, `DATE_SOURCE_LABELS`): `evidence` describes *eligibility* evidence, not deadline provenance, so it cannot justify "Published by the program". All program dates get one neutral label, "From your program research", and the entry's `deadline_note` is carried into the row so estimates stay visibly estimates. Programs-tab lede in `app.js` stops claiming every date is host-published — it says dates come from the student's own research of each program page.
5. `pipeline.py` cover letter (~4101): no "engineering" default. Degree falls back to profile `degree`; if neither resume nor profile has degree/school, emit the existing `todo` placeholder instead of inventing text; strip any leading degree abbreviation (B.S./B.A./B.S.E./B.Eng./M.S.) not just "B.S."; drop the "at {school}" clause when school is empty.

### B. Privacy / state
6. Capture visibility in the read model and assistant: move the visibility predicate into `pipeline_core` (stdlib-only) as the single source; `opportunity_app/urgent.capture_visible_sql` re-exports it. Apply it in `OpportunityRepository` tenant list SQL and `get()`, and in `student_agent` deadline query. Every assistant opportunity read passes the authenticated `user_id` into `OpportunityRepository(conn, user_id=...)` (recommendations, search tool, detail tool) and the save-proposal existence check applies the same predicate, so a guessed ID of someone else's private capture is refused. Test: student-b cannot list/get/search/recommend/propose-save/see-in-deadlines the owner's private manual capture; owner still can.
7. `PUT /api/v1/profile`: add a dedicated web-profile *type* validator in `opportunity_app/profile.py` (not `setup.validate_profile`, whose completeness rules — nonempty `state_markers`/places — would reject regions the existing profile form legitimately creates with `state_markers: []`). Field-by-field nullable types, and it never traverses a container that failed its type check: list fields = `null` or list of strings (rejecting `null`/dict elements), and every scoring consumer treats a null list as empty (`profile.get(k) or []`); `max_years_experience`, `out_of_region_penalty`, `hours_per_week` = `null` or a non-bool INTEGER (fractions rejected, since scoring uses ints), and scoring treats null as unanswered → its existing default (1 year / 15 penalty), explicit 0 preserved; `work_authorized_us`, `us_citizen`, `requires_sponsorship`, `remote_ok`, `willing_to_relocate` = `null` or bool (unanswered stays unanswered — `is_answered` already keeps null out of facts); `regions` = `null` or list of objects with string `name`, optional list-of-string markers/places (empty allowed), optional non-bool integer `bonus`, optional string `phrase`; free-text fields = `null` or string; structured resume-like fields (education/experience/projects/…) = `null`, list, or object — shape-checked only where scoring reads them. Failure → 422 with a readable message and nothing stored.
   Order of operations (fixes first-time provisioning, transactions, and the owner mirror): (a) read the current profile row WITHOUT provisioning (new helper; `get_profile` keeps provisioning for GET); (b) merge + validate; (c) compute all new scores in memory with `score_job` (pure) — any exception here writes nothing; (d) for the local owner with a `profile_file`, write the file first via the existing atomic replace, keeping the previous text; (e) in ONE `with conn:` insert-or-update the profile row (covers a first-time user), facts, and fit_scores (a non-nesting `_write_scores` helper, `rescore_profile` keeps its public contract); (f) if (e) raises, restore the previous file text (or remove it if it did not exist) and re-raise. Profile saves are serialised with a module-level lock so overlapping saves cannot interleave their file write / rollback. Residual risk accepted: a hard process kill between (d) and (e) leaves the file ahead of the DB by one validated, user-submitted save, which the next refresh imports — i.e. the user's own intended edit, not corrupted or invented data. File and DB therefore move together, so a later refresh — which copies legacy scores computed from the file — cannot revert committed scores. Harden `score_job`: default `max_years_experience` only when missing/None (preserve explicit 0), and skip regions without a `name`. Tests: invalid first PUT before any GET stores no profile row/scores; null max_years/penalty/skills/available_terms → 200, scored with defaults, no facts for the nulls; `max_years_experience: 1.9`, region `bonus: 2.5` → 422; `regions: 1`, `preferred_role_types: [{}]`, `skills: [null]`, region `bonus: "bad"`, `hours_per_week: true` → 422 unchanged; partially answered profile (null hours/flags) saves and creates no facts for the nulls; region with empty markers saves; max_years 0 vs 1 threshold; injected DB failure after file write restores the file; injected scoring failure leaves DB and file unchanged.

### C. Dates and time zones
8. `read_model` "deadline before" filter: compare the date part (`substr(deadline_at,1,10) <= ?`) and fix the contract-test fixture to use the stored `YYYY-MM-DDT00:00:00+00:00` format. Check the "deadline after" side the same way.
9. `notifications` due reminders: compare instants, not strings — select candidates and filter in Python with parsed aware datetimes (or normalise stored `due_at` to UTC at write time plus compare parsed). Test: Chicago 09:00-05:00 not due at 10:00Z; Tokyo 09:00+09:00 due at 05:00Z.
10. `notifications` quiet hours: use `user_time.user_timezone(conn, user_id)` (same resolver as Urgent) rather than the raw `preferences.timezone` column default.
11. `student_agent` "closes soon": use the user's local today from `user_timezone`, not UTC.
12. `extract_deadline`: also accept abbreviated months ("Sep 30, 2026", "Sept 30 2026") — additive, never guessing.
13. `send_due_reminders`: skip email delivery when the user has no email address.

### D. Outreach
14. `outreach_drafting.validate_draft`: accept either the region name or its `phrase` in both checks (~376/378).
15. Outreach drafting prompt: remove the hardware-only "physical verbs" instruction and CAD/soldering/flight examples; instead instruct the model to take contribution verbs from the student's lead experience and skills (neutral fallback wording).
16. `outreach._profile_regions` / Programs file: for a non-local user, read regions from that user's confirmed facts; serve `early_programs.local.json` only to the local owner, others get the existing empty state. (Low; each student normally runs their own copy.)

### E. Frontend (`app.js`)
17. `api()` error detail: when `detail` is a list (FastAPI 422), join the `msg` fields into a readable string.
18. Registration form: send `invite_token` as `null`/omitted when blank.
19. Selects that auto-save on `change` (Programs status, application stage, outreach status): keyboard arrow navigation no longer commits each intermediate value — track a keyboard-pending flag; commit on Enter or blur; mouse/pointer selection still saves immediately. After a Programs reload triggered by Enter or pointer selection, restore focus to the same program's rebuilt control; if the row left the current sub-tab (e.g. todo → applied), fall back to the next visible row's control, else the active sub-tab button. For a blur-triggered save, before rebuilding the list remember which control currently has focus inside the Programs list (by program id + control role, via data attributes) and restore focus to that control's rebuilt counterpart afterwards; never move focus if it was outside the list. Browser test: arrow keys send no PUT; Enter sends one; Tab to another row's control keeps focus there after a delayed save.
20. Stage select: revert `select.value` on failed save.
21. Application import: set the "Imported X; skipped Y" status after the reload (and list returned `errors`), so it is not overwritten.
22. `programWhen`: entries in the `closed` bucket read "Closed" (or their closed note), never "N days left".

### F. Docs / catalog (user-agnostic)
23. README: replace one-school-specific portal and research-listing lines with generic "your school's career portal (add to `manual_check_sources`)" / "a relevant lab at your school"; broaden mechanical-only interview examples; reword the `deprioritize_title_keywords` sentence to be profile-neutral.
24. `config/sources.json` notes: neutral wording (geography only, never "inside the owner's region list" or "for this profile").
25. `profile.example.json` + SETUP.md step 3: add `break_location` and document the region `phrase` key; document the outreach-relevant resume/profile fields briefly.

### G. Verification
26. `py -3 -m unittest discover -s tests` (and parallel pytest), `.venv-ui` browser suite, `node tests/extension/run_tests.mjs`, `node --check` on JS.
27. Playwright visual sweep against the sandbox (`scripts/serve_for_testing.py`): every tab at 1280×900 and 390×844, screenshot review, console errors, Programs + Urgent + Applications flows, keyboard select behaviour.
28. Post-build Codex cross-inspection (fresh read-only session).

## Key decisions & tradeoffs (all locked via escape hatch)
- Fix everything confirmed now rather than pin-and-defer: the owner asked for a bug fix run; each fix gets a live regression test (no xfail pins needed since fix + test land together).
- Seniority: suppress penalty for intern titles rather than weaken the word list — interns with "Manager" in a program-manager title are a real, common false negative; genuine senior roles rarely carry "Intern".
- Profile PUT: reject-before-write (422) over coerce-silently — coercion would invent values, which the product invariants forbid.
- Selects: keyboard-commit-on-Enter/blur rather than adding Save buttons — keeps the layout and the mouse flow unchanged.
- Reminder comparison in Python rather than migrating stored `due_at` data — no migration, no rewrite of existing rows.
- Visibility predicate moves into `pipeline_core` to respect rule 4 (pipeline_core may not import `opportunity_app.urgent`).

## Assumptions
- Baseline: 751 unit tests + 217 browser tests pass on 4c0fac9 (run this session).
- Repros for backend bugs are scratch scripts r1–r7 (throwaway DBs via `build_and_migrate`).
- `urgent.capture_visible_sql` is the authoritative visibility rule (Urgent already hides others' captures correctly).
- No uncommitted work anywhere; branch starts at origin/main.

## Risks / open questions
- Score changes (experience/seniority) will shift some stored scores after the next rescore — intended, but reasons must still account for the raw score.
- Tightened YEARLY_PAY_RE may drop some genuine yearly salaries written without a period ("$120,000 salary"); acceptable — unknown beats invented.
- Playwright MCP server failed to connect this session; the visual sweep uses Playwright from `.venv-ui` directly (same engine).

## Out of scope
- Assistant using owner scores for non-owner users; graduation-year extraction heuristic; program `opens_on` in the future; year bounds on program dates; extension "evidence refreshed" wording; Programs sub-tab switching from Urgent. Logged for follow-up.
- Rebalancing the shared source catalog (only wording changes).
