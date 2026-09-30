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
3. Sensitive or consequential fields remain manual. A non-sensitive saved
   answer whose question has the same words as the field can be checked after
   review; fuzzy matches are never prechecked. "Exact" means the same words, not
   that the answer is true here. The panel shows a line under every field
   saying why it was proposed: an exact match reads "Same question saved for
   this company; verify before filling" (or "Same question, saved as reusable;
   verify before filling"), and anything saved at another company reads "Saved
   for another company; direct review required" and is not prechecked. A saved
   answer is exact only for the company it was saved at, or when it is tagged
   `reusable`, is not a radio or checkbox option, and its question is not
   context-dependent. That holds for every saved row, including rows saved
   before the clean question was kept. A row with no company is exact only when
   it is tagged `reusable`.
   What counts as context-dependent is decided from wording, not meaning. A
   question is kept at its own company when it is under three words, is a
   follow-up (see below), or matches a fixed list: previously worked, employed
   or applied here, worked for or with us or this company, employed by or at,
   interviewed with us, relatives or family members, referred, know anyone,
   how did you hear, current employee, "this organization/firm/company/
   employer". An employer-relative question worded some other way is not
   caught, so do not tag such an answer `reusable`.
   Radio and checkbox options, follow-ups that depend on the question above them
   ("if yes, please explain", "please provide more details", "which company was
   it?"), very short questions, and a question that appears twice on one form
   are saved on the field's own label plus its form name and id, so two
   different fields on one page never share a key. A field with no name and no
   id has no such key, and its answer cannot be saved. A field with no label
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
   `confirm-submitted` action. There is no success-page inference.

Workday, Greenhouse, Lever, Ashby, SmartRecruiters, and conservative generic
HTML forms are recognized. iCIMS and Workable are detected as experimental and
remain manual for unsupported widgets. LinkedIn is out of scope.

## Verification gates

The implementation is guarded at the API/DB boundary by
`tests/test_extension_apply.py` and at the field-mutation boundary by
`tests/extension/run_tests.mjs`. Normal release verification also runs the
Python API suite, Playwright UI suite, API fuzzer, visual checks, and PostgreSQL
contracts. Never point any test at `data/platform.db`; browser and API tests use
throwaway seeded databases.
