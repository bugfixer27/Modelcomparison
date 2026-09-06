"""Herbie point extraction. One GRIB subset download per (model, init, fxx); nearest grid point per site.

Native-time rule: a stored forecast's valid time is always init + fxx hours for an fxx in the model's
configured step list. No temporal interpolation anywhere.
"""
from __future__ import annotations
import logging, os, re, warnings
from dataclasses import dataclass, field
from pathlib import Path
import numpy as np
import pandas as pd
from .config import Config
from .store import Store
from .manifest import Manifest
from .util import k_to_f, utcnow, to_utc, retry

log = logging.getLogger("scoreboard.extract")
warnings.filterwarnings("ignore", category=FutureWarning)
warnings.filterwarnings("ignore", category=UserWarning)

# cfgrib (shortName, typeOfLevel) -> our var id
SHORTNAMES = {
    "t2m": {("2t", "heightAboveGround")},
    "td2m": {("2d", "heightAboveGround")},
    "mslp": {("mslma", "meanSea"), ("prmsl", "meanSea"), ("msl", "meanSea")},
    "vis": {("vis", "surface")},
    "precip": {("tp", "surface")},
    "elev": {("orog", "surface")},
}
ACC_RE = re.compile(r":(\d+)-(\d+) hour acc")


@dataclass
class Stats:
    requests: int = 0
    bytes: int = 0
    files_ok: int = 0
    files_missing: int = 0
    files_error: int = 0
    rows: int = 0
    started: pd.Timestamp = field(default_factory=lambda: pd.Timestamp.now("UTC"))

    def line(self, todo: int | None = None, done: int | None = None) -> str:
        el = (pd.Timestamp.now("UTC") - self.started).total_seconds()
        s = f"req={self.requests} MB={self.bytes/1e6:.0f} ok={self.files_ok} missing={self.files_missing} err={self.files_error} rows={self.rows} elapsed={el/60:.1f}m"
        if todo and done is not None and done > 0:
            rate = el / done
            s += f" ETA={(todo-done)*rate/3600:.1f}h"
        return s


def _nearest_ij(lat: np.ndarray, lon: np.ndarray, slat: float, slon: float) -> tuple[int, int, float, float]:
    """Nearest grid point by (cos-lat scaled) degree distance. Works for 1-D (regular) or 2-D (curvilinear) grids."""
    lon = np.where(lon > 180, lon - 360.0, lon)
    if lat.ndim == 1:
        i = int(np.abs(lat - slat).argmin())
        j = int(np.abs(lon - slon).argmin())
        return i, j, float(lat[i]), float(lon[j])
    d = (lat - slat) ** 2 + ((lon - slon) * np.cos(np.radians(slat))) ** 2
    i, j = np.unravel_index(int(d.argmin()), d.shape)
    return int(i), int(j), float(lat[i, j]), float(lon[i, j])


class Extractor:
    PRIORITY = ["aws", "google", "azure", "nomads"]

    def __init__(self, cfg: Config, store: Store, man: Manifest, stats: Stats | None = None):
        self.cfg, self.store, self.man = cfg, store, man
        self.stats = stats or Stats()
        self.cache_dir = cfg.herbie_cache
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self._gp: dict[tuple[str, str, str], tuple] = {}   # (model, site, shape) -> (i,j,lat,lon,elev)

    # ------------------------------------------------------------------ helpers
    def _herbie(self, model: str, init: pd.Timestamp, fxx: int):
        from herbie import Herbie
        hb = self.cfg.main["models"][model]["herbie"]
        self.stats.requests += 1
        # aws/google/azure/nomads only: UCAR RDA is excluded (SSL certificate failures and slow).
        return Herbie(init.tz_convert(None).to_pydatetime(), model=hb["model"], product=hb["product"], fxx=fxx,
                      save_dir=str(self.cache_dir), verbose=False, priority=self.PRIORITY)

    def _search_pattern(self, model: str, fxx: int, inv: pd.DataFrame, need_elev: bool, precip_bucket: tuple | None) -> str:
        f = self.cfg.main["models"][model]["fields"]
        pats = [f[v] for v in ("t2m", "td2m", "mslp", "vis") if v in f]
        if need_elev and "elev" in f:
            pats.append(f["elev"])
        if precip_bucket is not None:
            a, b = precip_bucket
            if self.cfg.main["models"][model]["precip_kind"] == "bucket":
                pats.append(f"{f['precip']}{a}-{b} hour acc")
            else:
                pats.append(f["precip"])
        return "|".join(pats)

    def _choose_bucket(self, model: str, fxx: int, inv: pd.DataFrame) -> tuple | None:
        """For NCEP APCP: choose the bucket ending at fxx with the shortest length. Cumulative models: (0, fxx)."""
        mc = self.cfg.main["models"][model]
        if "precip" not in mc["fields"] or fxx == 0:
            return None
        if mc["precip_kind"] == "cumulative":
            return (0, fxx)
        cands = []
        for s in inv["search_this"]:
            if mc["fields"]["precip"] in s:
                m = ACC_RE.search(s)
                if m and int(m.group(2)) == fxx:
                    cands.append((int(m.group(1)), int(m.group(2))))
        if not cands:
            return None
        return min(cands, key=lambda ab: ab[1] - ab[0])

    def _gridpoint(self, model: str, site: str, lat: np.ndarray, lon: np.ndarray, elev_field: np.ndarray | None):
        shape = "x".join(map(str, lat.shape if lat.ndim == 2 else (lat.shape[0], lon.shape[0])))
        key = (model, site, shape)
        if key in self._gp:
            return self._gp[key]
        r = self.man.gridpoint(model, site, shape)
        if r is not None and (r[4] is not None or elev_field is None):
            self._gp[key] = tuple(r)
            return self._gp[key]
        s = self.cfg.sites[site]
        i, j, glat, glon = _nearest_ij(lat, lon, float(s["lat"]), float(s["lon"]))
        elev = float(elev_field[i, j]) if elev_field is not None else (r[4] if r else None)
        self.man.gridpoint_set(model, site, shape, i, j, glat, glon, elev)
        self._gp[key] = (i, j, glat, glon, elev)
        log.info("gridpoint %s %s -> (%d,%d) %.4f,%.4f elev=%s (site elev %s m)", model, site, i, j, glat, glon, elev, s.get("elev_m"))
        return self._gp[key]

    def _need_elev(self, model: str) -> bool:
        if "elev" not in self.cfg.main["models"][model]["fields"]:
            return False
        df = self.man.gridpoints_df()
        have = set(df[(df["model"] == model) & df["grid_elev"].notna()]["site"]) if not df.empty else set()
        return any(sid not in have for sid in self.cfg.sites)

    # ------------------------------------------------------------------ one file
    def fetch_fields(self, model: str, init: pd.Timestamp, fxx: int, need_elev: bool = False) -> dict | None:
        """Download one subset and return {var: {site: value}} in storage units, plus bucket metadata.
        Returns None when the file does not exist (yet)."""
        import cfgrib
        H = self._herbie(model, init, fxx)
        if H.grib is None:
            return None
        inv = H.inventory()
        bucket = self._choose_bucket(model, fxx, inv)
        pattern = self._search_pattern(model, fxx, inv, need_elev, bucket)
        path = Path(retry(lambda: H.download(pattern, verbose=False), tries=3, base=3, log=log, what=f"{model} {init:%Y%m%d%H} f{fxx:02d}"))
        self.stats.requests += 1
        self.stats.bytes += path.stat().st_size
        out: dict = {"_bucket": bucket, "_valid": None}
        try:
            dss = cfgrib.open_datasets(str(path), backend_kwargs={"indexpath": ""})
            # pass 1: orography (lives in its own surface-level dataset)
            elev_da = None
            for ds in dss:
                for v in ds.data_vars:
                    a = ds[v].attrs
                    if (a.get("GRIB_shortName"), a.get("GRIB_typeOfLevel")) in SHORTNAMES["elev"]:
                        elev_da = ds[v].values
            # pass 2: fields
            for ds in dss:
                lat, lon = ds["latitude"].values, ds["longitude"].values
                vt = pd.Timestamp(ds["valid_time"].values).tz_localize("UTC")
                if out["_valid"] is None:
                    out["_valid"] = vt
                for v in ds.data_vars:
                    a = ds[v].attrs
                    key = (a.get("GRIB_shortName"), a.get("GRIB_typeOfLevel"))
                    var = next((k for k, s in SHORTNAMES.items() if key in s), None)
                    if var is None or var == "elev":
                        continue
                    arr = ds[v].values
                    units = a.get("GRIB_units", "")
                    vals, meta = {}, {}
                    for sid in self.cfg.sites:
                        i, j, glat, glon, gelev = self._gridpoint(model, sid, lat, lon, elev_da)
                        x = float(arr[i, j])
                        if var in ("t2m", "td2m"):
                            x = k_to_f(x)
                        elif var == "mslp":
                            x = x / 100.0
                        elif var == "vis":
                            x = min(x / 1609.344, float(self.cfg.main["vis_cap_mi"]))
                        elif var == "precip":
                            if units == "m":
                                x = x * 1000.0
                            x = x / 25.4          # mm -> in
                        vals[sid] = x
                        meta[sid] = (glat, glon, gelev)
                    out[var] = vals
                    out["_meta"] = meta
        finally:
            try:
                os.remove(path)
            except OSError:
                pass
        return out

    # ------------------------------------------------------------------ one cycle
    def extract_cycle(self, model: str, init, fxx_list: list[int] | None = None, force: bool = False,
                      retry_missing: bool = False, flush_every: int = 8) -> dict:
        """Extract all sites/vars for one model cycle. Resumable via the manifest. Never raises."""
        init = to_utc(init)
        mc = self.cfg.main["models"][model]
        step = self.cfg.model_step_h(model)
        window = int(mc["precip_window_h"])
        pvar = self.cfg.precip_var(model)
        fxx_all = self.cfg.model_fxx(model)
        fxx_list = fxx_list or fxx_all
        done = set() if force else self.man.fcst_done(model, init)
        todo = [f for f in fxx_list if f not in done]
        if not force and not retry_missing:
            pass
        summary = {"model": model, "init": str(init), "ok": 0, "missing": 0, "error": 0, "skipped": len(fxx_list) - len(todo)}
        if not todo:
            return summary
        need_elev = self._need_elev(model)
        prev_precip: dict[int, dict] = {}   # fxx -> {"bucket": (a,b), "vals": {site: mm_in}}
        rows, marks = [], []

        def flush():
            nonlocal rows, marks
            if rows:
                self.store.write_fcst(pd.DataFrame(rows))
                self.stats.rows += len(rows)
            if marks:
                self.man.fcst_mark_many(marks)
            rows, marks = [], []

        for fxx in todo:
            if not retry_missing and not force and self.man.fcst_status(model, init, fxx) == "missing":
                summary["missing"] += 1
                continue
            try:
                res = self.fetch_fields(model, init, fxx, need_elev)
            except Exception as e:
                log.warning("%s %s f%02d error: %s", model, init, fxx, str(e)[:200])
                self.man.error(f"fcst/{model}", f"{init} f{fxx:02d}: {e}")
                marks.append((model, init, fxx, "error", 0, str(e)))
                summary["error"] += 1
                self.stats.files_error += 1
                continue
            if res is None:
                marks.append((model, init, fxx, "missing", 0, None))
                summary["missing"] += 1
                self.stats.files_missing += 1
                continue
            need_elev = False
            valid = init + pd.Timedelta(hours=fxx)
            if res.get("_valid") is not None and res["_valid"] != valid:
                msg = f"valid_time mismatch {res['_valid']} != {valid}"
                marks.append((model, init, fxx, "error", 0, msg))
                summary["error"] += 1
                continue
            now = utcnow()
            n = 0
            for var in ("t2m", "td2m", "mslp", "vis"):
                if var in res:
                    for sid, x in res[var].items():
                        glat, glon, gelev = res["_meta"][sid]
                        rows.append(dict(model=model, init=init, fxx=fxx, valid=valid, site=sid, var=var, value=x,
                                         grid_lat=glat, grid_lon=glon, grid_elev=gelev, ingested_at=now))
                        n += 1
            # precip over the model's native window (fxx-window, fxx]
            if "precip" in res and fxx >= window:
                bucket = res["_bucket"]
                cur = res["precip"]
                vals = None
                if mc["precip_kind"] == "cumulative":
                    prev = prev_precip.get(fxx - window)
                    if prev is None and fxx - window > 0:
                        prev = self._fetch_precip_only(model, init, fxx - window)
                    if fxx - window == 0:
                        vals = cur
                    elif prev is not None:
                        vals = {s: max(cur[s] - prev["vals"][s], 0.0) for s in cur}
                else:
                    a, b = bucket
                    if b - a == window:
                        vals = cur
                    elif b - a > window:
                        prev = prev_precip.get(fxx - window)
                        if prev is None or prev["bucket"][0] != a:
                            prev = self._fetch_precip_only(model, init, fxx - window)
                        if prev is not None and prev["bucket"][0] == a:
                            vals = {s: max(cur[s] - prev["vals"][s], 0.0) for s in cur}
                    # b - a < window can't happen (we pick the shortest bucket >= step)
                prev_precip[fxx] = {"bucket": bucket, "vals": cur}
                if vals is not None:
                    for sid, x in vals.items():
                        glat, glon, gelev = res["_meta"][sid]
                        rows.append(dict(model=model, init=init, fxx=fxx, valid=valid, site=sid, var=pvar, value=x,
                                         grid_lat=glat, grid_lon=glon, grid_elev=gelev, ingested_at=now))
                        n += 1
            marks.append((model, init, fxx, "ok", n, None))
            summary["ok"] += 1
            self.stats.files_ok += 1
            if len(marks) >= flush_every:
                flush()
        flush()
        return summary

    def _fetch_precip_only(self, model: str, init: pd.Timestamp, fxx: int) -> dict | None:
        try:
            r = self.fetch_fields(model, init, fxx, need_elev=False)
        except Exception as e:
            log.warning("precip prev fetch failed %s %s f%02d: %s", model, init, fxx, e)
            return None
        if r is None or "precip" not in r:
            return None
        return {"bucket": r["_bucket"], "vals": r["precip"]}
