"""Parquet store. Long-format rows; idempotent upsert keyed on natural keys.

obs/{site}/{year}.parquet           keys: site, valid, var
fcst/{model}/{site}/{year}-{month}.parquet   keys: model, init, fxx, site, var
"""
from __future__ import annotations
import fcntl
from contextlib import contextmanager
from pathlib import Path
import pandas as pd
import numpy as np
from .util import to_utc

OBS_COLS = ["site", "valid", "var", "value", "qc_flag", "raw", "ingested_at"]
OBS_KEYS = ["site", "valid", "var"]
FCST_COLS = ["model", "init", "fxx", "valid", "site", "var", "value",
             "grid_lat", "grid_lon", "grid_elev", "ingested_at"]
FCST_KEYS = ["model", "init", "fxx", "site", "var"]


def _utc(df: pd.DataFrame) -> pd.DataFrame:
    for c in ("valid", "init", "ingested_at"):
        if c in df.columns and len(df):
            df[c] = pd.to_datetime(df[c], utc=True).dt.tz_convert("UTC")
    return df


def _empty(cols: list[str]) -> pd.DataFrame:
    df = pd.DataFrame({c: pd.Series(dtype="object") for c in cols})
    for c in ("valid", "init", "ingested_at"):
        if c in cols:
            df[c] = pd.Series(dtype="datetime64[ns, UTC]")
    for c in ("value", "grid_lat", "grid_lon", "grid_elev"):
        if c in cols:
            df[c] = pd.Series(dtype="float64")
    if "fxx" in cols:
        df["fxx"] = pd.Series(dtype="int64")
    return df


def _norm_obs(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df["valid"] = pd.to_datetime(df["valid"], utc=True)
    df["ingested_at"] = pd.to_datetime(df["ingested_at"], utc=True)
    df["value"] = pd.to_numeric(df["value"], errors="coerce").astype("float64")
    df["qc_flag"] = df["qc_flag"].fillna("").astype(str)
    df["raw"] = df["raw"].astype(object).where(df["raw"].notna(), None)
    return df[OBS_COLS]


def _norm_fcst(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    for c in ("init", "valid", "ingested_at"):
        df[c] = pd.to_datetime(df[c], utc=True)
    df["fxx"] = df["fxx"].astype("int64")
    for c in ("value", "grid_lat", "grid_lon", "grid_elev"):
        df[c] = pd.to_numeric(df[c], errors="coerce").astype("float64")
    return df[FCST_COLS]


@contextmanager
def _locked(path: Path):
    """Advisory lock per parquet file so a backfill and the hourly run can't lose each other's rows."""
    lock = path.with_suffix(".lock")
    with open(lock, "w") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


def _upsert(path: Path, new: pd.DataFrame, keys: list[str], cols: list[str]) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    with _locked(path):
        return _upsert_unlocked(path, new, keys, cols)


def _upsert_unlocked(path: Path, new: pd.DataFrame, keys: list[str], cols: list[str]) -> int:
    if path.exists():
        old = _utc(pd.read_parquet(path))
        both = pd.concat([old, new], ignore_index=True)
    else:
        both = new
    both = both.sort_values("ingested_at").drop_duplicates(keys, keep="last")
    sort_keys = [k for k in ("site", "model", "init", "fxx", "valid", "var") if k in cols]
    both = both.sort_values(sort_keys).reset_index(drop=True)
    tmp = path.with_suffix(".tmp.parquet")
    both[cols].to_parquet(tmp, index=False)
    tmp.replace(path)
    return len(both)


class Store:
    def __init__(self, root: Path):
        self.root = Path(root)

    # ---------------- obs ----------------
    def obs_path(self, site: str, year: int) -> Path:
        return self.root / "obs" / site / f"{year}.parquet"

    def write_obs(self, df: pd.DataFrame) -> None:
        if df.empty:
            return
        df = _norm_obs(df)
        for (site, year), g in df.groupby([df["site"], df["valid"].dt.year]):
            _upsert(self.obs_path(site, int(year)), g, OBS_KEYS, OBS_COLS)

    def read_obs(self, site: str, start=None, end=None, vars: list[str] | None = None) -> pd.DataFrame:
        d = self.root / "obs" / site
        if not d.exists():
            return _empty(OBS_COLS)
        years = sorted(int(p.stem) for p in d.glob("*.parquet") if p.stem.isdigit())
        if start is not None:
            years = [y for y in years if y >= pd.Timestamp(start).year]
        if end is not None:
            years = [y for y in years if y <= pd.Timestamp(end).year]
        parts = [pd.read_parquet(self.obs_path(site, y)) for y in years]
        if not parts:
            return _empty(OBS_COLS)
        df = _utc(pd.concat(parts, ignore_index=True))
        if start is not None:
            df = df[df["valid"] >= to_utc(start)]
        if end is not None:
            df = df[df["valid"] <= to_utc(end)]
        if vars:
            df = df[df["var"].isin(vars)]
        return df.reset_index(drop=True)

    # ---------------- fcst ----------------
    def fcst_path(self, model: str, site: str, year: int, month: int) -> Path:
        return self.root / "fcst" / model / site / f"{year}-{month:02d}.parquet"

    def write_fcst(self, df: pd.DataFrame) -> None:
        if df.empty:
            return
        df = _norm_fcst(df)
        for (model, site, y, m), g in df.groupby([df["model"], df["site"], df["init"].dt.year, df["init"].dt.month]):
            _upsert(self.fcst_path(model, site, int(y), int(m)), g, FCST_KEYS, FCST_COLS)

    def read_fcst(self, model: str, site: str, start=None, end=None, vars: list[str] | None = None) -> pd.DataFrame:
        """start/end filter on init month files, then on valid time."""
        d = self.root / "fcst" / model / site
        if not d.exists():
            return _empty(FCST_COLS)
        files = sorted(d.glob("*.parquet"))
        if start is not None:
            s = pd.Timestamp(start) - pd.Timedelta(days=7)
            files = [f for f in files if f.stem >= f"{s.year}-{s.month:02d}"]
        if end is not None:
            e = pd.Timestamp(end)
            files = [f for f in files if f.stem <= f"{e.year}-{e.month:02d}"]
        parts = [pd.read_parquet(f) for f in files]
        if not parts:
            return _empty(FCST_COLS)
        df = _utc(pd.concat(parts, ignore_index=True))
        if start is not None:
            df = df[df["valid"] >= to_utc(start)]
        if end is not None:
            df = df[df["valid"] <= to_utc(end)]
        if vars:
            df = df[df["var"].isin(vars)]
        return df.reset_index(drop=True)

    def fcst_files(self) -> list[tuple[str, str, Path]]:
        out = []
        for p in (self.root / "fcst").glob("*/*/*.parquet"):
            out.append((p.parent.parent.name, p.parent.name, p))
        return out

    def obs_files(self) -> list[tuple[str, Path]]:
        return [(p.parent.name, p) for p in (self.root / "obs").glob("*/*.parquet")]
