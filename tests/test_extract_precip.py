"""Precip bucket / cumulative differencing logic, exercised without network by stubbing fetch_fields."""
import pandas as pd
from scoreboard.store import Store
from scoreboard.manifest import Manifest
from scoreboard.extract import Extractor


class Stub(Extractor):
    def __init__(self, cfg, store, man, model, buckets, cum):
        super().__init__(cfg, store, man)
        self.model, self.buckets, self.cum = model, buckets, cum
        self.calls = []

    def fetch_fields(self, model, init, fxx, need_elev=False):
        self.calls.append(fxx)
        sites = list(self.cfg.sites)
        meta = {s: (40.0, -74.0, 10.0) for s in sites}
        if self.cum is not None:
            return {"_bucket": (0, fxx), "_valid": init + pd.Timedelta(hours=fxx), "_meta": meta,
                    "t2m": {s: 60.0 for s in sites}, "precip": {s: self.cum[fxx] for s in sites}}
        a, b, _ = self.buckets[fxx]
        return {"_bucket": (a, b), "_valid": init + pd.Timedelta(hours=fxx), "_meta": meta,
                "t2m": {s: 60.0 for s in sites}, "precip": {s: self.buckets[fxx][2] for s in sites}}


def _precip(store, model, site="KNYC"):
    df = store.read_fcst(model, site)
    return df[df["var"].str.startswith("precip")].set_index("fxx")["value"].to_dict()


def test_nam_three_hour_buckets_difference_to_hourly(cfg):
    store, man = Store(cfg.store), Manifest(cfg.store / "manifest.sqlite")
    # NAM buckets: f1 0-1, f2 0-2, f3 0-3, f4 3-4, f5 3-5, f6 3-6 ; values are bucket totals (inches)
    buckets = {1: (0, 1, 0.10), 2: (0, 2, 0.30), 3: (0, 3, 0.35), 4: (3, 4, 0.00), 5: (3, 5, 0.20), 6: (3, 6, 0.25)}
    ex = Stub(cfg, store, man, "nam3km", buckets, None)
    ex.extract_cycle("nam3km", pd.Timestamp("2025-01-01 00:00", tz="UTC"), [1, 2, 3, 4, 5, 6])
    p = _precip(store, "nam3km")
    assert {k: round(v, 3) for k, v in p.items()} == {1: 0.10, 2: 0.20, 3: 0.05, 4: 0.00, 5: 0.20, 6: 0.05}


def test_gfs_six_hour_bucket_is_differenced_to_three_hourly_and_restart_refetches(cfg):
    store, man = Store(cfg.store), Manifest(cfg.store / "manifest.sqlite")
    buckets = {0: (0, 0, 0.0), 3: (0, 3, 0.10), 6: (0, 6, 0.40), 9: (6, 9, 0.05), 12: (6, 12, 0.15)}
    ex = Stub(cfg, store, man, "gfs", buckets, None)
    init = pd.Timestamp("2025-01-01 00:00", tz="UTC")
    ex.extract_cycle("gfs", init, [0, 3, 6, 9])
    # simulate a restart: f12 processed in a fresh extractor (no in-memory previous bucket) -> must refetch f09
    ex2 = Stub(cfg, store, man, "gfs", buckets, None)
    ex2.extract_cycle("gfs", init, [12])
    p = _precip(store, "gfs")
    assert {k: round(v, 3) for k, v in p.items()} == {3: 0.10, 6: 0.30, 9: 0.05, 12: 0.10}
    assert ex2.calls == [12, 9]


def test_ifs_cumulative_is_differenced_per_native_step(cfg):
    store, man = Store(cfg.store), Manifest(cfg.store / "manifest.sqlite")
    cum = {0: 0.0, 3: 0.02, 6: 0.02, 9: 0.12}
    ex = Stub(cfg, store, man, "ifs", None, cum)
    ex.extract_cycle("ifs", pd.Timestamp("2025-01-01 00:00", tz="UTC"), [0, 3, 6, 9])
    p = _precip(store, "ifs")
    assert {k: round(v, 3) for k, v in p.items()} == {3: 0.02, 6: 0.0, 9: 0.10}
    df = store.read_fcst("ifs", "KNYC")
    assert set(df[df["var"] == "precip3h"]["fxx"]) == {3, 6, 9}   # nothing at f00
