# Threat model

Last reviewed: 2026-08-10. Scope: API, browser UI, extension, worker, SQLite/PostgreSQL data, private files, and sandbox provider adapters.

| Asset / boundary | Primary threats | Implemented control | Verification |
|---|---|---|---|
| Student, employer, admin APIs | tenant/role confusion, token theft | distinct constant-time bearer checks; strict session cookie; ownership predicates | role-isolation tests |
| Browser session | CSRF, XSS, clickjacking | SameSite cookie, double-submit CSRF on browser writes, CSP, no inline script, frame denial | security middleware tests |
| Resume/capture upload | malware, polyglots, decompression abuse | allowlisted signatures/types, bounded reads, deterministic scan, private generated filenames | hostile upload tests |
| Posting/email/employer text | prompt/tool injection | untrusted text is evidence only; deterministic tool router; consequential proposal approval | hostile content tests |
| Dossier/candidate handoff | over-sharing, stale access | item-level preview, hashed expiring grant, revocation checked on every read, access log | consent/revocation tests |
| Extension | over-broad access, accidental submit | activeTab permission, sensitive-field review, no submit implementation or DOM action | source/static fixtures |
| Providers/notifications | credential disclosure, unwanted delivery | token-at-rest adapter, disconnect erasure, sandbox-suppressed development outbox, opt-outs | lifecycle tests |
| Operations | replay, overload, silent job loss | idempotency keys, per-client rate limits, durable retries/dead letters, lease recovery, queue backpressure signal | operations tests |
| Backups | plaintext exposure, unusable restore | Fernet encryption, integrity check, documented restore drill | backup/restore test |

Residual risks: local bearer tokens remain appropriate only for the current-user/invited-tester deployment. Public signup requires a managed identity provider, per-user identities, external penetration testing, and live-provider compliance review. Demographic attributes are deliberately not collected, so subgroup error rates are reported as not measurable rather than fabricated.
