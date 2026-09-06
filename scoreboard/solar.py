"""Solar elevation angle (degrees), NOAA-style algorithm, vectorized. Accuracy ~0.1 deg, adequate for regime bins."""
from __future__ import annotations
import numpy as np
import pandas as pd


def solar_elevation(valid_utc: pd.Series | pd.DatetimeIndex, lat: float, lon: float) -> np.ndarray:
    t = pd.DatetimeIndex(pd.to_datetime(valid_utc, utc=True))
    # Julian day
    jd = t.to_julian_date().to_numpy()
    jc = (jd - 2451545.0) / 36525.0
    gmls = (280.46646 + jc * (36000.76983 + jc * 0.0003032)) % 360.0
    gmas = 357.52911 + jc * (35999.05029 - 0.0001537 * jc)
    eeo = 0.016708634 - jc * (0.000042037 + 0.0000001267 * jc)
    seqc = (np.sin(np.radians(gmas)) * (1.914602 - jc * (0.004817 + 0.000014 * jc))
            + np.sin(np.radians(2 * gmas)) * (0.019993 - 0.000101 * jc)
            + np.sin(np.radians(3 * gmas)) * 0.000289)
    stl = gmls + seqc
    sal = stl - 0.00569 - 0.00478 * np.sin(np.radians(125.04 - 1934.136 * jc))
    moe = 23.0 + (26.0 + (21.448 - jc * (46.815 + jc * (0.00059 - jc * 0.001813))) / 60.0) / 60.0
    oc = moe + 0.00256 * np.cos(np.radians(125.04 - 1934.136 * jc))
    decl = np.degrees(np.arcsin(np.sin(np.radians(oc)) * np.sin(np.radians(sal))))
    vy = np.tan(np.radians(oc / 2.0)) ** 2
    eqt = 4.0 * np.degrees(vy * np.sin(2 * np.radians(gmls)) - 2 * eeo * np.sin(np.radians(gmas))
                           + 4 * eeo * vy * np.sin(np.radians(gmas)) * np.cos(2 * np.radians(gmls))
                           - 0.5 * vy * vy * np.sin(4 * np.radians(gmls))
                           - 1.25 * eeo * eeo * np.sin(2 * np.radians(gmas)))
    minutes = (t.hour * 60 + t.minute + t.second / 60.0).to_numpy(dtype=float)
    tst = (minutes + eqt + 4.0 * lon) % 1440.0
    ha = np.where(tst / 4.0 < 0, tst / 4.0 + 180.0, tst / 4.0 - 180.0)
    latr, declr = np.radians(lat), np.radians(decl)
    cosz = np.sin(latr) * np.sin(declr) + np.cos(latr) * np.cos(declr) * np.cos(np.radians(ha))
    zen = np.degrees(np.arccos(np.clip(cosz, -1, 1)))
    return 90.0 - zen
