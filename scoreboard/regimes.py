"""Regime tagging from observations only. Deterministic and re-runnable from the store."""
from __future__ import annotations
import numpy as np
import pandas as pd
from .solar import solar_elevation
from .qc import is_good

REGIME_DIMS = ["sky", "flow", "daynight", "season", "wet"]
SEASON_OF_MONTH = {12: "DJF", 1: "DJF", 2: "DJF", 3: "MAM", 4: "MAM", 5: "MAM",
                   6: "JJA", 7: "JJA", 8: "JJA", 9: "SON", 10: "SON", 11: "SON"}


def _in_sector(deg: np.ndarray, sector) -> np.ndarray:
    a, b = float(sector[0]), float(sector[1])
    d = deg % 360.0
    if a <= b:
        return (d >= a) & (d <= b)
    return (d >= a) | (d <= b)


def sky_from_cover(cover: pd.Series) -> pd.Series:
    """cover: numeric max cloud cover code (0 CLR/SKC, 1 FEW, 2 SCT, 3 BKN, 4 OVC/VV), NaN unknown."""
    out = pd.Series("unknown", index=cover.index, dtype=object)
    out[cover <= 2] = "clear"
    out[cover >= 3] = "cloudy"
    return out


def tag_site(obs: pd.DataFrame, site_id: str, site_cfg: dict, regime_cfg: dict) -> pd.DataFrame:
    """obs: long obs df for one site (any vars). Returns df(site, valid, sky, flow, daynight, season, month, wet)."""
    if obs.empty:
        return pd.DataFrame(columns=["site", "valid"] + REGIME_DIMS + ["month"])
    o = obs.copy()
    o["valid"] = pd.to_datetime(o["valid"], utc=True)
    good = o[is_good(o["qc_flag"])]
    piv = good.pivot_table(index="valid", columns="var", values="value", aggfunc="first")
    # every valid time that has ANY obs gets a row, so joins never silently drop times
    idx = pd.DatetimeIndex(sorted(o["valid"].unique()))
    piv = piv.reindex(idx)
    out = pd.DataFrame(index=idx)
    out["site"] = site_id

    # sky
    out["sky"] = sky_from_cover(piv["skycov"]) if "skycov" in piv else "unknown"

    # flow
    calm_kt = float(regime_cfg.get("calm_kt", 3))
    flow = pd.Series("other", index=idx, dtype=object)
    if "wspd" in piv:
        wd = piv["wdir"].to_numpy(dtype=float) if "wdir" in piv else np.full(len(idx), np.nan)
        ws = piv["wspd"].to_numpy(dtype=float)
        sect = site_cfg.get("flow", {})
        kws = np.isfinite(ws)
        kwd = np.isfinite(wd)
        on = kwd & _in_sector(wd, sect.get("onshore", [0, -1]))
        off = kwd & _in_sector(wd, sect.get("offshore", [0, -1]))
        flow[on] = "onshore"
        flow[off] = "offshore"
        flow[~kwd & kws] = "other"      # direction absent but speed known: METAR VRB (variable) winds
        flow[~kws] = "unknown"
        flow[kws & (ws < calm_kt)] = "calm"    # calm is decided by speed alone (direction is often absent when calm)
    else:
        flow[:] = "unknown"
    out["flow"] = flow

    # daynight
    dn = regime_cfg.get("daynight", {})
    elev = solar_elevation(idx, float(site_cfg["lat"]), float(site_cfg["lon"]))
    out["daynight"] = np.where(elev > float(dn.get("day_min_elev_deg", 10)), "day",
                               np.where(elev < float(dn.get("night_max_elev_deg", -6)), "night", "transition"))

    # season
    out["month"] = idx.month
    out["season"] = [SEASON_OF_MONTH[m] for m in idx.month]

    # wet
    thr = float(regime_cfg.get("wet_threshold_in", 0.005))
    if "precip1h" in piv:
        p = piv["precip1h"]
        out["wet"] = np.where(p > thr, "wet", np.where(p.notna(), "dry", "unknown"))
    else:
        out["wet"] = "unknown"

    out = out.reset_index().rename(columns={"index": "valid"})
    return out[["site", "valid"] + REGIME_DIMS + ["month"]]
