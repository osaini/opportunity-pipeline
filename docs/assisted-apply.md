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
3. Sensitive or consequential fields remain manual. Exact, non-sensitive saved
   questions can be checked after review; fuzzy matches are never prechecked.
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
