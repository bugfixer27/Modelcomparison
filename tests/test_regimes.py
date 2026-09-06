import numpy as np, pandas as pd
from scoreboard.regimes import tag_site
from scoreboard.solar import solar_elevation


def _obs(site="KWST"):
    t = pd.date_range("2025-07-01", periods=48, freq="h", tz="UTC")
    rng = np.random.default_rng(0)
    rows = []
    for i, v in enumerate(t):
        rows += [dict(site=site, valid=v, var="t2m", value=70 + rng.normal(), qc_flag="", raw=None, ingested_at=v),
                 dict(site=site, valid=v, var="wdir", value=float((i * 15) % 360), qc_flag="", raw=None, ingested_at=v),
                 dict(site=site, valid=v, var="wspd", value=float(i % 8), qc_flag="", raw=None, ingested_at=v),
                 dict(site=site, valid=v, var="skycov", value=float(i % 5), qc_flag="", raw=None, ingested_at=v),
                 dict(site=site, valid=v, var="precip1h", value=0.02 if i % 7 == 0 else 0.0, qc_flag="", raw=None, ingested_at=v)]
    return pd.DataFrame(rows)


def test_tagging_is_deterministic_and_pure(cfg):
    obs = _obs()
    a = tag_site(obs, "KWST", cfg.sites["KWST"], cfg.regimes)
    b = tag_site(obs.sample(frac=1, random_state=3), "KWST", cfg.sites["KWST"], cfg.regimes)  # shuffled input
    pd.testing.assert_frame_equal(a.reset_index(drop=True), b.reset_index(drop=True))
    assert set(a["sky"]) <= {"clear", "cloudy", "unknown"}
    assert set(a["flow"]) <= {"onshore", "offshore", "other", "calm", "unknown"}
    assert set(a["daynight"]) == {"day", "night", "transition"}
    assert (a["season"] == "JJA").all()
    assert set(a["wet"]) == {"wet", "dry"}


def test_sector_change_retags_without_refetch(cfg):
    obs = _obs()
    site = dict(cfg.sites["KWST"])
    a = tag_site(obs, "KWST", site, cfg.regimes)
    site2 = dict(site); site2["flow"] = {"onshore": [0, 359], "offshore": [400, 401]}
    b = tag_site(obs, "KWST", site2, cfg.regimes)
    assert (b["flow"][b["flow"] != "calm"] == "onshore").all()
    assert not a["flow"].equals(b["flow"])


def test_calm_uses_speed_only(cfg):
    obs = _obs()
    obs.loc[obs["var"] == "wdir", "value"] = np.nan   # direction absent, as METAR reports for calm/variable
    r = tag_site(obs, "KWST", cfg.sites["KWST"], cfg.regimes)
    w = obs[obs["var"] == "wspd"].set_index("valid")["value"]
    calm = r.set_index("valid")["flow"] == "calm"
    assert (calm == (w.reindex(calm.index) < cfg.regimes["calm_kt"])).all()


def test_solar_elevation_sanity():
    # Central Park, summer solstice local noon (~16:56Z) elevation ~72.6 deg; local midnight well below -6
    e_noon = solar_elevation(pd.DatetimeIndex([pd.Timestamp("2025-06-21 16:56", tz="UTC")]), 40.7789, -73.9692)[0]
    e_mid = solar_elevation(pd.DatetimeIndex([pd.Timestamp("2025-06-21 05:00", tz="UTC")]), 40.7789, -73.9692)[0]
    assert abs(e_noon - 72.6) < 1.0
    assert e_mid < -20
