"""
extract.py — the ONE LLM step in Part 1.

Turns each scraped Scribe ad (creative + metadata from the Apify Facebook
Ad Library actor) into a fixed-schema record. Everything downstream (score.py)
is deterministic arithmetic on these records, so THIS SCHEMA IS THE CONTRACT
between the fuzzy vision step and the exact scoring step.

Cost control: this is the only place Part 1 spends LLM tokens besides the scrape.
One vision call per ad — 145 calls, not batched: a bad response then validates and
attributes to a single ad rather than poisoning a group. See README 'Cost per run'.
"""
from __future__ import annotations

import base64
import json
import os
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import anthropic
import requests
from dotenv import load_dotenv


# ----------------------------------------------------------------------
# Provider config — pinned so a run is reproducible and costable.
# ----------------------------------------------------------------------
MODEL = "claude-sonnet-5"
MAX_TOKENS = 4000            # room for adaptive thinking + the JSON object
EFFORT = "low"               # fixed-schema extraction; depth buys nothing here

# claude-sonnet-5 list price, USD per million tokens (see README 'Cost per run').
PRICE_IN_PER_MTOK = 2.00
PRICE_OUT_PER_MTOK = 10.00

IN_PATH = Path("data/ads_raw.json")
OUT_PATH = Path("data/ads.json")

# 145 sequential vision calls run ~12 min; a small stdlib pool cuts that to ~2.
# Results are reassembled in input order, so the output file is byte-identical
# to what the sequential version would write.
WORKERS = 6

PROVENANCE_FIELDS = ("ad_id", "start_date", "format",
                     "impression_bucket", "impression_rank")
ALLOWED_MEDIA = {"image/jpeg", "image/png", "image/gif", "image/webp"}
MAX_IMAGE_B64 = 5 * 1024 * 1024      # API limit on a base64 image payload


# ----------------------------------------------------------------------
# Enums — keep these CLOSED. Clustering in score.py is only stable if
# structure_type comes from a fixed vocabulary, not free text.
# ----------------------------------------------------------------------
FORMATS = ["image", "video", "carousel"]

STRUCTURE_TYPES = [
    "talking_head_testimonial",  # single presenter speaking to camera
    "conversational_demo",       # 2+ presenters, product shown through dialogue
    "screen_demo",               # screen/product recording is the focus, no presenter
    "text_animation",            # kinetic text / motion graphics, no presenter
    "other",
]


# ----------------------------------------------------------------------
# Per-ad schema (the contract).
#   CLUSTER-DEFINING fields decide the pattern  -> used by score.py
#   DESCRIPTIVE fields feed the Part 2 brief    -> NOT clustered on
# angle_or_usecase is descriptive ON PURPOSE: it's the variable Scribe
# rotates, so it must NOT enter the cluster key (see README 'How I defined best').
# ----------------------------------------------------------------------
AD_SCHEMA = {
    # provenance (from Apify metadata, not the LLM):
    "ad_id":                "str",
    "start_date":           "YYYY-MM-DD  # ad_delivery_start_time",
    # cluster-defining:
    "format":               f"one of {FORMATS}  # PROVENANCE — real value from the scrape",
    "structure_type":       f"one of {STRUCTURE_TYPES}  # from the LLM",
    # descriptive — brief only (from the LLM):
    "hook_type":            "str  # what grabs attention in the first ~3s",
    "angle_or_usecase":     "str  # the specific pain/use-case (the rotated variable)",
    "cta":                  "str",
    "on_screen_text_style": "str",
}

# format is NOT here: a single still frame cannot tell video from image, and the
# scrape already has the truth from snapshot.display_format. Asking the model to
# guess a field we know would put noise into score.py's cluster key.
LLM_FIELDS = ("structure_type", "hook_type",
              "angle_or_usecase", "cta", "on_screen_text_style")


EXTRACTION_PROMPT = """You are analyzing a single Facebook ad for competitive research.
Return ONLY a JSON object, no prose and no markdown fences, with exactly these keys:

  structure_type: one of {structures}
  hook_type: short phrase for what grabs attention in the first ~3 seconds
  angle_or_usecase: the specific pain point or use-case the ad sells
  cta: the call to action (button text or the on-screen/spoken ask)
  on_screen_text_style: short description of the on-screen text treatment

Rules for structure_type:
- 'conversational_demo' only if 2+ presenters appear together
- 'talking_head_testimonial' for a single presenter speaking to camera
- 'screen_demo' if a product/screen recording is the focus with no presenter
- 'text_animation' if it's kinetic text with no presenter
- otherwise 'other'

Base every field only on what is visibly/audibly present. Do not guess.
""".format(structures=STRUCTURE_TYPES)


# ----------------------------------------------------------------------
# Creative fetch — the image is the evidence; ad_text alone is the fallback.
# ----------------------------------------------------------------------
def sniff_media_type(data: bytes) -> str | None:
    """
    Identify the image from its magic bytes.

    The Facebook CDN serves Content-Type: image/jpeg for bytes that are actually
    PNG. Trusting the header makes the API reject the request with
    'specified as image/jpeg, but the image appears to be a image/png image'.
    Inspect the bytes, don't believe the label.
    """
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith(b"GIF87a") or data.startswith(b"GIF89a"):
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def fetch_image(url: str) -> tuple[str, str] | None:
    """Return (base64_data, media_type), or None if the creative can't be had."""
    if not url:
        return None
    try:
        resp = requests.get(url, timeout=30, headers={"User-Agent": "Mozilla/5.0"})
        resp.raise_for_status()
    except requests.RequestException:
        return None
    if not resp.content:
        return None
    media_type = sniff_media_type(resp.content)
    if media_type not in ALLOWED_MEDIA:
        return None
    b64 = base64.standard_b64encode(resp.content).decode("utf-8")
    if len(b64) > MAX_IMAGE_B64:          # API caps base64 payload at 5MB
        return None
    return b64, media_type


def _parse_json(text: str) -> dict:
    """The prompt forbids fences, but strip them anyway rather than lose a record."""
    t = text.strip()
    if t.startswith("```"):
        t = t.split("\n", 1)[1] if "\n" in t else t
        t = t.rsplit("```", 1)[0]
    start, end = t.find("{"), t.rfind("}")
    if start == -1 or end == -1:
        raise ValueError(f"no JSON object in response: {text[:120]!r}")
    return json.loads(t[start:end + 1])


def modality_note(fmt: str | None) -> str:
    """
    State the real modality in-context. structure_type distinguishes presenters,
    screen recordings and kinetic text — all motion judgements. Without this the
    model is reading a still frame and inferring the wrong thing.
    """
    if fmt == "video":
        return ("This ad is a VIDEO. The image above is its preview frame, not the "
                "whole ad. Judge structure_type as the structure of the video.")
    if fmt == "carousel":
        return "This ad is a CAROUSEL. The image above is its first card."
    return "This ad is a STATIC IMAGE — there is no motion or spoken audio."


def extract_one(media: dict, client: anthropic.Anthropic) -> tuple[dict, dict]:
    """
    One ad -> one vision call -> one record. Returns (record, stats).

    stats carries per-call usage + whether the creative was available, so the
    runner can report cost and image-failure counts without a global.
    """
    image = fetch_image(media.get("image_url", ""))
    content = []
    if image is not None:
        b64, media_type = image
        content.append({"type": "image", "source": {
            "type": "base64", "media_type": media_type, "data": b64}})
    content.append({"type": "text",
                    "text": EXTRACTION_PROMPT
                            + "\n\n" + modality_note(media.get("format"))
                            + "\n\nAd copy:\n" + (media.get("ad_text") or "")})

    stats = {"in": 0, "out": 0, "image_ok": image is not None,
             "error": None, "truncated": False}
    parsed = {}
    # One attempt, then exactly one retry — the spec allows no more.
    for attempt in (1, 2):
        try:
            resp = client.messages.create(
                model=MODEL,
                max_tokens=MAX_TOKENS,
                thinking={"type": "adaptive"},
                output_config={"effort": EFFORT},
                messages=[{"role": "user", "content": content}],
            )
            stats["truncated"] = resp.stop_reason == "max_tokens"
            stats["in"] += resp.usage.input_tokens
            stats["out"] += resp.usage.output_tokens
            text = next((b.text for b in resp.content if b.type == "text"), "")
            parsed = _parse_json(text)
            stats["error"] = None
            break
        except Exception as exc:                    # noqa: BLE001 — report, don't crash
            stats["error"] = f"{type(exc).__name__}: {exc}"[:160]
            if attempt == 2:
                parsed = {}

    record = {
        "ad_id": media["ad_id"],
        "start_date": media["start_date"],
        # provenance carried through untouched — score.py clusters and ranks on these
        "format": media.get("format"),
        "impression_bucket": media.get("impression_bucket"),
        "impression_rank": media.get("impression_rank"),
        **{k: parsed.get(k) for k in LLM_FIELDS},
    }
    return record, stats


def extract_batch(ads_media, client=None):
    """
    ads_media: list of {ad_id, start_date, image_url, ad_text, impression_*}
               (records straight out of data/ads_raw.json)
    Returns:   (records, stats_list) — one call per ad, order preserved.
    """
    client = client or anthropic.Anthropic()
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        results = list(pool.map(lambda m: extract_one(m, client), ads_media))
    return [r for r, _ in results], [s for _, s in results]


def validate(record):
    """Judge what the tool handed back — cheap gate before scoring trusts it."""
    errs = []
    if record.get("format") not in FORMATS:
        errs.append(f"bad format: {record.get('format')!r}")
    if record.get("structure_type") not in STRUCTURE_TYPES:
        errs.append(f"bad structure_type: {record.get('structure_type')!r}")
    for k in ("ad_id", "start_date"):
        if not record.get(k):
            errs.append(f"missing {k}")
    return errs


# ----------------------------------------------------------------------
# Runner
# ----------------------------------------------------------------------
def main():
    load_dotenv()
    os.environ["ANTHROPIC_API_KEY"]          # fail loudly now, not 145 calls in
    ads = json.loads(IN_PATH.read_text())
    print(f"\nExtracting {len(ads)} ads with {MODEL} (effort={EFFORT}, "
          f"1 vision call per ad)...")

    records, stats = extract_batch(ads)

    valid, failures = [], []
    for rec, st in zip(records, stats):
        errs = validate(rec)
        if errs:
            failures.append((rec["ad_id"], errs, st["error"]))
        else:
            valid.append(rec)

    no_image = sum(1 for s in stats if not s["image_ok"])
    tok_in = sum(s["in"] for s in stats)
    tok_out = sum(s["out"] for s in stats)
    cost = tok_in / 1e6 * PRICE_IN_PER_MTOK + tok_out / 1e6 * PRICE_OUT_PER_MTOK

    print(f"\n  ads read            : {len(ads)}")
    print(f"  extracted + valid   : {len(valid)}")
    print(f"  validation failures : {len(failures)}")
    print(f"  creative unavailable: {no_image}  (extracted from ad_text alone)")

    print("\n  structure_type distribution:")
    for k, n in Counter(r["structure_type"] for r in valid).most_common():
        print(f"    {str(k):26} {n:>4}")
    print("  format distribution:")
    for k, n in Counter(r["format"] for r in valid).most_common():
        print(f"    {str(k):26} {n:>4}")

    if failures:
        print("\n  failures (ad_id -> why):")
        for ad_id, errs, api_err in failures[:10]:
            print(f"    {ad_id}: {'; '.join(errs)}"
                  + (f"  [{api_err}]" if api_err else ""))

    truncated = sum(1 for s in stats if s["truncated"])
    if truncated:
        print(f"  responses hit max_tokens: {truncated}  (raise MAX_TOKENS)")

    if not valid:
        print("\n  No valid records — nothing written. Fix the errors above and re-run.")
        return

    print("\n  Sample record:")
    print(json.dumps(valid[0], indent=2))

    print(f"\n  Cost: {tok_in:,} in + {tok_out:,} out tokens "
          f"@ ${PRICE_IN_PER_MTOK}/${PRICE_OUT_PER_MTOK} per MTok = ${cost:.2f}")

    expected = set(AD_SCHEMA) | set(PROVENANCE_FIELDS)
    raw_by_id = {r["ad_id"]: r for r in ads}
    for r in valid:
        assert set(r) == expected, f"schema drift on {r['ad_id']}: {sorted(set(r))}"
        src = raw_by_id[r["ad_id"]]
        for f in PROVENANCE_FIELDS:
            assert r[f] == src[f], f"provenance changed on {r['ad_id']}: {f}"
    print(f"  Assertions passed: {len(valid)} records, all schema keys present, "
          f"provenance identical to ads_raw.json.")

    OUT_PATH.write_text(json.dumps(valid, indent=2))
    print(f"Wrote {OUT_PATH} ({len(valid)} ads)")


if __name__ == "__main__":
    main()
