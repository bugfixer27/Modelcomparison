"""Acceptance check: no stored forecast value sits at a non-native valid time.
Runs against the REAL store (skips if empty) and against a synthetic violation."""
import pandas as pd, pytest
from pathlib import Path
from scoreboard.config import load
from scoreboard.store import Store


def violations(store: Store, cfg) -> list[str]:
    bad = []
    for model, site, path in store.fcst_files():
        if model not in cfg.main["models"]:
            bad.append(f"{path}: unknown model {model}"); continue
        native = set(cfg.model_fxx(model))
        df = pd.read_parquet(path)
        off = df[df["fxx"].map(lambda f: f not in native)]
        if len(off):
            bad.append(f"{path}: {len(off)} rows with non-native fxx {sorted(off['fxx'].unique())[:5]}")
        mism = df[(df["init"] + pd.to_timedelta(df["fxx"], unit="h")) != df["valid"]]
        if len(mism):
            bad.append(f"{path}: {len(mism)} rows where valid != init + fxx")
        # valid must be on the model's native hour grid relative to init (redundant with fxx check but explicit)
        step = cfg.model_step_h(model)
        offgrid = df[((df["valid"] - df["init"]).dt.total_seconds() / 3600) % step != 0]
        if len(offgrid):
            bad.append(f"{path}: {len(offgrid)} rows off the {step}h native grid")
    return bad


def test_real_store_has_only_native_valid_times():
    cfg = load()
    store = Store(cfg.store)
    if not store.fcst_files():
        pytest.skip("empty store")
    assert violations(store, cfg) == []


def test_detects_synthetic_violation(cfg):
    store = Store(cfg.store)
    init = pd.Timestamp("2025-01-01 00:00", tz="UTC")
    rows = [dict(model="gfs", init=init, fxx=f, valid=init + pd.Timedelta(hours=f), site="KNYC", var="t2m", value=50.0,
                 grid_lat=40.75, grid_lon=-74.0, grid_elev=10.0, ingested_at=init) for f in (3, 4)]  # 4 is not a GFS step
    store.write_fcst(pd.DataFrame(rows))
    v = violations(store, cfg)
    assert v and "non-native fxx" in v[0]
