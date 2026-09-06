"""Thermodynamic and pressure derivations, vectorized over pandas/numpy."""
from __future__ import annotations
import numpy as np


def rh_from_t_td_f(t_f, td_f):
    """RH (%) from T and Td in degF using Magnus (over water)."""
    t = (np.asarray(t_f, dtype=float) - 32.0) * 5.0 / 9.0
    td = (np.asarray(td_f, dtype=float) - 32.0) * 5.0 / 9.0
    a, b = 17.625, 243.04
    rh = 100.0 * np.exp(a * td / (b + td) - a * t / (b + t))
    return np.clip(rh, 0.0, 100.0)


def td_from_t_c_vp_kpa(t_c, e_kpa):
    """Dewpoint (degC) from vapor pressure (kPa), Magnus inversion."""
    e = np.asarray(e_kpa, dtype=float)
    a, b = 17.625, 243.04
    ln = np.log(np.maximum(e, 1e-6) / 0.61094)
    return b * ln / (a - ln)


def mslp_from_station_hpa(p_sta_hpa, elev_m, t_c):
    """Reduce station pressure to MSL with the hypsometric equation (standard lapse rate)."""
    p = np.asarray(p_sta_hpa, dtype=float)
    t = np.asarray(t_c, dtype=float) + 273.15
    g, R, L = 9.80665, 287.05, 0.0065
    return p * (1.0 - L * elev_m / (t + L * elev_m)) ** (-g / (R * L))


def mslp_from_altimeter_hpa(alt_inhg, elev_m, t_c):
    """Altimeter setting (inHg) -> station pressure -> MSLP with actual temperature."""
    alt = np.asarray(alt_inhg, dtype=float) * 33.8639
    p_sta = alt * ((288.15 - 0.0065 * elev_m) / 288.15) ** 5.2559
    return mslp_from_station_hpa(p_sta, elev_m, t_c)
