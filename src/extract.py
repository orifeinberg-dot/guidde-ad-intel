"""
extract.py — the ONE LLM step in Part 1.

Turns each scraped Scribe ad (creative + metadata from the Apify Facebook
Ad Library actor) into a fixed-schema record. Everything downstream (score.py)
is deterministic arithmetic on these records, so THIS SCHEMA IS THE CONTRACT
between the fuzzy vision step and the exact scoring step.

Cost control: this is the only place Part 1 spends LLM tokens besides the scrape.
Batch the work (~15 ads per request grouping) — see README 'Cost per run'.
"""
from __future__ import annotations

import json
# import anthropic   # wire your provider here; kept out so the file imports clean


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
    # cluster-defining (from the LLM):
    "format":               f"one of {FORMATS}",
    "structure_type":       f"one of {STRUCTURE_TYPES}",
    # descriptive — brief only (from the LLM):
    "hook_type":            "str  # what grabs attention in the first ~3s",
    "angle_or_usecase":     "str  # the specific pain/use-case (the rotated variable)",
    "cta":                  "str",
    "on_screen_text_style": "str",
}

LLM_FIELDS = ("format", "structure_type", "hook_type",
              "angle_or_usecase", "cta", "on_screen_text_style")


EXTRACTION_PROMPT = """You are analyzing a single Facebook ad for competitive research.
Return ONLY a JSON object, no prose and no markdown fences, with exactly these keys:

  format: one of {formats}
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
""".format(formats=FORMATS, structures=STRUCTURE_TYPES)


def extract_batch(ads_media, client=None):
    """
    ads_media: list of {ad_id, start_date, image (bytes/url), ad_text}
               (assembled from the Apify actor output)
    Returns:   list of records conforming to AD_SCHEMA.

    The provider call is stubbed so this file runs without keys. Wire `client`
    to your LLM provider and uncomment the block. One creative image per ad.
    """
    records = []
    for media in ads_media:
        # --- provider call (STUB — wire your key/model) -------------------
        # resp = client.messages.create(
        #     model="claude-...",
        #     max_tokens=400,
        #     messages=[{
        #         "role": "user",
        #         "content": [
        #             {"type": "image", "source": {...}},              # the creative
        #             {"type": "text",
        #              "text": EXTRACTION_PROMPT
        #                      + "\n\nAd copy:\n" + media.get("ad_text", "")},
        #         ],
        #     }],
        # )
        # parsed = json.loads(resp.content[0].text)
        # -----------------------------------------------------------------
        parsed = {}  # <- replace with parsed provider output

        record = {
            "ad_id": media["ad_id"],
            "start_date": media["start_date"],
            **{k: parsed.get(k) for k in LLM_FIELDS},
        }
        records.append(record)
    return records


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


if __name__ == "__main__":
    # tiny self-check of the schema/enum wiring (no network needed)
    demo = {"ad_id": "123", "start_date": "2026-07-28",
            "format": "video", "structure_type": "conversational_demo"}
    print("validate errors:", validate(demo) or "none")
    print(json.dumps(AD_SCHEMA, indent=2))
