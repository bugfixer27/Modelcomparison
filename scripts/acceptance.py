"""Acceptance checks that need REAL data (run after some backfill):
  1. fair-comparison mode changes the ranking on at least one real cell
  2. answer: is the NAM-3km 2 m temperature cool-biased at KNYC, and under which regimes?
Usage: .venv/bin/python scripts/acceptance.py [months]"""
import sys, os, json
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import pandas as pd
from scoreboard.config import load
from scoreboard.store import Store
from scoreboard import metrics
from scoreboard.aggregate import rank_models

months = int(sys.argv[1]) if len(sys.argv) > 1 else 12
cfg = load(); store = Store(cfg.store)
now = pd.Timestamp.now("UTC")
pairs = metrics.build_pairs(cfg, store, now - pd.DateOffset(months=months), now)
print(f"pairs: {len(pairs)}  models: {sorted(pairs['model'].unique())}")

# 1. fair vs all ranking flips
flips = []
for (site, var), sv in pairs.groupby(["site", "var"]):
    if var in metrics.PRECIP_VARS:
        continue
    cells = metrics.compute_cells(sv, cfg, combos=[{}])
    for lb in metrics.lead_bin_labels(cfg.lead_bins):
        a = rank_models([c for c in cells if c["mode"] == "all" and c["lead_bin"] == lb and c["n"] >= cfg.main["min_n"]], var)
        f = rank_models([c for c in cells if c["mode"] == "fair" and c["lead_bin"] == lb and c["n"] >= cfg.main["min_n"]], var)
        if len(a) >= 2 and len(f) >= 2 and [c["model"] for c in a] != [c["model"] for c in f]:
            flips.append((site, var, lb, [f"{c['model']}:{c['mae']:.2f}/n{c['n']}" for c in a], [f"{c['model']}:{c['mae']:.2f}/n{c['n']}" for c in f]))
print(f"\n[1] cells where fair mode changes the ranking: {len(flips)}")
for s in flips[:8]:
    print(f"   {s[0]} {s[1]} {s[2]}h  all={s[3]}  fair={s[4]}")
print("   PASS" if flips else "   FAIL (or not enough overlapping data yet)")

# 2. NAM-3km t2m at KNYC by regime
sv = pairs[(pairs["site"] == "KNYC") & (pairs["var"] == "t2m") & (pairs["model"] == "nam3km")]
print(f"\n[2] NAM-3km 2 m T at KNYC: n={len(sv)}")
if len(sv):
    st = metrics.summarize(sv["err"])
    print(f"   overall bias {st['bias']:+.2f} F (MAE {st['mae']:.2f}, p10 {st['p10']:+.1f}, p50 {st['p50']:+.1f}, p90 {st['p90']:+.1f})")
    for dim in ("daynight", "sky", "flow", "season", "wet", "lead_bin"):
        g = sv.groupby(dim)["err"].agg(["size", "mean", lambda e: e.abs().mean()]).rename(columns={"size": "n", "mean": "bias", "<lambda_0>": "mae"})
        g = g[g["n"] >= cfg.main["min_n"]]
        print(f"   by {dim}: " + "; ".join(f"{k} {r['bias']:+.2f} (n={int(r['n'])})" for k, r in g.iterrows()))
    verdict = "cool-biased" if st["bias"] < -0.5 else ("warm-biased" if st["bias"] > 0.5 else "not meaningfully biased")
    print(f"   verdict: NAM-3km 2 m T at KNYC is {verdict} overall ({st['bias']:+.2f} F, n={st['n']}); see regime rows above for where it is worst.")
else:
    print("   no NAM pairs yet — run the Phase B backfill for nam3km")
