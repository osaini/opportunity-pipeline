# Phase 5 specification: "Apply for me" (Greenhouse pilot)

- **Status:** revision 2.2, 2026-09-29. Revision 2.1 was written 2026-09-28 after an internal
  review and two Codex rounds (log: `phase5-apply-agent-spec-review.md`). On 2026-09-29 the
  student answered D1 to D14 (M0 done; see "Answers recorded 2026-09-29" at the top of section
  3) and approved building **M1 to M4, then M4s**, under D1 B. AGENTS.md "Never auto-apply"
  still stands: nothing in M1 to M5b presses Submit. M5a onward, and M6 above all, still need
  their own go-ahead.
- **Build order (2026-09-29):** M1, M2 and M3 in parallel, then M4, then M4s. Because D5 is C,
  M4s comes before M5b so that Finish in browser can fill the stored answers.
- **Base:** origin/main `5d95aa9` (Phases 0 to 2 merged and deployed). Line numbers below were
  taken from that commit and were refreshed against ac2398d on 2026-09-29. Revision 2 re-checked the ones the review questioned: sidepanel.js:82
  (injection list) and :212 (answer save; the call starts at :210) are right; the contact-form
  factory wiring is api.py:1063-1064; `discover_ats` lives in pipeline.py:2277, not opportunities/boards.py.
- **Since this was written (2026-10-02):** the code moved into packages (`opportunity_app/apply/`, `outreach/`,
  `mail/`, `automation/`, `student/`, `core/`, and so on), `api.py`'s routes into `web/routers/<feature>.py`, and
  `app.js` into ordered `app-*.js` scripts. Paths in this document use the new layout. A bare module name such as
  `apply_runs`, `apply_policy`, `apply_checks`, `apply_preflight`, `extension_apply`, `mail_trust`,
  `resume_variants`, `outreach_forms`, `outreach_gmail`, `outreach_render`, `document_artifacts` or `user_time` is the
  module's name before the move, kept here as shorthand (`apply/runs.py`, `apply/policy.py`, `apply/checks.py`, `apply/preflight.py`,
  `applications/extension.py`, `mail/trust.py`, `student/resume_variants.py`, `outreach/forms.py`,
  `outreach/gmail.py`, `outreach/render.py`, `student/artifacts.py`, `core/user_time.py`); `apply_runs` is also a
  table name. File:line citations of `api.py` and `app.js` are as of the base commit and no longer match the
  split files. The README manual is now `docs/guide/`, so citations of README sections name the heading. The
  migration this plan calls 0044 was numbered 0045 when it was built (`migrations/0045_apply_agent.sql`), and
  `apply_agent.py`, `test_apply_watch.py`, `test_apply_agent_browser.py` and `scripts/apply_shape_check.py` are
  planned files that M5a and M5b have not built.
- **Relation to PLAN.md:** this document expands PLAN.md "Phase 5: Apply agent pilot"
  (PLAN.md:1099-1255). PLAN.md is the student's automation roadmap and is kept outside the
  repository, so its line references are to that file. Where the two differ, this document wins. Appendix A lists every
  difference and why.
- **How to read it:** section 1 is the plain-words version. Section 3 holds the choices only the
  student can make; nothing in sections 4 to 14 should be built until those are answered.
  Sections 4 to 12 are for the engineer. Sections 13 and 14 are for both.

Research confidence marks, where a claim rests on outside research:
**[live]** seen on a live public page on 2026-09-28; **[doc]** vendor documentation;
**[1-src]** one third-party report (lower confidence).

---

## 1. Summary in plain words

**What it does.** On a saved Greenhouse job, the student presses **Apply for me**. The app
opens a visible Chromium window, loads the job's own Greenhouse application form, and fills it
in from four places only:

1. facts the student has confirmed in their profile (name, email, phone, links);
2. answers the student saved before, when the question is word for word the same **and** the
   answer was saved for this company (as built, v1: never carried to another company, see 7.1 "As built");
3. the résumé picked for this role, or the student's confirmed résumé when no variant is picked;
4. exact option labels the student picked once for typeahead lists such as school and location.

**A rehearsal comes first, and it sends nothing.** The app fills the form, stops before the
Submit button, and shows the student screenshots plus a table of every field, the value that
went in, and where it came from. During a rehearsal the app blocks every request that could
submit the form, and every request, to Greenhouse or anywhere else, that carries a filled-in
value. It does not type sensitive answers (work authorization, demographics, consent boxes) into
the page at all during a rehearsal; it only checks that the answer it would give is one of the
options. The one exception is typeahead fields such as location and school: looking up the
options sends the text typed into that field to Greenhouse's lookup service, and the preview names
those fields.

**In the first version the student always presses Submit.** **Finish in browser** opens the
form again, fills everything it can fill truthfully, leaves the rest empty, lists it under
"left for you", and brings the window to the front. The student completes those fields and
presses Greenhouse's own Submit button. When Greenhouse shows its confirmation page, the app
asks "Greenhouse showed its confirmation page. Mark as applied?".

**Later, only if the student decides to (D1),** a one-click **Submit application** button would
let the app press Submit itself. It would fill the form again on a fresh page, check that
nothing changed since the student confirmed that exact plan, and only then press the button.

**Looking changes nothing.** Opening the Apply for me section, seeing what is missing, and
rehearsing never create or change an application in the tracker. The application row is
created only when the student starts Finish in browser or a submit.

**The email watch.** For 24 hours after a submission, the app looks for Greenhouse's "thank you
for applying" email among the mail it already reads.

- If an email from Greenhouse's verified sender matches the role, the card says so.
- Some employers turn that email off **[doc]**. So when none arrives, the card says "No
  confirmation email yet. Some employers don't send one", never that the application failed,
  and it never suggests applying again.

**When it can't fill something truthfully, it leaves it for the student.** The app never
guesses. Examples:

- a required question with no saved answer for this company;
- a sensitive question (work authorization, sponsorship, demographics, salary, consent) that the
  student has not allowed the app to answer;
- a picture CAPTCHA, or Greenhouse asking for an emailed security code;
- a required cover letter the student has not approved.

In a rehearsal these appear as a list, each with one action: answer it once and save it, look up
the exact option, draft a cover letter. In Finish in browser they are left empty for the
student to complete in the window.

**What it never does:**

- invent an answer, or reuse an answer saved for another company unless the student marked it
  reusable;
- write an essay;
- solve a CAPTCHA;
- disguise the browser;
- sign in to MyGreenhouse or click Greenhouse's "Autofill my application";
- touch LinkedIn or Workday;
- submit the same Greenhouse job twice, even from two saved copies of the same posting;
- send an application without the student pressing a button for that specific application.

**Why it is built so cautiously.** It is the student's name on every application.

- Greenhouse lets a recruiter mark a candidate as spam. After that, every future application
  from that email address to that employer is rejected automatically **[doc]**. One bad
  automated submission can close an employer off for good.
- Greenhouse scores every submission with reCAPTCHA Enterprise. A browser driven by automation
  may be scored as a bot even when it is honest about what it is **[live]**, **[1-src]**.

So the pilot:

- fills only from exact sources;
- makes at most one agent submission per company per month by default;
- rehearses on real postings (filling without sending) before a Submit button is ever offered;
- corroborates each submission with the confirmation email, where the employer sends one.

**Later, only if the student decides to,** an unattended mode could apply to high-scoring saved
roles on its own, within a daily cap. This spec describes it, but it needs its own decision and
its own rule change, after the one-click version has a track record.

---

## 2. Goals and non-goals

### Goals

- G1. Fill a Greenhouse hosted application form (new `job-boards.greenhouse.io` board, and the
  legacy form where it still exists) from confirmed facts, exact saved answers for this company
  or marked reusable, the chosen résumé and, when required, an approved cover letter.
- G2. Rehearse without submitting. Exactly what is enforced: no request that could submit the
  form leaves the browser; no request to any host, Greenhouse included, carries a filled-in value;
  after the first input only audited lookup endpoints and static assets are reachable; WebSockets
  are refused; sensitive answers are not typed into the page. The one disclosed exception is a
  typeahead lookup, which sends the text typed into its own field to Greenhouse's lookup
  service. The student sees exactly what would be sent.
- G3. After the student confirms that specific plan (D1 A only), submit by pressing the ATS's own
  submit control, never a generic "button that says Submit".
- G4. Record the outcome honestly (`submitted`, `unconfirmed`, `needs_you`, `failed`) with
  evidence. Show an inferred result as inferred. Say "nothing was sent" only when the app saw
  that nothing left.
- G5. Never submit the same Greenhouse job twice, across crashes, restarts, races, two server
  processes, and duplicate saved copies of one posting.
- G6. Corroborate submissions with the Phase 1 confirmation email where the employer sends one,
  and show when none has arrived without calling it a failure.
- G7. Share one field engine between the browser extension and the agent, so there is one set
  of refusal rules.
- G8. Keep it personal and off by default: every setting per student, an honest empty state, and
  a SETUP.md step.
- G9. Never change the tracker unless something was actually handed to Greenhouse, or the student
  said so.

### Non-goals (v1)

- Lever and Ashby. They come later (section 14; Lever has a draft spec, `phase5-lever-handoff-spec.md`). Ashby's forms send data as they are filled **[1-src]**,
  and Lever's page sends the résumé the moment it is attached **[live]**, so the Greenhouse rehearsal model does not carry over.
- Workday, iCIMS, SmartRecruiters, Workable, company-built forms that post through the
  employer's own API key, and multi-page flows.
- LinkedIn Easy Apply or any LinkedIn automation (PLAN.md:1342).
- MyGreenhouse sign-in, "Autofill my application", "Apply with SEEK", or any account creation.
- CAPTCHA solving, paid solver services, stealth plugins, or changing the browser's user agent,
  timezone or locale (PLAN.md:23).
- AI-written answers, essays or field mappings. The pilot has no model in the loop. If one is
  ever added, it needs its own provider selector with a fallback (memory note
  ai-feature-model-selectors).
- ~~Reading the Greenhouse security code from Gmail automatically (D10).~~ No longer a non-goal:
  the student chose D10 B on 2026-09-29.
- Running from anywhere but the student's own computer. Cloud and data-center IP addresses are
  a high-risk fraud signal for Greenhouse **[doc]**.
- A real browser in the sandbox. The sandbox and every test use a fake agent or a local fake
  Greenhouse; nothing in them can reach a real employer (12.2, 12.6).
- Unattended mode is described but is **not** part of the first build (D2).

---

## 3. Decisions the student must make before build

### Answers recorded 2026-09-29

| # | Answer | Differs from recommendation? |
| --- | --- | --- |
| D1 | **B.** The student presses Submit (Finish in browser). "Never auto-apply" stays; M6 waits for a later D1 A. | No |
| D2 | **B.** Unattended only after one-click has 10+ submissions, 8+ `email_confirmed`, no wrong marks, and a separate yes. | No |
| D3 | **B.** 3 clean rehearsals at different companies. | No |
| D4 | **As tabled.** 10 min spacing, 5/day, 1 per company per 30 days (overridable per application), 20 rehearsals/day; unattended 1/hour, 3/day. | No |
| D5 | **C (i).** Work authorization, sponsorship, 18+, plus EEO stored only as "Decline to self-identify", legal acknowledgments and consents. Export control, citizenship, clearance, salary and "everything else" stay manual. | **Yes** (recommended A while D1 is B) |
| D6 | **B.** A newer intent wins. | No |
| D7 | **A.** Visible window. | No |
| D8 | **90 days, masking (i).** | No |
| D9 | **B.** Tick only on an exact stored statement match; statements citing a document are per company. | **Yes** (recommended A while D1 is B) |
| D10 | **B.** Read the security code from Gmail and type it in, in every mode. The student confirmed after being told it conflicts with their 2026-09-26 "no solvers" line, defeats Greenhouse's low-score check, and risks a spam mark. | **Yes** (recommended A) |
| D11 | **B.** Attach the latest approved letter for this role; otherwise Draft one. Optional cover-letter fields stay empty. | No |
| D12 | **C.** Watch required for one-click and unattended; optional for Finish in browser. | No |
| D13 | **A.** Fill optional fields from exact sources only; optional sensitive fields only from the store. | No |
| D14 | **A.** Never click a visible CAPTCHA checkbox. | No |

**What the three departures change:**

- **D5 C (i) means M4s is built**, before M5b, so that Finish in browser can fill these fields.
  The M4s PR rewrites docs/assisted-apply.md step 3 ("Sensitive or consequential fields remain
  manual"). Sub-choice (i) stores no demographic value, so THREAT_MODEL.md:17 and
  PRIVACY_ACCESSIBILITY.md:7 stay true, but the store still needs its Threat model row and the
  12.7 test that employer and reporting code never read it.
- **D9 B applies under Finish in browser too:** consent boxes with an exact stored statement are
  ticked before the window is handed over, and the plan preview shows each ticked box with its
  linked document address. It lands with M4s (store) and M5b (fill), not only with M6.
- **D10 B adds a Gmail code reader.** It reads, through the Phase 1 connection (read-only scope),
  only a message from Greenhouse's verified sender that arrives after the submit it belongs to
  and names the same company. It types the code only into the same window's code field, once.
  If no such message arrives within 10 minutes, or two candidates arrive, it falls back to D10 A
  (the window comes to the front for the student). Each use is recorded on the run, and
  per-ATS statistics count security-code prompts (R1), so a rising rate is visible. It lands
  in M5b. The non-goal in section 2 is struck through. The student accepted these reader rules
  on 2026-09-29. The reader needs the same thing as D12: if the
  application email is not the Gmail account the app reads, D10 falls back to A. R4 (the code
  email misread as a confirmation) matters more now, so its labelled example is required in
  M5b, not open.

Each decision below lists options and a recommendation. The
recommendations follow how the student has decided similar questions before. For example, they
approved no-shadow sends only where they had already approved the content, and they kept
judgment calls manual (memory notes outreach-email-preferences, decline-thank-you and
automation-roadmap). They are still only recommendations.

**How the recommendations fit together.** The recommended path is D1 B now (the student always
presses Submit, through Finish in browser), with D5 A, D9 A and D14 A, and D12 C. That path needs
no rule change in AGENTS.md and creates no new store of demographic data: every sensitive
question and consent box is simply left for the student in the window. The one-click questions
(D1 A together with D5, D9 and D14) are best decided together, after the student has used
Finish in browser on real applications.

### D1. The gate and the "Never auto-apply" rule

AGENTS.md, "Product invariants", says "Never auto-apply." The same idea appears in several other places:

- README.md, "What it never does" (the intro sentence and the Apply Mode statement), and the
  Apply Mode bullet of docs/guide/web-app.md, "Full opportunity platform";
- docs/THREAT_MODEL.md:12;
- docs/assisted-apply.md:5-6 and :36-37, which says "There is no success-page inference";
- the "Extension safety" row of docs/ACCEPTANCE.md;
- apps/extension/README.md:3-5 and the extension's manifest.

**Options:**

- **A. Rewrite the rule and allow one-click submit.** AGENTS.md would read:
  "Applications are submitted only by the opt-in apply agent (`opportunity_app/apply_agent.py`),
  and only after the student confirms that specific submission in the app, under the policy in
  `docs/assisted-apply.md`. The browser extension never submits." The existing sentence about
  external delivery stays. This wording does **not** cover unattended mode, which would need its
  own rewrite (D2).
- **B. Keep the rule, and build rehearsal with Finish in browser only.** The agent fills the
  form; the student completes what is left and presses Greenhouse's Submit button themselves in
  the agent's window.
  - The agent never presses Submit, so "Never auto-apply" stays literally true.
  - The app would still see the confirmation page. It asks "Greenhouse showed its confirmation
    page. Mark as applied?" rather than moving the stage by itself, so assisted-apply.md's "no
    success-page inference" stays true. Each claim records this choice (`stage_policy='ask'`,
    5.2), so no later code can reinterpret it.
  - assisted-apply.md still needs one addition in M5b: a section saying the app can open the
    employer's form in its own window and fill it, while the student reviews, completes the
    sensitive fields and presses Submit. docs/THREAT_MODEL.md gains its "Apply agent" row in the
    same PR (11).
- **C. No agent.** Do only the engine split (M1). The extension keeps filling in the student's
  own Chrome, and they submit. This is the Simplify model.

**Recommendation:** approve **B now and A later**. Milestones M1 to M5b build B. M6, the step
that presses Submit, is the only part that needs A. Decide it after the student has used Finish
in browser on a few real applications and seen the rehearsal plans.

### D2. One-click only, or unattended later

- **A.** One-click only, and never unattended in this phase.
- **B.** One-click now. Unattended becomes a separate later decision, offered only after one-click
  has at least 10 submissions with at least 8 of them `email_confirmed` and no "wrong" marks.
- **C.** Build unattended now, in shadow.

Unattended is the application version of first-email autopilot, which the student declined on
2026-09-26. The D1 A wording covers only submissions the student confirmed one by one, so
unattended mode would also need its own AGENTS.md rewrite, in the M8 PR, decided by the student
then. Each sensitive answer would need to be consented again for unattended use (5.4).

**Recommendation: B.** Section 9.4 specifies unattended mode so the design does not paint it
into a corner, but milestone M8 waits for its own yes.

### D3. Rehearsals before the Submit button appears

A "clean rehearsal" is a real posting filled end to end with no required-field gaps and no
deferred résumé upload, which the student marks right (9.2). How many clean rehearsals on the
ATS, each at a different company, before the **Submit application** button is offered?

- **A.** 5, as PLAN.md:1128 says.
- **B.** 3.
- **C.** 1.

**Recommendation: B, 3.** Every one-click run already rehearses and shows its plan before each
submit. The gate is about trusting the Greenhouse adapter itself, and three different employers
exercise three different sets of custom questions. Unattended (D2) would need 5 more on top.
Finish in browser needs no gate, because the student presses Submit.

### D4. Caps and pacing

| Limit | Options | Recommended |
| --- | --- | --- |
| Time between two agent submissions | 5 / 10 / 30 minutes | **10 minutes** |
| Agent submissions per day (one-click and unattended) | 3 / 5 / 10 | **5** |
| Agent submissions per company | 1 per 30 days / 1 per 90 days / none | **1 per 30 days**. One-click and Finish in browser may override it for a single application with an explicit tick: "I know Apply for me handed an application to {company} to Greenhouse on {date} (it may not have gone through). Apply anyway." |
| Rehearsals and option lookups per day | 10 / 20 / 40 | **20** |
| Unattended only (if ever, D2) | 1 per hour and 3 per day / 1 per hour and 1 per day | **1 per hour, 3 per day** |

- Finish in browser counts toward the spacing and the company limit, but not the daily cap
  (9.1).
- Every attempt that was handed over to Greenhouse counts, whatever happened after, including
  one the student later said didn't go through. Greenhouse may still have it.
- The company limit matches both the company name and the Greenhouse board, so a company listed
  under two spellings is still one company.
- **The same Greenhouse job can never be submitted twice by the agent.** No tick overrides that
  (9.1).
- **An interview or an offer already in progress at the company, for another role, asks for a tick.**
  The code is `active_at_company` and the sentence names the stage and the role: "You have an
  interview in progress at {company} for {role}. Applying to another role there may cross wires with
  it." Finish in browser and one-click may carry the tick; unattended mode cannot, because the tick is
  the student's own decision. The company is matched by name (the same key as the company limit), and
  the stages that count are `interview` and `offer`.

The per-company limit exists because of Greenhouse's application-limit rules and its permanent
spam marks **[doc]**.

### D5. Which sensitive questions may ever be answered automatically

The app decides what counts as sensitive. Section 7.3 has the exact classifier. The categories
are:

- work authorization;
- visa sponsorship and immigration status (including F-1, OPT, CPT, J-1, H-1B, TN, E-3);
- 18 or older;
- export control, citizenship and security clearance ("Are you a U.S. person?", which aerospace
  and robotics employers ask often);
- EEO voluntary self-identification (gender, Hispanic or Latino, race, veteran, disability);
- legal acknowledgments (privacy notice, accuracy attestation);
- data-processing consents;
- salary expectation;
- everything else sensitive (age, birth date, pronouns, sexual orientation, marital status,
  religion, criminal history, non-compete agreements, and any sensitive question the classifier
  cannot place). These are never answered automatically, in any option.

**Options:**

- **A.** None. Every sensitive field is left for the student: in Finish in browser they complete
  it in the window; in one-click the run stops as Needs you. Nothing sensitive is stored.
- **B.** Work authorization, sponsorship and 18-or-older only.
- **C.** B, plus EEO self-ID, legal acknowledgments and consents. With a sub-choice for EEO:
  - **(i)** store only "Decline to self-identify" answers;
  - **(ii)** store the student's actual answers.
- **D.** C, plus export control, citizenship and clearance.
- **E.** D, plus salary.

In options B to E, a sensitive field is answered **only** from an entry the student added
deliberately in Apply agent settings. The entry holds the exact question wording, the answer, a
consent timestamp and the consent's scope (5.4). It never comes from the saved-answer library or
the profile.

**What B to E change.** They reverse two documented positions, and the PR that builds the store
(M4s) must rewrite them:

- docs/assisted-apply.md step 3: "Sensitive or consequential fields remain manual."
- docs/THREAT_MODEL.md:17: "Demographic attributes are deliberately not collected" (options C to
  E with EEO sub-choice (ii); (i) stores no demographic value). docs/PRIVACY_ACCESSIBILITY.md:7
  and accounts/employer.py's `not_measurable_without_explicit_consented_demographic_data` are written to
  start using consented demographic data once it exists. The consent text therefore limits use
  to filling application forms, and a test proves the employer and reporting code never read the
  store (12.7).

**Recommendation: A while D1 is B.** Under Finish in browser the student is at the window
anyway, so leaving these fields to them costs a few clicks and stops nothing. It keeps both
documented positions true and needs no new store. Decide B or C (with EEO sub-choice (i))
together with D1 A, if one-click is wanted. In every option:

- keep export control, citizenship and clearance out: a wrong answer there can have legal
  weight;
- keep salary out: it is a negotiating position, not a fact;
- "everything else" is never auto-answered.

### D6. Pause and a confirmed submit

The code's precedent is that the student's own Send through a form is never stopped by pause
(outreach/forms.py:33-34, automation/ledger.py:12-15). PLAN.md decision 6 says pause holds even sends
the student scheduled (PLAN.md:1304), and "Pause means nothing leaves".

- **A.** Pause stops everything, including a one-click submit the student confirmed and a
  Finish in browser submission.
- **B.** Pause stops every run the app starts by itself (worker rehearsals, anything unattended).
  For a one-click submit, a pause pressed **after** the student's confirm stops it, right up to
  the moment of hand-over, because that pause is the newer intent. A pause that was already on
  when the student confirmed does not stop it: the confirm is the newer intent. In Finish in
  browser the student's own press of Submit in the window is the confirm, so pause does not stop
  it.

Either way, a one-click run shows **Cancel** until hand-over (10.2), and closing the window
before hand-over cancels it too. The confirm expires 15 minutes after the rehearsal it approved.

**Recommendation: B.** A confirmed submit is the student's own act, like Send now, but unlike
Send now there are 1 to 3 minutes of filling between the confirm and the send, and a newer pause
must win in that gap. Pause still shows a submit as in flight once it is handed over (5.6).

### D7. Visible or minimized window

Headless is not offered. PLAN.md 5.3b requires a headed browser, and headless automation is
flagged more often.

- **A.** Visible window.
- **B.** Minimized window. Chromium may slow down timers in a minimized window, which can break
  the page's scripts. See 13 R8.
- **C.** Window placed off-screen.

**Recommendation: A** for everything in v1. The student is at the computer, and watching it fill
is part of trusting it. B could come later, for unattended mode only. On Linux the app's
background service needs access to the desktop display for this (5.6, SETUP step).

### D8. Screenshot retention and masking

Screenshots of filled forms contain personal data. They are stored under
`data/private/apply/` (section 11).

- Retention options: **30 days / 90 days / 180 days** (the mail-evidence default,
  applications/inbox.py:109), or **until the application closes plus 30 days**.
- Masking options. A mask covers the whole field: its label, the control, the value shown next to
  it (react-select shows the chosen value in a separate element) and any error text.
  - **(i)** mask the fields filled from sensitive answers;
  - **(ii)** mask every personal field (phone, address, answers).

**Recommendation: 90 days, masking (i).** The file's SHA-256 stays on the run record for good.
The plan table in the app shows the values in full, and it is local to the student's own
loopback app. Deletion runs on its own once a day (11).

### D9. Consent and acknowledgment checkboxes

There are two rules in the code today. The contact-form submitter ticks required consent boxes
(outreach/forms.py:393). The extension refuses anything that says consent (content.js:7).

- **A.** Never tick them. In Finish in browser they are left for the student; in one-click they
  are Needs you.
- **B.** Tick them only from a sensitive-answers entry whose stored statement text matches the
  box's label exactly. Any change in wording means the box is left alone.
  - A statement that says "I have read" or links to a document (a privacy notice) must be saved
    for one company, never for any company: one employer's notice is not another's.
  - The plan preview shows the linked document's address next to the tick.
  - Requires D5 C or higher.

**Recommendation: A while D1 is B** (the student is at the window). **B** together with D1 A and
D5 C, since Greenhouse forms often require a privacy acknowledgment **[live]** and a one-click run
would otherwise stop nearly every time.

### D10. Greenhouse's emailed security code

When reCAPTCHA scores a submission low, Greenhouse asks for an 8-character code it emails to the
applicant **[doc]**, **[1-src]**.

- **A.** Manual. The window comes to the front and stays open for up to 10 minutes, and the
  student types the code in.
- **B.** Read the code from Gmail through the Phase 1 connection and type it in automatically.

**Recommendation: A.** The code is a humanity check, and B would be a solver in all but name. The
student held that line on 2026-09-26.

### D11. Cover letters

- **A.** A required cover letter is always left for the student (Finish in browser) or Needs you
  (one-click).
- **B.** Attach an approved cover letter for this role if one exists (the existing
  `preparation` draft, approve and render path). Otherwise left for the student or Needs you,
  with a **Draft one** button that opens that path; the student approves it, then runs again.
- **C.** B, plus attach an approved cover letter to *optional* cover-letter fields too.

"Approved cover letter for this role" means the **latest** cover-letter version for this role,
and only when that version is approved. A newer draft, or two approved candidates, means the app
asks rather than choosing (6.9). An optional cover-letter field with nothing approved is left
empty in every option.

**Recommendation: B.** It uses the existing template drafter (student/preparation.py:98-116) and needs
the student's approval (student/preparation.py:251). The agent never attaches an unapproved or outdated
document.

### D12. Must the confirmation-email watch be available?

The watch needs two things:

- "Update applications from job emails" (`application_mail`) in shadow or on. Shadow still reads
  and records messages (applications/inbox.py:1165, 1707).
- The email typed into the application must be the Gmail account the app reads. The app does not
  store that address today; revision 2 records it when Gmail is connected (5.1). A connection
  made before this phase needs one reconnect.

Greenhouse lets employers turn the confirmation email off **[doc]**, so the watch corroborates;
it cannot prove a submission was lost.

- **A.** Required for every mode. Apply for me cannot be turned on without both.
- **B.** Not required for any mode. The card shows "The app isn't checking for a confirmation
  email" instead.
- **C.** Required for one-click and unattended; optional for Finish in browser, where the student
  pressed Submit and saw the result themselves.

**Recommendation: C.** It keeps Finish in browser usable for a student without Gmail connected,
while the modes where the app presses Submit keep their corroboration.

### D13. Optional fields

- **A.** Fill optional fields only when the same exact-source rules give a value; otherwise leave
  them blank.
- **B.** Leave every optional field blank.

**Recommendation: A**, except that an optional *sensitive* field is left blank unless a
sensitive-answers entry exists for it.

### D14. A visible CAPTCHA checkbox

Greenhouse's usual reCAPTCHA is invisible **[live]**, so this is rare. Some boards may still show
an "I'm not a robot" checkbox (reCAPTCHA, hCaptcha or Turnstile).

- **A.** Never click it. In Finish in browser it is left for the student. In one-click the window
  comes to the front before hand-over, the student ticks it, and the run then continues; nothing
  has been sent until then.
- **B.** Click the checkbox, and only the checkbox, as the contact-form sender does
  (outreach/forms.py:1047-1076). A picture challenge still goes to the student.

**Recommendation: A.** Ticking "I'm not a robot" in the student's name is in the same family as
reading the security code (D10), and B would be inherited from contact forms rather than decided
for applications.

---

## 4. Architecture

### 4.1 The pieces

```
 app.js (detail panel: Apply for me)                      Gmail (Phase 1 reader)
        |  REST: session cookie + CSRF. Consent and submit                |
        |  routes refuse bearer tokens (4.6).                             v
        v                                                  application_mail_messages
 api.py  /api/v1/apply-agent/*  ---->  apply/runs.py  <----------------'
                                        |  claims, runs, watch, caps, gate, recovery
                                        |  RUNNER: one run at a time, in a child process
                                        |  with a deadline (4.6)
                                        v
       schema client (injected) -->  apply/policy.py   (pure: schema -> plan -> eligibility)
       Greenhouse Job Board API         |
       (GET, no key; fake in tests)     |       apply/checks.py (pure: outcome, join,
                                        v        required check; stdlib only)
                                   apply_agent.py  (Playwright; modelled on FormSubmitter)
                                        |   GreenhouseAdapter (Python, deterministic)
                                        |   REQUIRED_CHECK_SCRIPT (from apply/checks.py)
                                        |   frame.evaluate(adapters.js + field-engine.js + apply-engine.js)
                                        v
                                   Chromium (headed, fresh context, request guard)

 apps/extension:  sidepanel.js -> content.js (thin, chrome.runtime) -> apply-engine.js (shared, no clicks)
```

New files:

- `apps/extension/apply-engine.js`
- `opportunity_app/apply/checks.py` (stdlib only; created in M3)
- `opportunity_app/apply/policy.py`
- `opportunity_app/apply_agent.py`
- `opportunity_app/apply/runs.py`
- `migrations/0044_apply_agent.sql`
- tests and fixtures (section 12)

Changed files are listed in Appendix B.

### 4.2 The shared engine: `apps/extension/apply-engine.js`

**Why it is needed.** All of the field engine sits in one closure in `content.js`. Its only
public surface is a `chrome.runtime.onMessage` listener (content.js:239-243). Loading it into a
normal page with Playwright would throw at line 239, because `chrome.runtime` is undefined, and
none of its functions are reachable.

**The split.**

- `apply-engine.js` holds everything in content.js:1-237 except the listener:
  - the `SENSITIVE`, `PROHIBITED` and `NEVER_GENERIC_TYPES` rules;
  - `labelFor`, `controlType`, `isVisible`, `sameOriginDocuments`, `fingerprint` and
    `matchAnswer`;
  - `scan`, `resolveControl`, `fillOne`, `fill` and `attachDocument`.

  It exposes them as:

  ```js
  globalThis.OpportunityApplyEngine = Object.freeze({
    version: "1",
    scan,                    // (profile, answers, options?) -> {ats_type, page_url, fields}
    fill,                    // (reviewedFields) -> results      (extension path only)
    attachDocumentFromBytes, // (message) -> result              (extension path only; was attachDocument)
    questionText,            // (control) -> clean question text (4.2 "new outputs")
    questionKey,             // (text) -> normalized key         (same as normalizedQuestion today)
  });
  ```

  It guards against loading twice with a check on `globalThis.OpportunityApplyEngine?.version`.
  It uses no `chrome.*`. Every new output guards against a missing DOM API
  (`getComputedStyle`, `getBoundingClientRect`, `closest`, `getElementById`, a container's
  `querySelector`): when one is absent, as in the node DOM stub (dom_stub.mjs:120-164), the output
  falls back to "unknown" and the existing outputs are unaffected. The stub also gains the ones
  the new tests need.
- `content.js` becomes about 20 lines. It checks that the adapter, field-engine and engine globals
  exist (as content.js:14-16 does today), registers the same listener, and still returns
  `final_submit_available: false`.
- The injection list gains `apply-engine.js` between `field-engine.js` and `content.js`. Update
  it in four places:
  - sidepanel.js:82;
  - tests/extension/browser/run_browser_tests.mjs:109;
  - the loader in tests/extension/dom_stub.mjs:120-164;
  - the `node --check` line in ci.yml:39.

**New scan outputs.** These are added to each field. None of them changes a value the side panel
reads today:

- `question`: the clean question text. It is the `<label for>` text, else the wrapping label's
  text without the control's own text, else the `aria-labelledby` text, else `aria-label`. A
  trailing `*`, `(required)` or `required` is stripped and whitespace is collapsed.
  - **It never includes `name`, `id` or `placeholder`.** This is the fix for saved answers not
    carrying over between postings. `labelFor` (content.js:23-30) joins the per-posting `name`
    and `id` into the label, so on Greenhouse (`question_10215389004`) an exact match never
    carries to another job.
  - `label` itself stays exactly as today, so fingerprints and `resolveControl` behave
    identically in the extension.
- `required_markers`: a list drawn from:
  - `attr` (the `required` attribute);
  - `aria` (`aria-required="true"` on the control or on an ancestor `[role=group]`);
  - `asterisk` (the label contains a `*`, as text or in `span[aria-hidden=true]`);
  - `hidden_required_sibling` (an `input[required][aria-hidden=true]` in the same field
    container, which is how react-select marks it);
  - `span_required` (a `span.required` in the field container, used by upload groups).
- `required_any`: true when any marker is present. **`required` itself is unchanged** (the first
  two markers only, content.js:149), because the side panel reads it (sidepanel.js:188, :251,
  :264) and syncs it into extension session steps. Only the agent reads `required_any`.
- `widget`: `native`, `react_select`, `file_group` or `location`.
- `visible_css`: computed style is not `display:none`, `visibility:hidden` or `opacity:0`, and
  the bounding box is at least 2 by 2 px on screen. This is the same test as the contact-form
  extractor (outreach/forms.py:664-671). It is reported, not used to filter, so the extension's
  field list does not change.
- `name` and `id`, as raw strings, for joining with the Greenhouse schema (6.5).
- With `options.tag === true`, which only the agent passes, each scanned control gets a
  `data-opportunity-field="<key>"` attribute, so Python can find it with a locator. The
  contact-form extractor uses the same pattern (`data-pipeline-field`).

**Saved answers keep matching in the extension.** M1 makes the side panel save `field.question`
instead of `field.label` (sidepanel.js:212). `matchAnswer` (content.js:96-99) is changed in the
same PR to compare a saved question with `questionKey(field.question)` first and with
`normalizedQuestion(field.label)` second, so both new rows and legacy rows keep their exact
match. A node test saves the clean question and asserts an exact (0.9) match.

**Deliberate behavior changes in M1**, each called out in the PR and pinned by a node test:

1. An `<input role="combobox">` (react-select) is now typed `custom_select`. controlType
   (content.js:48-56) only did this for non-input elements, so the extension would type free
   text into a react-select. It now marks the field unsupported, which is the safe direction.
2. The side panel saves the clean question (above).
3. The extension's `SENSITIVE` regex gains the immigration, clearance, 18-or-older, criminal
   history and non-compete terms the agent's classifier adds (7.3 step 6). The extension then also
   stops offering to save answers to those questions in the general library, which is the safe
   direction.

**The no-submit guarantee survives the split.** The static guards move with the code:

- The positive assertions (the source contains `"submit"` and `SENSITIVE`; run_tests.mjs:170,
  test_platform.py:1039-1040) move to `apply-engine.js`, where those rules now live.
- The negative assertions (no `.click(`, `requestSubmit`, `.submit(`, `new MouseEvent`, `new
  PointerEvent`, `dispatchEvent`) apply to **both** `content.js` and `apply-engine.js`.

**Every click the agent makes lives in Python**, in `apply_agent.py`, behind the allowlisted
helpers (4.3). That is what keeps apps/extension/README.md:3-5 and the manifest's "Never submits
forms." true.

### 4.3 `opportunity_app/apply_agent.py`: the browser driver

`ApplyAgent` is modelled on `outreach_forms.FormSubmitter` (outreach/forms.py:784-1155). It is a
context manager used on one thread, and it never raises out of `run()`. The outcome always says
whether anything could have left the page.

```python
@dataclass(frozen=True)
class ApplyTimeouts:
    settle_s: float = 2.0            # after the page settles, before the first input
    between_fields_s: float = 0.25
    choice_settle_s: float = 3.0     # after a react-select choice (in-flight counter, below)
    navigation_s: float = 30.0
    outcome_s: float = 30.0          # 6.14 window
    captcha_s: float = 12.0          # D14 B only
    person_s: float = 5 * 60         # D14 A: the student ticks a CAPTCHA box before hand-over
    handoff_s: float = 20 * 60
    security_code_s: float = 10 * 60

class ApplyAgent:
    def __init__(self, *, mode: Literal["lookup", "rehearse", "submit", "handoff"],
                 adapter: GreenhouseAdapter, screenshot_dir: Path,
                 resolve: Resolver = _resolve_host,
                 timeouts: ApplyTimeouts = ApplyTimeouts(),
                 route_hook: Callable[[Any], None] | None = None,         # tests only
                 student_hook: Callable[[Any, str], None] | None = None,   # tests only: plays the student inside wait loops
                 headless: bool = False,                                   # tests only; production never sets it
                 on_progress: Callable[[str], None] | None = None,
                 heartbeat: Callable[[], None] | None = None) -> None: ...
    def run(self, plan: Plan, *, files: dict[str, FilePayload],
            hand_over: Callable[[], bool] | None = None,
            cancelled: Callable[[], bool] | None = None) -> RunResult: ...
```

- `student_hook(page, step)` is called from inside every wait loop (CAPTCHA box, handoff,
  security code) on the agent's own thread, so a browser test can play the student with the
  sync API. `default_apply_agent_factory` never passes `route_hook`, `student_hook` or
  `headless`; a test asserts it (12.7).
- `heartbeat()` is called at least every 30 s from every wait loop and between fill steps
  (5.2 rule 4).
- `cancelled()` is polled between steps and inside wait loops until hand-over (6.13).

**Launch.** Chromium is bundled with Playwright (no `channel`), as in outreach/forms.py:836-844,
but **headed**: `headless=False`. FormSubmitter defaults to headless (outreach/forms.py:787,
842); the agent does not. The context is created with:

- `service_workers="block"`, `accept_downloads=False`, `permissions=[]` (so the geolocation
  "Locate me" is never granted), and `no_viewport=True` so the window keeps its natural size;
- **no** user agent, locale, timezone or geolocation override, because Greenhouse counts a
  timezone and location mismatch as a fraud signal **[doc]**;
- **no** stealth plugin, no `--disable-blink-features=AutomationControlled`, no CDP patches to
  `navigator.webdriver`.

A test asserts that the launch arguments contain none of these (12.4).

**Playwright is optional.** Import it inside `_start()` only, as FormSubmitter does. If it is
missing, `run()` returns `failed` with the same install sentence FormSubmitter uses:
"Install Playwright and Chromium: python -m playwright install chromium". The agent uses
`context.route_web_socket`, which needs Playwright 1.48 or later, so
`requirements-optional.txt` is raised from `playwright>=1.40` to `playwright>=1.48`
(requirements-ui.txt already pins `>=1.50`).

**Routing.** `context.route("**/*", self._route)` and `context.route_web_socket("**/*", ...)`
handle every request. Hosts:

- *Greenhouse hosts*: any `*.greenhouse.io` host (main-frame navigation is narrower, rule 1).
- *Lookup endpoints*: `GREENHOUSE_LOOKUP_ENDPOINTS`, each an exact host and path prefix that a
  typeahead calls, tied to the field it serves (location, school, degree, discipline). The list is
  confirmed against a live board before M5a and pinned in a fixture, with a note of exactly what
  each endpoint receives (the text typed into its field). Until then it is empty, and those fields
  are left unfilled.
- *Static assets*: GET requests to a Greenhouse host whose Playwright resource type is `image`,
  `font`, `stylesheet`, `script` or `media`.
- *CAPTCHA endpoints*: `CAPTCHA_ENDPOINTS`, each an exact host and path prefix of the reCAPTCHA,
  hCaptcha or Turnstile service (for example `www.google.com/recaptcha/`), pinned in a fixture.

Rules for every mode, in this order:

1. **Main-frame navigations** may go only to `job-boards.greenhouse.io` and
   `boards.greenhouse.io`. Anything else is aborted, and the run ends `needs_you` with "This
   posting sends applicants to {host}". Never `my.greenhouse.io`.
2. **WebSockets are refused**, and each refusal is recorded (host only).
3. **Public addresses only**, through `outreach_render.request_allowed` (outreach/render.py:33-44),
   so loopback is refused. `route_hook` replaces this rule in tests, as FormSubmitter's does.
4. **Value guard, on every host, Greenhouse included.** A request whose URL, headers or body
   contain a planned value of 4 or more characters (raw, URL-encoded or case-folded, checked in
   Python against the values held in memory) is aborted. There are exactly two exceptions: the
   submit POST itself (below), and a GET to the lookup endpoint of the field being typed, which may
   carry that field's own typed text and nothing else. The record names the host and the field
   key, never the value.

Rules by mode and phase:

| Mode and phase | Allowed | Aborted and recorded |
| --- | --- | --- |
| `lookup`, `rehearse`, before the first input | GET, HEAD, OPTIONS. | Every other method, CAPTCHA endpoints included (no exception, unlike outreach/forms.py:772-775). |
| `lookup`, `rehearse`, after the first input | GETs to the lookup endpoint of the field being typed, and static assets. | Every other request, on any host: any other GET (document, XHR, fetch, beacon, other), and every other method. |
| `submit`, `handoff` before hand-over | GET, HEAD, OPTIONS (subject to rule 4). Non-GET only to a CAPTCHA endpoint (subject to rule 4, so its body may carry no planned value). | Every other non-GET, on any host, including every upload. So nothing that could carry the application can leave before hand-over, whatever a page script does (a `requestSubmit`, an Enter key in a typeahead, a lead-capture script). |
| `handoff`: the student's first POST to `submitPath` on `boards.greenhouse.io` | The route handler asks the parent process for the hand-over (5.2 rule 3, 6.13) and calls `route.continue_()` only on a committed True reply. | The POST, on a False reply, an error, or no reply within 10 s. |
| `submit`, `handoff` after hand-over | **One** POST to `submitPath` per attempt (in submit mode, the one the agent's click causes). One more only after a 428 security-code answer, for the code, once per prompt. Non-GET to a CAPTCHA endpoint. GETs (subject to rule 4 until the submit POST has passed). | Every other non-GET: a second submit POST from a double click or a page script, and a POST to any other path or host. Every non-GET that passed is recorded with method, host, path and status (never its body). |

On a board that uploads files as soon as they are attached (`data-allow-s3="true"`), the upload
is itself a non-GET that would have to pass before hand-over. How Greenhouse's page gets the
upload address, and to which exact endpoint the file goes, has not been seen live (the sampled
board had `false`). So `S3_UPLOAD_ENABLED = False`: such a board is rehearsal-only, and submit and
handoff refuse it (6.9). Enabling it later means confirming the flow live and allowing exactly the
address Greenhouse's own response names, with a body that contains the planned file's bytes.

Why rehearsal blocks CAPTCHA traffic: Greenhouse's reCAPTCHA is invisible and runs only on
submit, so a rehearsal never needs it, and submit runs happen on a fresh page (6.12), so blocking
it in a rehearsal cannot affect a real submission's score.

What this lets the preview claim, and nothing more, is in 10.4.

**Injection.** Use `frame.evaluate(ENGINE_SOURCE)`, not `page.add_script_tag`.

- `ENGINE_SOURCE` is the concatenated text of `adapters.js`, `field-engine.js` and
  `apply-engine.js`, read once from `apps/extension/` at import time.
- `evaluate` is not subject to the page's Content Security Policy. The contact-form submitter
  relies on the same property for `EXTRACT_SCRIPT` (outreach/forms.py:1033-1045). A script tag
  can be blocked by CSP.
- The engine runs in the page's own JavaScript world, so the page can see it and could tamper
  with it. The engine's output is therefore **advisory**. Every value the agent relies on is read
  back through Playwright's own reads (`input_value()`, `is_checked()`, element text) in Python
  (6.8, 6.10).

**Mutations use Playwright's input methods, through five helpers.** Every change the agent makes
to the page goes through one of:

- `_type(locator, value, key)`: `locator.fill()`, which produces trusted input events;
- `_tick(locator, key)`: `locator.set_checked()` for a planned checkbox or radio;
- `_choose(locator, label, key)`: `select_option(label=...)` for a planned native select, or the
  react-select sequence below;
- `_attach(locator, payload, key)`: `set_input_files(FilePayload)` (6.9);
- and `_click(locator, purpose)`, below.

For react-select comboboxes, `_choose` calls `_click(..., "select_open")` on the control,
`_type`s the search text, then `_click(..., "select_option")` on the one option whose text equals
the planned label. The engine's `fill` and `attachDocumentFromBytes` stay the extension's path.

Whether untrusted script events lower a reCAPTCHA score is unknown (research section 4).
Trusted events avoid the question.

**Clicks are allowlisted.** `_click(locator, purpose)` accepts only:

- `select_open` or `select_option`, inside a planned react-select's own field container;
- `submit`, the adapter's submit control, in submit mode only, after `hand_over()` returned True;
- `captcha_checkbox`, **only if the student chose D14 B**, in submit mode only, and only inside a
  frame matching `CAPTCHA_WIDGETS` (outreach/forms.py:776-781).

Any other purpose raises. The adapter also keeps a denylist of controls that are never clicked,
even by accident; a test pins it:

- "Autofill my application" (MyGreenhouse) and "Apply with SEEK";
- any "Apply with LinkedIn";
- "Locate me";
- the Dropbox and Google Drive attach buttons, and "Enter manually".

A static test scans `apply_agent.py`, including its JavaScript string constants, for every other
way to cause a click or a submission (12.7).

**Screenshots.** Taken with `page.screenshot(full_page=True, mask=[...])`. For each field D8 says
to mask, the mask is the field's whole container (label, control, the separate
`.select__single-value` or multi-value display, and error text), not just the input. Files go to
the run's directory (section 11), and the SHA-256 is computed on write. As in FormSubmitter, a
failed screenshot never changes the outcome.

**Pacing.**

- Wait `settle_s` (2 s) after the page settles before the first input.
- Wait `between_fields_s` (250 ms) between fields.
- After each react-select choice, wait until no request is in flight, or `choice_settle_s`
  (3 s), whichever comes first. The in-flight count is kept from the context's `request`,
  `requestfinished` and `requestfailed` events. (`wait_for_load_state("networkidle")` cannot do
  this: it resolves at once on a page that already reached that state, and a page polling
  analytics may never reach it.)
- There are **no randomized delays, simulated mouse paths or character-by-character typing meant
  to look human.** That would be disguise. The fixed waits exist so the page's own scripts can
  settle.

### 4.4 The Greenhouse adapter (Python, in `apply_agent.py`)

Deterministic code only. Page text never chooses an action (docs/THREAT_MODEL.md:10).

| Function | What it does |
| --- | --- |
| `identify(conn, opportunity_id)` | Returns `(board_token, job_id)` or None. See the rules below. |
| `canonical_url(token, job_id)` | `https://job-boards.greenhouse.io/{token}/jobs/{job_id}` |
| `schema_url(token, job_id)` | `https://boards-api.greenhouse.io/v1/boards/{token}/jobs/{job_id}?questions=true`. Public and keyless **[doc, live]**. |
| `detect_page(page)` | One of `application_form_new`, `application_form_legacy`, `confirmation`, `closed`, `offsite`, `unknown` (6.3). |
| `loader_paths(html)` | Reads `"submitPath":"…"` and `"confirmationPath":"…"` from the served HTML **[live]**. The submit path is on `boards.greenhouse.io`, not the job-boards host. Submit and handoff runs refuse to start filling without a `submitPath` (6.2). |
| `submit_control(frame)` | New board: the single `form#application-form button[type=submit]` whose text, normalized, is exactly "submit application", and which is not `disabled` or `aria-disabled="true"`. Legacy: the single `#application_form #submit_app`. Zero or more than one match means `needs_you`. |
| `fill_react_select(frame, key, option_text)` | Opens the control, types the text, and clicks the option whose visible text equals `option_text` after normalization. **Never the first option.** It reads back `.select__single-value` (or the multi-value labels). |
| `read_options(frame, key)` | Opens a react-select or reads a native select and returns its option labels without choosing one (6.7 deferred fields, 5.5 lookups). |
| `fill_location(frame, key, label)` | Pelias geocoder. It types the city part of the stored exact label, then picks the option whose text equals the whole label. Picking the top result chooses the wrong place **[1-src]**. |
| `security_code_prompt(frame, responses)` | True when `#security-input-0` is visible, or when the submit POST answered 428 with `captcha-failed` **[1-src, two projects]**. |

`identify` works like this:

1. It parses `opportunities.url`, then the source URL, for either Greenhouse host with
   `/{token}/jobs/{digits}`, or `/embed/job_app?for={token}&token={digits}`. A token parsed from a
   URL wins.
2. Otherwise it looks for an `opportunity_sources` row whose `source_key` matches the pattern
   `'greenhouse:%'`, **passed as a query parameter**, as pipeline.py:1928 and core/schema.py:770 do.
   (A literal `%` in the SQL breaks on PostgreSQL, where `PostgresConnection` turns `?` into
   `%s`.) The token is the part after `greenhouse:`, and the job id is `external_id`
   (pipeline.py:827-857). The source key is `kind:(id or token)` (pipeline.py:2127-2139), so a
   `sources.local.json` entry with its own `id` gives a key that is not the board token; the
   schema fetch in 6.0 then answers 404 and the result is "The app couldn't find this posting on
   Greenhouse".
3. The job id must match `^\d+$`. Anything else (the test fixture's `a-1`) is not identified.
4. A company-site URL carrying only `gh_jid` is not supported in v1 (13 R7).

The adapter carries `ADAPTER_VERSION = "greenhouse-1"`. Any change to its selectors or rules bumps
it. The rehearsal gate (9.2) counts only rehearsals made with the current version.

**The legacy form** is `form#application_form`, with `job_application[...]` names, an
`.asterisk` span as the required marker, and a `#submit_app` button. It is supported behind
`LEGACY_ENABLED = False` until someone checks it against a live legacy page.

- `boards.greenhouse.io` URLs now answer 301 to the new host **[live]**, so no live legacy page
  was available on 2026-09-28.
- Until then, a legacy page is detected and returns `needs_you` with "This is Greenhouse's older
  form, which the app does not fill yet".

### 4.5 `opportunity_app/apply/policy.py`: plan and eligibility (pure)

No browser and no network. Every function takes plain data and returns plain data, so the truth
table in 7.5 is tested without Playwright.

- `parse_schema(json) -> list[SchemaField]` covers `questions`, `compliance`,
  `demographic_questions`, `data_compliance`, `location_questions` and education settings. Each
  field has `name`, `label`, `required`, `type` (`input_text`, `input_file`, `textarea`,
  `input_hidden`, `multi_value_single_select`, `multi_value_multi_select`), the option labels, and
  `section` (`standard`, `custom`, `compliance`, `demographic`, `data_compliance`, `location`,
  `education`).
- `question_key(text) -> str`: the same normalization as the engine's `questionKey`; parity is
  tested with a shared vector file (12.3).
- `sources_for(conn, user_id, opportunity_id) -> Sources` gathers everything a value may come
  from:
  - confirmed facts, via `preparation.confirmed_facts` (student/preparation.py:33-40), including the new
    `name_parts` field (7.1). This is not `extension_apply._confirmed_profile`, which skips the
    `is_answered` filter;
  - **all** saved answers, with their company and tags, not the 200 most recent that
    `_safe_answers` reads (applications/extension.py:301-320);
  - the student's ATS option labels (5.5);
  - sensitive answers (5.4), only when the student chose D5 B to E;
  - the résumé to use (`resume_for`, 6.9);
  - the cover letter to use (6.9).
- `build_plan(schema, scan, sources, company, mode) -> Plan` returns `Plan(fields:
  list[PlanField], problems: list[Problem], plan_hash: str)` (6.6). `mode` decides what happens to
  a field that cannot be filled (6.6).
- `classify_sensitive(question, options, section, field_name) -> str | None` returns a category,
  the explicit value `"uncategorized"` (sensitive, never storable), or None (not sensitive)
  (7.3).
- `plan_hash(plan, canonical_url)` (6.6).

### 4.6 `opportunity_app/apply/runs.py`: claims, runs, watch, and the service layer

This module owns all database writes:

- starting a run;
- claim insert, heartbeat and settle;
- the hand-over to `clicking`;
- recording the outcome and the stage change;
- releasing an attempt;
- the verification watch;
- per-ATS statistics;
- the cap, rehearsal-count and gate checks;
- crash recovery and evidence retention.

It is the only caller of `ApplyAgent`.

**Runs happen off the request thread, in a child process with a deadline.** A run takes one to
three minutes, and a hung page must never hold the app hostage.

- `apply_runs.RUNNER` is a process-wide single-slot runner. Only one agent run happens at a time
  per server process. A second start while one is running returns HTTP 409 with "Another
  application is being filled. Wait for it to finish."
- In production, the runner starts each run in a child process (multiprocessing, `spawn`), which
  owns Playwright and Chromium. The parent keeps every database write; the child sends progress,
  heartbeats and the `RunResult` over a pipe. The agent factory is a picklable module-level
  callable, so tests can pass a fake one that builds `FakeGreenhouse` inside the child.
- **The process boundary for the three callbacks.** In the child, `hand_over()`, `heartbeat()`
  and `cancelled()` are proxies over the pipe:
  - `hand_over()` sends `{"op": "hand_over", "token": ...}` and blocks for the reply. The parent
    runs the hand-over transaction (5.2 rule 3), **commits**, and only then replies `True` or
    `False`. The child treats anything but an explicit `True` (a `False`, an error, a closed pipe,
    or no reply within 10 s) as False. In handoff mode this call is made from inside the route
    handler, before `route.continue_()` (6.13), so the student's POST waits for the commit.
  - `heartbeat()` sends a one-way message; the parent writes `heartbeat_at`.
  - `cancelled()` reads the latest cancel flag the parent pushed down the pipe.
  - If the parent's heartbeat writes stop (the parent died), the child's next `hand_over()` gets
    no reply and aborts; before hand-over nothing has left (4.3).
- Each kind has a deadline, with the declared waits for the student added explicitly: lookup
  2 min; rehearsal 5 min; submit 5 min plus the CAPTCHA and security-code waits; handoff
  `handoff_s` plus `security_code_s` plus 2 min. A watchdog in the parent terminates the child's
  whole process tree at the deadline (`taskkill /T /F` on Windows; a new session and `os.killpg`
  on POSIX), then finishes the run: `failed` before hand-over, `unconfirmed` after it. This is the
  only way to stop a page script that never yields: `frame.evaluate` has no timeout, and
  Playwright's sync objects cannot be closed from another thread.
- The run writes step-by-step progress to `apply_runs.progress_json` and refreshes
  `heartbeat_at`, and the UI polls it.
- The runner records an `apply_agent.runner` health component (busy since, last outcome), as
  AutomationWorker and InboxWatcher already do.

**Factories are wired only for the real product database**, like the contact-form submitter
(api.py:1063-1064):

- `create_app(..., apply_agent_factory=None, apply_schema_client_factory=None)`. Each defaults to
  its real implementation only when `real_product_db`; otherwise it stays None.
- Without an agent factory, the start routes return 503 with "Apply for me runs only in your own
  app, with Playwright installed."
- Without a schema client factory, the check route returns 503 with the same sentence, at once.
- The sandbox may opt in to a **fake** agent and a **fake** schema client together (12.6).
- `apply_runs.setup_requirement` asks the agent factory's `available()` probe whether Playwright
  and Chromium are present, never `importlib.util.find_spec`. A fake factory answers
  deterministically, so the same tests pass with or without Playwright installed.

**Routes that record consent or confirmation need the student's browser session.** A dependency
`require_browser_session` accepts only a cookie-authenticated session, always checks the
`X-CSRF-Token` header against the session's token (whether or not an `Origin` header is
present; the general middleware checks only when it is, api.py:1301-1308), and answers 403 to
any request carrying an `Authorization` header. The owner's bearer token lives in `.env` and is
used by local tooling and scheduled agents, which must not be able to act for the student. It
guards:

- starting a submit or a Finish in browser run, including the company override and the duplicate
  ticks;
- Cancel;
- rehearsal review marks (they open the gate);
- "It went through" / "It didn't go through" and "Mark as applied";
- creating, editing or deleting a sensitive answer, and changing `apply_sensitive_categories` or
  `apply_eeo_store_values`.

A submit also needs a **single-use confirm nonce**, issued only with the plan preview, bound to
that rehearsal's `plan_hash`, and consumed in the claim transaction (5.3).

### 4.7 `opportunity_app/apply/checks.py`: the decisions, as pure functions

Standard library only, created in M3 so the default unittest suite (which has no Playwright)
covers every integrity-critical decision. Browser tests then only prove that the observations are
gathered correctly.

- `REQUIRED_CHECK_SCRIPT`: the independent JavaScript re-scan (6.10), as a string constant.
- `decide_outcome(obs: Observation) -> Outcome`: the 6.14 table. `Observation` holds the main
  frame's path, whether the form is present, every non-GET request seen after hand-over (method,
  host, path, status or None), whether security-code inputs are visible, whether a challenge
  frame is present, the loader paths, and whether any main-frame navigation happened.
- `route_decision(mode, phase, request, state) -> Allow | Abort(reason)`: the whole routing policy
  of 4.3 (hosts, endpoints, the value guard and its two exceptions, the one-submit-POST rule and
  the post-428 allowance, `S3_UPLOAD_ENABLED`). The agent's route handler only gathers the request
  facts and applies the answer.
- `join(schema_fields, scan_fields) -> list[Problem]` (6.5).
- `check_required(items, plan, schema, initial_values) -> list[Problem]` (6.10).
- `clean_rehearsal(run) -> bool` (9.2).

### 4.8 What stays true

- `pipeline.py` and `pipeline_core/` gain nothing (README.md, "What it never does"; AGENTS.md hard rule 4). All new code
  is in `opportunity_app/`. Playwright stays optional (requirements-optional.txt), and
  `tests/test_dependency_boundary.py` is unchanged.
- The extension keeps its no-submit guarantee and `final_submit_available: false`
  (content.js:241-242, applications/extension.py:518, api.py:3984).
- `extension_apply.apply_context` keeps excluding sensitive answers. A new test asserts it never
  returns an `apply_sensitive_answers` entry (12.7).
- Loopback only. The server binds 127.0.0.1 as before. The agent's browser can reach only public
  addresses, through `request_allowed`.
- Tests, the sandbox and the fuzzer never reach Greenhouse. A test runs the whole preflight and
  API suite under an httpx transport that raises on any request (12.6).

---

## 5. Data model and migration

### 5.1 Migration number and shape

On 2026-09-29 origin/main ends at `0042_outreach_tech_brief.sql`, and Gmail reply-label work in
flight in another worktree has taken `0043`. This phase therefore uses
**`migrations/0044_apply_agent.sql`**. Whichever lands second renumbers to the next free number.
Because D5 is C, the migration creates the `apply_sensitive_answers` table (5.4) too; M4s adds
the code that uses it.

The migration:

- creates four tables and their indexes (`CREATE TABLE IF NOT EXISTS`);
- adds three columns to existing tables, through a guarded Python step
  `schema._apply_apply_agent` registered in `_MIGRATION_STEPS` (core/schema.py:387-396), each column
  added only if missing, as 0038 does, so a crash before the migration is marked cannot make the
  next start fail on a duplicate column:
  - `connector_accounts.account_email TEXT NOT NULL DEFAULT ''`: the Gmail address, recorded from
    Gmail's `/profile` at OAuth connect and reconnect (D12). connector_accounts stores no address
    today (migrations/0001:362-374); the only sources are the optional
    `PIPELINE_OUTREACH_ACCOUNT` and a live `/profile` call (outreach/gmail.py:612-617), which a
    settings render cannot make;
  - `application_mail_messages.sender_verified INTEGER NOT NULL DEFAULT 0`: 1 when
    `mail_trust.authenticate` (mail/trust.py:239) vouched for the sender. The Phase 1 reader sets
    it when it records the row. The watch trusts only verified rows (6.16);
  - `generated_document_artifacts.content_sha256 TEXT NOT NULL DEFAULT ''`: the SHA-256 of the
    approved text the PDF was rendered from, so a stale PDF is never attached (6.9).

The SQL must run on both SQLite and PostgreSQL: TEXT and INTEGER columns only, no AUTOINCREMENT,
ISO text timestamps as elsewhere. The two partial unique indexes in 5.2 (`... WHERE state <>
'released'`) are supported by SQLite 3.8 and later and by PostgreSQL. `tests/test_postgres.py`
gains cases (12.3).

### 5.2 `application_submit_claims`: one row per attempt, with two locks

One row per submit or Finish in browser attempt. Rows are never deleted (except by account
deletion): an attempt that is abandoned becomes a `released` tombstone, so the limits and the
duplicate checks can still see it. Two partial unique indexes are the locks, in the spirit of
`outreach_send_claims` (migrations/0029): at most one live attempt per application, and at most
one live attempt per Greenhouse job.

```sql
CREATE TABLE IF NOT EXISTS application_submit_claims (
    token TEXT PRIMARY KEY,           -- this attempt; conditional updates name it
    application_id TEXT NOT NULL REFERENCES applications(id) ON DELETE CASCADE,
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    opportunity_id TEXT NOT NULL,
    instance TEXT NOT NULL,           -- opportunity_app.SERVER_INSTANCE of the holder
    mode TEXT NOT NULL CHECK (mode IN ('one_click', 'handoff', 'unattended')),
    state TEXT NOT NULL CHECK (state IN
        ('claimed', 'clicking', 'submitted', 'unconfirmed', 'needs_you', 'failed', 'released')),
    after_click INTEGER NOT NULL DEFAULT 0 CHECK (after_click IN (0, 1)),
    ats TEXT NOT NULL,                -- 'greenhouse'
    board_token TEXT NOT NULL,
    job_ref TEXT NOT NULL,            -- '{board_token}/{job_id}'
    company_key TEXT NOT NULL,        -- pipeline.identity_tokens(company), sorted, space-joined
    stage_policy TEXT NOT NULL CHECK (stage_policy IN ('record', 'ask', 'ledger')),
    plan_hash TEXT NOT NULL,          -- the plan the student confirmed (one_click) or the agent's fill (handoff)
    run_id TEXT NOT NULL DEFAULT '',  -- the apply_runs row of this attempt
    confirmed_at TEXT,                -- one_click: the student's confirm; NULL otherwise
    handed_over_at TEXT,              -- set in the hand-over transaction; never cleared
    heartbeat_at TEXT NOT NULL,
    cancel_requested INTEGER NOT NULL DEFAULT 0 CHECK (cancel_requested IN (0, 1)),
    verification TEXT NOT NULL DEFAULT '' CHECK (verification IN
        ('', 'awaiting_email', 'email_confirmed', 'no_email_24h', 'not_watched')),
    watch_until TEXT,                 -- submitted_at + 24 h, extended while the mail reader is stalled
    submitted_at TEXT,                -- when the confirmation page was seen, or the student or email said so
    verified_at TEXT,                 -- when verification last changed
    stage_recorded INTEGER NOT NULL DEFAULT 0 CHECK (stage_recorded IN (0, 1)),
    resolved_by TEXT NOT NULL DEFAULT '' CHECK (resolved_by IN ('', 'page', 'email', 'student')),
    note TEXT NOT NULL DEFAULT '',    -- plain sentence for the card; never a field value
    detail_json TEXT NOT NULL DEFAULT '{}',  -- overrides, waiting sub-state, possible_email_at, ...
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_submit_claims_live_application
    ON application_submit_claims(application_id) WHERE state <> 'released';
CREATE UNIQUE INDEX IF NOT EXISTS ux_submit_claims_live_job
    ON application_submit_claims(user_id, ats, job_ref) WHERE state <> 'released';
CREATE INDEX IF NOT EXISTS idx_submit_claims_user_state ON application_submit_claims(user_id, state);
CREATE INDEX IF NOT EXISTS idx_submit_claims_handed_over ON application_submit_claims(user_id, handed_over_at);
CREATE INDEX IF NOT EXISTS idx_submit_claims_company ON application_submit_claims(user_id, company_key, handed_over_at);
CREATE INDEX IF NOT EXISTS idx_submit_claims_board ON application_submit_claims(user_id, ats, board_token, handed_over_at);
CREATE INDEX IF NOT EXISTS idx_submit_claims_verification ON application_submit_claims(user_id, verification, watch_until);
```

A `submitted` row keeps both locks for good: the same Greenhouse job can never be claimed again,
from this application or from a second saved copy of the same posting.

**The state machine.** `state` describes the click. `verification` describes the email
afterwards. They are kept separate, as PLAN.md:1213-1215 requires.

```
            start (submit or handoff run)
                    |
                 claimed ---------------------------> failed / needs_you, after_click=0
                    |                                  (before hand-over; nothing could have left)
   hand-over txn    |                                        |
   (rule 3)         v                                        '-- student retries --> released
                 clicking --(POST seen + confirmation page)--> submitted --> verification (6.16)
                    |
                    |--(anything sent, but no confirmation / crash / timeout)--> unconfirmed
                    |--(challenge after click; security code not completed)---> needs_you, after_click=1
                    '--(POST answered 4xx other than 428)---------------------> failed, after_click=1

   unconfirmed, and needs_you / failed with after_click=1:
        confirmation email (strong match) --> submitted, resolved_by='email'
        "It went through"                 --> submitted, resolved_by='student'
        "It didn't go through"            --> released (tombstone; still counted by the limits)
```

**Rules:**

0. **Lock order.** Every transaction that writes this table starts with
   `UPDATE users SET id=id WHERE id=?` for the student. On SQLite that takes the database write
   lock; on PostgreSQL it locks the student's `users` row. Either way, claim inserts, hand-overs,
   re-checks and resolutions for one student are serialized across threads **and** server
   processes. (The single-slot RUNNER only covers one process; a stale server that survived a
   restart is a second one, memory note restart-web-dashboard.) Reads of `applications` in these
   transactions use the codebase's `_for_update` helper.
1. **Claim before the browser.** In one transaction: the lock; the application row if missing
   (6.1); the insert (`state='claimed'`, a new `token`, `instance=SERVER_INSTANCE`,
   `heartbeat_at=now`, `stage_policy` from 8 below); then the re-checks (6.1). Then it commits. A
   unique-index conflict stops the run with a sentence naming the lock:
   - `ux_submit_claims_live_application`: "This application is already being submitted, or was
     submitted.";
   - `ux_submit_claims_live_job`: "This Greenhouse job already has an attempt from another saved
     copy of the role ({title}). Finish or release that one first."
2. **Retrying after a stop** happens only on the student's explicit retry, never by the worker.
   The retry's claim transaction first releases every stopped attempt for this application or
   this job that sent nothing:
   `UPDATE application_submit_claims SET state='released', resolved_by='student', updated_at=?
   WHERE user_id=? AND (application_id=? OR (ats=? AND job_ref=?)) AND after_click=0 AND state IN
   ('failed','needs_you')`, then inserts the new row. This is the `stale_token` pattern in
   outreach/gmail.py:720-753, without the delete. An attempt with `after_click=1` is never
   released this way (rule 6).
3. **Hand-over** happens just before the click (submit), or inside the route handler that sees
   the student's POST (handoff). It is one transaction:
   - the lock (rule 0), then `pause_guard` (automation/ledger.py:481-496);
   - it returns False when:
     - `cancel_requested=1`;
     - `unattended`, and automation is paused;
     - `one_click`, and automation is paused with the pause setting's `updated_at` later than
       `confirmed_at` (D6 B: a newer pause wins; a pause already on at the confirm does not);
     - `one_click`, and the confirmed rehearsal is 15 minutes old or more (the one clock, 6.0
       step 8);
   - then `UPDATE application_submit_claims SET state='clicking', after_click=1,
     handed_over_at=?, heartbeat_at=?, updated_at=? WHERE token=? AND state='claimed'`;
   - it returns True only if exactly one row changed. It is the `hand_over` pattern at
     outreach/forms.py:1299-1314.
   - `handoff` is not refused by a pause: the student's own press of Submit in the window is the
     confirm (D6 B). Under D6 A it is refused like the others.
4. **Heartbeat.** At least every 30 s, from every wait loop (fill, CAPTCHA box, handoff, security
   code, outcome): `UPDATE ... SET heartbeat_at=? WHERE token=? AND state IN
   ('claimed','clicking')`. The claim stays in `clicking` for the whole security-code wait, with
   `detail.waiting='security_code'`.
5. **Settle** with `WHERE token=? AND state IN ('claimed','clicking')`, so an old attempt can never
   overwrite a newer one. A settle to `submitted` after the confirmation page was seen may also
   move the row from `unconfirmed` (the page is stronger evidence than a crash recovery). If a
   settle after a seen confirmation page matches no row (the student released it meanwhile), the
   agent still writes the `apply_agent_submitted` event and a notice: "Greenhouse showed its
   confirmation page for {company}, after this attempt was marked as not sent. Check it." It never
   raises, like `outreach_gmail.settle_send_claim`.
6. **Uncertain attempts are never retried automatically.** That is `unconfirmed`, and
   `needs_you` or `failed` with `after_click=1`. Only the confirmation email (6.16, then
   `submitted` with verification `email_confirmed` and `resolved_by='email'`) or the student can
   resolve one:
   - "It went through" records `submitted`, `resolved_by='student'`, `submitted_at=now`;
   - "It didn't go through" records `released`, `resolved_by='student'`. The row stays as a
     tombstone: `handed_over_at` keeps counting toward the limits (9.1), the duplicate check asks
     before the same job is tried again (6.0 step 4), and the watch can still find its
     confirmation email for 14 days.
   - Both answers are refused with 409 while the claim is held (rule 7).
7. **Crash recovery** runs `apply_runs.recover_stale(conn, now)` at server start and on every
   AutomationWorker pass, for every student with a non-terminal claim or a running run (5.6). A
   claim is **held** while its token is in this process's running set, or, for another instance,
   while its `heartbeat_at` is less than 2 minutes old. A claim that is not held:
   - `claimed` becomes `failed`, `after_click=0`, "The app stopped before handing your
     application to Greenhouse. Nothing was sent." That is true because the routing rules (4.3)
     abort every request that could carry the application before hand-over;
   - `clicking` becomes `unconfirmed`, "The app stopped while submitting. Check whether it
     arrived";
   - `submitted` with `stage_recorded=0` gets its stage write retried (6.15), **only** when
     `stage_policy` is `record` or `ledger`.
   - Orphaned runs: an `apply_runs` row still `running` whose id is not in this process's running
     set and whose `heartbeat_at` is more than 2 minutes old is finished as `failed`, "The app
     stopped during this run", with a notice.
8. **`stage_policy`** is set once, at claim time, from the mode and the student's D1 answer, so no
   later code or policy change can reinterpret an old claim:
   - `record`: one_click, and handoff under D1 A. The stage moves to applied when the
     confirmation page is seen (6.15).
   - `ask`: handoff under D1 B. The stage never moves by itself; the card asks.
   - `ledger`: unattended (M8). The stage moves through `automation.perform`.

### 5.3 `apply_runs`: every lookup, rehearsal, submit and handoff

```sql
CREATE TABLE IF NOT EXISTS apply_runs (
    id TEXT PRIMARY KEY,                       -- 'run-<uuid hex>'
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    opportunity_id TEXT NOT NULL,
    application_id TEXT REFERENCES applications(id) ON DELETE CASCADE,  -- NULL for lookup and rehearsal
    claim_token TEXT NOT NULL DEFAULT '',      -- submit and handoff only
    kind TEXT NOT NULL CHECK (kind IN ('lookup', 'rehearsal', 'submit', 'handoff')),
    started_by TEXT NOT NULL CHECK (started_by IN ('student', 'worker')),
    ats TEXT NOT NULL,
    adapter_version TEXT NOT NULL,
    company_key TEXT NOT NULL,
    board_token TEXT NOT NULL,
    page_url TEXT NOT NULL,                    -- canonical URL
    status TEXT NOT NULL CHECK (status IN ('running', 'finished')),
    outcome TEXT NOT NULL DEFAULT '' CHECK (outcome IN
        ('', 'looked_up', 'rehearsed', 'submitted', 'unconfirmed', 'needs_you', 'failed')),
    clean INTEGER NOT NULL DEFAULT 0 CHECK (clean IN (0, 1)),  -- apply_checks.clean_rehearsal, set at finish
    reasons_json TEXT NOT NULL DEFAULT '[]',   -- plain sentences; never a field value
    plan_json TEXT NOT NULL DEFAULT '[]',      -- value-free plan (below)
    plan_hash TEXT NOT NULL DEFAULT '',
    options_json TEXT NOT NULL DEFAULT '{}',   -- lookup only: {field: [option labels]} (public page text)
    progress_json TEXT NOT NULL DEFAULT '[]',  -- [{at, step, text}] for the live view
    screenshots_json TEXT NOT NULL DEFAULT '[]', -- [{step, path, sha256, masked: [...keys]}]; path cleared by retention
    refused_json TEXT NOT NULL DEFAULT '[]',   -- [{method, host, rule, field_key?}]; never a URL query or body
    requests_json TEXT NOT NULL DEFAULT '[]',  -- after hand-over: non-GET method, host, path, status
    evidence_json TEXT NOT NULL DEFAULT '{}',  -- submit POST seen and status, confirmation path, form absent, screenshot sha256
    confirm_nonce_sha256 TEXT NOT NULL DEFAULT '',  -- rehearsal only: the one-click confirm nonce (4.6)
    nonce_used_at TEXT,
    review TEXT NOT NULL DEFAULT '' CHECK (review IN ('', 'right', 'wrong')),
    review_note TEXT NOT NULL DEFAULT '',
    reviewed_at TEXT,
    heartbeat_at TEXT NOT NULL,
    deadline_at TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_apply_runs_opportunity ON apply_runs(user_id, opportunity_id, started_at DESC);
CREATE INDEX IF NOT EXISTS idx_apply_runs_ats ON apply_runs(user_id, ats, kind, started_at DESC);
CREATE INDEX IF NOT EXISTS idx_apply_runs_status ON apply_runs(status, heartbeat_at);
```

**The browserless check writes nothing.** Opening the Apply for me section runs the preflight
(6.0) read-only and returns its result; it creates no application and no run row. Lookups and
rehearsals have no application either (G9); they are keyed by `opportunity_id`.

**The plan is stored without values.** Each `plan_json` entry holds:

- `key`: the schema field name;
- `question` (the schema label), `control`, `required`, the option labels, and for statements the
  statement text and its links (all public page text);
- `sensitive` with its category;
- `disposition`: `fill`, `deferred`, `left_for_you` or `blank` (6.6);
- `source`, which is `{kind, ref, company, reusable}`:
  - `kind` is one of `profile`, `ats_label`, `answer`, `sensitive`, `resume`, `cover_letter`,
    `none`;
  - `ref` is a fact path, an answer id, a sensitive-answer id, a résumé version id, or a document
    id and version;
- `value_mac`, and `file_sha256` for files;
- `problem`, a sentence or "".

`value_mac` is HMAC-SHA256 of the value with a per-install key, not a plain SHA-256: a plain hash
of "Yes", an EEO option or a phone number can be reversed with a short dictionary, and these
records are kept for good and exported. The key is 32 random bytes in
`data/private/apply/hash-key`, created on first use; it is never exported, logged or sent
anywhere. If it is lost, old runs can no longer be compared, which only means "rehearse again".

The preview in the UI looks the values up again from `ref` when it is shown. If a value's MAC no
longer matches, the preview says "this answer changed since the rehearsal" and the plan must be
rehearsed again. The same design choice keeps the extension's session records value-free
(applications/extension.py:34-46).

**"Rehearsal records"** are `apply_runs` rows with `kind='rehearsal'`. `kind='lookup'` is the
option lookup for a typeahead (5.5).

### 5.4 `apply_sensitive_answers`: the consent store (only if D5 is B to E)

Built in M4s, and only if the student chooses D5 B, C, D or E. Under D5 A there is no store.

```sql
CREATE TABLE IF NOT EXISTS apply_sensitive_answers (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    category TEXT NOT NULL CHECK (category IN ('work_authorization', 'sponsorship', 'age_18',
        'export_control', 'eeo_gender', 'eeo_hispanic', 'eeo_race', 'eeo_veteran', 'eeo_disability',
        'acknowledgment', 'consent', 'salary')),
    question_text TEXT NOT NULL,     -- exactly as the form showed it
    question_key TEXT NOT NULL,      -- apply_policy.question_key(question_text); display and matching
    question_hash TEXT NOT NULL,     -- SHA-256 of question_key; the unique index uses this
    answer_kind TEXT NOT NULL CHECK (answer_kind IN ('option', 'options', 'text', 'checkbox')),
    answer TEXT NOT NULL,            -- option label(s) joined by '\n', text, or 'checked'
    company_key TEXT NOT NULL DEFAULT '',  -- '' = any company (not allowed for statements that cite a document)
    statement_links_json TEXT NOT NULL DEFAULT '[]',  -- acknowledgment: the documents the statement links to
    consent_scope TEXT NOT NULL CHECK (consent_scope IN ('confirmed', 'unattended')),
    consented_at TEXT NOT NULL,
    last_used_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(user_id, question_hash, company_key)
);
```

The unique key uses `question_hash` because for acknowledgments and consents `question_key` is the
whole statement, and PostgreSQL refuses btree index rows over about 2,704 bytes; privacy
statements are often longer.

**How entries get in and are used:**

- **Written in one place only:** the Apply agent settings page, or the Needs you flow of a run,
  which opens the same form. Both use `require_browser_session` (4.6).
- **Consent.** The form requires an unticked-by-default checkbox: "Use this answer only to fill
  application forms that the app submits after I confirm each one." Its tick time becomes
  `consented_at`, with `consent_scope='confirmed'`. Unattended mode (M8) would use only entries
  consented again with scope `unattended`.
- **Accepted categories:** the API refuses any category outside the set the student chose in D5.
  That choice is stored per student in `user_settings` under `apply_sensitive_categories`, as a
  comma-separated list, empty by default.
- **EEO values.** Unless `user_settings.apply_eeo_store_values` is `on` (D5 C sub-choice (ii)),
  the API accepts for `eeo_*` only an answer that is a decline option ("Decline to
  self-identify", "I don't wish to answer" and the like, matched from a fixed list).
- **Statements that cite a document.** An `acknowledgment` whose statement says "I have read" or
  "I acknowledge receipt", or that links to a document, must carry a `company_key`; the API
  refuses `''`. Its links are stored and shown in the plan preview (D9 B).
- **Read only by `apply_policy.sources_for`, server-side.** `extension_apply.apply_context`,
  `/api/v1/extension/*`, `/api/v1/preparation/answers`, `accounts/employer.py` and every report or
  aggregate never read it. A source test enforces that only `apply/policy.py`, `apply/runs.py`,
  `accounts/operations.py` (export and deletion) and the settings routes name the table (12.7).
- **Matching is exact:** `question_key` equality, and `company_key` either '' or equal to this
  company. For `acknowledgment` and `consent`, the key is the full statement text, so any change
  in wording is a miss.
- **Deleting an entry** removes it at once. A plan that used it fails the plan-hash check at
  submit.

`salary` is in the CHECK so the table does not need a migration if the student ever picks D5
option E. The API refuses it unless that option is chosen. Questions the classifier returns as
`uncategorized` (age, birth, pronouns, religion, criminal history and so on) have no category, so
they can never be stored.

**As built in M4s (2026-09-30), where it differs from the text above:**

- The service is `opportunity_app/apply/sensitive.py` (`add_entry`, `delete_entry`, `list_entries`, `lookup`,
  `allowed_categories`, `set_allowed_categories`). It is the only file that names the table besides
  `accounts/operations.py` (export and deletion); `apply_policy.stored_sensitive_answer` is now a one-line call to
  `lookup`. The 12.7 allowlist reads: `apply/sensitive.py` and `accounts/operations.py`, plus a check that only
  `apply/policy.py`, `apply/preflight.py` and `api.py` import the module.
- **`apply_eeo_store_values` is not built.** The student chose D5 C (i), so the service refuses every EEO value
  that is not a decline, on a whole-label match against a fixed list, whatever a route or a future caller passes.
  Sub-choice (ii) would need its own decision and a THREAT_MODEL.md:17 rewrite.
- The routes are all under `require_browser_session` (new in `api.py`; refuses any `Authorization` header, and for
  a write checks the CSRF header whether or not an `Origin` is present). Reading the list needs it too, since an
  entry is the student's own answer. `GET/POST /api/v1/apply-agent/sensitive-answers`,
  `DELETE .../sensitive-answers/{id}`, `PUT .../sensitive-categories`, and the Needs you form
  `POST /api/v1/apply-agent/opportunities/{id}/sensitive-answers`, which takes only the answer and the tick and
  reads the category, wording and options from the form it re-reads.
- The category the student switches on is a choice of storable categories only (`export_control`, `salary`
  and `uncategorized` are refused, and ignored if hand-written into `user_settings`). Nothing is on by default.
- A statement that reads a document, or links one, is saved for one company; its links are stored, shown in the
  check's field list (the plan preview's "links to {address}"), and re-checked against the form: a changed
  address is a mismatch, not a tick. A data-processing consent (`data_compliance`) has no statement in the
  listing, so it stays left for the student until M5b reads the statement from the page.
- An optional sensitive question, which is never a "problem", is offered in the check's `optional_sensitive`
  list with the same form, so an optional EEO field can be answered with the form's own decline label.
- The consent wording is "Use this answer only to fill in application forms when I ask the app to apply, and for
  nothing else. I look over each application before it is sent." The spec's sentence ("...that the app submits
  after I confirm each one") is not true under D1 B, where the student presses Submit; M6 rewrites it with the
  D1 A wording. Every entry is stored with `consent_scope='confirmed'`; `unattended` is refused until M8.
- `last_used_at` is not written yet: the check and the plan write nothing, and the run that fills the field
  (M5b) is the one to record it.

### 5.5 `apply_ats_labels`: exact option labels, confirmed once

Some Greenhouse fields are typeahead lists whose labels differ from how the student writes the
value. For example, the school catalog may list "University of Example - City" where the
official name is "The University of Example at City" **[1-src]**. Pelias locations and degree and
discipline lists work the same way. Guessing the option is fuzzy matching, so the student picks
the exact label once.

```sql
CREATE TABLE IF NOT EXISTS apply_ats_labels (
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    ats TEXT NOT NULL,               -- 'greenhouse'
    field TEXT NOT NULL,             -- 'location', 'school', 'degree', 'discipline', 'phone_country',
                                     -- 'education_start_month', 'education_start_year', 'education_end_month', 'education_end_year'
    label TEXT NOT NULL,             -- the exact option text the student chose
    confirmed_at TEXT NOT NULL,
    PRIMARY KEY (user_id, ats, field)
);
```

These labels live in their own table, not in `profile_facts`. `preparation.confirmed_facts`
returns every confirmed fact as a flat map (student/preparation.py:33-40), and other features such as
the résumé renderer read that map. ATS-specific labels must not leak into them.

**How a label is found.** The schema carries no options for Pelias locations or the school
catalog, so the options must come from the page. The Needs you action for a typeahead (10.3) has a
text box, filled in from the matching confirmed fact when there is one (the profile's school, for
example), and a **Look up options** button. That starts a `kind='lookup'` run:

- rehearsal routing (4.3), so after the first input nothing leaves but that field's lookup GETs
  (carrying only the typed text) and static assets;
- it types the text into that one field, reads up to 20 option labels with `read_options`, fills
  nothing else, takes no screenshot, and closes;
- the options are stored in `options_json` (public page text) and shown as radio buttons; the
  student's pick is saved here and used from then on.

The lookup sends what was typed to Greenhouse's lookup service, and the action says so. Lookups
count toward the daily rehearsal limit (9.1).

### 5.6 Settings, features, and the code that reads claims

**Features** go in `automation.FEATURES` (automation/ledger.py:150-213):

- `Feature("apply_agent", "Apply for me", "Fill a Greenhouse application from your confirmed facts and saved answers, show you the result, and send it only when you press Submit", "applications", "external")`
  - Its mode is `OFF_ON`, and there is no shadow, because each submission needs the student.
  - It gets a `REQUIREMENTS` entry (automation/ledger.py:260-265), `apply_runs.setup_requirement`. That
    returns the first of these sentences that applies:
    - the agent factory's `available()` probe says Playwright or Chromium is missing ("Install
      Playwright and Chromium: python -m playwright install chromium");
    - on Linux, neither `DISPLAY` nor `WAYLAND_DISPLAY` is set in the server's environment. The
      dashboard runs as a systemd --user unit (launch.py:277-290) that has no display by default
      ("Apply for me needs to open a window. Run: systemctl --user import-environment DISPLAY
      WAYLAND_DISPLAY, then restart the dashboard");
    - no confirmed first and last name: `name_parts` not confirmed and `name` not exactly two
      words ("Add your first and last name for applications in your profile", 7.1);
    - no confirmed email;
    - no confirmed résumé;
    - under D12 A: `application_mail` is off ("Turn on Update applications from job emails, in
      shadow is enough, so the app can look for each confirmation"); or
      `connector_accounts.account_email` is '' ("Reconnect Gmail once so the app knows which
      address it reads"); or it differs, case-insensitively, from the confirmed `contact.email`.
      Under D12 C the same three checks run in the preflight of a one-click submit (6.0 step 7)
      instead of gating the switch, and Finish in browser works without them (its card then says
      the app isn't checking for a confirmation email).
  - These are all database or environment reads; none makes a network call, because
    requirements run on every settings render (automation/ledger.py:611-622).
- `auto_apply`, for unattended mode, is **not registered until M8** (D2).

**Per-student limits** default in code, can be overridden in the student's profile, and are
read the way `application_silence` reads its day count:

| Profile key (under `apply_agent`) | Default | Meaning |
| --- | --- | --- |
| `spacing_minutes` | 10 | minimum time between two hand-overs |
| `daily_cap` | 5 | one-click and unattended hand-overs per local day (user_time.user_timezone) |
| `company_days` | 30 | at most one hand-over per company (name or board) in this many days |
| `rehearsals_per_day` | 20 | lookups and rehearsals per local day |
| `rehearsals_before_submit` | 3 | the D3 gate |

The student's D3 and D4 answers become these defaults.

**Per-student state in `user_settings`:**

| Key | Meaning |
| --- | --- |
| `apply_sensitive_categories` | D5 choice, comma-separated; empty by default |
| `apply_eeo_store_values` | `on` only for D5 C sub-choice (ii) |
| `apply_gate_reset_at:<ats>` | set when the breaker trips (9.2); the gate counts only rehearsals after it |
| `apply_ats_disabled:<ats>` | set by the 8.8 threshold; cleared only by the student |

**Screenshot retention:** `PIPELINE_APPLY_EVIDENCE_DAYS`, default 90 (D8), following
`PIPELINE_MAIL_EVIDENCE_DAYS` (applications/inbox.py:2181-2186).

**The worker step.** `AutomationWorker.run_once` today works only for students with an outreach or
internal switch on (outreach/automation.py:386-458, `_users_with`). It gains an apply step that
does not depend on switches:

- for every student returned by `apply_runs.students_to_watch(conn)` (a query: a claim in
  `claimed` or `clicking`; an uncertain attempt or a tombstone with `handed_over_at` in the last
  14 days; a submitted claim still being watched; or a `running` run), it runs `recover_stale`
  and `watch`, so a student who turned `apply_agent` off still gets their open claims finished and
  watched;
- once per local day, for every student, `purge_evidence` (11);
- it records `apply_agent.runner` and `apply_agent.watch` health components.

**Existing readers that must learn about the new claims:**

- `automation.in_flight` (automation/ledger.py:530-572): add each `clicking` claim that is held (5.2
  rule 7: running here, or heartbeat under 2 minutes old), with `action: "application"`,
  `source: "apply_claim"`, and the company. Not by claim age: a handoff claim can legitimately be
  20 minutes old at hand-over.
- `automation.unconfirmed` (automation/ledger.py:575-608): add `unconfirmed` claims, `clicking` claims
  that are not held, and `after_click=1` claims in `needs_you` or `failed`.
- `automation.paused_text` (automation/ledger.py:2158-2171): generalize its grammar from exactly two
  categories to any number ("1 email was already handed to Gmail, 1 contact form was already
  being sent, and 1 application was already being submitted, and none of them can be stopped").
- `app.js`: `unconfirmedSentence` (app.js:7417) and the in-flight and Health rendering (around
  app.js:7561) learn `action: "application"`.
- `applications/urgent.py`: add one kind for claims that need the student (`unconfirmed`, `needs_you`) and one
  for `no_email_24h`. Each kind must be added to **both** `DATE_SOURCE_LABELS` (applications/urgent.py:48) and
  `KIND_PRIORITY` (applications/urgent.py:68), or the aggregator raises KeyError (PLAN.md:74-75).

### 5.7 Export and deletion

- **Account export** (accounts/operations.py):
  - include `apply_runs`, value-free. `export_account` redacts only columns named `storage_path`
    or ending in `_path` (accounts/operations.py:235-238), so `screenshots_json` is special-cased: every
    `path` inside it becomes `[private-file-reference-redacted]`;
  - include `application_submit_claims`, `apply_sensitive_answers` and `apply_ats_labels`, which
    are the student's own data;
  - never include the HMAC key (5.3).
- **Account deletion.** `operations.delete_account` (accounts/operations.py:245-275) takes a list of
  storage roots and has three callers: api.py:3207, tests/test_automation.py:1267 and
  tests/test_urgent.py:492. It gains an optional keyword argument `apply_root: Path | None =
  None`. When given, it removes `data/private/apply/<user folder>/` in full (section 11) before
  it deletes the user row, which cascades the new tables. api.py:3207 passes it; the two tests are
  unchanged, and a new test passes it.

---

## 6. The run loop, step by step

One run is one call of `apply_runs.start_run(kind=..., now=...)`. The numbered steps are the order
the code runs in. **Bold** outcomes end the run.

### 6.0 Preflight (read-only, no browser)

Preflight runs at the start of every run, and on its own for the check route when the student
opens the Apply for me section. **It writes nothing**: no application, no interaction, no event,
no run row. The section asks for it asynchronously and shows "Checking…" meanwhile, so the detail
panel never waits on Greenhouse.

1. `apply_agent` is on. If not, the route returns 409 with the setup requirement sentence.
2. The adapter identifies the posting (4.4). If it does not, the UI shows "Apply for me works with
   Greenhouse postings only, for now" and there is no run.
3. **The application, if any.** Look up `applications` by `(opportunity_id, user_id)`, read-only,
   the way `import_applications` does (applications/actions.py:729-736). An application row may already exist
   from the posting's Apply link (`apply_opened`, applications/actions.py:131), a capture (opportunities/captures.py:337) or an
   import (core/schema.py:681); any existing row is used. None is created here (6.1 creates one for
   submit and handoff only).
4. **Stage and duplicate checks.** "Ask" means a tick the student must give on the start of a
   submit or Finish in browser run (10.3); lookups and rehearsals are never stopped by an ask.
   - An application exists and its stage is not `applying`: **failed**, "This application is
     already {stage}".
   - An `application_confirmation` in `application_mail_messages` for this application:
     **failed**, "Greenhouse already confirmed an application from you on {date}".
   - An `application_form_sessions` row for this application with status `completed` (the
     extension's confirm-submitted, applications/extension.py:593-632): **failed**, "You marked this
     application submitted on {date}".
   - A claim for this application or for this Greenhouse job (`job_ref`) that is `claimed`,
     `clicking`, `submitted` or `unconfirmed`, or `needs_you`/`failed` with `after_click=1`:
     **failed**, with the claim's note, or "another saved copy of this role" for a job match. A
     `needs_you` or `failed` claim with `after_click=0` does not block: nothing was sent, and the
     next submit or handoff releases it (5.2 rule 2).
   - A `released` tombstone for this job with `handed_over_at` set: **ask**, "You said the attempt
     on {date} didn't go through. Greenhouse may still have it. Send it again anyway."
   - An `application_confirmation` with `application_id=''` (unmatched or ambiguous) received since
     the role was saved, from a Greenhouse sender, whose subject contains every company token:
     **ask**, "An application confirmation from {company} arrived on {date} that the app couldn't
     match to a role. I haven't applied to this role."
   - The application is `applying` and was created more than a day ago: **ask**, "Did you already
     apply to this by hand? I haven't applied yet."
5. **Fetch the schema** through the injected schema client (4.6): TLS verification on, a 20 s
   timeout and the pipeline's usual user agent. The check route keeps an in-memory cache per
   `(token, job_id)` for one hour; runs always fetch fresh. A 404 is **failed**, "The app couldn't
   find this posting on Greenhouse. It may be closed". Any other error is **failed**, "Greenhouse
   did not answer. Try again later".
6. **Build a draft plan** from the schema alone (6.6 without the DOM join). Every required field
   with no source is listed as a problem.
   - **Submit:** any problem is **needs_you** with the list, and no browser is opened.
   - **Rehearsal and handoff:** the run continues. A rehearsal records the gaps (and is then not
     clean, 9.2); a handoff leaves those fields empty and lists them as "left for you".
   - The check route returns the list, and the UI offers an action for each (10.3).
7. **Limits.** For submit and handoff runs, check the caps (9.1). For submit runs only, check the
   rehearsal gate (9.2) and, under D12 C, the email-watch checks (5.6). For lookups and
   rehearsals, check the daily rehearsal limit (9.1). A block
   is **needs_you** with the reason, such as "The next agent submission is allowed at 3:40 PM" or
   "You applied to {company} with Apply for me 12 days ago". The company limit offers its
   override tick for both submit and handoff (D4).
8. **For submit runs,** the confirmed `plan_hash` must equal the chosen rehearsal's, that
   rehearsal must be less than 15 minutes old, and the confirm nonce must be valid and unused.
   This is the one confirm clock: the age of the confirmed rehearsal. It is checked here and again
   in the hand-over transaction (5.2 rule 3).

### 6.1 Claim (submit and handoff only)

One transaction, in this order:

1. the lock (5.2 rule 0);
2. **the application row, if missing.** `actions.ensure_application_tx(conn, opportunity_id,
   user_id, event_type="apply_agent_started", detail={mode, run_id})`, a small helper factored
   out of `_record_intent_tx`'s `apply_opened` branch (applications/actions.py:127-152). It inserts the
   `applying` row if missing (`ON CONFLICT(opportunity_id, user_id) DO NOTHING`) and writes an
   `application_events` row `apply_agent_started`. It writes no `opportunity_interactions` row:
   that table's `action` CHECK allows only five actions (migrations/0001:141), and "the app
   started filling" is not the student opening the posting;
3. the claim insert (5.2 rule 1), with the consumed nonce for submit;
4. the re-checks of 6.0 steps 4 and 7, inside the same transaction. Because of the lock, a pause,
   a stage edit by the student, a second tab, or a second server process either lands first and
   is seen, or waits.

An abandoned Finish in browser leaves an `applying` row, as clicking the posting's Apply link does
today (13 R11).

### 6.2 Launch and navigate

The runner starts the child process (4.6), which starts the browser (4.3).
`page.goto(canonical_url, wait_until="domcontentloaded", timeout=navigation_s)`.

- A status of 400 or above is **failed**, "Greenhouse answered HTTP {status}".
- Otherwise wait for `networkidle`, up to 8 s. A busy page is read as it stands, as in
  outreach/forms.py:892-896.
- Keep the served HTML for `loader_paths`. For submit and handoff runs, no `submitPath` or no
  `confirmationPath` is **needs_you**, "The app couldn't find where this form sends applications,
  so it won't submit it. Apply from the posting instead." Without the submit path the app could not
  tell the student's submission from any other request.

### 6.3 Detect the page

`adapter.detect_page(page)`:

- `application_form_new` continues. `application_form_legacy` continues only when
  `LEGACY_ENABLED`; otherwise **needs_you** (4.4).
- `closed` is **failed**, "The posting is no longer accepting applications". This covers the
  board page with `?error=true`, a redirect to the board index, or the absence of the form
  together with a closed-posting notice. The notice is looked for only when the form is absent,
  and is never used to decide an outcome by itself.
- `offsite` is **needs_you**, "This posting sends applicants to {host}". The main frame left the
  Greenhouse hosts.
- A form split over several pages (a second page appears) is **needs_you**.
- A form with `data-allow-s3="true"` (it uploads files as soon as they are attached): for submit
  and handoff runs, **needs_you** while `S3_UPLOAD_ENABLED` is False (6.9); a rehearsal continues.
- `unknown` is **needs_you**, "The page did not look like a Greenhouse application form".

### 6.4 Inject, scan, and snapshot

`frame.evaluate(ENGINE_SOURCE)`, then
`frame.evaluate("(a) => OpportunityApplyEngine.scan(a.profile, a.answers, {tag: true})", ...)`.

- The profile and answers passed in are only those the plan may use. Sensitive answers are
  **never** passed into the page's JavaScript.
- The form is in the main frame when the canonical URL is used. If Greenhouse ever serves it in
  `#grnhse_iframe`, use that frame. Playwright can reach it even though it is cross-origin, which
  the extension cannot (content.js:63-72).
- **Snapshot.** Right after the first scan, record in memory each control's initial value (text,
  checked state, selected option). 6.10 item 4 uses it.

### 6.5 Join schema and DOM

Each schema field is matched to DOM controls by `name`/`id`. Greenhouse's control ids equal the
schema field names on the sampled board: `first_name`, `last_name`, `email`, `phone`, `resume`,
`question_{id}` **[live]**.

**One question key.** For every joined field the question key is
`question_key(schema_field.label)`. The DOM `question` from the engine must normalize to the same
key; otherwise it is a problem, "The form's wording differs from Greenhouse's listing
({question})". So the browserless preflight, the live plan, and answers saved from the Needs you
flow all use the same key.

Each field must join to exactly one visible control, or one field container for react-select and
file groups. Anything else is a problem:

- a required schema field with no control or more than one control: "The form does not match what
  Greenhouse's own listing describes ({question})";
- a DOM control with any required marker (`required_any`) that is not in the schema: "The form has
  a required field the listing does not mention ({question})";
- a control with `visible_css=false` that the plan would fill. The exception is a file input
  inside a visible upload group: Greenhouse's `input#resume` is `visually-hidden` **[live]**. Any
  other hidden field is never filled, because it may be a spam trap.

Join problems are computed by `apply_checks.join` (4.7). Any join problem is **needs_you** in a
submit run, makes a rehearsal not clean, and in a handoff leaves that field "left for you" (and
stops the handoff if the field would be a hidden one).

### 6.6 Plan

`apply_policy.build_plan` applies section 7 to every field and produces `PlanField`s:

- key, question (the schema label), control, required, options, statement text and links,
  sensitive category, source `{kind, ref, company, reusable}`;
- the value, which is **held in memory only**, and its `value_mac`;
- a **disposition**:
  - `fill`: the agent fills it;
  - `deferred` (rehearsal only): the value is known and checked against the page, but not put in
    the page until submit. This is every sensitive field, and the résumé on a board that uploads as
    you attach (6.9);
  - `left_for_you` (handoff only): a required field with a problem, or a sensitive field the
    student has not allowed. The agent leaves it empty and lists it;
  - `blank`: an optional field with no source, or with a problem. The preview says so.

It also produces problems. What a problem on a *required* field does depends on the mode:
**needs_you** in a submit run; recorded (and the rehearsal is not clean) in a rehearsal;
`left_for_you` in a handoff. The claim, the no-guessing rule and the hidden-field rule apply in
every mode.

`plan_hash` is the SHA-256 of canonical JSON (sorted keys) covering everything the student is
approving:

- `canonical_url` and `ADAPTER_VERSION`;
- the list, sorted by key, of each field's `key`, `question`, `control`, `required`, option
  labels, statement text and links, `source.kind`, `source.ref`, `value_mac`, `file_sha256`, and
  for a cover letter its document id, version and content SHA-256.

`disposition` is **not** in the hash, so a rehearsal that deferred a field and the submit that
fills it compare like with like (6.10 item 6).

### 6.7 Fill

The order follows FormSubmitter (outreach/forms.py:919-947): choices first, because a choice can
redraw the form.

1. react-selects and location, via `adapter.fill_react_select` / `fill_location`;
2. radios and checkboxes, via `_tick`;
3. text inputs and textareas, via `_type`, then `dispatch_event("change")` and `blur()`, as in
   outreach/forms.py:958-962.

Only `fill` fields are touched. For a `deferred` field the rehearsal checks, without choosing
anything, that the page offers what the plan would give: `read_options` must show exactly one
option whose label equals the planned label, or the checkbox's label must equal the stored
statement. A mismatch is a problem. `left_for_you` and `blank` fields are not touched.

After step 1, scan again. If the set of field keys changed, re-plan once. A second change is
**needs_you**, "The form kept changing as it was filled in".

Any Playwright error on a field is **needs_you** in a rehearsal or a submit, "The field
"{question}" did not take the answer". This covers a covered element, a read-only field, or a
missing option. In a handoff the agent clears that field if it can, and moves it to "left for
you".

### 6.8 Read back every field

Read through Playwright, not the engine:

- text: `input_value()`, compared with `outreach_forms.formatting_problem`
  (outreach/forms.py:1157), which allows only the CRLF difference;
- react-select: the single-value text, which must equal the planned option label after
  normalization (the engine's `normalized`, field-engine.js);
- native select: the selected option's label;
- checkbox or radio: `is_checked()`.

Any difference is **needs_you** (in a handoff: cleared and left for you), with the difference in
words and never the value.

### 6.9 Attach the résumé and cover letter

**Which résumé** (`apply_policy.resume_for`):

1. **The pick in force**, from `resume_variants.stored_pick` (student/resume_variants.py:293-320): the
   student's own pick, or an automatic pick with status `picked`. The file used is the most recent
   **confirmed** version of that résumé file. (`stored_pick`'s `version_id` is the newest version
   of any status; `artifact_path` accepts only confirmed ones, applications/extension.py:656-664.) No
   confirmed version is a problem: "The résumé picked for this role has no confirmed version".
2. **An automatic pick with status `unsure`**: a problem that opens the résumé chooser (10.3).
3. **No pick at all** (resume_variant_pick is off, the student has no variants, or the status is
   `no_variants`), which is the common case: the most recently confirmed résumé, the rule
   `resume_check` already uses (student/resume_variants.py:430-440). The preview shows it as "Your
   confirmed résumé".
4. No confirmed résumé at all: the feature cannot be turned on (5.6).

The source ref is the version id.

**Reading the file.** `extension_apply.artifact_path` needs an application id, which a rehearsal
does not have. Its résumé branch is factored out into
`extension_apply.confirmed_resume_file(conn, version_id, storage_root, *, user_id) -> (path,
original_name, media_type, sha256)`, with the same confinement to the storage folder;
`artifact_path` calls it. The preflight (6.0 step 6) already resolves the version, checks that
the file exists and that its SHA-256 matches `resume_files.sha256`, and reports a problem if not.
The agent reads the bytes and checks the SHA-256 again before attaching.

**Attaching.** `set_input_files` with a path would upload the storage file name
(`resume-file-<uuid>.pdf`, student/resumes.py:437-440). The agent attaches a payload instead:
`set_input_files({"name": original_name, "mimeType": media_type, "buffer": data})`, so the
employer receives the name the student sees.

- The file's extension must be in the control's `accept` list; Greenhouse accepts
  pdf/doc/docx/txt/rtf **[live]**.
- Read-back: `input.files[0]` has `original_name` and the right size, and the upload group shows
  `original_name`.

**Cover letter (D11).**

- The candidate is the **latest** cover-letter version in `generated_documents` for this
  opportunity, and only if that version is `approved`. A newer draft, or more than one approved
  candidate, is a problem: "Your cover letter for this role has a newer draft. Approve it or
  discard it".
- Before attaching, `document_artifacts.ensure_document_artifact` re-renders when the stored
  artifact's `content_sha256` (5.1) differs from the SHA-256 of the approved text. Today it returns
  any existing file (student/artifacts.py:105-108), and the API deletes the old PDF only after
  an edit has committed (api.py:3409-3410), so a failed delete could leave a stale PDF.
- The document id, version and content SHA-256 are in the plan hash, and the preview shows the
  letter's text.
- Attached with a payload, as above, using the artifact's file name.
- Without an approved letter: a problem with a **Draft one** action. An optional cover-letter
  field is left empty (D11 A/B).

**Boards that upload as you attach** (some boards have `data-allow-s3="true"`; the sampled one had
`false` **[live]**). Attaching there sends the file at once, before any Submit, and the exact
upload flow has not been seen live, so `S3_UPLOAD_ENABLED = False` (4.3):

- a rehearsal leaves the résumé `deferred` and records "This board uploads your résumé as soon as
  it is attached, so the app can't attach it without sending it". Such a rehearsal is **not
  clean** for the gate (9.2);
- submit and handoff refuse the board before filling: **needs_you**, "This board uploads files as
  soon as they are attached, which the app does not support yet. Apply from the posting instead."

Enabling it later is a code change with a live check and a fixture: the only upload allowed before
hand-over would be to the exact address Greenhouse's own response names, with a body containing
the planned file's bytes, and 6.10 would check the result.

### 6.10 The independent pre-submit check

`REQUIRED_CHECK_SCRIPT` is a separate JavaScript string in `apply/checks.py`. It deliberately
shares no code or selectors with `apply-engine.js`, so a bug in the scanner cannot hide itself.
It walks `form#application-form` (legacy: `#application_form`) and returns one item for every
field container that is required by **any** of:

- the `required` attribute;
- `aria-required="true"` on the control or its `[role=group]`;
- an asterisk in the label;
- a hidden `input[required]` sibling;
- a `span.required` in the container.

Each item holds `{key, question, markers, kind, value_text, empty}`. `value_text` is the text
value, the react-select single or multi value text, the checked boxes' labels, or the attached
file name. It also returns the current value of every other control in the form.

`apply_checks.check_required` then requires all of the following. Any failure is **needs_you** in
a submit run and makes a rehearsal not clean; in a handoff the field is cleared if the agent put a
wrong value there and listed as left for you. Items whose key is `deferred`, `left_for_you` or
`blank` in the plan are skipped for 1 to 3.

1. Every item is non-empty.
2. Every item's key is in the plan with a source, and its `value_text` matches the planned value
   (for files, `original_name`).
3. Every schema-required field appears among the items. This is a third, independent source.
4. **No control holds a value the plan did not put there.** Exceptions:
   - Greenhouse's own hidden inputs (`input_hidden` in the schema, and the react-select mirrors);
   - an optional field that still holds exactly its initial value from the snapshot (6.4): a
     native select whose first option is a real value, a pre-selected radio, a box the employer
     pre-checked. The preview lists these under "left as the page set it".

   A required field holding a value the plan did not set is always a failure. This catches
   autofill, and page scripts that fill fields.
5. No visible `[aria-invalid="true"]` control and no visible field error text. A native `:invalid`
   check runs as in outreach/forms.py:987-996.
6. Submit runs only: the live plan's `plan_hash` equals the confirmed one. Otherwise the result is
   "The form or your answers changed since you confirmed. Look at the new plan".

Then take the **filled screenshot**, masked per D8, and record its SHA-256.

### 6.11 CAPTCHA widgets

If a visible checkbox widget (reCAPTCHA anchor, hCaptcha checkbox, Turnstile) is present, as in
`CAPTCHA_WIDGETS` (outreach/forms.py:776-781):

- **Rehearsal:** record "The form shows a CAPTCHA checkbox", and do not touch it.
- **Handoff:** left for the student.
- **Submit, D14 A:** before hand-over, the window comes to the front with "Tick the CAPTCHA box in
  the window, then the app will continue". The claim stays `claimed`, so nothing has been sent.
  The agent waits up to `person_s` for the widget's token, heartbeating. No token is
  **needs_you**, `after_click=0`.
- **Submit, D14 B:** `_click(..., "captcha_checkbox")`, then wait up to `captcha_s` for a token, as
  `_pass_captcha` does (outreach/forms.py:1047-1076).
- A picture challenge, in any mode, is never touched by the agent: in submit mode it is handled
  like D14 A.

Greenhouse's usual reCAPTCHA Enterprise is invisible **[live]**. There is nothing to click, and it
runs when Submit is pressed.

### 6.12 Rehearsal ends here

The outcome is **rehearsed**. Record the plan, the screenshots, `refused_json`, the reasons, and
`clean` (`apply_checks.clean_rehearsal`). Close the browser. When the student opens the preview in
the one-click stage, the server issues the confirm nonce (4.6) and stores its hash on this run.

**As built (2026-10-08): a form of more than one page is not "rehearsed".** The app reads one page. After the filled form's picture, a
rehearsal runs `MORE_PAGES_SCRIPT` (`apply/checks.py`, read-only, in the form frame): a visible Next, Continue or Save and continue
control in the form or the element around it, or a step counter ("Step 1 of 3", or `aria-current=step`), means the app read only the
first page. The run then ends **needs_you** with "This form has more than one page, and the app read only the first" (so the view
says "The rehearsal stopped: ... No application was sent."), `evidence.more_pages` is true, and it is never a clean rehearsal. No
multi-page Greenhouse form has been recorded, so the words the script looks for are a guess made to fail closed; a Finish in browser
run is unchanged (the student completes the form in the window).

**Submit runs always start from a fresh page and fill again.** Rehearsal and submit are separate
runs because:

- the rehearsal blocked CAPTCHA and telemetry traffic;
- the rehearsal did not put sensitive answers in the page;
- the student may take minutes to review;
- re-filling proves the plan still holds (6.10 item 6).

### 6.13 Hand over and press Submit

**Submit mode (one-click, M6):**

1. Until hand-over the Running state offers **Cancel**, which sets `cancel_requested=1`; the agent
   polls `cancelled()` between steps. Closing the Chromium window before hand-over counts as a
   cancel. Either gives **failed**, `after_click=0`, "Cancelled before pressing Submit. Nothing was
   sent".
2. `hand_over()` runs the hand-over transaction (5.2 rule 3). False gives **failed**,
   `after_click=0`, with the reason: "Automation was paused after you confirmed. Nothing was
   sent", "Cancelled…", "The rehearsal you confirmed is more than 15 minutes old. Rehearse again.
   Nothing was sent", or "This attempt is no longer the current one".
3. Start collecting the observation for `decide_outcome` (4.7): every non-GET request from now on,
   with its status, and every main-frame navigation.
4. `_click(adapter.submit_control(frame), "submit")`.

From step 4 on, any exception gives **unconfirmed**, as in outreach/forms.py:1017-1023.

**Handoff mode (Finish in browser, M5b):**

1. After the fill and the check, the window comes to the front with the "left for you" list shown
   in the app. The claim stays `claimed`.
2. The student completes the fields and presses Submit in the window. The route handler sees the
   POST to `submitPath` on `boards.greenhouse.io` and calls `hand_over()` **inside the handler,
   before `route.continue_()`**. That call crosses to the parent process, which commits the
   hand-over transaction before replying (4.6). Only an explicit True reply lets the POST
   continue. Otherwise (False, an error, or no reply within 10 s) the handler calls
   `route.abort()`, and the run ends **needs_you**, `after_click=0`, "The app couldn't record this
   submission, so it stopped it. Nothing was sent. Try again." A second press of Submit after the
   first POST passed is aborted (4.3).
3. A POST from the page to any other Greenhouse address is aborted before hand-over (4.3). If
   that happens when the student presses Submit, the page shows an error and the run ends
   **needs_you**, `after_click=0`, "The form tried to send to an address the app doesn't
   recognize, so the app stopped it. Nothing was sent. Apply from the posting instead." Blocking
   is the safe direction: the app never loses track of a submission.
4. If `handoff_s` (20 minutes) passes, or the window is closed, or the student presses **Stop**
   in the app, with no hand-over: the agent **closes the browser first**, so a Submit pressed later
   cannot reach Greenhouse, and only then settles **needs_you**, `after_click=0`, "You didn't
   submit it in the window. Your application was not sent." Then the attempt can be retried.
   (Uploads are refused before hand-over, 4.3, so nothing else could have left either.)

After hand-over both modes continue with 6.14.

**As built (M5b part 2).** Finish in browser (handoff) works end to end. Where it differs from the text above, or the
spec left a choice open:

- **D1 B holds everywhere.** The agent never presses Submit, including the second Submit after a security code. While the
  agent types a code the route aborts every submit-path POST without spending the prompt's allowance (`code_post_while_typing`),
  and after it has typed a code the route aborts the code POST until the student has pressed Submit (below).
- **The code POST waits for the student's press (decided 2026-10-08, open question Q4).** Once the app has typed the emailed
  code, `RouteState.code_press_required` is set and the prompt's one code POST is refused (`code_post_before_press`, nothing
  spent) until a trusted click on the form's submit control has been seen after the typing finished (`RouteState.code_pressed`),
  however long the widget waits and however often it retries. The two-second tail (`CODE_GUARD_S`) is gone: a student who presses
  at once is not held up, and a widget that sends at 2.5 s is refused like one that sends at 0.3 s. A refusal for either reason is
  recorded as the widget sending by itself (`auto_submit_blocked`) and the student has been told to press Submit. The press is
  heard through the one DevTools session the agent opens (`_watch_presses`): a listener in an isolated world
  (`PRESS_LISTENER`, `PRESS_WORLD`) registered on the window in the capture phase before any page script runs, which reports a
  click only when `event.isTrusted` is true, the target is inside the application form's submit control (Enter in a box becomes
  such a click in the browser) and the page is a board's own. It reports through a binding that exists in that world only, so a
  page script cannot call it, find its name, or reach the listener's built-ins; a script click, a made-up event or a submit by
  script has `isTrusted` false and is ignored (`tests/test_apply_agent_browser.py`, `security_code_forger`). The request can reach
  this process a few milliseconds before the report of the click that made it, so a code POST refused for want of a press waits up
  to `PRESS_GRACE_S` for the report and is judged again. If the listener cannot be set up, the app does not type the code (reason
  `press_unseen`) and the student types it, which needs no press to be seen. The static guard allows the four DevTools calls in
  `PRESS_CDP_CALLS` and nothing else.
- **Telemetry.** Greenhouse posts Snowplow telemetry to `c.spl.greenhouse.io`, so step 3 above ("a POST from the page to
  any other Greenhouse address") cannot be read literally. Only an aborted non-GET to a form host (`job-boards`, `boards`,
  `boards-api.greenhouse.io`), or any aborted form navigation, ends the turn. Telemetry hosts are refused for every method,
  silently, and the value guard also checks base64 forms.
- **The parent answers the security-code op for its own claim**, never a token the child names, and the child types the
  code only after an `id`-matched reply; "typed" is recorded only on the child's acknowledgment.
- **Deadline.** `fill_s + handoff_s + code_read_s + security_code_s + 3 x outcome_s + 120` (2910 s), one shared budget for
  every wait after the press, and every agent wait capped by `job.ends_at`. The watchdog is a backstop for hangs.
- **Hand-over.** The child stamps a monotonic `expires`; the parent refuses a late commit. Any claim in `clicking` is at
  most `unconfirmed`, never "nothing was sent". The window is confirmed closed by process id before a claim that is
  still `claimed` settles with `after_click` 0.
- **Field failures are cleared and left for the student**, except a select holding a wrong option, which stops the run
  (`FIELD_TOOK`). A submit POST or an upload refused during the fill ends the run. A challenge frame after the press waits
  for the student, within the shared budget. No final screenshot after Stop, a closed window, or the timeout.
- **Cover letters** are never attached (M7): a required one is left for the student, an optional one blank. The "Draft
  one" hint is dropped until then.
- **A student-stopped handoff** is `needs_you`, `detail.stopped_by = 'student'`, with no notice and no Urgent row.
- **Screenshots and values need the browser session.** The masked pictures show every non-sensitive filled value (D8 (i)),
  so they and `GET .../values` need the student's own browser session. A handoff result shows only provably unchanged
  values ("What the app filled").
- **A posting that differs from the saved role** needs the student's tick (`posting_confirmed`) before Finish in browser.
- **A pause does not stop a Finish in browser window**, and the pause reply and the health card say so.
- **Failed outcomes name the field, never the page's error text**, so a value the student typed cannot reach a note.
- **A send to an address the app does not recognize is said in the turn (2026-10-08).** Only an aborted non-GET to a form host, a
  file going anywhere, or a form navigation ends the turn (above). Any other refused non-GET that carries a form-like body
  (multipart, URL-encoded or JSON) within 15 s of the student's press of Submit (`checks.looks_like_a_send`, the press as the
  listener of 6.13 reports it) is refused as before, the turn goes on, and the agent reports the progress step `form_elsewhere` with
  the host ("The form tried to send to {host}, which the app doesn't recognize, so the app stopped it. Nothing was sent. ...") once.
  The run view shows it as the turn's sentence (phase `form_elsewhere`, still the student's turn), and a finished run lists "While the
  window was open the form tried to send to {host} ... nothing was sent" beside its ending (`evidence.elsewhere_seen` holds the host
  only). A beacon with no form body, Greenhouse's telemetry and a CAPTCHA request say nothing. The board's real submit address for such
  a form is still unknown: when a live board shows one, add it to `FORM_POST_HOSTS` so the turn ends as it does for the others.
- **Finish in browser is offered again only where a second try can differ (2026-10-08).** A run that stops before the turn on a
  property of the board itself (no submit address the app knows, a board that uploads on attach, a hidden field the app would have
  filled) records `handoff_end` "board". The run view carries `handoff_end` and `finish_again`; the result panel offers Finish in
  browser again only when `finish_again` is true (the turn ended by Stop, the closed window, the clock, a refused or early press,
  a send to another address or an upload the student's page made, a crashed window, an attempt the student released, or a run with
  no report from the browser at all) and otherwise offers "Open the posting". It costs no limit either way.
- **Open question Q4, answered 2026-10-08** (the plan called it Q1, but Q1 in section 13 was already taken; Q4 is listed there too).
  Does Greenhouse's security-code widget submit by itself when its eighth character is typed? No recording of the live widget exists.
  The owner decided that it does not matter: the app types the code and the code POST waits for the student's press, whenever the
  widget sends and however often it retries (the bullet above). A recording of the live widget would still be worth keeping.

### 6.14 Decide the outcome

`apply_checks.decide_outcome` decides from the observation. The window is `outcome_s` (30 s),
polling every 500 ms. Page wording is never used. The first matching row wins.

| What is seen after hand-over | Outcome |
| --- | --- |
| A POST to `submitPath` answered 2xx or 3xx, **and** the main frame's path then equals `confirmationPath` from the loader (or matches `^/{token}/jobs/{id}/confirmation/?$`, or the embed's `/embed/job_app/confirmation` with the same `for` and `token`), **and** no `form#application-form` is in the DOM | **submitted**, `resolved_by='page'` |
| Security-code inputs visible, or the POST answered 428 `captcha-failed` | The claim stays `clicking` (`detail.waiting='security_code'`). Window to the front; wait up to `security_code_s` (D10 A) for the student to type the code and press Greenhouse's second Submit, heartbeating. Then the table is applied again; a confirmation now is **submitted** with `detail.security_code=true`. No confirmation by the end is **needs_you**, `after_click=1`. |
| A challenge frame (`iframe[src*='bframe']`, hCaptcha challenge) | **needs_you**, `after_click=1` |
| The POST to `submitPath` answered 4xx other than 428, with the form still present | **failed**, `after_click=1`, "Greenhouse refused the form (HTTP {status})" plus the first visible field error |
| **No** POST to `submitPath` passed the route, and no main-frame navigation happened. (Every other non-GET is aborted after hand-over too, and only CAPTCHA-endpoint requests with no planned value in them may pass, 4.3.) | **failed**, `after_click=0`. Nothing that could carry the application left the browser. If the route aborted a POST to another path or host, the note says "The form tried to send to an address the app doesn't recognize, so the app stopped it. Nothing was sent. Apply from the posting instead"; otherwise client-side validation stopped it, and the first visible error is recorded. This is the only row that may say "nothing was sent" after hand-over. |
| Anything else: the submit POST passed but answered 5xx or never answered, a main-frame navigation (including to the confirmation path) without a submit POST, a "thank you" text with the form still present | **unconfirmed**, "Your application may have been sent, but Greenhouse did not show its confirmation page. Look for its email" |

Greenhouse lets each employer edit the confirmation page's wording **[doc]**. A page that says
"Thank you for applying" at the same URL with the form still present is therefore
**unconfirmed**, and a fixture test pins it (12.4). The table is tested row by row in the default
suite, without a browser (12.3).

### 6.15 Record the result

1. Take the final screenshot (masked) if hand-over happened or the outcome is `needs_you`, as in
   outreach/forms.py:1024-1027.
2. In one transaction:
   - settle the claim (5.2 rule 5) with the state, note, `submitted_at`, `after_click` and
     `resolved_by`;
   - finish the run row: outcome, reasons, evidence, screenshots, requests;
   - on `submitted`:
     - set `verification` to `awaiting_email` with `watch_until = submitted_at + 24h`, or to
       `not_watched` when the student's D12 answer does not require the watch for this mode and
       it is not available;
     - insert `application_events(event_type='apply_agent_submitted', detail_json={run_id, mode,
       confirmation_path, screenshot_sha256, post_status, security_code})`.
3. **Stage change**, on `submitted` only, by the claim's `stage_policy` (5.2 rule 8). It is
   forward-only.
   - **`record`:** in one transaction: `UPDATE applications SET updated_at=updated_at WHERE id=?`
     (the lock), re-read the stage, and only if it is still `applying` call
     `_update_application_tx(stage='applied', applied_at=submitted_at,
     source='apply_agent:confirmation_page')`. `_update_application_tx` re-reads the row under the
     lock but does not compare it with anything (applications/actions.py:567-625), so the caller must: a stage
     the student changed meanwhile wins. Then set `stage_recorded=1`. This is a student action,
     because they confirmed. It is not a ledger action, so pause does not block it once the
     submission has happened.
   - **`ask`:** no change. The card asks "Greenhouse showed its confirmation page. Mark as
     applied?", and that button (a browser-session route) calls the same write with
     `source='apply_agent:student_confirmed'`.
   - **`ledger` (unattended, M8):** `automation.perform(feature="auto_apply",
     action_type="application.stage", subject_kind="application", subject_id=app_id,
     after={"stage": "applied", "only_from": "applying", "applied_at": submitted_at},
     evidence={...claim, run, screenshot hash...}, summary="Applied to {title} at {company}
     (Greenhouse showed its confirmation page)", basis="confirmation_page", confidence=None,
     idempotency_key=f"apply:{token}", auto=True)` (automation/ledger.py:1452-1585).
     `ApplicationStage.effective` honours `only_from` (automation/ledger.py:740-749).
     - `perform` returns None while paused. The submission has already happened, so the claim
       keeps `stage_recorded=0`, and `recover_stale` retries with the same idempotency key after
       the student resumes. This is the Phase 1 `awaiting_resume` pattern
       (applications/inbox.py:111-114).
     - The card says "Submitted. Applied will be recorded when you resume automation".
4. Write a notice through `automation.notice`, with no field values and no links (as in
   applications/inbox.py:108). For example: "Greenhouse showed its confirmation page for your
   application to {title} at {company}", or "{company}: your application needs you".
5. Close the browser, unless a wait is running.

### 6.16 The 24-hour confirmation watch

`apply_runs.watch(conn, user_id, now)` runs on every AutomationWorker pass, every 60 s
(outreach/automation.py:333+), for every student `students_to_watch` returns (5.6). It is a
database query only.

**Which claims it looks at:**

- `submitted` claims with verification `awaiting_email` or `no_email_24h` and `submitted_at` in the
  last 14 days;
- uncertain attempts: `unconfirmed`, `needs_you` or `failed` with `after_click=1`, and `released`
  tombstones with `handed_over_at`, all with `handed_over_at` in the last 14 days. This is how an
  email resolves an uncertain attempt (5.2 rule 6).

**What confirms (a strong match), and nothing else does:** an `application_mail_messages` row
(migrations/0038:50-66) with:

- `application_id = claim.application_id`;
- `kind = 'application_confirmation'`;
- `state IN ('done', 'awaiting_resume')`;
- `matched_by IN ('job_id', 'company_title')`: not `company_single`, which Phase 1 accepts for a
  confirmation (applications/inbox.py:1036) but which means only "the one open application at this
  company";
- `sender_verified = 1` (5.1), PLAN.md decision 11's "authenticated sender plus a strong match";
- `received_at >= handed_over_at - 5 minutes`;
- `subject` not matching `/security code/i`. Greenhouse's code email is reportedly titled
  "Security code for your application to …" **[1-src]**, and it must never count as a
  confirmation.

**Weaker evidence, which never confirms:** the same row but `matched_by='company_single'`, or a
row with `application_id=''` from a Greenhouse sender (application_senders.json: `greenhouse.io`,
`greenhouse-mail.io`) whose subject contains every company token. It sets
`detail.possible_email_at`, and the card adds "An email from {company} arrived on {date}; it may be
for this application." It does not stop the clock, and it does not count for 8.8.

**What happens:**

- A strong match on a `submitted` claim: verification `email_confirmed`, `verified_at`, and a
  timeline event `apply_agent_verification`.
- A strong match on an uncertain attempt or a tombstone: `state='submitted'`, verification
  `email_confirmed`, `resolved_by='email'`, `submitted_at` = the email's time, and a notice
  "Greenhouse confirmed your application to {company} by email". If `stage_policy` is `record` or
  `ledger`, the forward-only stage write of 6.15 runs, unless Phase 1 already moved the stage (it
  may have, with `application_mail` on); either way the claim is reconciled rather than left
  "may have been sent". If it is `ask`, the card asks.
- **The clock.** When `now > watch_until` with no strong match, the claim moves to `no_email_24h`
  **only if the mail reader was working for the whole window**: `application_mail_sync.last_ok_at
  >= watch_until`, `pending_ids_json` is empty, and `recovery_state` is '' (migrations/0038:21-38).
  Otherwise it stays `awaiting_email`, `watch_until` is extended until the reader catches up, and
  the card says "Looking for its email: paused, {reason}" (for example "Gmail needs
  reconnecting", from the existing health summary). A stalled watch never counts for 8.8.
- `no_email_24h` adds a notice and an Urgent row, and puts the badge on the card (10.5). A later
  strong match still moves it to `email_confirmed`.

**What the absence means.** Greenhouse lets employers turn the confirmation email off **[doc]**,
so `no_email_24h` means only that no email came. The card never says the application failed and
never suggests applying again, which could send a duplicate.

**Why read the mail table rather than the ledger.** After an agent submission with
`stage_policy='record'`, the application is already `applied` with `applied_at = submitted_at`.
Phase 1's confirmation handling then plans `stage_change(current)`, and `_next_applied_at` keeps
the earlier time (applications/actions.py:544-566). So the email changes nothing, and no ledger row is
written. The corroboration can only be read from `application_mail_messages`.

**As built (M5b part 1).** `opportunity_app/apply/watch.py` holds the watch, the card states, the statistics and the
card's two answers; `apply/security_code.py` holds the D10 B reader. Decisions the spec left open:

- The watch is passed to `apply_runs.run_worker_step(watch=...)` by the AutomationWorker, because the watch imports
  `apply_runs`. Its health row is `apply_agent.watch`.
- Phase 1 now writes `application_mail_messages.sender_verified` (inbox.py), which the strong match needs. Greenhouse's
  "Security code for your application to ..." from Greenhouse's own senders is never read as a confirmation, and never
  sent to a model: `mail_rules.classify` decides it as `unknown` before TypeSafe is asked, so the code in its body is
  not sent or stored (R4; pinned in `tests/fixtures/application_mail_eval.json`, `security_code`). The classifier rule
  is that narrow on purpose (a role titled "Security Code ..." keeps its offer or interview label); the watch's own
  exclusion is the broad `/security code/i`.
- A stall is added to `watch_until` when it ends, not minute by minute, so `last_ok_at >= watch_until` can hold.
  `no_email_24h` also needs a pass that began after the deadline to have finished. A watch whose reader stays stalled
  until 13 days after the submission, or whose extended deadline would pass day 13, becomes `not_watched`
  (`detail.watch_stopped = 'reader_stalled'`); one still awaiting its email when its 14 days end becomes `not_watched`
  too (`'window_ended'`). Neither counts. The reader is not "working" while an email it set aside unread (state `error`)
  falls in the window, or while the Gmail account read is not the application's address: the watch pauses.
- One email confirms one attempt (`detail.email_gmail_id`), the newest attempt first. A tombstone whose application or
  job has a newer live attempt is not flipped; the student gets a notice instead.
- Weak evidence also covers a strong-tier email from an unverified sender.
- The Urgent kinds are the code's `apply_needs_you` and `apply_no_email`.
- The security-code reader reads Gmail directly (Phase 1 passes are ten minutes apart), at most every 15 seconds, and
  needs the read scope and the address match of D12, not the `application_mail` switch. Handing the code to the child is
  recorded as `handed`; only the child's acknowledgement that it typed it (`{"op": "security_code_result", "typed": true}`,
  the reader's `confirm_typed`) records `typed` (and the notice and the statistic). A result with `typed: false` records
  the fallback with its reason, and `abandoned` (the agent stopped waiting) drops whatever a look still running finds. A
  repeat request for a handed, never-acknowledged code falls back to the student (`not_confirmed`), and a window that
  ends while Gmail could not be read says so (`gmail_unreachable`), not "no email".
- The reader's record is `detail.security_code_reader`; `detail.security_code` stays the boolean 6.14 settles with, so
  the shallow merge in `runs.settle` cannot erase the count. The statistics count a prompt from either.

---

## 7. Eligibility policy

Section 6 checks these rules in `apply_policy.build_plan` and in the preflight. The rules are
exact, and there is no scoring.

### 7.1 Where a value may come from

A field may be filled only from one of these sources. Anything else is `source.kind = 'none'`.

| Source kind | What | Match rule |
| --- | --- | --- |
| `profile` | A confirmed fact from `preparation.confirmed_facts`, looked up as content.js:38-46 does (`contact.email` reads `facts["contact"]["email"]`) | Only through the **submit-mode mapping list** below. |
| `ats_label` | `apply_ats_labels` row | The field kind equals the row's `field`, and the option text on the page equals the label exactly. |
| `answer` | `answer_library` row, any age | The question key equals the row's key; **the company rule** and the one-answer rule (below). |
| `sensitive` | `apply_sensitive_answers` row (D5 B to E only) | The category is allowed by D5, the key is equal, the `company_key` is '' or this company, `consent_scope` covers the mode, and `consented_at` is set. |
| `resume` | `apply_policy.resume_for` (6.9) | The pick in force (confirmed version), or the most recently confirmed résumé when there is no pick. Never when the pick is `unsure`. |
| `cover_letter` | The latest cover-letter version for this opportunity, if approved (6.9) | D11. |

**The company rule, for every field kind.** A saved answer is used only when the row's
`company` equals this company by `pipeline.identity_tokens`, **or** the row carries the tag
`reusable`, which the student sets (10.3, and in the answer library). Many questions share wording
but have a company-relative truth ("Have you previously worked here?", "Who referred you?"), so
an answer saved at one employer is never assumed true at another. The extension saves answers
with the company attached (sidepanel.js:212); preparation answers saved with company '' carry over
only once the student marks them reusable.

**Context-dependent questions are never reused across companies,** even when tagged reusable. A
key is context-dependent when:

- it has fewer than 3 words;
- it starts with "if yes", "if so", "if no", "if other", "please specify", "please explain",
  "please describe", "other" or "explain";
- or it matches `previously (worked|been employed|applied)|worked (here|for us|for this
  company|at)|applied (here|before|previously)|referr|who referred|know (anyone|someone)|how did
  you hear|where did you (hear|find)|current(ly)? (an )?employee|related to|spouse|immediate
  family|former employee|employed here|relations? working` (and the other wordings in
  `CONTEXT_WORDING`, which the engine and `apply_policy` share).

A leading "Follow-up:", "Follow-on question:", "Sub-question:", "(Optional)", "Question no. 3:" or
a number or letter is taken off, in any order and up to six times, before the opener test.
`tests/fixtures/apply/context_keys.json` is run by both the engine and `apply_policy`, so the two
cannot drift apart.

**As built (2026-09-30), a deliberate deviation from the company rule above: Apply for me v1 does no cross-company
reuse.** No classifier can tell every personal, legal or agreement question from an ordinary one (three review rounds
each found wordings the lists missed), so an `answer_library` row is used by `build_plan` only when its `company`
equals this company by `identity_tokens`. The `reusable` tag is ignored there: a reusable row saved at another
company is a row "saved elsewhere" and fills nothing (`_saved_answer`). The what's-missing view no longer offers "Use
for any company" (`apply_preflight._action` has no `reusable_allowed`, and `app.js` shows "This answer is saved for
this company only"), and `answer_missing` refuses `reusable=True`. Confirmed profile facts (name, email, phone, links,
`name_parts`) are unaffected. Nothing else changes for the extension, which still proposes a reusable row for
review on an ordinary question (10.3, 7.3 "As built"). Restoring reuse later is a small change (read the tag again in
`_saved_answer` and offer the tick again) and needs the student's say-so first.

For a sub-question that starts with one of those openers, the key is prefixed with its parent: the
nearest fieldset legend, or else the preceding schema question's label (`"{parent} / {question}"`),
so "If yes, please explain" under two different questions are two different keys. The Needs you
flow saves such answers for this company only and hides the reusable tick.

**A follow-up is as sensitive as its parent.** A field whose own words continue another question
("If yes, please explain", "Please provide details") and that is filed under its parent is classified
on its parent's own category too (7.3). A short question that stands alone ("LinkedIn Profile", "GPA")
inherits nothing, even when it sits below a sensitive one: "If yes, please explain" under "Have you ever been convicted
of a felony?" is `uncategorized`, and under a sponsorship question it is `sponsorship`. It is
never saved to the answer library or filled from it. The category travels down a chain of
follow-ups: "If yes, when?" under that follow-up is `uncategorized` too.

The exception is a parent that is `uncategorized`, `export_control`, `sponsorship` or `salary`: any
question filed under such a parent (a short or repeated question) inherits its category whatever its
own wording, and so does a short phrase that asks about no one ("Nature of charge"). "Year",
"Location", "Expiration date" and "Type" under a felony or visa question therefore fail closed. Only
the profile fields ("LinkedIn", "GitHub", "Website") keep their own source. A field whose own words
also ask a voluntary self-identification question, under work authorization, sponsorship or 18 or
older, is `uncategorized`, and the store refuses such a wording under any kind but EEO.

**The one-answer rule.** If several `answer_library` rows share the key and, after the company
rule, have different answers, it is a problem: "You have two different saved answers for
"{question}". Keep one". A row saved for this company is the student's answer for this
company and wins over a `reusable` row saved elsewhere, so answering a question in the "what's
missing" view settles it; the rule applies within the tier that wins (this company's rows, else
the reusable ones). The problem's action opens the answer library, where the student keeps one.

**The submit-mode mapping list** lives in the adapter. Adding to it is a code change with a test.

- `first_name`, `last_name`:
  - the confirmed `name_parts.first` and `name_parts.last`;
  - otherwise the confirmed `name`, **only if it is exactly two words**;
  - otherwise the field is a problem: "Add your first and last name for applications in your
    profile".

  `name_parts` is a new profile field, `{first, last, preferred}`, added to `ALLOWED_PROFILE_FIELDS`
  and `validate_profile_types` (student/profile.py:16-48, :409-411), to the profile UI as "Name for
  applications", and to a SETUP.md step. Today the profile has only one `name` text field, and
  `update_profile` refuses any other key. Splitting a longer name is a guess. content.js:43-44
  splits by whitespace for the extension, where a person reviews every field; the agent does not.
- `preferred_name`: the confirmed `name_parts.preferred`, if any.
- `email`: the confirmed `contact.email`.
- `phone`: the confirmed `contact.phone`. The phone-country select uses `ats_label: phone_country`.
- A custom question whose question key is in:
  - `{"linkedin", "linkedin profile", "linkedin url", "linkedin profile url"}` gets
    `contact.linkedin`;
  - `{"github", "github url", "github profile"}` gets `contact.github`;
  - `{"website", "portfolio", "personal website", "portfolio url", "website url"}` gets
    `contact.portfolio`.
- Location, school, degree, discipline and education dates use `ats_label`.
- `resume` gets `resume_for`; `cover_letter` gets D11.

The profile also holds `work_authorized_us`, `us_citizen` and `requires_sponsorship`. They are
**not** mapped: sensitive answers come only from the consent store (D5).

**The label-pattern mappings in `adapters.js` (adapters.js:12-29) are not used by the agent.**
They are regexes: `/\b(degree|program|major|field of study)\b/` would map "What program did you
hear about us from?" to the student's degree. They stay in the extension, where a person reviews
each field. The agent uses the exact list above. This extends PLAN.md 5.3's "fuzzy matches
disabled" to mappings.

**The fuzzy saved-answer tier is never used by the agent.** That is `matchAnswer`'s word-overlap
tier at confidence 0.7 (content.js:96-110). Only exact equality of keys counts.

### 7.2 Rules by field kind

| Row | Field | Rule |
| --- | --- | --- |
| T | Short text (`input_text`) | Mapping list, or an exact answer under the company rule. |
| X | Long text (`textarea`, free-text essays) | An exact answer under the company rule. The text must fit the control's `maxlength`, and read-back is exact. Otherwise it is a problem, "No saved answer for this company". |
| S | Single select, radio | The answer text must equal exactly **one** option **label** after normalization. Option values are never matched: a hidden value such as "1" or "0" says nothing about which label it stands for. Zero or several matches is a problem. |
| M | Multi select | The answer is split on newlines or ";". Each part must equal exactly one option label. The order does not matter. |
| C | Checkbox (non-consent) | An exact answer under the company rule that is "yes"/"true"/"checked", or "no"/"false". |
| A | Acknowledgment or consent checkbox | D9 A: never ticked (left for you, or a problem). D9 B: **only** `sensitive` with category `acknowledgment`/`consent` and an exact statement key; a statement that cites a document needs an entry for this company. The statement is always the whole of what the box shows: the heading, the option and the description together (a heading the option repeats is said once), never the option alone, however long it is, because a generic option such as "I have read and agree to the following" names nothing, and two boxes that agree to different things never share a stored answer. When the option (or, for a Yes/No question, its question) is under six words or points elsewhere, the statement also carries the question above it (`{parent} / `) and is saved for one company only; a statement of fewer than three words is left for the student. A Yes/No agreement question is matched on its answer key (which carries `{parent} / ` for a follow-up, a short question or one the form repeats) and its description. A checkbox, or a Yes/No question, is read on its heading, its option text and its description together: `acknowledg`, `terms` or a privacy statement, notice or policy in any of them (and `agree`, `accept`, `policy`, `certif`, `read`, `reviewed`, `understood`, `abide`, `bound` or `received` on a checkbox, and `accept the`, `abide` or `bound by` on a Yes/No question) makes it an acknowledgment, so it never gets the ordinary answer form or the reusable tick. |
| E | Anything sensitive (7.3) | **Only** `sensitive` (D5 B to E). It is never filled from `answer`, `profile` or `ats_label`. Required and not in the store: a problem in a submit, left for you in a handoff. Optional and not in the store: left blank (D13). Category `uncategorized`: never filled. |
| F | File: résumé | `resume_for` (6.9), only for the field named `resume`. An `unsure` pick opens the chooser. |
| L | File: cover letter | D11, only for the field named `cover_letter`. |
| H | `input_hidden` | Never touched. |
| U | Anything else unrecognized | A problem if required; left blank if optional. This includes every other upload (a transcript, a writing sample, a custom "Cover letter" question): the résumé is never the answer to one. |

**Legacy saved answers.** Rows saved by the extension before M1 contain field ids in their
question text, so they will not match exactly. That is intended. The first run lists them as
missing answers, and the student confirms each once, which saves an id-free row. No automatic
cleanup rewrites old rows.

### 7.3 What counts as sensitive

`apply_policy.classify_sensitive(question, options, section, field_name)` returns a category, the
explicit value `"uncategorized"` (sensitive, never storable), or None (not sensitive). It works on
the normalized question, case-insensitively, in this order:

1. **Never-storable first, for every section.** If the question (with any 18-or-older phrase
   removed first, so "at least 18 years of age" is not caught by `\bage\b`) matches the
   never-storable pattern, the result is `"uncategorized"`.
2. **Section rules.**
   - `compliance` and `demographic_questions`: the EEO fields map to `eeo_*` **by their schema
     field names only** (the EEOC names in the fixture schema: gender, Hispanic ethnicity, race,
     veteran status, disability status; confirmed against a live schema before M4). Any other field
     in these sections is `"uncategorized"`: this is where employers put sexual orientation,
     pronouns and age range.
   - `data_compliance`: `consent`.
3. **Question patterns.** Collect every category below whose pattern matches. If several match,
   the most restrictive wins, in this order: `uncategorized` > `export_control` > `salary` >
   `sponsorship` > `work_authorization` > `age_18` > `eeo_*` > `acknowledgment` > `consent`. So
   "Are you a U.S. citizen or authorized to work in the U.S.?" is `export_control`, not the more
   permissive `work_authorization`.
4. **Options fail closed.** A select or radio whose option labels match
   `visa|citizen|clearance|green card|permanent resident|h ?1 ?b|\bopt\b|sponsor` is sensitive
   even if its question is vague (the pattern also reads visa status lists such as `f ?1|j ?1|cpt`);
   its category is the most restrictive of the option matches and the question's own. Options that include "Decline to self-identify" or "I don't wish to answer"
   make the field `"uncategorized"`, unless step 2 already mapped it to an EEO category by name.
5. **Superset check.** Anything the extension's `SENSITIVE` regex (content.js:6, repeated in
   applications/extension.py:24-29) matches, and steps 1 to 4 leave as None, is `"uncategorized"`. For
   example "Do you have authorization to work in the US?" matches the extension's
   `authori[sz](?:ed|ation)` and is caught by step 3's `authori[sz]ation to work`; anything like it
   that no row places can never be answered, but is never treated as ordinary either. A test runs
   both over the shared vector file and fails if the extension flags something this classifier
   returns None for. The agent must never be looser than the extension.
6. **Mirror into the extension (M1).** The extension's `SENSITIVE` gains the terms added here that
   it lacks (immigration status terms, clearance, 18-or-older phrases, felony, criminal, convict,
   non-compete), so the student stops saving answers to those questions in the general library,
   from which rows T and S could otherwise fill them.

| Category | Pattern (on the normalized question) |
| --- | --- |
| work_authorization | `authori[sz]ed to work\|authori[sz]ation to work\|work authori[sz]ation\|legally (eligible\|authori[sz]ed)\|right to work\|eligible to work\|legally ((able\|permitted\|allowed) to )?work\|eligib\w* (for\|to) (employment\|work)\|work permit` |
| sponsorship | `sponsor\|immigration\|petition\|employment based\|visa (sponsor\|status\|support\|type\|holder\|transfer)\|(require\|need\|hold)\w* (a )?visa\|work visa\|student visa\|\b(f ?1\|j ?1\|h ?1 ?b\|tn\|e ?3)\b\|\bstem opt\b\|\bopt\b(?! (in\|out))\|\bcpt\b\|practical training\|type of visa\|\b(hold\|have\|has\|current\w*\|which) ((a\|an\|your\|any\|the) )?(\w+ )?visa\b` |
| age_18 | `\b18 (years )?(or older\|of age)\|over (the age of )?18\|at least 18\|age of 18\|(are you\|you are\|must be) 18` (\"eighteen\" is read as 18, and \"18+\" has lost its plus sign by then) |
| export_control | `u s person\|us person\|itar\|export administration regulations\|export control\|citizen\|permanent resident\|green card\|clearance\|nationalit\|\b(u s\|us\|united states\|american) national\b\|\bnational of\b` |
| eeo_gender | `\bgender\b\|\bsex\b` |
| eeo_hispanic | `hispanic\|latin[oax]` |
| eeo_race | `\brace\b\|ethnic` |
| eeo_veteran | `veteran\|military\|armed forces` |
| eeo_disability | `disab` |
| acknowledgment | `i (certify\|attest\|acknowledge\|confirm\|understand\|agree)\|accura\|truthful\|have read\|privacy (notice\|policy\|statement)\|acknowledg` (and, for a checkbox or a Yes/No question, the words in 7.2 row A) |
| consent | `consent\|retain\|retention\|process(ing)? (of )?(my\|your) (personal )?(data\|information)\|gdpr` |
| salary | `salary\|compensation\|pay (expectation\|range)\|desired pay\|expected pay\|hourly rate\|wages?\b\|base pay\|pay rate` |
| *never storable* (`uncategorized`) | `\bage\b\|birth\|pronoun\|marital\|religio\|genetic\|pregnan\|criminal\|convict\|felony\|misdemeanor\|arrest\|background check\|sexual\|transgender\|non ?compete\|crimes?\b\|offen[cs]es?\b\|lgbt\|queer` |

Changes from revision 1, each pinned by a vector: `ear\b` (which matched "hear" and "year") is
replaced by "export administration regulations"; bare `visa` (which matched the company Visa) is
tied to sponsorship wording; `authori[sz]ation to work` is added.

Changes after the M4 review, each pinned by a vector: everyday wordings of work authorization ("legally
work", "eligible for employment", "work permit"), visa ("type of visa", "which visa", "have a visa"),
nationality, criminal history ("crime", "offence", LGBTQ), wage and base pay, "18+" and "eighteen", and
military service or the armed forces are placed, and the same terms (apart from the acknowledgment words,
which the extension deliberately leaves to its own consent rule) are mirrored into the extension's
`SENSITIVE` and `extension_apply.SENSITIVE_FIELD`. A vector may also carry `parent` (a follow-up's
own words come back as its parent's category), `control` (`checkbox`) and `description`.

**As built: the broad net (after M4s, 2026-09-30).** Every review round found wordings the lists above miss
(the student's own "visa's", a follow-up such as "Please tell us what happened", a work-authorization question worded
"Are you permitted to work in the United States?", an agreement box worded "I will comply with the Code of Ethics"),
so the precise classifier is not the only guard. A second, deliberately wide reading sits beside it:
`apply_policy.net_topics` and `possibly_sensitive` (repeated as `netTopics` and `possiblySensitive` in
`apps/extension/apply-engine.js`, pinned by `tests/fixtures/apply/broad_net.json`, run by both suites). It is one list
per topic (immigration, work authorization, criminal, demographic, money, security, agreement, employer-relative), each item
a topic word or short phrase, never a sentence shape. It never marks a question sensitive and never picks a category; it
only tightens what may be done with a question the classifier called ordinary:

- **Safety does not rest on the net.** Apply for me never carries an answer from one company to another (7.1 "As
  built") and never fills a checkbox, or an agreement, from the answer library (below), so a wording the lists miss can at
  worst be saved by the student for that one company. The net is a best-effort refusal on top.
- A question that hits the criminal, demographic (apart from an 18-or-older wording and the EEO decline path), money or
  security topic, or follows one (its parent, when the child follows it by the follow-up rules, the parent is precisely
  sensitive, or the net finds a topic in the parent; the text of a text field's description is read too), is **never
  storable**: the what's-missing view offers no form and says why, `answer_missing` refuses it, the plan never fills it
  from the answer library, even at the same company, and the extension's Save (`POST /api/v1/extension/answers`) refuses
  it. A select's option labels are read for the status topics and for narrow pay, clearance, race and pay-range phrases
  ("Asian", "White", "$40,000-$50,000"). The lists are best-effort: they do not catch every wording (a fresh one is
  at worst saved for one company), and the extension's Save can only judge the question in front of it plus the chain
  of follow-ups above it (below).
- **No checkbox or agreement control is filled from the answer library** (D9 B). A checkbox, single or a group, never is;
  nor is a select or multiselect whose option labels, heading or description agree to, accept, acknowledge, consent to,
  certify, attest or confirm something (an agreement word in the heading alone is enough: "Do you certify that your
  answers are true?" with the options "Yes I do" and "No I do not"), a Yes/No-shaped question that hits the agreement
  topic, or a typed signature or typed initials ("Type your initials to agree"). Only an
  exact sensitive-store statement ticks or chooses it; otherwise it is left for the student.
- **Every stored statement and tick-box entry is per company** (C). An acknowledgment or consent statement, and any
  work-authorization, sponsorship or 18-or-older entry whose `answer_kind` is a tick box, is typed text, or whose question or
  option also agrees to something, is refused for "any company" by `add_entry`, is not offered for it, and is ignored at
  other companies when a row says otherwise (`lookup`). Any-company scope remains only for a select's exact option label
  for those three kinds, and for an EEO decline. No word list decides what a statement names, so even a plain "I certify
  that my answers are true" is one company's.
- A work-authorization, sponsorship or 18-or-older entry whose statement (heading, option, description) or stored answer also
  hits criminal, demographic, money or security is refused, and the field is read as a personal question. An
  acknowledgment or consent that merely names such words ("EEOC poster") is an agreement and stays storable for one company.
- Ordinary prompts are not over-read: "take charge of a project", "in a sentence" or "in 2-3 sentences", "network
  security", "security tools", "exporting data" and "hourly availability" are removed before the topics are read
  (`NET_BENIGN`, repeated in the engine and pinned by the same vectors). "Security clearance", "export control",
  "charged with" and "hourly rate" are untouched.
- In the extension, `mayUseAtCompany` still carries a reusable row for an ordinary question, but never onto a field that
  hits the net (its question, its help text read from `aria-describedby`, or for a select its option labels, or a select
  whose options or heading agree to something, or a typed signature or initials), nor onto a field that follows one that
  does. "Follows" is a chain, read in page order by `netReadings`: the field right after a hitting field always
  follows it, and a field that is itself short or follow-up shaped (under six words, a follow-up wording, or one that
  opens with a question word) passes the chain on, so "Year it happened" and then "Please tell us what happened" both
  follow a felony question, as do "Which type?" and then "What is the expiration date of your current status?" under a
  visa question. This mirrors the `own` chain in `build_plan`. A follow-up-shaped field in a chain under a never-storable
  question is marked `never_storable`, so the panel offers no Save for it; an independent question after one is still
  offered. A checkbox is never pre-ticked from a row saved at another company (an option row never travels).

The net over-reads on purpose ("Would you like to opt in to updates?" is immigration wording to it, and a question that
only comes after a sensitive one takes that one's topics). Over-blocking costs some reuse; under-blocking is the bug. It
does not replace the classifier: what the net alone catches is company-only or left to the student, never answered from
the store.

`tests/fixtures/apply/sensitive_vectors.json` holds, with the expected result:

| Question | Expected |
| --- | --- |
| How did you hear about us? | None |
| Expected graduation year | None |
| Why do you want to work at Visa? | None |
| Would you like to opt in to text messages? | None |
| Do you have authorization to work in the US? | work_authorization |
| Are you a U.S. citizen or authorized to work in the U.S.? | export_control |
| Will you now or in the future require sponsorship for an H-1B visa? | sponsorship |
| Are you currently on F-1 OPT or STEM OPT? | sponsorship |
| Do you hold an active security clearance? | export_control |
| Are you at least 18 years of age? | age_18 |
| What is your age? | uncategorized |
| Have you ever been convicted of a felony? | uncategorized |
| Are you bound by a non-compete agreement? | uncategorized |
| (demographic section) Sexual orientation | uncategorized |
| (demographic section, EEOC name) Gender | eeo_gender |
| (data_compliance) any statement | consent |

#### EEOC field names, confirmed live 2026-09-29 (before M4)

Read-only GETs of the public Job Board API (`boards-api.greenhouse.io/v1/boards/{board}/jobs/{id}?questions=true`),
3 jobs each on 10 boards (8 answered; 2 tokens 404'd). **[live]**

- `compliance` entries have `type: "eeoc"`. Their field names on every board that had them (6 of 6):
  `gender`, `race`, `veteran_status`, all `multi_value_single_select`. `hispanic_ethnicity` and
  `disability_status` did not appear as compliance field names in this sample; "Hispanic or Latino" was an
  option of `race`. The classifier still accepts all five EEOC names (gender, race, hispanic_ethnicity,
  veteran_status, disability_status), since the legacy form uses the other two; any other name in these
  sections stays `"uncategorized"`.
- Decline options differ by field and board: "Decline To Self Identify" (race, gender) and
  "I don't wish to answer" (veteran_status). Under D5 C(i) the stored answer must match the exact option
  label on the form (7.2 row S), so one decline entry per distinct label is needed; the "what's missing"
  view should show the label the form offers.
- `demographic_questions` (the newer survey) is an object `{header, description, questions}`. Each question
  has a numeric `id`, `label`, `required`, `type`, and `answer_options` of `{id, label, free_form,
  decline_to_answer}`. There are **no field names**, so under 7.3 step 2 every one of them is
  `"uncategorized"` and is left for the student, including required ones labelled "Gender",
  "Veteran Status" and "Disability Status", and optional ones such as LGBTQ+ membership.
  **Open (for the student):** each option carries a `decline_to_answer` flag. Under D5 C(i) the app could
  pick the flagged option for these questions too. That would be a spec change, so v1 does not do it.

### 7.4 Hard stops (never filled around)

- A picture CAPTCHA or challenge; the security code (D10); a "verify your email" page after submit.
- Any page that asks to sign in, create an account, or use MyGreenhouse.
- A second page or a multi-step flow.
- A main-frame redirect off the Greenhouse hosts.
- For submit and handoff: no `submitPath` or `confirmationPath` in the page (6.2), or a board that
  uploads files as soon as they are attached (6.9).
- A required field the independent check (6.10) cannot confirm (submit).
- A posting that is closed, an application not at `applying`, a confirmation already on file, a
  live claim for the application or the job.
- Caps, company limit, spacing, or rehearsal gate not met (submit; caps also for handoff).
- The plan changed since the confirm, or the confirmed rehearsal is 15 minutes old or more.
- Page text is never an instruction. No model reads the page, and the adapters are deterministic,
  so a question that says "ignore previous instructions" is just a field with no saved answer.

### 7.5 The eligibility truth table (for tests)

`tests/test_apply_policy.py` and `tests/test_apply_runs.py` run every row with no browser.

- **Plan** is the outcome of `build_plan` plus the preflight, for a submit run.
- **Handoff** is what Finish in browser does.
- **Submit** is whether a submit run may press Submit.

| # | Situation | Plan | Handoff | Submit |
| --- | --- | --- | --- | --- |
| 1 | All required fields from the mapping list and exact answers for this company, résumé resolved, gate met | ready | runs, nothing left | yes |
| 2 | A required custom question with only a 0.7-overlap answer | needs_you (missing answer) | runs, field left for you | no |
| 3 | A required question whose saved question contains `question_123` ids | needs_you (missing answer) | runs, left for you | no |
| 4 | Required work authorization, store entry, category allowed (D5 B+) | ready | runs, filled | yes |
| 5 | Same, category not allowed by D5 (including D5 A) | needs_you | runs, left for you | no |
| 6 | Same, answer in `answer_library` only (not the store) | needs_you | runs, left for you | no |
| 7 | Store entry for another company | needs_you | runs, left for you | no |
| 8 | Privacy acknowledgment, D9 B, store key equal, entry for this company | ready | runs, ticked | yes |
| 9 | Privacy acknowledgment, wording differs by one word | needs_you | runs, left for you | no |
| 10 | Required salary, D5 not E | needs_you | runs, left for you | no |
| 11 | Optional EEO, no store entry | ready, left blank | runs, blank | yes |
| 12 | Required textarea, exact answer saved for another company, not `reusable` | needs_you | runs, left for you | no |
| 13 | Same, tagged `reusable` | ready | runs, filled | yes |
| 14 | Two different saved answers with the same key | needs_you | runs, left for you | no |
| 15 | Select answer matching two options | needs_you | runs, left for you | no |
| 16 | Résumé pick `unsure` | needs_you (chooser) | runs, résumé left for you | no |
| 17 | Required cover letter, none approved | needs_you (Draft one) | runs, left for you | no |
| 18 | Required cover letter, latest version approved | ready | runs, attached | yes |
| 19 | Name "Ana María de la Cruz", no `name_parts` | needs_you (name) | runs, names left for you | no |
| 20 | School typeahead, no `apply_ats_labels.school` | needs_you (Look up options) | runs, left for you | no |
| 21 | Label-regex mapping would fill a "program" question with the degree | not used; missing answer | runs, left for you | no |
| 22 | Everything ready, 2 clean rehearsals, gate is 3 | ready (rehearsal only) | runs | no |
| 23 | Everything ready, company applied 12 days ago, limit 30 | needs_you (override offered) | only with the override tick | only with the override tick |
| 24 | Everything ready, previous hand-over 6 minutes ago, spacing 10 | needs_you (time given) | refused (time given) | no |
| 25 | Stage `applied` | failed | refused | no |
| 26 | `application_confirmation` mail already on file | failed | refused | no |
| 27 | Claim `unconfirmed` | failed (resolve first) | refused | no |
| 28 | Hidden (CSS) field the plan would fill | needs_you | refused | no |
| 29 | Required DOM field not in schema | needs_you | runs, left for you | no |
| 30 | Question text contains an instruction-like sentence, no saved answer | needs_you | runs, left for you | no |
| 31 | No résumé pick, one confirmed résumé | ready ("Your confirmed résumé") | runs, attached | yes |
| 32 | Picked résumé file has no confirmed version | needs_you | runs, résumé left for you | no |
| 33 | "Have you previously worked for this company?" saved "Yes" at another company, tagged reusable | needs_you (context-dependent) | runs, left for you | no |
| 34 | "Who referred you?" saved at another company | needs_you | runs, left for you | no |
| 35 | Short-text answer saved for another company, not reusable | needs_you | runs, left for you | no |
| 36 | Select answer equals an option's value but no option's label | needs_you | runs, left for you | no |
| 37 | "How did you hear about us?" with an answer saved for this company | ready (not sensitive) | runs, filled | yes |
| 38 | "Are you a U.S. citizen or authorized to work in the U.S.?" with only a work_authorization entry | needs_you (export_control) | runs, left for you | no |
| 39 | Demographic-section "Sexual orientation", required | needs_you (never storable) | runs, left for you | no |
| 40 | Confirmed rehearsal 15 minutes old or more | ready | (not used) | no ("Rehearse again") |
| 41 | An answer edited after the rehearsal (plan hash changed) | ready, new hash | (not used) | no (re-confirm) |
| 42 | Daily cap reached | needs_you (time given) | runs (not counted) | no |
| 43 | 21st rehearsal or lookup today, limit 20 | rehearsal refused | (not affected) | (not affected) |
| 44 | Adapter version bumped | ready (rehearsal only; gate count 0) | runs | no |
| 45 | Legacy form detected, `LEGACY_ENABLED=False` | needs_you | refused | no |
| 46 | Pause turned on after the one-click confirm (D6 B) | ready | (not used) | no; hand-over refused, nothing sent |
| 47 | Pause already on at the one-click confirm (D6 B) | ready | runs | yes |
| 48 | Pause on, unattended | (worker skips) | (not used) | no |
| 49 | Same Greenhouse job already submitted through another saved copy | failed (job lock) | refused | no |
| 50 | Released tombstone after hand-over for this job | ask (tick) | only with the tick | only with the tick |
| 51 | Unmatched company confirmation email on file | ask (tick) | only with the tick | only with the tick |
| 52 | `applying` row created more than a day ago | ask (tick) | only with the tick | only with the tick |
| 53 | No `submitPath` in the page | (browser step) | refused, nothing sent | no |
| 54 | Cover letter v2 approved, v3 draft newer | needs_you | runs, left for you | no |
| 55 | A board that uploads as you attach (`S3_UPLOAD_ENABLED=False`) | rehearsal only: résumé deferred, not clean | refused before filling | no |
| 56 | Sensitive field in a rehearsal, entry allowed | deferred: option checked, not filled | runs, filled | yes |

---

## 8. Bot detection and politeness

**What the research says.**

- Greenhouse loads reCAPTCHA Enterprise **[live]**, and each board has its own spam-sensitivity
  setting **[doc]**.
- A low score does not drop the application silently. The POST answers 428 and the form asks for
  an emailed 8-character code **[doc]**, **[1-src, two projects]**.
- Silent losses come from three other rules **[doc]**:
  - the spam blocklist rejects "at intake" by IP address, email or domain;
  - application-limit rules reject after submit;
  - one recruiter "mark as spam" auto-rejects every future application from that email to that
    employer.
- The fraud rules score a "data-center IP" as high risk, and a device timezone that does not match
  the stated location as a weak signal **[doc]**.
- A Playwright-launched Chromium can be flagged even when headed and even when a person clicks
  **[1-src, Ashby]**.
- Employers can turn off the applicant confirmation email **[doc]**, so a missing email is a weak
  signal on its own.

**The rules that follow:**

1. **No solvers, no stealth, no disguise.** A real headed Chromium, honest about what it is, with
   no user-agent, locale or timezone overrides, no automation-hiding flags, and no human-mimicking
   input (4.3). A picture CAPTCHA goes to the student. A CAPTCHA checkbox goes to the student
   unless they chose D14 B. The security code is read from Gmail under the reader rules in section 3 (D10 B), and typed by the student (D10 A) when it cannot be.
2. **Local only.** The agent runs on the student's computer, behind its normal connection. The
   SETUP step says to turn off a VPN before using Apply for me, since VPN exits are often
   data-center addresses. The app cannot detect this reliably, so it is guidance, not a check.
3. **Human-scale volume.** Default limits (D4), all enforced by 9.1:
   - 10 minutes between hand-overs;
   - 5 one-click or unattended hand-overs per day;
   - 1 per company per 30 days;
   - 20 rehearsals and lookups per day, to stay polite to Greenhouse's servers. Rehearsals are
     GET-only and do not count toward the submission caps.
4. **One exact form per run.** Go straight to the canonical hosted URL, fetch the schema once,
   and fill. There is no crawling and no retries of a submission (5.2 rule 6).
5. **Security code** keeps the claim in `clicking` while the window waits at the prompt for up to
   10 minutes, with a notice. The rate is recorded per ATS.
6. **"Verify your email" or a challenge after submit** gives `needs_you`.
7. **The 24-hour watch** (6.16) corroborates. Note its limits honestly: an employer may not send
   the email at all, and if the spam blocklist rejects at intake but Greenhouse still sends its
   confirmation email, the watch cannot tell. Whether it does is unknown (13 R2).
8. **Per-ATS threshold.** Over the last 10 agent submissions on an ATS whose watch finished with
   the mail reader working (6.16):
   - more than 1 in 5 `no_email_24h`;
   - or more than 1 in 3 security-code prompts.

   Either one sets `apply_ats_disabled:<ats>`, which disables unattended mode for that ATS, when it
   exists. One-click then shows a warning above the Submit button: "2 of your last 8 Greenhouse
   applications got no confirmation email. Some employers don't send one, but consider Finish in
   browser". The student clears it explicitly after reading why. The statistics are shown from
   M5b; the threshold lands in M6.
9. **Terms.** The MyGreenhouse User Agreement forbids automated means on my.greenhouse.io
   **[doc]**. The agent never goes there and never clicks MyGreenhouse autofill. No candidate
   terms were found for the job board itself, and that is a gap, not a clearance. The SETUP step
   and the first-use dialog say plainly: "Your name goes on every application the app helps you
   submit. If an employer treats it as spam, they may reject future applications from your
   email."

---

## 9. Modes and rollout

### 9.1 Caps and limits

`apply_runs.limits_block(conn, user_id, company_key, board_token, mode, now) -> str | None`
reads `application_submit_claims` rows with `handed_over_at` set, **in any state, including
`released`**, so an attempt that reached Greenhouse keeps counting whatever the student or the
app later concluded about it. It returns the first blocking sentence, or None:

- spacing: the latest `handed_over_at` is less than `spacing_minutes` ago;
- daily cap (one-click and unattended only): the number of one-click and unattended hand-overs
  today, in the student's timezone, is at least `daily_cap`;
- company: the latest hand-over for this `company_key` **or** this `board_token` is within
  `company_days`. One-click and handoff may carry an override tick, recorded in the claim's
  `detail_json`.

Finish in browser (`handoff`) counts toward the spacing and the company limit, because it is the
same submission from Greenhouse's view. It does not count toward the daily cap, because the
student pressed Submit.

**The job lock** is separate and has no override: the partial unique index on `job_ref` (5.2)
refuses a second live attempt for the same Greenhouse job, and a released tombstone for the job
asks first (6.0 step 4).

`apply_runs.rehearsal_block(conn, user_id, now)` counts `apply_runs` rows of kind `lookup` or
`rehearsal` started today in the student's timezone, and blocks at `rehearsals_per_day`.

### 9.2 The rehearsal gate, per ATS

`apply_runs.gate(conn, user_id, ats)` returns `(met, count, needed)`:

- `count` is the number of distinct `company_key`s among runs with `kind='rehearsal'`,
  `outcome='rehearsed'`, `clean=1`, `review='right'`, `adapter_version` equal to the current one,
  and `started_at` after `user_settings.apply_gate_reset_at:<ats>` when that is set;
- `needed` is `rehearsals_before_submit` (D3).

**Clean** (`apply_checks.clean_rehearsal`) means: no problem on a required field, no join
problem (6.5), and no planned file deferred (the upload-as-you-attach case, 6.9). Optional fields
left blank and deferred sensitive fields do not make a rehearsal unclean, because every rehearsal
defers those.

**Breaker for one-click.** If 2 of the student's last 5 reviewed runs on an ATS are marked
`wrong`, whether rehearsals or submits, the breaker writes `apply_gate_reset_at:<ats> = now`. The
gate then needs `needed` new clean rehearsals, and a notice says why. This mirrors the automation
breaker (automation/ledger.py:30, BREAKER_LIMIT 2 of BREAKER_WINDOW 5), applied to the student's reviews,
because one-click actions are not ledger actions.

### 9.3 The stages the student sees

1. **Off.** `apply_agent` is off, which is the default for every student.
2. **Rehearsal and Finish in browser** (D1 B, and until the gate is met under D1 A). The Apply for
   me button runs a rehearsal and shows the plan. The ways forward are **Finish in browser** (the
   student submits in the window) and **Mark this rehearsal right or wrong**. Under D1 B, the
   product stays here permanently.
3. **One-click** (gate met, D1 A approved, M6 merged). The plan preview also shows **Submit
   application to {company}**, a two-click confirm like outreach Send (outreach-email-preferences
   note). The first click turns it into "Submit to {company} now? This can't be taken back"; the
   second click starts the submit run with the confirm nonce (4.6).
4. **Unattended** (only if D2 says so, M8). See 9.4.

**Pause** (D6 B) stops worker-started runs and unattended mode, and a one-click submit when the
pause came after the confirm. It shows a submit that was already handed over as in flight (5.6).

### 9.4 Unattended mode (specified, not scheduled)

- **Its own rule change.** The D1 A wording covers only submissions the student confirmed one by
  one. M8 needs its own AGENTS.md rewrite, decided by the student in that PR (D2).
- **Switch.** `Feature("auto_apply", ..., "applications", "external", OFF_SHADOW_ON)`.
  `REQUIREMENTS`: `apply_agent` on; the profile's `automation.auto_apply_at` set, with no default,
  so the switch cannot turn on without it (the pattern of `auto_save_at`, the automation thresholds table in SETUP.md step 7);
  `application_mail` on or shadow with the Gmail address known; the gate met with
  `rehearsals_before_submit + 5`; the ATS not disabled by the 8.8 threshold; and, if D5 is B to E,
  each sensitive entry consented again with `consent_scope='unattended'` (entries without it are
  treated as absent).
- **Candidates.** An opportunity qualifies when:
  - the student **saved it themselves**: the latest `saved` interaction has `source='user'`, not
    `automation:*` (the `OpportunityIntent` handler marks automatic saves that way);
  - its legacy-v1 score is at least `auto_apply_at`;
  - it is identified as Greenhouse, is active, and has no claim for the application or the job;
  - `resume_for` resolves without asking;
  - the browserless plan has no problem.

  At most one run per hour, within the unattended caps (D4).
- **Shadow.** The worker runs a full rehearsal (browser, GET only) and records a ledger row with
  status `shadow` through `perform` (subject: the opportunity; `after`: the stage change it would
  make; evidence: the run id and plan hash). `can_turn_on` then applies unchanged: 48 hours, at
  least 5 shadow rows, all reviewed, none wrong (automation/ledger.py:367-406).
- **On.** The worker submits (6.13) with the pause honored at hand-over, and records the stage
  through `perform` (6.15, `stage_policy='ledger'`).
  - Undo reverts only the tracker. The UI must say so: "Undo changes your tracker only. The
    application was sent to {company} and can't be taken back."
  - The automation breaker turns the switch off after 2 undos or rejections in the last 5
    actions.
- **Visible window** (D7). It opens and fills on its own. That can startle, so the notice fires
  when the run starts: "Applying to {company} for you (you can close the window to stop before it
  submits)". Closing the window before hand-over gives `failed`, nothing sent.

---

## 10. UI

All UI is in `opportunity_app/static/app.js` and `styles.css`. It follows the existing patterns:

- an `element(...)` builder;
- `role="status"` live regions;
- two-click confirms, as outreach Send does;
- `announceWithUndo` (app.js:1858), for undoable ledger actions only.

The wording is plain and never hardcodes anything about one student.

### 10.1 Where it appears

- **Opportunity detail:** a new `applyForMeSection(item)` inserted in `renderDetail`
  (app.js:10354) right after the résumé section (app.js:10441, `resumePickSection`, app.js:10157).
- **Application card:** status badges in `createApplicationCard` (app.js:1951).
- **Automation panel:** the `apply_agent` switch with its requirement sentence, rendered like the
  other features.
- **Apply agent settings** (reached from the Automation panel):
  - the sensitive answers store (list, add with the consent tick, delete), only if D5 is B to E,
    with the EEO "store my actual answers" opt-in only for D5 C (ii);
  - the ATS option labels;
  - the limits in force, read-only, with "change these in your profile";
  - per-ATS statistics (from M5b);
  - the screenshot retention sentence.
- **Profile:** the new "Name for applications" field (`name_parts`).

### 10.2 The Apply for me section: states

| State | What the student sees |
| --- | --- |
| Not available | One sentence of why: "Greenhouse postings only, for now", or the setup requirement. No button. |
| Checking | "Checking the Greenhouse form…" while the read-only check runs (6.0). |
| Ready | The check's result: "Ready: 14 fields, all from your profile and saved answers", or "3 questions need an answer first" (10.3). Buttons: **Apply for me** (starts a rehearsal) and **Finish in browser**. Nothing in the tracker has changed. |
| Running | A live step list polled from `/runs/{id}` every second: "Opening the Greenhouse form", "Filling 14 fields", "Checking every required field", "Taking a picture of the filled form". Note: "A Chromium window is open. You can watch, but please don't type in it." For one-click: **Cancel**, active until hand-over. Polling stops when the run is finished, or when its heartbeat is more than 2 minutes old ("The app stopped during this run"). |
| Your turn in the window (handoff) | "The form is filled in the Chromium window. Complete the fields below, then press Submit application there." The "left for you" list, each with its reason. **Stop** closes the window (nothing is sent). |
| Plan ready | 10.4 |
| Needs you | 10.3 |
| Submitting | "Submitting to Greenhouse…". No cancel is offered after hand-over. |
| Result | 10.5 |

### 10.3 Needs you: one action per reason

| Reason | Inline action |
| --- | --- |
| Missing answer (non-sensitive) | The question, exactly as the form shows it, with a text box (or the option list for selects) and **Save and use for this question**. It saves an id-free `answer_library` row **for this company** and says so ("This answer is saved for this company only."). |
| Missing sensitive answer, category allowed (D5 B to E) | The same, plus the unticked consent checkbox, "Use this answer only to fill application forms that the app submits after I confirm each one". Saves to `apply_sensitive_answers` (browser session only). |
| Sensitive, category not allowed | "The app doesn't answer this kind of question for you ({category}). Finish in browser leaves it for you." |
| Typeahead label (school, location, degree) | A text box, filled in from the matching confirmed fact when there is one, and **Look up options** ("This sends what you typed to Greenhouse's lookup service"). Then the options as radio buttons, with **Use this one from now on** (saves `apply_ats_labels`). |
| Résumé pick unsure | The résumé chooser (reuses the résumé section's control). |
| Picked résumé has no confirmed version | A link to the résumé section. |
| Cover letter required, or a newer draft exists | **Draft one** or **Open the draft**, which opens the existing preparation cover-letter flow. After approval: **Try again**. |
| First and last name | A link to the profile's "Name for applications" field. |
| CAPTCHA, security code, challenge | **Bring the window forward** (if still open) or **Finish in browser**. |
| Limits | The sentence with the time or date. Company limit (one-click and handoff): the explicit override tick. |
| Duplicate asks (6.0 step 4) | The question, with its tick. The run starts only once it is ticked. |
| Form changed, or plan changed | **Rehearse again**. |
| After-click needs_you or unconfirmed | "This may have been sent." **It went through** / **It didn't go through** (disabled while the attempt is still running). |

**As built (2026-09-30):** the **Use for any company** tick on a missing answer is gone. Apply for me v1 does no
cross-company reuse (7.1 "As built"), so the tick would promise something the plan no longer does; `answer_missing`
refuses `reusable=True`. The tick on the sensitive-answer form stays, and is offered only where the store may keep an
answer for any company: a select's exact option label for work authorization, sponsorship or 18 or older, and an EEO
decline (7.3 "As built").

### 10.4 The plan preview

1. **Header:** "Here is what the app would send to {company} for {title}. Your application has
   not been submitted." Beneath it, what the rehearsal measured, and only that:
   "During the rehearsal the app blocked {n} requests that could have submitted the form or
   carried a filled-in answer, to Greenhouse or anywhere else. Sensitive answers were not put in the
   page; they go in only when you submit. To find the options for {fields}, the app sent the text
   typed into those fields to Greenhouse's lookup service; nothing else you entered left the
   browser." The last sentence is left out when no lookup ran. The counts come from
   `refused_json`; the submit-path evidence (that no request reached it) is kept in
   `evidence_json`.
2. **Screenshots:** the filled form, full page and masked per D8, as a scrollable image with a
   larger view on click. Served by an authenticated route with `Cache-Control: no-store`.
3. **Field table** (a `<table>` with a caption, for accessibility):

   | Question | Answer | From |
   | --- | --- | --- |
   | First Name | (value) | Profile: name for applications |
   | Are you legally authorized to work in the US? | Yes | Sensitive answer (you added it 2026-10-02) · filled when you submit |
   | Why do you want to work at {company}? | first 120 characters… | Saved answer for {company} |
   | Are you available to start in June? | Yes | Saved answer, reusable (first saved for {other company}) |
   | I have read the privacy notice | Ticked | Your acknowledgment for {company} · links to {address} |
   | Resume/CV | {original file name} | Résumé variant "{label}", or "Your confirmed résumé" |
   | Cover Letter | {file name}; the letter's text below | Approved cover letter, version {n} |

   - Required fields are marked.
   - Answers saved for another company and marked reusable are flagged, so the student can catch
     one that isn't true here.
   - Left-blank optional fields are listed in a collapsed "Left blank" group with the reason, and
     page-set defaults in a "Left as the page set it" group (6.10 item 4).
   - Values are looked up from `ref` on display (5.3). A changed value shows "changed since the
     rehearsal".
4. **Review:** "Was this rehearsal right?" with **Right** and **Something's wrong** (a note box).
   The answer feeds the gate (9.2).
5. **Actions:**
   - rehearsal stage: **Finish in browser** and **Not now**;
   - one-click stage: also **Submit application to {company}** (two-click confirm; the second
     label reads "Submit to {company} now? This can't be taken back"). It carries the confirm
     nonce. It is disabled once the rehearsal is 15 minutes old, with **Rehearse again**.

### 10.5 Results: the card and the timeline

**Card badges**, with plain wording and no jargon:

- `submitted`, `stage_policy='record'`, `awaiting_email`: "Applied with Apply for me · Greenhouse
  showed its confirmation page · Looking for its email until {time}".
- `submitted`, `stage_policy='ask'`: "Greenhouse showed its confirmation page. **Mark as
  applied?**", with the email line below it.
- `email_confirmed`: "Greenhouse's confirmation email arrived {date}" (after "Applied" when the
  stage is applied).
- Watch paused (6.16): "Looking for its email: paused, {reason}".
- A weak match (6.16): "An email from {company} arrived on {date}; it may be for this
  application."
- `no_email_24h`: "No confirmation email yet". The note reads: "Some employers don't send one. If
  you want to be sure, check the employer's portal or your spam folder." It never says the
  application failed and never suggests applying again.
- `not_watched`: "The app isn't checking for a confirmation email".
- `unconfirmed`: "May have been sent. Check your email or the Greenhouse portal", with **It went
  through** / **It didn't go through**. **It is never shown as Applied** (AGENTS.md, "The product's core promise is source integrity").
- `needs_you` or `failed`: the claim's note, with the matching action from 10.3.

**Timeline** (`application_events`) entries, each linking to its run:

- `apply_agent_started` (a Finish in browser or submit run created or used this application);
- `apply_agent_submitted` (with "screenshot" and "confirmation page seen" as evidence);
- `apply_agent_unconfirmed`;
- `apply_agent_resolved` (the student's answer, or the email);
- `apply_agent_verification` (email confirmed, or no email in 24 h).

Rehearsals and lookups have no application, so they are listed in the Apply for me section, not on
the timeline. Each timeline entry says who acted: "you confirmed", "you submitted in the window",
"the confirmation email" or "the app, on its own" (unattended).

---

## 11. Privacy and retention

- **Where screenshots live:** `data/private/apply/<user folder>/<opportunity_id>/<run_id>-<step>.png`.
  - `<user folder>` is the first 16 hex characters of SHA-256(user_id), so deleting an account
    removes one folder.
  - They are **not** in `output/apply/` as PLAN.md said. `data/private/` is the precedent for
    personal evidence (`outreach_forms.SCREENSHOT_DIR`, outreach/forms.py:76), and everything
    under `data/` is gitignored (AGENTS.md rule 2).
- **Masking** (D8): `page.screenshot(mask=...)` over the whole container of each field D8 names
  (4.3). It is on for every screenshot, including the ones only the student sees.
- **Retention.** `apply_runs.purge_evidence(conn, now)`:
  - deletes screenshot files older than `PIPELINE_APPLY_EVIDENCE_DAYS` (default 90) and clears
    their `path` in `screenshots_json`, keeping the `sha256` for good, as mail evidence keeps its
    hashes;
  - deletes files under `data/private/apply/` that no run row references (left by a crash
    mid-screenshot);
  - trims `progress_json` to its last step once the run is 30 days old.

  It runs **on its own** from the AutomationWorker once per local day (5.6), with its health
  recorded, because `operations.run_retention` runs only when a retention job is enqueued from
  the admin route (api.py:4600, :4623). `run_retention` calls it too.
- **Deletion.** 5.7. Account deletion removes the user folder. Row cascades remove the runs,
  claims, sensitive answers and labels.
- **Value-free records.** Values are not stored in any of these places:
  - runs, claims and events (plans keep an HMAC per value, 5.3, not a plain hash);
  - logs;
  - notices (as applications/inbox.py:108);
  - `refused_json` and `requests_json`, which hold method, host, path and status only, never a
    query string or body.

  Values exist only in the student's own tables (`profile_facts`, `answer_library`,
  `apply_sensitive_answers`, `apply_ats_labels`) and in the page itself.
- **Sensitive answers.**
  - They are never sent to the extension or included in `apply_context`.
  - They are never passed into the page's JavaScript; Python types them.
  - A rehearsal does not put them in the page at all (6.7).
  - They reach the page only in a submit run, or a handoff under D5 B to E. There, like anything
    the student types into a web form, any script the employer's board loads can read them. The
    value guard (4.3) stops any other request, to any host, that carries one.
- **The browser context is fresh every run.** It uses no stored cookies, never uses the
  student's Chrome profile, and never imports cookies (memory note linkedin-test-account; Chrome
  136+ also refuses remote debugging on the default profile). Downloads are blocked, and so are
  service workers. WebSockets are refused.
- **The export** includes the new tables, with file paths redacted and without the HMAC key
  (5.7).
- **The threat model** (docs/THREAT_MODEL.md) gains an "Apply agent" row in M5b, kept separate
  from the unchanged Extension row:

  | Threats | Controls |
  | --- | --- |
  | Wrong answer | Exact sources only; the company rule; the plan preview |
  | Duplicate submit | The application and job locks; tombstones; limits counted from hand-overs |
  | A request that could carry the application before the student's press | Routing: every non-GET aborted before hand-over |
  | Silent spam drop | The 24 h watch (corroboration only) and the per-ATS threshold |
  | Screenshot exposure | `data/private`, whole-field masking and automatic retention |
  | A page tampering with the engine | Python read-back and the independent check |
  | Prompt injection from page text | No model; deterministic adapters |
  | Off-site redirect | The main-frame host allowlist |
  | A local script acting as the student | Browser-session-only consent and submit routes; the confirm nonce |

---

## 12. Testing strategy

Everything uses throwaway databases. **Nothing touches `data/platform.db`** (AGENTS.md rule 1),
and nothing reaches a real employer or Greenhouse.

### 12.1 Fixtures

`tests/fixtures/apply/greenhouse/`:

- `new_form.html`: a trimmed, fictional-company copy of the new board's structure.
  - `form#application-form` with the "Submit application" button.
  - Standard fields, and `input#resume.visually-hidden` inside an upload group.
  - All five required-marker kinds.
  - Custom questions: short text, a textarea, a single select, a multi select, a work
    authorization select, a sponsorship select, a salary text field, a privacy acknowledgment
    checkbox with a link, an accuracy attestation, a "previously worked here" radio, and an
    "If yes, please explain" sub-question.
  - An EEOC block (with the EEOC field names), a demographic question outside it, and a location
    field.
  - A native select whose first option is a real value, and a pre-checked optional box (6.10
    item 4).
  - A **vanilla-JS imitation of react-select**: a combobox input, a menu of options on typing, the
    single-value display in a separate element, and a hidden required mirror. It is labeled in a
    comment as an imitation.
  - An "Autofill my application" button and a "Locate me" button, each of which increments a
    `window.__forbiddenClicks` counter.
  - The loader JSON with `submitPath` and `confirmationPath`.
  - A spam-trap text input hidden by CSS.
  - It uses no real employer's text, name or ids. Ids are fictional, such as `question_4000000101`.
- `new_confirmation.html`, `closed.html`, `offsite.html`, and a
  `text_only_thanks.html` variant: "Thank you for applying" at the same URL with the form still
  present.
- `legacy_form.html` and `legacy_confirmation.html` (4.4 legacy; only detection is live while
  `LEGACY_ENABLED=False`).
- `schema_new.json` and `schema_legacy.json`: Job Board API responses matching the pages, in the
  shape seen live.
- `security_code_428.json`.
- `tests/fixtures/apply/sensitive_vectors.json` (7.3) and `question_keys.json` (12.3).

### 12.2 FakeGreenhouse and the fake schema client

`tests/apply_fake_ats.py` is an in-process fake served through `route_hook`, like
`tests/test_outreach_forms.py`'s `Site` (:670-687). It answers the **real** hostnames:

- `job-boards.greenhouse.io/examplerobotics/jobs/4000000001`;
- `boards-api.greenhouse.io/...?questions=true`;
- POSTs to `boards.greenhouse.io/examplerobotics/jobs/4000000001`.

Using the real hostnames means the adapter's host checks run as in production, with no network.
It records every request that reached it, `forbidden_clicks` read from the page, and whether the
submit path was ever hit. Its Playwright import is lazy, so collecting it never fails without
Playwright.

The `scenario` switch:

- `confirm`: the POST gives 303 to the confirmation path;
- `security_code`: 428, then the page shows `#security-input-0..7`; a second POST with a code
  gives the confirmation;
- `validation_422`;
- `server_500`;
- `hang`: the POST never answers;
- `hang_evaluate`: a page script loops forever after load (the watchdog test);
- `text_only_thanks`;
- `confirmation_without_post`: a page script navigates to the confirmation path without a POST;
- `other_path_post`: the form posts to a path other than `submitPath`;
- `request_submit_during_fill`: a page script calls `requestSubmit()` while the agent fills;
- `redirect_offsite`;
- `closed`: the schema gives 404;
- `loader_missing`: no `submitPath` in the HTML;
- `eager_script`: a page script POSTs on every keystroke, like lead-capture scripts;
- `eager_get`: a page script sends a GET beacon to another host carrying a field value;
- `eager_get_greenhouse`: the same beacon, to a Greenhouse host;
- `lookup_leak`: a typeahead's lookup GET also carries another field's value;
- `double_submit`: a page script (or a double click) sends a second POST to `submitPath` right
  after the first;
- `captcha_body_leak`: a page script POSTs a field value to a CAPTCHA endpoint;
- `websocket`: a page script opens a WebSocket;
- `s3_upload`: the form has `data-allow-s3="true"`, and attaching makes a PUT request to an S3
  host.

`FakeSchemaClient` (same module) serves `schema_new.json` or a 404, with no network. Tests, the
sandbox and the UI suite pass it as `apply_schema_client_factory`.

There is no loopback demo server and no loopback flag in production code: an agent that could be
pointed at 127.0.0.1, in a sandbox holding fixture identity data, is one import away from a real
board.

The existing node fake ATS (tests/extension/browser/run_browser_tests.mjs:39-66) stays as the
extension's MV3 test and is unchanged.

### 12.3 Pure and database tests (the default `unittest` suite, no browser)

- `tests/test_apply_checks.py` (M3):
  - `decide_outcome`, every row of 6.14 including: 303 plus confirmation path and form absent
    gives `submitted`; `server_500` gives `unconfirmed`; no submit POST passed and no navigation
    gives `failed` with `after_click=0`; a POST to another path, aborted by the route, gives
    `failed` with `after_click=0` and the "address the app doesn't recognize" note; a 3xx without
    a confirmation gives `unconfirmed`; the embed confirmation path; the confirmation path with the
    form still present gives `unconfirmed`; a navigation to the confirmation path without a submit
    POST gives `unconfirmed`;
  - the route policy as a pure function (`route_decision(mode, phase, request, state)`): every
    row of the 4.3 table, the value guard with its two exceptions, the one-submit-POST rule and
    the post-428 allowance, the CAPTCHA endpoint list, and `S3_UPLOAD_ENABLED=False`;
  - `join`, every problem kind in 6.5, including the question-key mismatch;
  - `check_required`, items 1 to 6 of 6.10, including the page-set-default exception and the
    skipped keys;
  - `clean_rehearsal`: deferred résumé, optional blanks, deferred sensitive fields.
- `tests/test_apply_policy.py` (M4):
  - the truth table (7.5), row by row;
  - the sensitive classifier against `sensitive_vectors.json`, including the most-restrictive
    rule and the options rule;
  - the superset check against the extension's `SENSITIVE`, using the same vector file;
  - question-key parity with JS using `tests/fixtures/apply/question_keys.json`, run by both
    suites;
  - `plan_hash` stability and sensitivity: any value, source, file, question text, option label,
    required flag or statement change alters it; a disposition change does not;
  - the name rule, and `name_parts` validation in `profile.update_profile`;
  - select and multi-select matching by label only;
  - the company rule, `reusable`, and the context-dependent keys;
  - `resume_for`: pick with a confirmed version, pick without one, `unsure`, no pick;
  - `identify`: URL token wins, parameterized LIKE, `^\d+$` job ids, the fixture's `a-1` refused;
  - that the regex mappings are not used.
- `tests/test_apply_runs.py` (M2 unless noted):
  - **the claim lock, directly.** Two connections on two threads call `apply_runs.claim` for one
    application; exactly one inserts. Then two `create_app` instances on one SQLite file, each
    with its own RUNNER, start the same claim; exactly one gets it and the other gets "already
    being submitted". (A test through `start_run` alone would pass on the RUNNER's 409 without
    reaching the claim.)
  - the job lock: a second application for the same `job_ref` is refused while the first is live
    or submitted, and allowed after release;
  - the retry releases an `after_click=0` attempt and inserts a new row; it cannot release an
    `after_click=1` one;
  - the conditional hand-over refuses a claim whose token changed, a cancelled claim, and (M6) a
    one-click claim whose rehearsal is 15 minutes old;
  - pause at hand-over: refused for `unattended`; for `one_click`, refused when the pause came
    after `confirmed_at` and allowed when it was already on (M6); `handoff` allowed (M5b);
  - heartbeat: a claim with a fresh heartbeat from another instance is held; with a stale one it
    is recovered; a handoff claim 19 minutes old with a fresh heartbeat is held;
  - `recover_stale(conn, now)`: `claimed` becomes `failed` (after_click 0); `clicking` becomes
    `unconfirmed`; a `submitted` claim with `stage_recorded=0` is retried only for `record` and
    `ledger`, never for `ask`; an orphaned `running` run is finished as failed;
  - a settle after a seen confirmation page that matches no row writes the event and a notice;
  - uncertain attempts are never picked up by the worker; "It went through" and "It didn't go
    through" resolve them, and both are refused while the claim is held;
  - `limits_block` at the edges, in the student's timezone: spacing, daily cap (handoff not
    counted), company by name and by board; a security-code attempt (after_click=1) followed by
    an immediate retry is blocked by spacing; "It didn't go through" followed by a retry still
    sees the earlier attempt for the company limit;
  - `rehearsal_block` at the edge, in the student's timezone;
  - the gate counts distinct companies, `clean=1`, current adapter version only; the breaker
    writes `apply_gate_reset_at:<ats>` and older rehearsals stop counting;
  - the forward-only stage write: a stage the student changed meanwhile wins (M5b);
  - unattended with the stage write deferred while paused and retried after resume (M8);
  - the check route writes nothing: opening the section 5 times creates no application, no
    event and no run row, and fetches the schema at most once within the cache hour (M4);
  - rehearsals and lookups leave `applications` unchanged (M5a);
  - `in_flight`, `unconfirmed` and `paused_text` include application claims, by heartbeat not
    age; `paused_text` reads correctly with one, two and three categories;
  - the Urgent kinds exist in both registries;
  - the worker step runs `recover_stale` and `watch` for a student with `apply_agent` off but an
    open claim.
- `tests/test_apply_watch.py` (M5b):
  - a strong match (`job_id` or `company_title`, `sender_verified=1`) gives `email_confirmed`;
  - a `company_single` confirmation for another role at the same company does **not** give
    `email_confirmed`; it sets `possible_email_at`;
  - a second-tier subject match does not confirm;
  - none after 24 h with the reader healthy gives `no_email_24h` plus a notice;
  - a stale `last_ok_at`, a non-empty `pending_ids_json`, or a `recovery_state` keeps
    `awaiting_email` and extends the window; after the reader catches up the clock resumes;
  - a late strong match gives `email_confirmed`;
  - the security-code subject is ignored;
  - crash in `clicking`, then `recover_stale` gives `unconfirmed`, then a strong mail row gives
    `submitted`/`email_confirmed` with `resolved_by='email'` and the stage write;
  - Phase 1 already moved the stage to applied: the claim is reconciled, not left unconfirmed;
  - a released tombstone whose email later arrives is flipped to submitted with a notice;
  - `not_watched` when the watch is not required and not available (D12);
  - the per-ATS statistics, and (M6) the 8.8 threshold arithmetic, excluding stalled watches.
- **Migration** (M2): 0044 applies on a fresh database and on a database at 0043; the Python step
  adds its three columns once and is safe to rerun. `tests/test_postgres.py` runs the claim race,
  the job lock, the partial unique indexes and the hand-over on PostgreSQL.
- **Retention and deletion** (M2): a worker pass with a frozen clock removes screenshots older than
  N days, keeps their hashes, and removes orphan files; `run_retention` does the same;
  `delete_account(..., apply_root=...)` removes the user's apply folder; the existing callers
  still pass.
- **Export** (M2): `screenshots_json` paths are redacted; the HMAC key is never in the export.

### 12.4 Browser tests (Playwright; skipped without Chromium, required in the new CI job)

`tests/test_apply_agent_browser.py` uses FakeGreenhouse and a shared helper,
`tests/browser_support.py`, which skips when Chromium is absent **unless**
`PIPELINE_REQUIRE_BROWSER_TESTS=1`, in which case it raises and shows the launch exception.
`tests/test_outreach_forms.py` moves to the same helper: its `_chromium_available()` swallows
every exception today, so a broken launch would still skip silently.

Browser tests pass `headless=True`, except one headed launch test that runs only when
`PIPELINE_HEADED_TESTS=1`, which only the `browser-python` CI job sets. So
`py -3 -m unittest discover` on the student's machine, which has Playwright, never pops windows.
The waits come from `ApplyTimeouts` shortened for tests, and the student is played by
`student_hook`.

- **A rehearsal sends nothing that could carry the application:**
  - on `eager_script`, FakeGreenhouse received no non-GET request, and `refused` names the
    blocked ones;
  - on `eager_get` and `eager_get_greenhouse`, the beacon carrying a field value was refused by
    the value guard, whichever host it went to;
  - on `lookup_leak`, the lookup GET carrying another field's value was refused, and the field was
    left unfilled;
  - after the first input, no request other than lookup GETs for the field being typed and static
    assets reached FakeGreenhouse;
  - on `websocket`, the socket was refused;
  - nothing reached the submit path;
  - the same holds for `s3_upload`, with the résumé note recorded and `clean=0`; a submit or
    handoff on `s3_upload` is refused before filling.
- A rehearsal fills every `fill` field, and every read-back matches; sensitive fields are
  `deferred`, their options are checked, and the page never held their values.
- The react-select imitation gets the exact option, not the first one. A missing option gives
  `needs_you`.
- The CSS-hidden spam trap is never filled, and a plan that targets it gives `needs_you`.
- The résumé goes into the visually-hidden `input#resume` with its original file name, and the
  name and size are verified; a file whose bytes do not match `resume_files.sha256` is refused.
- The pre-submit check catches:
  - a required field marked only by an asterisk and left empty;
  - a required react-select marked only by its hidden mirror;
  - a value put there by a page script and not by the plan;
  - and allows the page-set default on an optional field.
- **Submit, happy path:** `submitted`, with the POST seen, the confirmation path seen, the form
  absent, and a screenshot taken and hashed.
- `text_only_thanks` and `confirmation_without_post` give **unconfirmed**; the page wording is
  never trusted.
- `other_path_post`: the POST is aborted by the route, FakeGreenhouse never sees it, and the run
  ends `failed`, `after_click=0`, with the "address the app doesn't recognize" note.
- `double_submit`: FakeGreenhouse receives exactly one submit POST; the second is aborted and
  recorded. In `security_code`, exactly one more POST (the code) passes after the 428.
- `captcha_body_leak`: the POST to the CAPTCHA endpoint carrying a field value is aborted.
- `request_submit_during_fill`: the POST is blocked before hand-over, FakeGreenhouse never sees
  it, and the run ends `needs_you` with `after_click=0`.
- `loader_missing`: submit and handoff refuse before filling; a rehearsal continues.
- `security_code` gives `needs_you` with after_click 1 when nobody types; with `student_hook`
  typing a code and pressing the second Submit, the outcome is `submitted`, and the claim stayed
  `clicking` (with heartbeats) during the wait.
- `validation_422` gives `failed` with after_click 1. `server_500` and `hang` give
  `unconfirmed`. An exception injected after the click gives `unconfirmed`.
- `redirect_offsite` and `closed` give `needs_you` and `failed`.
- `forbidden_clicks == 0` in every scenario.
- **Handoff:** `student_hook` fills a left-for-you field and presses Submit; the hand-over runs
  inside the route handler before the POST continues; the claim goes to `clicking`, then
  `submitted`; the agent itself never clicked Submit. A hand-over that returns False aborts the
  POST, and FakeGreenhouse never sees it. On timeout the browser is closed before the claim is
  settled, and a Submit pressed by the hook after that never reaches FakeGreenhouse.
- **Cancel:** a cancel before hand-over ends `failed`, nothing sent; closing the window does the
  same.
- **Watchdog:** `hang_evaluate` ends at the deadline, the child process tree is gone, the slot is
  free, and the run is finished as failed.
- **Masking:** pixels over the `.select__single-value` element of a masked EEO react-select, and
  over its label, are one flat color.
- **Launch options:** the agent launches with none of the forbidden flags and no context
  overrides. This is asserted on the launch call, recorded through a thin wrapper.

Every rehearsal and handoff test also asserts that FakeGreenhouse recorded no submit-path POST
except one the student hook caused.

### 12.5 Extension tests (node)

- `run_tests.mjs`:
  - all 15 existing tests stay green;
  - new: the clean `question` excludes name and id;
  - new: every kind of `required_markers`, and `required_any`; `required` unchanged;
  - new: `input[role=combobox]` becomes `custom_select`;
  - new: `visible_css`;
  - new: `tag` adds attributes only when asked;
  - new: a saved clean question matches exactly, and a legacy label-keyed row still matches;
  - new: the extended `SENSITIVE` flags the new vectors;
  - new: question-key parity vectors;
  - **the no-submit static guard:** positive assertions on `apply-engine.js`, negative
    assertions on both files.
- `dom_stub.mjs` answers the new selector strings the engine uses (it only answers exact strings,
  dom_stub.mjs:108-111) and gains the DOM methods the new tests need. It also loads the four
  files. The engine's guards keep the existing tests working where a method is absent.
- The MV3 browser test's checks are unchanged. Only its injection list gains `apply-engine.js`.
- `tests/test_platform.py:1038-1043`: `'"submit"'` and `'SENSITIVE'` are asserted on
  `apply-engine.js`; the negative assertions cover both files. `final_submit_available: False`
  stays asserted (test_platform.py:1080, test_e2e_smoke.py:190).

### 12.6 API and UI tests

- `tests/test_apply_api.py` (TestClient, fake agent factory, `FakeSchemaClient`):
  - every route's happy path and refusals;
  - 503 without a factory, for the start routes and the check route;
  - 409 on a second concurrent run;
  - **the owner bearer token gets 403** on every browser-session route (4.6), and a cookie
    request without the CSRF header gets 403 even with no `Origin` header;
  - the confirm nonce is single-use and bound to its plan hash;
  - the sensitive-answers API refuses disallowed categories, requires the consent tick, refuses an
    EEO value unless `apply_eeo_store_values` is on, and refuses `company_key=''` for a statement
    that cites a document (M4s);
  - the screenshot route requires auth, sends `no-store`, and refuses paths outside the apply
    root;
  - `extension_apply.apply_context` and every `/api/v1/extension/*` response never contain a
    sensitive-answers entry (the answer text is planted in the store and searched for in the
    JSON).
- **No network.** A test installs an httpx transport that raises on any request, then runs the
  check route, every start route with the fake agent, and the rest of `test_apply_api.py`.
- `scripts/run_api_fuzz.py` covers the new routes. In the fuzz sandbox both factories are None, so
  the check and start routes return 503 quickly, with no network and no browser. Nothing needs
  excluding.
- **UI (`tests/ui`):** `scripts/serve_for_testing.py` gains `PIPELINE_SANDBOX_FAKE_APPLY=1` (M4).
  That wires `FakeSchemaClient` and a fake agent returning canned run results from the fixtures,
  with no browser, and makes `setup_requirement` answer from fake values: the fake factory reports
  Playwright available, and the sandbox's requirement treats the Gmail address as known, since the
  sandbox has no Gmail connection. The tests cover:
  - the section's states, including Checking and the handoff turn;
  - the plan preview table and screenshot;
  - the two-click confirm (M6);
  - each Needs you action, including Look up options (M5a);
  - the card badges, including `ask` and the paused watch;
  - axe checks on the new section.

### 12.7 Policy tests

- `apply_context` never returns store entries (above).
- `apply-engine.js` and `content.js` have no click or submit calls (above).
- **A static scan of `apply_agent.py`**, including its JavaScript string constants and
  `REQUIRED_CHECK_SCRIPT` in `apply/checks.py`, asserts that:
  - every `.click(`, `.check(`, `.set_checked(`, `.select_option(`, `.fill(`, `.press(`,
    `.tap(`, `.dispatch_event(`, `keyboard.`, `mouse.` and `set_input_files(` sits inside one of
    the five mutation helpers (4.3);
  - no `press("Enter")` or `keyboard.press` exists at all;
  - no `requestSubmit`, `.submit(`, `new MouseEvent` or `new PointerEvent` appears in any string.
- `default_apply_agent_factory` never sets `route_hook`, `student_hook` or `headless`.
- Only `apply/policy.py`, `apply/runs.py`, `accounts/operations.py` and the settings routes in `api.py`
  name `apply_sensitive_answers`; `accounts/employer.py` and the report code never do.

### 12.8 CI (Playwright is optional)

- **Job `test`:** unchanged install (`requirements-web.lock`, no Playwright). The new pure,
  database and API tests run, and the browser tests skip as they do today. The `node --check`
  line gains `apps/extension/apply-engine.js`.
- **New job `browser-python`** (M3):
  - `pip install -r requirements-web.lock -r requirements-ui.txt` (already pins Playwright 1.50+);
  - cache `~/.cache/ms-playwright` as the `ui` job does;
  - `python -m playwright install --with-deps chromium`;
  - `PIPELINE_REQUIRE_BROWSER_TESTS=1 PIPELINE_HEADED_TESTS=1 xvfb-run -a python -m unittest
    tests.test_outreach_forms tests.test_apply_agent_browser -v`.

  `xvfb-run` lets the headed launch test run for real. This job also closes the existing gap: the
  contact-form browser tests `BrowserSubmitTests` never run in CI today, because the unit job has
  no Playwright and the UI job runs only `tests/ui`. Adding it to the required status checks is an
  M3 deliverable.
- **Job `extension-browser`:** unchanged apart from the injection list.
- **Optional drift check** (with Phase 4.4 nightly CI, if it lands): `scripts/apply_shape_check.py`.
  It fetches three public Greenhouse schemas and loads their forms with rehearsal routing, **scan
  only, no fill**, then reports markers, selectors and lookup endpoints the adapter no longer finds. It
  never submits and never types.

### 12.9 Which milestone lands which tests

| Milestone | Tests |
| --- | --- |
| M1 | 12.5 |
| M2 | 12.3 `test_apply_runs` (claims, locks, retry, heartbeat, recovery, limits, gate, readers, Urgent, worker step), migration, postgres, retention, deletion, export |
| M3 | 12.3 `test_apply_checks` (including `route_decision`); the `browser-python` job with `tests/browser_support.py` and `test_outreach_forms` moved to it |
| M4 | 12.3 `test_apply_policy`; the check-writes-nothing test; 12.6 check route, no-network test, fuzz; the sandbox flag with fake schema client and fake agent |
| M4s | 12.6 sensitive-answers API; 12.7 store-reader scan |
| M5a | 12.4 rehearsal, lookup, watchdog, masking, launch, static scan (12.7); rehearsals leave applications unchanged |
| M5b | 12.4 handoff and cancel; 12.3 `test_apply_watch` (all but the threshold); forward-only stage write; 12.6 UI states, preview and badges |
| M6 | 12.4 submit scenarios; pause and 15-minute hand-over tests; nonce; 8.8 threshold |
| M7 | cover-letter rows of 7.5; artifact freshness |
| M8 | unattended shadow, deferred stage write, re-consent |

---

## 13. Risks and open questions

- **R1. Bot scoring.** Automated Chromium may be scored as a bot even when headed. That would
  mean a high rate of security-code prompts, or silent spam flags. Mitigation: measure per ATS
  from the first submission (8.8), the threshold, and Finish in browser as the default.
  **Open:** whether trusted CDP input events score better than script events is unknown.
- **R2. Silent loss the watch can't see, and emails that never come.** Blocklist rejection "at
  intake" and application-limit auto-rejects may still send a confirmation email, and some
  employers send none at all. So `email_confirmed` does not prove the application reached a human,
  and `no_email_24h` does not prove it didn't. The card says "Confirmation email arrived", never
  "Received by the recruiter", and never calls a missing email a failure.
- **R3. One bad submission can close off an employer permanently** (a spam mark). Mitigation:
  exact sources, the company rule, the per-company limit, the job lock, and rehearsals. **The
  residual risk is the student's to accept (D1).**
- **R4. Phase 1 misreading the security-code email as a confirmation.** Mitigation: the subject
  exclusion in the watch. **Open:** add a labelled example to
  tests/fixtures/application_mail_eval.json so the classifier's reading is known.
- **R5. Layout drift** in react-select, Pelias, the school catalog, the confirmation path, or
  where the form posts. Mitigation: the adapter version, rehearsals before every submit, the
  independent check, the schema join, blocking unrecognized POSTs before hand-over, and the
  optional shape check. Drift fails closed, as `needs_you` or "nothing was sent".
- **R6. Legacy boards** cannot be verified live today (301). The legacy adapter stays
  detection-only until verified.
- **R7. Company-embedded postings** (only `gh_jid` known). These are unsupported in v1. **Open:**
  whether to look up the board token through `pipeline.discover_ats` (pipeline.py:2277).
- **R8. Minimized window throttling** (D7 B). Chromium slows timers in background windows, which
  may break the page's scripts. This needs testing before B is offered. The possible fixes are
  the Chromium performance flags that stop background throttling, which are not disguise, but they
  are untested here.
- **R9. The engine is visible in the page's JavaScript world.** A page could detect it or tamper
  with it. Mitigation: Python read-back of every value and the independent check. Detection by
  the page is accepted; the agent does not hide.
- **R10. Double fill** (rehearsal plus submit) costs 1 to 3 minutes per application. That is
  accepted in exchange for a strict rehearsal and a fresh submit session.
- **R11. An abandoned Finish in browser leaves an `applying` row**, just as clicking Apply does
  today. Opening the section and rehearsing no longer create one (6.0). Phase 1 can move any
  `applying` row to applied on a confirmation matched at `company_single`
  (applications/inbox.py:1036); that behaviour predates this phase and is not changed here.
  **Open:** whether to remove the row when the student stops a first-ever Finish in browser for
  that role before hand-over.
- **R12. The email must be the Gmail account the app reads** (D12). If the student applies with a
  different email, the watch cannot work, and under D12 A or C one-click cannot be turned on.
- **R13. Terms.** No job-board candidate terms were found (a gap). MyGreenhouse terms are
  respected by never going there.
- **R14. Concurrency with Phase 6.** Both touch `automation.FEATURES` and migration numbering.
  Whichever merges second rebases (5.1).
- **R15. Scope.** M1 to M5b are useful without M6. If time runs short, stop after M5b (D1 B).
- **R16. The lookup endpoints are unknown.** The location and school typeaheads call services whose
  hosts must be confirmed on a live board before M5a (4.3). Until then those fields are left
  unfilled.
- **R17. Process isolation.** Running each agent run in a child process, and killing its process
  tree at the deadline, is more machinery than FormSubmitter has. It is the only way to bound a
  page that never yields; the watchdog test (12.4) pins it on Windows and Linux.
- **R18. Boards that upload as you attach** are rehearsal-only until their upload flow is seen
  live (`S3_UPLOAD_ENABLED=False`, 4.3, 6.9). How common they are is unknown; the sampled board
  was not one.
- **Open question Q1.** Does Greenhouse ever require an email verification step *before* submit
  for new candidates? No evidence was found. If it appears, it is `needs_you`.
- **Open question Q4. The emailed security code's widget. Answered 2026-10-08.** Does it submit by itself when its eighth character is
  typed? No recording of the live widget exists. Owner decision: the app still types the code (D10 B), and the code POST waits for
  the student's own press of Submit, seen as a trusted click that page scripts cannot forge, after the typing. A widget that sends
  at once, later, or repeatedly is refused until then. See section 6.13 (as built).
- **Open question Q2.** Should the bundled Chromium be replaced by the student's installed Google
  Chrome binary (`channel="chrome"`) with a fresh profile? It is not disguise, and it may score
  differently. Evaluate after R1 data exists.
- **Open question Q3. AI browser agents.** Examples are Vercel's `agent-browser`, Browser Use,
  Stagehand and Claude in Chrome. Recommendation: **never in the submission path.** They have an
  LLM choose each click, which is not reproducible, needs a model session and its cost per
  application, and cannot be pinned by tests. This spec's rule is exact matches to confirmed
  facts, enforced in plain code. They are acceptable as a **development aid only**: pointing
  one at a new ATS form (Lever, Ashby) to map its fields quickly before writing a normal,
  tested adapter. The student asked about this on 2026-09-28.

---

## 14. Milestones

Each milestone is one PR. Each is Greenhouse only, off by default, with its SETUP.md changes,
and passes the full CI gate: unit, UI, extension, extension-browser, the new browser-python job
(from M3), fuzz, postgres and secrets. Section 12.9 maps the tests.

**M1 to M5b never press Submit.** They can land after the student approves D1 B. M6 needs D1 A.
M4s is built only if D5 is B to E.

| # | PR | Contents | Done when |
| --- | --- | --- | --- |
| M0 | Decisions | The student answers D1 to D14; this file is updated with the answers. No code. | Answers recorded here. |
| M1 | Shared engine | `apply-engine.js` split with DOM guards; thin `content.js`; new scan outputs incl. `required_any` (4.2); `matchAnswer` compares the clean question first; `input[role=combobox]` becomes `custom_select`; side panel saves `field.question` (sidepanel.js:212) and injects 4 files (sidepanel.js:82); `SENSITIVE` extended (7.3 step 6); stub, loader, MV3 and CI lists updated; static guards moved and extended. | 12.5 green; the extension fills exactly as before on its fixtures, apart from the three listed behavior changes. |
| M2 | Data layer | Migration 0044 (four tables, partial unique indexes, the guarded Python step for three columns); `apply/runs.py` claims, locks, retry, heartbeat, `recover_stale`, limits, rehearsal limit, gate (no browser); the worker step; `in_flight`/`unconfirmed`/`paused_text`/Urgent and app.js reader additions; retention on the worker and deletion hooks; export. | 12.9 M2 tests green. |
| M3 | Pure checks, fixtures and CI | `apply/checks.py` (`REQUIRED_CHECK_SCRIPT`, `decide_outcome`, `join`, `check_required`, `clean_rehearsal`); Greenhouse fixtures (12.1); FakeGreenhouse and `FakeSchemaClient` (12.2); `tests/browser_support.py`; the `browser-python` CI job, required, running the existing contact-form browser tests. | The new job is green and fails if browser tests skip; `test_apply_checks` green. |
| M4 | Policy and read-only check | `apply/policy.py` (schema parsing, plan, classifier, company rule, `resume_for`, truth table); the schema client factory; the read-only check route; `name_parts` in the profile and its SETUP step; Apply agent settings (ATS labels, limits); the `apply_agent` feature (OFF_ON) with its requirement; the Gmail address recorded at connect; the UI "what's missing" view (10.3) for text, select and name answers; `PIPELINE_SANDBOX_FAKE_APPLY`. Still no browser. | Truth table green; the check writes nothing; for any saved Greenhouse role the student can see what is missing and answer the text and select questions. |
| M4s | Sensitive store (only if D5 is B to E) | `apply_sensitive_answers` with consent scope, the EEO opt-in, company-specific statements; its settings UI and Needs you form; the doc changes in the same PR: assisted-apply.md step 3, THREAT_MODEL.md:17, PRIVACY_ACCESSIBILITY.md:7. | 12.9 M4s tests green; the docs say what the store does. |
| M5a | Rehearsal engine | `apply_agent.py` in lookup and rehearse modes; the child-process runner with deadlines and the watchdog; the start, run and lookup API; Look up options in the UI; `GREENHOUSE_LOOKUP_ENDPOINTS` and `CAPTCHA_ENDPOINTS` confirmed on a live board. | 12.9 M5a tests green. |
| M5b | Finish in browser and the watch | Handoff mode with hand-over in the route handler; the plan preview with screenshots and review marks; handoff recording with `stage_policy` (`ask` under D1 B); `watch()` with its badges, notices, Urgent kind and paused state; per-ATS statistics; the assisted-apply.md section and the THREAT_MODEL Apply agent row; SETUP.md step (Playwright install, Linux display, VPN off, "your name on every application", name for applications, limits, retention). | 12.9 M5b tests green. |
| M6 | **Gate: one-click submit** (needs D1 A) | The policy rewrite in the same PR, with the D1 A wording (AGENTS.md "Product invariants", README.md "What it never does" and the Apply Mode bullet of docs/guide/web-app.md, THREAT_MODEL row, assisted-apply.md:5-6 and :36-37, the "Extension safety" row of ACCEPTANCE.md, the "7 — Apply Mode" row of PHASE_VERIFICATION.md; the extension README and manifest stay "never submits"); submit mode; hand-over with pause-after-confirm, Cancel and the 15-minute clock; outcome detection; `record` stage write; two-click confirm with nonce; the rehearsal gate; the 8.8 threshold and warning; D9 B and D14 B if chosen. | 12.9 M6 tests green; sandbox acceptance with the fake agent. |
| M7 | Cover letters in the flow (D11 B) | The Draft one path wired to preparation; the latest-approved-version rule; `content_sha256` freshness; attaching approved letters. | The cover-letter rows of 7.5 pass. |
| M8 | Unattended (**only with a separate yes**, D2) | Its own AGENTS.md rewrite; `auto_apply` feature, shadow, the worker step, limits, `ledger` stage write, breaker, Undo wording, re-consent. | 48 h shadow with 5 reviewed clean rows before `on` is offered. |
| Later | Lever, then Ashby | Lever: `/parseResume` fires on attach **[live]**, so upload first, then overwrite and verify; hidden `timezone` field; hCaptcha may escalate. Ashby: fields autosave as they are filled **[1-src]**, so rehearsal means "fill-only on a local fixture" or accepting that filling sends data; a puzzle question is always `needs_you`. Each needs its own rehearsal definition first. | Separate specs. Lever, Finish in browser only: `phase5-lever-handoff-spec.md` (draft 1, 2026-10-04). Ashby: not yet written; its open points are in that file's Appendix A. |

**Rollout checklist (after merge, in the student's own app; not a PR condition):**

- after M4: open the section on three saved Greenhouse roles and confirm the "what's missing"
  lists look right and the tracker did not change;
- after M5a: three live rehearsals (GET only) at three companies, marked right or wrong;
- after M5b: the first Finish in browser submission, watched end to end, including the email;
- after M6: the first one-click submission, watched by the student.

**Deploying** follows PLAN.md:128-131: back up `data/platform.db` to
`data/platform.db.pre-0044-backup` first, fast-forward live main, and restart the server by
process id (memory note restart-web-dashboard).

---

## Appendix A. Differences from PLAN.md Phase 5, and why

1. **Screenshots go in `data/private/apply/`, not `output/apply/`** (PLAN.md:1130). That follows
   the personal-evidence precedent (outreach/forms.py:76), with automatic retention and deletion
   added.
2. **The stage is recorded through `automation.perform` only for unattended runs, not
   `automation.act`** (PLAN.md:1233). There is no `automation.act`. A one-click submit is the
   student's own act, and a pause must not drop the record of a submission that already happened
   (6.15). Finish in browser under D1 B only asks.
3. **Injection uses `frame.evaluate`, not `page.add_script_tag`** (PLAN.md:1122), because a
   script tag is subject to the page's Content Security Policy. The engine's output is advisory,
   and Python does the read-back.
4. **Mutations use Playwright's input methods through five helpers**, and the engine's `fill`
   stays the extension's path. This gives trusted events and keeps every click in Python.
5. **The Greenhouse Job Board API schema** is used for a read-only preflight and as the third
   independent source for required fields. PLAN.md had not found it.
6. **A rehearsal blocks every non-GET request, CAPTCHA endpoints included, refuses WebSockets,
   blocks any request that carries a filled value (except a typeahead's own lookup), allows only
   lookups and static assets after the first input, and does not put sensitive answers in the
   page.** Submit and handoff allow exactly one submit POST after hand-over. Submit runs fill again
   on a fresh page. This resolves the conflict between PLAN.md
   5.6 "Rehearsal never POSTs" and FormSubmitter's CAPTCHA exception.
7. **The label-regex profile mappings are disabled** for the agent, in addition to the fuzzy
   answer tier (7.1). **Saved answers are used only for the company they were saved for**, unless
   marked reusable.
8. **Sensitive categories** are chosen by the student (D5), and consent and acknowledgment boxes
   (D9) and CAPTCHA checkboxes (D14) are separate decisions. Under the recommended D1 B they are
   all left for the student.
9. **ATS option labels** get their own table (5.5) and a GET-only lookup, because typeahead
   options cannot be matched exactly from profile facts.
10. **Added a per-company limit, a daily cap, a daily rehearsal limit and a job-level lock**
    (D4, 9.1), because of Greenhouse's permanent spam marks and application limits. PLAN.md had
    only 1 per 10 minutes and an unattended cap.
11. **The security code** is its own path (D10). **The 24-hour watch** reads
    `application_mail_messages`, not the ledger, trusts only a strong match from a verified
    sender, pauses while the mail reader is down, and can resolve uncertain attempts (6.16).
12. **"No confirmation email" no longer says the submission may have been filtered or suggests
    reapplying** (PLAN.md:1206-1208), because employers can turn that email off and a reapply
    risks a duplicate.
13. **The claim table** is one row per attempt with two partial unique indexes (application and
    job), tombstones instead of deletes, heartbeats, `stage_policy`, and hand-over times. PLAN.md
    had one row per application.
14. **Finish in browser is the first way to submit**, with its own policy: it leaves what it
    cannot fill for the student instead of stopping.
15. **Nothing is recorded in the tracker on opening, checking or rehearsing** (G9).
16. **The migration number is 0044** (0040 to 0043 were taken by work that landed or is in flight first),
    and it adds three columns through a guarded Python step.
17. **The rehearsal gate** counts distinct companies, clean runs only, current adapter version
    only, reviewed right, and resets through a stored timestamp on 2 wrong in 5 (9.2). The count
    itself is the student's choice (D3).
18. **Unattended mode** is specified but deferred to its own decision and its own rule change
    (D2), rather than being a Could item in the same phase.

## Appendix B. Files touched

**New:**

- `apps/extension/apply-engine.js`
- `opportunity_app/apply/checks.py`, `opportunity_app/apply/policy.py`,
  `opportunity_app/apply_agent.py`, `opportunity_app/apply/runs.py`
- `migrations/0044_apply_agent.sql`
- tests: `tests/test_apply_checks.py`, `tests/test_apply_policy.py`, `tests/test_apply_runs.py`,
  `tests/test_apply_watch.py`, `tests/test_apply_api.py`, `tests/test_apply_agent_browser.py`,
  `tests/apply_fake_ats.py`, `tests/browser_support.py`
- fixtures: `tests/fixtures/apply/...`
- scripts (optional): `scripts/apply_shape_check.py`

**Changed:**

- **Extension:** `apps/extension/content.js`, `apps/extension/sidepanel.js` (:82, :212).
- **App code:**
  - `opportunity_app/automation/ledger.py`: FEATURES, REQUIREMENTS, `in_flight`, `unconfirmed`,
    `paused_text`;
  - `opportunity_app/applications/actions.py`: `ensure_application_tx`, factored out of `_record_intent_tx`;
  - `opportunity_app/applications/extension.py`: `confirmed_resume_file`, factored out of
    `artifact_path`;
  - `opportunity_app/student/artifacts.py`: `content_sha256` and re-render on mismatch (M7);
  - `opportunity_app/student/profile.py`: `name_parts` in `ALLOWED_PROFILE_FIELDS` and
    `validate_profile_types`;
  - `opportunity_app/applications/inbox.py`: set `sender_verified` when recording a message;
  - the Gmail connect callback (with `outreach/gmail.py`): record `account_email`;
  - `opportunity_app/core/schema.py`: `_apply_apply_agent` in `_MIGRATION_STEPS`;
  - `opportunity_app/accounts/operations.py`: `run_retention`, `delete_account(apply_root=...)`, export;
  - `opportunity_app/applications/urgent.py`;
  - `opportunity_app/api.py`: routes, `require_browser_session`, the factory wiring next to
    api.py:1063-1064, and the deletion caller at :3207;
  - `opportunity_app/outreach/automation.py`: the apply worker step;
  - `opportunity_app/static/app.js`, `styles.css`.
- **Scripts:** `scripts/serve_for_testing.py` (the fake apply flag).
- **Tests:** `tests/extension/run_tests.mjs`, `tests/extension/dom_stub.mjs`,
  `tests/extension/browser/run_browser_tests.mjs`, `tests/test_platform.py`,
  `tests/test_extension_apply.py`, `tests/test_postgres.py`, `tests/test_outreach_forms.py`
  (moves to `browser_support`).
- **Dependencies:** `requirements-optional.txt` (`playwright>=1.48`).
- **CI:** `.github/workflows/ci.yml`.
- **Docs:** `SETUP.md` (new step, including `name_parts` and the Linux display);
  `docs/assisted-apply.md` and `docs/THREAT_MODEL.md` (Apply agent section and row, M5b).
- **Only in M4s:** `docs/assisted-apply.md` step 3, `docs/THREAT_MODEL.md:17`,
  `docs/PRIVACY_ACCESSIBILITY.md:7`.
- **Only in M6:** `AGENTS.md`, `README.md` and `docs/guide/web-app.md`, `docs/THREAT_MODEL.md`, `docs/assisted-apply.md`,
  `docs/ACCEPTANCE.md`, `docs/PHASE_VERIFICATION.md`.
- **Only in M8:** `AGENTS.md` again, for unattended mode.

**Unchanged by design:**

- `pipeline.py` and `pipeline_core/`;
- `apps/extension/manifest.json` (still "Never submits forms.");
- `apps/extension/README.md`'s no-submit statements;
- `extension_apply.apply_context`'s sensitive-answer exclusion;
- `accounts/employer.py` (a test proves it never reads the sensitive store).
