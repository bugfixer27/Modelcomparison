"""Matched pairs and verification statistics.

Pairs are formed at analysis time by joining forecasts and QC-passed obs on (site, var, valid).
Nothing here interpolates in time: a forecast only pairs with an ob at exactly its valid time.
"""
from __future__ import annotations
import itertools, logging
import numpy as np
import pandas as pd
from .config import Config
from .store import Store
from .qc import is_good
from .regimes import REGIME_DIMS, tag_site

log = logging.getLogger("scoreboard.metrics")
PRECIP_VARS = ("precip1h", "precip3h", "precip6h")


def lead_bin_labels(bins: list[tuple[int, int]]) -> list[str]:
    return [f"{lo}-{hi}" for lo, hi in bins]


def assign_lead_bin(fxx: pd.Series, bins: list[tuple[int, int]]) -> pd.Series:
    out = pd.Series(pd.NA, index=fxx.index, dtype=object)
    for lo, hi in bins:
        m = (fxx > lo) & (fxx <= hi) if lo > 0 else (fxx >= 0) & (fxx <= hi)
        out[m & out.isna()] = f"{lo}-{hi}"
    return out


# ------------------------------------------------------------------ obs series
def obs_truth(obs: pd.DataFrame, var: str) -> pd.Series:
    """QC-passed truth series for `var`, indexed by valid. precip{N}h (N>1) is summed from hourly obs
    and requires every hour in the window to be present and QC-good."""
    good = obs[is_good(obs["qc_flag"])]
    if var in ("precip3h", "precip6h"):
        n = int(var[6])
        p = good[good["var"] == "precip1h"].set_index("valid")["value"].sort_index()
        if p.empty:
            return p
        full = p.resample("1h").first()          # NaN where an hour is missing/flagged
        s = full.rolling(n).sum()                # NaN if any hour in the window is NaN
        s = s[full.index.isin(p.index)]          # keep only hours that themselves have an ob
        return s.dropna()
    s = good[good["var"] == var].set_index("valid")["value"].sort_index()
    return s[~s.index.duplicated(keep="last")]


# ------------------------------------------------------------------ pairs
def build_pairs(cfg: Config, store: Store, start, end, sites=None, models=None, vars=None) -> pd.DataFrame:
    """All matched pairs in [start, end] (by valid). Columns:
    model, site, var, init, fxx, valid, lead_bin, fc, ob, err + regime dims."""
    sites = sites or list(cfg.sites)
    models = models or list(cfg.models)
    vars = vars or cfg.variables
    bins = cfg.lead_bins
    parts = []
    regimes = []
    for site in sites:
        obs = store.read_obs(site, pd.Timestamp(start) - pd.Timedelta(hours=8), end)
        if obs.empty:
            continue
        reg = tag_site(obs, site, cfg.sites[site], cfg.regimes)
        regimes.append(reg)
        truths = {v: obs_truth(obs, v) for v in vars}
        # rh2m on the model side is derived from t2m/td2m
        for model in models:
            fc = store.read_fcst(model, site, start, end)
            if fc.empty:
                continue
            if "rh2m" in vars and "t2m" in set(fc["var"]) and "td2m" in set(fc["var"]):
                from .derive import rh_from_t_td_f
                w = fc[fc["var"].isin(["t2m", "td2m"])].pivot_table(index=["init", "fxx", "valid"], columns="var", values="value")
                w = w.dropna()
                rh = w.reset_index()
                rh["var"] = "rh2m"
                rh["value"] = rh_from_t_td_f(rh["t2m"], rh["td2m"])
                rh["model"], rh["site"] = model, site
                fc = pd.concat([fc, rh[["model", "init", "fxx", "valid", "site", "var", "value"]]], ignore_index=True)
            for var in vars:
                t = truths.get(var)
                if t is None or t.empty:
                    continue
                f = fc[fc["var"] == var]
                if f.empty:
                    continue
                m = f.merge(t.rename("ob"), left_on="valid", right_index=True, how="inner")
                if m.empty:
                    continue
                m = m.rename(columns={"value": "fc"})
                m["err"] = m["fc"] - m["ob"]
                m["lead_bin"] = assign_lead_bin(m["fxx"], bins)
                parts.append(m[["model", "site", "var", "init", "fxx", "valid", "lead_bin", "fc", "ob", "err"]])
    if not parts:
        return pd.DataFrame(columns=["model", "site", "var", "init", "fxx", "valid", "lead_bin", "fc", "ob", "err"] + REGIME_DIMS + ["month"])
    pairs = pd.concat(parts, ignore_index=True)
    pairs = pairs[pairs["lead_bin"].notna()]
    reg = pd.concat(regimes, ignore_index=True) if regimes else pd.DataFrame(columns=["site", "valid"] + REGIME_DIMS + ["month"])
    pairs = pairs.merge(reg, on=["site", "valid"], how="left")
    for d in REGIME_DIMS:
        pairs[d] = pairs[d].fillna("unknown")
    pairs["month"] = pairs["month"].fillna(pairs["valid"].dt.month).astype(int)
    return pairs.reset_index(drop=True)


# ------------------------------------------------------------------ fair mode
def fair_mask(pairs: pd.DataFrame, key_cols=("site", "var", "lead_bin")) -> tuple[pd.Series, dict]:
    """Keep only (site, var, lead_bin, valid) keys where every compared model has >= 1 pair.
    Compared models for a (site, var) = models with any pair for it. Returns (mask, {(site,var): [models]})."""
    if pairs.empty:
        return pd.Series(dtype=bool), {}
    compared = pairs.groupby(["site", "var"])["model"].unique().to_dict()
    keys = list(key_cols) + ["valid"]
    have = pairs.groupby(keys)["model"].nunique()
    need = pairs[["site", "var"]].drop_duplicates()
    need["n_models"] = [len(compared[(s, v)]) for s, v in zip(need["site"], need["var"])]
    ok_keys = have.reset_index().merge(need, on=["site", "var"])
    ok_keys = ok_keys[ok_keys["model"] >= ok_keys["n_models"]][keys]
    ok_keys["_ok"] = True
    m = pairs[keys].merge(ok_keys, on=keys, how="left")["_ok"].fillna(False).to_numpy(dtype=bool)
    return pd.Series(m, index=pairs.index), {k: sorted(v.tolist()) for k, v in compared.items()}


# ------------------------------------------------------------------ statistics
def summarize(err: np.ndarray) -> dict:
    e = np.asarray(err, dtype=float)
    e = e[np.isfinite(e)]
    n = int(e.size)
    if n == 0:
        return {"n": 0}
    q = np.percentile(e, [10, 50, 90])
    return {"n": n, "bias": float(e.mean()), "mae": float(np.abs(e).mean()), "rmse": float(np.sqrt((e ** 2).mean())),
            "p10": float(q[0]), "p50": float(q[1]), "p90": float(q[2])}


def contingency(fc: np.ndarray, ob: np.ndarray, thresholds: list[float]) -> dict:
    fc, ob = np.asarray(fc, float), np.asarray(ob, float)
    ok = np.isfinite(fc) & np.isfinite(ob)
    fc, ob = fc[ok], ob[ok]
    out = {"n": int(fc.size), "thr": {}}
    for t in thresholds:
        f, o = fc >= t, ob >= t
        h, m, fa, cn = int((f & o).sum()), int((~f & o).sum()), int((f & ~o).sum()), int((~f & ~o).sum())
        d = {"hits": h, "misses": m, "fa": fa, "cn": cn, "events": h + m}
        d["pod"] = h / (h + m) if h + m else None
        d["far"] = fa / (h + fa) if h + fa else None
        d["csi"] = h / (h + m + fa) if h + m + fa else None
        d["fbias"] = (h + fa) / (h + m) if h + m else None
        out["thr"][f"{t:.2f}"] = d
    # conditional amount error where both wet at the lowest threshold
    t0 = min(thresholds)
    both = (fc >= t0) & (ob >= t0)
    if both.any():
        e = fc[both] - ob[both]
        out["cond"] = {"n": int(both.sum()), "bias": float(e.mean()), "mae": float(np.abs(e).mean())}
    else:
        out["cond"] = {"n": 0}
    return out


def cell_stats(sub: pd.DataFrame, var: str, thresholds: list[float]) -> dict:
    if var in PRECIP_VARS:
        return contingency(sub["fc"].to_numpy(), sub["ob"].to_numpy(), thresholds)
    return summarize(sub["err"].to_numpy())


# ------------------------------------------------------------------ regime combos
def regime_combos(pairs: pd.DataFrame, max_dims: int = 2) -> list[dict]:
    """'all', every single-dim value (incl. month), and every pair of dims with observed values."""
    combos = [{}]
    vals = {d: sorted(v for v in pairs[d].dropna().unique()) for d in REGIME_DIMS}
    for d in REGIME_DIMS:
        for v in vals[d]:
            combos.append({d: v})
    for m in sorted(pairs["month"].dropna().unique()):
        combos.append({"month": int(m)})
    if max_dims >= 2:
        for a, b in itertools.combinations(REGIME_DIMS, 2):
            present = pairs[[a, b]].drop_duplicates()
            for va, vb in present.itertuples(index=False):
                combos.append({a: va, b: vb})
    return combos


def combo_key(c: dict) -> str:
    return "all" if not c else "|".join(f"{k}={v}" for k, v in sorted(c.items()))


def combo_mask(df: pd.DataFrame, c: dict) -> np.ndarray:
    m = np.ones(len(df), dtype=bool)
    for k, v in c.items():
        m &= (df[k] == v).to_numpy()
    return m


def compute_cells(pairs: pd.DataFrame, cfg: Config, combos: list[dict] | None = None) -> list[dict]:
    """Cells for one (site, var) subset of pairs, both modes. Each cell:
    {model, lead_bin, regime, mode, ...stats}"""
    if pairs.empty:
        return []
    var = pairs["var"].iloc[0]
    thr = cfg.main["precip_thresholds_in"]
    combos = combos if combos is not None else regime_combos(pairs)
    fm, compared = fair_mask(pairs)
    out = []
    for mode, base in (("all", np.ones(len(pairs), bool)), ("fair", fm.to_numpy())):
        p = pairs[base]
        if p.empty:
            continue
        for c in combos:
            sub = p[combo_mask(p, c)]
            if sub.empty:
                continue
            for (model, lb), g in sub.groupby(["model", "lead_bin"], observed=True):
                st = cell_stats(g, var, thr)
                out.append({"model": model, "lead_bin": lb, "regime": combo_key(c), "mode": mode, **st})
    return out
