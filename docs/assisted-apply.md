# First-party Assisted Apply

The pipeline implements Jobright-like workflow conveniences without connecting
to Jobright or copying its proprietary service. All facts and documents come
from the local pipeline, every proposed value shows provenance, and the user
remains responsible for navigation, review, consent, and final submission.

## Trust boundaries

The web account creates a single-use 10-minute pairing challenge. Redemption is
limited to a valid Chrome extension origin and issues a random device token.
Only the SHA-256 digest is stored. Device tokens are tenant-scoped, origin-bound,
revocable, and accepted solely under `/api/v1/extension/*`. Failed code guesses
are throttled without retaining attempted codes.

`ApplyContext` contains only confirmed `profile_facts`, non-sensitive saved
answers, approved documents, the deterministic score explanation, and prior
value-free progress. The extension never receives the editable profile draft.

## Assisted workflow

1. Candidate resolution compares exact and canonical URLs, then same-host and
   recent `apply_opened` evidence. Only one unambiguous exact/canonical match is
   eligible for preselection, and the panel still requires user confirmation.
2. The field engine inventories supported controls with a stable fingerprint.
   It refuses hidden, disabled, duplicate/ambiguous, navigation, CAPTCHA,
   consent, messaging, and submit controls. Every mutation is verified against
   the live DOM after normal `input` and `change` events.
3. Sensitive or consequential fields remain manual in the extension: it
   refuses them, and the saved-answer library never holds an answer to one. The
   one exception is **Apply for me**, the app's own filler for saved Greenhouse
   roles (a separate feature, off until the student turns it on), and only for
   what the student has switched on and stored themselves under Apply for me
   settings. That store holds:
   - work authorization, visa sponsorship, and 18 or older, each as the student's
     own answer;
   - an EEO self-identification question, only as a decline answer such as
     "Decline To Self Identify" or "I don't wish to answer" (the service refuses
     any other value, so no demographic value is ever stored);
   - a legal acknowledgment or a data-processing consent, stored word for word.
     Its box is ticked only when the form's statement is exactly the stored one.
     The statement is always the whole of what the box shows: its heading, its
     option and any description under it, together (the option alone is never
     the statement, however long, because "I have read and agree to the
     following" names nothing). When the option is short, points elsewhere ("I
     agree to the above terms") or the box has a description, the statement is
     also filed under the question above it and saved for one company. A
     statement is saved for one company, never for any company, whatever its
     words: no list can prove a statement names no document, so even a plain
     certification that the student's own answers are true is one company's. So
     is any question that depends on its company. A tick box or a typed answer
     for work authorization, sponsorship or 18 or older is one company's too,
     and so is a choice that also agrees to something ("Yes, and I agree to an
     E-Verify check"). Only a choice from the form's own option list and an EEO
     decline may be kept for any company. The plan shows the form's own
     document addresses next to the tick; if the form links to other addresses
     than the ones saved, or to none, the box is left for the student, and so is
     a statement whose description is longer than the app keeps. A Yes/No
     question that asks for agreement is matched on its question (under the
     question above it when it is short or a follow-up) and its description the
     same way, and only a box or a Yes/No question is ever
     ticked; an acknowledgment on a text field or a list of options is left for
     the student. A box that states a fact about the student (work
     authorization, sponsorship, 18 or older) is matched on its heading and its
     option together, since the heading is the question.

   Every entry records the exact question, the answer, and the student's consent
   with the time (the consent says the answer is used only to fill in
   application forms). Export control, citizenship and security clearance,
   salary, and every other personal question (age, birth date, pronouns,
   religion, criminal history, and anything the classifier cannot place) are
   never stored or answered. The extension's `apply_context`, the answer
   library, employer views and every report never read the store
   (`tests/test_apply_sensitive.py` scans the source for it). Writing or reading
   the store needs the student's own browser session: a request that sends the
   access token as an Authorization header is refused, and a write needs the
   session's CSRF header. The access token can still sign in a browser session,
   and the account export includes the stored answers, so this guards against a
   stray script and is not a lock against whoever holds the token. The student
   still presses Submit themselves.

   Where a sensitive field is not covered by a stored answer it stays manual,
   as above. A non-sensitive saved
   answer whose question has the same words as the field can be checked after
   review; fuzzy matches are never prechecked. "Exact" means the same words, not
   that the answer is true here. The panel shows a line under every field
   saying why it was proposed: an exact match reads "Same question saved for
   this company; verify before filling" (or "Same question, saved as reusable;
   verify before filling"), and anything saved at another company reads "Saved
   for another company; direct review required" and is not prechecked. A
   matched field with no name to tell it apart reads "Same words saved before,
   but this field has no name to tell it apart; direct review required", and a
   fuzzy match reads "Similar saved question; direct review required". A saved
   answer is exact only for the company it was saved at, or when it is tagged
   `reusable`, is not a radio or checkbox option, and its question is not
   context-dependent. That holds for every saved row, including rows saved
   before the clean question was kept. A row with no company is exact only when
   it is tagged `reusable`.
   What counts as context-dependent is decided from wording, not meaning. A
   question is kept at its own company when it is under three words, is a
   follow-up (see below), or matches a fixed list: previously worked, employed
   or applied here, worked for or with us or this company, employed by or at,
   interviewed with us, relatives or family members, related to, spouse,
   immediate family, employed here, current or former employee, referred, know
   anyone, how did you hear, "this organization/firm/company/employer". An
   employer-relative question worded some other way is not caught by that list,
   so do not tag such an answer `reusable`.
   A second, deliberately wide check backs both lists up: the broad net
   (`possiblySensitive` in the extension, `possibly_sensitive` in the agent),
   one list of topic words per topic (immigration, work authorization, criminal
   history, personal details such as age or gender, pay, security clearance,
   agreements, employer relatives). It never decides what a question is; it only
   keeps a possibly sensitive question at its own company. In the extension, a
   reusable saved answer never carries onto a question the net hits (its
   wording, its help text, or a select's options), nor onto one that comes
   right after a question the net hits or further down a chain of short
   follow-ups under it, and a box is never pre-ticked from an answer saved at
   another company. **Apply for me never
   carries a saved answer from one company to another**, whatever it is tagged:
   no list of words, the net included, is complete, so nothing travels, and the
   Apply for me check offers no "Use for any company". (Your confirmed name,
   email, phone and links are the same at every company.) The agent also, on a
   best-effort basis, refuses criminal history, personal details, pay and
   security: it never offers to save an answer to such a question, or to one
   filed under it ("Please tell us what happened" under a felony question), and
   never fills one from the answer library, even at the same company; the
   extension's Save refuses the same wordings. A wording the net misses can at
   worst be saved by the student for that one company, and is never carried
   elsewhere. It never ticks a box, or a group of boxes, from the answer
   library, nor chooses an option (or answers a question whose heading) that
   agrees, accepts, acknowledges, consents to or certifies something, nor
   types a signature or initials; only a stored
   acknowledgment or consent that matches word for word ticks or chooses it, and
   that stays with one company. The net over-reads on purpose (a question that
   only comes after a sensitive one takes its topics too), though it leaves
   ordinary prompts alone ("take charge of a project", "in two sentences",
   "network security", "exporting data", "hourly availability"). Nothing is ever
   answered on its say-so.
   Radio and checkbox options, follow-ups that depend on the question above them
   ("if yes, please explain", "please provide more details", "which company was
   it?", also after numbering or tags such as "Q4b.", "Question 3:", "1.2.3",
   "Follow-up:", "Sub-question:" or "(optional)"), very short questions, and a
   question that appears twice on one form
   are saved on the field's own label plus its form name and id, so two
   different fields on one page never share a key. A field with no name and no
   id has no such key, and its answer cannot be saved; a row saved on its
   wording earlier or by hand is shown there for review, never as exact. A field with no label
   text on the page cannot be saved either. These are exact only on that same
   label, and only under the company rule above; a radio or checkbox option
   row never travels to another company, even when tagged `reusable`, because
   the row does not record its group question.
   Known limitations, which is why every match says to verify before filling.
   Follow-up detection is wording-based, so a follow-up worded like a
   standalone question can still match an answer saved under a different
   question at the same company. And a name and id are not always unique to one
   posting: legacy `boards.greenhouse.io` forms number their fields by position
   (`answers_attributes_3_text_value`), so a follow-up or an option saved on one
   posting can match the same wording at the same position on another posting
   at the same company, under a different parent question.
4. Confirmed uploaded PDF/DOCX résumés and approved generated PDFs can be
   selected explicitly. Filename, media type, size, and SHA-256 are checked
   before insertion. File bytes are never persisted by the extension.
5. One session follows each application while each ATS page has an idempotent
   step record. Records contain outcomes, not proposed or observed values.
6. The tracker moves to `applied` only through the panel's explicit
   `confirm-submitted` action, or the student's own **Mark as applied** after
   Finish in browser. There is no success-page inference.

Workday, Greenhouse, Lever, Ashby, SmartRecruiters, and conservative generic
HTML forms are recognized. iCIMS and Workable are detected as experimental and
remain manual for unsupported widgets. LinkedIn is out of scope.

## Finish in browser (Apply for me)

**Apply for me** is the app's own filler for saved Greenhouse roles. It is off until the student turns it on, and **Finish
in browser** is the part of it that fills a real form. The student presses Submit; the app never does.

1. The app opens the employer's Greenhouse form in its own Chromium window, with a fresh profile and no cookies, and fills it
   from confirmed facts, the answers saved for that company, the confirmed résumé, and the stored sensitive answers the
   student switched on. A consent or acknowledgment box is ticked only on an exact stored statement with the same
   documents, and the **Your turn** panel lists every box the app ticked, with the addresses the statement links to, before
   the student presses Submit. Everything else is left for the student: cover letters (the app attaches none yet), any
   CAPTCHA box, and any field it could not fill or read back. The panel lists those under **Left for you**.
2. The student completes the form in the window and presses **Submit application** there. Before that press nothing that could
   carry the application leaves the window: every request that is not a plain read is refused, except the CAPTCHA
   service's own requests, which may carry no answer, and Greenhouse's telemetry is refused outright. The press goes
   through only after the app has recorded the attempt, so a submission can never be sent without a record of it. Once typed,
   the employer's page scripts can read your answers, including sensitive ones; the app blocks any request that carries
   a value it filled to any other address, before and after the press (spec 11). It checks only what the app itself put in
   the form: an answer you type in the window is not known to the app, so it is not watched for. After the press the
   confirmation page's own requests to Greenhouse's board addresses are not checked, and every other address still is.
3. A second press, a file upload, or a send to an address the app does not recognize is stopped. When a form sends its fields
   to an address the app does not recognize just after your press, the app refuses it as always, leaves the window open for
   you, and the panel says "The form tried to send a request to {address}, which the app doesn't recognize, so the app stopped that
   request", so you are not left looking at the page's own error. **Stop**, closing the window,
   or 20 minutes with no press close the window and send nothing. After the press Stop is gone: the app can no longer
   say that nothing was sent, and it tells the student what it saw.
4. If Greenhouse asks for its emailed security code, the app reads the code from Gmail (read-only, a verified Greenhouse
   sender, after the press, for the same company, once) and types it into the same window. It does not press Submit
   again: the student does. The app refuses to let the code leave the window until it has seen the student's own click on
   the form's Submit button (or Enter in the form) after the typing finished, so a page that sends the code by itself, as it is
   typed or seconds later, or again and again, sends nothing, and a second prompt needs a new click of its own; a click a page script makes does not count. If the app cannot read
   the code, or cannot watch for the student's click, the student types it. The code is never stored or logged, and the picture taken at the end
   covers the code boxes.
5. Pausing automation does not stop a window the student opened (their own Submit is the confirm), and the pause reply
   says so.
6. The tracker never moves by itself. When Greenhouse shows its confirmation page, the application card asks "Greenhouse
   showed its confirmation page. Mark as applied?" and only the student's click moves it to `applied`. An attempt that may
   have reached Greenhouse says so and asks **It went through** or **It didn't go through**; the app also looks for
   Greenhouse's confirmation email for 24 hours when Gmail is connected.
7. Screenshots of the filled form, with sensitive fields covered, are kept 90 days under `data/private/apply/` and shown
   only in the student's own signed-in browser.

Every application goes out under the student's own name and is the student's own act. In a rehearsal the **Answer**
column shows the answer the app would use today, which is what Finish in browser would fill, and marks one that changed
since the rehearsal. After Finish in browser the **What the app filled** column shows a value only when it is provably the
one the app filled; after the student edits a field in the window the app does not claim to know what Greenhouse received.

## Verification gates

The implementation is guarded at the API/DB boundary by
`tests/test_extension_apply.py` and at the field-mutation boundary by
`tests/extension/run_tests.mjs`; Finish in browser by `tests/test_apply_handoff.py`,
`tests/test_apply_agent_browser.py` and `tests/ui/test_apply_handoff.py`. Normal release verification also runs the
Python API suite, Playwright UI suite, API fuzzer, visual checks, and PostgreSQL
contracts. Never point any test at `data/platform.db`; browser and API tests use
throwaway seeded databases.
