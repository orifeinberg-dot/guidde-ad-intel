# Guidde take-home — competitor ad intelligence → creative brief

Part 1 finds Scribe's best-performing Facebook ad; Part 2 rebuilds it as a Guidde
creative brief. Run order is fixed: `scrape → extract → dedup → score → generate`.

## The deliverable — open this first

**[`results/storyboard.html`](results/storyboard.html)** — the creative brief and all 10
storyboard shots in one page. Self-contained: every frame is embedded, so it renders by
double-click in any browser, even if the file is moved or emailed.
**[`results/storyboard.pdf`](results/storyboard.pdf)** is the same content if you prefer PDF.

Part 1's reviewable artifacts sit alongside them: `results/02_scoreboard.md` (the ranked
pattern table) and `results/03_winner.json` (the winning creative).

## Limitations & known issues

**Meta publishes no spend or impression data for commercial ads.** `impression_bucket`
is null for 83 of 94 creatives (the 11 that carry one carry `"<100"`, which is not a
tier in `BUCKET_MIDPOINTS`). So the reach signal is really Meta's impressions-descending
**sort order** — a relative standing, not a measured quantity. Every reach number in the
output is an ordering, and should be read as "ranked higher than", never as impressions.

**Fixed 2026-09-12 — a unit bug in `perf`.** `score.py` originally computed
`impressions_per_day = exposure / days_running`. That treats the reach signal as a
cumulative lifetime total, but the rank fallback is a current standing, so the division
was a unit error. It produced a 6.1x recency bias (median ipd 0.128 for creatives ≤7 days
old vs 0.021 for ≥30 days) and inverted longevity from evidence-of-proven-ness into a
penalty. The pre-fix winner was a rank-36, three-day-old creative. Reach is now a standing
in [0,1] and longevity is a separate positive term. `score.py` was unfrozen for this one
correctness fix; the definition of "best" (freq × perf, product form, clustering,
outlier threshold) is unchanged.

**Winner selection is restricted to video creatives.** Part 2 produces a shot-by-shot
storyboard, which a static image cannot support, so `WINNER_FORMATS = ("video",)` in
`score.py` limits which clusters are eligible to win. The scoring math is unchanged and the
ranked table still shows every cluster — including the finding that **static patterns
dominate Scribe's mix** (the unconstrained winner is `image/text_animation`). Both winners
are printed and `winner.json` records the unconstrained one alongside the selected one.

Note the constraint is doing real work: the two video clusters both score PatternScore
0.000 (see the min-max brittleness below), so the choice between them falls to a documented
tiebreak on cluster size — `video/talking_head_testimonial` (n=30) over
`video/text_animation` (n=3). The selected creative is `1155616497059211`.

**Storyboard frames are references; the end card is a brand composite.** Shots 1-9 are
AI-generated visual references (`gpt-image-2`, 1024x1536, portrait) — mood and composition
guides, not finished art, and deliberately free of text since image models garble type
(captions and VO are metadata beside each frame). Shot 10, the end card, is NOT generated:
it is assembled in HTML/CSS over the real Guidde vector wordmark (`assets/guidde_logo.svg`)
with the CTA as real type on a real button in Guidde red `#CB0000`. Brand lockups are
graphic elements an image model cannot render reliably, so the brand is applied as a real
vector overlay — which is also how it is done in production.

**The winner is NOT robust to the perf weighting.** The winning *cluster* flips between
`image/text_animation` and `image/screen_demo` depending on how reach and longevity are
weighted, and the winning *creative* changes under four of five weightings. See the
sensitivity table printed by `score.py`. Treat the winner as one defensible reading, not
a determined result.

**Min-max over 7 clusters makes PatternScore brittle.** Whichever cluster holds the
minimum on either axis is normalised to exactly 0 and its product collapses to 0 —
regardless of the other axis. `video/talking_head_testimonial` (n=30, the single
most-repeated pattern) scores 0.000 because it holds the minimum perf; `video/text_animation`
scores 0.000 despite the highest perf of any cluster, because it holds the minimum freq.

**Duplicate creatives.** Scribe runs everything as DCO and Meta registers one creative
under several `ad_archive_id`s — 152 records carry 94 distinct creatives. `dedup.py`
collapses them by SHA-256 of the media bytes before scoring. Two creatives whose duplicate
instances received conflicting labels were resolved by majority vote; one 1–1 tie was
resolved by a human viewing the creative (`MANUAL_LABELS` in `dedup.py`).

**`structure_type` is an LLM judgement.** `format` is ground truth from the scrape, but
structure is inferred — from 3 sampled frames for video ads, a single still for image ads.
Measured self-consistency: on identical media bytes the model disagreed with itself on
2 of 46 duplicate groups (~4%).
