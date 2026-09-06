"""ZentraCloud get_readings ingestion for the campus logger. Pages through history; honors 429 Retry-After."""
from __future__ import annotations
import logging, time
import numpy as np
import pandas as pd
import requests
from .derive import rh_from_t_td_f, td_from_t_c_vp_kpa, mslp_from_station_hpa
from .util import utcnow, c_to_f

URL = "https://zentracloud.com/api/v4/get_readings/"
log = logging.getLogger("scoreboard.obs_zentra")
MAX_DAYS_PER_CALL = 13   # 10-min data: 2000 readings/page ~= 13.9 days


def fetch_raw(token: str, device_sn: str, start: pd.Timestamp, end: pd.Timestamp, session=None) -> list[dict]:
    """Returns the raw 'data' dicts from every page in [start, end]."""
    s = session or requests.Session()
    h = {"Authorization": f"Token {token}"}
    pages, page = [], 1
    while True:
        params = dict(device_sn=device_sn, start_date=start.strftime("%Y-%m-%d %H:%M"), end_date=end.strftime("%Y-%m-%d %H:%M"),
                      output_format="json", per_page=2000, page_num=page, sort_by="asc")
        for attempt in range(8):
            r = s.get(URL, headers=h, params=params, timeout=120)
            if r.status_code == 429:
                wait = int(r.headers.get("Retry-After", "60")) + 2
                log.warning("Zentra 429; sleeping %ds", wait)
                time.sleep(min(wait, 180))
                continue
            if r.status_code >= 500:
                time.sleep(10 * (attempt + 1)); continue
            r.raise_for_status()
            break
        else:
            raise RuntimeError("Zentra: too many retries")
        j = r.json()
        pages.append(j.get("data", {}))
        pg = j.get("pagination", {})
        if not pg.get("next_url") or pg.get("page_num_readings", 0) == 0:
            break
        page += 1
        time.sleep(1.0)
    return pages


def _series(pages: list[dict], name: str, port: int | None = None) -> pd.Series:
    vals = {}
    for data in pages:
        for s in data.get(name, []):
            md = s.get("metadata", {})
            if port is not None and int(md.get("port_number", -1)) != port:
                continue
            for r in s.get("readings", []):
                if r.get("error_flag"):
                    continue
                v = r.get("value")
                if v is None:
                    continue
                vals[pd.Timestamp(int(r["timestamp_utc"]), unit="s", tz="UTC")] = float(v)
    return pd.Series(vals, dtype=float).sort_index()


def to_long(pages: list[dict], site_id: str, elev_m: float, soil_cfg: dict | None = None) -> pd.DataFrame:
    t_c = _series(pages, "Air Temperature", 1)
    if t_c.empty:
        return pd.DataFrame()
    vp = _series(pages, "Vapor Pressure", 1)
    p_kpa = _series(pages, "Atmospheric Pressure", 1)
    pr_mm = _series(pages, "Precipitation", 1)
    wd = _series(pages, "Wind Direction", 1)
    ws = _series(pages, "Wind Speed", 1)

    # instantaneous values at the top of the hour
    idx = t_c.index[(t_c.index.minute == 0) & (t_c.index.second == 0)]
    now = utcnow()
    rows = []

    def add(var, ser: pd.Series, flag=""):
        ser = ser.reindex(idx)
        ser = ser[ser.notna()]
        if ser.empty:
            return
        rows.append(pd.DataFrame({"site": site_id, "valid": ser.index, "var": var, "value": ser.values.astype(float),
                                  "qc_flag": flag, "raw": None, "ingested_at": now}))

    t_f = c_to_f(t_c)
    add("t2m", t_f)
    td_c = pd.Series(td_from_t_c_vp_kpa(t_c.reindex(vp.index).values, vp.values), index=vp.index)
    td_f = c_to_f(td_c)
    add("td2m", td_f, "derived")
    add("rh2m", pd.Series(rh_from_t_td_f(t_f.reindex(td_f.index).values, td_f.values), index=td_f.index), "derived")
    if not p_kpa.empty:
        tt = t_c.reindex(p_kpa.index)
        add("mslp", pd.Series(mslp_from_station_hpa(p_kpa.values * 10.0, elev_m, tt.values), index=p_kpa.index), "sta_fallback")
    add("wdir", wd)
    add("wspd", ws * 1.943844)   # m/s -> kt
    if not pr_mm.empty:
        # 1-h accumulation ending at the top of the hour: sum of 10-min intervals in (h-60min, h]
        acc = pr_mm.rolling("60min", closed="right").sum()
        # require full coverage: 6 samples in the window
        cnt = pr_mm.rolling("60min", closed="right").count()
        acc[cnt < 6] = np.nan
        add("precip1h", acc / 25.4)
    if soil_cfg and soil_cfg.get("enabled"):
        for port, depth in (soil_cfg.get("port_depth") or {}).items():
            add(f"soilm_{depth}", _series(pages, "Water Content", int(port)))
    if not rows:
        return pd.DataFrame()
    return pd.concat(rows, ignore_index=True)
