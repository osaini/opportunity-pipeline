# Threat model

Last reviewed: 2026-08-10; the Apply for me sensitive-answers store row was added 2026-09-30. Scope: API, browser UI, extension, worker, SQLite/PostgreSQL data, private files, and sandbox provider adapters.

| Asset / boundary | Primary threats | Implemented control | Verification |
|---|---|---|---|
| Student, employer, admin APIs | tenant/role confusion, token theft | distinct constant-time bearer checks; strict session cookie; ownership predicates | role-isolation tests |
| Browser session | CSRF, XSS, clickjacking | SameSite cookie, double-submit CSRF on browser writes, CSP, no inline script, frame denial | security middleware tests |
| Resume/capture upload | malware, polyglots, decompression abuse | allowlisted signatures/types, bounded reads, deterministic scan, private generated filenames | hostile upload tests |
| Posting/email/employer text | prompt/tool injection | untrusted text is evidence only; deterministic tool router; consequential proposal approval | hostile content tests |
| Dossier/candidate handoff | over-sharing, stale access | item-level preview, hashed expiring grant, revocation checked on every read, access log | consent/revocation tests |
| Extension | over-broad access, accidental submit | activeTab permission, sensitive-field review, no submit implementation or DOM action | source/static fixtures |
| Apply for me sensitive-answers store | an answer given without the student's consent or for another use; a stored answer read by employer views, reports or the extension; demographic data collected; a local script adding, reading or removing entries; one employer's notice taken as another's; a changed statement or document agreed to unseen | entries are added only by the student, with an unticked-by-default consent recording its time and scope, and used only to fill application forms; EEO questions are stored only as a decline answer, checked in the service, so the table holds no demographic value and "Demographic attributes are deliberately not collected" stays true; export control, citizenship, clearance, salary and every uncategorized question are refused; matching is exact (question, category, company), a statement is stored word for word, and one that reads or links a document is saved for one company with its address shown and re-checked; the store is read only by the apply plan, never by `apply_context`, `/api/v1/extension/*`, the answer library, employer or report code; its routes need the student's browser session with CSRF, so the access token in `.env` is refused | `tests/test_apply_sensitive.py`, `tests/test_apply_api.py` (browser-session, refusal and no-leak tests), the store-reader source scan |
| Providers/notifications | credential disclosure, unwanted delivery | token-at-rest adapter, disconnect erasure, sandbox-suppressed development outbox, opt-outs | lifecycle tests |
| Operations | replay, overload, silent job loss | idempotency keys, per-client rate limits, durable retries/dead letters, lease recovery, queue backpressure signal | operations tests |
| Backups | plaintext exposure, unusable restore | Fernet encryption, integrity check, documented restore drill | backup/restore test |

Residual risks: local bearer tokens remain appropriate only for the current-user/invited-tester deployment. Public signup requires a managed identity provider, per-user identities, external penetration testing, and live-provider compliance review. Demographic attributes are deliberately not collected, so subgroup error rates are reported as not measurable rather than fabricated.
