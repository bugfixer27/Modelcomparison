"""Observation QC. Flags are stored with the obs and excluded from metrics, never deleted.

Excluding flags:  range, td_gt_t, flatline, spike
Informational:    alt_fallback (MSLP derived from altimeter), corrected (METAR COR), sta_fallback (MSLP from station pressure)
"""
from __future__ import annotations
import pandas as pd
import numpy as np

EXCLUDE_FLAGS = {"range", "td_gt_t", "flatline", "spike"}
INFO_FLAGS = {"alt_fallback", "corrected", "sta_fallback", "derived"}


def split_flags(s: str) -> set[str]:
    return {f for f in (s or "").split(",") if f}


def join_flags(fs: set[str]) -> str:
    return ",".join(sorted(fs))


def is_good(flag_series: pd.Series) -> pd.Series:
    return ~flag_series.fillna("").apply(lambda s: bool(split_flags(s) & EXCLUDE_FLAGS))


def apply_qc(obs: pd.DataFrame, qc_cfg: dict) -> pd.DataFrame:
    """obs: long df (site, valid, var, value, qc_flag, raw, ingested_at) for ONE site, any span.
    Returns a copy with qc_flag updated (existing informational flags preserved, quality flags recomputed)."""
    if obs.empty:
        return obs
    df = obs.copy()
    df["valid"] = pd.to_datetime(df["valid"], utc=True)
    df = df.sort_values(["var", "valid"]).reset_index(drop=True)
    # keep informational flags, drop quality flags (they are recomputed)
    base = df["qc_flag"].fillna("").apply(lambda s: split_flags(s) - EXCLUDE_FLAGS)
    flags = [set(b) for b in base]

    ranges = qc_cfg.get("range", {})
    spikes = qc_cfg.get("spike", {})
    flat_n = int(qc_cfg.get("flatline_hours", 6))
    flat_vars = set(qc_cfg.get("flatline_vars", []))

    val = df["value"].to_numpy(dtype=float)
    var = df["var"].to_numpy()
    # range
    for v, (lo, hi) in ranges.items():
        m = (var == v) & np.isfinite(val) & ((val < lo) | (val > hi))
        for i in np.flatnonzero(m):
            flags[i].add("range")

    # per-var sequential checks
    for v, idx in df.groupby("var").indices.items():
        idx = np.asarray(idx)
        t = df["valid"].to_numpy()[idx]
        x = val[idx]
        # spike: hour-over-hour jump (only when consecutive samples are ~1 h apart)
        if v in spikes and len(idx) > 1:
            dt_h = np.diff(t).astype("timedelta64[m]").astype(float) / 60.0
            dx = np.abs(np.diff(x))
            bad = (dt_h <= 1.5) & (dx > spikes[v])
            for k in np.flatnonzero(bad):
                # flag the later point (the jump); if next point jumps back, the middle is the spike
                flags[idx[k + 1]].add("spike")
        # flatline
        if v in flat_vars and len(idx) >= flat_n:
            run_start = 0
            for k in range(1, len(idx) + 1):
                if k == len(idx) or not (np.isfinite(x[k]) and x[k] == x[run_start]):
                    if k - run_start >= flat_n:
                        for j in range(run_start, k):
                            flags[idx[j]].add("flatline")
                    run_start = k

    # dewpoint > temperature -> flag both
    piv = df.pivot_table(index="valid", columns="var", values="value", aggfunc="first")
    if "t2m" in piv and "td2m" in piv:
        bad_times = piv.index[(piv["td2m"] > piv["t2m"] + 0.05)]
        if len(bad_times):
            m = df["valid"].isin(bad_times) & df["var"].isin(["t2m", "td2m", "rh2m"])
            for i in np.flatnonzero(m.to_numpy()):
                flags[i].add("td_gt_t")

    df["qc_flag"] = [join_flags(f) for f in flags]
    return df
