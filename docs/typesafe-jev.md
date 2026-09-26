# TypeSafe Jev integration

## Why Jev fits this project

Jev accepts shared state plus typed questions and returns only bounded Choice,
Score, or Noul answers. It does not generate prose. That makes it useful at the
specific seam where deterministic extraction ends and a student still needs a
semantic judgment. TypeSafe recommends keeping control flow and arithmetic in
code, decomposing broad judgments into atomic questions, and routing uncertainty
to review. That matches this project's source-integrity rules.

The integration therefore does **not** replace `pipeline.py` scoring. It adds an
explicit, read-only second opinion in the opportunity detail panel:

- semantic role alignment (Score)
- explicit skill overlap (Score)
- education compatibility (Choice)
- experience compatibility (Choice)
- work-authorization compatibility (Choice)
- term and schedule compatibility (Choice)
- deadline clarity (Choice)
- compensation clarity (Choice)

All eight questions share one bounded state and run in one System One request.
They remain independent; code does not ask Jev to blend them into a hidden
overall ranking.

## Trust and privacy boundary

The user must click **Run Jev review**. The review makes no refresh-time,
ingestion-time, or background calls. The result is not persisted and cannot
modify an opportunity, profile, score, application, or outreach record.
Inbox suggestions (below) are the one other use, and they run only after the
student turns them on.

The external request sends these opportunity fields when present:

`company`, `title`, `location`, `role_type`, `description`, `terms`,
`graduation_years`, `remote_mode`, `compensation`, and `source_name`.

It sends only these profile fields when present. The UI names the sensitive
eligibility categories before the user clicks, rather than hiding them behind a
generic "profile data" label:

`degree`, `graduation_year`, `degree_keywords`, `preferred_role_types`,
`regions`, `remote_ok`, `willing_to_relocate`, `skills`, `interest_keywords`,
`max_years_experience`, `work_authorized_us`, `us_citizen`, `requires_sponsorship`,
`hours_per_week`, `available_terms`, and `compensation_preferences`.

It intentionally excludes name, school, contact details, notes, application
history, answers, full experience/project narratives, and resume text. TypeSafe
states that customer data is not used for model training; its standard service
still processes data remotely, and zero-data-retention is documented as an
enterprise option. Review TypeSafe's current legal terms before using real data.

## Source-integrity behavior

- Every response is labeled `kind: ai_suggestion` and `confirmed: false`.
- Compatibility choices include `unclear` and `not_applicable`; missing profile
  data is explicitly described as unknown, not proof that a student lacks it.
- Potential conflicts remain prompts to verify. They are never written back as
  eligibility facts.
- Deadline questions ask only whether a date is explicit, ambiguous, or absent.
  Jev never compares dates; the existing parser and code own date arithmetic.
- Numeric scoring and score clamping stay in deterministic code.
- The resolved model ID and question-set version are returned so an alias or
  prompt change cannot be mistaken for the same evaluation.

The default model is pinned to `jev-1.13.0`. Change `TYPESAFE_MODEL` deliberately
after testing a new model against labeled examples; do not silently move tuned
confidence thresholds to a new alias.

## Inbox suggestions, with a keyword-rule fallback

`opportunity_app/inbox_classifiers.py` asks Jev one Choice question for each of
two suggestions the student already confirms by hand:

- the outreach status a pasted cold-email reply points to (offer, call scheduled,
  paused, declined, replied), in `outreach.log_reply`;
- the kind of an application email a connector delivers (offer, rejected,
  interview, confirmation, deadline, recruiter reply, unknown), in
  `connections.ingest_message`.

Not every copy has Jev access, so the keyword rules that shipped before
(`suggest_reply_status`, `classify_monitored_message`) stay the fallback. They
answer whenever any of these holds:

- the student has not turned on **Suggest reply and email outcomes with Jev**
  (Outreach settings). This is a per-student `user_settings` row, off by
  default, because it sends message text to TypeSafe;
- `TYPESAFE_API_KEY` is not set, or the TypeSafe configuration is invalid;
- the request times out (8 seconds, two attempts), is rate limited, or returns
  an answer this app cannot read;
- Jev's top answer is less than 50% sure.

Each result records which one answered: a reply suggestion carries `source`
(`jev` or `rules`), `confidence`, `model`, and `fallback_reason`; a monitored
event stores the same under `payload.classified_by`. The UI says which one made
each suggestion. Logging a reply never fails because TypeSafe is down.

Why these two: in a blind test on 2026-09-25 (gold labels from Claude and Codex
labelling independently, with a third labeler settling disagreements), Jev's
suggestion matched on 90% of 100 replies against the rules' 58%, and 82% of 100
application emails against 41%. Both sets were synthetic, so treat these as
evidence for the choice, not as accuracy on real mail. The question wording in
`inbox_classifiers.py` is the tested wording; change it only with a new test.

The same test found three places Jev should **not** go yet. Posting titles and
most posting fields (role type, seniority, citizenship, remote mode, graduation
years) were already 92–99% accurate with the rules. On "will not sponsor" and
season/term, Jev was worse than the regex. Chat intent routing failed a
pre-registered check on 120 fresh messages: it matched the topic but routed 16
open questions to canned answers, against the keywords' 13, often at 0.9+
confidence.

## Configuration

```dotenv
TYPESAFE_API_KEY=...
TYPESAFE_MODEL=jev-1.13.0
TYPESAFE_BASE_URL=https://api.typesafe.ai/v1
TYPESAFE_TIMEOUT=20
```

The HTTP adapter retries only TypeSafe's documented transient `429` and `529`
responses, honors a short `Retry-After`, verifies the returned answer types and
option sets, and never disables TLS verification. The API key stays server-side.

## API

- `GET /api/v1/typesafe` reports configuration and model metadata without a key.
- `POST /api/v1/opportunities/{id}/jev-review` performs the explicit review.

Both require normal student authentication. Cookie-authenticated POST requests
also pass through the existing CSRF middleware.

## Known Jev 1.13 limitations accounted for

TypeSafe documents literal reading, weak arithmetic/date comparison, degraded
accuracy with irrelevant context, susceptibility to adversarial state, and
poor generation as current jagged edges. This implementation responds by using
precise atomic criteria, keeping math and dates in code, minimizing state,
telling the model the posting is untrusted data, exposing uncertainty, and not
asking Jev to generate text.

Primary references:

- [System One API reference](https://docs.typesafe.ai/api)
- [How to build with TypeSafe](https://docs.typesafe.ai/concepts/how-to-build-with-system-one)
- [Confidence guidance](https://docs.typesafe.ai/confidence)
- [Jev 1.13 jaggedness](https://docs.typesafe.ai/model-jaggedness/jev-1.13)
- [Models and version aliases](https://docs.typesafe.ai/models)
- [TypeSafe legal and data-processing links](https://docs.typesafe.ai/legal)

