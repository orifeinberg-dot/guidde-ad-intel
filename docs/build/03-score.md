# Stage 03 — `src/score.py`

RETROSPECTIVE SPEC. Stage 03 was built through direct instruction and never got a spec
file at the time; this records what the stage actually became, to match the convention
that every milestone has a `docs/build/NN-*.md`. It is a record, not a build order.

## Goal
Run the deterministic scoring engine over the 94 deduplicated creatives in
`data/ads.json`, produce the ranked cluster table and the winning creative, and write
`data/winner.json` — the hand-off artifact between Part 1 and Part 2.

No LLM, no network. Pure arithmetic, so every number is inspectable and reproducible.

## I/O
- reads `data/ads.json` — dedup.py's output: 94 distinct creatives, ranks dense 1..94
- writes `data/winner.json`

Both paths were previously repo-root relative (`ads.json`, `winner.json`), which did not
match the rest of the pipeline and would not run on a clean checkout. Fixed as part of
this stage; the change was three path literals and touched no logic.

## Definition of best — unchanged
`PatternScore = norm(frequency) x norm(performance)`, a product, so a pattern wins only if
it is BOTH heavily repeated AND its instances survive. Clustering is on
`(format, structure_type)`. Clusters of size <= `OUTLIER_MAX_SIZE` (2) are unproven
experiments, reported and excluded from winner selection. None of this changed in stage 03.

## score.py was unfrozen twice — both recorded

### 1. Unit bug in perf (correctness fix)
`impressions_per_day = exposure / days_running` assumed `exposure` was a cumulative
lifetime impression count. It is not: for the 83 of 94 creatives with no usable impression
bucket, the reach signal is Meta's impressions-descending SORT ORDER — a CURRENT STANDING.
Dividing a standing by age is a unit error and the result has no meaning.

Measured effect: a ~6x recency bias (median ipd 0.128 for creatives <= 7 days old vs 0.021
for >= 30 days); all of the top 8 by ipd were 1-3 days old. It also INVERTED the model's
own intent — longevity is meant to be evidence a pattern is proven, but age had become a
divisor, i.e. a penalty. It decided the outcome: the pre-fix winner was rank 36 of 94 and
three days old, from the cluster with the youngest age profile.

Fix: never divide reach by age. Reach is a standing in [0,1]; longevity is a separate,
positive component of perf (`W_REACH` / `W_LONGEVITY`, 0.5 / 0.5). Both reach sources map
to a within-set [0,1] standing, so a bucket midpoint (30,000) and a rank standing (0.63)
never share one min-max scale. After the fix, creatives >= 30 days old score 4.7x the
<= 7 day ones — age reads as evidence.

This is a correctness fix, not a redefinition of best: freq, the product form, clustering
and `OUTLIER_MAX_SIZE` are byte-identical.

### 2. Video winner-selection constraint (selection, not scoring)
`WINNER_FORMATS = ("video",)` restricts which clusters may produce the winner, because
Part 2 has to produce a shot-by-shot storyboard and a static image cannot be storyboarded.

Applied AFTER scoring. The scoring math is untouched and the full ranked table still prints
EVERY cluster, static included, so the finding that static patterns dominate Scribe's mix
stays visible rather than hidden by the constraint. `score.py` prints BOTH winners and
`winner.json` records the unconstrained one alongside the selected one.

## Output — what the stage prints
- the ranked cluster table: every proven cluster with `n`, `freq`, `perf`, `PatternScore`
- the excluded outlier clusters and their ad_ids
- the UNCONSTRAINED winner (transparency)
- the VIDEO-CONSTRAINED winner (what `winner.json` carries), with the video-eligible
  clusters listed and any tiebreak named
- a sensitivity table: does the winning cluster survive a different perf blend?

## The winner
`1155616497059211` — `video / talking_head_testimonial`, rank 3 of 94, started 2026-08-03
(40 days running), reach standing 0.979, perf 0.864.

## Known limitations — state these plainly, do not bury them
- **The winner is not weight-robust.** The top three clusters sit within 0.005 of each
  other on perf (0.298 / 0.303 / 0.303), so the weighting, not the evidence, decides. The
  unconstrained winning cluster FLIPS from `image/text_animation` to `image/screen_demo`
  at reach weights >= 0.7, and the winning creative changes under four of five weightings.
  Only the default 50/50 produces the reported unconstrained winner.
- **Product + min-max annihilates axis-minimum clusters.** Whichever cluster holds the
  minimum on either axis normalises to exactly 0, and the product sends PatternScore to 0
  regardless of the other axis. `video/talking_head_testimonial` (n=30, the single
  most-repeated pattern) scores 0.000 on minimum perf; `video/text_animation` scores 0.000
  on minimum freq despite the highest perf of any cluster (0.681). With only 7 clusters,
  min-max guarantees at least one zero per axis.
- **The video winner was chosen by a tiebreak, not by PatternScore.** Both video-eligible
  proven clusters score exactly 0.000 (see above), so ordering by PatternScore alone would
  pick by dict insertion order. The tie breaks on cluster size `n` — 30 vs 3 — which is the
  tiebreak most faithful to the stated definition of best, "the most-repeated,
  durability-proven formula". Recorded in `winner.json` as
  `patternscore_tie_broken_on_cluster_size`. Note `video/text_animation` would have won a
  perf-based tiebreak (0.681 vs 0.233).
- **Reach is an ordering, never a measured quantity.** Meta publishes no spend or
  impressions for commercial ads. Read every reach number as "ranked higher than".

## Constraints
- Modify only `src/score.py`; clustering, freq, perf, the product form and
  `OUTLIER_MAX_SIZE` stay byte-identical.
- No LLM, no network, no CLI parser, no deployment. Deterministic script.
