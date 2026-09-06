"""Build site/data/*.json from the store. Small pre-aggregated files only; no raw pairs ship to the browser."""
from __future__ import annotations
import json, logging, math
from pathlib import Path
import numpy as np
import pandas as pd
from .config import Config
from .store import Store
from .manifest import Manifest
from . import metrics
from .metrics import PRECIP_VARS
from .regimes import REGIME_DIMS, SEASON_OF_MONTH, tag_site
from .qc import is_good
from .health import build_health

log = logging.getLogger("scoreboard.aggregate")
LADDER = [  # (label, dims kept)
    ("full regime", ["sky", "flow", "daynight", "season", "wet"]),
    ("sky relaxed", ["flow", "daynight", "season", "wet"]),
    ("sky+flow relaxed", ["daynight", "season", "wet"]),
    ("sky+flow+season relaxed", ["daynight", "wet"]),
    ("day/night only", ["daynight"]),
    ("unconditioned (all data)", []),
]


def _clean(o):
    """JSON-safe: NaN -> None, numpy -> python, Timestamps -> ISO."""
    if isinstance(o, dict):
        return {str(k): _clean(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [_clean(v) for v in o]
    if isinstance(o, (np.integer,)):
        return int(o)
    if isinstance(o, (np.floating, float)):
        return None if not math.isfinite(float(o)) else round(float(o), 4)
    if isinstance(o, (pd.Timestamp,)):
        return o.strftime("%Y-%m-%dT%H:%MZ")
    if isinstance(o, np.bool_):
        return bool(o)
    return o


def _write(path: Path, obj) -> bool:
    """Write JSON if changed. Returns True when the file changed."""
    path.parent.mkdir(parents=True, exist_ok=True)
    s = json.dumps(_clean(obj), separators=(",", ":"))
    if path.exists() and path.read_text() == s:
        return False
    path.write_text(s)
    return True


# ------------------------------------------------------------------ ranking
def rank_models(cells: list[dict], var: str) -> list[dict]:
    """Sort model cells best-first. Non-precip: MAE then |bias|. Precip: CSI@lowest threshold desc, then |fbias-1|."""
    if var in PRECIP_VARS:
        def key(c):
            t = c.get("thr", {})
            k0 = sorted(t)[0] if t else None
            csi = t[k0]["csi"] if k0 and t[k0]["csi"] is not None else -1.0
            fb = t[k0]["fbias"] if k0 and t[k0]["fbias"] is not None else 99.0
            return (-csi, abs(fb - 1.0))
    else:
        def key(c):
            return (c.get("mae", float("inf")), abs(c.get("bias", float("inf"))))
    return sorted(cells, key=key)


def _score(c: dict, var: str):
    if var in PRECIP_VARS:
        t = c.get("thr", {})
        k0 = sorted(t)[0] if t else None
        return t[k0]["csi"] if k0 else None
    return c.get("mae")


def current_regime(obs: pd.DataFrame, site: str, cfg: Config, now: pd.Timestamp) -> dict:
    """Regime at the most recent QC'd obs time."""
    if obs.empty:
        return {"obs_time": None, "stale_h": None, **{d: "unknown" for d in REGIME_DIMS}}
    good = obs[is_good(obs["qc_flag"]) & obs["var"].isin(["t2m", "wspd", "wdir", "skycov", "precip1h"])]
    if good.empty:
        return {"obs_time": None, "stale_h": None, **{d: "unknown" for d in REGIME_DIMS}}
    t = good["valid"].max()
    reg = tag_site(obs[obs["valid"] >= t - pd.Timedelta(hours=1)], site, cfg.sites[site], cfg.regimes)
    row = reg[reg["valid"] == t]
    r = row.iloc[0].to_dict() if not row.empty else {d: "unknown" for d in REGIME_DIMS}
    out = {d: r.get(d, "unknown") for d in REGIME_DIMS}
    out["season"] = SEASON_OF_MONTH[now.month]
    out["obs_time"] = t
    out["stale_h"] = round((now - t).total_seconds() / 3600, 1)
    # a few raw values for the chips
    piv = good.pivot_table(index="valid", columns="var", values="value", aggfunc="first")
    if t in piv.index:
        out["obs"] = {k: (None if pd.isna(v) else float(v)) for k, v in piv.loc[t].items()}
    return out


def trust_cell(pairs_sv: pd.DataFrame, regime: dict, cfg: Config, mode: str, lead_max_h: int = 24) -> dict:
    """Apply the fallback ladder for one site x var in one mode. Returns the chosen step and ranking."""
    var = pairs_sv["var"].iloc[0] if not pairs_sv.empty else None
    min_n = int(cfg.main["min_n"])
    min_ev = int(cfg.main.get("min_events", 10))
    thr = cfg.main["precip_thresholds_in"]

    def enough(c: dict) -> bool:
        if c.get("n", 0) < min_n:
            return False
        if var in PRECIP_VARS:
            t = c.get("thr", {})
            k0 = sorted(t)[0] if t else None
            return bool(k0) and t[k0]["events"] >= min_ev
        return True

    p = pairs_sv[pairs_sv["fxx"] <= lead_max_h]
    compared = sorted(p["model"].unique().tolist())
    if mode == "fair" and not p.empty:
        fm, _ = metrics.fair_mask(p.assign(lead_bin="0-24"))
        p = p[fm.to_numpy()]
    result = {"mode": mode, "compared": compared, "lead_h": [0, lead_max_h], "steps": []}
    unknown = [d for d in REGIME_DIMS if regime.get(d, "unknown") == "unknown"]
    chosen = None
    for label, dims in LADDER:
        dims_eff = [d for d in dims if d not in unknown]
        constraint = {d: regime[d] for d in dims_eff}
        sub = p[metrics.combo_mask(p, constraint)] if not p.empty else p
        cells = []
        for model, g in sub.groupby("model"):
            st = metrics.cell_stats(g, var, thr)
            cells.append({"model": model, **st})
        ranked = rank_models(cells, var)
        n_ok = sum(1 for c in ranked if enough(c))
        step = {"label": label, "constraint": constraint, "relaxed": [d for d in REGIME_DIMS if d not in dims_eff],
                "unknown_dims": unknown, "models": ranked, "n_models_ok": n_ok}
        result["steps"].append({"label": label, "n_models_ok": n_ok, "n_best": ranked[0]["n"] if ranked else 0})
        accept = (n_ok >= 2) or (len(compared) <= 1 and n_ok >= 1)
        if chosen is None and (accept or label == LADDER[-1][0]):
            chosen = step
    if chosen is None or not chosen["models"]:
        result.update(winner=None, models=[], label="no data", constraint={}, relaxed=REGIME_DIMS, insufficient=True)
        return result
    top = chosen["models"][0]
    s1 = _score(top, var)
    s2 = _score(chosen["models"][1], var) if len(chosen["models"]) > 1 else None
    margin = None
    if s1 is not None and s2 is not None:
        margin = (s1 - s2) if var in PRECIP_VARS else (s2 - s1)   # positive = winner better
    result.update(winner=top["model"], models=chosen["models"], label=chosen["label"], constraint=chosen["constraint"],
                  relaxed=chosen["relaxed"], unknown_dims=chosen["unknown_dims"], margin=margin,
                  runner_up=chosen["models"][1]["model"] if len(chosen["models"]) > 1 else None,
                  insufficient=not enough(top))
    return result


# ------------------------------------------------------------------ builders
def build_all(cfg: Config, store: Store, man: Manifest, months: int = 12, now: pd.Timestamp | None = None) -> dict:
    now = now or pd.Timestamp.now("UTC")
    out_dir = cfg.site_data
    start = now - pd.DateOffset(months=months)
    changed = []
    log.info("building pairs %s .. %s", start, now)
    pairs = metrics.build_pairs(cfg, store, start, now)
    log.info("pairs: %d", len(pairs))

    # ---- meta ----
    gp = man.gridpoints_df()
    na = {}
    for v in cfg.variables:
        na[v] = [m for m in cfg.models if v not in cfg.model_vars(m)]
    meta = {
        "generated_at": now, "analysis_window": {"start": start, "end": now, "months": months},
        "sites": {sid: {k: s.get(k) for k in ("name", "lat", "lon", "elev_m", "obs", "notes", "flow")} for sid, s in cfg.sites.items()},
        "models": {m: {"label": mc.get("label", m), "step_h": cfg.model_step_h(m), "cycles": mc["cycles"],
                       "vars": cfg.model_vars(m), "precip_var": cfg.precip_var(m), "fxx_max": max(cfg.model_fxx(m))} for m, mc in cfg.models.items()},
        "variables": cfg.variables, "units": cfg.main["units"], "lead_bins": metrics.lead_bin_labels(cfg.lead_bins),
        "min_n": cfg.main["min_n"], "precip_thresholds_in": cfg.main["precip_thresholds_in"],
        "series_leads_h": cfg.main["series_leads_h"], "series_days": cfg.main["series_days"],
        "gridpoints": gp[["model", "site", "grid_lat", "grid_lon", "grid_elev"]].to_dict("records") if not gp.empty else [],
        "na": na, "regime_dims": REGIME_DIMS, "regime_cfg": cfg.regimes,
        "pair_counts": pairs.groupby(["model", "site", "var"]).size().reset_index(name="n").to_dict("records") if not pairs.empty else [],
    }
    if _write(out_dir / "meta.json", meta):
        changed.append("meta.json")

    # ---- trust today ----
    tt = {"generated_at": now, "sites": {}}
    obs_cache = {}
    for site in cfg.sites:
        obs = store.read_obs(site, now - pd.Timedelta(days=3), now)
        obs_cache[site] = obs
        reg = current_regime(obs, site, cfg, now)
        vars_out = {}
        for var in cfg.variables:
            sv = pairs[(pairs["site"] == site) & (pairs["var"] == var)] if not pairs.empty else pairs
            if sv.empty:
                vars_out[var] = {"na": True, "reason": "no matched pairs"}
                continue
            vars_out[var] = {"fair": trust_cell(sv, reg, cfg, "fair"), "all": trust_cell(sv, reg, cfg, "all")}
        tt["sites"][site] = {"regime": reg, "vars": vars_out}
    if _write(out_dir / "trust_today.json", tt):
        changed.append("trust_today.json")

    # ---- explorer + series per site x var ----
    for site in cfg.sites:
        obs_all = store.read_obs(site, now - pd.Timedelta(days=cfg.main["series_days"] + 1), now)
        for var in cfg.variables:
            sv = pairs[(pairs["site"] == site) & (pairs["var"] == var)] if not pairs.empty else pairs
            cells = metrics.compute_cells(sv, cfg) if not sv.empty else []
            _, compared = metrics.fair_mask(sv) if not sv.empty else (None, {})
            ex = {"site": site, "var": var, "generated_at": now, "n_pairs": int(len(sv)),
                  "compared": compared.get((site, var), []), "cells": cells,
                  "window": {"start": sv["valid"].min() if not sv.empty else None, "end": sv["valid"].max() if not sv.empty else None}}
            if _write(out_dir / "explorer" / f"{site}_{var}.json", ex):
                changed.append(f"explorer/{site}_{var}.json")
            # series
            ser = {"site": site, "var": var, "generated_at": now, "obs": [], "models": {}}
            if not obs_all.empty:
                if var in ("precip3h", "precip6h"):
                    t = metrics.obs_truth(obs_all, var)
                    ser["obs"] = [[i, float(v), ""] for i, v in t.items()]
                else:
                    o = obs_all[obs_all["var"] == var].sort_values("valid")
                    ser["obs"] = [[r.valid, float(r.value), r.qc_flag] for r in o.itertuples()]
            for model in cfg.models:
                if var not in cfg.model_vars(model):
                    continue
                fc = store.read_fcst(model, site, now - pd.Timedelta(days=cfg.main["series_days"]), now)
                if fc.empty:
                    continue
                if var == "rh2m":
                    from .derive import rh_from_t_td_f
                    w = fc[fc["var"].isin(["t2m", "td2m"])].pivot_table(index=["init", "fxx", "valid"], columns="var", values="value").dropna().reset_index()
                    w["value"] = rh_from_t_td_f(w["t2m"], w["td2m"])
                    fc = w
                else:
                    fc = fc[fc["var"] == var]
                leads = {}
                for L in cfg.main["series_leads_h"]:
                    if L not in cfg.model_fxx(model):
                        continue
                    g = fc[fc["fxx"] == L].sort_values("valid")
                    if not g.empty:
                        leads[str(L)] = [[r.valid, float(r.value)] for r in g.itertuples()]
                if leads:
                    ser["models"][model] = leads
            if _write(out_dir / "series" / f"{site}_{var}.json", ser):
                changed.append(f"series/{site}_{var}.json")

    # ---- health ----
    h = build_health(cfg, store, man, now)
    if _write(out_dir / "health.json", h):
        changed.append("health.json")
    return {"changed": changed, "n_pairs": int(len(pairs))}
