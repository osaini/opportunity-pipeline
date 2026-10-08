# Phase 5 addendum: "Apply for me" on Lever (Finish in browser only)

- **Status:** draft 1, 2026-10-04; L1 and L2 answered 2026-10-07 (LV0 done, section 4). Nothing here is built. It is
  the "Later: Lever" row of the Phase 5 milestone table (`phase5-apply-agent-spec.md` section 14, which said "Separate
  specs"). Building LV1a onward still needs the student's go.
- **Base:** `origin/main` 946c524 plus PR #81 (the `active_at_company` tick and the notice that every model call
  carries). Line numbers below are as of that tree.
- **Relation to Phase 5:** this file changes only what Lever forces to change. Every Phase 5 rule not named here
  still holds for Lever: the student presses Submit (D1 B), the recorded answers D1 to D14 (D5 C (i), D9 B, D13 A,
  D14 A), the caps and company limit (D4, 9.1), the request guard, the sensitive-answer store, masking and
  retention. "Phase 5 6.13" means section 6.13 of `phase5-apply-agent-spec.md`. Where this file and Phase 5
  disagree about Lever, this file wins; about Greenhouse, Phase 5 wins.
- **How to read it:** section 1 is the plain-words version. Section 3 is what a Lever form actually is, read from
  live pages; the whole design hangs on it. Section 4 holds the two choices only the student can make. Sections
  5 to 11 are for the engineer. Section 12 is the milestone plan. Appendix A is the Ashby note.

Research marks, as in Phase 5: **[live]** seen on a live public page; **[doc]** vendor documentation; **[1-src]**
one third-party report; **[unseen]** not observed, and a decision below does not depend on it being true. The live
reading of 2026-10-04 was GET requests only to public Lever pages and scripts (six boards: `palantir`, `leverdemo`,
`spotify`, `rover`, `paytm`, `gohighlevel`). Nothing was submitted, and nothing was typed into a form.

---

## 1. Summary in plain words

Today Apply for me opens a saved Greenhouse role, fills what it can from your confirmed facts, and hands the window
to you to press Submit. This adds Lever, in that same mode and nothing more. No one-click submit, no unattended
mode, no rehearsal that stands in for a submission.

Lever is the smaller change of the two ATSs Phase 5 deferred. Its application is one ordinary form on one page,
posted once, with fixed names for the standard fields and a small JSON description of every company-written
question embedded in the page. The app can read that description with a plain GET, before any browser opens, the way
it reads Greenhouse's listing today. That gives the same "what would you fill, and what do you still need to
answer?" view for a Lever role as for a Greenhouse one.

Three things about Lever are different enough to need your say:

1. **Attaching your résumé sends it to Lever at once.** Lever's page reads the résumé the moment it is attached,
   to pre-fill the form, and that sends the file to Lever before you press Submit. It is also what happens when you
   attach it yourself in the window. Greenhouse's boards the app supports do not do this. Section 4 asks whether the app may attach your résumé on Lever (L1).
2. **Lever's résumé reader fills in fields with guesses** (your name, company, links). If the app attaches the résumé,
   it must then overwrite those guesses with your confirmed facts, and decide what to do with a guess it has no
   fact for (L2).
3. **A hCaptcha runs when Submit is pressed**, and it can show a picture challenge. As with Greenhouse (D14 A), the
   app never touches it. You solve it in the window. Nothing about that changes, but it is the step most likely to
   need you.

What you will see: the same Apply for me section, saying "Apply for me works with Greenhouse and Lever postings",
the same list of what is missing, and the same window with a "left for you" list. The window will say, before it
opens, if it is about to send your résumé to Lever (if you answered yes to L1).

Everything else is the Greenhouse behavior you already have: nothing is sent without your press, a sensitive question
or a consent box is left for you unless you stored an exact answer, the same 10-minute spacing, 5 a day and one
application per company per 30 days (with the same tick to override), the same masked screenshots, and a
confirmation email watch.

---

## 2. Goals and non-goals

### Goals

- A saved Lever posting gets the same read-only check as a Greenhouse one (Phase 5 6.0): what the app would fill,
  what is missing, with an action for each, and no browser.
- Finish in browser works on Lever: the app fills the form deterministically, brings the window to the front with a
  "left for you" list, the student presses Submit, the app records the result.
- Source integrity holds: every value the app puts in the form is an exact, confirmed fact or an exact stored
  answer. A value Lever's own résumé reader guessed is never left in the form unflagged (L2).
- The Greenhouse path does not change. The generalization (section 5) is a refactor with no behavior change, and
  lands in its own PRs (section 12), per AGENTS.md section 8 rule 14.
- A form the app does not recognize fails closed: the window opens, nothing is filled that the app is unsure of,
  and the unrecognized part is on the "left for you" list.

### Non-goals

- **One-click submit and unattended mode on Lever.** They need a rehearsal that proves the plan without sending
  anything. Lever's résumé step sends the file on attach (section 3, item 8), so a rehearsal that attaches it is not
  a dry run, and one that does not is not a rehearsal of the real form. They wait for their own spec and for D1 A.
- **Ashby.** Its forms autosave as they are filled **[1-src]**, which is a different problem (Appendix A).
- **Lever's application API** (`POST /v0/postings/{site}/{id}?key=...`). It needs the employer's API key. **[doc]**
- **Workable, SmartRecruiters, Workday, iCIMS, and company-built forms.**
- **Lever "apply with LinkedIn" and other sign-in or social-referral paths.** The hidden `linkedInData`,
  `socialReferralKey` and `socialSource` fields are the page's own and are never written (5.4 item 7).
- **Solving, clicking or working around a CAPTCHA or a Cloudflare check by any means** (D14 A, PLAN.md:23). The app
  only waits for the student to deal with one.
- **Changing the browser's user agent, timezone or locale.** Lever's page fills a hidden `timezone` field from the
  browser by itself; the app leaves it alone (section 8).
- **The browser extension.** `apps/extension` fills pages in the student's own browser from its generic engine and
  is not part of this work. It may already match Lever's labels; this spec does not promise it.

---

## 3. What a Lever form is **[live]**

Read on 2026-10-04 from six public boards. Each item is something the design relies on.

1. **The posting API holds no form.** `GET api.lever.co/v0/postings/{site}?mode=json` returns the posting
   (`id`, `text`, `categories`, `lists`, `hostedUrl`, `applyUrl`, `createdAt`, ...) and nothing about the
   application. `applyUrl` is `hostedUrl + "/apply"`. The EU host is `api.eu.lever.co` for the list and
   `jobs.eu.lever.co` for the page **[doc]**. A posting that does not exist, or has closed, answers 404 at
   `/apply`.
2. **The page is the schema.** `GET {hostedUrl}/apply` returns server-rendered HTML (about 0.7 to 1.9 MB, most of
   it inline script and style) with one `<form id="application-form" method="POST"
   enctype="multipart/form-data">` and no `action`, so it posts to its own URL.
3. **Standard fields have fixed names.** `resume` (file), `name` (one "Full name" field), `email`, `phone`, `location`
   with a hidden `selectedLocation`, `org` ("Current company"), `urls[<label>]` (the label is the company's: `LinkedIn`,
   `GitHub` or `Github`, `Portfolio`, `Twitter`, `Other`, `Other Website`, `Video Link ` with a trailing space), and
   `comments` ("Additional information"). Some forms add a `pronouns` checkbox group with a `pronouns` text field,
   `opportunityLocationId` (a select of the posting's locations, required on one board read and optional on another),
   `consent[marketing]` (a hidden `0` and a checkbox), and `residentialLocation[<part>]` address fields (the parser
   fills them; none appeared on the six pages). Two of the six pages also carry an unnamed `select.candidate-location`
   that `/js/hideAndShowSurveys.js` uses to show and hide the surveys. `org` was required on four of the six.
4. **Company questions are "cards".** Each card is a list item whose controls are named
   `cards[<card-uuid>][field<N>]`, beside a hidden `cards[<card-uuid>][baseTemplate]` whose value is
   HTML-escaped JSON:
   `{createdAt, text, instructions, type: "posting", id, accountId, fields: [{type, text, description, required, id,
   options?: [{text, optionId}], prompt?}]}`. `N` is the index into `fields`. Field types seen in 41 fields on 21
   templates: `text`, `textarea`, `dropdown` (a `<select>`), `multiple-choice` (radios), `multiple-select`
   (checkboxes, several with the same name), and `file-upload` (an `<input type=file>`, in one case a
   "Cover Letter:" field). `required` is the JSON flag, the `required` attribute and a "✱" in the label.
5. **Surveys are cards of another kind.** `surveysResponses[<uuid>][...]` with `baseTemplate`, `surveyId`,
   `candidateSelectedLocation` and `responses[field<N>]`. The template's `type` is `"survey"`; most of its options
   carry no `optionId` (2 of 84 seen did). The questions seen asked age range, ethnicity, gender and veteran status.
6. **EEO is a fixed block.** `eeo[gender]` (select), `eeo[race]` (select or a radio group), `eeo[veteran]` (select),
   `eeo[disability]` (select), and, when disability is answered, `eeo[disabilitySignature]` and
   `eeo[disabilitySignatureDate]`, which are **text fields for a typed signature and a date**. Choosing **any**
   disability answer, the decline included, makes both required (`application.js`). The block is optional, says so,
   and its sections expand and collapse by script. Decline labels differ by board ("Decline to self-identify",
   "I do not want to answer", "I decline to self-identify for protected veteran status").
7. **The page owns some hidden fields:** `accountId`, `linkedInData`, `origin`, `referer`, `timezone`,
   `socialReferralKey`, `socialSource`, `resumeStorageId`, `h-captcha-response` and `source`. The app writes none of
   them.
8. **Attaching the résumé sends it.** `/js/parseResume.js` posts the file to the same origin at
   `POST /parseResume` as soon as the file input changes, and on success fills `org`, `phone`, `name`, `email`,
   `location`, `selectedLocation`, the `urls[...]` and `residentialLocation[...]` fields and `resumeStorageId`. A field
   counts as the user's only if it was **changed or pasted into and holds a value**, and only the parser's own field
   list is protected at all: `selectedLocation` is not, so a parse always rewrites it. The request carries two parts,
   `resume` (under a file name the page sanitizes) and `accountId`. The limit is 100 MB. The form posts the file
   again at Submit, from the input, under its own name. Whether Lever refuses a Submit with no `resumeStorageId` is
   **[unseen]**.
9. **Location is a typeahead that forgets.** Typing calls `GET /searchLocations?text=...` (debounced 500 ms, up to
   100 characters), and choosing an option writes its JSON into `selectedLocation`. Leaving the field without
   choosing one **empties both fields**. A plain GET of that path from outside a browser session answers **403**, so
   the lookup is reachable only from the page itself; the app does not try to get around that, and what the
   endpoint returns is **[unseen]**.
10. **hCaptcha is wired to Submit.** The visible button is `#btn-submit` (`type=button`). It runs `hcaptcha.execute()`
    (the sitekey is on the page), and when the token arrives the page clicks a hidden `#hcaptchaSubmitBtn`
    (`type=submit`) so the browser's own `required` checks run first. Pressing Enter in a text field is guarded the
    same way. On the page read, the `#btn-submit` click handler is attached only inside hCaptcha's `onLoad`, so if
    `js.hcaptcha.com` cannot load, pressing Submit does nothing at all. A comment in the page's script says an
    earlier version also ran `execute()` when the location field was focused; the script read does not. Whether a
    challenge can appear **during the fill** is **[unseen]**, and the design copes with one anyway (6.6).
11. **A required-checkbox rule that spans the whole page.** `application.js` takes every required checkbox in every
    `.required-field` as one set (`$('.required-field :checkbox[required]')`) and removes `required` from all of
    them once any one is ticked, restoring it when none is. One tick in one group therefore relaxes every other
    required group, so the browser's own validation cannot say that a required group is unmet. The app reads
    `required` from the card JSON and from the page at load, never from the DOM after something has been ticked.
12. **A confirmation page exists, and a plain GET shows it.** `GET {hostedUrl}/thanks` answers 200 with
    "Application submitted!" with no application made. So reaching that path is not proof of a submission; the POST
    in front of it is. What Lever answers to a successful POST (a redirect to `/thanks` is the likely shape) and to a
    refused one is **[unseen]**.
13. **The page is behind Cloudflare** (a `__cf_bm` cookie on the first response) and shows a cookie banner
    (`cookieconsent.min.js`; the banner shows Accept and Deny, and the script also defines a Dismiss option).
14. **Third-party requests the page makes:** `js.hcaptcha.com` and the hCaptcha challenge hosts, Google Tag Manager,
    Bugsnag, and `/js/*` and `/searchLocations` and `/parseResume` on its own origin. Cloudflare's detection script
    (`/cdn-cgi/challenge-platform/scripts/jsd/main.js`) is on **every** one of the six pages, not only on an
    interstitial; it loads as a script and may post beacons under `/cdn-cgi/`.
15. **Lever sends the applicant a confirmation email** from the `hire.lever.co` domain **[1-src]**; the application
    inbox rules already list that domain (`applications/mail_rules.py`).

**Note, 2026-10-08 (read again while building the parser; three boards, `leverdemo`, `rover` and `palantir`, GET requests
only, nothing typed or submitted).** Sections 3 and 5.4 held. The parser follows them, and these are the places where the
pages say a little more than section 3 did:

- **A required résumé is shown by the star, not by an attribute.** On `rover` and `palantir` the label reads
  "Resume/CV ✱", and the hidden file input (`#resume-upload-input`, `tabindex="-1"`) has no `required` attribute on any of
  the three pages. The page's script does the check. The parser therefore reads a fixed field as required when the control
  has the attribute **or** its label shows the star. Cards still use the attribute only (5.4 item 5), where the JSON and the
  attribute agreed on all three pages.
- **`location` can be required** (`palantir`: the attribute and a star), and `phone` is optional there. `org` was optional
  on `rover` and `palantir` and required on `leverdemo`.
- **`comments` and `consent[marketing]` have no `application-label`.** `comments` is a `textarea#additional-information`
  under a `<label for>` that holds an `<h4>` "Additional information". The marketing consent is a `<label>` that wraps a
  `<span><div>` with the statement, the hidden `0` and the checkbox. The parser takes the label from the `label[for]` and
  the wrapping `<label>` in those two cases.
- **An EEO option's label is not always its value.** The veteran select shows "I identify as one or more of the
  classifications of protected veteran listed above" for the value "I am a Protected Veteran", and the disability decline
  has the value "I do not want to answer " (trailing space) under the label "I do not want to answer". The parser lists the
  label, which is what `select_option(label=...)` takes.
- **`eeo[disabilitySignature]` and its date are not `required` as loaded.** Only answering the disability question makes
  them required (3.6), by script.
- **A dropdown's placeholder differs by card** ("Select ...", "Select...", and a sentence beginning "Click Here (If you
  encounter an issue...") but always has the value `""`, as 5.4 item 5 assumes. A 33-box required `multiple-select` and a
  3,301-option dropdown (a 600 KB template) both read with nothing unreadable.
- **`<title>`** was "{Company} - {Role}" on all three, as before. A posting whose role contains " - " is handled by
  the no-split check in 5.4 item 8.
- **`GET {hostedUrl}/thanks`** answered 200 with no application form and no `form#application-form`; a posting that does
  not exist answered 404 with a short page and no form. Neither parses as a form.

---

## 4. Decisions the student must make before build

Only L1 and L2 are new. Section 4.1 lists the Phase 5 answers that carry over unchanged, so nothing is asked twice.

### Answers recorded 2026-10-07

| # | Answer | Differs from recommendation? |
| --- | --- | --- |
| L1 | **A.** The app may attach the résumé on Lever, in Finish in browser only, behind the `apply_lever_resume_upload` setting, which is off by default. While it is off, Lever behaves as C. When it is on, the start confirmation says the file goes to Lever as soon as it is attached, the run records it, and the request guard lets only the one `POST {origin}/parseResume` the app's attach causes. | No |
| L2 | **A.** A field Lever's résumé reader filled where the app has no confirmed fact is cleared and listed as "left for you", and the read-back proves it is empty (6.7, 6.8). | No |

### L1. May the app attach your résumé on Lever, knowing Lever reads it at once?

Attaching the file is what makes Lever pre-fill the form (3.8). It sends the file to Lever, and Lever stores it
under a `resumeStorageId`, before you press Submit. No application exists until the form is posted, but the file
has left your computer. This is the same thing that happens to anyone who clicks Lever's own "Attach resume".

Phase 5 refuses the nearest Greenhouse case: a board that uploads as you attach is refused before filling (6.9),
because that flow has not been seen live and its destination could not be pinned. Lever's flow has been read (3.8):
one request, to one path, on the page's own origin.

- **A. Yes, in Finish in browser only, behind a setting.** The setting is off until you turn it on ("Let the app
  attach my résumé on Lever. Lever reads it as soon as it is attached."). When it is on, the start confirmation says
  so in plain words, the run records it, and the request guard lets the one request the app's attach causes pass,
  `POST {origin}/parseResume` with the planned file's bytes in it (section 7), and no other upload.
- **B. Attach without Lever reading it.** Put the file in the input without the change event, so nothing is sent
  until Submit. Whether Lever then accepts the Submit is **[unseen]**, it departs from how the page behaves for a
  person, and it can only be tried by a real submission. Not recommended unless a live check shows it works.
- **C. No.** The résumé is left for you in the window, as a required "left for you" item.

**Recommendation: A, with the setting off by default** (so until you turn it on, Lever behaves as C). The file is
the one you confirmed, the destination is one fixed same-origin path, you are watching the window, and the app
says it before it happens. A student who answers C loses little: the résumé is one click in the window.

**What your own attach does, whatever you answer.** Under C, and whenever you attach a file yourself in the window,
Lever's page sends the same request. The app lets that one pass during your turn, because it is your own act, on your
own file, in your own window, and it then tells you by name which fields Lever changed so you can check them (6.12
step 7). It is still aborted before your turn begins and after hand-over.

### L2. What happens to the values Lever's résumé reader filled in?

After L1 A, Lever has filled in your name, company, phone, location and links from the file, wherever the app had
not typed first, and some of those are wrong (the parser reads "Current company" from your latest job title, for
example). The app overwrites them with your confirmed facts. For a field where the app has **no** confirmed fact:

- **A. Clear it and list it as "left for you".** Nothing in the form is a guess you did not confirm. The window
  shows the field empty with a reason.
- **B. Leave Lever's value and list it as "check this".** Less work in the window, but a value neither you nor the
  app confirmed is in the form when you press Submit, which is the thing the product promises never to do.

**Recommendation: A.** It is the same rule as Greenhouse (an optional field is filled only from an exact source,
D13 A). A skipped optional field costs you a click, and a wrong "Current company" costs you a bad first
impression. If L1 is C the app attaches nothing, so there is nothing for it to clear before your turn; what Lever
fills when you attach the file yourself is reported to you (6.12) and is yours to check.

### 4.1 What carries over from Phase 5 (nothing is asked again)

| Phase 5 answer | On Lever |
| --- | --- |
| D1 B: the student presses Submit | Same. The app never presses `#btn-submit` or `#hcaptchaSubmitBtn`. |
| D3 B: three clean rehearsals before a Submit button | Not used. Finish in browser needs no gate (D3 text). No rehearsal mode on Lever in this spec. |
| D4: spacing, daily cap, one per company per 30 days | Same counters. The company key is shared across ATSs (a company on both counts once). |
| D5 C (i): sensitive questions | Same classification. EEO only as a stored "Decline to self-identify". `eeo[disabilitySignature]` and its date are never filled (6.6). |
| D8: 90-day screenshots, masking (i) | Same. |
| D9 B: consent boxes ticked only on an exact stored statement | Same. Lever's certification and GDPR boxes are consent boxes. |
| D10: emailed security code | Not applicable. No Lever code step was seen **[unseen]**; if one appears, the run stops and says so (6.9). |
| D11 B: latest approved cover letter | Same, and still M7. A Lever `file-upload` "Cover Letter" card is left empty until then. |
| D12 C: the email watch is optional for Finish in browser | Same. The watch learns Lever's sender (6.15). |
| D13 A: optional fields only from exact sources | Same. |
| D14 A: never click a visible CAPTCHA | Same. Lever's runs `execute()` on its own; the challenge, if shown, is the student's. |

---

## 5. Architecture

### 5.1 What a second ATS touches

A read of `opportunity_app/apply/` on 2026-10-04 found the code is tied to Greenhouse in a small number of named
places and neutral in the rest. Nothing below needs a new table.

| Layer | Today | For Lever |
| --- | --- | --- |
| Claims, runs, limits, watch statistics, retention | Neutral: `ats` is a free `TEXT NOT NULL` column with no CHECK (`migrations/0045_apply_agent.sql:34,82,149`); the live-job lock `(user_id, ats, job_ref)` and the labels key `(user_id, ats, field)` are already ATS-scoped. | Unchanged. `ats='lever'`, `board_token` is the site, `job_ref` is `site/uuid`. |
| Identify, canonical URL, schema fetch | `apply/greenhouse.py` (`identify`, `canonical_url`, `schema_url`), one `SchemaClient` (`apply/schema_client.py:33`). | A parallel Lever module and client, chosen by ATS (5.2). |
| Schema to plan | `policy.parse_schema` reads Greenhouse's JSON blocks; `control_of`, `label_field_of`, `STANDARD_FIELDS` and `_EDUCATION` assume its names (`policy.py:83-250,556`). | A pure `parse_lever_form` yields the same `SchemaField` rows (5.4). `build_plan`, MACs and `plan_hash` are neutral and reused. |
| Request policy and outcome | Module constants: `BOARD_HOSTS` and `SUBMIT_HOST` (`greenhouse.py:26,28`), `FORM_POST_HOSTS`, `TELEMETRY_HOSTS`, the S3 rule and `confirmation_reached` (`checks.py`), `RESOLVABLE_HOSTS` (`agent.py:299`). Only the lookup and CAPTCHA endpoint lists are injectable (`RouteState`, `checks.py:198-199`). | A per-ATS `RoutePolicy` value replaces the constants (5.2). Greenhouse's values are unchanged. |
| The driver | `GreenhouseAdapter` (`agent.py:558`) is a concrete class; `ApplyAgent.__init__` is typed to it (:801), and `DefaultApplyAgentFactory` hardwires it (:2631). There is no adapter Protocol. | An `AtsAdapter` Protocol over the methods the agent already calls, and a `LeverAdapter` (5.3). |
| Classification | Label-text rules are neutral; section-keyed rules (`classify.py:36-39,216-219,533`) expect Greenhouse's `section` names. | The Lever parser assigns sections so the same rules apply (6.6). |
| Mail | `mail/data/application_senders.json` already lists `lever.co` and `hire.lever.co`; confirmation detection is keyword-based; `job_ids` accepts UUIDs (`mail_rules.py:281-291`). `inbox._job_link` keeps only `gh_jid` (:520). | Add the UUID link to `_job_link`; no new sender list. |
| UI | One section, any saved role, asks `/check`; the server answers `NOT_GREENHOUSE` when `identify` returns None. About 25 student-facing sentences say "Greenhouse". | ATS-aware sentences (9). |

### 5.2 The ATS seam: a refactor with no behavior change

Lever cannot be added by copying Greenhouse's branches. The seam lands first, as its own PRs, with Greenhouse
behavior pinned (section 12, LV1). It does these things and nothing else:

1. **`apply/ats.py`**, a registry of one `AtsSpec` per ATS: the key (`"greenhouse"`, `"lever"`), the display name used
   in sentences, `adapter_version`, `supported_modes`, `identify`, `canonical_url`, the schema client and the
   `parse_schema` for it, the confirmation-sender check, and the `RoutePolicy`. Greenhouse registers with exactly
   today's values. The existing `greenhouse.py` names stay where they are; no compatibility re-exports (AGENTS.md
   section 8 rule 5), so every import and every `mock.patch` target is updated, and a moved patch is proved to still bite.
2. **`AtsAdapter`**, a `typing.Protocol` of the methods the agent calls today (`agent.py:566-784`): `form_frame`,
   `detect_page`, `uploads_on_attach`, `security_code_prompt`, `security_code_inputs`, `captcha_widget`, `control`,
   `control_kind`, `field_container`, `choices`, `fill_location`, `read_options`, `loader_paths`, and `is_react_select`
   (which `_choose` calls unconditionally, `agent.py:1172`, so it stays in the Protocol and Lever's returns False);
   the other react-select methods become optional capabilities that Lever does not have. `submit_control` is never
   called (the student presses Submit) and is dropped. `AgentJob` gains an `ats` field, and
   `ApplyAgentFactory.create` takes it.
   **The branches in `ApplyAgent._run` itself** (`agent.py:1536-1591`) become per-ATS too: the navigation-host check
   against `BOARD_HOSTS` (:1539); `bind_endpoints(GREENHOUSE_LOOKUP_ENDPOINTS, board_token(...))` (:1543-1546);
   `posting_ids` for the different-posting guard (:1569-1573), which returns `("", "")` for a non-Greenhouse URL and so
   would silently switch the guard off for Lever; the page kind `application_form_new` (:1577); `loader_paths` with
   `submit_host == SUBMIT_HOST` (:1578-1591), which Lever replaces by "the apply POST goes to the page's own URL"; and
   the `HANDOFF_S3` refusal on `uploads_on_attach` (:1589). `uploads_on_attach` is split in two: it keeps meaning "the
   board uploads to a storage address" (Greenhouse's S3 case; Lever returns False), and a new `reads_on_attach` means
   "the page reads the file as it is attached" (Lever returns True). L1 governs `reads_on_attach`, and it never
   refuses the board. `CLICK_PURPOSES` (`agent.py:165`) gains `option_pick`, and the denylist (:160-162) gains any
   Lever control that proves to need one (none was seen).
3. **`RoutePolicy`**, a frozen value built by each `AtsSpec`: the hosts main-frame navigation may reach, the hosts a
   form may post to, the matcher for "the submit POST", the telemetry hosts, the upload rule, the lookup endpoints, the
   CAPTCHA endpoints, the exact non-GET exceptions allowed before hand-over, and `confirmation_reached`. `route_decision`
   (`checks.py:385`) and `decide_outcome` take it as an argument instead of reading module constants.
   `RESOLVABLE_HOSTS` and the Chromium resolver rule in `LAUNCH_ARGS` (`agent.py:299-311`), which fail DNS for any
   host not listed, become the union over registered ATSs of each policy's hosts.
4. **Three small corrections the seam needs**, each with a test that fails first:
   - `_limit_check` matches `board_token` without the ATS (`runs.py:294-301`), so a Lever site called `acme` would
     trip the company-limit ask for a Greenhouse board called `acme`. It matches on `(ats, board_token)`.
   - `SchemaCache` keys on `(token, job_id)` (`preflight.py:57-66`). It keys on `(ats, token, job_id)`.
   - The runner stamps `ADAPTER_VERSION` of Greenhouse on every run (`runner.py:1152,1282`). It stamps the run's own
     ATS's version.
5. **Parity tests.** Per AGENTS.md section 8 rule 14, each moved decision (`route_decision`, `decide_outcome`,
   `confirmation_reached`, `identify`, `parse_schema`) gets a test that runs the old code from a frozen copy against
   the new on every Greenhouse fixture and vector, old against new, never new against new.
6. New modules are placed in `tests/test_layers.py` at the lowest layer that holds their top-of-file imports, and
   `tests/test_leaf_modules.py` is updated if a leaf is added.

### 5.3 The Lever modules

| Module | Holds | Imports |
| --- | --- | --- |
| `apply/lever.py` | Constants and URLs: `ATS_LEVER`, `ADAPTER_VERSION = "lever-1"`, `LEVER_HOSTS = ("jobs.lever.co", "jobs.eu.lever.co")`, `canonical_url(site, job_id, host)`, `identify`, the sender-domain check, and the `AtsSpec`/`RoutePolicy` for Lever. | Stdlib and its own siblings only, like `greenhouse.py`. |
| `apply/lever_form.py` | The pure parser `parse_lever_form(html) -> LeverForm` (5.4). No I/O, no browser. | `html.parser`, `json`, `re`. |
| `apply/lever_adapter.py` | `LeverAdapter` (the Playwright side, 6.4 to 6.9). Playwright is imported inside the class, as `agent.py` does. | `apply/lever.py`, `apply/checks.py`, `apply/agent_types.py`. |
| `apply/schema_client.py` | A `LeverPageClient` beside `GreenhouseSchemaClient`: `fetch(site, job_id, host) -> str` of HTML. TLS on, 20 s timeout, the pipeline's user agent, response read capped at 4 MB. | Unchanged module, one added class and the factory. |

**`identify`** works on the opportunity's `url`, then its source URLs, then `opportunity_sources` rows whose
`source_key` matches `'lever:%'` (passed as a parameter, as Greenhouse does for PostgreSQL's sake). It accepts
`https://jobs.lever.co/{site}/{uuid}` and `https://jobs.eu.lever.co/{site}/{uuid}`, with an optional `/apply`, `/thanks`,
a query string or a fragment after the uuid, and nothing else. The uuid must match
`^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$`; the site must match `^[A-Za-z0-9_-]{1,100}$`
(the pipeline already stores the site as the source identity and the UUID as `external_id`,
`pipeline_core/sources.py:87-125`). A company-site page that embeds Lever is identified only if its URL names
`jobs.lever.co`; anything else is not a Lever posting for this feature. The host is kept on the result so an EU
posting stays on the EU host.

### 5.4 `parse_lever_form`: the page is the schema

A pure function over the HTML of `GET {canonical_url}`. It is what `parse_schema` is for Greenhouse, and it is the
reason a Lever role can get the read-only check with no browser.

1. Find `form#application-form`. None means the page is not a form (6.0 step 5).
2. Walk its controls in document order with `html.parser`, never a regex over the whole page (the page is up to
   1.9 MB, mostly script). Collect for each control: `name`, tag, `type`, `required`, `disabled`, the label text of
   its `application-label` or option label, and for `select`, `radio` and `checkbox` the option labels and values.
   Membership and `disabled` follow HTML, not just the text between the tags: a control with a `form` attribute is in
   the form only when it names `application-form` (so one outside the element that names it is submitted, and is
   recorded as an unknown control, and one inside that names another form is not read); a control inside a
   `fieldset[disabled]` is disabled, except inside that fieldset's first `legend`; and an `application-form` that sits
   inside another form is not a form the browser builds, so the page has none. More than 100 disabled fieldsets open
   inside one another, or more than 50 `<label>` elements open inside one another, is a page the parser will not read
   (it returns none). The walk takes time in proportion to the page: a stray end tag is turned away at once, not found
   by searching everything still open.
3. **Standard fields** are recognised by exact name: `resume`, `name`, `email`, `phone`, `location`,
   `selectedLocation`, `org`, `urls[...]`, `pronouns`, `comments`, `opportunityLocationId`, `consent[marketing]` and
   `residentialLocation[...]`. A control with **no name** is never filled and never listed: it is not submitted.
4. **Cards and surveys** are recognised by `cards[<uuid>][field<N>]` and `surveysResponses[<uuid>][responses][field<N>]`.
   For each, the `[baseTemplate]` hidden input is HTML-unescaped and parsed as JSON, and `N` indexes its `fields`.
   Limits, all failing to "unreadable" (the field is left for the student, never guessed): the JSON is at most
   2 MB (a university dropdown's template was 622 KB, with 3,302 options), is an object, has `fields` as a list of at
   most 200 objects with at most 20,000 options each, and each field has a string `type` in
   `{text, textarea, dropdown, multiple-choice, multiple-select, file-upload}` and a string `text`. A survey option
   without an `optionId` is normal.
5. **Cross-check against the DOM.** The JSON says what the question is; the DOM says what can be filled. A field is
   readable only if the DOM control type fits the JSON type (`dropdown` to `select`, `multiple-choice` to radios,
   `multiple-select` to checkboxes, `text` to a text input, `textarea` to a textarea, `file-upload` to a file input),
   the JSON `required` equals the DOM `required` **as scanned at load** (the page relaxes it after a tick, 3.11; a card's
   required checkbox group is one question), and every option label in the DOM appears in the JSON options and the
   reverse, **ignoring options with an empty `value`** (the `Select...` placeholder every dropdown starts with, which
   the JSON does not list). An option's label is its `label` attribute when it has one, else its text. For a card or
   survey choice, every option with a non-empty `value` must also submit the answer it shows: its value, with
   whitespace collapsed, equals its label (a radio with no `value` submits "on", so it fails). A page that shows one
   answer and submits another is a page the parser does not understand. The EEO selects and the office select keep a
   label that is not their value (see the 2026-10-08 note). Any mismatch marks the field unreadable.
6. **Unknown controls.** A named control outside the families above is recorded with its name and type. If it is
   required the plan lists it as a problem ("Lever's form has a question the app doesn't read: {label}"); if it is
   optional it is left empty and listed as "left for you".
7. **Page-managed hidden fields** are listed in a constant and never become schema fields: `accountId`, `linkedInData`,
   `origin`, `referer`, `timezone`, `socialReferralKey`, `socialSource`, `resumeStorageId`, `h-captcha-response`,
   `source`, and every `[baseTemplate]`, `surveyId` and `candidateSelectedLocation`. This holds when every control under
   the name is hidden (hCaptcha's answer textarea is the one other exception). A visible control that shares one of these
   names is a question the page asks, so it is recorded as an unknown control (item 6).
8. **Posting facts for the "differs from the saved role" tick** come from `<title>`, which was
   `"{Company} - {Role}"` on all six boards read **[live]**. A role can contain " - ", so the check does not split:
   it requires the page title, after normalization, to begin with the saved company (as whole words) and to carry the
   saved title after it. A company named only in the role half is another employer's posting. Otherwise it asks for the
   student's tick (`posting_confirmed`), as Greenhouse does.
9. The output is `LeverForm(fields: tuple[SchemaField, ...], posting: {company_title, ...}, unreadable, unknown)`.
   `SchemaField.section` is set so the existing classifier applies (6.6): `standard` for the fixed fields, `custom`
   for cards and surveys, and `demographic` for the four `eeo[...]` names, whose `name` is one of Phase 5's EEOC
   names (`gender`, `race`, `veteran_status`, `disability_status`).

A fixture test pins every row of this list against sanitized copies of three real pages (6.x in section 10).

---

## 6. The run, step by step

This section lists only what differs from Phase 5 section 6. A step not listed is the same.

### 6.0 Preflight (read-only, no browser)

1. The `apply_agent` switch is on, and so is `apply_agent_lever` (section 9). If only the first is, the answer for a
   Lever posting is "Apply for me works with Lever postings once you turn it on in Apply agent settings".
2. `identify` (5.3). A posting that is on neither ATS gets "Apply for me works with Greenhouse and Lever postings, for
   now".
3. The application lookup and every stage and duplicate check are Phase 5 6.0 steps 3 and 4, with "Greenhouse" in the
   sentences replaced by the ATS's display name. The new `active_at_company` tick applies unchanged.
4. **Fetch.** `GET {canonical_url}` through `LeverPageClient`. Only a **404** is "The app couldn't find this posting
   on Lever. It may be closed" (3.1). A **403, 429 or 5xx**, a timeout, or a 200 that has no `form#application-form`
   (a Cloudflare interstitial is a 200 page with no form) is "Lever did not answer. Try again later", never "closed".
   The check route caches per `(ats, site, job_id)` for an hour; runs always fetch fresh.
5. `parse_lever_form`, then `posting_difference`, then the draft plan from the schema alone (6.6 without the DOM
   join). Every required field with no source is a problem; the UI offers an action for each (Phase 5 10.3).
6. **Limits.** Handoff caps and the company limit, Phase 5 9.1. The rehearsal gate does not apply.

### 6.1 Claim

`apply_runs.claim` with `mode="handoff"` only. A `submit`, `one_click` or `unattended` claim, a `rehearse` run or a
`lookup` run for `ats='lever'` is refused with code `ats_mode` and "Lever supports Finish in browser only, for now",
by one check against `AtsSpec.supported_modes` in the runner and again inside the claim transaction. Greenhouse's
`supported_modes` lists them all.

### 6.2 Launch and navigate

- Main-frame navigation may go only to the posting's own Lever host (`jobs.lever.co` or `jobs.eu.lever.co`).
  Anything else, including a redirect to the company's site, is aborted and the run ends `needs_you` with "This posting
  sends applicants to {host}".
- The app opens `canonical_url`, which ends in `/apply`.

### 6.3 Detect the page

`LeverAdapter.detect_page` returns one of `application_form` (`form#application-form` present), `confirmation` (path
ends `/thanks` and no form), `closed` (the main-frame response was HTTP 404), `challenge` (a Cloudflare interstitial: the title or
body shows the "checking your browser" page, or no form is present on a 200), `offsite`, `unknown`.

- `challenge`: the window comes to the front with "Lever is checking the browser. Finish that check in the window and
  the app will continue." The app touches nothing on it, waits up to `person_s` (D14 A) heartbeating, and continues if
  the form appears. No form by then is `needs_you`, `after_click=0`.
- `confirmation` before any press is `needs_you`, `after_click=0`: "This page already says the application was
  submitted. The app did nothing." (Reaching `/thanks` proves nothing, 3.12.)

### 6.4 Scan and join

`LeverAdapter` does **not** inject the shared engine in v1. The shared engine is Greenhouse-shaped in three places
(`widgetKind` hardcodes the id `candidate-location`; `controlType` treats `role=combobox` as a react-select;
`requiredMarkers` reads the react-select mirror, `apply-engine.js:142,450-477`), and Lever's controls are plain HTML.
The adapter runs one read-only `frame.evaluate` that returns, for every control in `form#application-form`: name,
tag, type, `required`, visibility, current value, option labels and the label text. Phase 5 6.4's rule still holds,
that the output is advisory and every value the agent relies on is read back through Playwright's own reads.

The join (Phase 5 6.5) is by `name`. Because the schema and the DOM come from the same HTML, a difference means
script changed the page after load. Rules:

- A planned field missing from the DOM, or present with another type or other options, is a problem and is left for
  the student.
- A DOM control the schema did not list is treated as an unknown control (5.4 item 6).
- A planned field that is not visible is a problem.
- **The app never writes a page-managed hidden field** (5.4 item 7), and the final check (6.10) compares them with
  their scanned values.

### 6.5 The résumé: attach first, wait, then overwrite

This is the step that differs most from Greenhouse, because Lever's page pre-fills the form from the file (3.8).

1. **If L1 is off (the default) or C:** the app attaches nothing; the résumé is a required item left for the student,
   and this step is skipped. Nothing is sent by the app. What happens when the student attaches it is 6.12 step 7.
2. **If L1 is A:** the résumé is attached first, before any text field is touched, with the payload form of Phase 5
   6.9 (`set_input_files({"name": original_name, "mimeType": ..., "buffer": ...})`), so the input holds the name the
   student sees and the form posts it under that name at Submit. Extensions and the 100 MB limit are checked first.
   The page then posts the file to `/parseResume` (3.8; under a name it sanitizes), the request the guard lets pass
   during the app's fill (section 7).
3. The agent waits for the parse to end: `.resume-upload-working` is hidden and one of `.resume-upload-success`,
   `.resume-upload-failure` or `.resume-upload-oversize` is visible, within `parse_s` (30 s). A failure is not an
   error: Lever could not read the file, the form is as it was, and the run continues. **A timeout is different**: the
   page's request is still pending, and a late reply would rewrite fields after the app had filled and cleared them
   (`selectedLocation` is always rewritten, 3.8). So a timeout settles `needs_you`, `after_click=0`, **before any field
   is touched**, with the browser closed first so the reply cannot land: "Lever did not finish reading your résumé.
   Nothing was filled. Lever may still have the file." The run records
   `resume_sent_to_lever` (true) and the parse result (`success`, `failure`, `oversize`, `timeout`) in
   `apply_runs.evidence_json`, and the POST itself in `requests_json` as method, host, path and status. Never the parsed
   values, which are Lever's guesses about the student. Closing the window or Stop during the wait settles
   `needs_you` with `after_click=0`, as in Phase 5 6.13.
4. Only after the parse has ended does the app fill (6.6). The page's script overwrites a field only if the user has
   not **changed or pasted** into it (`parseResume.js` marks a field touched on `change` and `paste`, and only if it
   then holds a value), so filling first and attaching second would leave a field the app left empty open to
   Lever's guess. Attach, wait, then fill is the order that cannot be undone by the parser.

### 6.6 Plan and fill

The plan is Phase 5 6.6 with these sources. Every value is an exact confirmed fact or an exact stored answer; the
mapping is by the control's name and the question's label.

| Lever control | Source and rule |
| --- | --- |
| `name` | `first + " " + last` from `name_parts` (the "name for applications"), never the preferred name. Missing parts are a problem. |
| `email`, `phone` | `contact.email`, `contact.phone`. The email is the address the confirmation watch reads (D12). |
| `location` and `selectedLocation` | The stored `apply_ats_labels` row for `(user, 'lever', 'location')`. The adapter types the city part of the label into `location` with real key presses (`press_sequentially`: the page
starts its search on `keydown`, and `fill()` sends none), waits for the page's own `GET /searchLocations`, and clicks the option whose text **equals** the stored label, never the first one. It reads back that `location` holds the label and that `selectedLocation` is non-empty JSON. It never leaves the field while no option is chosen (3.9). No stored label is a problem with the Phase 5 `ats_label` action. |
| `org` ("Current company") | No Phase 5 source exists for it, so it is empty, and it was **required on four of the six boards read**. A required `org` is a problem with the action "type it in the window"; it does not stop the handoff. Under L2 A a value Lever's reader put there is cleared (6.7). |
| `urls[<label>]` | Phase 5's label table (`policy._PROFILE_KEYS`): a label that normalizes to LinkedIn, GitHub or Portfolio and website takes `contact.linkedin`, `contact.github` or `contact.portfolio` when confirmed. Any other label (`Twitter`, `Other`, `Other Website`, `Video Link `) is empty. Nothing is guessed from a résumé. |
| `pronouns` (checkbox and text) | Classified as an identity disclosure, like gender. Never filled. |
| `comments` | No source. Empty. |
| `opportunityLocationId` (a select of the posting's locations) | The app has no confirmed fact about which office the student means. Left for the student; required on one board read, so it can be a problem. |
| `consent[marketing]` | A marketing consent is never ticked (D9 B names no exact statement for it). The hidden `0` stays. |
| `residentialLocation[<part>]` | No Phase 5 source. Never planned; cleared under L2 A if the parser filled it. |
| A control with no name | Never touched and never listed: it is not submitted. |
| A card or survey field, `text` or `textarea` | The answer library, by the same exact question-key match as Greenhouse (D13 A). No saved answer: a required field is a problem, an optional one stays empty. |
| `dropdown`, `multiple-choice` | `match_options(answer, options, several=False)`, the option whose label equals the answer. Never the first option. |
| `multiple-select` | `match_options(..., several=True)`. A required group is satisfied by any one box (3.11), so the plan ticks only the chosen boxes. |
| `file-upload` card | A "cover letter" field is left empty (D11 B, M7). Any other file field is left for the student: the app never attaches a file the student did not choose. |
| `eeo[gender]`, `eeo[race]`, `eeo[veteran]` | D5 C (i): only the option whose label is a decline ("Decline to self-identify", "I decline to self-identify for protected veteran status"; `sensitive.is_decline` reads the label) and only when the student stored that answer. Otherwise left. |
| `eeo[disability]` | **Never filled in v1.** Any answer, the decline included, makes the signature and its date required (3.6), and the app never fills those, so the app would leave the student a required signature it had just caused. The whole question is the student's. |
| `eeo[disabilitySignature]`, `eeo[disabilitySignatureDate]` | **Never filled.** A typed signature and a date are the student's own act. |
| A survey question (age range, ethnicity, and the like) | Classified by its label text; one that names a demographic topic falls to the broad net (`classify.NET_TOPICS`) and is never filled. |
| A consent or certification box | D9 B: ticked only on an exact stored statement; otherwise left, with the statement shown. |

Fill rules:

- Helpers are Phase 5's five: `_type` (`fill`), `_tick` (`set_checked`), `_choose` (`select_option(label=...)`, which
  covers every native `<select>`; Lever has no react-select), `_attach`, and the allowlisted `_click`. The only clicks
  are `option_pick` (a new `CLICK_PURPOSES` entry: a `/searchLocations` result in the location dropdown, inside the
  field's own container) and none else: **there is no `submit` click and no CAPTCHA click.**
- **The cookie banner is never touched.** The app clicks neither Accept, Deny nor Dismiss; that is a consent the
  student gives. If it covers a target, the adapter scrolls the target to the middle of the viewport and retries
  once; a control that stays covered is left for the student.
- The location field is filled last among the text fields, because a field that is left with no chosen option
  empties itself.
- **A challenge during the fill.** If a visible hCaptcha challenge frame appears (3.10), the app stops, brings the
  window forward, touches nothing, heartbeats, and waits up to `person_s` for the frame to go away, then continues.
  No change by then is `needs_you`, `after_click=0`.
- Pacing is Phase 5's (`settle_s`, `between_fields_s`), with no disguise. The location text is typed key by key because
  the page searches on `keydown`; that is function, not disguise, and it uses no randomized delays.

### 6.7 Clear Lever's guesses (L2 A)

When L1 is A and L2 is A, after the parse and the fill the app clears every field the parser can set that the plan
left without a source. The list is the parser's own (`parseResume.js`'s `parsedInputFields` plus `selectedLocation`):
`org`, `phone`, `name`, `email`, `location`, `selectedLocation`, `urls[LinkedIn]`, `urls[Twitter]`, `urls[Quora]`,
`urls[GitHub]`, `urls[Other]`, and every `residentialLocation[<part>]`. The adapter keeps it as a constant, and the
shape check compares it with the script. Each is cleared with `fill("")`. For `location`, the `input` event that
`fill("")` sends shows the dropdown container, and the blur that follows is then answered by the page's own handler,
which empties `selectedLocation` too (3.9). That handler does nothing while the container is hidden, so the order
is fill, then blur, and the read-back proves it. The app then reads back that each field and, for `location`, the
hidden field are empty. A field that cannot be cleared stops the run with `FIELD_TOOK`. Each cleared field is listed in the window as
"left for you", with "Lever filled this from your résumé and the app did not have a confirmed value, so it
cleared it".

### 6.8 Read back

Phase 5 6.8: every filled field is read back through Playwright (`input_value()`, `is_checked()`, the selected
option's label), and a mismatch is cleared and left for the student, except a select that took a wrong option,
which stops the run. Extra Lever reads: `resumeStorageId` is non-empty after a successful parse (it is the page's
own and is only read), and `selectedLocation` parses as JSON with a `name` that equals the location text.

### 6.9 Cover letter and other files

Phase 5 6.9 for the cover letter (M7 wires it). A Lever `file-upload` card is never auto-attached in v1.
**Uploads before hand-over:** the only upload allowed is the résumé to `/parseResume`: the planned file, once, during
the app's fill under L1 A, and any file the student attaches during the student's turn (section 7). Any other upload
attempt, including a second résumé POST during the app's fill, ends the run `needs_you`,
`after_click=0`, "The form tried to send a file the app did not plan, so the app stopped it. Nothing was sent."

**Wording once the résumé has gone.** Phase 5's `needs_you`, `after_click=0` sentences end "Nothing was sent." That is
false once `resume_sent_to_lever` or `student_attached_resume` is true, because Lever has the file. For such a run each
of those sentences ends instead "Your application was not sent. Lever received your résumé." A test pins every
`after_click=0` sentence both ways.

**Security code.** Lever has no emailed-code step **[unseen]**, so `security_code_prompt` always returns False and
the Greenhouse reader is never started. If an unexpected code field appears, the run settles `needs_you` with
"Lever asked for a code the app does not handle".

### 6.10 The independent pre-submit check

The check is Phase 5 6.10 against Lever's controls, and it adds: every planned value read back equals its plan; no
page-managed hidden field has changed from its scan except `resumeStorageId` and `selectedLocation` (which the page
sets); `h-captcha-response` is untouched; no required control is empty unless it is on the "left for you" list; and
the load-time scan still matches the DOM (same names, types and options: nothing re-rendered). `required` is judged
from the card JSON and the load-time scan, never from the DOM after a tick (3.11): a required group is on the "left
for you" list unless a box the plan ticked is checked, whatever the browser's own validity says. A failure is a
problem on the "left for you" list, not a silent fix.

### 6.11 CAPTCHA

Phase 5 6.11 and D14 A: the app never touches hCaptcha. In handoff it is entirely the student's, and it runs only
when the student presses Submit (3.10). A challenge frame after the press waits for the student within the shared
budget.

### 6.12 Hand over and press Submit

Finish in browser is Phase 5 6.13's handoff mode with these Lever facts:

1. The window comes to the front with the "left for you" list, and the claim stays `claimed`. If L1 is A, the list
   begins with "Your résumé was sent to Lever when the app attached it."
2. The student completes what is left and presses Lever's own Submit button (`#btn-submit`). The page runs hCaptcha,
   then clicks its hidden submit so the browser's own required-field checks run, then posts the form (3.10).
3. The route handler recognises the submit POST by **method `POST`, the posting's own Lever host, the exact path
   `/{site}/{job_id}/apply` (the canonical URL's path), and a `multipart/form-data` content type**. It asks the parent
   for the hand-over (Phase 5 5.2 rule 3) **inside the handler, before `route.continue_()`**, and continues only on an
   explicit committed True. Otherwise it aborts and the run ends `needs_you`, `after_click=0`, "The app couldn't
   record this submission, so it stopped it. Nothing was sent. Try again."
4. **One** such POST per attempt. A second press after the first passed is aborted. Every other non-GET to a Lever
   host is aborted before hand-over (7), and after it.
5. `handoff_s`, a closed window and Stop behave as in Phase 5: the browser is closed first, then the claim settles
   `needs_you`, `after_click=0`, "You didn't submit it in the window. Your application was not sent."
6. The agent never presses `#btn-submit` or `#hcaptchaSubmitBtn`. The static scan (section 10) fails if either
   identifier appears in the adapter outside its denylist.
7. **If the student attaches a résumé in the window** (always, under C), the page sends `POST /parseResume` during the
   student's turn. The guard lets it pass if it has the exact shape of section 7. When the page's reply arrives, the
   agent re-reads the parser's fields and tells the student, by name and never by value, which changed ("Lever filled
   Current company and Location from the résumé you attached. Check them."), and records `student_attached_resume`
   with the file's SHA-256 and a count. This is the student's own act and its effects are the student's to check;
   the app does not undo them, because that would mean typing in the student's window while they are working.

### 6.13 Decide the outcome

The Lever rows of Phase 5 6.14's table. The window is `outcome_s` (30 s), polling every 500 ms, and page wording is
never used. The first matching row wins.

| What is seen after hand-over | Outcome |
| --- | --- |
| The POST to the apply URL answered 2xx or 3xx, **and** the main frame's path then equals `/{site}/{job_id}/thanks` on the same host, **and** no `form#application-form` is in the DOM | **submitted** |
| The POST answered 4xx, with the form still present | **failed**, `after_click=1`, "Lever refused the form (HTTP {status})" and the name, never the text, of the first field the page marks invalid |
| No POST passed the route and no main-frame navigation happened | **failed**, `after_click=0`, "Nothing that could carry the application left the window" |
| Anything else: the POST answered 5xx or never answered, a main-frame navigation to `/thanks` without a POST, a 2xx that left the form on the page, "submitted" wording with the form still present | **unconfirmed**: "Your application may have been sent, but Lever did not show its confirmation page" |

A visible hCaptcha challenge **before** a POST is not an outcome: nothing has been sent, hand-over has not happened,
and the student is solving it. It is handled by 6.11's wait. A challenge frame after a POST is `needs_you`,
`after_click=1`, as Phase 5 has it.

What Lever answers to a successful and to a refused POST is **[unseen]** (3.12), so the first and last rows are
written to be safe whichever it is: a redirect to `/thanks` after the POST is `submitted`, a 200 with the form
re-rendered is `unconfirmed`, and the email watch (6.15) settles which. Open question Q1 (section 11) closes this
with the first real handoff.

### 6.14 Record the result

Phase 5 6.15. The stage policy is `ask` under D1 B: on `submitted` the card asks "Lever showed its confirmation
page. Mark as applied?" and the student's button writes the stage. The notice names the ATS.

### 6.15 The confirmation watch

Phase 5 6.16 with Lever's sender (`hire.lever.co`, already in `mail/data/application_senders.json`), the same
sender-verified (DMARC) rule, and the posting matched by its UUID: `inbox._job_link` is extended to keep a
`jobs.lever.co/{site}/{uuid}` link beside `gh_jid` (5.1). `watch.ATS_NAMES` already falls back to the title-cased key,
so "Lever" needs no new code. A Greenhouse-only classifier, the security-code one (`mail_rules.py:181-190`), stays
Greenhouse-only.

---

## 7. The request policy, Lever

The phases are `checks.py`'s: `PHASE_FILL` (`before_hand_over`: the app is filling), `PHASE_STUDENT` (the window is the
student's; it begins when the agent sends `OP_HANDOFF_READY`) and `PHASE_AFTER_HAND_OVER`. "The app's fill" below is
the first, and "the student's turn" the second.

Phase 5 4.3's routing applies, with Lever's `RoutePolicy`. Rules for every mode, in order:

1. **Main-frame navigations** only to the posting's Lever host (`jobs.lever.co` or `jobs.eu.lever.co`).
2. **WebSockets are refused**, recorded by host.
3. **Public addresses only**, as Phase 5.
4. **Value guard on every host.** A request whose URL, headers or body contains a planned value of 4 or more
   characters is aborted, with exactly **three** exceptions: the hand-over POST (6.12); a GET to `/searchLocations` of
   the field being typed, which may carry that field's text and nothing else; and the **résumé POST** below.

**Hosts.** Lever hosts for documents and static assets (GET of `image`, `font`, `stylesheet`, `script`, `media`).
`LEVER_LOOKUP_ENDPOINTS` is the one pair `{jobs.lever.co, jobs.eu.lever.co} /searchLocations`, tied to the location
field. `CAPTCHA_ENDPOINTS` are the hCaptcha hosts (`js.hcaptcha.com`, `hcaptcha.com`, `api.hcaptcha.com`, and the
asset and image hosts of the challenge), each an exact host and path prefix, **pinned in a fixture after a live
recording** (the checked-in list holds only `hcaptcha.com` and `api.hcaptcha.com`, `checks.py:97-98`). Cloudflare's
detection script is on every Lever page (3.14): its GETs, and any beacon POST under `/cdn-cgi/` on a Lever host, pass
subject to the value guard, and an abort of one is never the turn-ending "aborted non-GET to a form host" of Phase 5's
as-built rule (Phase 5 6.13). The exact paths are pinned after the same recording **[unseen]**. `js.hcaptcha.com` must
be reachable by GET: on the page read, Submit does nothing without it (3.10).

**Refused for every method, silently (as Greenhouse's Snowplow host is):** `googletagmanager.com`,
`google-analytics.com`, and Bugsnag's hosts. The page works without them.

| Mode and phase | Allowed | Aborted and recorded |
| --- | --- | --- |
| `handoff`, before hand-over | GET, HEAD, OPTIONS (subject to rule 4). `GET /searchLocations` for the field being typed. Non-GET to a CAPTCHA endpoint and to Cloudflare's challenge path (rule 4 applies, so their bodies carry no planned value). **The résumé POST** (below): once during the app's fill if L1 is A, and during the student's turn for any file the student attaches. | Every other non-GET, on any host, including every other upload, and a second résumé POST during the app's fill. |
| `handoff`: the student's first POST to the apply URL | The handler asks the parent for the hand-over and calls `route.continue_()` only on a committed True. | The POST, on a False reply, an error, or no reply within 10 s. |
| `handoff`, after hand-over | One POST to the apply URL per attempt. Non-GET to a CAPTCHA endpoint. GETs. | Every other non-GET. |

**The résumé POST** passes only if all of these hold, each tested: during the app's fill, L1 is A and the run has not yet sent one (during the student's turn, no such
condition); the host is
the posting's own Lever host and the path is exactly `/parseResume`; the content type is `multipart/form-data`; the body
has **exactly two parts**, `resume`, whose bytes, during the app's fill, equal the planned file's (checked by SHA-256 against the plan) and, during
the student's turn, are whatever file the student chose, and
`accountId`, which equals the value the page carries in its own hidden `accountId` field (so the app writes no value
into it); and the URL and headers carry no planned value. The multipart body is exempt from the value guard's body check
**only for the `resume` part**, because a résumé contains the student's name and email by design. It is recorded as
`resume_sent_to_lever` with its SHA-256 and the page's reply status, never its body.

**Navigation DNS layer.** `RESOLVABLE_HOSTS` and the Chromium resolver rule (`agent.py:299-311`) fail DNS for every host
outside the list, independent of `route_decision`. Adding Lever means adding its hosts to that union (5.2 item 3); a test
fails if a host in any registered `RoutePolicy` is not resolvable, and the reverse.

---

## 8. Bot detection and politeness

Phase 5 section 8 holds. What Lever adds:

- **Cloudflare sits in front of the page** (3.13) and answered a plain request to `/searchLocations` with 403 where the
  page's own request carries its session. The app does not set a referer, copy cookies, rotate addresses or alter the
  browser to get past that. The check route's plain GET of the apply page worked on 2026-10-04; if Cloudflare ever
  refuses it, the answer is "Lever did not answer" (6.0 step 4), and the run, which uses a real browser window,
  handles a challenge by waiting for the student (6.3).
- **hCaptcha scores the session.** A Playwright-launched Chromium can be flagged even when a person clicks **[1-src,
  Ashby]**. The app changes nothing about the browser (no user agent, timezone or locale change; Lever's page fills a
  hidden `timezone` from the browser by itself and the app leaves it alone). Per-ATS statistics (Phase 5 R1) count
  challenges, so a rising rate for Lever is visible.
- **One application per attempt, the Phase 5 spacing and caps.** Lever documents no per-candidate limit **[doc]**; the
  company limit (D4) is the app's own.

---

## 9. Settings, UI and sentences

- **Settings.** `apply_agent_lever` (a feature registered like `apply_agent`, modes `OFF_ON` as in `automation/ledger.py`, off by default, needs `apply_agent` on) and
  `apply_lever_resume_upload` (off by default; the L1 setting). Both are in Apply agent settings, with the wording of
  L1 beside the second. Neither is ever on by default or turned on by an agent.
- **Per-ATS sentences.** The about 25 student-facing sentences that say "Greenhouse" (`app-apply.js:37,381,493,873,
  1119,1528`, `app-applications.js:46,67`, `app-automation.js:250,296,1213`, `automation/ledger.py:193`, and the server
  strings in `preflight`, `agent`, `agent_types`, `checks`, `runs`, `runner` and `watch`) take the ATS's display name from
  `AtsSpec`, in the LV1 seam PRs. The wording is unchanged for Greenhouse, pinned by the existing tests that name
  `NOT_GREENHOUSE` (`test_apply_api.py`, `test_apply_handoff.py`, `test_apply_policy.py`, `test_apply_runner.py`,
  `tests/ui/test_apply_for_me.py`).
- **The Apply for me section** shows for a saved Lever role when both switches are on, with Finish in browser as its
  only action. There is no rehearsal button, no "Look up options" button and no Submit.
- **Needs you, per reason**, unchanged, plus: "Choose your current location" (the `ats_label` action for `location`), and
  "Lever's form has a question the app doesn't read" (an unknown required control), each linking to the posting.
- **The start confirmation** says, when L1 is A, "The app will attach your résumé. Lever reads it as soon as it is
  attached, so it is sent to Lever before you press Submit." and the run view shows `resume_sent_to_lever`.
- **Location labels.** The store is already per ATS (`apply_ats_labels` is keyed `(user_id, ats, field)`). Lever's
  allowed label fields are `("location",)`; `ALLOWED_ATS_LABEL_FIELDS` becomes per ATS. The student types the label in
  Apply agent settings, and the fill checks it against the page's own options at fill time: an exact match, or the field
  is left. A browser "Look up options" run for Lever's location, so the student can pick from real options as they do on
  Greenhouse, is a later milestone (12, LV5), because the endpoint answers only to a browser session (3.9).

---

## 10. Testing strategy

Every company, site and posting in a fixture is fictional (the Phase 5 rule), and every test installs the real-data guard
(`realdata_guard.install()`, AGENTS.md section 8 rule 12).

### 10.1 Fixtures: `tests/fixtures/apply/lever/`

Three pages, each **rewritten with a fictional company and fictional questions, keeping the structure and every control
name and attribute exactly as read on 2026-10-04** (so the parser is tested against the real shape, and no third party's
text is checked in). Each holds the `<title>`, the `form#application-form` with its inline scripts removed, and nothing
else. `accountId` is a fixed fake UUID.

| File | Modelled on | Exercises |
| --- | --- | --- |
| `demo_eeo_survey.html` | `leverdemo` | the full `eeo[...]` block with the disability signature and date, a `surveysResponses` survey (age range, ethnicity), `pronouns`, `urls[Other Website]`, `urls[Video Link ]` with its trailing space |
| `cards_files_consent.html` | `rover` | a `file-upload` card ("Cover Letter:"), a required certification checkbox, an optional text card, radios, `urls[Twitter]`, `urls[Other]` |
| `many_cards.html` | `palantir` | a 33-box required `multiple-select`, dropdowns that start with the empty-valued `Select...` placeholder the JSON does not list, two `multiple-choice` Yes/No pairs, long `textarea` cards, section headings between cards, the work-authorization and sponsorship questions |

A fourth, `variants.html`, is constructed from the structures seen on the other boards: `opportunityLocationId`,
`consent[marketing]`, `residentialLocation[...]`, a control with no name, an unnamed `select.candidate-location` beside
survey cards, and a survey whose options carry no `optionId`. A dropdown card whose template is 600 KB with 3,000
options is **generated by the test**, not checked in.

Beside them: `thanks.html`, `closed.html` (a 404 body), `cloudflare_interstitial.html`, `parse_resume_reply.json` (a
canned profile with a deliberately wrong `position`), `search_locations_reply.json`, and `endpoints.json` (the pinned
lookup, CAPTCHA and Cloudflare endpoints, filled from the live recording in Q3). The shared `broad_net`, `context_keys`,
`question_keys` and `sensitive_vectors` fixtures (neutral, run by both the Python and JS suites) gain the survey and
EEO wordings seen on the three pages.

### 10.2 Pure and database tests (the default suite, no browser)

- **`test_lever_form.py`:** every numbered row of 5.4, against the three fixtures: standard fields; cards and surveys by
  `baseTemplate`; the JSON limits (oversize, non-object, 201 fields, an unknown `type`, a non-string `text`); the
  DOM and JSON cross-check (type, `required` and option mismatches each give "unreadable"); unknown controls (required and
  optional); page-managed fields never become schema; sections assigned (`standard`, `custom`, `demographic`); the
  `<title>` check with a role that contains " - ".
- **`test_lever_identify.py`:** both hosts; with and without `/apply`, `/thanks`, query and fragment; a non-uuid; a
  site with a bad character; a `lever:%` source row; a Greenhouse URL is not identified; a company-site URL is not.
- **`test_apply_ats_seam.py` (LV1):** old against new for `route_decision`, `decide_outcome`, `confirmation_reached`,
  `identify` and `parse_schema` on every Greenhouse fixture and vector, from a frozen copy of the old code; the three
  corrections (`_limit_check` by `(ats, board_token)`, `SchemaCache` key, per-run `adapter_version`); the per-ATS
  sentences equal today's for Greenhouse.
- **Plan truth table, Lever rows** (Phase 5 7.5 / `policy`): each row of 6.6, including that `eeo[disabilitySignature]`
  and its date are never planned, `pronouns` is never planned, a `urls[...]` label outside the three is empty, an
  unmatched dropdown is a problem and not the first option, and a required multiple-select ticks only the chosen boxes.
- **Policy rows (`checks.py`):** the Lever `RoutePolicy` rows of section 7, one test per cell of the table, and one per
  condition of the résumé POST (L1 off; second résumé POST; wrong host; wrong path; wrong content type; a third part; a
  `resume` part whose bytes differ; an `accountId` that differs from the page's; a planned value in the URL or a
  header). `RESOLVABLE_HOSTS` equals the union of every registered policy's hosts, and the reverse.
- **Outcome rows:** every row of 6.13 without a browser, plus a challenge before a POST is not an outcome.
- **Claims:** a `submit`, `one_click`, `unattended`, `rehearse` or `lookup` request for `ats='lever'` is refused with
  `ats_mode`, in the runner and again inside the claim transaction; the company limit counts a Lever handoff and a
  Greenhouse one for the same company key together, and a Lever site named like a Greenhouse board does not trip it.
- **Mail:** `_job_link` keeps the Lever UUID link; a Lever confirmation from `hire.lever.co` that passes the sender rule
  is matched to its application; the Greenhouse security-code classifier still ignores Lever mail.

### 10.3 `FakeLever` and the sandbox

A test server like `FakeGreenhouse` (`tests/apply_fake_ats.py:204`): the browser sees the real hostnames and a route hook
answers them, so the adapter's host checks run as in production with no network. It serves the fixtures at
`https://jobs.lever.co/{site}/{id}/apply` and `/thanks`; `POST /parseResume` (returning the canned profile after a short
delay); `GET /searchLocations`; a small stand-in for `/js/parseResume.js` written to the **observed behavior** (a field is
the user's only after `change` or `paste` and only if it holds a value; the working, success and failure indicators), not
a copy of Lever's script; a stand-in hCaptcha script whose challenge frame can be switched on and off; and the apply
`POST`, which per scenario answers 302 to `/thanks`, 200 with the form re-rendered, 4xx, or 5xx. A `FakeLeverPageClient`
serves the fixtures to the preflight. `PIPELINE_SANDBOX_FAKE_APPLY=1` also seeds one Lever role, so the sandbox shows a
Lever "what's missing" view and a canned handoff, with no window and nothing sent.

### 10.4 Browser tests (`browser-python`, Chromium, required in CI)

Against `FakeLever`, through the real runner, child and driver:

1. **Order.** With L1 on, the résumé is attached before any field is touched; the fill starts only after the parse ends;
   the fields end up holding the student's facts, not the canned `position`.
2. **L2 A.** A field the parser filled and the plan has no source for is cleared and read back empty, including
   `location` and `selectedLocation` (via the blur), and appears on the "left for you" list.
3. **L1 off.** No non-GET leaves the page before the student's turn; the résumé is on the list.
4. **Location.** The option whose text equals the stored label is chosen, never the first; with no stored label the field
   is left, and is never left half-typed.
5. **Guard.** A résumé POST with other bytes, a second résumé POST, a POST to any other path, a request to a telemetry
   host, a WebSocket, and a planned value in a GET are each aborted and recorded.
6. **Hand-over.** The student's Submit press reaches the handler, the parent commits, and only then does the POST
   continue; a False reply aborts it; a second press is aborted; a closed window or Stop settles `needs_you`,
   `after_click=0`.
7. **Outcomes.** The four rows of 6.13 against the apply `POST` scenarios.
8. **hCaptcha.** A challenge frame during the fill makes the app wait and then continue; a challenge after the press waits
   for the student; the app never clicks inside it.
9. **Banner and interstitial.** The cookie banner in the fixture is never clicked (no click is recorded on it); a
   Cloudflare interstitial makes the app wait, then continue when the form appears or settle `needs_you`.
10. **Page-managed fields** are byte-identical before and after the fill, except `resumeStorageId` and `selectedLocation`.
11. **The student's own attach.** During the student's turn, the page's parse POST passes and the app then names the
    fields Lever changed; before the student's turn and after hand-over the same request is aborted.
12. **A parse that times out** settles `needs_you` with nothing touched and the browser closed, and a reply that
    would have arrived late changes nothing.
13. **A page-wide checkbox relaxation:** with two required checkbox groups, ticking one leaves the other on the "left
    for you" list, whatever the DOM's `required` says.
14. **Keystrokes.** The location search starts only from real key events; `fill()` alone starts none, and the test
    proves the adapter does not rely on it.
15. **Wording.** Every `after_click=0` sentence reads "Nothing was sent" with no résumé sent and "Your application was
    not sent. Lever received your résumé." after one.
16. **`reads_on_attach` does not refuse the board:** a handoff on `FakeLever` starts, where the same flag set the
    Greenhouse way (`uploads_on_attach`) would end it `HANDOFF_S3`.

### 10.5 Static scans (through `tests/helpers_source.py`, a directory, never one file; rule 13)

- `btn-submit` and `hcaptchaSubmitBtn` appear in `apply/` only in the adapter's denylist; no `submit` click purpose
  exists for Lever.
- `_click` accepts only the allowlisted purposes; `route.continue_` is called only on the hand-over path and the
  allowed-request path.
- No module under `apply/` builds a path from its own `__file__` (`tests/test_resource_paths.py`).

### 10.6 CI and the contract tests

No new route and no schema change are expected: the claim refusal reuses the existing start routes, and the new settings
are keys. If a route or the OpenAPI document does change, it is regenerated deliberately (`UPDATE_SNAPSHOTS=1`) and said
in the PR, never to make a refactor pass. `tests/test_layers.py` places every new module; the guards of AGENTS.md section 4
stay as they are.

### 10.7 Which milestone lands which tests

| Milestone | Tests |
| --- | --- |
| LV1 | `test_apply_ats_seam.py`; all existing Greenhouse tests unchanged and green; layer placement |
| LV2 | 10.2 parser, identify, plan rows, claims refusal, mail link; the Lever preflight through `FakeLeverPageClient`; UI test of the Lever "what's missing" view |
| LV3 | 10.2 policy and résumé-POST rows; 10.3 `FakeLever`; 10.4 items 1 to 5, 8, 9, 10; 10.5 |
| LV4 | 10.2 outcome rows; 10.4 items 6 and 7; the sandbox canned handoff; the e2e test (real runner, child and driver) |

---

## 11. Risks and open questions

- **R1. Lever changes its markup.** The adapter carries `ADAPTER_VERSION = "lever-1"`; any change to its selectors or
  rules bumps it. `scripts/apply_shape_check.py` (Phase 5 12.8; planned and not built yet) is built with a Lever mode, GET
  only, that reports markers the adapter no longer finds.
- **R2. hCaptcha challenges.** The student solves them in the window (D14 A). Per-ATS statistics count them.
- **R3. Cloudflare refuses the check route's plain GET** at some point. The answer is "Lever did not answer", never
  "closed" (6.0 step 4), and the one-hour cache keeps the request count low. The app does not work around it.
- **R4. A guess left in the form.** Lever's reader fills fields from the résumé. L2 A clears the ones the app has no fact
  for and the read-back proves it (6.7, 6.8). The risk that remains is a field the app does not know the parser fills.
  The clear list is the parser's own field list (`parseResume.js`), recorded in the adapter and checked by the shape check.
- **R5. Variants.** Surveys, `residentialLocation[...]` address fields, file cards and pronouns exist. Anything the parser
  does not recognize is left for the student, never filled; a required one is a problem that blocks nothing but is listed.
- **R6. The résumé leaves before Submit** (L1 A). Disclosed in the start confirmation, off by default, recorded.
- **R7. Slug collisions across ATSs** (a Lever site and a Greenhouse board with one name). Fixed in the seam (5.2 item 4).
- **R8. EU and custom hosts.** `jobs.eu.lever.co` is covered; a company's own domain that fronts Lever is not identified.

**Open questions** (each answered by a recording, not by argument):

- **Q1. What does Lever answer to a successful and to a refused POST?** Written for both in 6.13. The first real Finish
  in browser records the status codes and the redirect; this file and a fixture are then updated, and the
  `unconfirmed` row is narrowed if the answer allows.
- **Q2. Does Lever accept a Submit whose form carries the file but no `resumeStorageId`?** Matters only for L1 B and for
  a parse that failed (the run continues in that case, so a real Submit is the test).
- **Q3. The exact hCaptcha and Cloudflare endpoint lists.** The load-time hosts (`js.hcaptcha.com`, its asset hosts,
  Cloudflare's `/cdn-cgi/` paths) are recorded from a page load before LV3 merges and pinned in `endpoints.json`. The
  execute-time endpoints exist only after Submit is pressed, so they are first seen at the first real handoff. Until
  then the non-GET allowlist holds what `checks.py` already has (`hcaptcha.com` and `api.hcaptcha.com`, the hosts
  hCaptcha posts to). If the challenge needs another, the run says "The form tried to send to an address the app doesn't
  recognize" and nothing has left; the list is then extended from the recording.
- **Q4. The reply shape of `/searchLocations`** (and its option text), for the location label and its fixture.
- **Q5. Does any Lever board show an emailed code?** None of six did. If one does, the run stops (6.9).

---

## 12. Milestones

Each is one PR (or two where marked), off by default, with its `SETUP.md` change, and passes the full CI gate: unit, UI,
extension, extension-browser, `browser-python`, fuzz, postgres, secrets. **No milestone presses Submit**, and none needs a
change to AGENTS.md "Never auto-apply".

| # | PR | Contents | Done when |
| --- | --- | --- | --- |
| LV0 | Decisions | The student answers L1 and L2; this file is updated with the answers. No code. | Done 2026-10-07: L1 A, L2 A (section 4). |
| LV1a | The seam, part 1 (refactor only) | `apply/ats.py` (`AtsSpec`, registry), the `AtsAdapter` Protocol, `AgentJob.ats`, the factory argument. Greenhouse is the only registered ATS. | All existing tests green unchanged; `test_apply_ats_seam.py` parity rows green. |
| LV1b | The seam, part 2 | `RoutePolicy` and the per-ATS `route_decision`, `decide_outcome`, `confirmation_reached`; the `RESOLVABLE_HOSTS` union; the per-ATS branches in `ApplyAgent._run` and the `uploads_on_attach` split (5.2 item 2); per-ATS sentences. **The three corrections of 5.2 item 4 land as separate commits**, each with a test that failed first. | Parity green; the three corrections' tests green; wording for Greenhouse unchanged. |
| LV2 | Lever, read-only | `apply/lever.py`, `apply/lever_form.py`, `LeverPageClient`, the three fixtures, the `apply_agent_lever` switch, the Lever check route answer, `ats_mode` refusal, per-ATS label fields, the sandbox Lever role, `_job_link` for UUIDs. No browser. | 10.7 LV2 row green; for any saved Lever role the student sees what is missing, and the tracker is unchanged. |
| LV3 | Lever driver | `apply/lever_adapter.py`, the Lever `RoutePolicy`, the résumé flow (L1, L2), EEO and consent rules, `FakeLever`, the load-time recording of Q3 and the reply shape of Q4, both from a browser page load with GETs only, pinned in `endpoints.json`. Reachable only in tests and the sandbox. | 10.7 LV3 row green; the load-time part of Q3 and Q4 recorded. |
| LV4 | Finish in browser on Lever | Hand-over interception on the apply POST, the outcome table, record, the watch, the `SETUP.md` step (the two switches, L1's wording), `docs/assisted-apply.md` and the Threat model row in `docs/THREAT_MODEL.md` for the résumé POST. | 10.7 LV4 row green; the e2e test green; `portability` workflow dispatched and green (this change touches processes). |
| LV5 | Look up options for Lever's location (optional) | A browser lookup run for the `location` label, so the student picks from the page's own options. | The label store fills from a real option list in the sandbox. |

**Rollout checklist** (after merge, in the student's own app; not a PR condition):

- after LV2: open three saved Lever roles and confirm the "what's missing" lists look right and the tracker did not change;
- after LV4: the first Finish in browser at a Lever posting the student actually wants, watched end to end including the
  email and the hCaptcha, with `apply_lever_resume_upload` on or off as chosen. It answers Q1 and Q2; this file and a
  fixture are updated from what was seen before a second one is run.

**Deploying** follows PLAN.md:128-131 and the memory note `restart-web-dashboard`: back up `data/platform.db` first, fast-forward
live main, restart the server by process id. No migration is part of this plan.

---

## Appendix A. Ashby: what carries over, and what is still open

Ashby is not designed here. It needs its own spec after LV4 has run on a real posting. What the Lever work gives it, and
what it does not:

**Carries over:** the seam (`AtsSpec`, `AtsAdapter`, `RoutePolicy`, per-ATS sentences), the classifier and sensitive store,
the claims, limits and watch (`ashbyhq.com` is already in `application_senders.json`), and the handoff structure.

**Different, and not yet decided:**

1. **Autosave.** Ashby's fields autosave as they are filled **[1-src]**, so filling a form sends partial data to the employer
   before Submit. The student must decide first whether Finish in browser may fill at all on Ashby: with a disclosure at
   every start, only fields the student types, or not at all. Nothing in the Lever work answers this.
2. **No read-only check without a browser, unless a fragile source is accepted.** The posting API gives the job, not the
   form. The other project reads the form from an unauthenticated GraphQL endpoint (`ApiJobPosting`) whose introspection is
   disabled; it is undocumented and could change. The alternative is to scan the DOM, which needs a browser.
3. **Hydration and binding quirks [1-src]:** text typed before hydration finishes is wiped; a Yes/No pair is one checkbox that
   does not bind on its first touch; radio groups sit outside the field container; an upload reflows the page and can wipe
   typed values; a short answer can truncate silently at `maxLength`. Each needs a read-back rule and a fixture.
4. **A puzzle question is always `needs_you`**, and a "please don't use AI" essay is a hard stop (Phase 5 7.4).
5. **Per-candidate application limits** (the other project's table lists OpenAI, Mercor, Cognition and others, **[1-src]**) show
   as a banner on the form. A banner reading is a `needs_you` rule, not a fill.
6. **Boards embedded on a company's own site**, where the posting API is live and the public page is not, and the form may be
   cross-origin.

---

## Appendix B. Files touched

New: `docs/phase5-lever-handoff-spec.md` (this file); `opportunity_app/apply/ats.py`, `lever.py`, `lever_form.py`,
`lever_adapter.py`; `tests/test_lever_form.py`, `test_lever_identify.py`, `test_apply_ats_seam.py`, `test_apply_lever_*.py`;
`tests/fixtures/apply/lever/`; `FakeLever` in `tests/apply_fake_ats.py`.

Changed: `opportunity_app/apply/agent.py` (adapter Protocol, factory, host union), `agent_types.py` (`AgentJob.ats`, sentences),
`checks.py` (`RoutePolicy`), `schema_client.py` (the Lever page client), `preflight.py` (identify and parse by ATS, sentences,
cache key), `policy.py` (`ALLOWED_ATS_LABEL_FIELDS` per ATS, `sources_for`), `runner.py` (ATS, adapter version, refusal),
`runs.py` (`_limit_check`, sentences), `watch.py` (sentences), `applications/inbox.py` (`_job_link`), the Apply for me static
scripts and Apply agent settings (the two switches and the wording), `docs/phase5-apply-agent-spec.md` (the "Later" row points
here), `docs/assisted-apply.md`, `docs/THREAT_MODEL.md`, `SETUP.md`, `tests/test_layers.py`.

Not changed: any migration, `pipeline_core/` (dependency-free, and not involved), the browser extension, `runner_child.py`,
`claims.py`, `sessions.py`.
