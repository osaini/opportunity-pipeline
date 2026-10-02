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
| Mail, Gmail and inboxes | 0 | 4 | 6 | 10 |
| Outreach drafting, research, forms and CLI | 0 | 4 | 2 | 6 |
| Agents and notifications | 0 | 1 | 1 | 2 |
| Web API, auth and storage | 0 | 4 | 1 | 5 |
| Scoring, scheduling and configuration | 1 | 1 | 4 | 6 |
| Packaging and docs | 0 | 2 | 1 | 3 |
| Test tooling | 0 | 0 | 3 | 3 |
| **Total** | **1** | **22** | **21** | **44** |

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

### The Gmail labelling worker can write label rows for an account that was just deleted
- **Severity:** medium, privacy; left by design for an owner decision (found in review of the PR #60 erase fix, which closed the missing-tables gap but not this race)
- **Where:** `opportunity_app/outreach/labels.py:311` `_add_thread()` (called at `:308`, `:892` and `:1052`) and the `outreach_label_searches` insert at `:894`; the step is `opportunity_app/automation/inbox_watcher.py:179`; `opportunity_app/accounts/operations.py:302-307` `delete_account()` and `ACCOUNT_EXPLICIT_DELETES` at `:240`; `migrations/0044_outreach_sent_labels.sql` (both tables)
- **What happens:** The two label tables have no foreign key to `users`, so deleting the user row does not cascade, and `delete_account` erases them with its own `DELETE` inside the same transaction. A labelling pass that started before the delete (it holds the Gmail client and waits on Google between its search and its write) then commits its `INSERT` afterwards, because nothing checks that the user still exists. The deleted account's thread ids come back, and so do the Sent-mail search queries, which name the contacts the student wrote to. The next export or deletion for that id finds nothing, so nothing erases them again.
- **Suggested fix:** Give both tables `user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE` through a forward migration that rebuilds them (SQLite needs a table rebuild; keep the rows), so a late insert fails with an integrity error that the labeller treats as "account gone", then drop `ACCOUNT_EXPLICIT_DELETES` and its guard. A smaller change is to write each row with `INSERT ... SELECT ... WHERE EXISTS (SELECT 1 FROM users WHERE id=?)`, which makes the check atomic with the write.
- **Regression suite:** tests/ unittest (`test_outreach_labels` with `test_account_coverage`: a `client_factory` whose Gmail call deletes the account before the pass writes; no label row remains for that user)

## Outreach drafting, research, forms and CLI

### FormSubmitter does not block WebSockets, so page scripts get past the request guard
- **Severity:** medium, privacy (notes 15, 97)
- **Where:** `opportunity_app/outreach/forms.py:816-824` `FormSubmitter._start()` (compare `opportunity_app/outreach/render.py:80-84`)
- **What happens:** `context.route()` does not intercept WebSockets, and only the renderer closes them. A contact page loaded for submission or rehearsal can open ws:// connections to loopback or private hosts, such as the local app. It can also stream typed form fields out during a rehearsal that promises nothing leaves the page. The class docstring claims the renderer's guard.
- **Suggested fix:** After `route()`, add `route_web_socket('**/*', lambda s: s.close())`. Better, build both browser contexts with one shared guarded-context helper.
- **Regression suite:** tests/ unittest (`test_outreach_forms`: a fake context asserts that `route_web_socket` is installed)

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

## Agents and notifications

### Codex web research, once the student opts in, still reaches apply_patch through code mode
- **Severity:** medium, privacy (only with the opt-in: it is reachable only when `PIPELINE_OUTREACH_RESEARCH_ALLOW_CODEX` is set; found fixing the Codex sandbox entry)
- **Where:** `opportunity_app/outreach/agents.py` `codex_runner`; `opportunity_app/integrations/agent_providers.py` `codex_command(web_search=True)`
- **What happens:** Codex's web tool is carried by code mode (`--disable code_mode_host` removes it), and code mode also exposes `apply_patch`. With `PIPELINE_OUTREACH_RESEARCH_ALLOW_CODEX=1` the research call keeps code mode on, so a page the agent reads could steer it into patch attempts. The read-only sandbox blocks the write, but the failure message tells the model whether the patch's context lines matched a local file, a one-bit-per-try test of file contents. Shell, MCP servers, plugins and file writes stay off, and without the opt-in the call is refused. Every Codex call that carries no web search runs with code mode off and no environment (`CODEX_EXEC_SERVER_URL=none` on the process, set by `run_headless`), which is what keeps `apply_patch` away from models such as gpt-5.5 whose catalog entry lists it as a direct tool whatever `code_mode_host` says (Codex 0.157.0 and 0.159.2: asked to list its tools, the model named only `request_user_input` and `multi_tool_use.parallel`). `multi_agent` is off and `agents.enabled=false` is set in every call, so there is no `spawn_agent` and no sub-agent to switch models (`--disable multi_agent` alone leaves `spawn_agent` listed for models whose catalog entry carries `multi_agent_version` v1 or v2, such as gpt-6.1-sol; asked to list its tools with both settings, gpt-6.1-sol on 0.159.2 and gpt-6-sol on 0.157.0 named no `spawn_agent`). Not tried: whether the research call could keep its web tool with the empty environment too; if it can, the opt-in path can go.
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

### test_switching_two_kinds_quickly_keeps_both_changes asserts database state after a fixed 1000 ms wait
- **Severity:** low (notes 157)
- **Where:** `tests/ui/test_apply_sensitive.py:247`
- **What happens:** On a loaded machine the second PUT may not have committed when the database is read, so the test fails intermittently. The other flaky tests named in the note did not reproduce.
- **Suggested fix:** Poll for the database condition, or wait for both responses with `page.expect_response`.
- **Regression suite:** tests/ui
