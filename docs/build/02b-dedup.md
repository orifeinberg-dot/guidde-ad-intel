# Stage 02b — `src/dedup.py`

RETROSPECTIVE SPEC. `dedup.py` was built mid-flight in response to something the data
turned out to be doing, and never got a spec at the time; this records what it became.
Sub-numbered `02b` so it slots between `02-extract.md` and `03-score.md` without
renumbering existing files.

## Goal
Collapse duplicate creatives so `score.py` ranks DISTINCT CREATIVES rather than repeated
records: **152 records -> 94 distinct creatives**.

## Why this stage exists — it was not in the original plan
Scribe runs everything as DCO (Dynamic Creative Optimization), and Meta surfaces the SAME
creative under multiple `ad_archive_id`s. Measured on this set: 58 of 152 records (38%) are
redundant. Group sizes: 48 singletons, 36 pairs, 8 triples, 2 quadruples.

Left alone this corrupts BOTH axes of `score.py`'s PatternScore:

- **frequency** — duplicate registrations count as independent repetitions, and the
  duplication rate differs per cluster (one cluster shrank 7 -> 3, another 16 -> 12). So
  clusters REORDER on the frequency axis, not merely rescale.
- **reach** — `impression_rank` is dense 1..N over RECORDS. One creative occupying ranks
  4, 5, 6 and 7 consumes four slots and pushes every genuinely distinct creative below it
  down by three, systematically depressing their rank-derived reach.

It was found by inspecting the `('video','conversational_demo')` cluster, which looked like
n=4 but was 2 distinct creatives shown as 4 records — enough to move it across
`OUTLIER_MAX_SIZE` and change which ads were eligible to win.

Dedup belongs in the data layer, before rank is final. It is NOT in `score.py`, which stays
frozen deterministic arithmetic.

## Dedup key: SHA-256 of the actual media bytes
The video for video ads, the still otherwise. Verified: 0 fetch failures across 152, and
cross-checked against an independent length-based pass that agreed at 94.

URL strings are NOT a safe key — identical creatives appear under different CDN paths
(grouping by path gave 128, not 94), and Facebook's signed URL params differ per fetch.

## Merge rules — provenance is merged, not picked at random
- `start_date` — **earliest** instance. That is the creative's true first-live date, which
  is what longevity in `score.py` is measuring.
- `impression_rank` — the **best (lowest)** rank any instance achieved becomes the
  creative's basis, then all 94 are **densely re-ranked 1..94** by that basis. This is the
  whole point: no creative's reach is depressed by another creative's duplicate slots.
- `impression_bucket` — any non-null value; on disagreement, the one tied to the best rank.
- descriptive LLM fields (`hook_type`, `angle_or_usecase`, `cta`, `on_screen_text_style`)
  — from the **best-ranked instance**, so the surviving record is internally consistent
  rather than stitched together from several.
- the surviving `ad_id` is the best-ranked instance's.

## Label-conflict resolution — majority vote, never best-rank
Identical media bytes sometimes received DIFFERENT `structure_type` labels: 2 of the 46
multi-instance groups (~4% of groups; 2 of 94 creatives). That is a direct measurement of
extraction noise — same input, different answer.

Resolution order is **manual override -> majority vote -> escalate the tie**:

- **Majority vote**, not the best-ranked instance's label. Rank is a reach ordering; it
  carries no information about what the creative IS. The majority reading of the same
  bytes is the better estimate. This mattered: creative `1371724434533607` had the
  MINORITY label (`text_animation`, 1 vote) riding on the best rank, against
  `before_after_comparison` (2 votes) — the vote corrected it.
- **Ties are escalated, never guessed.** A 1-1 tie is reported as `UNRESOLVED-TIE` and the
  run prints the exact line to add. The human decision is recorded in `MANUAL_LABELS` in
  code, keyed by the surviving `ad_id`, so it survives re-runs and is auditable.
  Currently: `"1040103798853141": "before_after_comparison"` — resolved by a human viewing
  the creative, which is a static image, and `text_animation` requires motion.

## CRITICAL run-order constraint — read before touching this
```
extract.py -> data/ads_extracted.json  (152 labelled RECORDS)
dedup.py   -> data/ads.json            (94 distinct CREATIVES)
score.py   -> data/winner.json
```

**Input and output are DELIBERATELY different files.** The majority vote needs the
PER-INSTANCE labels from `extract.py`. If `dedup.py` ever read its own output, a second run
would see one already-merged record per creative and that evidence would be destroyed
permanently — the vote would silently become a no-op and any manual tie resolution would
be unverifiable.

Never point `dedup.py` at `ads.json`. It must read extract's full 152-record output.

Safeguards in the file: input and output paths are separate constants, and the stage is
idempotent — input with no duplicate creatives is written through unchanged rather than
being reprocessed.

## Cost / re-runs
Media hashes are cached to `data/media_hashes.json` (gitignored). The first run downloads
and hashes all 152 creatives; re-runs are free. No Apify call, no LLM call — this stage
spends nothing.

## Prove it worked — what the stage prints
- records in, distinct creatives out, redundant records collapsed, group-size histogram
- the per-cluster deduplicated distribution with proven vs outlier status
- every label conflict, how it was resolved (`resolved-majority` / `resolved-manually` /
  `UNRESOLVED-TIE`), the vote counts, and each instance's label
- assertions: schema unchanged, ranks dense 1..N, ad_ids unique

## Constraints
- Modify only `src/dedup.py`. Do not touch `score.py`.
- Dependencies: `requests` + stdlib.
- No LLM, no network beyond fetching media to hash it.
