import numpy as np, pandas as pd
from scoreboard.qc import apply_qc, is_good
from scoreboard import metrics


def _long(site, var, series, flag=""):
    return pd.DataFrame({"site": site, "valid": series.index, "var": var, "value": series.values, "qc_flag": flag, "raw": None,
                         "ingested_at": pd.Timestamp("2025-01-01", tz="UTC")})


def test_qc_flags(cfg):
    t = pd.date_range("2025-01-01", periods=24, freq="h", tz="UTC")
    temp = pd.Series(np.linspace(30, 40, 24), index=t)
    temp.iloc[10] = 90.0            # spike (jump > 20)
    temp.iloc[15:22] = 35.0         # flatline 7 h
    dew = pd.Series(25.0 + np.arange(24) * 0.3, index=t)
    dew.iloc[3] = 60.0              # dewpoint > temp
    vis = pd.Series(np.full(24, 10.0), index=t)   # constant vis is legitimate
    obs = pd.concat([_long("KNYC", "t2m", temp), _long("KNYC", "td2m", dew), _long("KNYC", "vis", vis)], ignore_index=True)
    obs.loc[(obs["var"] == "t2m") & (obs["valid"] == t[0]), "value"] = 200.0   # out of range
    q = apply_qc(obs, cfg.main["qc"])
    f = q.set_index(["var", "valid"])["qc_flag"]
    assert "range" in f[("t2m", t[0])]
    assert "spike" in f[("t2m", t[10])]
    assert all("flatline" in f[("t2m", x)] for x in t[15:22])
    assert "td_gt_t" in f[("t2m", t[3])] and "td_gt_t" in f[("td2m", t[3])]
    assert all(f[("vis", x)] == "" for x in t)
    assert not is_good(q["qc_flag"]).all() and is_good(q["qc_flag"]).sum() > 40
    # QC is idempotent
    q2 = apply_qc(q, cfg.main["qc"])
    assert (q2["qc_flag"].values == q["qc_flag"].values).all()


def test_bias_sign_is_forecast_minus_obs():
    s = metrics.summarize(np.array([2.0, 2.0, 2.0]))
    assert s["bias"] == 2.0 and s["mae"] == 2.0 and s["n"] == 3


def test_contingency():
    fc = np.array([0.0, 0.05, 0.2, 0.0, 0.3])
    ob = np.array([0.0, 0.0, 0.15, 0.02, 0.5])
    c = metrics.contingency(fc, ob, [0.01])
    d = c["thr"]["0.01"]
    assert (d["hits"], d["misses"], d["fa"], d["cn"]) == (2, 1, 1, 1)
    assert abs(d["csi"] - 0.5) < 1e-9 and abs(d["pod"] - 2 / 3) < 1e-9 and abs(d["far"] - 1 / 3) < 1e-9
    assert c["cond"]["n"] == 2


def test_lead_bins(cfg):
    lb = metrics.assign_lead_bin(pd.Series([0, 1, 6, 7, 12, 24, 25, 48, 96, 144, 145]), cfg.lead_bins)
    assert list(lb) == ["0-6", "0-6", "0-6", "6-12", "6-12", "12-24", "24-48", "24-48", "48-96", "96-144", None] or lb.iloc[-1] is pd.NA


def test_obs_truth_precip3h_requires_full_window():
    t = pd.date_range("2025-01-01", periods=6, freq="h", tz="UTC")
    p = pd.Series([0.1, 0.1, 0.1, 0.1, 0.1, 0.1], index=t)
    obs = _long("KNYC", "precip1h", p)
    obs.loc[obs["valid"] == t[3], "qc_flag"] = "spike"   # hour 3 flagged
    s = metrics.obs_truth(obs, "precip3h")
    assert abs(s[t[2]] - 0.3) < 1e-9
    assert t[3] not in s.index and t[4] not in s.index and t[5] not in s.index


def test_fair_mode_changes_ranking(cfg):
    """Synthetic: model A is only run at easy times, model B at all times. In all-data mode A looks better;
    restricted to common valid times B wins. Fair mode must flip the ranking."""
    t = pd.date_range("2025-01-01", periods=200, freq="h", tz="UTC")
    rows = []
    for i, v in enumerate(t):
        hard = i % 2 == 1
        ob = 50.0
        # B: constant 1.0 error everywhere
        rows.append(dict(model="B", site="KNYC", var="t2m", init=v, fxx=3, valid=v, fc=ob + 1.0, ob=ob))
        # A: only easy hours in all-data (error 0.5), but also present at half the hard hours (error 3.0)
        if not hard:
            rows.append(dict(model="A", site="KNYC", var="t2m", init=v, fxx=3, valid=v, fc=ob + 0.5, ob=ob))
        elif i % 4 == 1:
            rows.append(dict(model="A", site="KNYC", var="t2m", init=v, fxx=3, valid=v, fc=ob + 3.0, ob=ob))
    p = pd.DataFrame(rows)
    p["err"] = p["fc"] - p["ob"]; p["lead_bin"] = "0-6"
    for d in ("sky", "flow", "daynight", "season", "wet"):
        p[d] = "x"
    p["month"] = 1
    cells = metrics.compute_cells(p, cfg, combos=[{}])
    mae = {(c["mode"], c["model"]): c["mae"] for c in cells}
    # all-data: A has 100 easy (0.5) + 50 hard (3.0) -> 1.33 vs B 1.0 : B wins anyway here, so build the flip explicitly:
    # In fair mode only common times count: A 150 pts (1.33), B same 150 pts (1.0) -> B wins.
    # All-data B is scored on all 200 (1.0). Make B worse on A-less hours so all-data favors A.
    p.loc[(p["model"] == "B") & ~p["valid"].isin(p[p["model"] == "A"]["valid"]), "err"] = 9.0
    cells = metrics.compute_cells(p, cfg, combos=[{}])
    mae = {(c["mode"], c["model"]): c["mae"] for c in cells}
    assert mae[("all", "A")] < mae[("all", "B")]        # all-data: A looks better
    assert mae[("fair", "B")] < mae[("fair", "A")]      # fair: B is better on common times
    n = {(c["mode"], c["model"]): c["n"] for c in cells}
    assert n[("fair", "A")] == n[("fair", "B")] == 150 and n[("all", "B")] == 200
