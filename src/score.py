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

# SELECTION CONSTRAINT (not scoring). Part 2 has to produce a shot-by-shot storyboard,
# which is a video artifact: you cannot storyboard a static image. So the WINNER must come
# from a video cluster. This filters WHO CAN WIN — it does not touch how anything is
# scored. The full ranked table still reports every cluster, static included, so the
# finding that static patterns dominate Scribe's mix stays visible rather than hidden by
# the constraint. The unconstrained winner is computed and reported alongside.
WINNER_FORMATS = ("video",)

# Per-execution performance blend (within a cluster). TWO INDEPENDENT components —
# deliberately NOT divided into each other (see the unit-bug note in reach_standings).
W_REACH = 0.5        # how much reach the creative has achieved (a STANDING)
W_LONGEVITY = 0.5    # how long it has survived (evidence of proven-ness)


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
def reach_standings(ads: list[Ad]) -> dict[str, float]:
    """
    Relative reach STANDING per ad, in [0,1]. 1 = highest reach in the set.

    UNIT BUG FIXED 2026-09-12 (see CLAUDE.md 'Corrections'). This was previously
    `exposure(ad) / days_running`, i.e. an impressions-per-day RATE. That is a unit
    error: for the 83 of 94 creatives with no usable impression bucket, the reach
    signal is Meta's impressions-descending SORT ORDER — a CURRENT STANDING, not a
    cumulative lifetime impression count. Dividing a standing by age produces a
    quantity with no meaning, and empirically a 6.1x recency bias (median ipd 0.128
    for creatives <=7 days old vs 0.021 for those >=30 days). It also INVERTED the
    intent of longevity: age became a divisor/penalty when longevity is supposed to
    be evidence that a pattern is proven.

    The fix: never divide by age. Reach is a standing; longevity is a separate,
    positive component of perf. Both reach sources are mapped to a within-set [0,1]
    standing so neither dominates by raw magnitude — a bucket midpoint (30,000) and
    a rank standing (0.63) must never share one min-max scale.
    """
    n = len(ads)
    tiers = sorted({BUCKET_MIDPOINTS[a.impression_bucket] for a in ads
                    if a.impression_bucket in BUCKET_MIDPOINTS})
    out: dict[str, float] = {}
    for a in ads:
        if a.impression_bucket in BUCKET_MIDPOINTS:
            # real cumulative data: position among the bucket tiers present in the set
            out[a.ad_id] = (tiers.index(BUCKET_MIDPOINTS[a.impression_bucket]) + 1) / len(tiers)
        elif a.impression_rank is not None:
            out[a.ad_id] = (n - a.impression_rank + 1) / n   # rank 1 -> 1.0
        else:
            raise ValueError(f"Ad {a.ad_id}: no impression_bucket or impression_rank")
    return out


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


def score(ads: list[Ad], w_reach: float = W_REACH,
          w_longevity: float = W_LONGEVITY) -> dict:
    n = len(ads)

    # 1) Per-ad signals, normalized across the whole set. Reach and longevity are
    #    independent axes of perf; neither is divided into the other.
    reach = reach_standings(ads)
    reach_n = minmax([reach[a.ad_id] for a in ads])
    lon_n = minmax([float(a.days_running) for a in ads])
    perf_per_ad = {a.ad_id: w_reach * reach_n[i] + w_longevity * lon_n[i]
                   for i, a in enumerate(ads)}
    reach_by_id = {a.ad_id: reach[a.ad_id] for a in ads}

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

    # 6) Winner: best-performing execution inside the winning cluster — same blend of
    #    reach standing + longevity, with reach then age as tiebreaks.
    def best_in(key):
        return max(proven[key], key=lambda a: (perf_per_ad[a.ad_id],
                                               reach_by_id[a.ad_id], a.days_running))

    winning_key = rows[0]["cluster"]
    winner = best_in(winning_key)

    # 6b) Video-constrained selection (see WINNER_FORMATS). Applied AFTER scoring, over
    #     the same proven clusters and the same PatternScore ordering.
    #     TIEBREAK — necessary, not cosmetic: both video clusters currently score exactly
    #     0.000, because min-max sends whichever cluster holds an axis minimum to zero and
    #     the product annihilates it (talking_head holds min perf, text_animation min freq).
    #     Ordering by PatternScore alone would therefore pick by dict insertion order. Ties
    #     break on cluster size n, which is the tiebreak most faithful to the stated
    #     definition of best: "the most-repeated, durability-proven formula".
    fmt_i = CLUSTER_KEY_FIELDS.index("format")
    vid_rows = [r for r in rows if r["cluster"][fmt_i] in WINNER_FORMATS]
    constrained = None
    if vid_rows:
        vid_rows = sorted(vid_rows, key=lambda r: (r["pattern_score"], r["n"]), reverse=True)
        ckey = vid_rows[0]["cluster"]
        constrained = {"cluster": ckey, "winner": best_in(ckey),
                       "rows": vid_rows,
                       "tied": len({r["pattern_score"] for r in vid_rows}) < len(vid_rows)}

    return {
        "winner": winner,
        "winning_cluster": winning_key,
        "ranked_clusters": rows,
        "outliers": [{"cluster": k, "n": len(v), "ad_ids": [a.ad_id for a in v]}
                     for k, v in outliers.items()],
        "reach_by_id": reach_by_id,
        "perf_by_id": perf_per_ad,
        "constrained": constrained,
    }


def _key_str(k: tuple) -> str:
    return " / ".join(str(x) for x in k)


def main():
    ads = load_ads(Path("data/ads.json"))
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
    print(f"\nUNCONSTRAINED WINNER (top PatternScore over ALL proven clusters): {w.ad_id}")
    print(f"  pattern : {_key_str(result['winning_cluster'])}")
    print(f"  started : {w.start_date}  ({w.days_running} days running)")
    print(f"  reach   : {result['reach_by_id'][w.ad_id]:.3f} standing (1.0 = highest in set)")
    print(f"  perf    : {result['perf_by_id'][w.ad_id]:.3f}  "
          f"(= {W_REACH} x reach + {W_LONGEVITY} x longevity)")

    c = result["constrained"]
    if not c:
        raise SystemExit(f"No proven cluster with format in {WINNER_FORMATS} — "
                         f"cannot select a storyboardable winner.")
    cw = c["winner"]
    print(f"\nVIDEO-CONSTRAINED WINNER (format in {WINNER_FORMATS}; "
          f"Part 2 needs a storyboard): {cw.ad_id}")
    print(f"  pattern : {_key_str(c['cluster'])}")
    print(f"  started : {cw.start_date}  ({cw.days_running} days running)")
    print(f"  reach   : {result['reach_by_id'][cw.ad_id]:.3f} standing")
    print(f"  perf    : {result['perf_by_id'][cw.ad_id]:.3f}")
    print("  video-eligible proven clusters, by PatternScore then n:")
    for r in c["rows"]:
        print(f"    {_key_str(r['cluster']):40} n={r['n']:>3}  score={r['pattern_score']:.3f}")
    if c["tied"]:
        print("    NOTE: PatternScores tie here — resolved on cluster size n "
              "(most-repeated proven formula).")

    # Clean hand-off artifact: the winner + ranking for the README and Part 2.
    Path("data/winner.json").write_text(json.dumps({
        "winner_ad_id": cw.ad_id,
        "winning_pattern": _key_str(c["cluster"]),
        "selection_constraint": {
            "winner_formats": list(WINNER_FORMATS),
            "why": "Part 2 produces a shot-by-shot storyboard, which is a video artifact.",
            "patternscore_tie_broken_on_cluster_size": c["tied"],
            "unconstrained_winner_ad_id": w.ad_id,
            "unconstrained_pattern": _key_str(result["winning_cluster"]),
        },
        "start_date": cw.start_date.isoformat(),
        "days_running": cw.days_running,
        "reach_standing": round(result["reach_by_id"][cw.ad_id], 4),
        "perf": round(result["perf_by_id"][cw.ad_id], 4),
        "winner_record": cw.raw,   # hook/angle/cta etc. for the brief
        "ranked_clusters": [{**r, "cluster": _key_str(r["cluster"])}
                            for r in result["ranked_clusters"]],
    }, indent=2))
    # Committed Part-1 deliverables. data/ is gitignored scratch; these two are the
    # reviewable artifacts of the scoring stage — the ranked table a human reads, and the
    # winner hand-off. Additive output only: no scoring logic is involved in writing them.
    RESULTS = Path("results")
    RESULTS.mkdir(exist_ok=True)

    sb = ["# Scoreboard — ranked creative patterns", "",
          f"{len(ads)} distinct creatives (deduplicated from raw ad records by dedup.py).",
          f"PatternScore = norm(frequency) x norm(performance), a product: a pattern wins",
          f"only if it is BOTH heavily repeated AND its instances survive.", "",
          "| pattern (format / structure_type) | n | freq | perf | PatternScore |",
          "|---|---|---|---|---|"]
    for r in result["ranked_clusters"]:
        sb.append(f"| {_key_str(r['cluster'])} | {r['n']} | {r['freq']:.3f} | "
                  f"{r['perf']:.3f} | {r['pattern_score']:.3f} |")
    sb += ["", "## Outliers — unproven experiments, excluded from winner selection", "",
           f"Clusters of n <= {OUTLIER_MAX_SIZE} are too small to call a proven pattern.", ""]
    for o in result["outliers"]:
        sb.append(f"- `{_key_str(o['cluster'])}` (n={o['n']}): {', '.join(o['ad_ids'])}")
    sb += ["", "## Winner", "",
           f"Unconstrained (top PatternScore, all clusters): `{w.ad_id}` "
           f"— {_key_str(result['winning_cluster'])}", ""]
    if c:
        sb.append(f"Video-constrained (WINNER_FORMATS={WINNER_FORMATS}; Part 2 needs a "
                  f"storyboard): **`{cw.ad_id}`** — {_key_str(c['cluster'])}, "
                  f"rank {cw.impression_rank} of {len(ads)}, {cw.days_running} days running.")
        if c["tied"]:
            sb.append("")
            sb.append("Both video clusters score PatternScore 0.000 (min-max sends an "
                      "axis-minimum cluster to zero and the product annihilates it), so the "
                      "tie was broken on cluster size n — the most-repeated proven formula.")
    (RESULTS / "02_scoreboard.md").write_text("\n".join(sb) + "\n")
    (RESULTS / "03_winner.json").write_text((Path("data/winner.json")).read_text())

    print("\nWrote data/winner.json")
    print("Wrote results/02_scoreboard.md  (committed: the ranked table)")
    print("Wrote results/03_winner.json    (committed: winner hand-off)")

    print("\nSensitivity — does the winning cluster survive a different perf blend?")
    print(f"  {'weights (reach/longevity)':30} {'winning cluster':36} winner")
    for wr, wl, label in ((1.0, 0.0, "reach only"), (0.7, 0.3, "reach-weighted"),
                          (0.5, 0.5, "blended (default)"), (0.3, 0.7, "longevity-weighted"),
                          (0.0, 1.0, "longevity only")):
        alt = score(ads, w_reach=wr, w_longevity=wl)
        same = "" if alt["winning_cluster"] == result["winning_cluster"] else "   <- FLIPS"
        mark = "" if alt["winner"].ad_id == w.ad_id else "  (different creative)"
        print(f"  {label + f' {wr}/{wl}':30} {_key_str(alt['winning_cluster']):36} "
              f"{alt['winner'].ad_id}{mark}{same}")


if __name__ == "__main__":
    main()
