# Scoreboard — ranked creative patterns

94 distinct creatives (deduplicated from raw ad records by dedup.py).
PatternScore = norm(frequency) x norm(performance), a product: a pattern wins
only if it is BOTH heavily repeated AND its instances survive.

| pattern (format / structure_type) | n | freq | perf | PatternScore |
|---|---|---|---|---|
| image / text_animation | 23 | 0.245 | 0.298 | 0.107 |
| image / screen_demo | 12 | 0.128 | 0.303 | 0.052 |
| image / before_after_comparison | 11 | 0.117 | 0.303 | 0.046 |
| image / other | 7 | 0.074 | 0.278 | 0.015 |
| video / text_animation | 3 | 0.032 | 0.681 | 0.000 |
| video / talking_head_testimonial | 30 | 0.319 | 0.233 | 0.000 |
| image / talking_head_testimonial | 3 | 0.032 | 0.411 | 0.000 |

## Outliers — unproven experiments, excluded from winner selection

Clusters of n <= 2 are too small to call a proven pattern.

- `video / conversational_demo` (n=2): 1035536132425989, 1542745056939087
- `video / screen_demo` (n=2): 1216554333972546, 1352293446894545
- `video / other` (n=1): 1104458769102878

## Winner

Unconstrained (top PatternScore, all clusters): `3252904458431344` — image / text_animation

Video-constrained (WINNER_FORMATS=('video',); Part 2 needs a storyboard): **`1155616497059211`** — video / talking_head_testimonial, rank 3 of 94, 40 days running.

Both video clusters score PatternScore 0.000 (min-max sends an axis-minimum cluster to zero and the product annihilates it), so the tie was broken on cluster size n — the most-repeated proven formula.
