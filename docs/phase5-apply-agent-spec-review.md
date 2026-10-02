# Phase 5 spec review log

Review of `PHASE5-SPEC.md` revision 1 (2026-09-28), producing revision 2 the same day, and revision 2.1 after Codex round 2.

- **Reviewers:** an internal review (70 findings: feasibility, safety policy, testability and
  operations) and Codex round 1 (10 findings, thread `01a0e924-f7f0-7e60-86c1-29cb4efb4f65`,
  verdict REVISE).
- **Arbiter:** the spec author. Each finding was checked against the code at `5d95aa9` where it
  cited code. Decisions: **accepted**, **partly** (accepted with a different fix or a narrower
  scope, reason given), or **rejected** (reason given). No finding was rejected outright.
- **Section 3 stays open.** Every student decision remains a question with a recommendation.
  Some recommendations changed because of the findings (noted below); none was decided.
- **Milestones were renumbered:** M5 split into M5a and M5b; the watch moved from M7 into M5b;
  cover letters became M7; unattended became M8; a conditional M4s holds the sensitive store.

## Recommendations that changed in section 3

| Decision | Revision 1 | Revision 2 | Why |
| --- | --- | --- | --- |
| D1 A wording | "submitted automatically only by the opt-in apply agent" | "...and only after the student confirms that specific submission" | Finding 43: the old wording would also have authorized unattended mode. |
| D5 | C (store work auth, sponsorship, EEO, consents) | **A while D1 is B**; decide B or C (EEO decline-only) with D1 A | Findings 1, 44, 45: under Finish in browser the fields are simply left for the student, so A stops nothing, keeps assisted-apply.md step 3 and THREAT_MODEL.md:17 true, and needs no demographic store. |
| D6 B | Pause never stops a confirmed submit | A pause pressed after the confirm stops it until hand-over; Cancel added | Finding 37. |
| D9 | B | **A while D1 is B**; B with D1 A and D5 C | Same reasoning as D5. |
| D12 | A (required) | **C**: required for one-click and unattended, optional for Finish in browser | Codex 6: employers may not send the email; finding 14: the address must be stored first. |
| D14 | (did not exist) | New: CAPTCHA checkbox, recommend A (never click it) | Findings 20, 49. |

## Internal findings

1. **[major] Finish in browser cannot run in the cases it is offered for.** Accepted. Handoff has
   its own policy: required fields with problems and disallowed sensitive fields become
   `left_for_you` (6.6); 6.0 step 6 blocks only submit runs; 6.10 items 1 to 3 skip those keys;
   the claim, no-guessing and hidden-field rules stay; the company-limit override tick applies to
   handoff too (D4, 6.0 step 7, 9.1). The truth table gained a Handoff column (7.5).
2. **[major] `stage_recorded=0` means both "retry pending" and "deliberately not recorded".**
   Accepted. New claim column `stage_policy` (`record`/`ask`/`ledger`) set at claim time;
   `recover_stale` retries only `record` and `ledger`; the `ask` badge reads "Greenhouse showed its
   confirmation page. Mark as applied?" (5.2 rules 7 and 8, 6.15, 10.5).
3. **[major] Classifier marks "hear" and "year" as export control; no "uncategorized" value.**
   Accepted. `ear\b` replaced by "export administration regulations"; `visa` tied to sponsorship
   wording; `authori[sz]ation to work` added; `classify_sensitive` returns an explicit
   `"uncategorized"` (never storable); most restrictive category wins; the finding's strings are in
   `sensitive_vectors.json` (7.3).
4. **[major] Rule 1 turns demographic questions into "consent".** Accepted. The never-storable
   pattern runs first for every section; `compliance` and `demographic_questions` map to EEO by
   schema name only, else `"uncategorized"`; only `data_compliance` maps to consent; the "Decline
   to self-identify" options rule gives `"uncategorized"` unless an EEO name matched (7.3 steps 1,
   2, 4).
5. **[major] `name.first`/`name.last`/`name.preferred` do not exist as profile facts.** Accepted
   (verified: student/profile.py:16-48 has one `name` field; update_profile refuses others at :409-411).
   New profile field `name_parts {first, last, preferred}` in `ALLOWED_PROFILE_FIELDS`,
   `validate_profile_types`, the profile UI ("Name for applications") and SETUP.md; mapping list,
   requirement sentence, 10.3 link and Appendix B updated (7.1, 5.6).
6. **[major] `set_input_files(path)` uploads the storage file name.** Accepted. Attach with a
   `FilePayload` using `original_name` and `media_type`; SHA-256 checked against
   `resume_files.sha256`; the planned and read-back name is `original_name` (6.9).
7. **[major] No rule for a role with no résumé pick.** Accepted, merged with Codex 8.
   `apply_policy.resume_for`: the pick in force resolves to its latest *confirmed* version; no pick
   (or `no_variants`) falls back to the most recently confirmed résumé, the `resume_check` rule,
   shown as "Your confirmed résumé"; `unsure` opens the chooser; the source ref is the version id
   (6.9, 7.1, truth-table rows 31 and 32).
8. **[major] Opening the section creates an application and calls Greenhouse.** Accepted
   (verified: applications/actions.py:127-152). The check is read-only and asynchronous, with a one-hour schema
   cache; `apply_runs.application_id` is nullable; the application row is created only when a
   submit or handoff claim is taken. This follows the stricter finding 36 rather than "when a
   rehearsal starts" (6.0, 6.1, 5.3).
9. **[major] Typeahead labels depend on a rehearsal that preflight never lets run.** Accepted.
   Rehearsals now run with gaps (6.0 step 6), and a GET-only **Look up options** action starts a
   `kind='lookup'` run that types the student's text into one field and lists the options (5.5,
   10.3). M4 now covers text, select and name answers; typeahead labels land in M5a.
10. **[major] Schema fetch cannot be injected; fuzz and sandbox would call real Greenhouse.**
    Accepted (verified: tests/helpers_platform.py:43 seeds `greenhouse:acme`). New
    `create_app(apply_schema_client_factory=...)`, real only for the real product database;
    `FakeSchemaClient` for tests, the sandbox and the UI suite; the fuzz sandbox gets 503;
    `identify` requires `^\d+$` job ids, so the fixture's `a-1` is not identified; the sandbox flag
    also fakes the requirement it cannot meet (4.4, 4.6, 12.2, 12.6).
11. **[major] "No POST to the submit path" treated as nothing sent.** Accepted. Before hand-over
    every non-GET is aborted except CAPTCHA traffic and the planned résumé upload, so nothing can
    leave; after hand-over only zero non-GET requests (other than CAPTCHA hosts) and no main-frame
    navigation may be reported as "nothing sent"; everything else is `unconfirmed` at best (4.3,
    6.14).
12. **[major] Caps have no click time to count from.** Accepted, as `handed_over_at` (set in the
    hand-over transaction, never cleared). `limits_block` counts every claim with it set, in any
    state including `released`; indexes on `(user_id, company_key, handed_over_at)` and the board
    (5.2, 9.1).
13. **[major] After M1 the extension stops exact-matching answers it saved.** Accepted.
    `matchAnswer` compares `questionKey(question)` first and the legacy label second; node test
    added (4.2, 12.5).
14. **[major] D12 A needs the connected Gmail address, which is not stored.** Accepted (verified:
    migrations/0001:362-374 has no address column; outreach/gmail.py:612-617 reads it live). New
    `connector_accounts.account_email`, recorded at connect, added by a guarded Python step; 5.1
    no longer says "no column, no Python step"; the requirement compares the stored value
    case-insensitively (5.1, 5.6).
15. **[minor] UNIQUE index on the full statement text fails on PostgreSQL.** Accepted.
    `question_hash` column; `UNIQUE(user_id, question_hash, company_key)` (5.4).
16. **[minor] Split breaks the existing static guards and the DOM stub.** Accepted. Positive
    assertions move to `apply-engine.js`, negative ones cover both files; the engine guards absent
    DOM APIs and the stub gains what the new tests need; stated in M1 (4.2, 12.5).
17. **[minor] `delete_account` has more callers; export misses screenshot paths.** Accepted
    (verified three callers). `apply_root` is an optional keyword; `screenshots_json` paths are
    redacted by a special case (5.7).
18. **[minor] `identify()`: literal LIKE breaks PostgreSQL; source key may not be the token.**
    Accepted. The pattern is a parameter; a token parsed from the URL wins; `^\d+$` job ids; a
    wrong source-key token surfaces as a schema 404 with a plain sentence (4.4).
19. **[minor] Crashed rehearsal and check runs stay "running" forever.** Accepted, merged with 56:
    orphaned `running` rows are finished as failed by `recover_stale`; the UI stops polling on a
    stale heartbeat. Check runs no longer exist as rows (5.2 rule 7, 10.2).
20. **[minor] Click allowlist has no CAPTCHA purpose; `check()` bypasses `_click`.** Accepted,
    folded into new decision D14. `captcha_checkbox` exists only under D14 B; every mutation goes
    through five helpers (`_type`, `_tick`, `_choose`, `_attach`, `_click`), and the static scan
    covers them (4.3, 12.7).
21. **[minor] Rehearsal on upload-as-you-attach boards contradicts the pre-submit check.**
    Accepted. The résumé is `deferred` in that rehearsal and skipped by 6.10 items 1 to 3;
    `plan_hash` excludes the disposition so submit compares like with like; such a rehearsal is not
    clean for the gate (Codex 9) (6.6, 6.9, 9.2).
22. **[minor] The new "required" rule changes what the side panel shows.** Accepted, with the
    new-field option: `required_any` is added and `required` is unchanged (4.2).
23. **[minor] Worker hooks and existing UI code do not cover application claims.** Accepted. The
    worker's apply step runs for every student `students_to_watch` returns, regardless of
    switches; `paused_text` handles any number of categories; `unconfirmedSentence` and the
    in-flight and Health rendering learn `action: "application"` (5.6).
24. **[minor] Gate reset and per-ATS disable have nowhere to be stored.** Accepted.
    `user_settings` keys `apply_gate_reset_at:<ats>` and `apply_ats_disabled:<ats>` (5.6, 8.8,
    9.2).
25. **[minor] Preflight and the live run key questions from different text.** Accepted. The
    schema label is the one key; the normalized DOM question must equal it or it is a problem
    (6.5).
26. **[minor] Headed Chromium on Linux has no display under the systemd service.** Accepted, as
    a requirement check (`DISPLAY` or `WAYLAND_DISPLAY`) with the `systemctl --user
    import-environment` instruction in the sentence and in SETUP.md. launch.py is not changed
    (5.6, D7).
27. **[minor] Unsalted hashes of short answers are not value-free.** Accepted. `value_mac` is
    HMAC-SHA256 with a per-install key in `data/private/apply/hash-key`, never exported (5.3,
    5.7, 11).
28. **[minor] Rehearsal banner overstates what stayed in the browser.** Accepted, merged with 41
    and 61. The header states only what is measured, including that typeahead lookups send what
    was typed; submit-path evidence is kept in `evidence_json` (10.4).
29. **[minor] Claim-transaction re-checks are only safe on SQLite.** Accepted, with a variant of
    the fix: every claim-table transaction starts with `UPDATE users SET id=id WHERE id=?`, which
    takes SQLite's write lock and PostgreSQL's row lock alike (the pattern of
    `_update_application_tx`); `applications` reads use `_for_update` (5.2 rule 0).
30. **[minor] `allow_loopback` is not in the signature and conflicts with the host allowlist.**
    Accepted, by dropping the loopback demo altogether (with 46 and 69) (12.2).
31. **[minor] Pre-filled defaults on the page would stop every run.** Accepted. Initial values are
    snapshotted after load; an optional field still at its initial value is allowed and listed as
    "left as the page set it"; a required one never is (6.4, 6.10 item 4).
32. **[minor] Forward-only stage write is not done by `_update_application_tx`.** Accepted
    (verified applications/actions.py:567-625 re-reads but does not compare). The caller locks, re-reads and
    skips unless the stage is `applying`; the unattended path uses `only_from` (6.15).
33. **[minor] Some code references are off.** Partly. Checked each:
    - factory wiring: the spec's api.py:1036-1040 was loose, but the finding's 1044-1045 is the
      SystemStatus line; the right lines are 1039-1040, now cited;
    - sidepanel.js: the spec's :82 (injection) and :212 (answer save) are correct; the finding's
      :81 is the line before, and :210 is where the `api(` call starts. Kept, with a note;
    - `discover_ats` is pipeline.py:2277 (accepted);
    - opportunities/captures.py:337 also inserts an application (accepted; core/schema.py:606 does too).
34. **[critical] "Nothing was sent" is claimed but not enforced.** Accepted in full: (1) submit and
    handoff abort every non-GET before hand-over except CAPTCHA hosts and the résumé upload whose
    body is the planned bytes; (2) the handoff hand-over runs inside the route handler before
    `route.continue_()` and aborts on False; (3) after hand-over, "nothing sent" only with zero
    non-GET requests; (4) submit and handoff refuse to fill without `submitPath` and
    `confirmationPath`; (5) on handoff timeout or Stop the browser closes before the claim is
    settled; (6) fixtures `other_path_post` (gives `unconfirmed`) and
    `request_submit_during_fill` (blocked, `needs_you`) (4.3, 6.2, 6.13, 6.14, 12.2, 12.4).
35. **[major] The same Greenhouse job can be submitted twice.** Accepted, with one narrowing.
    Claims are one row per attempt with partial unique indexes on the application and on
    `(user_id, ats, job_ref)`, which no override bypasses; the company limit matches the board
    token too; "It didn't go through" leaves a `released` tombstone that the limits count, the
    duplicate check asks about, and the watch can still confirm; the preflight asks about an
    unmatched company confirmation and about an `applying` row more than a day old. Narrowed: those
    asks gate submit and handoff only, not rehearsals, which send nothing (5.2, 6.0 step 4, 9.1).
36. **[major] Preflight creates an `applying` row that Phase 1 can later mark Applied.** Accepted,
    with one deviation: the row is created at claim time with an `application_events` row
    `apply_agent_started`, not a new `opportunity_interactions` action, because that table's CHECK
    allows only five actions (migrations/0001:141) and adding one would mean a table rebuild. Test
    added: opening the section and rehearsing leave `applications` unchanged (6.1, 12.3). The
    older Phase 1 `company_single` behaviour itself is out of scope (13 R11).
37. **[major] A pause pressed after confirming does not stop the submit.** Accepted for one-click:
    the hand-over refuses when the pause's `updated_at` is later than `confirmed_at`; Cancel stays
    active until hand-over; closing the window before hand-over cancels; D6 B reworded. Partly for
    handoff: the student's own press of Submit in the window is the confirm, which is always newer
    than any pause, so D6 B does not stop it; D6 A still offers "pause stops everything" (D6, 5.2
    rule 3, 6.13, 10.2).
38. **[major] Exact question wording lets company-specific answers carry over falsely.**
    Accepted, with one parameter changed. The company rule applies to every field kind; `reusable`
    lifts it; context-dependent keys (company-relative questions and sub-question openers) are never
    reused across companies even when reusable, and sub-questions carry their parent in the key;
    option labels only, never values; 10.3 saves for this company by default; the preview flags
    answers first saved elsewhere; truth-table rows 33 to 36. The short-key threshold is "fewer than
    3 words" rather than "about 4", so common three-word questions still reuse when marked
    reusable (7.1, 7.2, 10.3, 10.4).
39. **[major] Gaps in the sensitive classifier.** Partly. Added: F-1, J-1, OPT (not "opt in/out"),
    STEM OPT, CPT, TN, E-3, H-1B, petition, employment based, immigration to sponsorship;
    clearance to export control; felony, misdemeanor, arrest and non-compete to never storable;
    the options fail-closed rule; the mirror into the extension's `SENSITIVE`; vectors. Changed:
    "18 or older" is its own storable category `age_18` (offered in D5 B and up), not never
    storable: it is a yes/no eligibility fact like work authorization, and making it never storable
    would stop most one-click runs. The student decides through D5 (7.3, D5, 4.2).
40. **[major] Weak mail matches counted as "Confirmation email received".** Accepted. Only
    `matched_by IN ('job_id','company_title')` with the new `sender_verified=1` column confirms;
    `company_single` and subject matches only set `possible_email_at` and a "may be for this
    application" line, and do not stop the clock or count for 8.8; test added (5.1, 6.16).
41. **[major] Rehearsal places sensitive answers in a live page; claims overstate what is
    blocked.** Accepted. Rehearsals defer sensitive fields (the option is checked, not chosen);
    after the first input only Greenhouse and lookup hosts are reachable; WebSockets refused; a
    value guard blocks requests to other sites carrying a filled value; G2, 10.4 and 11 now state
    exactly what is enforced; `eager_get` fixture (4.3, 6.7, 10.4, 11, 12.4).
42. **[major] Consent and confirmation routes accept the owner bearer token.** Accepted (verified:
    api.py:1263-1270 exempts bearer requests and checks CSRF only with an `Origin` header).
    `require_browser_session`: cookie session, CSRF always checked, 403 on any `Authorization`
    header; a single-use confirm nonce bound to the plan hash; tests for each route. It guards
    against local tooling or an agent acting for the student, not against a process that can read
    the database (4.6, 12.6).
43. **[major] D1 A wording authorizes unattended submission too.** Accepted. New wording; D2, 9.4
    and M8 say unattended needs its own AGENTS.md rewrite, decided by the student.
44. **[major] M4 and M5 contradict docs/assisted-apply.md without a doc change.** Accepted. The
    recommended path (D5 A and D9 A under D1 B) keeps "sensitive fields remain manual" true;
    assisted-apply.md gains its Apply agent section and THREAT_MODEL its row in M5b, not M6; if the
    student chooses D5 B to E, M4s rewrites step 3 in the same PR (D1 B, D5, 14).
45. **[major] D5 C reverses "demographic attributes are deliberately not collected".** Accepted.
    D5 states the reversal; EEO sub-choice (i) stores only decline answers; the consent text
    limits use to filling forms; THREAT_MODEL.md:17 and PRIVACY_ACCESSIBILITY.md:7 change in M4s;
    a source test proves `accounts/employer.py` and reports never read the store (D5, 5.4, 12.7).
46. **[major] The loopback demo builds a real agent that can reach real Greenhouse.** Accepted:
    the loopback demo and flag are dropped; the sandbox has only the fake agent (12.2).
47. **[minor] Screenshot masking misses react-select values.** Accepted. Masks cover the whole
    field container; the test samples pixels over `.select__single-value` (4.3, D8, 12.4).
48. **[minor] Which approved cover letter is undefined; stale PDF possible.** Accepted. Latest
    version only, only if approved; new `content_sha256` column and re-render on mismatch; document
    id, version and hash in the plan hash; the preview shows the text (D11, 5.1, 6.9).
49. **[minor] Auto-clicking the CAPTCHA checkbox contradicts the no-solver reasoning.** Accepted:
    new D14, recommendation A (never click it) (D14, 6.11).
50. **[minor] Consent carries into unattended mode; global acknowledgments.** Accepted.
    `consent_scope`; M8 needs re-consent; statements that say "I have read" or link a document need
    a company; links shown in the preview (5.4, D9, 9.4).
51. **[major] The 24-hour watch ships after the first real submissions.** Accepted. The watch,
    badges, notices and Urgent kind are in M5b with the first handoff submissions; statistics in
    M5b; the 8.8 threshold in M6 (14).
52. **[major] `no_email_24h` fires when the mail reader is down.** Accepted (verified the sync
    columns in migrations/0038:21-38). The clock expires only when `last_ok_at >= watch_until`,
    `pending_ids_json` is empty and `recovery_state` is ''; otherwise the window is extended and
    the card says why; stalled watches never count for 8.8; `apply_agent.watch` health component;
    tests (6.16, 8.8, 12.3).
53. **[major] Schema fetch and Playwright probe have no injectable seam.** Accepted (with 10).
    `setup_requirement` asks the agent factory's `available()` probe; a no-network transport test
    runs the preflight and API suite (4.6, 12.6).
54. **[major] Opening the section creates an application row, a run row and a fetch.** Accepted
    (with 8 and 36). The check writes no run row; one-hour cache; test: five opens, no rows, at most
    one fetch (5.3, 6.0, 12.3).
55. **[major] Claim liveness is time-from-creation, but claims are held 20+ minutes.** Accepted.
    `heartbeat_at` and `handed_over_at`; held means running here or heartbeat under 2 minutes;
    `in_flight` and `unconfirmed` read heartbeats; the claim stays `clicking` through the security
    code wait; resolutions return 409 while held; a settle that finds no row after a confirmation
    page writes the event and a notice (5.2 rules 4 to 7, 5.6).
56. **[major] No run deadline or watchdog.** Accepted, choosing the child-process option: runs
    execute in a child process with per-kind deadlines, and a watchdog kills the process tree,
    because `frame.evaluate` has no timeout and Playwright's sync objects cannot be closed from
    another thread; orphaned runs are finished; polling stops on a stale heartbeat;
    `apply_agent.runner` health; watchdog test (4.6, 5.2 rule 7, 12.4, 13 R17).
57. **[major] Pacing limits ignore submissions that reached Greenhouse.** Accepted. Claims are now
    append-only, so the limits read `application_submit_claims.handed_over_at` in any state rather
    than a second log in `apply_runs`; tests for the security-code retry and the "didn't go
    through" retry (9.1, 12.3).
58. **[major] The confirmation email never resolves an unconfirmed claim.** Accepted. The watch
    scans uncertain attempts and tombstones for 14 days, flips a strong match to `submitted` with
    `resolved_by='email'`, and reconciles with a stage Phase 1 already moved (6.16, 12.3).
59. **[major] Outcome and required-field decisions are only testable with Chromium.** Accepted.
    New stdlib-only `apply/checks.py` in M3 with `decide_outcome`, `join`, `check_required` and
    `clean_rehearsal`, table-tested in the default suite, including the missing rows (4.7, 12.3).
60. **[major] Screenshot retention never runs automatically.** Accepted (verified: retention jobs
    are enqueued only from the admin route). The worker purges once per local day and sweeps
    orphan files; `run_retention` keeps its hook (5.6, 11, 12.3).
61. **[major] The "nothing left the browser" guarantee is method-based.** Partly. Accepted:
    `route_web_socket` with `playwright>=1.48`; `eager_get` and `websocket` scenarios; header
    reworded. Narrowed: the value guard applies to hosts other than Greenhouse (typeahead lookups
    to Greenhouse legitimately carry what was typed) and to values of 4 or more characters ("Yes"
    would match unrelated URLs) (4.3, 10.4, 12.2).
62. **[minor] Browser tests lack a student seam, timeouts and a headless policy.** Accepted.
    `ApplyTimeouts`, `student_hook`, `now` parameters; headless by default with one headed test
    behind `PIPELINE_HEADED_TESTS` (4.3, 12.4).
63. **[minor] Fail-on-skip needs changes the spec does not list.** Accepted.
    `tests/browser_support.py`; `test_outreach_forms.py` moves to it and is in Appendix B; lazy
    import; browser cache; required status check in M3 (12.4, 12.8).
64. **[minor] Milestones are misordered, and M5 is oversized.** Accepted. Test-to-milestone map
    (12.9); `REQUIRED_CHECK_SCRIPT` and the outcome detector in M3's `apply/checks.py`; the sandbox
    flag and fake schema client in M4; M4 limited to text, select and name answers; M5 split into
    M5a and M5b; live rehearsals moved to a post-merge rollout checklist (14).
65. **[minor] The gate reset cannot be computed; "clean" is ambiguous.** Accepted (with 24).
    Stored reset timestamp; `clean` defined in `apply_checks.clean_rehearsal`: no required-field or
    join problem and no deferred file; optional blanks and deferred sensitive fields do not count
    against it (9.2).
66. **[minor] Rehearsal cap not implemented; network-idle wait cannot work.** Accepted.
    `rehearsals_per_day` with `rehearsal_block` and a timezone test; the per-choice settle uses an
    in-flight request counter (4.3, 5.6, 9.1).
67. **[minor] Missing truth-table rows; two 15-minute clocks.** Accepted. One clock (the confirmed
    rehearsal's age, checked at start and at hand-over); rows 40 to 48 added (6.0 step 8, 7.5).
68. **[minor] The claim-race test passes without exercising the lock.** Accepted. The claim is
    tested directly on two connections, and with two `create_app` instances on one SQLite file
    (12.3).
69. **[minor] Loopback demo contradicts the no-browser rule.** Accepted: dropped (with 30 and 46).
70. **[minor] The static click guard misses other ways to submit.** Accepted. The static scan
    covers `press`, `keyboard.`, `mouse.`, `tap`, `dispatch_event` and `requestSubmit`/`.submit(`
    in strings, and every rehearsal and handoff browser test asserts no submit-path POST except one
    the student hook caused (12.4, 12.7).

## Codex round 1 (verdict: REVISE)

1. **Rehearsal can leak application data (GET, WebSockets).** Accepted; same changes as internal
   41 and 61 (4.3, 10.4).
2. **The handoff can send after its claim is released.** Accepted; same as internal 34 (2) and
   (5): the hand-over runs inside the route handler before the POST continues, and the browser
   closes before a timed-out claim is settled (6.13).
3. **A confirmation URL alone can produce false success.** Accepted. `submitted` now requires a
   POST to `submitPath` seen by this run with a 2xx or 3xx answer, then the confirmation path, and
   the form absent; `confirmation_without_post` fixture gives `unconfirmed` (6.14, 12.2).
4. **The plan hash omits what the student is approving.** Accepted. It covers question text,
   control, required flag, option labels, statement text and links, sources, value MACs, file
   hashes and the cover letter's id, version and content hash (6.6).
5. **Sensitive classification can approve the wrong category.** Accepted; same as internal 3 and
   4: unknown fields in compliance sections are never storable; prohibited categories are checked
   first; the most restrictive match wins (7.3).
6. **"No confirmation email" treated as a delivery warning when none may be expected.** Accepted.
   The card says "No confirmation email yet. Some employers don't send one", never a failure, and
   never suggests reapplying; D12 gained option C (recommended); 8.8's warning and R2 reworded;
   Appendix A item 12 records the change from PLAN.md:1206-1208 (6.16, 10.5).
7. **The email watch can corroborate the wrong application.** Accepted; same as internal 40
   (6.16).
8. **A picked résumé is not necessarily attachable.** Accepted; same as internal 7, plus the
   preflight checks the file exists and its SHA-256 matches (6.9).
9. **An upload-blocked rehearsal can pass the readiness gate.** Accepted. Such a rehearsal is not
   clean (9.2); the submit run still checks the upload before pressing Submit (6.10).
10. **The preflight mutates application history on page open.** Accepted; same as internal 8,
    36 and 54 (6.0).

## Left open after revision 2 (before Codex round 2)

- Section 3 (D1 to D14) awaits the student.
- `GREENHOUSE_LOOKUP_HOSTS` and the EEOC schema field names must be confirmed on a live board
  before M5a and M4 (13 R16, 7.3).
- Whether Greenhouse sends a confirmation email after an intake rejection is unknown (13 R2).
- Phase 1 can still move any `applying` row to applied on a `company_single` confirmation; that
  predates this phase (13 R11).

## Codex round 2 (verdict: REVISE)

Codex resumed the same thread in a read-only sandbox, with this prompt: "I revised
PHASE5-SPEC.md. Re-review it. Same rules. End with VERDICT: APPROVED or VERDICT: REVISE." It said
the revision resolves the earlier résumé, email, plan-hash and handoff-claim findings, and raised
four remaining gaps. All four were material. They were addressed in revision 2.1 (below). The run
allowed one resume round, so there is no round-3 verdict on these fixes.

1. **Double submit is still possible after hand-over.** Routing allowed every public request
   after hand-over, including a second POST to `submitPath` from a double click or a page script.
   **Accepted.** After hand-over exactly one POST to `submitPath` passes per attempt, plus one
   more only after a 428 security-code answer, once per prompt. Every other non-GET after
   hand-over is aborted and recorded: a second submit POST, and a POST to any other path or host.
   So a POST to an unexpected path is now blocked rather than let through, and 6.14 reports it as
   `failed`, `after_click=0`, "The form tried to send to an address the app doesn't recognize".
   That is accurate, because nothing that could carry the application passed. It replaces
   round 1's "`other_path_post` gives `unconfirmed`". New fixture `double_submit` (4.3 table,
   6.13, 6.14, 12.2, 12.3, 12.4).
2. **Pre-hand-over exceptions can carry application data.** The CAPTCHA and résumé-upload
   exceptions were not bound to exact endpoints and payloads. **Accepted:**
   - CAPTCHA traffic may pass only to `CAPTCHA_ENDPOINTS`, exact host and path prefixes pinned in a
     fixture, and the value guard now also checks request bodies, so a CAPTCHA request carrying a
     planned value is aborted (fixture `captcha_body_leak`).
   - Uploads: the exact upload flow on a board that uploads as you attach has not been seen live,
     so the spec no longer allows any upload before hand-over. `S3_UPLOAD_ENABLED=False` makes such
     boards rehearsal-only, and submit and handoff refuse them before filling. Enabling them later
     means allowing only the address Greenhouse's own response names, with the planned file's
     bytes (4.3, 6.3, 6.9, 7.4, 7.5 row 55, 13 R18).
   - The handoff timeout note about uploaded files is gone, since no upload can pass.
3. **The handoff transaction has no defined process boundary.** The parent owns the database,
   but the child's route handler must commit the hand-over before forwarding the POST.
   **Accepted.** 4.6 now specifies the pipe protocol: `hand_over()` in the child sends a
   synchronous request; the parent runs the transaction, commits, and only then replies; the
   child continues only on an explicit True, and treats False, an error, a closed pipe or no reply
   within 10 s as False, so `route.abort()`. The same applies to submit mode. `heartbeat()` and
   `cancelled()` are specified too (4.3 table, 4.6, 6.13).
4. **"Nothing has been sent" overstates rehearsal privacy.** GETs to Greenhouse and lookup hosts
   were still allowed after filling, and the value guard exempted Greenhouse hosts. **Accepted:**
   - after the first input, a rehearsal or lookup allows only GETs to the audited lookup endpoint
     of the field being typed (`GREENHOUSE_LOOKUP_ENDPOINTS`, exact host and path prefix per
     field, with a note of what each receives) and static assets (by resource type); every other
     request is aborted;
   - the value guard applies to every host, Greenhouse included, with exactly two exceptions: the
     submit POST, and a lookup GET carrying its own field's typed text;
   - the preview header now reads "Your application has not been submitted" and names the fields
     whose typed text went to the lookup service. §1, G2, 11 and Appendix A were reworded to match.
   - New fixtures `eager_get_greenhouse` and `lookup_leak`.
   - The whole routing policy is now a pure function, `apply_checks.route_decision`, table-tested
     in the default suite (4.3, 4.7, 5.5, 10.4, 12.3).

**Superseded round-1 wording.** Round-1 responses above that mention "CAPTCHA hosts", "the
résumé upload whose body is the planned bytes", or "Greenhouse and lookup hosts" describe
revision 2. Revision 2.1 tightened each of these as described in this section.

## Verdict history

| Round | Verdict | Findings | Outcome |
| --- | --- | --- | --- |
| Internal review | (no verdict) | 70 | 66 accepted (several with a variant fix, reason stated in the entry), 4 partly (33 line references, 37 handoff and pause, 39 age 18, 61 value-guard scope), none rejected |
| Codex round 1 | REVISE | 10 | All accepted |
| Codex round 2 | REVISE | 4 | All accepted and fixed in revision 2.1; not re-reviewed (one resume round only) |

## Left open

- **Section 3 (D1 to D14) awaits the student.** Recommended path: D1 B now (Finish in browser
  only), D2 B, D3 B (3), D4 as tabled, D5 A while D1 is B, D6 B, D7 A, D8 90 days with mask (i),
  D9 A while D1 is B, D10 A, D11 B, D12 C, D13 A, D14 A.
- **Codex has not re-reviewed revision 2.1** (the round-2 fixes). A further round would be the
  next step if wanted.
- Must be confirmed on a live board before the milestone that needs it:
  - `GREENHOUSE_LOOKUP_ENDPOINTS` and `CAPTCHA_ENDPOINTS` (before M5a);
  - the EEOC schema field names (before M4);
  - the upload-as-you-attach flow (only if `S3_UPLOAD_ENABLED` is ever wanted).
- Unknown: whether Greenhouse sends a confirmation email after an intake rejection (13 R2).
- Existing behaviour, not changed here: Phase 1 can move any `applying` row to applied on a
  `company_single` confirmation (13 R11).
- Child-process isolation with a process-tree kill is new machinery for this codebase (13 R17).
