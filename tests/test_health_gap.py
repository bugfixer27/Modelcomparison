"""Acceptance check: the health report calls out a deliberately induced 2-day gap."""
import pandas as pd, numpy as np
from scoreboard.store import Store
from scoreboard.manifest import Manifest
from scoreboard.health import build_health


def test_induced_two_day_gap_is_reported(cfg):
    store, man = Store(cfg.store), Manifest(cfg.store / "manifest.sqlite")
    now = pd.Timestamp("2025-03-15 00:00", tz="UTC")
    # 14 days of obs, remove Mar 5-6 entirely
    t = pd.date_range(pd.Timestamp("2025-03-01", tz="UTC"), now, freq="h")
    keep = ~((t >= pd.Timestamp("2025-03-05", tz="UTC")) & (t < pd.Timestamp("2025-03-07", tz="UTC")))
    obs = pd.DataFrame({"site": "KNYC", "valid": t[keep], "var": "t2m", "value": 40.0, "qc_flag": "", "raw": None, "ingested_at": now})
    store.write_obs(obs)
    # HRRR cycles for the same span, skipping Mar 5-6
    init = pd.Timestamp("2025-03-01", tz="UTC")
    marks, rows = [], []
    while init < now:
        if not (pd.Timestamp("2025-03-05", tz="UTC") <= init < pd.Timestamp("2025-03-07", tz="UTC")):
            for f in cfg.model_fxx("hrrr"):
                marks.append(("hrrr", init, f, "ok", 5, None))
            rows.append(dict(model="hrrr", init=init, fxx=1, valid=init + pd.Timedelta(hours=1), site="KNYC", var="t2m", value=41.0,
                             grid_lat=40.8, grid_lon=-74.0, grid_elev=20.0, ingested_at=now))
        init += pd.Timedelta(hours=6)
    man.fcst_mark_many(marks)
    store.write_fcst(pd.DataFrame(rows))
    h = build_health(cfg, store, man, now=now)
    obs_gap_days = {g["day"] for g in h["gaps"]["obs"] if g["site"] == "KNYC"}
    assert obs_gap_days == {"2025-03-05", "2025-03-06"}
    fc_gap_inits = {g["init"] for g in h["gaps"]["fcst"] if g["model"] == "hrrr"}
    assert fc_gap_inits == {f"2025-03-0{d}T{h:02d}Z" for d in (5, 6) for h in (0, 6, 12, 18)}
    cov = [c for c in h["fcst_coverage"] if c["model"] == "hrrr"][0]
    assert 80 < cov["pct"] < 90    # 8 of 56 cycles missing
