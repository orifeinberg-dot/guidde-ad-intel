"""
scrape.py — Part 1, stage 1: pull one competitor's ACTIVE Facebook ads.

Source: the Apify Facebook Ad Library actor (curious_coder/facebook-ads-library-scraper).
Not the official Meta Ad Library API: that API only returns political/issue ads for US
commercial advertisers, so it cannot see Scribe's SaaS ads at all. The actor scrapes the
public web Ad Library, which shows every active commercial ad.

Writes data/ads_raw.json — the provenance half of the schema contract in CLAUDE.md.
extract.py fills in the LLM half; score.py does the arithmetic. Deterministic, no LLM here.
"""
from __future__ import annotations

import json
import os
from datetime import date, datetime
from pathlib import Path

from apify_client import ApifyClient
from dotenv import load_dotenv

# ----------------------------------------------------------------------
# Config
# ----------------------------------------------------------------------
COMPETITOR = "Scribe"

# Scribe's Ad Library page view, verbatim from docs/build/01-scrape.md. page_id confirmed
# against live actor output: snapshot.page_profile_uri == "https://www.facebook.com/ScribeHow/".
# The sort_data params state impressions-desc in the URL; scrapePageAds.sortBy below states it
# again as actor input. Belt-and-braces on purpose — the actor builds its own query from a page
# URL, so the input field is what actually drives the sort.
AD_LIBRARY_URL = (
    "https://www.facebook.com/ads/library/"
    "?active_status=active&ad_type=all&country=US"
    "&is_targeted_country=false&media_type=all&search_type=page"
    "&sort_data[direction]=desc&sort_data[mode]=total_impressions"
    "&view_all_page_id=110376044484488"
)

COUNTRY = "US"
ACTIVE_ONLY = True
MAX_RESULTS = 200          # Scribe runs ~150 active ads; cap bounds Apify cost
ACTOR_ID = "curious_coder/facebook-ads-library-scraper"

# Ask the actor for impressions-descending order. Meta exposes no impression VALUES for
# commercial ads (impressions_with_index.impressions_text is null, index -1), so the
# returned ORDER is the only reach signal available -> it becomes impression_rank.
SORT_BY = "impressions_desc"

MIN_ADS_TO_RANK = 5        # below this, ranking patterns is meaningless
OUT_PATH = Path("data/ads_raw.json")


# ----------------------------------------------------------------------
# Defensive field mapping — the actor's key names, confirmed by inspecting
# real output before writing any of this (CLAUDE.md rule 5).
# ----------------------------------------------------------------------
def _clean(value) -> str:
    """Actor sometimes returns placeholder/template junk instead of a real string."""
    if not isinstance(value, str):
        return ""
    v = value.strip()
    if not v or v.startswith("{{"):   # DCO templates, e.g. "{{product.brand}}"
        return ""
    return v


def pick_ad_id(rec: dict) -> str:
    # `ad_id` exists as a key but is null on every record; ad_archive_id is the real one.
    return str(rec.get("ad_archive_id") or rec.get("ad_id") or "").strip()


def pick_start_date(rec: dict) -> str | None:
    """ISO YYYY-MM-DD, or None if unparseable (caller drops the record)."""
    formatted = rec.get("start_date_formatted")   # "2026-08-10 07:00:00"
    if isinstance(formatted, str) and formatted:
        try:
            return datetime.fromisoformat(formatted).date().isoformat()
        except ValueError:
            pass
    epoch = rec.get("start_date")                 # unix seconds
    if isinstance(epoch, (int, float)) and epoch > 0:
        try:
            return date.fromtimestamp(epoch).isoformat()
        except (OverflowError, OSError, ValueError):
            pass
    return None


def pick_impression_bucket(rec: dict) -> str | None:
    """Populated only for political/issue ads; null for commercial. Kept for correctness."""
    return _clean((rec.get("impressions_with_index") or {}).get("impressions_text")) or None


def pick_image_url(rec: dict) -> str:
    """Primary creative still. For video ads that's the preview frame."""
    snap = rec.get("snapshot") or {}
    pools = [snap.get("videos"), snap.get("images"), snap.get("cards"),
             snap.get("extra_videos"), snap.get("extra_images")]
    fields = ("video_preview_image_url", "original_image_url",
              "resized_image_url", "watermarked_resized_image_url")
    for pool in pools:
        for entry in pool or []:
            if not isinstance(entry, dict):
                continue
            for f in fields:
                url = _clean(entry.get(f))
                if url:
                    return url
    return ""


def pick_ad_text(rec: dict) -> str:
    """Primary copy. DCO ads leave snapshot.body as a template, so fall through to cards."""
    snap = rec.get("snapshot") or {}
    body = snap.get("body")
    candidates = [body.get("text") if isinstance(body, dict) else body]
    for card in snap.get("cards") or []:
        if isinstance(card, dict):
            candidates += [card.get("body"), card.get("title"), card.get("link_description")]
    candidates += [snap.get("title"), snap.get("link_description"), snap.get("caption")]
    for c in candidates:
        text = _clean(c)
        if text:
            return text
    return ""


def to_record(rec: dict) -> dict | None:
    ad_id = pick_ad_id(rec)
    start_date = pick_start_date(rec)
    if not ad_id or not start_date:
        return None
    return {
        "ad_id": ad_id,
        "start_date": start_date,
        "impression_bucket": pick_impression_bucket(rec),
        "impression_rank": None,          # assigned after filtering, from actor order
        "image_url": pick_image_url(rec),
        "ad_text": pick_ad_text(rec),
    }


# ----------------------------------------------------------------------
# Run
# ----------------------------------------------------------------------
def fetch() -> list[dict]:
    load_dotenv()
    token = os.environ["APIFY_TOKEN"]
    client = ApifyClient(token)

    run = client.actor(ACTOR_ID).call(
        run_input={
            "urls": [{"url": AD_LIBRARY_URL}],
            "count": MAX_RESULTS,
            "scrapeAdDetails": False,
            "scrapePageAds.activeStatus": "active" if ACTIVE_ONLY else "all",
            "scrapePageAds.sortBy": SORT_BY,
            "scrapePageAds.countryCode": COUNTRY,
        },
        max_items=MAX_RESULTS,
    )
    return list(client.dataset(run.default_dataset_id).iterate_items())


def main():
    raw = fetch()

    kept, dropped = [], 0
    for rec in raw:
        mapped = to_record(rec)
        if mapped is None:
            dropped += 1
            continue
        kept.append(mapped)

    # Rank = position in the actor's impressions_desc ordering, over kept records only
    # (so drops don't punch holes in the ranking). 1 = highest reach.
    for i, rec in enumerate(kept, start=1):
        rec["impression_rank"] = i

    print(f"\n{COMPETITOR} — active ads, {COUNTRY}, sorted {SORT_BY}")
    print(f"  actor returned : {len(raw)}")
    print(f"  kept           : {len(kept)}")
    print(f"  dropped        : {dropped}  (missing ad_id or start_date)")

    pages = sorted({(r.get("page_name") or "?") for r in raw})
    print(f"  advertiser(s)  : {', '.join(pages) or '—'}")
    with_bucket = sum(1 for r in kept if r["impression_bucket"])
    print(f"  impression buckets present: {with_bucket}/{len(kept)} "
          f"(Meta publishes none for commercial ads -> score.py uses impression_rank)")
    no_image = sum(1 for r in kept if not r["image_url"])
    no_text = sum(1 for r in kept if not r["ad_text"])
    print(f"  missing image_url: {no_image}   empty ad_text: {no_text}")

    if len(kept) < MIN_ADS_TO_RANK:
        print(f"\nWARNING: only {len(kept)} active ads for {COMPETITOR} — too few to rank "
              f"creative patterns (need >= {MIN_ADS_TO_RANK}). Pick another competitor.")
        return

    print("\nSample kept record:")
    print(json.dumps(kept[0], indent=2)[:1200])

    # Hard assertions — the contract extract.py/score.py depend on.
    for r in kept:
        assert r["ad_id"], "empty ad_id"
        assert date.fromisoformat(r["start_date"]), f"bad start_date {r['start_date']}"
        assert set(r) == {"ad_id", "start_date", "impression_bucket",
                          "impression_rank", "image_url", "ad_text"}, "schema drift"
    assert len({r["ad_id"] for r in kept}) == len(kept), "duplicate ad_ids"
    print(f"\nAssertions passed: {len(kept)} records, unique ad_ids, all dates valid ISO.")

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(kept, indent=2))
    print(f"Wrote {OUT_PATH} ({len(kept)} ads)")


if __name__ == "__main__":
    main()
