"""
score.py — deterministic 'define best' engine for the Scribe ad set.

Runs AFTER extraction (expects ads.json produced from extract.py records).
No LLM, no network: pure arithmetic, so every number in the output is
inspectable and reproducible.

Definition of best (see README 'How I defined best'):
    best = top-performing execution of Scribe's most-repeated,
    durability-proven creative formula.

    PatternScore(cluster) = norm(frequency) * norm(performance)   # product, not sum
    winner = highest impressions-per-day ad inside the top-PatternScore cluster

Why a PRODUCT, not a sum: a pattern must be BOTH heavily repeated AND have
instances that survive/scale. A big cluster that churns fast (freq high, perf low)
and a lone high performer (perf high, freq low) are both correctly rejected —
a sum would let either win on one axis alone.
"""
from __future__ import annotations

import json
import statistics
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path


# ----------------------------------------------------------------------
# Config / assumptions — documented here, never buried inline.
# ----------------------------------------------------------------------
TODAY = date.today()

# Impression-range bucket -> midpoint, used for impressions-per-day.
# The open-ended top bucket uses a documented floor (see README 'Limitations').
BUCKET_MIDPOINTS: dict[str, float] = {
    "<1K": 500,
    "1K-5K": 3_000,
    "5K-10K": 7_500,
    "10K-50K": 30_000,
    "50K-100K": 75_000,
    "100K-500K": 300_000,
    "500K-1M": 750_000,
    "1M+": 1_000_000,        # ASSUMPTION: floor of the open bucket
}

# The common denominator: cluster on format + structure ONLY.
# use_case is excluded on purpose — it's the variable Scribe rotates, not the pattern.
CLUSTER_KEY_FIELDS = ("format", "structure_type")

# Clusters this small are unproven experiments: flagged, reported, and excluded
# from winner selection. (This is where 'structural distinctiveness' now lives —
# demoted from a positive signal to an outlier flag.)
OUTLIER_MAX_SIZE = 2

# Per-execution performance blend (within a cluster).
W_IPD = 0.5
W_LONGEVITY = 0.5


# ----------------------------------------------------------------------
# Data model
# ----------------------------------------------------------------------
@dataclass
class Ad:
    ad_id: str
    format: str
    structure_type: str
    start_date: date
    # exactly one of these is expected (bucket preferred; rank is the fallback):
    impression_bucket: str | None = None   # e.g. "50K-100K"
    impression_rank: int | None = None     # 1 = highest-impression ad in the set
    raw: dict = field(default_factory=dict)  # full extracted record, for Part 2

    @property
    def days_running(self) -> int:
        return max((TODAY - self.start_date).days, 1)  # guard div-by-zero

    def cluster_key(self) -> tuple:
        return tuple(getattr(self, f) for f in CLUSTER_KEY_FIELDS)


# ----------------------------------------------------------------------
# Signals
# ----------------------------------------------------------------------
def exposure(ad: Ad, n_ads: int) -> float:
    """
    Reach proxy feeding impressions-per-day.
    Primary: impression-bucket midpoint (real Meta data).
    Fallback: if only sort ORDER is on the card (no bucket values), convert rank
    to a pseudo-exposure. Documented as a fallback in the README.
    """
    if ad.impression_bucket in BUCKET_MIDPOINTS:
        return BUCKET_MIDPOINTS[ad.impression_bucket]
    if ad.impression_rank is not None:
        return (n_ads - ad.impression_rank + 1) / n_ads  # rank 1 -> highest
    raise ValueError(f"Ad {ad.ad_id}: no impression_bucket or impression_rank")


def impressions_per_day(ad: Ad, n_ads: int) -> float:
    return exposure(ad, n_ads) / ad.days_running


def minmax(values: list[float]) -> list[float]:
    lo, hi = min(values), max(values)
    rng = hi - lo
    if rng == 0:
        return [1.0] * len(values)  # all equal -> constant; ranking unaffected
    return [(v - lo) / rng for v in values]


# ----------------------------------------------------------------------
# Pipeline
# ----------------------------------------------------------------------
def load_ads(path: Path) -> list[Ad]:
    ads = []
    for r in json.loads(path.read_text()):
        ads.append(Ad(
            ad_id=r["ad_id"],
            format=r["format"],
            structure_type=r["structure_type"],
            start_date=date.fromisoformat(r["start_date"]),
            impression_bucket=r.get("impression_bucket"),
            impression_rank=r.get("impression_rank"),
            raw=r,
        ))
    return ads


def score(ads: list[Ad]) -> dict:
    n = len(ads)

    # 1) Per-ad signals, normalized across the whole set.
    ipd = [impressions_per_day(a, n) for a in ads]
    longevity = [float(a.days_running) for a in ads]
    ipd_n, lon_n = minmax(ipd), minmax(longevity)
    perf_per_ad = {a.ad_id: W_IPD * ipd_n[i] + W_LONGEVITY * lon_n[i]
                   for i, a in enumerate(ads)}
    ipd_by_id = {a.ad_id: ipd[i] for i, a in enumerate(ads)}

    # 2) Cluster on the common-denominator key.
    clusters: dict[tuple, list[Ad]] = defaultdict(list)
    for a in ads:
        clusters[a.cluster_key()].append(a)

    # 3) Split proven patterns from unproven experiments (the outlier flag).
    proven = {k: v for k, v in clusters.items() if len(v) > OUTLIER_MAX_SIZE}
    outliers = {k: v for k, v in clusters.items() if len(v) <= OUTLIER_MAX_SIZE}
    if not proven:
        raise SystemExit("No cluster exceeds OUTLIER_MAX_SIZE — set too small to rank.")

    # 4) Cluster frequency + performance (median so one freak ad can't carry a cluster).
    keys = list(proven.keys())
    freq = [len(proven[k]) / n for k in keys]
    perf = [statistics.median(perf_per_ad[a.ad_id] for a in proven[k]) for k in keys]
    freq_n, perf_n = minmax(freq), minmax(perf)

    # 5) PatternScore = product (both axes must be real).
    pattern_scores = {k: freq_n[i] * perf_n[i] for i, k in enumerate(keys)}

    rows = [{"cluster": k, "n": len(proven[k]),
             "freq": round(freq[i], 4), "perf": round(perf[i], 4),
             "pattern_score": round(pattern_scores[k], 4)}
            for i, k in enumerate(keys)]
    rows.sort(key=lambda r: r["pattern_score"], reverse=True)

    # 6) Winner: top impressions-per-day in the winning cluster (longevity tiebreak).
    winning_key = rows[0]["cluster"]
    winner = max(proven[winning_key],
                 key=lambda a: (ipd_by_id[a.ad_id], a.days_running))

    return {
        "winner": winner,
        "winning_cluster": winning_key,
        "ranked_clusters": rows,
        "outliers": [{"cluster": k, "n": len(v), "ad_ids": [a.ad_id for a in v]}
                     for k, v in outliers.items()],
        "ipd_by_id": ipd_by_id,
    }


def _key_str(k: tuple) -> str:
    return " / ".join(str(x) for x in k)


def main():
    ads = load_ads(Path("ads.json"))
    result = score(ads)

    print(f"\nAds scored: {len(ads)}")
    print("\nRanked patterns (freq x perf = PatternScore):")
    for r in result["ranked_clusters"]:
        print(f"  {_key_str(r['cluster']):40}  n={r['n']:>3}  "
              f"freq={r['freq']:.3f}  perf={r['perf']:.3f}  score={r['pattern_score']:.3f}")

    if result["outliers"]:
        print("\nOutliers (unproven experiments — excluded from winner):")
        for o in result["outliers"]:
            print(f"  {_key_str(o['cluster']):40}  n={o['n']}  {o['ad_ids']}")

    w = result["winner"]
    print(f"\nWINNER: {w.ad_id}")
    print(f"  pattern : {_key_str(result['winning_cluster'])}")
    print(f"  started : {w.start_date}  ({w.days_running} days running)")
    print(f"  imp/day : {result['ipd_by_id'][w.ad_id]:.1f}")

    # Clean hand-off artifact: the winner + ranking for the README and Part 2.
    Path("winner.json").write_text(json.dumps({
        "winner_ad_id": w.ad_id,
        "winning_pattern": _key_str(result["winning_cluster"]),
        "start_date": w.start_date.isoformat(),
        "days_running": w.days_running,
        "impressions_per_day": round(result["ipd_by_id"][w.ad_id], 1),
        "winner_record": w.raw,   # hook/angle/cta etc. for the brief
        "ranked_clusters": [{**r, "cluster": _key_str(r["cluster"])}
                            for r in result["ranked_clusters"]],
    }, indent=2))
    print("\nWrote winner.json")


if __name__ == "__main__":
    main()
