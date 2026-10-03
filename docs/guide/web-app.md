# The web app

The FastAPI web app, the student workspace, the AI agent and Jev reviews, and what to read before hosting it. To keep the app running without a terminal, see [scheduling.md](scheduling.md).

## Full opportunity platform

The repository also implements a clean-room, UTern-inspired
product: a separate product database, versioned role-scoped API, responsive student
workspace, employer/admin workspaces, durable worker, and Apply Mode extension.
The original CLI remains dependency-free and continues to own ingestion and scoring.

Install the web dependencies, create the local files, and open the app:

```bash
python -m pip install -r requirements-web.lock
python -m opportunity_app.setup init
python -m opportunity_app.launch open
```

`launch open` starts the server in the background if it isn't running, then
opens your browser already signed in. It trades `PIPELINE_WEB_TOKEN` from `.env`
for a one-time ticket, so there is no token to copy; the session lasts 30 days.
`launch stop`, `restart`, and `status` manage the server, and
`launch install-autostart` starts it at login on Windows, macOS, or Linux.
Running `python -m opportunity_app.api` directly still works, and prints the
token for the sign-in page's **Owner token** field.

On a computer nobody else uses, `PIPELINE_SKIP_SIGN_IN=1` in `.env` turns sign-in
off: every page this computer's browser opens, from a bookmark or a typed
address, is signed in, and each visit renews the 30-day session. Restart the
server after changing it (`launch restart`). It applies only to a server that
answers to `127.0.0.1` and `localhost` alone (the launcher and the scheduled task
start it that way), never in production, and never to another machine; a
browser signed in to a student account keeps that account. Signing out shows the
sign-in page until the next reload. Any program running on the computer can open
the app as you while it is on, so leave it off on a shared computer.

Important properties:

- `data/pipeline.db` is opened read-only and is never changed by the migration.
- Migrated data is written to gitignored `data/platform.db`.
- The migration is idempotent and checks active-unique counts plus the first 200
  ranked IDs against the legacy pipeline before it reports success.
- `/api/v1/*` data routes require the local access token or the signed session
  cookie created by the login screen.
- The web UI reads, filters, and searches opportunities; displays source and fit
  evidence; persists reversible save/pass actions; records an explicit Apply
  click without submitting the employer form; and provides an application
  tracker with audited stage changes.
- Profile includes onboarding completeness, editable goals/availability/location/
  compensation/authorization fields, and explicit confirmation records. Use
  **Export scoring profile** to download the exact JSON schema accepted by the
  legacy deterministic scorer.
- Resume ingestion accepts content-sniffed PDF/DOCX files up to 5 MB. Files are
  kept in gitignored private storage, scanned for test malware and active or
  embedded document payloads, parsed into drafts, and never copied into the
  profile until individual suggestions are selected and confirmed. Original
  files can be downloaded or permanently deleted from Profile.
- Rerunning the legacy migration refreshes opportunities but never overwrites a
  profile already edited in the web app or its confirmed-fact provenance.
- Preparation includes evidence-linked resume/cover-letter versions, reusable
  answers, and typed or visibly transcribed mock interviews. The student agent
  uses durable threads, auditable tools, budgets, abstention, and explicit
  approval cards for consequential actions.
- Apply Mode in `apps/extension` uses transient `activeTab` access, inventories
  Workday/Greenhouse/Lever/Ashby/SmartRecruiters/generic fields, requires review
  for sensitive fields, and has no final-submit capability.
- **Apply for me** (opt-in, Greenhouse only): rehearses a saved role's form in a window and sends nothing; **Finish in browser** fills it and leaves Submit to you. See `docs/assisted-apply.md`.
- Connections default to sandbox suppression. Optional Google/Microsoft OAuth
  uses PKCE, least-privilege read scopes, encrypted tokens, signed webhooks,
  preview/confirm tracker updates, quiet hours, verified phone state, and STOP.
- The private career dossier has classified evidence, pause/retention/export/
  deletion controls, granular expiring shares, revocation, and access logs.
- Market issues use hashed dated snapshots and an editorial publish gate.
- Outreach tracks cold outreach to companies with no posting (startup incubator
  portfolios, student accelerators). Targets live in their own tables, never in
  `opportunities` or `applications`, so they cannot pass as sourced postings.
  Each keeps its research source URLs, research date, and a contact confidence
  label (confirmed, unverified, unknown). Import CSV or JSON adds new companies
  and never overwrites existing ones (a matching name or website domain counts
  as existing); `data/outreach-*.json` is gitignored for personal research lists.
  See [Cold outreach pipeline](outreach.md#cold-outreach-pipeline).
- `/employer` and `/admin` use separate role tokens. Employer evidence access is
  consent-gated, protected/proxy rubric criteria are rejected, agent summaries
  cannot decide, and recruiter messages require approval. Admin controls expose
  verification, moderation, source health/control, flags, queue state, metrics,
  aggregate school reporting, and audit history.
- Account registration is invitation-gated; email/password login and sandbox
  recovery are available. Full account export and confirmed deletion are API
  operations, with private-file download/delete kept explicit.
- Interactive API documentation is available at `http://127.0.0.1:8765/docs`;
  the data endpoints still require authorization.

### Enable the AI career agent

The Agent tab supports OpenAI and Anthropic through the same audited tool and
approval runtime. Add either provider to the gitignored `.env` file, restart the
server, and select it when starting a thread:

```dotenv
OPENAI_API_KEY=your-key
OPENAI_AGENT_MODEL=gpt-5.4

# Or:
ANTHROPIC_API_KEY=your-key
ANTHROPIC_AGENT_MODEL=claude-sonnet-5
```

API keys remain server-side. The browser receives only provider/model names and
configuration status. Read-only questions can search the student's opportunities,
profile, applications, deadlines, and tasks. Saves, stage changes, new tasks, and
preparation documents appear as approval cards and execute only after approval.
Every model turn and tool run records its provider, model, status, token usage,
and request identifier. The Prepare tab can also use a configured provider for a
draft, while the no-AI grounded template remains the default.

### Enable on-demand Jev reviews

[TypeSafe Jev](https://docs.typesafe.ai/introduction) is used as a narrow
decision model, not as another career agent or a replacement scorer. Add the
credential to `.env` and restart the server:

```dotenv
TYPESAFE_API_KEY=your-key
TYPESAFE_MODEL=jev-1.13.0
```

Opportunity details then offer **Run Jev review**. One batched request evaluates
role and skill alignment, education, experience, authorization, and term
compatibility, plus deadline and compensation clarity. The result exposes each
choice or score, its probability distribution, confidence, resolved model, and
token usage. It is visibly labeled an unconfirmed AI suggestion, is not stored,
and never changes the deterministic 0–100 score or performs an action.

The click is also the privacy boundary: no TypeSafe request happens in the
background. The request includes the posting fields and only a fixed set of
non-contact profile fields needed for matching. It excludes the student's name,
school, contact details, notes, application history, and resume prose. See
[`docs/typesafe-jev.md`](../typesafe-jev.md) for the full contract, limitations,
and test strategy.

**Jev inbox suggestions** are a separate, per-student switch under Outreach →
Outreach settings, off by default. When on, the text of a reply you paste and of
an application email a connector delivers is sent to Jev to suggest its outcome.
Without a key, with the switch off, when TypeSafe errors, or when Jev is less than
50% sure, the keyword rules suggest instead, and each suggestion says which one
made it. Either way you confirm every change.

### Hosting, the worker and backups

Keep the direct development server bound to `127.0.0.1`. For hosted use, the API
supports `DATABASE_URL=postgresql://...`, HTTPS-only cookies in
`PIPELINE_ENV=production`, a separately deployed worker, and the container stack
in `infra/docker-compose.yml`. See `docs/THREAT_MODEL.md`, `docs/RUNBOOK.md`, and
`docs/PRIVACY_ACCESSIBILITY.md` before enabling live provider credentials.

Run the worker and encrypted backup drill locally with:

```powershell
py -3 -m opportunity_app.worker --once
py -3 -m opportunity_app.ops_cli backup backups/platform.enc
py -3 -m opportunity_app.ops_cli drill backups/platform.enc
```

Back to the [README](../../README.md) index.
