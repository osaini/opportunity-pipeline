# Implementation acceptance record

Recorded: 2026-08-10; re-verified 2026-08-23

**Scope decision, 2026-09-17:** the gates still open below are closed as out of
scope for how this project is actually deployed. See
[Scope decision](#scope-decision--2026-09-17) at the end of this file.

| Gate | Evidence | Result |
|---|---|---|
| Legacy behavior | 105 unchanged `test_pipeline` cases | Pass |
| Python behavior and policies | 208 tests across legacy, platform, tenancy, workers, connectors, notifications, operations, and end-to-end smoke | Pass; one isolated PostgreSQL contract skipped locally without `POSTGRES_TEST_URL` |
| PostgreSQL | Isolated contract in `tests/test_postgres.py`; PostgreSQL 17 CI job | Configured; skipped locally without server |
| Production migration parity | 604/604 rows, 482/482 active unique, first 200 IDs exact | Pass |
| Python/JavaScript validity | `compileall` and Node checks for web/extension assets | Pass |
| Dependency integrity | locked environment, `pip check`, `pip-audit` | Pass; zero known vulnerabilities |
| Security/privacy | role isolation, CSRF, rate limit, upload rejection, prompt injection, consent/revocation, account deletion | Pass |
| Operations | idempotent jobs, retry/dead letter, lease recovery, backpressure signal, encrypted backup/restore | Pass |
| Local observability | structured request logs, propagated trace IDs, bounded admin trace view, p95/error metrics, SLO evaluation, alert state, identifier-free product aggregates | Pass; external collector and alert routing remain an operator gate |
| Employer ranking integrity | criterion-level consented evidence, score recomputation check, outcome-measurement status, no fabricated override/subgroup metrics | Pass; thresholds and outcome-review policy remain a governance gate |
| Browser behavior/accessibility | 88 Playwright tests; all axe WCAG 2.1 A/AA rules unquarantined; keyboard, focus, responsive, recording-state, and target-size checks | Pass |
| Visual baselines | 5 Windows Chromium screenshot comparisons after intentional accessibility/mobile navigation updates | Pass |
| Extension safety | 9 DOM fixtures including sensitive-field answer-library exclusion and no-submit guarantee | Pass |
| API fuzzing | 5,940 generated cases over 102 selected operations; server-error check plus stateful phase | Pass; role-auth and missing-sequence warnings retained |
| Hosted visual/screen-reader pass | requires a deployed URL and human NVDA/VoiceOver/TalkBack session | Operator release gate |
| Live Google/Microsoft delivery | OAuth/token/webhook paths are implemented; provider credentials and approval are required | Disabled safely by default |
| Live SMS/voice/web-push delivery | preference, verification, outbox, opt-out, and sandbox paths exist; production transports have not been selected or implemented | Open product/provider gate |

The hosted pass and live-provider rows are intentionally not simulated or
claimed as completed locally. SMTP email has a disabled-by-default live adapter;
SMS, voice, and web push still require provider selection, compliance review,
budgets, and transport implementation.

## Gap-remediation acceptance update — 2026-08-22

The automated portions of R-A through R-G of the remediation plan
([2026-08-platform-build-plan.md](plans/2026-08-platform-build-plan.md) §11)
are covered by the expanded suite (208 Python tests, 88 browser tests, 5 visual
baselines, 9 extension fixtures, and 5,940 generated API cases):

| Gate | Evidence | Result |
|---|---|---|
| Correctness baseline (R-A) | auth-scoped apply-sessions, configurable extension origin, UTF-8-safe ids, DOM-mutation-safe fills | Pass |
| Verification infrastructure (R-B) | worker/auth/migrate/ops/OAuth unit tests; extension fixtures with hand-rolled DOM stub; CI wiring | Pass |
| Real tenancy (R-C) | per-user hashed API tokens, flagged public signup, required `user_id` across domain functions, tenant-scoped opportunity reads/scores/intents/stats, cross-user isolation suite | Pass |
| Delivery engine (R-D) | provider protocol with sandbox default, SMTP email, digest honoring frequency/quiet hours, reminder dispatch, connector health, gated recovery/employer delivery | Partial: SMS/voice/web-push transports open |
| Scheduled ingestion (R-E) | worker pipeline stages with run audit trail, RSS discovery fetcher, env-cadence scheduling (off by default) | Pass |
| Live connector paths (R-F) | signed webhook ingest bound to connector owner; optional tesseract OCR with graceful degrade | Pass |
| Utern-parity depth (R-G) | match explanations + evidence + gaps on cards/detail; persistent save/pass/apply; sensitive-field-safe answer proposals with provenance; session resume; auto LLM provider selection | Pass |
| End-to-end smoke (R-H automated portion) | signup → match explanation → save → apply → webhook → confirm → digest delivered (`tests/test_e2e_smoke.py`) | Pass |
| Hosted visual/screen-reader pass | unchanged | Operator release gate |
| Live Google/Microsoft delivery sign-off | OAuth path implemented; requires credentials and operator approval | Operator release gate |
| Live SMS/voice/web-push | sandbox controls implemented; no production transports selected | Product/provider implementation gate |

## Scope decision — 2026-09-17

This project runs as a local-first, single-user, loopback-only workspace for
one student (see `AGENTS.md`). It is not hosted, has no second tenant, and has
no employer or school users. The remaining open gates exist for a hosted,
multi-user product, so they are closed as **out of scope** rather than
claimed as passed. The code paths they refer to stay in place, disabled or
sandboxed by default, and the automated suites keep covering them.

| Open gate | Disposition | What stands in for it |
|---|---|---|
| Hosted visual/screen-reader pass | Out of scope: no hosted deployment | Automated axe WCAG 2.1 A/AA, keyboard, focus, responsive and target-size suites; Windows visual baselines |
| Live Google delivery sign-off | Superseded: Google OAuth is configured locally and used for outreach Gmail drafts and for sending an approved email after the student presses Send and confirms the recipient. The Cloud project stays in Testing mode, so consent is renewed about weekly | OAuth, encrypted-token, Gmail-draft and Gmail-send tests |
| Live Microsoft delivery sign-off | Out of scope: no Microsoft account in use | OAuth path stays disabled until credentials exist |
| Live SMS/voice/web-push transports | Out of scope: SMTP email (disabled by default) and in-app reminders are the shipped channels | Sandbox provider, STOP/opt-out and destination-safety tests |
| External alert routing and distributed tracing | Out of scope: one local machine | Local structured logs, request traces, p95/error metrics and SLO evaluation |
| Employer ranking thresholds and outcome-review policy | Out of scope: no employer users | Criterion-level evidence and honest "unavailable" metrics remain |
| Public signup abuse review | Out of scope: public signup stays off unless the `allow_public_signup` flag is set | Cross-user isolation suite |

Reopen a row if the deployment model changes, for example a hosted build or a
second user.
