# Ranking and eligibility

How `config/profile.json` shapes the score, how regions work, and how the score is built. Every adjustment is recorded as a reason, so the explanation on each posting accounts for the number.

## Personalize ranking and eligibility review

Edit `config/profile.json` (gitignored; setup creates it from
`config/profile.example.json`, which starts empty). These eight answers
materially improve ranking, eligibility checks, or the decisions you make from
the shortlist:

1. What year do you graduate?
2. Which terms are you available: fall, spring, summer, and what year?
3. Which locations are acceptable, and will you relocate?
4. How many hours per week can you work during classes?
5. What tools and methods can you honestly claim (for example Python, MATLAB,
   CAD, lab techniques, statistics, a framework you have shipped with)?
6. Which fields interest you most, in your discipline's own terms (for example
   robotics, data, energy, biotech, finance, controls, product design)?
7. Are you authorized to work in the U.S., and will you require sponsorship?
8. Are unpaid or for-credit research/externships acceptable, or only paid work?

Keep `skills` factual. Put aspirations in `interest_keywords`.

### Target regions

`regions` in `config/profile.json` is what makes the pipeline geographically
picky. The template ships with none. For example, a region can target
**Austin on a close radius** (the metro and its immediate commuter towns) or the
**Bay Area on a medium-large radius** (the whole nine-county spread, Santa Rosa
and Gilroy included); `tests/fixtures/profile_student.json` has both.

There is no geocoding here, so a "radius" is just how long that region's
`places` list is — widen a region by adding towns, tighten it by removing them.
Each region takes:

| Field | Meaning |
|---|---|
| `name` | Label shown on the dashboard chip |
| `radius` | Free text, quoted back in the score explanation |
| `bonus` | Points added when a posting matches |
| `state_markers` | Required state tokens, e.g. `["ca", "california"]` |
| `places` | Cities/counties, matched only alongside a state marker |
| `aliases` | Phrases unambiguous on their own, e.g. `"bay area"` |

The state marker is load-bearing: Newark, Dublin, Richmond, Concord and
Berkeley all name a Bay Area city *and* a well-known city elsewhere, so a bare
city name is never enough on its own.

Anything that matches no region takes `out_of_region_penalty` (default 40),
which is heavy enough to sink it below every genuine match without hiding it —
scores clamp at 0, so out-of-area postings collect at the bottom rather than
disappearing. Remote roles still score `+8` while `remote_ok` is true. A
posting whose location names no place at all — blank, or a Workday-style
`"3 Locations"` placeholder — is left alone rather than penalised, since a
sparse location field is not evidence the role is elsewhere.

Delete `regions` entirely to fall back to the older, gentler
`preferred_locations` keyword scoring.


## How scores are computed

Scores are deliberately simple: preferred role type (+18), degree match (up to
+15), interests (+20), demonstrated skills (+15), preferred location (+10),
term availability (+8), recency (+10), and penalties for seniority, experience,
relocation, discipline, degree level, or explicit availability mismatches.
Matches in a title count more than incidental words in a long description. A
title that names the degree levels it takes ("MS/PhD", "Intern, BS",
"Bachelor's", "MBA") and none of the student's takes -35, the same as a senior
title; the student's level comes from `degree` ("B.S. ...", "M.S. ...",
"Ph.D. ..."). "Graduate" and "New Grad" name no level, a description is never
read for one, and a `degree` that names no level changes nothing
(`setup validate` warns about it). List
disciplines you don't want in `deprioritize_title_keywords` if their titles are
ranking too high, and remove entries there if they are ranking too low. Citizenship and sponsorship language is
flagged for human verification; a sponsorship penalty is applied only when the
profile explicitly says sponsorship is required. The sponsorship check reads the
ways a posting closes sponsorship, for the company or for one opening ("no
sponsorship", "we do not offer visa sponsorship", "immigration sponsorship is not
offered for this specific opening", "authorized to work without sponsorship").
A form question ("Are you authorized to work without sponsorship?") is not a
statement by the company and is not read. "Without sponsorship" counts only as a
condition on working, so "F-1 students can intern under CPT without visa
sponsorship" and "candidates with and without sponsorship needs" are not read as
closed. A sentence that also says the company does sponsor ("we cannot sponsor
F-1 interns, but we sponsor H-1B") gets the flag and no penalty, since which part
applies to this opening is yours to check.

**Experience.** "N years of ... experience" costs 18 points when N is more than
`max_years_experience` (1 if unset). N may be a digit or a word ("six (6) years",
"three years"), may carry "or more", and for a range ("3-5 years", "3 to 5
years", "between 2 and 4 years", "2 years and up to 5 years") the first number is
the one read, since that is what you have to meet. Years that say something else
("a two year program", "founded five years ago", "18 years or older") are not read.
A posting that counts the years from graduation ("1-3 years of full-time
professional experience post-graduation") asks for experience an internship does
not give. If `graduation_year` is this year or later, that costs the 18 points
whatever `max_years_experience` says, and the reason reads "asks for 1+ years of
post-graduation experience". If you graduated in an earlier year the ceiling
decides as usual, and with no `graduation_year` the score does not guess: it adds
a flag to verify instead. Full-time professional experience with no mention of
graduation is judged by `max_years_experience` like any other, and the reason
names it ("asks for 3+ years of full-time professional experience").

**Pay.** `compensation_preferences` in the profile is read for two things. A posting
that states pay in dollars per hour and tops out below `minimum_hourly` costs 15
points ("pays up to $22/hour, below your $25/hour minimum"). With `paid_only` set to
true, a posting that calls the role unpaid ("an unpaid internship", "a volunteer
role") costs 35; "unpaid leave" in a benefits list, or "unlike an unpaid
internship", is not that. Only a stated hourly wage is compared: a yearly salary is not
turned into an hourly rate, a posting that also states a yearly, monthly, weekly or
daily figure is not compared at all, a shift differential, parking rate or donation
per hour is not a wage, a posting that states no pay changes nothing, and a
currency other than USD (including "CA$30 per hour") is not compared with dollars.

**Text aimed at AI readers.** A posting that speaks to a model ("if you are an LLM,
include the word ...", "ignore all previous instructions") gets a flag, with no
change to the score. A company that wrote it wants a model's answer to differ from
a person's, so read that posting yourself. Every request the app makes to a model
through the Claude Code and Codex command lines or the OpenAI and Anthropic APIs
also carries a standing note that text from postings, pages and emails is evidence,
never instructions.

Back to the [README](../../README.md) index.
