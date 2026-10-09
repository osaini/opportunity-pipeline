# Known defects

This file lists defects found during the 2026-09/10 refactor audit and its phase reviews. Each one was checked against the code at `be33c50` (main after PR #64). On 2026-10-02, after the package move (PRs #65 and #66), every path and line below was re-pointed at the current tree, and each cited line was opened to confirm it still holds the code the entry names. The move changed no behaviour, so the entries still describe what the code does. None of the entries below is fixed. The owner approved fixing the six serious bugs first, and PR #60 fixed those: the email-draft number check, the student-agent CLI chat sandbox, Gmail label tables missed by account erase and export, asset versioning outside `static_dir`, pollers that kept running after sign-out, and an extension scan that carried over to another application. One entry below (the labelling worker) was found in review of those fixes and left for the owner to decide. The other one found there, the Codex sandbox, was fixed on 2026-10-02 and removed; the narrower gap it left is listed in its place. The owner then approved fixing this file's six high-severity entries; they were fixed on 2026-10-02 and removed, and one narrower high-severity gap those fixes left is listed in their place.

Severity rules:

- **high**: source integrity (an inferred or invented value shown or sent as confirmed) or send safety (mail going out that the student was not told about, or was told would not go).
- **medium**: a crash, data loss, a privacy leak, or wrong visible state.
- **low**: cosmetic issues, logs, and latent bugs that no current input triggers.

"Notes" are the audit note numbers each entry came from. Where several notes describe the same root cause, they are merged into one entry.

When you fix a defect, delete its entry in the same change and name it in the PR (AGENTS.md hard rule 5, and rule 16 of section 8). When you find one and do not fix it, add an entry here with the same fields. Line numbers drift as the code changes, so find the code by the function or name given, not by the line alone. A line number after a function name is a line inside that function.

## Summary

| Area | High | Medium | Low | Total |
| --- | ---: | ---: | ---: | ---: |
| Frontend (web UI) | 0 | 5 | 3 | 8 |
| Browser extension | 0 | 1 | 0 | 1 |
| Apply for me | 0 | 4 | 11 | 15 |
| Mail, Gmail and inboxes | 0 | 4 | 8 | 12 |
| Outreach drafting, research, forms and CLI | 0 | 8 | 4 | 12 |
| Agents and notifications | 0 | 1 | 1 | 2 |
| Web API, auth and storage | 0 | 4 | 1 | 5 |
| Scoring, scheduling and configuration | 1 | 2 | 8 | 11 |
| Packaging and docs | 0 | 2 | 1 | 3 |
| Test tooling | 0 | 0 | 2 | 2 |
| **Total** | **1** | **31** | **39** | **71** |

## Start here: the high-severity entries

The audit's six high-severity defects were fixed on 2026-10-02 (branch `osaini/fix-high-defects`): the Outreach page's
send wording, HTML-only job and sequence mail counted as replies, unsupported numbers in decline thank-yous, numbers
run into a lowercase word in drafts, the student's own greeting word, and the repost FLAG on profile save. The gap that
listed an Outlook-quoted sequence bump with a trailing tracking pixel as a confirmed reply no longer exists: the
sales-tool check reads every link, quoted ones included (`all_link_hosts`). One narrower high-severity gap remains:

- [The repost FLAG disappears the day after the daily purge removes the retired twin](#the-repost-flag-disappears-the-day-after-the-daily-purge-removes-the-retired-twin)

The entry flagged for an owner decision is
[the labelling worker](#the-gmail-labelling-worker-can-write-label-rows-for-an-account-that-was-just-deleted).

---

## Frontend (web UI)

### A facets/stats failure after a valid sign-in shows the sign-in gate and wipes the workspace
- **Severity:** medium (notes 7)
- **Where:** `opportunity_app/static/app.js:66-84` `initialize()`, `:87-117` authForm submit; `opportunity_app/static/app-session.js:50-58` `showAuth()`
- **What happens:** Any non-auth error after `hideAuth` falls into a catch that calls `showAuth`, such as a 500 from `/facets` or `/stats`, or "Connection lost." `showAuth` then calls `resetWorkspace()` and covers a session that is still valid with the sign-in gate. The submit handler also shows the server error on the sign-in form, so it reads as a failed sign-in.
- **Suggested fix:** Move the post-sign-in steps into one `enterApp(session)` helper. Once `hideAuth` has run, report failures with `showError`. Keep `showAuth` for failures of the `/session` probe and the POST only.
- **Regression suite:** tests/ui (route `/api/v1/facets` to 500 after sign-in; the gate stays hidden and an error is shown)

### Agent: a failed message send wipes its error and the student's typed text
- **Severity:** medium (notes 8)
- **Where:** `opportunity_app/static/app-agent.js:177-191` `sendContent()` in `loadAgent()`
- **What happens:** When the messages POST fails (budget used up, provider error, 422), the catch sets the composer status and then awaits `loadAgent()`. `runViewLoad` clears errors and rebuilds the results, so the status line and the typed question are both lost.
- **Suggested fix:** Call `showError(error.message)` after the reload, and put the unsent text back into the new composer. Alternatively, skip the reload when the server did not save the message.
- **Regression suite:** tests/ui (agent view: stub the POST to fail; the error and the text stay visible)

### Thirteen click/submit handlers await api() with no catch, so failures are silent
- **Severity:** medium (notes 9)
- **Where:** `app-agent.js:96`; `app-preparation.js:101, 409, 420`; `app-profile.js:396, 407, 648, 660, 675, 704, 712, 724, 767` (all under `opportunity_app/static/`)
- **What happens:** These handlers have no try/catch, and the page has no `unhandledrejection` listener. A 4xx/5xx or network error shows nothing. If Delete account, a device Revoke or a dossier share revoke fails, the student is not told, so they believe data was deleted or access was revoked when it was not. The PDF download (`app-preparation.js:65-68`) uses a raw fetch: a 422 shows "[object Object]", and a 401 does not raise the gate.
- **Suggested fix:** Add a shared `runAction(button, statusLine, fn)` helper that disables the button and reports `error.message`. Read the PDF error with `errorDetailText`.
- **Regression suite:** tests/ui (stub each DELETE to 500; a visible error appears). tests/ unittest cannot see this.

### A different user on a shared browser inherits the previous user's search, outreach filter and tabs
- **Severity:** medium, privacy and wrong state (notes 149, 196)
- **Where:** `opportunity_app/static/app-session.js:27-48` `resetWorkspace()`
- **What happens:** `resetWorkspace()` leaves several things in place: the search input, the sort and filter selects, `state.subtabs`, the outreach query, tag, sort, open and keep state, `deepSearchOpen` and `agentThreadId`. User B signs in after A and gets A's search and filters, and A's search text stays visible behind the gate.
- **Suggested fix:** Reset all of these from the defaults in `app-context.js`.
- **Regression suite:** tests/ui (owner sets a search and an outreach filter, signs out, a student signs in, defaults are shown)

### Saving or moving one application card discards unsaved notes, follow-up dates and open panels in every other card
- **Severity:** medium, data loss (notes 151)
- **Where:** `opportunity_app/static/app-applications.js:491` `loadApplications()` (only `unsavedStageChoices()` at `:471` is carried over); triggered from `:84` and `:136`
- **What happens:** After a PATCH on card A, the whole board is rebuilt. Notes or a follow-up date typed into card B are lost without warning, and B's open details panel closes.
- **Suggested fix:** Before the reload, record each card's unsaved textarea and follow-up values and its open panels, keyed by `data-application-id`, and restore them afterwards the way `restoreStageChoices()` does. Alternatively, re-render only the card that changed.
- **Regression suite:** tests/ui (type notes in one card, change another card's stage, the text survives)

### ops.js request() turns error bodies into SyntaxError or "[object Object]"; the ops and market pages use undefined CSS classes
- **Severity:** low (notes 10)
- **Where:** `opportunity_app/static/ops.js:10-18` `request()`; `ops.html:10-15`; `market.html:5`; `market.js:12`
- **What happens:** `response.json()` runs before the `ok` check, so a plain-text 500 throws a SyntaxError and a 422 detail list shows as "[object Object]". The pages use `.shell`, `.topbar` and `.panel`, which `styles.css` does not define, so they render unstyled.
- **Suggested fix:** Parse the body inside a try/catch and format the error with `errorDetailText`. Either add minimal rules for those classes or remove the classes.
- **Regression suite:** tests/ui (page smoke for /admin, /employer and /market with a stubbed 422)

### The Apply tracker error shows "[object Object]" when the server's detail is not a string
- **Severity:** low (notes 148)
- **Where:** `opportunity_app/static/app-opportunities.js:139-146` apply click handler (`apply_opened` POST)
- **What happens:** A validation-list `detail` is interpolated as is, so the error reads "the tracker could not record it: [object Object]".
- **Suggested fix:** ``errorDetailText(body.detail) || `Request failed (${response.status})` ``.
- **Regression suite:** tests/ui (route the actions POST to a 422 with a list detail)

### The reduced-motion rule does not stop the progress-bar width transition
- **Severity:** low (notes 155)
- **Where:** `opportunity_app/static/styles.css:505` `.refresh-dialog progress::-webkit-progress-value` (global rule at `:791`)
- **What happens:** The `*, *::before, *::after` selector does not match the progress-value pseudo-element, so the bars in the Refresh dialog still animate under `prefers-reduced-motion: reduce`.
- **Suggested fix:** Add `transition: none` for that selector inside a reduced-motion block, as was done for `select::picker-icon`.
- **Regression suite:** tests/ui (`reduced_motion='reduce'`, assert the computed transition is none)

## Browser extension

### The side panel calls chrome.permissions.request on every API call, including the startup flush, which has no user gesture
- **Severity:** medium, data loss (notes 32)
- **Where:** `apps/extension/sidepanel.js:51-56` `api()`; `:525` startup `flushPendingMetadata`
- **What happens:** `permissions.request` needs a user gesture. On panel open, the startup flush runs without one and without a `.catch`, so metadata queued while offline is never flushed and the rejection goes unhandled. Late calls such as `syncStep` after a long scan can fail the same way. The harness stubs `request` to always succeed (`tests/extension/sidepanel_harness.mjs:185`). Chrome's exact gesture behaviour still needs confirming in real Chrome.
- **Suggested fix:** Check `chrome.permissions.contains` first, and call `request` only from the Pair click. Add a `.catch` to the startup flush.
- **Regression suite:** `node tests/extension/run_tests.mjs` (a permissions stub that rejects without a gesture, plus a stored pending queue); confirm once in real Chrome

## Apply for me

The first three were left open by PR #54 (the fail-closed net) and recorded here on 2026-10-03; each was narrowed on 2026-10-08 (the lists were widened, a page's headings now count, and the chain follows every follow-up-shaped child). Apply for me never carries an answer across companies, so each can at worst affect one company's own saved answer, and the student still presses Submit (D1 B). The next three were found while building the rehearsal engine (M5a), and the next one while building Finish in browser (M5b part 2); none was fixed there. The next four were found on 2026-10-04 and 2026-10-08, and the last one on 2026-10-08 while diagnosing a kill-ordering test flake.

### Agreement-shaped choices and signatures in wordings no list has still fill from a same-company saved answer
- **Severity:** medium (PR #54 review; narrowed on 2026-10-08)
- **Where:** `opportunity_app/apply/classify.py` `field_net()` (`_AGREEMENT_OPTION`, `_SIGNATURE`, `_SIGNED_HEADING`); `apps/extension/apply-engine.js` `AGREEMENT_OPTION`, `SIGNATURE`
- **What happens:** A one-option select is read as a tick box, a select whose options or heading hit the agreement topic is left for the student, and a single-line text field is a signature line when its heading, or its description beside a name heading, says it signs ("By typing your name...", "electronically signing", "I certify that..."). A select that agrees in words none of those lists has ("I honour the policy", "Done" and "Not yet" for "Please indicate your compliance") or a signature line worded in an unusual way still gets no `agreement` mark, so a saved answer the student gave at the same company for another posting fills it. Nothing carries across companies, and the student still presses Submit.
- **Suggested fix:** Keep adding wordings from real Greenhouse questions as students meet them. A word list cannot be complete; the per-company rule is what bounds a miss.
- **Regression suite:** tests/ unittest (`test_apply_broad_net`), node tests/extension/run_tests.mjs

### A grandchild of a never-storable question still gets a save form when its parent is long and does not open as a follow-up
- **Severity:** low (PR #54 review; narrowed on 2026-10-08)
- **Where:** `opportunity_app/apply/policy.py` `build_plan()` (`never_chain`, `follow_up_shaped`); `apps/extension/apply-engine.js` `netReadings()` (`followsNever`)
- **What happens:** A never-storable topic runs down every follow-up-shaped child (short, a follow-up wording, or one that opens with a question word). A child of a felony question that is six or more words and does not open that way ("Please list the employer you worked for at the time of the incident") is refused in the plan, because it is filed under the felony question, but it does not pass the topic on, so its own follow-up ("Anything else") is ordinary: the Needs you view offers to save it, and the side panel offers Save on both. The plan cannot tell such a child from a long independent question that happens to sit under the parent ("Tell us about a project you are proud of"), which must not hand a topic on, so passing it on would over-block. A same-company row can fill the grandchild; nothing carries across companies.
- **Suggested fix:** Give the schema a way to tell a continuation from an independent question (the form's own grouping), then carry what the child took into `never_chain` and the engine's chain.
- **Regression suite:** tests/ unittest (`test_apply_broad_net`, `tests/fixtures/apply/net_chains.json`), node tests/extension/run_tests.mjs

### The broad never-storable net still misses wordings no list has, and a heading covers only the questions under it
- **Severity:** medium (PR #54 review; narrowed on 2026-10-08)
- **Where:** `opportunity_app/apply/classify.py` `NET_TOPICS` / `net_topics()` / `NET_SECTION`; `apps/extension/apply-engine.js` `netTopics` / `inNeverSection`
- **What happens:** The lists were widened from a fresh set of 128 never-storable wordings (criminal history, demographics, pay, security): the net missed 59 of them before and misses 11 now. Those left have no keyword to read ("Have you ever been fired?", "Do you have a record of any violations?", "Did you grow up in a rural community?", "Do you have relatives who live outside the US?"), or are immigration-status questions the precise classifier already files by category. A question under a demographic, compliance or background heading is now never storable whatever it says, but only in a run that has read the page: the engine reads the section, fieldset, region or group around each control (a heading that is a sibling of the fields, with nothing wrapped around them, is not read), and the plan takes the engine's mark from the scan. The check that drives the Needs you view reads no page, so it still offers to save such a question. A custom question under a plain heading ("Application questions") or none depends on the lists. A missed question is treated as ordinary, so the student can save its answer for that company and a later posting at the same company fills it; nothing carries across companies.
- **Suggested fix:** Keep adding wordings from real Greenhouse questions as students meet them. A word list cannot be complete; the per-company rule is what bounds a miss.
- **Regression suite:** tests/ unittest (`test_apply_broad_net`, `tests/fixtures/apply/broad_net.json`), node tests/extension/run_tests.mjs

### A request a page makes while its window is closing skips the route handler, so a hostile script can carry a typed value to one of the allowed hosts
- **Severity:** medium, privacy (found 2026-10-03, in the M5a recheck)
- **Where:** `opportunity_app/apply/agent.py` `NO_SIDE_CHANNELS` (the dismissal listeners, `sendBeacon`, keepalive) and `RESOLVABLE_HOSTS`; Playwright's `Page._onRoute` (stalls every request once `page.close()` has been called) with Chromium (lets a stalled request go when the page's session ends)
- **What happens:** Measured on Playwright 1.62's Chromium: an image, a plain fetch, an XHR, a stylesheet link, a beacon and a keepalive fetch made from a `pagehide`, `unload` or `visibilitychange` handler all reached a listener with no route handler call, with a route that refuses everything. The init script now stops that: the events never reach a page script, `sendBeacon` returns false, `keepalive` is always false, and the resolver rule lists only the hosts a rehearsal needs (not the analytics collector, my.greenhouse.io, www.google.com or the unconfirmed CAPTCHA hosts). What is left: a hostile page script that sends a request carrying a value on a timer, so that one is in flight when the window closes, still has that request released; it can only reach the names this run's own ATS uses (`ApplyAgent.run_launch_options`: for a Greenhouse run, Greenhouse's board, lookup and static hosts, fonts.googleapis.com, fonts.gstatic.com, www.recaptcha.net and www.gstatic.com; for a Lever run, Lever's two hosts, its font and logo hosts and the five hCaptcha hosts of its form, and the fonts), and the value guard refuses every one of its earlier requests. The preview's "nothing leaves the browser" is true of every request the handler judged.
- **Suggested fix:** Take the page offline before closing it: navigate to `about:blank` and wait while the route handler is still being asked (the agent's own closes), and for a window the student closes, load the form through a proxy the agent runs that sees every request, or cut the board hosts out of the resolver rule once the form is loaded.
- **Regression suite:** tests/test_apply_agent_browser.py (`SideChannelTests`: a page that sends one request every 20 ms with its value, closed with `page.close()`, with the listener on an allowed host name mapped to loopback)

### A prerender link in a visible window loads a page of Greenhouse's own with no request the request policy sees
- **Severity:** low (found 2026-10-03, in review of the rehearsal engine, M5a)
- **Where:** `opportunity_app/apply/agent.py` `NO_SIDE_CHANNELS` (its speculation sweep) and `LAUNCH_ARGS` (the resolver rule)
- **What happens:** A script on the board can add `<link rel="prerender" href="...">` after it reads a filled field. A visible Chromium (the build the app uses; the headless one ignores it) starts the load as the element is inserted, before the sweep that removes speculation rules can run, and no request reaches the route handler. The resolver rule (`resolver_rule`) means the target has to be one of this run's own ATS's hosts (for a Greenhouse run, Greenhouse's board, lookup and static hosts), so a value in the URL can reach Greenhouse and nothing else: another site, a name made up from a value and an IP address all fail inside Chromium. `<script type="speculationrules">`, the Protected Audience calls, Shared Storage and the DNS and preconnect hints are closed (`SideChannelTests`). The preview still says "the app saw nothing else you entered leave the browser" only after a lookup, and says nothing about this.
- **Suggested fix:** A Chromium switch or profile preference that turns link prerendering off (none of `Prerender2`, `NoStatePrefetch` or `--disable-prerender` did on Playwright 1.62's Chromium; the preference `net.network_prediction_options` needs a profile directory the launcher does not take), or load the form through a proxy that sees every request.
- **Regression suite:** tests/test_apply_agent_browser.py (a headed run that adds a prerender link to a page of the board's own host, asserting the listener hears nothing)

### A second app on the same database can close the first one's running rehearsal
- **Severity:** low (found 2026-10-03, in review of the rehearsal engine, M5a)
- **Where:** `opportunity_app/apply/runner.py` `orphaned` and `ApplyRunner._finish`, `opportunity_app/web/routers/apply_agent.py` the cancel route, `opportunity_app/apply/runs.py` `recover_stale`
- **What happens:** `orphaned` decides a "running" row is dead because this process holds no run of that id, on the reasoning that only this server runs rehearsals. Two servers on one database (`python -m opportunity_app.api` on another port beside the launcher's) break that: the second shows the first's live run as stopped after 15 seconds, its Stop closes the row as "The app stopped during this run", and `recover_stale` can do the same after two minutes of failed heartbeat writes. When the first run ends, its result is not stored (the runner now logs that and reports the row's outcome, not its own).
- **Suggested fix:** Write a server instance id on the run row when it starts, and let `orphaned`, the cancel route and `recover_stale` close only rows that carry this server's id (or none).
- **Left open on 2026-10-08 (the Apply for me hardening branch):** the id needs a new column on `apply_runs`, so a migration, and a migration number taken now would collide with the migrations other open branches add. Keeping the id in `evidence_json` instead is not safe: `record_ready` and the finish write replace the whole document while the run is going. `recover_stale` also decides by the claim's in-process hold (`claim_held`), which would need the same id. Do it with the next planned migration, as one change with the three readers.
- **Regression suite:** tests/test_apply_runner.py (two runners on one database; the second does not close the first's running row)

### The answers the student types in the Finish in browser window are not watched by the request guard
- **Severity:** low (found 2026-10-03, review of Finish in browser, M5b part 2)
- **Where:** `opportunity_app/apply/checks.py` `RouteState.values`, `leaked_field` and `route_decision`; `opportunity_app/apply/agent.py` `_refresh_values`
- **What happens:** The request guard looks for the values the app planned and typed (plain, URL-encoded, base64 and escaped forms). A field the app left for the student, and any answer the student types or changes in the window, is not in that set, so a page script that sends it in a request to another address is not stopped. After the press, reads from `job-boards.greenhouse.io` and `boards.greenhouse.io` are not checked (the confirmation page loads from there); every other address still is.
- **Suggested fix:** None that is cheap: guarding what the student types means reading the form's values in the window, which the app does not do. State it in the student-facing documents (done) and keep the boards' own addresses the only exemption.
- **Regression suite:** tests/test_apply_checks.py (`test_in_a_handoff_a_get_to_another_host_is_guarded_after_the_press_too`)

### The Greenhouse embed form is never used when a posting redirects to the company's own site
- **Severity:** low (found 2026-10-04, comparison with another project's ATS notes)
- **Where:** `opportunity_app/apply/greenhouse.py:48` `canonical_url`; `opportunity_app/apply/checks.py:406` (`offsite_navigation`)
- **What happens:** A company that embeds Greenhouse on its own domain makes `job-boards.greenhouse.io/<board>/jobs/<id>` redirect to that domain. The run navigates to the canonical URL, sees the redirect leave the Greenhouse hosts, and stops with "This posting sends applicants to {host}". Greenhouse's embed address (`job-boards.greenhouse.io/embed/job_app?for=<board>&token=<id>`) usually reaches the real form, is on `BOARD_HOSTS`, and the parser already accepts it as input, but nothing builds it as a fallback. Whether the form it opens can be filled under the app's browser policy has not been tried.
- **Suggested fix:** When the canonical URL redirects off the Greenhouse hosts, try the embed address once, and stop as now if that redirects off them too. Check on a rehearsal first that the embed form's file input is reachable.
- **Regression suite:** tests/test_apply_checks.py (an off-site redirect on the canonical URL falls back to the embed address once)

### An interview the student never moved on keeps asking before every application to that company
- **Severity:** low (found 2026-10-08, review of PR #81)
- **Where:** `opportunity_app/apply/runs.py` `_active_elsewhere()` (reads `applications.stage IN ('interview', 'offer')` only), called from `duplicate_block()` (`ASK_ACTIVE_AT_COMPANY`)
- **What happens:** The check reads the stage alone. An application left at "interview" after the process quietly ended (no rejection email, a stage the student never updated) counts as an interview in progress indefinitely, so every later Apply for me run at that company stops to ask "You have an interview in progress at ...", and unattended mode can never pass it. Nothing is sent and the student can tick past it, so the cost is a repeated question, not a wrong application.
- **Suggested fix:** Count an interview only while its `updated_at` is recent (for example 60 days), and say "an interview last updated on {date}" so the student can see why it was asked; or offer "this ended" beside the tick, which moves the old application out of the interview stage.
- **Regression suite:** tests/test_apply_active_interview.py (an interview last updated 90 days ago does not ask, or asks with its date)

### The route handler's value guard never sees cookies
- **Severity:** low (found 2026-10-08, review of the Lever request policy)
- **Where:** `opportunity_app/apply/agent.py` the route handler (`RouteRequest(... headers=request.headers ...)`); `opportunity_app/apply/checks.py` `leaked_field()`
- **What happens:** Playwright's `Request.headers` leaves out cookie headers, so a value a page script writes to `document.cookie` rides on the request to the page's own host and is never checked. `leaked_field` does read a `Cookie` header it is given (the pure-function tests in `tests/test_apply_lever_policy.py` show that), but the handler never gives it one. The cookie goes only to the cookie's own domain, which is the ATS's, so the reach is a value written into the ATS's own cookie jar, not another host.
- **Suggested fix:** Build the facts from `request.all_headers()` and fall back to refusing the request if that fails. Check first on a live run that `all_headers()` returns inside a route handler before the request is sent, since it waits for the browser's extra header info.
- **Regression suite:** tests/test_apply_agent_browser.py (a page that sets a cookie holding a planned value; the next request to its host is refused)

### A page script can send the attached résumé as text to an address the fill lets writes through
- **Severity:** medium, privacy (found 2026-10-08, review of the Lever driver)
- **Where:** `opportunity_app/apply/checks.py` `route_decision()` (the `upload_elsewhere` rule covers the planned file's own bytes, a multipart file part, an octet-stream body and a body of an odd declared type; after the student's first press only the planned file's own bytes), `LEVER_CLOUDFLARE_PATH_PREFIXES`, `LEVER_CAPTCHA_ENDPOINTS`
- **What happens:** Once the app has put the résumé in the file input, a script on the page can read `input.files[0]` and send it as a base64 or JSON string in a POST to the Cloudflare challenge path of the Lever host or to a recorded hCaptcha host. A compressed PDF or DOCX holds none of the student's words, so the value guard finds nothing, and the request is not a file upload by its form. The same holds for a GET in pieces. `upload_elsewhere` refuses the file as itself (the planned file's bytes as a whole body or a form part, in every phase), the multipart and octet-stream forms and any body of a declared type that is not text, URL-encoded or JSON (in the fill and until the student's first press, and ends the run); this one passes. So does a body with no declared type that is not the planned file's exact bytes, and, after the student's first press, anything that is not those bytes (a file the student chose, a re-encoded copy), since hCaptcha is running by then and a wrong reading would close the turn in the middle of it. Cloudflare's allowed path is wider than the one beacon path the recording saw (`/cdn-cgi/challenge-platform/h/g/jsd/oneshot/`) because no interstitial has been recorded and its own requests are unseen.
- **Suggested fix:** After a recording of a Cloudflare interstitial, narrow the allowed writes to the paths it and the beacon use. Refuse a write to these addresses whose body is longer than a beacon needs (a few hundred bytes), or whose bytes decode to the file's digest; or attach the résumé last, after the checks, for the page that does not read it on attach.
- **Regression suite:** tests/test_apply_lever_browser.py (`GuardTests`: a script that sends `input.files[0]` as text to each allowed write address ends the run)

### A Lever form over the text budget counts as "1 question" while every question is left to the student
- **Severity:** low (found 2026-10-08, review of Lever LV2)
- **Where:** `opportunity_app/apply/lever_form.py` `parse_lever_form()` (the `MAX_FORM_TEXT_CHARS` branch), which makes one plan row; the what's-missing view counts plan rows
- **What happens:** A page whose labels and answers carry more than 100,000 characters between its controls is not read at all. The parser returns one unreadable question named "Every question on the form" and the student is told, in words, that the page has too much text. Every real question is left to the student, but the what's-missing view counts the plan's rows, so it says "1 question".
- **Suggested fix:** Give the unreadable question a marker the view reads ("every question") and word the count from it, for example "Every question is yours to answer".
- **Regression suite:** tests/test_lever_form.py (a form over the budget; the count the view shows says every question is the student's)

### Lever's budget of text exempts card questions, and nothing limits how many card templates a page carries
- **Severity:** low (found 2026-10-08, review of Lever LV2)
- **Where:** `opportunity_app/apply/lever_form.py` `_text_spent()` and `_left_to_the_student()` (the `section == "custom"` exemptions), `_template()` (`MAX_TEMPLATE_BYTES`, `MAX_TEMPLATE_FIELDS`, `MAX_FIELD_OPTIONS` limit one template)
- **What happens:** The exemption says a card question's words are its own and the template's size limits them. That limit covers one template; a page can carry any number of them. Only the page cap of `LeverPageClient` (4 MB) bounds the total, so the text a plan reads is linear in the page and never multiplied by controls the way a shared label is. No test times a page of many large templates, and a page just under the cap is not counted against the budget at all.
- **Suggested fix:** Count a card question's label and options in `_text_spent()` against a budget of its own, sized from the largest real template (a university dropdown of about 3,300 options), and read the form as unreadable above it. Time a page of many templates first.
- **Regression suite:** tests/test_lever_form.py (many large card templates under the page cap are read in under three seconds, or the form is left to the student)

### A field the student types in the same moments as the page reads their file is named as filled by Lever
- **Severity:** low (found 2026-10-08, build of Lever LV4)
- **Where:** `opportunity_app/apply/agent.py` `_watch_student_read()`
- **What happens:** After the student attaches a file in the window, the agent names the fields Lever's reader changed by comparing what they held at the last look (every 250 ms) with what they hold once the page has applied its answer. A parser field the student types into between those two looks, and that the reader leaves alone because the student made it theirs, is named too: "Lever filled Current company ... Check it". It says check, and nothing is changed, but the sentence is not true of that field.
- **Suggested fix:** Have the press listener's world also report a trusted `input` on the form's named controls (their names, never their values) and leave those out of the comparison.
- **Regression suite:** tests/test_apply_lever_handoff_browser.py (the student types into the company box while the page reads their file; only the fields the reader changed are named)

### A process only the kill's own listing found is killed but never checked, so the close can be confirmed while it runs
- **Severity:** low, latent (found 2026-10-08, diagnosing the KillOrderingTests flake)
- **Where:** `opportunity_app/apply/runner.py` `_end_child()` (`killed = kill_tree(pid)`, then `_kill_survivors(outcome.pids, ...)`); `kill_tree()` lists the tree again through `descendants()`
- **What happens:** `_end_child` lists the processes below the child, records them in `outcome.pids`, then calls `kill_tree`, which lists the tree a second time and kills what it finds. `_kill_survivors` re-checks only `outcome.pids` (and the child), so a process that started between the two listings is killed by pid but never looked at again; `killed_pids` names it and `closed_confirmed` can be True while it is still running. With `_process_table` returning the second listing only for the kill, a process that survives the kill, and `process_alive` True for it, `_end_child` sets `closed_confirmed` True. For a claim still `claimed`, row 7 ("couldn't confirm the window closed") is then skipped. It needs a process born within milliseconds of the kill that also survives `SIGKILL` or `taskkill /F`. Chromium's main process is listed long before (the ready snapshot), so this has not been seen.
- **Suggested fix:** Pass `kill_tree`'s targets to `_kill_survivors` with the recorded pids (record their start times first, as `_remember` does), or list the tree once more after the kill and repeat until nothing new appears.
- **Regression suite:** tests/test_apply_handoff.py (a process that only the second listing finds and that survives the kill leaves `closed_confirmed` False)

## Mail, Gmail and inboxes

### Application inbox: a "Last, First" From name empties the sender, so the email is skipped
- **Severity:** medium, wrong state and missed mail (notes 25)
- **Where:** `opportunity_app/applications/mail_rules.py:93-98` `parse_message()`; `opportunity_app/applications/inbox.py:248` `_is_candidate()`, `:589-590` `decide()`
- **What happens:** `parseaddr('Lee, Greg <greg@acme.com>')` returns `('', '')` on Python 3.14, so an interview, rejection or offer email from a trusted domain is recorded as skipped. A From header with a trailing group (`, :;`) raises IndexError, and the email is set aside as an error. `sender()` in `opportunity_app/mail/message.py` already handles both cases. The own-mail check uses `.lower()` (see "own address" below).
- **Suggested fix:** Use `sender(message)` from `opportunity_app/mail/message.py` in `parse_message`, and compare the own address with `mailbox_key`.
- **Regression suite:** tests/ unittest (`test_application_inbox`: a trusted-domain sender with a comma in the display name becomes a candidate)

### outreach/labels.py rejects apostrophe addresses that outreach accepts, so "label: complete" can be wrong
- **Severity:** medium, wrong visible state (notes 44)
- **Where:** `opportunity_app/outreach/labels.py:120-121` `_ADDRESS_PART`/`_ADDRESS`, `:329` `_addresses()`; validation in `opportunity_app/outreach/targets.py:155` `EMAIL_ADDRESS`
- **What happens:** `o'brien@acme.com` passes contact validation and receives mail, but `_addresses` drops it. Those threads get no address mark and are not counted as unlabelled, so `whoami` can report 0 unlabelled threads while that contact's mail has no label. That breaks the completeness promise in AGENTS.md.
- **Suggested fix:** Use one shared address pattern for contact validation and labelling, and allow RFC 5322 atext such as an apostrophe. The exact pattern is the owner's decision.
- **Regression suite:** tests/ unittest (`test_outreach_labels`: an `o'brien@` contact produces an address mark)

### Send Now returns an error after the email has already gone out when cancel_send fails
- **Severity:** medium, wrong visible state (notes 115)
- **Where:** `opportunity_app/web/routers/outreach_delivery.py:91` `gmail_send_for_outreach()`
- **What happens:** `cancel_send` runs inside the same try as the send. If it raises (database locked, target deleted at the same time, a ValueError), the student sees a failed send for an email that was sent, and the scheduled row stays "scheduled". A duplicate send is still blocked by `_already_sent` in `opportunity_app/outreach/gmail.py:423`. That is why this stays medium and not high: the email is in Gmail Sent, and pressing Send again is refused with the already-sent notice, so no second mail goes out and the wrong state is corrected on retry. It would be high if that guard did not hold.
- **Suggested fix:** Return the send result regardless. Run `cancel_send` in its own try that logs the failure and adds a note to the response.
- **Regression suite:** tests/ unittest (TestClient POST `/outreach/{id}/gmail-send` with `cancel_send` patched to raise; expect a 200 carrying the sent result)

### capture_gmail_sends marks drafts as looked at before the connection check, so sends made in Gmail show up to an hour late after a reconnect
- **Severity:** low (notes 22, 57, 65, 90)
- **Where:** `opportunity_app/outreach/gmail_sends.py:286-290` `capture_gmail_sends()`
- **What happens:** `take_due` stamps the drafts, and then the not-connected return skips `_LOOKS.forget`. After a reconnect those drafts wait out their interval, so a draft sent from Gmail still shows as unsent. `check_deliveries` already forgets in the same situation.
- **Suggested fix:** Call `_LOOKS.forget(user_id, due)` before the not-connected return, or check the connection first. Update the LookSchedule docstring and the test that pins the current behaviour.
- **Regression suite:** tests/ unittest (`test_outreach_gmail_sends`: a disconnected check followed by a connected check looks at the draft at once)

### application_emails links to /mail/u/0 rather than the outreach account
- **Severity:** low (notes 16, 100)
- **Where:** `opportunity_app/applications/inbox.py:1274` `application_emails()`
- **What happens:** The link opens the first signed-in Google account. Students signed into several accounts land in the wrong mailbox. The outreach links already use `gmail_web_url` with `authuser`.
- **Suggested fix:** Build the link with `gmail_web_url(f"all/{quote(thread)}")` from `opportunity_app/outreach/config.py`. The URL shape changes, so the owner signs off.
- **Regression suite:** tests/ unittest (`test_application_inbox`: `gmail_url` carries `authuser`)

### Modules compare the student's own address with different normalizations (lower, casefold, mailbox_key)
- **Severity:** low, fails closed (notes 23, 92)
- **Where:** `opportunity_app/outreach/gmail.py:271-276` `_require_account()` (`.lower()`), `:142` `gmail_drafts_status` (`.casefold()`); `opportunity_app/applications/inbox.py:589` `decide()`; `opportunity_app/outreach/labels.py:353` `_own_addresses`; `opportunity_app/outreach/inbox.py` uses `mailbox_key`
- **What happens:** A dotted or +tag spelling of the connected account shows as "connected as a different account", and every send and draft is refused. `lower()` and `casefold()` can also disagree on non-ASCII addresses.
- **Suggested fix:** Use one own-address comparison everywhere. Which folding to use is the owner's decision.
- **Regression suite:** tests/ unittest (`test_outreach_gmail` with a dotted or +tag outreach address)

### outreach/delivery.py `_watched` compares a naive created_at with an aware Gmail send time
- **Severity:** low, latent (notes 26, 94)
- **Where:** `opportunity_app/outreach/delivery.py:307-311` `_watched()` (also `_bounced_since` `:318`)
- **What happens:** A `created_at` with no offset raises TypeError and stops the bounce check. The only writer stores aware UTC times, so this does not happen today.
- **Suggested fix:** Parse the time with `parse_app_instant`.
- **Regression suite:** tests/ unittest (a delivery check with a naive `created_at` row)

### _already_sent and _previous_draft assume an event's detail decodes to a dict
- **Severity:** low, latent, fails closed (notes 72)
- **Where:** `opportunity_app/outreach/gmail.py:375` `_already_sent()`, `:224` `_previous_draft()`
- **What happens:** A detail that is valid JSON but not an object raises AttributeError out of the send path. The app writes only dicts.
- **Suggested fix:** Skip any decoded detail that is not a dict, as `_watched` does.
- **Regression suite:** tests/ unittest (`test_outreach_gmail` with a list detail)

### pipeline_mailbox _Session.get raises TypeError on a 403 whose error reason is not a string
- **Severity:** low, latent (notes 88)
- **Where:** `opportunity_app/pipeline_mailbox.py:192` `_Session.get()`
- **What happens:** A list or dict reason is unhashable when tested against the frozenset, so the script reports "failed (TypeError)" instead of CANNOT_READ or WAIT.
- **Suggested fix:** Reuse `is_throttle` from `opportunity_app/integrations/gmail_client.py`.
- **Regression suite:** tests/ unittest (`test_pipeline_mailbox`)

### Apply for me's confirmation watch cannot tell that the reader was in another mailbox for part of the window
- **Severity:** low (found in review of the confirmation watch, M5b part 1; narrowed on 2026-10-08)
- **Where:** `opportunity_app/apply/watch.py` `mailbox_reason()` and `reader_health()`; `opportunity_app/applications/inbox.py` (`application_mail_sync` records no account)
- **What happens:** The claim now records the address it went out under (`detail.mailbox_hash`), and the watch compares the connected account with that at each watch pass, so editing the profile email after submitting no longer pauses it. A student who reconnects as another account and then back between two watch passes is not told the reader was in the wrong mailbox for part of it: the inbox reader's passes leave no record of the account they read, so the watch can still end as "no email in 24 hours".
- **Suggested fix:** Record the account hash each reader pass ran under (in `application_mail_sync`), and pause or extend the watch when any pass since the hand-over ran under another account.
- **Regression suite:** tests/ unittest (`test_apply_watch`: switch the account and back between two watch passes and expect a pause)

### An email that might be a confirmation and keeps failing to be decided stalls every Apply for me watch until its 13-day give-up
- **Severity:** low (found in review of the confirmation watch, M5b part 1; narrowed on 2026-10-08)
- **Where:** `opportunity_app/applications/inbox.py` `_retry_errors()` and `_decide_safely()`; `opportunity_app/apply/watch.py` `reader_health()` (`READER_SET_ASIDE`)
- **What happens:** A message set aside as an error is read again after half an hour, then after waits that double, for 14 days, and an email the reader parsed before the decision failed carries its sender and the match it found, so one that is not from Greenhouse and named none of the student's applications no longer holds the watch. An email that could be the confirmation (from Greenhouse, or from the company's own domain and naming one of the applications) or that could not even be parsed, and whose reading fails every time, because something in its content breaks the parse or the decision, stays an error until a code change ships, so the watch stays paused until it gives up as not watched, and the row also pauses watches for applications handed over before it. The retries end after 14 days, a day after the give-up, so they never release a watch.
- **Suggested fix:** Decide a Greenhouse email whose decision keeps failing as "not a confirmation" after a few tries, or let the student dismiss the pause from the card.
- **Regression suite:** tests/ unittest (`test_application_inbox`, `test_apply_watch`)

### The Gmail labelling worker can write label rows for an account that was just deleted
- **Severity:** medium, privacy; left by design for an owner decision (found in review of the PR #60 erase fix, which closed the missing-tables gap but not this race)
- **Where:** `opportunity_app/outreach/labels.py:311` `_add_thread()` (called at `:308`, `:892` and `:1052`) and the `outreach_label_searches` insert at `:894`; the step is `opportunity_app/automation/inbox_watcher.py:179`; `opportunity_app/accounts/operations.py:302-307` `delete_account()` and `ACCOUNT_EXPLICIT_DELETES` at `:240`; `migrations/0044_outreach_sent_labels.sql` (both tables)
- **What happens:** The two label tables have no foreign key to `users`, so deleting the user row does not cascade, and `delete_account` erases them with its own `DELETE` inside the same transaction. A labelling pass that started before the delete (it holds the Gmail client and waits on Google between its search and its write) then commits its `INSERT` afterwards, because nothing checks that the user still exists. The deleted account's thread ids come back, and so do the Sent-mail search queries, which name the contacts the student wrote to. The next export or deletion for that id finds nothing, so nothing erases them again.
- **Suggested fix:** Give both tables `user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE` through a forward migration that rebuilds them (SQLite needs a table rebuild; keep the rows), so a late insert fails with an integrity error that the labeller treats as "account gone", then drop `ACCOUNT_EXPLICIT_DELETES` and its guard. A smaller change is to write each row with `INSERT ... SELECT ... WHERE EXISTS (SELECT 1 FROM users WHERE id=?)`, which makes the check atomic with the write.
- **Regression suite:** tests/ unittest (`test_outreach_labels` with `test_account_coverage`: a `client_factory` whose Gmail call deletes the account before the pass writes; no label row remains for that user)

## Outreach drafting, research, forms and CLI

### A required box about the company (its address, website, size) is typed with the student's school
- **Severity:** medium, wrong value sent (found while adding the mailing address to contact forms)
- **Where:** `opportunity_app/outreach/forms.py`: the `company` entry of `_ROLE_PATTERNS` (it matches before `link`), and the `company`/`phone`/`link`/`job_title` branch of `plan_fill`
- **What happens:** the `company` role matches any text box whose label, name or id says company, organization, business, employer, school, university or institution, and a required one is typed with `identity["school"]`. A required "Company website", "Company address" or "Company size" box therefore gets the school's name, which answers none of those questions, and the form goes out with it when the page accepts any text. A `type="url"` box is read as a link first, so only text boxes are affected. (A box whose label is only address words, such as "Address" or "City", and whose name or id says company, school, business or the like is no longer affected: `_role` makes it unanswerable. A label that itself says company still is.)
- **Suggested fix:** Give the `company` role only to a label that asks for the name ("Company", "Company name", "Organization", "School"). A label that also says address, website, phone, email, size or industry is unanswerable, so the form waits for the student.
- **Regression suite:** tests/ unittest (`test_outreach_forms`: required "Company website", "Company address" and "Company size" text boxes are named as unanswerable and nothing is typed in them)

### The app's own contact-form send does not block WebSockets or workers, so page scripts get past the request guard
- **Severity:** medium, privacy (notes 15, 97; narrowed 2026-10-09 by PR #97)
- **Where:** `opportunity_app/outreach/forms.py` `FormSubmitter._start()` (compare `opportunity_app/outreach/render.py`)
- **What happens:** `context.route()` does not intercept WebSockets, and no route sees a worker's requests. When the app itself sends a form ("Send through contact form", and the automatic path), a contact page can open ws:// connections, from the page or a worker, to loopback or private hosts such as the local app, and stream what was filled out. Finish in browser and a rehearsal refuse them (`SOCKET_GUARD`); the app's own send was left as it was, since refusing them there turned a page whose form sends over a socket from sent into a false "submitted" or a silent failure. Also as it was: a form that goes only over a socket and shows no thank-you the app recognises is recorded as failed ("The form did not send"), though it may have gone.
- **Suggested fix:** in the app's own send, refuse a WebSocket only to a loopback or private address (a `route_web_socket` handler that connects to the server otherwise), and say so in the outcome when the form needed one.
- **Regression suite:** tests/ unittest under Chromium (`test_outreach_forms`: a contact page in the app's own send opens a WebSocket to a loopback listener, which hears nothing)

### The app's own contact-form send records a thank-you that comes with nothing sent as submitted
- **Severity:** medium, wrong visible state (named in review of PR #97, 2026-10-09; the behaviour predates it)
- **Where:** `opportunity_app/outreach/forms.py` `FormSubmitter._await_outcome` (the "Send through contact form" and automatic paths, not Finish in browser)
- **What happens:** after the app presses send, fresh thank-you wording on the page is taken as the form having arrived, whether or not anything left the page. A page that thanks optimistically (it shows its message before, or without, a send that then fails) is recorded as submitted, and the company is marked sent though nothing reached them. Finish in browser does not judge this way: it asks the student.
- **Suggested fix:** take "submitted" only when a request that could carry the form left after the press, as the same check counts one; otherwise record unconfirmed and let the student look.
- **Regression suite:** tests/ unittest under Chromium (`test_outreach_forms.BrowserSubmitTests`: a page that thanks the student and sends nothing is not submitted)

### The renderer hangs on a page that opens a WebSocket
- **Severity:** medium, a crash (a hang) (found 2026-10-09, while closing the form submitter's WebSockets the same way)
- **Where:** `opportunity_app/outreach/render.py` (`route_web_socket("**/*", lambda socket: socket.close())` where the renderer starts its browser)
- **What happens:** the handler calls `socket.close()` from inside Playwright's own event dispatch, which deadlocks the sync API: the first page the renderer loads that opens a WebSocket stops the render (and whatever called it, such as a contact search) for good. The form submitter had the same handler and hung its tests for nine hours. It now makes a page's WebSocket fail as a blocked connection does (`SOCKET_GUARD`), and its backstop handler only notes a socket and leaves it unconnected, closing it later from the main greenlet.
- **Suggested fix:** as in `FormSubmitter._start`: `SOCKET_GUARD` as an init script, and a `route_web_socket` handler that makes no Playwright call.
- **Regression suite:** tests/ unittest under Chromium (render a page that opens a WebSocket to a loopback listener: it returns, and the listener hears nothing)

### Finish in browser for contact forms: sends it holds back, and what its gate cannot see
- **Severity:** low (found while making the window the student's to finish, and in six reviews of it, 2026-10-08/09). Gaps (1), (2), (4), (5) and (7) hold the form back, so nothing is sent and the record says so; (3) concerns what a page could send that is not the form; (6) could record a send as unsent, but needs a form sent only as an encoded image address, which no site the app has met does.
- **Where:** `opportunity_app/outreach/forms.py`: `FormSubmitter._route`, `_could_carry` and `_needles` (the gate), `PRESS_LISTENER`, `CLOSE_GUARD`, `FormSubmitter._hand_to_student`
- **What happens:** Finish in browser fails closed. From page load, nothing carrying the student's details leaves the window: their email, name, phone (as typed or as digits), school, link, street address, the subject, and the message's opening words, found as typed or once form-decoded, whatever the request and wherever it goes. From the app's first fill, nothing but reads leaves either (CAPTCHA calls aside), nor a script's read of the form's own site. What a closing page would send is kept from it, and speculation rules, prerender and prefetch links, browser sign-in (FedCM) and worklet modules are refused (`CLOSE_GUARD`), as is a page's own `Speculation-Rules` or prefetch `Link` header, taken off as the page loads (`FormSubmitter._guard`). A page's WebSocket fails as a blocked connection does, and workers are removed outside a CAPTCHA's own frames (`SOCKET_GUARD`). Both apply to Finish in browser and a rehearsal; the app's own send leaves the page as it is (the WebSocket entry above). So with no press seen, "Nothing was sent" is true. After the press, what may carry the form goes and is recorded, and the card asks the student whether their page said it was sent. It says "nothing was sent" instead only when nothing left and the page's way of sending was a WebSocket the app refused: one to the form's own site, or one the page sent on after the press (a chat widget's socket elsewhere decides nothing). Seven gaps remain:
  (1) A send control the listener does not recognise (a link, a plain `<div>` with a click handler, a button outside the form) is no press. Nor is a `<button>` with no type once a redraw or reload has dropped the app's mark from it (unmarked, only an explicit `type="submit"` counts), and the note then speaks of what was held back "before your press". Neither is a press that another site's frame stops with a listener it registered before the app's, since that frame is watched only from when it is found. Either way the form is held back: nothing is sent, the record says so, and the window's note names what was held back. The student then has to send it from the page outside the app.
  (2) A form whose own checks run before the press (an email lookup, a field check) has them held back, and may then never let the student send. The note and the result name the host.
  (3) A read (GET) to another site carrying only what the student typed into a box the app left for them, or carrying a detail in an encoding the app does not check (base64, double encoding), passes the gate before the press. It cannot be the form's own send without the app's details too, but it can carry an answer of theirs off the page unrecorded.
  (4) Under one process per site (as the headed window runs), the app's own tick of a box in another site's frame sometimes does not take. The note names it and the student ticks it.
  (5) A frame that shares the page's process is handed over on the page's own "ready", not its own, so a frame a script wrote into the page (about:blank) may lack the listener. Its form then cannot be sent.
  (6) After the press, anything but a read of an image, style, font or media file counts as the form leaving (and the card asks), and so does the form going from the page. A form sent as an image's address carrying the student's values in an encoding the app does not read, on a page that then stays as it was, would end as "Nothing was sent".
  (7) A CAPTCHA that computes in the page's own workers rather than in its own frame (Friendly Captcha, for one) cannot finish in the window, since workers are removed there outside a CAPTCHA's frames: the form cannot be sent from Finish in browser.
- **Suggested fix:** (7) Allow workers whose script comes from a known CAPTCHA host, with the WebSocket replacement put at the start of their script. (1) Recognise send links and click handlers in the listener from what EXTRACT_SCRIPT reads, count an unmarked typeless button that is its form's default button, and attach to another site's frame before its scripts run (auto-attach with the frame paused). (2) Allow a check that carries only the email to the form's own site, once there is evidence it is needed. (3) Hold every read to another site that a script makes after the first fill, once it is clear what that breaks. (4) Find why the tick fails in an out-of-process frame. (5) Require each frame's own "ready".
- **Regression suite:** tests/ unittest under Chromium (`test_outreach_forms.FinishInBrowserTests`, `FinishInBrowserGateTests`, `SpeculationHeaderTests`)

### One address block can read as two, so a contact form that wants the student's address waits for them
- **Severity:** low, the send stops when it could have gone (found in review of the mailing-address change)
- **Where:** `opportunity_app/outreach/forms.py` `plan_fill()` (the pre-pass that sets `two_blocks` when an address part appears in more than one box)
- **What happens:** a box hinted `autocomplete="street-address"` counts as both street lines, so a separate "Apt / Suite" box in the same block looks like a second line 2; a "State" list with a "State/Province" text fallback looks like two state boxes. Either way every required address box is named unanswerable and nothing is filled. Nothing wrong is sent.
- **Suggested fix:** do not count a `street_address` box and a line-2 box as a repeat; count a state list and a state text box as one only when both are visible.
- **Regression suite:** tests/ unittest (`test_outreach_forms` `AddressPlanTests`)

### outreach_cli recontact crashes with KeyError('') when PIPELINE_OUTREACH_DISCOVERY_PROVIDER is blank (the .env.example default)
- **Severity:** medium, crash (notes 12, 18, 98, 99)
- **Where:** `opportunity_app/outreach_cli.py:112-116` `build_parser()` recontact `--provider` default; `:237` `_recontact()` `RUNNERS[args.provider]`
- **What happens:** `.env.example:53` ships the variable blank, and argparse does not check a default against `choices`, so `recontact` without `--no-email-search` raises `KeyError: ''`. A related drift: `opportunity_app/outreach/settings.py:123` strips the value, but `discovery_provider()` in `opportunity_app/outreach/config.py` (`:42-44`) does not, so a whitespace-only value shows as claude-code in Settings while the CLI gets `'  '`.
- **Suggested fix:** Use `default=discovery_provider()`, and have that function return `.strip() or 'claude-code'`.
- **Regression suite:** tests/ unittest (`test_outreach_discovery`, which drives `outreach_cli.main` with the variable set to `''` and to whitespace)

### _profile_regions raises TypeError on a non-list "regions" in profile.json, so every outreach read returns 500
- **Severity:** medium, crash (notes 14, 101)
- **Where:** `opportunity_app/outreach/location.py:205-208` `_profile_regions`
- **What happens:** `"regions": 5` makes the list comprehension raise, and the whole Outreach tab returns 500s. The non-owner branch at `:222-223` already guards against this.
- **Suggested fix:** Return the list only when `isinstance(regions, list)`, keeping only its dict entries. Keep the function name, because tests patch it.
- **Regression suite:** tests/ unittest (patch `owner_profile` to `{'regions': 5}` and GET `/api/v1/outreach`)

### SafeFetcher's private-address check can be bypassed by DNS rebinding
- **Severity:** medium, privacy (notes 19; reasoned from the code, not reproduced)
- **Where:** `opportunity_app/integrations/web_fetch.py:183-195` `SafeFetcher.fetch()`; `:113` `_DeadlineBackend.connect_tcp()`
- **What happens:** The check resolves the name once and httpcore resolves it again when it connects. A short-TTL name can return a public address to the check and a loopback or LAN address to the connect. The URLs come from model output and cited pages, and the response body can reach research text.
- **Suggested fix:** In `connect_tcp` on the direct pool, check the connected peer address with `is_global` and close the socket if it fails. Alternatively, connect to the pre-resolved IP with SNI and Host set.
- **Regression suite:** tests/ unittest (fetcher tests with a stub resolver and a loopback server)

### recover_contact records a recovery even when automation was paused during a search that found nothing
- **Severity:** low (notes 27)
- **Where:** `opportunity_app/outreach/automation.py:122-180` `recover_contact()` (final `log_event` `:175`); `recovery_due` `:109`
- **What happens:** Only the `elif choice:` branch checks for a pause. On the no-website, no-address and new-contact branches the bounce is logged as tried and never searched again after the student resumes, which contradicts the docstring.
- **Suggested fix:** When `automatic` is set, run `_unless_stopped(...)()` before the final `log_event` and return `{'paused': True}`.
- **Regression suite:** tests/ unittest (`test_outreach_automation`: pause during an empty search; `recovery_due` still lists the target)

### queue_call_prep has no atomic claim, so a click and an automatic start can create two call-prep jobs
- **Severity:** low (notes 20)
- **Where:** `opportunity_app/outreach/call_prep.py:981-1017` `queue_call_prep()` (also via `auto_queue_call_prep` `:1020`)
- **What happens:** The read-then-unconditional-UPDATE sequence lets both callers enqueue. One job id overwrites the other, and the orphaned job still runs a second, untracked model and research run.
- **Suggested fix:** Use the conditional-claim pattern from `queue_research` (`opportunity_app/outreach/research.py:441-457`) and cancel the job that loses.
- **Regression suite:** tests/ unittest (`test_outreach_call_prep`: two interleaved calls leave one active job)

### The web-capable model calls can fetch any address, so a hostile page can carry the student's profile out in a URL
- **Severity:** medium, privacy (found 2026-10-04, prompt-injection audit)
- **Where:** `opportunity_app/outreach/agents.py:29` `claude_runner` (`--tools WebSearch,WebFetch`) and `:44` `codex_runner`; the prompts in `outreach/discovery.py` (`PROMPT`, `build_prompt`), `research.py` and `email_search.py`
- **What happens:** The deep search prompt carries the student's school, degree, skills, projects, experience, regions and home location, and the agent may fetch any URL. A page it reads can tell it to fetch an attacker's address with those facts in the query string. Every model call now says that text from pages is evidence and not instructions (`agent_providers.UNTRUSTED_TEXT_NOTICE`), which lowers the odds and stops nothing: no code limits where the agent fetches. `SafeFetcher` guards only the fetches Python makes itself. A search that went wrong this way would also write the page's `summary`, `fit_rationale` and `activity_signal` as written, and a later draft reads them as unverified research.
- **Suggested fix:** Run the web agent with a fetch allowlist if the CLI offers one, or split the work: one call with no profile reads pages and returns what it found, and a second call with the profile, and no web tools, judges fit. Failing both, send only the facts the search needs (field, regions) and keep projects, experience and home location out of the web call.
- **Regression suite:** a test that the discovery prompt holds none of the student's projects, experience or home location

## Agents and notifications

### Codex web research, once the student opts in, still reaches apply_patch through code mode
- **Severity:** medium, privacy (only with the opt-in: it is reachable only when `PIPELINE_OUTREACH_RESEARCH_ALLOW_CODEX` is set; found fixing the Codex sandbox entry)
- **Where:** `opportunity_app/outreach/agents.py` `codex_runner`; `opportunity_app/integrations/agent_providers.py` `codex_command(web_search=True)`
- **What happens:** Codex's web tool is carried by code mode (`--disable code_mode_host` removes it), and code mode also exposes `apply_patch`. With `PIPELINE_OUTREACH_RESEARCH_ALLOW_CODEX=1` the research call keeps code mode on, so a page the agent reads could steer it into patch attempts. The read-only sandbox blocks the write, but the failure message tells the model whether the patch's context lines matched a local file, a one-bit-per-try test of file contents. Shell, MCP servers, plugins and file writes stay off, and without the opt-in the call is refused. Every Codex call that carries no web search runs with code mode off and no environment (`CODEX_EXEC_SERVER_URL=none` on the process, set by `run_headless`), which is what keeps `apply_patch` away from models such as gpt-5.5 whose catalog entry lists it as a direct tool whatever `code_mode_host` says (Codex 0.157.0 and 0.159.2: asked to list its tools, the model named only `request_user_input` and `multi_tool_use.parallel`). `multi_agent` and `multi_agent_v2` are off and `agents.enabled=false` is set in every call, so there is no `spawn_agent` and no sub-agent to switch models (`--disable multi_agent` alone leaves `spawn_agent` listed for models whose catalog entry carries `multi_agent_version` v1 or v2, such as gpt-6.1-sol; asked to list its tools with both settings, gpt-6.1-sol on 0.159.2 and gpt-6-sol on 0.157.0 named no `spawn_agent`; `agents.enabled=false` alone does not close it either, because Codex's `multi_agent_version_override` returns v2 when the `multi_agent_v2` feature is on before it checks `agents.enabled`, so `--disable multi_agent_v2` is passed too and accepted under `--strict-config` in both versions). Not tried: whether the research call could keep its web tool with the empty environment too; if it can, the opt-in path can go.
- **Suggested fix:** Drop the opt-in path when a Codex release lets web search run without code mode (`standalone_web_search` is still under development in 0.159.2), or when the research call is shown to keep its web tool with no environment (untested); until then the opt-in is the owner's acceptance.
- **Regression suite:** tests/ unittest (`test_codex_isolation`: the web command is the only one with web search and code mode on, and it is refused without the opt-in)

### Desktop pop-ups skip waiting notices after a same-value re-save of the switch
- **Severity:** low (notes 76)
- **Where:** `opportunity_app/automation/desktop_notify.py:154` `_switched_on_at()`, used by `deliver_desktop_notices()` `:187`
- **What happens:** `_write_modes` rewrites unchanged settings, so re-saving "on" moves `updated_at`. Older unread notices in the two-day window are then never shown, and they are not marked shown either.
- **Suggested fix:** Use `automation.on_since`, falling back to `updated_at`.
- **Regression suite:** tests/ unittest (turn the switch on, create a notice, re-save "on", and the notice is still delivered)

## Web API, auth and storage

### A write route hit with no database creates an empty platform.db, returns 500 and hides the setup guidance
- **Severity:** medium (notes 2, 109)
- **Where:** `opportunity_app/web/dependencies.py:151` `writable_connection()` (via `_open_tracked` `:122-127`), also `extension_connection` `:190` and `employer_connection`/`admin_connection` `:225/:240`; `opportunity_app/core/database.py:204-220` `connect_product()`; `opportunity_app/web/app.py:62-68` lifespan
- **What happens:** Before `setup init`, the first writable request makes `sqlite3.connect` create an empty file. The route returns 500 "no such table" instead of 503, health flips from "missing" to "invalid", and the next start migrates the empty file and reports ready. That removes the "Run setup init" warning.
- **Suggested fix:** When the database is SQLite and `database_present()` is false, raise 503 DATABASE_UNAVAILABLE before opening it, as `require_extension_auth` does. Only `setup init` and migrate should create the file.
- **Regression suite:** tests/ unittest (create_app with a missing `db_path`, a write route returns 503, and no file is created)

### The employer and admin connection dependencies leak the connection and return an unmapped 500 when ensure_actor fails
- **Severity:** medium (notes 3, 110)
- **Where:** `opportunity_app/web/dependencies.py:225-251` `employer_connection()` (`ensure_actor` `:233`), `admin_connection()` (`:246`)
- **What happens:** `ensure_actor` runs before the try/finally. A "database is locked" error or a missing users table leaves the connection open and listed in `open_connections` until shutdown, and the client gets a 500 instead of 503. The in-code comment admits the gap.
- **Suggested fix:** Open the connection through `_open_tracked`, move `ensure_actor` inside the try, and map OperationalError to 503.
- **Regression suite:** tests/ unittest (patch `ensure_actor` to raise; expect 503 and an empty `open_connections`)

### Recovery and phone-verification codes count wrong attempts but never enforce a limit
- **Severity:** medium, security (notes 30)
- **Where:** `opportunity_app/accounts/auth.py:205-218` `complete_recovery`; `opportunity_app/mail/connections.py:465-476` `confirm_phone`
- **What happens:** `attempts` is incremented but never read. That allows about 3,600 guesses per 15-minute challenge under the 240/min IP limit, and new challenges can be requested again and again. The loopback-only binding limits the exposure.
- **Suggested fix:** Expire the challenge after about 5 wrong codes, and consider capping pending challenges per user. Do the same in `confirm_phone`.
- **Regression suite:** tests/ unittest (`test_auth`: the correct code is refused after five wrong ones)

### Account export replaces profile_facts.field_path and dossier_items.field_path with "[private-file-reference-redacted]"
- **Severity:** medium, data loss in export (notes 187)
- **Where:** `opportunity_app/accounts/operations.py:260-263` `export_account` (redaction by the `_path` suffix)
- **What happens:** `field_path` holds a profile field name such as `experience[0].title`, not a file path. Every exported fact and dossier item loses its field, so those tables cannot be read or restored from the export.
- **Suggested fix:** Redact an explicit list of file-reference columns instead of matching the suffix.
- **Regression suite:** tests/ unittest (an export test shows `field_path` is kept)

### require_owner tells students "Only the owner can refresh the pipeline" on unrelated routes
- **Severity:** low (notes 4)
- **Where:** `opportunity_app/web/dependencies.py:116-119` `require_owner()`; used by `web/routers/system.py:50, 55, 69, 77, 87, 96` and `web/routers/outreach_settings.py:59, 71`
- **What happens:** System status, schedules, sources and outreach settings routes all return 403 with the refresh message, which gives the wrong reason for most of them.
- **Suggested fix:** Use a generic message, or let each route pass its own.
- **Regression suite:** tests/ unittest (check the 403 detail on a route that is not refresh)

## Scoring, scheduling and configuration

### The repost FLAG disappears the day after the daily purge removes the retired twin
- **Severity:** high, source integrity and freshness (notes 0; the profile-save half was fixed on 2026-10-02)
- **Where:** `pipeline_core/scoring.py:400` `score_all()` and `pipeline_core/retention.py:67` `purge_expired()`, with the carry-forward in `opportunity_app/student/profile.py` `_synced_repost_flags()`
- **What happens:** a profile save now keeps the repost FLAG by carrying forward what the last refresh synced. The daily task, however, runs `purge-expired` right after `pipeline.py run`, which hard-deletes the retired twin from `jobs`. On the next day's run `score_all` finds no twin, rewrites the explanation without the FLAG, and the sync copies that over the local user's score row the carry-forward reads. So a relisted role stops warning that it was relisted about a day after the twin is purged.
- **Suggested fix:** keep a tombstone of retired (company, role key, url, first_seen_at) for the repost window that `repost_flags` also reads, or have `score_all` carry an existing FLAG forward the way `student/profile.py` does.
- **Regression suite:** tests/test_profile_save_repost_flag.py plus a pipeline test that runs two daily cycles across a purge

### daily.py skips the whole run, local steps included, when DNS for boards-api.greenhouse.io fails
- **Severity:** medium (notes 40)
- **Where:** `opportunity_app/daily.py:98-107` `network_available`; `:161-164` `run_daily`
- **What happens:** Once DNS for that one host keeps failing, `run_daily` returns 0 with nothing done. That skips purge-expired, platform-sync, platform-purge and outreach-remind. On a network that blocks the host, macOS and Linux never run the daily job, and reminders never fire. The Windows script proceeds after its deadline.
- **Suggested fix:** After the deadline, proceed when a non-loopback route exists, as the Windows script does. Alternatively, apply the check only to the network steps.
- **Regression suite:** tests/ unittest (`test_daily`: DNS fails, a route exists, and the run proceeds)

### The daily-run mutex name differs between run-daily.ps1 and Python when the path spelling differs, so runs can overlap
- **Severity:** low (notes 39, 43)
- **Where:** `scripts/run-daily.ps1:90` `$mutexName` (from `$PSScriptRoot` as invoked); `opportunity_app/core/daily_lock.py:42` `DailyRunMutex.acquire()` (from the resolved `ROOT`); docstring `opportunity_app/daily.py:5-6`, comment `opportunity_app/core/daily_lock.py:41`
- **What happens:** Win32 object names are case-sensitive. A different case, or a junction or subst path, gives two different mutexes, so a manual refresh and the scheduled run can both touch pipeline.db and platform.db ("database is locked", interleaved sync and purge). The docstring's claim that the two "can never run over each other" is then false.
- **Suggested fix:** Build both names from one canonical, lowercased spelling, or have the PowerShell script take the lock through `DailyRunMutex`. Then correct the docstring and the comment.
- **Regression suite:** tests/ unittest (the PowerShell and Python name formulas match for a mixed-case path)

### .env readers disagree on repeated keys: the app uses the first line, setup and pipeline_mailbox use the last
- **Severity:** low (notes 83)
- **Where:** `pipeline_core/config.py:15` `load_env_file()` (first wins) vs `opportunity_app/setup.py:158` `read_env()` (last wins), used by `set_env_values` `:163`, init `:219`, status `:438`, and `opportunity_app/pipeline_mailbox.py:415`
- **What happens:** Take a blank `GMAIL_CLIENT_ID=` from the template with a filled copy appended below. The app sees no credential, while `setup status` reports it configured and `setup init` will not fill it. Duplicate `DATABASE_URL` or Gmail keys point `pipeline_mailbox.py` at a different database or OAuth client than the app.
- **Suggested fix:** Use first-wins everywhere, and have `set_env_values` update every duplicate line. Alternatively, warn on repeated keys.
- **Regression suite:** tests/ unittest (a .env with a duplicated key; every reader agrees)

### load_resume and load_env_file bind their default paths at import time, against the paths module's read-at-call-time contract
- **Severity:** low, latent (notes 124)
- **Where:** `pipeline_core/artifacts.py:29` `load_resume(path=paths.RESUME_PATH)`; `pipeline_core/config.py:15` `load_env_file(path=paths.ENV_PATH)`; called without a path at `pipeline_core/artifacts.py:431`, `pipeline_core/cli.py:146`, `outreach_cli.py:131`, `web/context.py:260`
- **What happens:** A test that patches `paths.RESUME_PATH` or `ENV_PATH` still reads the real `config/resume.json` or `.env`, and `realdata_guard` covers only `data/`. No current test patches them.
- **Suggested fix:** Use a `None` default and resolve the path inside the function, as `load_profile()` does.
- **Regression suite:** tests/ unittest (patch `RESUME_PATH` to a temp file and `load_resume()` reads it)

### setup.validate_profile rejects regions the web profile form writes to config/profile.json
- **Severity:** low (notes 31)
- **Where:** `opportunity_app/setup.py:285` `REGION_FIELDS`, `:393-398` `validate_profile()`; `opportunity_app/static/app-profile.js:141-148`; `opportunity_app/student/profile.py:270-293` `_region_errors()`
- **What happens:** The form saves new regions with `state_markers: []`, and a string graduation year passes the web validator. `setup status` then reports ok=False with three errors. An agent following SETUP.md may "fix" valid student data.
- **Suggested fix:** Reuse `profile.validate_profile_types` for type errors, and downgrade an empty `state_markers` or `places` to a warning.
- **Regression suite:** tests/ unittest (`test_setup`: a profile shaped like the web form's output is ok)

### Confirming "contact" in résumé review replaces the whole contact object, wiping a confirmed mailing address and a hand-typed phone
- **Severity:** low (found while adding the mailing address to contact forms)
- **Where:** `opportunity_app/student/profile.py` `update_profile()` (`merged = {**_stored_profile(...), **updates}` replaces each top-level key); `opportunity_app/student/resumes.py` `confirm_resume()`; `opportunity_app/static/app-profile.js` `createResumeCard()` (the "Confirm selected facts" submit sends the résumé's whole `contact` suggestion)
- **What happens:** the résumé's `contact` suggestion holds only the email, the phone it found (or an empty one) and links. Ticking it in résumé review saves that object in place of the profile's `contact`, so the mailing address the student confirmed on the Profile page, and a phone they typed there, are gone. No wrong value is sent: a contact form that requires an address box then waits for the student, as it does with no address on file.
- **Suggested fix:** Merge `contact` key by key when it comes from a résumé (keep stored keys the résumé has no answer for), or leave the address keys out of what a résumé confirm may replace.
- **Regression suite:** tests/ unittest (`test_bugfix_profile_save` or `test_resumes`: confirm a résumé's contact after saving an address and a phone; both are kept)

### Greenhouse's posted_at is its updated_at, so any edit makes an old posting look fresh
- **Severity:** medium, source integrity and freshness (found 2026-10-04, board adapter audit)
- **Where:** `pipeline_core/sources.py` `greenhouse_jobs` (`"posted_at": detail.get("updated_at") or item.get("updated_at")`); `pipeline_core/scoring.py` `score_job` (the recency block)
- **What happens:** Greenhouse also returns `first_published`, and the two differ whenever a posting is edited. One live posting was first published on 2024-12-20 and last updated on 2026-08-21, so it gets "+10 updated within 7 days" the week after an edit and is never "over 60 days old". The reason says "updated", which is true, but the same column is the posting date the `posted_since` filter and the freshness text show, so a long-open role reads as new. Ashby and Lever give their publication and creation times.
- **Suggested fix:** Store `first_published` as `posted_at` and keep `updated_at` for the "updated" reasons, or add a second column. Decide first which one the "recency" bonus should follow, since using `first_published` lowers the score of every long-open role.
- **Regression suite:** tests/test_pipeline.py (a Greenhouse listing with a `first_published` far older than its `updated_at`)

### Board discovery reads a throttled or blocked board as "no board"
- **Severity:** low (found 2026-10-04, board adapter audit)
- **Where:** `pipeline_core/discovery.py` `_probe_greenhouse` (`:64`), `_probe_ashby` (`:81`), `_probe_lever` (`:97`)
- **What happens:** Each probe calls `request_json(..., retries=0)` and catches `RuntimeError`, so an HTTP 429, a 403 or a body that fails to parse returns None, the same as a board that does not exist. A live board that was probed during a throttle is reported unresolved. Discovery only suggests boards and removes nothing, so the cost is a missed suggestion.
- **Suggested fix:** Return a third answer for a throttle or a parse failure, and have the caller retry once after a pause or report "could not check".
- **Regression suite:** tests/test_boards.py (a 429 from a probe is reported as unchecked, not as no board)

### The sponsorship reader still misreads some wording, both ways
- **Severity:** low (found 2026-10-08, third review of the posting-language fixes; every version of the reader, main included, gets these wrong)
- **Where:** `pipeline_core/scoring.py` `sponsorship_closure()`, `_NO_SPONSORSHIP_RE`, `_also_sponsors_a_visa()` (`_ROLE_KIND_RE`)
- **What happens:** three kinds of wording are read wrongly. (1) Welcoming CPT or OPT wording is read as closed: in "Candidates authorized to work in the US, including F-1 students on CPT who can intern without visa sponsorship, are welcome; we sponsor H-1B after graduation" the role-kind check matches the verb "intern", so the "we sponsor H-1B" clause does not soften it and a student who needs sponsorship loses 35 points. (2) A refusal limited to green cards ("we do not sponsor green cards") is read as closed for an internship, which needs no green card. (3) Some refusals are not read at all, so no flag and no penalty: "Must not require sponsorship now or in the future", "This role is not eligible for visa sponsorship", "We are not sponsoring visas for this role", and "without the need for employer sponsorship" (`employer` is not in `_VISA_KIND`).
- **Suggested fix:** Match only role nouns in `_ROLE_KIND_RE` (interns, internship, co-op, this role or position), not the verb "intern" after "can" or "to". Treat a refusal that names only green cards or permanent residence as a FLAG without the penalty. Add "not require sponsorship", "not eligible for (visa) sponsorship", "not sponsoring" and `employer` to the refusal patterns, each with a test that the welcoming forms ("no sponsorship required to apply") stay open.
- **Regression suite:** tests/test_scoring_posting_language.py (each sentence above, with `requires_sponsorship` true)

### The unpaid reader is a wording heuristic and misreads some sentences
- **Severity:** low (found 2026-10-08, fifth review of the posting-language fixes; it applies only to a student whose profile sets `compensation_preferences.paid_only` true)
- **Where:** `pipeline_core/scoring.py` `_calls_the_role_unpaid()`, `_UNPAID_ROLE_RE`, `_asks_about_the_past()`, `_THIS_ROLE_BEFORE_RE`
- **What happens:** the reader decides from the words around "unpaid <role>" whether the posting calls this role unpaid, and some sentences fall on the wrong side. "Unpaid community service opportunity: interns volunteer one Friday a month." costs 35 although the role is not unpaid. "Experience such as an unpaid internship or volunteer work is a plus." costs 35 for a sentence about the candidate's past (the reader before PR #81 did the same). "Do not miss this unpaid internship opportunity" is not read as unpaid, because "not" within three words reads as a negation. Each rule added for one wording moves the line for others, so more cases of both kinds are likely.
- **Suggested fix:** Give the 35 points only for an unambiguous statement about this role ("this is an unpaid internship", "the internship is unpaid", "this role carries no compensation"), and turn every other "unpaid <role>" match into a FLAG ("mentions an unpaid role—verify pay") with no change to the score, so a misread costs a look rather than a rank.
- **Regression suite:** tests/test_scoring_pay_preferences.py (the three sentences above: the first two flagged without the penalty, the third flagged)

## Packaging and docs

### .dockerignore lets personal config, private terms and extra .env files into the image
- **Severity:** medium, privacy (notes 41)
- **Where:** `.dockerignore:1-10` (with `Dockerfile:8` `COPY . .`; `scripts/check_personal_data.py:56-66` `PERSONAL_PATHS`)
- **What happens:** The image gets `config/*.local.json`, `private/` (blocked and situation terms), `.env.local` and other `.env.*` files, and `.claude/settings.local.json`, along with `.venv*/`, `node_modules/` and `backups/`. Pushing or sharing the image distributes them.
- **Suggested fix:** Make `.dockerignore` match `.gitignore` and `PERSONAL_PATHS`, keeping `!.env.example`. Add a test that compares them.
- **Regression suite:** tests/ unittest (a new structural test of `.dockerignore` against `PERSONAL_PATHS`)

### The documented backup commands write an encrypted full-database copy to a repo-root backups/ folder that git and the hooks accept
- **Severity:** medium, privacy (notes 42)
- **Where:** `docs/guide/web-app.md:150-151`; `docs/RUNBOOK.md:38-40`; `.gitignore`; `scripts/check_personal_data.py:56-68` `PERSONAL_PATHS`/`DOCUMENT_SUFFIXES`
- **What happens:** `ops_cli backup backups/platform.enc` creates a file that neither `.gitignore` nor the hooks cover, so `git add -A` can publish the whole application history. The copy is Fernet-encrypted, but the key is in the same student's environment.
- **Suggested fix:** Point the docs at `data/backups/`. Add `backups/` and `*.enc` to `.gitignore`, `backups/` to `PERSONAL_PATHS`, and `.enc` to `DOCUMENT_SUFFIXES`.
- **Regression suite:** tests/ unittest (`check_personal_data` refuses `backups/x.enc`)

### The Dockerfile binds uvicorn to 0.0.0.0, against the loopback-only hard rule
- **Severity:** low (notes 51, 111)
- **Where:** `Dockerfile:12` CMD
- **What happens:** Compose publishes the port on `127.0.0.1:8765`, which is safe. A plain `docker run -p 8765:8765` publishes it on every host interface, and nothing documents or guards against that.
- **Suggested fix:** Keep 0.0.0.0 inside the container, and document `-p 127.0.0.1:8765:8765` in the Dockerfile and the docs. Optionally test that the compose port is loopback-prefixed, or have the owner record a rule 3 exception.
- **Regression suite:** tests/ unittest (a structural test over `infra/docker-compose.yml` and the Dockerfile)

## Test tooling

### run_api_fuzz never checks that its child server is alive, so it can fuzz whatever already listens on 8799
- **Severity:** low (notes 36)
- **Where:** `scripts/run_api_fuzz.py:88-99` `_wait_for_health()`; `:157-162` `main()`
- **What happens:** When an exploratory sandbox already holds 8799, the child fails to bind and exits, but the health check passes against the other process. Schemathesis then mutates the other process with the owner token, and the results are credited to a fresh sandbox.
- **Suggested fix:** Fail when `server.poll()` is not None, and pick a free port by default, as `tests/ui/conftest.py` does.
- **Regression suite:** tests/ unittest (occupy the port; `run_api_fuzz` exits with an error)

### The UI suite's LiveServer.reset copies the database in place while a late request can still open it
- **Severity:** low (notes 136)
- **Where:** `tests/ui/conftest.py:105-109` `LiveServer.reset()`
- **What happens:** `shutil.copyfile` truncates and rewrites the live file in place, so a request still in flight can open an empty or half-written database. The observed "no such table: users" setup error in `test_responsive` fits this, but there is no traceback to confirm it.
- **Suggested fix:** Copy to a temporary file in the same directory and `os.replace` it over the live file, retrying on Windows PermissionError.
- **Regression suite:** tests/ui (full run with no flake)
