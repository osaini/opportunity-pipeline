# Duplicates and re-listed roles

How the same opportunity arriving through several channels is linked rather than deleted.

## Duplicate handling

The same opportunity often arrives through several channels. Duplicates are
linked rather than deleted — one row is canonical and the rest point at it via
`duplicate_of`, so nothing is lost and the shortlist doesn't repeat itself.
Matching runs in three passes:

1. Identical company + title + location.
2. Same company and title across sources whose locations don't contradict each
   other.
3. Near-identical description bodies across *different* sources.

The second pass exists because channels format locations differently: Greenhouse
packs several cities into one field
(`"Austin, Texas, United States; South San Francisco, California, United States"`)
where LinkedIn gives `"Austin, TX"`. Comparing city tokens links those, while
genuinely different cities — the same title in Austin and in Boston — stay
separate. A blank location counts as unknown rather than conflicting, the same
way the scorer declines to penalise an uninformative location.

The third pass exists because the first two both key on the company name, so
neither can reconcile a posting that arrives from the employer's own board *and*
from a channel that restyled the company and rewrote the title. Employers rarely
rewrite the requirements text, so the body is the reliable key: each description
gets a 64-bit SimHash fingerprint over 3-token shingles, and two postings are
linked when at least 92% of those bits agree (at most 5 of 64 differ —
near-verbatim only). Descriptions under 200 characters carry too little signal
and are never fingerprinted, so a thin posting is never falsely merged.

That pass only ever links across *different* sources, and only where locations
don't contradict: one employer legitimately posts several requisitions off the
same JD template, and the same JD used for two cities is two opportunities.

**What it does and doesn't catch (measured 2026-08-05).** Across 217
fingerprintable postings and 369 comparable cross-source pairs, this pass linked
nothing — and the measurement is the reason the threshold stays at 0.92 rather
than being relaxed:

| Pair | Similarity |
| --- | --- |
| Same Figure job, Greenhouse vs Adzuna (a true duplicate) | 0.781 |
| Two *different* Figure roles, sharing company boilerplate | 0.719 |
| Neuralink vs Base Power, unrelated roles | 0.703 |

Only 0.06 separates a true match from a false one, so any threshold low enough
to catch the real pair would also merge unrelated companies' postings. The
reason the true pair scores so low is that **Adzuna truncates every description
to exactly 500 characters** — its copy is a prefix of the real body, not a
near-verbatim mirror. That case is already handled by pass 2 anyway, since the
company and title match. Pass 3 earns its keep on sources that carry full
bodies, which is what `enrich` gives agent-discovered rows.

Canonical is the furthest-along copy by status, then an ATS source over an
`agent:`/`manual:` one, then the longest description — so a tracked application
is never demoted to a duplicate of an untracked row.

### Re-listed roles

Separately from duplicate linking, scoring adds an informational note when a
role went away and came back at a *different* URL within 90 days:

> FLAG: this role has been listed under 2 different URLs since 2026-06-14 — may
> be an evergreen or re-listed req

Cohort markers are ignored when comparing roles, so "Mechanical Intern (Summer
2027)" and "Mechanical Intern [Fall 2026]" count as the same role.

Two conditions must both hold: an earlier posting of that role must have been
*retired*, and the live one must be at a *different URL*. So terms advertised
side by side are not flagged, and neither is the same URL going inactive and
coming back.

What this does flag, correctly, is a role an employer re-posts each cycle — the
12 hits on the current data are all SpaceX seasonal requisitions, where "Fall
2026 Engineering Internship/Co-op" closes and "Spring 2027" opens at a new URL.
That is what the "evergreen" half of the wording refers to; it is a recurring
pipeline posting rather than a live opening created for you, which is worth
knowing before you spend effort on it.

**The flag never changes the score.** A re-listed requisition is often just an
evergreen posting or an ATS migration — this is information for you, not a
verdict on the employer.

Back to the [README](../../README.md) index.
