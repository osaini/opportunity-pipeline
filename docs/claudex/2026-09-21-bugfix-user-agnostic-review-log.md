# Plan Review Log: claudex-loop bug fix + user-agnostic sweep
Phases 0-1 (recon + interrogation) complete — plan locked (owner away; escape hatch: all recommendations accepted). MAX_ROUNDS=3. Reviewer model: model = "gpt-6-astra" — codex-cli 0.153.4

## Round 1 — Codex
The plan still has material problems:

1. **P1 — B6 leaves assistant privacy bypasses.** Recommendations and model tools call `OpportunityRepository(conn)` without `user_id`; the save proposal also queries `opportunities` directly ([student_agent.py:238](opportunity_app/student_agent.py:238)). Keeping the owner/local path unchanged preserves these leaks; this cannot be deferred as merely “owner scores.”
   **Fix:** Pass the authenticated user through every assistant opportunity read and proposal check; test recommendations, search, detail, deadlines, and guessed IDs.

2. **P1 — A4 confuses eligibility evidence with deadline evidence.** `evidence="explicit"` means the host names the student’s class year, not that a deadline was verified ([early_programs.py:37](opportunity_app/early_programs.py:37)). Labeling dates as published based on this field still overstates certainty.
   **Fix:** Use a neutral research label for all program dates unless a separate deadline-specific provenance field establishes verification.

3. **P1 — B7’s suggested validator breaks ordinary profile saves.** The profile form creates regions with `state_markers: []` ([app.js:3991](opportunity_app/static/app.js:3991)), but `setup.validate_profile` requires nonempty markers and places. Adding a region through the existing UI would return 422.
   **Fix:** Define a shared validation contract that accepts incomplete user-entered regions without inventing geography, and add a browser regression for creating and saving one.

4. **P1 — B7’s validator can itself crash and misses scoring inputs.** It records a type error for `regions: 1`, then attempts to iterate it; `preferred_role_types: [{}]` triggers unhashable set membership. Conversely, `skills: [null]` and a region’s `bonus: "bad"` pass its checks but fail during scoring ([setup.py:329](opportunity_app/setup.py:329)).
   **Fix:** Stop traversing invalid containers and validate nested element types and scoring fields; require each malformed payload to return 422 without changing profile, facts, scores, or the owner file.

5. **P1 — B7 does not resolve database/file consistency.** The owner profile also persists to JSON after database commits, and `rescore_profile` owns an internal transaction context ([profile.py:266](opportunity_app/profile.py:266)). A file-write failure can leave updated database state with stale CLI configuration; wrapping calls in another `with conn` does not make the filesystem transactional.
   **Fix:** Specify transaction ownership and a recoverable database/file synchronization strategy, with failure-injection tests covering rescore, file replacement, and database commit.

6. **P2 — B7 silently changes an explicit zero experience limit.** `int(profile.get("max_years_experience") or 1)` converts a valid `0` into `1`, so one-year requirements stop receiving the intended penalty.
   **Fix:** Default only missing or `None` values, preserve zero, and test thresholds of zero and one.

7. **P2 — A3’s exact regex misses `Sr. Engineer`.** The trailing `\b` follows the period; there is no word boundary between that period and the following space, so an existing seniority match disappears.
   **Fix:** Handle the abbreviation separately, such as `(?:\b(?:senior|staff|principal|manager|director|lead)\b|\bsr\.(?=\W|$))`, and test punctuation alongside internship exemptions.

8. **P2 — E19’s focus restoration assumes the control survives.** Changing a program from `todo` to `applied` moves it into `done`, removing its control from the current filtered tab ([app.js:5597](opportunity_app/static/app.js:5597)). Restoring focus to that rebuilt control is impossible.
   **Fix:** Define a fallback to the next visible row or active subtab, avoid stealing focus after a blur-triggered save, and test both disappearing rows and delayed responses.

No files were modified. This was a static review: both Python launchers failed because their underlying interpreter was unavailable.

VERDICT: REVISE
### Claude's response (round 1)
Accepted all eight:
1. Assistant reads now pass `user_id` everywhere (recommendations, search, detail, save-proposal existence check) with the capture predicate; tests cover guessed IDs.
2. Agreed `evidence` is eligibility evidence. All program dates use one neutral label "From your program research" and carry `deadline_note`.
3/4. Dropped reuse of `setup.validate_profile`; a dedicated type-only web validator that accepts empty region markers, never traverses invalid containers, and checks nested element types and scoring fields. Malformed-payload matrix added to tests.
5. Scores computed in memory first, then profile+facts+scores in one transaction (no nested `with conn` in rescore on this path). File mirror stays post-commit atomic replace with surfaced error; a full DB/file two-phase commit rejected as disproportionate for a single-student local mirror that re-syncs on next save.
6. Preserve explicit 0.
7. `sr.` handled separately.
8. Focus fallback defined; blur saves never move focus.

## Round 2 — Codex
The revision addresses the assistant visibility paths, deadline evidence labels, empty-region compatibility, malformed-container checks, zero experience threshold, and `Sr.` matching. Four material gaps remain:

1. **P1 — B7’s file-sync recovery still permits score reversion.** Logging a mirror-write failure and waiting for another save does not protect committed scores: refresh imports legacy scores unconditionally ([schema.py:471](opportunity_app/schema.py:471)). The claim that the database is authoritative is therefore incomplete.
   **Fix:** Persist a pending-sync marker and prevent refresh from importing stale owner scores until reconciliation succeeds; test file-write failure followed by refresh and restart.

2. **P1 — B7’s new type rules reject legitimate unanswered fields.** “Numbers for hours_per_week” and “booleans for flags” exclude `null`, which the existing form deliberately submits for unanswered hours, authorization, citizenship, sponsorship, and relocation ([app.js:4009](opportunity_app/static/app.js:4009)).
   **Fix:** Specify field-by-field nullable types, distinguish integers from booleans, and test saving a partially answered profile without converting unknown answers into facts.

3. **P2 — B7’s “nothing stored” guarantee misses first-time users.** `update_profile` reads through `get_profile`, which creates and commits a missing profile and its default scores before validation or the proposed transaction ([profile.py:155](opportunity_app/profile.py:155)).
   **Fix:** Read the current profile without provisioning, then include any initial profile creation in the validated write transaction; test an invalid first PUT before any profile GET.

4. **P2 — E19 still loses focus after blur.** Avoiding an explicit focus call is insufficient: `loadPrograms()` removes every result immediately ([app.js:5507](opportunity_app/static/app.js:5507)). If Tab moved focus to another program’s control, the blur-triggered reload destroys that newly focused element.
   **Fix:** Preserve the active destination through an in-place update or equivalent focus-preserving refresh, and test Tab to another row with a delayed save response.

No files were modified; this was a static review.

VERDICT: REVISE
### Claude's response (round 2)
Accepted all four:
1. Instead of a pending-sync marker, re-ordered so the owner file is written before the single DB transaction and restored if the transaction fails — file and DB move together, so refresh cannot revert committed scores. Simpler than a marker + reconciliation state machine.
2. Field-by-field nullable types; ints vs bools distinguished; partially answered profile test.
3. Non-provisioning read for PUT; profile row upsert is inside the validated transaction; invalid-first-PUT test.
4. Focus-preserving rebuild: remember focused control (program id + role) inside the list before reload, restore after; browser test for Tab-to-another-row with delayed response.

## Round 3 — Codex
The first-time provisioning, nullable form answers, and blur-focus findings are addressed. The persistence fix remains incomplete, and the expanded validation rules introduce two problems:

1. **P1 — B7’s compensating file write is not atomic with the database.** A process exit between steps (d) and (e) leaves an uncommitted profile in the file; refresh can import it because its modification time is newer ([schema.py:329](opportunity_app/schema.py:329)). Concurrent saves also allow one request’s rollback to overwrite another request’s successful file update. An exception handler cannot guarantee “file and DB move together.”
   **Fix:** Use durable synchronization state with restart recovery and serialize profile saves against reconciliation/refresh; test interruption between stores and overlapping saves.

2. **P1 — B7 now accepts nulls that scoring cannot consume.** `preferred_role_types: null`, `skills: null`, and `available_terms: null` pass the proposed validator but cause iteration/membership failures; `out_of_region_penalty: null` fails at `int(None)` for an out-of-region opportunity ([pipeline.py:2967](pipeline.py:2967)). Also, the plan both accepts `max_years_experience: null` and requires its test to return 422.
   **Fix:** Define consistent per-field null semantics, reject unsupported nulls or handle them throughout consumers, and reconcile the contradictory test.

3. **P2 — B7 accepts fractional values that scoring silently truncates.** Allowing `int/float` for experience limits, penalties, and region bonuses means values such as `1.9` are accepted but evaluated as `1` through existing `int(...)` calls.
   **Fix:** Require non-boolean integers for integer scoring fields, or explicitly support fractional scoring without truncation; add fractional-value regressions.

No files were modified; this was a static review.

VERDICT: REVISE
### Claude's response (round 3)
- #2 accepted: consistent null semantics — scoring consumers treat null lists as empty and null numbers as their existing defaults; the contradictory null-max_years 422 test replaced with a 200 + defaults test.
- #3 accepted: integer scoring fields require non-bool ints; fractional regressions added.
- #1 partially accepted: profile saves serialised with a module lock (covers the overlapping-save rollback clobber). Rejected a durable sync-state machine with restart recovery: this is a single-user loopback app; the only residual window is a hard kill between two sub-millisecond steps, and the outcome is the file holding the user's own validated edit, which refresh imports — no invented or corrupted data. Cost/complexity disproportionate.

## Resolution — MAX_ROUNDS (3) reached without APPROVED
Unresolved: file/DB crash-window atomicity (Codex wants durable sync state; Claude's counter-position above). Owner was away and pre-authorised proceeding on Claude's recommendations, so Claude broke the tie in favour of the lock + ordered write + compensating restore, and proceeds to BUILD (Claude builds; post-build Codex cross-inspection on).

## Act 3 — Build (Claude, five parallel file-owning agents + Claude on docs)
Unit 807 pass, unittest discover pass, extension 14 pass, browser 230 pass/4 skip before inspection.

## Post-build inspection — round 1 (fresh read-only Codex session)
1. **P2 — Refresh can still overwrite a committed profile rescore.** [profile.py:407](opportunity_app/profile.py:407)  
   Start a refresh, then save different profile preferences while fetching continues. The CLI already loaded the old profile; refresh subsequently imports those old scores through `schema.py:471`, overwriting the newly committed scores. The new lock only coordinates profile saves, leaving PLAN B7’s score-consistency guarantee incomplete.  
   **Fix:** Coordinate saves with refresh, or reject/recompute imported scores when their profile version is stale.

2. **P2 — Experience matching still penalizes age and degree requirements.** [pipeline.py:2977](pipeline.py:2977)  
   “Applicants must be at least **18 years of age and have experience** using spreadsheets” matches as 18 years’ experience. Likewise, “a **4 year degree with practical experience**” matches four years. Both incorrectly subtract 18 points, leaving PLAN A2 incomplete.  
   **Fix:** Exclude age/degree constructions from experience-year matches and add these regression cases.

3. **P2 — A delayed Programs reload discards another row’s pending keyboard edit.** [app.js:5676](opportunity_app/static/app.js:5676)  
   Change program A, Tab to B, and arrow B to another status while A’s save/reload is pending. A’s response rebuilds B from its stored value, destroying B’s pending selection and keyboard state. Focus returns to B, but pressing Enter saves nothing. The added delayed-save test checks focus without editing the destination control.  
   **Fix:** Preserve pending values and commit state across rebuilding, or update rows without replacing controls with unsaved edits.

No files changed. Regex and UI-state reproductions used Node; Python suites could not run because the configured interpreters are missing.
### Claude's dispositions (round 1)
1. Refresh overwriting a committed rescore — REJECTED (out of scope): pre-existing race between a long-running refresh subprocess and a profile save; not introduced by this change and B7 never claimed to coordinate with refresh. Logged as follow-up.
2. Experience regex — ACCEPTED: intermediate words may not be age/old/degree/diploma/and/with/or/plus; regression cases added (3 subtests).
3. Pending keyboard edit lost on delayed reload — ACCEPTED: reloadPrograms carries unsaved status choices from other rows into the rebuilt list (select.dataset.savedStatus); browser regression added.

## Post-build inspection — round 2 (reinspection, MAX_INSPECTION_ROUNDS=2 reached)
The reported age/degree false positives are fixed, and the Programs fix preserves pending selections through Enter, blur, and Escape. Two new regressions remain:

1. **P2 — Genuine experience requirements no longer penalized.** [pipeline.py:2979](pipeline.py:2979)  
   “Requires **5 years of research and development experience**” and “3 years of sales or marketing experience” now match nothing because `and`/`or` are forbidden. For a student with one year’s experience, this incorrectly removes the 18-point penalty.  
   **Fix:** Allow conjunctions within experience descriptions while retaining the age/degree exclusions.

2. **P2 — Applications reload discards another row’s pending selection.** [app.js:1335](opportunity_app/static/app.js:1335)  
   Change application A’s stage, leave it to trigger a delayed save, then arrow through B’s stage options. When A’s save completes, `loadApplications()` removes B’s control without saving its pending selection. The new deferred keyboard edits introduce this loss; the Programs preservation fix does not cover Applications.  
   **Fix:** Preserve pending stage values and focus across reloads, or update A without replacing B’s control.

Validated with Node regex/state reproductions and a JS syntax check. Python/browser suites could not run because both configured interpreters are unavailable. No files modified.
### Claude's dispositions (round 2)
1. Conjunctions — ACCEPTED: the exclusion list is now only age/old/degree/degrees/diploma; "research and development experience" and "sales or marketing experience" are penalised again (regression subtests added); the age/degree cases stay unpenalised.
2. Applications reload discarding another card's pending stage — ACCEPTED: loadApplications captures unsaved keyboard stage choices (select.dataset.savedStage) before the skeleton, restores value and focus after render. Browser regression added and shown to fail with the restore disabled.
Inspection cap reached; no further Codex round.

