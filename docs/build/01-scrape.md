# Stage 01 — `src/scrape.py`

Build ONLY this stage. Follow the golden rules and build protocol in `CLAUDE.md`.
Do not modify `extract.py`, `score.py`, or the schema. Stop when the proof output prints.

## Goal
Pull one competitor's ACTIVE Facebook ads from the Meta Ad Library via the **Apify Facebook
Ad Library actor**, and write them to `data/ads_raw.json` in a shape `extract.py` can consume.

## Why Apify, not the official Meta API
The official Ad Library API only returns political/issue ads for US commercial advertisers, so
it cannot see Scribe's SaaS ads. The Apify actor scrapes the public web Ad Library, which shows
all active commercial ads.

## Config — constants at the top of the file
- `COMPETITOR = "Scribe"`
- `AD_LIBRARY_URL = "<<PASTE THE SCRIBE AD LIBRARY URL HERE>>"`  ← the human pastes this
- `COUNTRY = "US"`
- `ACTIVE_ONLY = True`
- `MAX_RESULTS = 200`  (Scribe has ~150 active ads; cap to bound Apify cost)
- Load `APIFY_TOKEN` from `.env` via `python-dotenv`. Never hardcode it. Use the `apify-client` package.

## Output contract — every record in `data/ads_raw.json` has exactly these keys
- `ad_id`: str
- `start_date`: str — ISO `YYYY-MM-DD`, the ad's delivery-start / first-seen date
- `impression_bucket`: str | null — e.g. `"50K-100K"` if the actor returns an impressions range; else null
- `impression_rank`: int | null — 1 = highest, if a sort order is derivable; else null
- `image_url`: str — primary creative image; for video ads, the thumbnail/preview frame
- `ad_text`: str — primary ad copy; `""` if none

## Defensive mapping — important
The actor's real output field names may differ from the above. FIRST fetch a few results,
inspect the ACTUAL keys, THEN map with `.get()` and sensible fallbacks. If a record is missing
`ad_id` or `start_date`, skip it and count it as dropped — never crash on one bad record.

## Prove it worked — print concisely to stdout
- total ads the actor returned
- total kept, and total dropped for missing required fields
- one sample kept record, pretty-printed
- assert every kept record has a non-empty `ad_id` and a valid ISO `start_date`

Then write `data/ads_raw.json` and STOP.

## Failure handling — real case, keep simple
If fewer than 5 ads come back, print a clear warning that this competitor has too few active ads
to rank, and exit cleanly. No retry frameworks, no queues.

## Constraints
- Modify only `src/scrape.py`.
- Dependencies: `apify-client`, `python-dotenv`, and stdlib only (`json`, `pathlib`, `datetime`).
- No auth, UI, tests, CLI parser, retry/backoff, or deployment. Deterministic script.
- After proof prints, stop and suggest the commit message. Do not run git.
