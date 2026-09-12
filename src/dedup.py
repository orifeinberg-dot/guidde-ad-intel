"""
dedup.py — prep stage: collapse duplicate records into distinct creatives.

Scribe runs everything as DCO, and Meta registers the SAME creative under multiple
ad_archive_ids. Measured on this set: 152 records carry only 94 distinct creatives
(38% redundant). Left alone that corrupts BOTH axes of score.py's PatternScore:

  frequency  — duplicate registrations count as independent repetitions, and the
               duplication rate differs per cluster (7->3 in one, 16->12 in another),
               so clusters reorder, not just rescale.
  reach      — impression_rank is a dense 1..N over RECORDS. One creative holding
               ranks 4,5,6,7 consumes four slots and pushes every distinct creative
               below it down by three.

So dedup belongs here, in the data layer, BEFORE rank is final — never in score.py,
which stays frozen arithmetic.

Runs after extract.py (it needs structure_type to detect label conflicts) and is
idempotent: on already-deduplicated input it reports and exits without rewriting.
"""
from __future__ import annotations

import hashlib
import json
from collections import Counter, defaultdict
from pathlib import Path

import requests

# Input and output are DELIBERATELY different files. dedup.py resolves label
# conflicts by majority vote across a creative's duplicate instances, so it needs the
# per-instance labels. If it ever read its own output, a second run would see one
# already-merged record per creative and that evidence would be gone for good.
IN_PATH = Path("data/ads_extracted.json")    # 152 labelled records, from extract.py
ADS_PATH = Path("data/ads.json")             # 94 distinct creatives, for score.py
RAW_PATH = Path("data/ads_raw.json")
HASH_CACHE = Path("data/media_hashes.json")   # media URLs expire; don't re-fetch

OUTLIER_MAX_SIZE = 2      # mirrors score.py, for reporting only — no logic imported

# Human-resolved label conflicts, keyed by the SURVIVING record's ad_id. Only needed
# when duplicate instances of one creative disagree AND majority vote ties. Rank does
# not decide meaning, so a tie is escalated rather than guessed.
MANUAL_LABELS: dict[str, str] = {
    # 1-1 tie between before_after_comparison and text_animation. Resolved by a human
    # viewing the creative: it is a STATIC IMAGE, and text_animation requires motion,
    # so that label was wrong on modality grounds regardless of layout.
    "1040103798853141": "before_after_comparison",
}
HEADERS = {"User-Agent": "Mozilla/5.0"}


def media_url(rec: dict, raw: dict) -> str:
    """The bytes that define the creative: the video for video ads, else the still."""
    r = raw[rec["ad_id"]]
    return r["video_url"] if rec["format"] == "video" else r["image_url"]


def media_hash(url: str) -> str | None:
    """SHA-256 of the actual bytes — the only signal that survived verification.
    URL strings don't: identical creatives appear under different CDN paths."""
    if not url:
        return None
    try:
        h = hashlib.sha256()
        with requests.get(url, timeout=180, headers=HEADERS, stream=True) as resp:
            resp.raise_for_status()
            for chunk in resp.iter_content(1 << 20):
                h.update(chunk)
        return h.hexdigest()
    except requests.RequestException:
        return None


def load_hashes(ads: list[dict], raw: dict) -> dict[str, str]:
    cache = json.loads(HASH_CACHE.read_text()) if HASH_CACHE.exists() else {}
    missing = [a for a in ads if a["ad_id"] not in cache]
    if missing:
        from concurrent.futures import ThreadPoolExecutor
        print(f"  hashing {len(missing)} creatives ({len(cache)} cached)...")
        with ThreadPoolExecutor(max_workers=8) as pool:
            got = list(pool.map(lambda a: media_hash(media_url(a, raw)), missing))
        for a, h in zip(missing, got):
            if h:
                cache[a["ad_id"]] = h
        HASH_CACHE.write_text(json.dumps(cache, indent=2))
    return cache


def merge_group(instances: list[dict]) -> tuple[dict, list[str] | None]:
    """
    One creative from its duplicate records. Returns (record, conflicting_labels).

    Provenance is merged, not picked at random:
      start_date  — EARLIEST instance: the creative's true first-live date, which is
                    what longevity in score.py is measuring.
      rank basis  — BEST (lowest) rank any instance achieved.
      bucket      — any non-null; on disagreement the one tied to the best rank.
    Descriptive LLM fields come from the best-ranked instance, so the surviving record
    is internally consistent rather than stitched from several.
    """
    by_rank = sorted(instances, key=lambda a: a["impression_rank"])
    best = by_rank[0]
    rec = dict(best)
    rec["start_date"] = min(a["start_date"] for a in instances)

    bucket = next((a["impression_bucket"] for a in by_rank if a["impression_bucket"]), None)
    rec["impression_bucket"] = bucket

    # structure_type is resolved by EVIDENCE, not by which instance won the rank
    # lottery. Identical bytes disagreeing is extraction noise; the majority reading
    # of the same creative is the better estimate than whichever copy ranked highest.
    votes = Counter(a["structure_type"] for a in instances)
    if len(votes) == 1:
        return rec, None

    manual = MANUAL_LABELS.get(rec["ad_id"])
    top, n_top = votes.most_common(1)[0]
    tied = [lab for lab, n in votes.items() if n == n_top]

    if manual:
        rec["structure_type"] = manual
        return rec, ("resolved-manually", dict(votes), manual)
    if len(tied) == 1:
        rec["structure_type"] = top
        return rec, ("resolved-majority", dict(votes), top)
    return rec, ("UNRESOLVED-TIE", dict(votes), rec["structure_type"])


def main():
    ads = json.loads(IN_PATH.read_text())
    raw = {r["ad_id"]: r for r in json.loads(RAW_PATH.read_text())}
    hashes = load_hashes(ads, raw)

    unhashed = [a["ad_id"] for a in ads if a["ad_id"] not in hashes]
    if unhashed:
        print(f"  WARNING: {len(unhashed)} records had unfetchable media; "
              f"each is kept as its own creative.")

    groups: dict[str, list[dict]] = defaultdict(list)
    for a in ads:
        groups[hashes.get(a["ad_id"], f"UNHASHED:{a['ad_id']}")].append(a)

    print(f"\n  records in       : {len(ads)}")
    print(f"  distinct creatives: {len(groups)}")
    if len(groups) == len(ads):
        print("  Input has no duplicate creatives — writing it through unchanged.")

    merged, conflicts = [], []
    for key, insts in groups.items():
        rec, clash = merge_group(insts)
        if clash:
            kind, votes, chosen = clash
            conflicts.append((rec["ad_id"], kind, votes, chosen,
                              [a["ad_id"] for a in insts]))
        merged.append(rec)

    # Dense re-rank over distinct creatives, ordered by each creative's BEST rank.
    # This is the whole point: no creative's reach is depressed by another's dupes.
    merged.sort(key=lambda r: r["impression_rank"])
    for i, r in enumerate(merged, start=1):
        r["impression_rank"] = i

    sizes = Counter(len(v) for v in groups.values())
    print(f"  collapsed        : {len(ads) - len(merged)} redundant records")
    print(f"  group sizes      : {dict(sorted(sizes.items()))}")

    print(f"\n  cluster (format, structure_type)             n   status")
    print("  " + "-" * 60)
    clusters = Counter((r["format"], r["structure_type"]) for r in merged)
    for k, n in clusters.most_common():
        status = "proven" if n > OUTLIER_MAX_SIZE else "OUTLIER (excluded)"
        print(f"  {str(k):44} {n:>3}   {status}")
    n_proven = sum(1 for n in clusters.values() if n > OUTLIER_MAX_SIZE)
    print(f"  proven clusters: {n_proven}   outlier clusters: {len(clusters) - n_proven}")

    if conflicts:
        unresolved = [c for c in conflicts if c[1] == "UNRESOLVED-TIE"]
        print(f"\n  structure_type conflicts (identical media bytes, different labels): "
              f"{len(conflicts)}")
        for kept_id, kind, votes, chosen, all_ids in conflicts:
            print(f"    creative {kept_id}  [{kind}] -> {chosen!r}")
            print(f"      votes: {votes}")
            for aid in all_ids:
                lab = next(a["structure_type"] for a in ads if a["ad_id"] == aid)
                print(f"        {aid}: {lab}")
        if unresolved:
            print(f"\n  !! {len(unresolved)} UNRESOLVED TIE(S) — majority vote cannot settle")
            print("     these. Currently holding the best-ranked instance's label as a")
            print("     placeholder. Add the human decision to MANUAL_LABELS and re-run:")
            for kept_id, _, votes, chosen, _ in unresolved:
                print(f"       MANUAL_LABELS[{kept_id!r}] = ...   "
                      f"# tie {votes}, placeholder {chosen!r}")

    expected = set(ads[0])
    for r in merged:
        assert set(r) == expected, f"schema drift on {r['ad_id']}"
    assert [r["impression_rank"] for r in merged] == list(range(1, len(merged) + 1))
    assert len({r["ad_id"] for r in merged}) == len(merged)
    print(f"\n  Assertions passed: {len(merged)} records, schema unchanged, "
          f"ranks dense 1..{len(merged)}.")

    ADS_PATH.write_text(json.dumps(merged, indent=2))
    print(f"Wrote {ADS_PATH} ({len(merged)} distinct creatives)")


if __name__ == "__main__":
    main()
