"""IEM ASOS ingestion. One request per period for all ASOS sites; chunk by year for backfill."""
from __future__ import annotations
import io, logging
import numpy as np
import pandas as pd
import requests
from .derive import rh_from_t_td_f, mslp_from_altimeter_hpa
from .util import retry, utcnow, f_to_c

URL = "https://mesonet.agron.iastate.edu/cgi-bin/request/asos.py"
SKY_CODE = {"CLR": 0, "SKC": 0, "NSC": 0, "NCD": 0, "FEW": 1, "SCT": 2, "BKN": 3, "OVC": 4, "VV": 4}
log = logging.getLogger("scoreboard.obs_asos")


def fetch_raw(station_ids: list[str], start: pd.Timestamp, end: pd.Timestamp, session: requests.Session | None = None) -> pd.DataFrame:
    s = session or requests.Session()
    params = [("data", "all"), ("tz", "UTC"), ("format", "onlycomma"), ("latlon", "yes"), ("elev", "yes"),
              ("missing", "M"), ("trace", "0.0001"), ("direct", "no"),
              ("report_type", "3"), ("report_type", "4"),
              ("year1", start.year), ("month1", start.month), ("day1", start.day), ("hour1", start.hour), ("minute1", start.minute),
              ("year2", end.year), ("month2", end.month), ("day2", end.day), ("hour2", end.hour), ("minute2", end.minute)]
    params += [("station", sid) for sid in station_ids]

    def go():
        r = s.get(URL, params=params, timeout=300)
        if r.status_code == 422:
            raise RuntimeError(f"IEM 422 (request too large?): {r.text[:200]}")
        r.raise_for_status()
        return r.text

    text = retry(go, tries=4, base=3, log=log, what="IEM ASOS")
    if not text.strip() or text.startswith("ERROR"):
        raise RuntimeError(f"IEM returned no data: {text[:200]}")
    df = pd.read_csv(io.StringIO(text), na_values=["M"], keep_default_na=False, low_memory=False)
    df["valid"] = pd.to_datetime(df["valid"], utc=True)
    return df


def _num(s):
    return pd.to_numeric(s, errors="coerce")


def to_long(raw: pd.DataFrame, site_id: str, asos_id: str, elev_m: float) -> pd.DataFrame:
    """Reduce METARs to one report per hour (routine preferred, COR wins), return long-format obs rows."""
    ids = {asos_id.upper(), ("K" + asos_id).upper(), asos_id.upper().lstrip("K")}
    d = raw[raw["station"].astype(str).str.upper().isin(ids)].copy()
    if d.empty:
        return pd.DataFrame()
    d["metar"] = d["metar"].astype(str)
    d["is_cor"] = d["metar"].str.contains(r"\bCOR\b")
    d["hour"] = d["valid"].dt.round("h")
    d["dist_min"] = (d["valid"] - d["hour"]).abs().dt.total_seconds() / 60.0
    d["is_routine"] = d["valid"].dt.minute >= 45   # routine ASOS reports go out ~:51-:59
    # priority: corrected first, then routine, then closest to the hour
    d = d.sort_values(["hour", "is_cor", "is_routine", "dist_min"], ascending=[True, False, False, True])
    d = d[d["dist_min"] <= 30].drop_duplicates("hour", keep="first")

    t = _num(d["tmpf"]); td = _num(d["dwpf"])
    rows = []
    now = utcnow()

    def add(var, val, flag="", rawv=None):
        rows.append(pd.DataFrame({"site": site_id, "valid": d["hour"].values, "var": var,
                                  "value": np.asarray(val, dtype=float), "qc_flag": flag,
                                  "raw": rawv if rawv is not None else None, "ingested_at": now}))

    cor = np.where(d["is_cor"], "corrected", "")
    add("t2m", t, cor, d["metar"].values)
    add("td2m", td, cor)
    add("rh2m", rh_from_t_td_f(t, td), "derived")
    add("wdir", _num(d["drct"]))
    add("wspd", _num(d["sknt"]))
    add("vis", _num(d["vsby"]))
    add("precip1h", _num(d["p01i"]))
    # MSLP: prefer reported SLP; fall back to altimeter reduction and flag it
    mslp = _num(d["mslp"]); alti = _num(d["alti"])
    fb = mslp.isna() & alti.notna() & t.notna()
    m = mslp.copy()
    m[fb] = mslp_from_altimeter_hpa(alti[fb], elev_m, f_to_c(t[fb]))
    add("mslp", m, np.where(fb, "alt_fallback", ""))
    # sky cover: max over layers
    cov = pd.DataFrame({c: d[c].map(SKY_CODE) for c in ("skyc1", "skyc2", "skyc3", "skyc4") if c in d}).max(axis=1)
    add("skycov", cov)
    out = pd.concat(rows, ignore_index=True)
    out = out[out["value"].notna() | (out["var"] == "t2m")]  # keep t2m row so the raw METAR survives even if T missing
    out["qc_flag"] = out["qc_flag"].astype(str)
    return out.reset_index(drop=True)
