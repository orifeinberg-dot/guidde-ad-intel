# CLAUDE.md — Guidde take-home: competitor ad intelligence → creative brief

This file is your persistent context. Read it fully before writing any code, every session.

## What this project is
**One pipeline, two parts.** Part 1 finds the best-performing competitor Facebook ad;
Part 2 rebuilds it as a Guidde creative brief + shot-by-shot storyboard.

This is a job take-home. It is graded on **judgment, communication, and defensibility** —
NOT on production polish. A reviewer reads the README and watches a demo; they are checking
whether the human can direct the build, judge the output, and explain every decision.

## Golden rules — read before touching anything
1. **Build ONE stage at a time.** Only build the stage named as "active" under Current status
   below, following its spec in `docs/build/`. Never build ahead to the next stage.
2. **Prove, then STOP. Do not commit.** After a stage works, print concise proof (see Build
   protocol) and stop. The human reviews and commits manually — you never run git.
3. **Never modify a working stage or the shared schema** unless the active spec explicitly says to.
   `src/scrape.py` and `src/score.py` already exist and are correct — treat them as frozen.
4. **Deterministic scripts only.** The LLM is a *labeled step* (extraction, generation), never
   an agent that decides the winner. The winner is chosen by arithmetic in `score.py`.
5. **Inspect, don't guess.** If an external field name / actor output / API shape is unknown,
   fetch a small sample and inspect the real keys before mapping. Never hardcode a guessed shape.

## What NOT to build — the assignment says so explicitly
No auth, no UI, no test suite, no CI/CD, no Dockerfile, no deployment, no CLI arg parser,
no retry/backoff frameworks, no queues, no database. If you feel an urge to make this robust
or production-grade, that urge is wrong for this project. **Files on disk are the interface.**

## Architecture — each stage is one script that reads a file and writes a file
```
manual qualification (done → Scribe)
   └─> src/scrape.py    → data/ads_raw.json     [Part 1]  (152 records)
       └─> src/extract.py → data/ads.json        [Part 1]  (labels the records)
           └─> src/dedup.py → data/ads.json      [Part 1]  (152 recs → 94 creatives)
               └─> src/score.py → data/winner.json    [Part 1]
                   └─> src/generate.py → results/brief.md + results/storyboard/   [Part 2]
```
Part 1 (scrape, extract, score) *finds* the best ad. Part 2 (generate) *rebuilds* it.
The seam between the two parts is `winner.json`. Stages are independent: each can be built
and tested against a hand-made input file before the stage upstream of it exists.

## `data/ads.json` holds DISTINCT CREATIVES, not raw ad records
Scribe runs everything as DCO and Meta registers the same creative under multiple
`ad_archive_id`s — 152 records carry only 94 distinct creatives (verified by SHA-256 of
the media bytes). `dedup.py` collapses them BEFORE scoring, because duplicates corrupt
both axes of PatternScore: duplicate registrations inflate cluster *frequency* unevenly,
and `impression_rank` is dense over records, so one creative holding ranks 4-7 pushes
every distinct creative below it down by three. After dedup, `impression_rank` is dense
1..94 over creatives, ordered by each creative's best rank, and `start_date` is the
earliest instance (its true first-live date, which is what longevity measures).

## The schema contract — the join between extract and score
Every per-ad record (in `data/ads.json`) has exactly these keys. `scrape.py` provides the
provenance + impression fields; `extract.py` fills the rest via one LLM call per ad.

Provenance (from scrape): `ad_id` (str), `start_date` (ISO "YYYY-MM-DD"),
`impression_bucket` (str|null), `impression_rank` (int|null), plus `image_url`, `ad_text`.
Cluster-defining (from LLM): `format`, `structure_type`.
Descriptive — brief only (from LLM): `hook_type`, `angle_or_usecase`, `cta`, `on_screen_text_style`.

`structure_type` is a CLOSED enum. `angle_or_usecase` is DESCRIPTIVE ONLY and must never enter
the clustering key — it is the variable the competitor rotates, not the pattern.

## How "best" is defined — already decided, do not re-litigate
Meta exposes no spend/performance for commercial ads, so "best" is inferred. `score.py` clusters
ads by structural pattern (`format` + `structure_type`), then scores each cluster as
**PatternScore = norm(frequency) × norm(performance)** — a product, not a sum, so a pattern wins
only if it is both heavily repeated AND its instances survive/scale. Performance = longevity +
impressions-per-day (age-normalized). The winning ad is the top impressions-per-day exemplar in
the winning cluster. Structural distinctiveness is an outlier flag, not a positive signal.

## Repo layout
```
guidde-ad-intel/
├─ CLAUDE.md              (this file)
├─ README.md  .env  .env.example  .gitignore  requirements.txt  architecture.mermaid
├─ src/       scrape.py ✓  extract.py ✓  dedup.py ✓  score.py ✓  generate.py
├─ data/      ads_raw.json  ads.json  winner.json      (gitignored scratch)
├─ results/   02_scoreboard.md  03_winner.json  04_brief.md  05_storyboard/   (committed)
└─ docs/build/  01-scrape.md  02-extract.md  03-generate.md   (per-stage specs)
```

## Secrets
`.env` holds real keys and is gitignored. `.env.example` holds variable NAMES only and is
committed. Load with `python-dotenv`. Never print or commit a key.

## Build protocol — every stage
1. Read the active stage spec in `docs/build/`.
2. Build only that stage's file.
3. Print concise proof it worked: counts, one sample record, and hard assertions on required fields.
4. STOP. Tell the human what to review and suggest a one-line commit message. Do not run git.

## Current status
- Built & frozen: `src/scrape.py`, `src/score.py`
- **Active stage: `src/extract.py` — spec in `docs/build/02-extract.md`**
- Not yet started: `src/generate.py` (spec `docs/build/03-generate.md`)
- Read `docs/build/02-extract.md` first — it is the active spec.
