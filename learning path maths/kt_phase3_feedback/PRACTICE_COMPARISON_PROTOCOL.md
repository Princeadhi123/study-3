# Practice comparison: source-masked review and oracle sensitivity

Version `practice_comparison_v1_20261006`, specified before creating the
comparison outputs. Reuse the frozen ten-case smoke inputs and decisions;
do not regenerate KT predictions, change the pool/band, or select favorable
cases. This study does not enable learner delivery.

## Question and comparison

Compare the recorded observed-error baseline with the recorded KT-assisted
practice policy. Starting assessment answers, practice pool and feedback are
identical within each case. Recommendation differences include abstention.
These ten scripted synthetic cases are not independent real students.

All 20 candidate item IDs are warm. The warm/cold assessment-history labels
do not establish cold-candidate performance. No actual candidate outcomes,
learning gains or independent human ratings are available at preparation.

## Source-masked review

Include all ten cases, including the two cases where neither method recommends
a question and the case where only the baseline recommends one. Assign opaque
task IDs, shuffle task order, and counterbalance which method appears as A:
five baseline-A and five KT-A assignments, permutation seed 20261006.

Show synthetic observed topic counts, the forty assessment questions/selections
and correctness, and the two suggested questions or absence of a suggestion.
Hide method names, original case names, warm/cold labels, item/question IDs,
KT probabilities, target-band details and investigator outcomes. Preserve
original question wording and option order. Reviewers need mathematical and
Finnish-language competence for this content; no unreviewed translation is
introduced.

Reviewers receive only the `reviewer/` directory. Keep source decoding keys,
simulator outputs and provenance in `investigator/`. Abstention/content style
can reveal method identity, so call this source-masked, not guaranteed blinded.
At least two reviewers should judge independently before discussing differences.
Pilot the instructions for clarity before a formal review; changes require a
new rubric version, not silent relabeling of earlier judgments.

For each alternative, record these separate categorical judgments:

| Dimension | Question | Choices |
|---|---|---|
| Relevance | Is this question relevant to the observed errors and assessed content? | appropriate / needs_revision / uncertain / not_applicable |
| Mathematical validity | Is the stem self-contained, with exactly one correct offered option? | appropriate / needs_revision / uncertain / not_applicable |
| Clarity | Can the learner understand what to do from the wording and options? | appropriate / needs_revision / uncertain / not_applicable |
| Challenge fit | Is this a reasonable next practice activity given the supplied evidence? | appropriate / needs_revision / uncertain / not_applicable |
| Claim scope | Does the displayed suggestion make unsupported mastery, misconception or learning-benefit claims? | no_concern / concern / uncertain / not_applicable |

Use `uncertain` when observed answers are insufficient, particularly for
challenge fit; do not assume a difficulty or mastery state from counts.
When no question is suggested, question-specific dimensions are
`not_applicable`. Record a separate paired preference A/B/either/neither/
insufficient_information plus a reason. Absence is not automatically a bad
recommendation. There is no composite score, automatic approval threshold,
or default assertion that one selection is uniquely correct.

Ratings templates remain blank. The coding agent does not impersonate
independent reviewers. After reviews, report dimension-level judgments,
disagreements, review coverage and blinding limitations. Cases, repeated
candidate questions and reviewers must not be treated as independent learner
outcome trials.

## Independent conditional response-oracle sensitivity

This is a transparent hypothetical continuation model, not a fitted or
validated learner simulator and not a coherent re-generation of the original
forty-response histories. It conditions on the existing scripted answers.
Its parameters are specified after the smoke decisions were known; report
this as exploratory sensitivity, not preregistered external validation.

For each topic with `c` correct among `n` observed answers:

```text
observed_rate_proxy = (c + 1) / (n + 2)
theta = log(observed_rate_proxy / (1 - observed_rate_proxy))
guess = 1 / option_count
slip = 0.05
assumed_p = guess + (1 - guess - slip) * sigmoid(theta - difficulty)
```

The proxy is an explicit assumption, not measured ability/mastery. The oracle
does not use KT probabilities, rankings, the selected method or simulator
outcomes to define `assumed_p`. Candidate difficulties are unknown in reality;
evaluate six illustrative worlds:

- All candidates have difficulty -1.5.
- All candidates have difficulty 0.
- All candidates have difficulty +1.5.
- Heterogeneous difficulties drawn uniformly from [-3, 3], seed 17.
- The same heterogeneous construction with seed 29.
- The same heterogeneous construction with seed 43.

Generate each heterogeneous assignment in sorted question-ID order and reuse
it for every case and both policies. These random values are not estimates
of actual item difficulty and are not expected to match the cues learned by KT.
No world is removed or adjusted to favor a method.

For each case/candidate, generate one reproducible Bernoulli response from
`assumed_p` using a SHA-256-derived uniform draw, response seed 20261006,
original case identity and question ID. The same hypothetical question has
the same draw for both policies within a case. Synthetic correctness is not
a human rating or an observed real response.

Primary comparison is descriptive distance from the declared challenge target
0.70, using ORACLE probabilities, not KT probabilities. Compare only cases in
which BOTH policies select a question (seven smoke cases); report the
KT-minus-baseline mean distance separately per world. Lower is closer under
that world, not necessarily better teaching. Also report assumed in-band
[0.60, 0.80] counts and simulated correct counts on those same paired cases.
Do not infer learning from a higher correctness rate.

Separately report availability: baseline-only, KT-only, both-selected and
neither-selected cases. Do not silently drop abstentions or score them as
incorrect answers. No confidence intervals or significance test: increasing
the number of hypothetical worlds would not create independent student data.

## Outputs and interpretation

`artifacts/practice_comparison_20261006/` stores a hash-bound reviewer pack,
blank rating/preference CSVs, an investigator decoding key, all six difficulty
assignments and oracle probabilities/draws, per-world paired comparisons,
and the source hashes/method/protocol.

Conclusion must distinguish software constraints, reviewer judgments and
assumption-dependent outcome simulation. Superiority under an assumed world
does not establish real recommendation quality. Real learning claims need a
separately approved student study with appropriate assignment and common
outcome questions. Original smoke/Phase 4 outputs remain frozen.
