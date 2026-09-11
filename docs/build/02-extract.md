# Stage 02 — `src/extract.py` (complete the wiring)

Build ONLY this stage. Follow the golden rules and build protocol in `CLAUDE.md`.
`extract.py` already exists as a skeleton — schema, enums, EXTRACTION_PROMPT, and
validate() are all present and correct. This stage is a WIRING job, not a rebuild:
replace the stubbed provider call with a real vision call, add a runner over the real
data, and carry the impression fields through. Do not change the schema, the enums, or
validate(). Do not touch scrape.py or score.py.

## Goal
Read `data/ads_raw.json` (145 Scribe ads), run one vision LLM call per ad to fill the
schema fields, and write `data/ads.json` — the input `score.py` consumes.

## What's already there (don't rebuild)
- AD_SCHEMA, FORMATS, STRUCTURE_TYPES enums
- EXTRACTION_PROMPT
- validate()
The stub currently hardcodes `parsed = {}`, so every LLM field returns None and
validate() fails. Replacing that stub is the core of this stage.

## Wire the provider call
- Use the Anthropic API with a vision-capable model. Load ANTHROPIC_API_KEY from .env
  via python-dotenv. Use the `anthropic` package.
- For each ad: send the creative image + the ad_text to the model with EXTRACTION_PROMPT,
  and parse the returned JSON into the LLM fields
  (format, structure_type, hook_type, angle_or_usecase, cta, on_screen_text_style).
- The image: fetch image_url and pass it to the model. If a fetch fails, skip that ad's
  image and still attempt extraction from ad_text alone; count it.

## Read the REAL creative — important (from the scrape findings)
Some ads are DCO: their real copy/creative live in `snapshot.cards[]`, not the top-level
body. scrape.py already resolved this — `data/ads_raw.json` has real image_url and ad_text
on all 145 records. So read those fields as-is; do not re-parse snapshot. Just trust the
scraped image_url and ad_text.

## Carry provenance through — required
Each output record in `data/ads.json` must include, unchanged from ads_raw.json:
`ad_id`, `start_date`, `impression_bucket`, `impression_rank`.
score.py needs these — if they don't survive the pass, ranking breaks.

## Validate what the model returns
Run validate() on every record. Count and report validation failures (bad enum, missing
field). Do not silently drop — report the count and write the valid ones.

## Cost control
This is the main token spend in Part 1. Print an estimated cost at the end
(ads × per-call tokens). One image per ad; no re-tries beyond one.

## Prove it worked — print concisely to stdout
- ads read, ads successfully extracted, validation failures
- the distribution of structure_type across all ads (how many of each enum)
- one sample full record, pretty-printed
- assert every written record has all schema keys + the four provenance fields
Then write `data/ads.json` and STOP.

## Constraints
- Modify only src/extract.py.
- Dependencies: anthropic, python-dotenv, requests, stdlib.
- No auth, UI, tests, CLI parser, retries beyond one, or deployment.
- After proof prints, stop and suggest the commit message. Do not run git.