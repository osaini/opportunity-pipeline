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

The user must click **Run Jev review**. There are no refresh-time, ingestion-time,
or background calls. The result is not persisted and cannot modify an
opportunity, profile, score, application, or outreach record.

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

