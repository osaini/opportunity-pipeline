# UI, UX, and bug-hunting pipeline

The unittest suite under `tests/` exercises the API through `TestClient` and
never opens a browser. CI checks `app.js` with `node --check`, which only proves
it parses. Everything between "the JSON is correct" and "a person can use this"
was unverified. This pipeline covers that gap.

It has four layers. The first three run in CI; the fourth is interactive.

| Layer | What it catches | Where |
| --- | --- | --- |
| Browser suite | Broken rendering, dead controls, failed journeys, console errors, 4xx/5xx during a flow | `tests/ui/`, CI job `ui` |
| Accessibility + UX | WCAG failures, unreachable keyboard paths, invisible focus, mobile layout breakage | `tests/ui/test_accessibility.py`, `test_keyboard.py`, `test_responsive.py` |
| Contract fuzzing | Unhandled exceptions from schema-valid input | `scripts/run_api_fuzz.py`, CI job `api-fuzz` |
| Exploratory (Playwright MCP) | Everything a written assertion did not anticipate | `.mcp.json`, driven conversationally |

## Setup

```bash
powershell -File .\scripts\ui-test.ps1 -Setup
```

(This machine has Windows PowerShell 5.1, not `pwsh`. From an interactive prompt
`.\scripts\ui-test.ps1 -Setup` also works — `CurrentUser` is `RemoteSigned`, which
permits locally-authored scripts.)

That creates `.venv-ui` on Python 3.12 (matching CI — Playwright wheels lag newer
releases), installs `requirements-web.lock` plus `requirements-ui.txt`, and
downloads Chromium.

The fuzzer needs a second environment, because schemathesis pins `starlette<1`
and this project pins `starlette==1.3.1`. They cannot coexist. Schemathesis only
needs an HTTP URL, never the application object, so the split costs nothing:

```bash
py -3.12 -m venv .venv-fuzz && .venv-fuzz/Scripts/pip install -r requirements-fuzz.txt
```

Playwright MCP is pinned in `package.json`:

```bash
npm install
```

## Running

The PowerShell wrapper is a convenience for humans. Anything scripted — CI, or an
agent working in a bash shell — should call the interpreter directly, which works
in any shell and ignores execution policy:

```bash
.venv-ui/Scripts/python -m pytest tests/ui -q
```

| Command | Purpose |
| --- | --- |
| `./scripts/ui-test.ps1` | Every browser test except the visual baselines |
| `./scripts/ui-test.ps1 -Headed -Filter test_keyboard` | Watch a suite run in a visible browser |
| `./scripts/ui-test.ps1 -Visual` | Compare screenshots against this platform's baselines |
| `./scripts/ui-test.ps1 -UpdateBaselines` | Rewrite baselines after an intentional design change |
| `py -3 scripts/run_api_fuzz.py` | Fuzz every OpenAPI operation for unhandled exceptions |
| `py -3 scripts/run_api_fuzz.py --strict` | Add response-schema and status-code conformance checks |

Failure traces land in `data/ui-artifacts`. Open one with:

```bash
.venv-ui/Scripts/playwright show-trace data/ui-artifacts/<test>/trace.zip
```

## How the suite stays deterministic

- **Its own database.** `tests/ui/conftest.py` migrates the same legacy fixture
  the unittest suite uses into a temporary directory. `data/platform.db` is never
  touched, and uploads are redirected into the temp tree.
- **Rewound between tests.** One uvicorn instance serves the session, because
  pytest-playwright's `browser_context_args` is session-scoped and so `base_url`
  must be. Isolation instead comes from restoring a pristine copy of the database
  file before each test — the app opens and closes a connection per request, so
  the file is free between them.
- **The rate limiter is raised.** Every test shares `127.0.0.1`, so the per-IP
  sliding window sees the whole suite as one client. The limiter is covered by
  the API-level unittest suite.
- **Runtime failures fail the test.** `page_is_clean` is autouse: any uncaught
  exception, console error, failed request, or 4xx/5xx fails the test that
  provoked it, whether or not it was asserted on. Tests that deliberately trigger
  an error opt out with `@pytest.mark.allow_page_errors` and assert on the
  `defects` fixture themselves.

Two allowances are deliberate: a `401` on `GET /api/v1/session` (the auth gate
probing on load) and `net::ERR_ABORTED` (a navigation cancelling an in-flight
fetch). Both are documented at their definitions in `conftest.py`.

## Defects found and fixed

The pipeline originally found six coverage-seam failures. The phase-verification
pass on 2026-08-23 fixed them and removed every strict xfail/quarantine marker;
the same tests now run as ordinary regression guards.

| Severity | Original defect | Current guard |
| --- | --- | --- |
| P0 | Browser writes lost `X-CSRF-Token` when `options.headers` replaced the merged headers, breaking Save, Pass, Undo, outbox replay, and account deletion. | Real cookie-authenticated Save/Pass journeys in `test_student_journey.py` |
| P1 | `hmac.compare_digest(str, str)` raised on non-ASCII credentials and returned 500. | Unicode session/invite browser cases plus direct arbitrary-Unicode comparison coverage |
| P2 | Signing in from a deep link always returned to Discover. | `test_sign_in_preserves_the_requested_destination` |
| P2 | Mobile/tablet CSS removed the only Sign out affordance. | Four-viewport visibility and target-size matrix |
| P1 data isolation | The legacy read-model view hardcoded `local-user`, leaking owner scores and interaction state into other student reads. | Cross-user score/intent/detail/list isolation in `test_tenancy.py` |
| — | Accessibility backlog: contrast, hidden focus, invalid list roles, missing file labels, and undersized controls. | Unquarantined axe, keyboard, responsive, and visual-baseline suites |
| P2 layout | At 375px Profile was 580px wide (bare `1fr` grid track plus unbreakable dossier JSON) and Applications 13px wide (toolbar buttons never wrapped); found in the 2026-09-17 polish pass. | `test_no_view_scrolls_sideways_on_a_phone` (every authenticated view at 375px) |

### Defects found 2026-09-14 (Playwright MCP)

An exploratory MCP pass found defects the written suite missed. All are fixed;
each row names the test that now keeps it found.

| Severity | Original defect | Current guard |
| --- | --- | --- |
| P1 data loss | "Save details" cleared an application's follow-up: a date-only value renders empty in a `datetime-local` input, and the empty field was saved as "clear". | `test_saving_tracker_details_without_edits_keeps_the_follow_up`; `test_saving_notes_alone_leaves_the_follow_up_untouched` |
| P1 honest failure | Any 5xx was reported as "Connection lost" and queued; the queue was not per user, survived sign-out, and replayed silently on the next sign-in. | `test_a_server_error_is_shown_as_itself_and_never_queued`; `test_an_offline_action_is_queued_for_this_user_and_synced_on_return` |
| P1 source integrity | The agent answered "What closes soon?" with deadlines that had already passed. | `test_agent_deadline_answers_exclude_deadlines_that_already_passed` |
| P1 source integrity | Saving the profile stored "Not answered" fields as `confirmed_fact` dossier items, shareable with employers. | `test_unanswered_profile_fields_never_become_confirmed_facts` |
| P2 | Calendar dates (deadline, posted, follow-up) rendered a day early west of UTC, contradicting the posting text. | `test_deadlines_keep_their_calendar_day_west_of_utc` |
| P2 a11y | The sign-in gate and detail panel did not trap focus; at 375px Tab reached a hidden Apply link. Escape and re-renders dropped focus on `body`. | `test_detail_panel_keeps_focus_inside_*`, `test_sign_in_gate_keeps_focus_inside_and_hands_it_to_the_page`, `test_detail_panel_closes_on_escape_and_restores_focus` (now asserts focus), `test_unsaving_a_card_keeps_keyboard_focus_on_the_list` |
| P2 a11y | Nav buttons had no accessible name on the 721–960px icon rail. | `test_primary_navigation_keeps_accessible_names` (adds a 900px viewport) |
| P2 privacy | After sign-out or session expiry, the previous user's name, stats, and cards stayed in the DOM behind the gate. | `test_signing_out_clears_private_data_*`; `test_an_expired_session_hides_the_previous_workspace` |
| P3 | Filter options duplicated on every re-sign-in; "tracked applications" counted shortlisted roles; the Applications header showed Discover copy; passed deadlines were unflagged; score reasons did not add up to the score; "1 saved roles". | `test_signing_out_clears_private_data_and_signing_back_in_does_not_duplicate_filters`, `test_the_applications_tile_matches_the_applications_view`, `test_each_authenticated_view_renders`, `test_the_score_explanation_accounts_for_the_whole_score`, `test_single_counts_read_as_singular` |

### Defect found 2026-09-21 (building recording playback)

| Severity | Original defect | Current guard |
| --- | --- | --- |
| P1 | Every response sent `Permissions-Policy: microphone=()`, which blocks `getUserMedia` on the app's own pages, so mock-interview Record answer and Transcribe voice never worked in a real browser. `test_mock_interview_recording_has_visible_state_and_private_upload` replaces `getUserMedia` with a stub and so could not see the policy. The header is now `microphone=(self)`. | `test_the_app_lets_its_own_pages_ask_for_the_microphone` (reads `document.featurePolicy`); header assertion in `test_platform.py` |

The visual baselines under `tests/ui/baselines/windows-amd64` were regenerated on
2026-09-15 after checking each diff: saved roles no longer appear in the Discover
review queue ("1 to review"), the tracked-applications tile no longer counts
shortlisted roles, and posted dates keep their calendar day.

Two additional non-browser pins are also fixed: CLI startup `OSError`/
`SubprocessError` now crosses the provider boundary as `RuntimeError`, and the
extension never proposes answer-library content for sensitive fields. The
extension's no-submit guarantee remains unchanged.

### Defects found 2026-09-19 (reported from real use)

Both were in the outreach draft editor, and both were reported by the student
rather than by a suite: the browser tests drove Approve and the reload actions,
but never with unsaved text in the box.

| Severity | Original defect | Current guard |
| --- | --- | --- |
| P1 data loss | Every action that reloads the outreach pane (Confirm research, a status change, applying a contact, the deep-search poll) silently discarded whatever was typed into the draft and not yet saved. Switching companies asked first; pressing a button in the same pane did not. | `test_an_action_beside_the_draft_keeps_unsaved_edits`, and `test_regenerating_over_unsaved_edits_shows_the_new_draft` for the case where replacing them is the point |
| P2 | "Approve draft" refused edits in the box and printed "Save your changes first" below the claims and the provenance note, far enough from the button to read as nothing happening. Approving now saves those words and approves what was saved, and the status line sits under the buttons. | `test_approving_saves_the_words_it_approves`; `test_declining_the_warnings_still_says_where_the_edits_went` |

The unsaved text is carried across one reload only and stays unsaved: the word
count still follows the box, and the hand-off to email still refuses, because it
sends the approved draft rather than the text box.

## Exploratory testing with Playwright MCP

The written suite checks what someone thought to assert. Playwright MCP is for
everything else — driving the real UI conversationally, reading the accessibility
tree, and following a hunch.

Start the sandbox app:

```bash
py -3 scripts/serve_for_testing.py
```

It seeds a throwaway database from the same fixture, prints fixed tokens, and
serves on `http://127.0.0.1:8799`. Sign in by pasting `sandbox-owner-token` into
the "Owner invitation" field. Nothing it does can reach real data.

`.mcp.json` restricts the browser to that origin via `--allowed-origins`, runs
`--isolated` so no profile is written to disk, and saves traces to
`data/playwright-mcp`.

> The Node install adds `C:\Program Files\nodejs` to the machine PATH, but a
> running editor keeps the PATH it started with. Restart Claude Code once before
> the `playwright` MCP server will launch.

Useful things to ask for, in rough order of value:

- "Sign in and try to complete an application end to end. Stop at anything
  confusing and tell me why."
- "Read the accessibility tree on the Prepare view and tell me what a screen
  reader user would hear."
- "Resize to 375px and walk the whole Discover flow using only the keyboard."
- "Open the agent view and try to make it produce an unsupported claim."

When exploration finds something real, add it to `tests/ui/` so it stays found.

### Defects found 2026-09-19 (import/export seam)

Found by reading for the seam rather than by a failing test: no suite imported a
file the product had itself exported. The round trip is where an unchecked
location acquired the student's name.

| Severity | Original defect | Current guard |
| --- | --- | --- |
| P0 source integrity | `create_target` stamped `location_basis = 'manual'` for every origin that was not `discovery`, so a location arriving in an import file was recorded as the student's own entry: rendered "from your entry", counted as `location_verified`, relied on by drafts asserting the student could be there in person, and — because `manual` outranks every source — never correctable by the company's own site or its Form D. A deep-search location laundered its provenance simply by being exported and re-imported. | `ImportedLocationProvenanceTests` in `tests/test_outreach.py`; `test_an_imported_location_says_a_file_said_so_and_nothing_checked_it` in `tests/ui/` |
| P1 source integrity | `location_usable` tested `basis != "research"`, so a blank or unrecognised basis read as **verified**. Fail-open is what made the above dangerous rather than untidy. | `test_an_unverified_basis_is_never_read_as_confirmed`; `test_a_location_nothing_established_is_still_due_for_checking` |
| P1 | Confirming a location sent a bare `confirm_location: true`. A locator or profile pass running between the render and the click made the student vouch for a place they never saw — permanently, since `manual` is never overwritten. It now carries the displayed location and the write is guarded on location, basis and inferred; a mismatch is a 409. | `ConfirmLocationRaceTests` |
| P2 | `apply_location` decided from a row read outside its write, so two enrichment passes could both read the weak state and the weaker result could land last. | `test_a_weaker_writer_cannot_land_on_top_of_a_stronger_one` |
| P2 | The export omitted `location_inferred`, presenting a place the site merely mentions as a flat `company_site`. | `test_a_round_trip_through_export_and_import_cannot_launder_provenance` |

`migrations/0019_outreach_import_location_basis.sql` repairs rows the bug already
laundered. It demotes every `origin='import'` row still holding `manual` unless
an explicit `location_confirmed` event evidences the student really did vouch
for it — a demoted row keeps its text and gains a Confirm button, whereas a row
wrongly left as `manual` is `location_verified` and so renders no button at all,
leaving the student no way to even see the claim. `updated_at` is deliberately
untouched: it drives the "Recently updated" sort, and correcting the app's own
past mistake is not activity by the student.

## Adding to the suite

- New page or view: add it to `AUTHENTICATED_VIEWS` or `PUBLIC_ROUTES` in
  `conftest.py`. The smoke, accessibility, and responsive suites pick it up.
- New journey: extend `test_student_journey.py`. Prefer asserting what the user
  sees over what the DOM contains.
- Fixing a quarantined defect: delete its entry from `KNOWN_VIOLATIONS` or
  `KNOWN_UNDERSIZED`, or remove the `xfail`. Strict xfails fail when they start
  passing, so the marker cannot outlive the bug.
