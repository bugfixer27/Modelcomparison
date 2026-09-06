"""HRRR backfill via the HRRR-Zarr archive (s3://hrrrzarr, anonymous). Backfill ONLY; forward collection stays on
Herbie/GRIB so production has one code path. One chunk read per (cycle, variable) returns all 48 leads for
a 150x150 tile; every configured site lies in the same tile, so a cycle costs ~5 small reads.

Grid indices come from the manifest gridpoint table populated by the GRIB path (same 1059x1799 grid), which
guarantees the two paths pick the same grid point."""
from __future__ import annotations
import logging, warnings
import numpy as np
import pandas as pd
from .config import Config
from .store import Store
from .manifest import Manifest
from .extract import Stats
from .util import k_to_f, utcnow, to_utc

log = logging.getLogger("scoreboard.extract_zarr")
warnings.filterwarnings("ignore")
ZVARS = {"t2m": ("2m_above_ground", "TMP"), "td2m": ("2m_above_ground", "DPT"), "mslp": ("mean_sea_level", "MSLMA"),
         "vis": ("surface", "VIS"), "precip1h": ("surface", "APCP_1hr_acc_fcst")}
GRID_SHAPE = "1059x1799"


class ZarrExtractor:
    def __init__(self, cfg: Config, store: Store, man: Manifest, stats: Stats | None = None):
        import s3fs
        self.cfg, self.store, self.man = cfg, store, man
        self.stats = stats or Stats()
        self.fs = s3fs.S3FileSystem(anon=True)
        self.gp = {}
        for sid in cfg.sites:
            r = man.gridpoint("hrrr", sid, GRID_SHAPE)
            if r is None:
                raise RuntimeError(f"no HRRR gridpoint for {sid}; run one GRIB extraction first (python -m scoreboard.run --no-obs --no-aggregate --models hrrr)")
            self.gp[sid] = r   # (i, j, lat, lon, elev)

    def _open(self, init: pd.Timestamp, level: str, var: str):
        import zarr, s3fs
        day, cyc = init.strftime("%Y%m%d"), init.strftime("%H")
        root = f"hrrrzarr/sfc/{day}/{day}_{cyc}z_fcst.zarr/{level}/{var}"
        g = zarr.open_group(s3fs.S3Map(root=root, s3=self.fs, check=False), mode="r")
        return g[f"{level}/{var}"], g

    def extract_cycle(self, init, fxx_list: list[int] | None = None) -> dict:
        init = to_utc(init)
        fxx_all = self.cfg.model_fxx("hrrr")
        todo = sorted(set(fxx_list or fxx_all) - self.man.fcst_done("hrrr", init))
        summary = {"model": "hrrr", "init": str(init), "ok": 0, "missing": 0, "error": 0, "skipped": len(fxx_all) - len(todo)}
        if not todo:
            return summary
        ii = [g[0] for g in self.gp.values()]; jj = [g[1] for g in self.gp.values()]
        i0, i1, j0, j1 = min(ii), max(ii) + 1, min(jj), max(jj) + 1
        series: dict[str, np.ndarray] = {}
        periods = None
        try:
            for var, (level, zv) in ZVARS.items():
                arr, g = self._open(init, level, zv)
                self.stats.requests += 1
                if periods is None:
                    # forecast_period lives at the level-group root of the same store
                    try:
                        periods = np.asarray(g["forecast_period"][:]) if "forecast_period" in g else None
                    except Exception:
                        periods = None
                block = arr[:, i0:i1, j0:j1]
                self.stats.bytes += block.nbytes
                series[var] = block
        except Exception as e:
            msg = f"zarr {init:%Y%m%d%H}: {str(e)[:160]}"
            log.warning(msg)
            self.man.fcst_mark_many([("hrrr", init, f, "missing", 0, msg) for f in todo])
            summary["missing"] += len(todo)
            self.stats.files_missing += len(todo)
            return summary
        n_lead = next(iter(series.values())).shape[0]
        if periods is not None and len(periods) == n_lead:
            # forecast_period is in hours (int) for hrrrzarr; validate it is 1..n
            lead_of_index = {k: int(periods[k]) for k in range(n_lead)}
        else:
            lead_of_index = {k: k + 1 for k in range(n_lead)}
        now = utcnow()
        rows, marks = [], []
        for k in range(n_lead):
            fxx = lead_of_index[k]
            if fxx not in todo:
                continue
            valid = init + pd.Timedelta(hours=fxx)
            n = 0
            for sid, (i, j, glat, glon, gelev) in self.gp.items():
                for var, block in series.items():
                    x = float(block[k, i - i0, j - j0])
                    if not np.isfinite(x):
                        continue
                    if var in ("t2m", "td2m"):
                        x = k_to_f(x)
                    elif var == "mslp":
                        x = x / 100.0
                    elif var == "vis":
                        x = min(x / 1609.344, float(self.cfg.main["vis_cap_mi"]))
                    elif var == "precip1h":
                        x = x / 25.4
                    rows.append(dict(model="hrrr", init=init, fxx=fxx, valid=valid, site=sid, var=var, value=x,
                                     grid_lat=glat, grid_lon=glon, grid_elev=gelev, ingested_at=now))
                    n += 1
            marks.append(("hrrr", init, fxx, "ok", n, None))
        missing = [f for f in todo if f not in lead_of_index.values()]
        marks += [("hrrr", init, f, "missing", 0, f"zarr has {n_lead} leads") for f in missing]
        if rows:
            self.store.write_fcst(pd.DataFrame(rows))
            self.stats.rows += len(rows)
        self.man.fcst_mark_many(marks)
        summary["ok"] += len(marks) - len(missing)
        summary["missing"] += len(missing)
        self.stats.files_ok += len(marks) - len(missing)
        return summary
