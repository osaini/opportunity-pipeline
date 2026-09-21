# Phase implementation verification

Last verified: 2026-08-23

This is the evidence ledger for the phases defined in
[`2026-08-platform-build-plan.md`](plans/2026-08-platform-build-plan.md). A phase is
marked **Pass** only when its repository-owned behavior is implemented and
exercised. External deployment, provider, policy, or human-review requirements
remain explicit open gates rather than being inferred from scaffolding.

| Phase | Implementation evidence | Verification evidence | Result |
|---|---|---|---|
| 0 — Legacy modularization | `pipeline.py`, `pipeline_core/`, locked dependencies, CI | 105 legacy tests; CLI/core parity tests | Pass |
| 1 — API, PostgreSQL, auth, shell | `api.py`, `auth.py`, `database.py`, migrations, worker, SPA | platform, auth, PostgreSQL CI contract, tenancy and browser suites | Pass; PostgreSQL contract is CI-only when no local server exists |
| 2 — Profile and resume | `profile.py`, `resumes.py`, private storage and deterministic document scanner | parsing/confirmation, hostile upload, version/delete, per-user score tests | Pass |
| 3 — Opportunity deck | tenant-scoped `read_model.py`, `actions.py`, responsive card/list UI and offline outbox | scoring/filter/action API tests plus browser student journey | Pass |
| 4 — Tracker | applications, immutable events, tasks, contacts, reminders, captures, import/export and board/list UI | tracker/capture/notification tests and browser views | Pass |
| 5 — Preparation | grounded/versioned documents, answer library, interview rubric, TTS, speech transcription, private audio recording | preparation regression plus browser/Node validation | Pass after 2026-08-23 recording implementation |
| 6 — Student agent | tool-backed durable threads, model adapters, budgets, audited proposals and approval boundary | deterministic/model-provider, prompt-injection and error-boundary tests | Pass |
| 7 — Apply Mode | MV3 extension, configurable loopback origin, provenance, sensitive-field review, resumable sessions, no-submit design | 9 extension fixtures across supported/generic/hostile DOMs | Pass |
| 8 — Connections and notifications | OAuth/PKCE, encrypted tokens, signed webhooks, preview/confirm, preferences, verified phone, outbox, destination-safe dispatch, SMTP and sandbox providers | OAuth, live-connector, digest/reminder/STOP/destination and e2e tests | Partial: production SMS, voice and web-push transports are not selected or implemented |
| 9 — Dossier and consent | classified evidence, memory controls, expiring item-level shares, access log and read-time revocation | dossier, consent and employer-boundary tests | Pass after tenant-safe private-file deletion fix |
| 10 — Market intelligence | reproducible snapshots, methodology flags, editorial publish gate and public archive | snapshot hash/recompute and publication tests | Pass |
| 11 — Employer/school/admin | verified organizations, rubrics, consented candidates, criterion-level ranking evidence, human decisions, ATS exchange, messaging/interviews, aggregate reports and moderation | role/consent/protected-criterion/explanation-integrity/admin tests | Conditional: ranking outcome data are insufficient, subgroup rates are unavailable without consented demographics, human override is inapplicable without automated decisions, and quality thresholds have not been agreed |
| 12 — Production hardening | threat/privacy docs, CSRF/rate limits, queues, structured logs, bounded request traces, p95/error metrics, SLO/alert evaluation, privacy-safe product aggregates, CI scans, encrypted restore drills, flags and runbook | security/operations tests, required fuzz job, browser/a11y suites and backup drills | Partial release gate: hosted rollout, manual screen-reader pass, external alert routing/distributed collector and live outage drills remain operator work |

## Defects corrected during this verification

- Browser mutations lost their CSRF header.
- Unicode credentials escaped authentication boundaries as server errors.
- Deep-link sign-in, mobile sign-out, and several accessibility paths failed.
- Opportunity scores, application state and Save/Pass intent leaked across users.
- New profiles did not receive their own deterministic scores.
- Extension answer reuse proposed sensitive-field values.
- CLI provider startup errors escaped the provider boundary.
- Account deletion erased unrelated users' files from shared storage roots.
- Mock-interview “recording” was transcription-only and never persisted audio.
- API fuzzing remained non-blocking after its pinned server errors were fixed.
- STOP left sandbox-suppressed notifications pending, and live phone channels
  resolved the user's email instead of the verified phone destination.
- Employer reporting mislabeled human rejections as overrides of an automated
  decision that the product deliberately does not make.

Every corrected defect has a live regression assertion; no expected-failure or
quarantine marker remains.

## Open gates requiring decisions or external state

**2026-09-17:** these gates are closed as out of scope for the local,
single-user deployment. The exception is the Google half of gate 3, which is
superseded because Google OAuth is now live for outreach Gmail drafts. See
[ACCEPTANCE.md](ACCEPTANCE.md#scope-decision--2026-09-17). The list is kept in
case the deployment model changes.

1. Select compliant SMS, voice, and web-push providers, approve budgets, and
   implement/test their transports. SMTP email is the only live notification
   transport currently shipped.
2. Agree ranking/explanation quality thresholds and an outcome-review protocol;
   decide whether to collect consented demographic outcomes for subgroup-error
   measurement. The current privacy-preserving behavior reports unavailable or
   inapplicable metrics honestly and never uses proxy attributes.
3. Deploy a hosted staging build, configure Google/Microsoft credentials, run
   live-provider outage/rollback drills, connect alerting/tracing infrastructure,
   and complete NVDA/VoiceOver/TalkBack review.
4. Obtain a user-controlled authenticated product specification if exact parity
   with private third-party behavior remains a requirement.
