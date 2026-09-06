"""SQLite manifest: what has been attempted / succeeded. Drives resumability."""
from __future__ import annotations
import sqlite3, json
from contextlib import contextmanager
from pathlib import Path
from datetime import datetime, timezone
import pandas as pd

SCHEMA = """
CREATE TABLE IF NOT EXISTS fcst_jobs (
  model TEXT NOT NULL, init TEXT NOT NULL, fxx INTEGER NOT NULL,
  status TEXT NOT NULL, attempted_at TEXT NOT NULL, n_rows INTEGER DEFAULT 0, error TEXT,
  PRIMARY KEY (model, init, fxx));
CREATE TABLE IF NOT EXISTS obs_jobs (
  source TEXT NOT NULL, site TEXT NOT NULL, period_start TEXT NOT NULL, period_end TEXT NOT NULL,
  status TEXT NOT NULL, attempted_at TEXT NOT NULL, n_rows INTEGER DEFAULT 0, error TEXT,
  PRIMARY KEY (source, site, period_start, period_end));
CREATE TABLE IF NOT EXISTS gridpoints (
  model TEXT NOT NULL, site TEXT NOT NULL, grid_shape TEXT NOT NULL,
  i INTEGER, j INTEGER, grid_lat REAL, grid_lon REAL, grid_elev REAL, updated_at TEXT,
  PRIMARY KEY (model, site, grid_shape));
CREATE TABLE IF NOT EXISTS runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT, started_at TEXT, finished_at TEXT, status TEXT, summary TEXT);
CREATE TABLE IF NOT EXISTS errors (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, source TEXT, msg TEXT);
CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT);
"""


def _ts(t=None) -> str:
    t = t or datetime.now(timezone.utc)
    return pd.Timestamp(t).tz_convert("UTC").strftime("%Y-%m-%dT%H:%M:%SZ") if getattr(t, "tzinfo", None) else pd.Timestamp(t).strftime("%Y-%m-%dT%H:%M:%SZ")


class Manifest:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as c:
            c.executescript(SCHEMA)

    @contextmanager
    def _conn(self):
        con = sqlite3.connect(self.path, timeout=60)
        try:
            yield con
            con.commit()
        finally:
            con.close()

    # ---- fcst jobs ----
    def fcst_status(self, model: str, init, fxx: int) -> str | None:
        with self._conn() as c:
            r = c.execute("SELECT status FROM fcst_jobs WHERE model=? AND init=? AND fxx=?",
                          (model, _ts(init), int(fxx))).fetchone()
        return r[0] if r else None

    def fcst_done(self, model: str, init) -> set[int]:
        with self._conn() as c:
            rows = c.execute("SELECT fxx FROM fcst_jobs WHERE model=? AND init=? AND status='ok'",
                             (model, _ts(init))).fetchall()
        return {r[0] for r in rows}

    def fcst_mark(self, model: str, init, fxx: int, status: str, n_rows: int = 0, error: str | None = None):
        with self._conn() as c:
            c.execute("INSERT OR REPLACE INTO fcst_jobs VALUES (?,?,?,?,?,?,?)",
                      (model, _ts(init), int(fxx), status, _ts(), int(n_rows), (error or "")[:2000] or None))

    def fcst_mark_many(self, rows: list[tuple]):
        """rows: (model, init, fxx, status, n_rows, error)"""
        with self._conn() as c:
            c.executemany("INSERT OR REPLACE INTO fcst_jobs VALUES (?,?,?,?,?,?,?)",
                          [(m, _ts(i), int(f), s, _ts(), int(n), (e or "")[:2000] or None) for m, i, f, s, n, e in rows])

    def fcst_jobs_df(self) -> pd.DataFrame:
        with self._conn() as c:
            df = pd.read_sql("SELECT * FROM fcst_jobs", c)
        if not df.empty:
            df["init"] = pd.to_datetime(df["init"], utc=True).dt.tz_convert("UTC")
            df["attempted_at"] = pd.to_datetime(df["attempted_at"], utc=True).dt.tz_convert("UTC")
        return df

    def fcst_missing_count(self, model: str, init) -> int:
        with self._conn() as c:
            r = c.execute("SELECT COUNT(*) FROM fcst_jobs WHERE model=? AND init=? AND status='missing'",
                          (model, _ts(init))).fetchone()
        return r[0]

    # ---- obs jobs ----
    def obs_status(self, source: str, site: str, start, end) -> str | None:
        with self._conn() as c:
            r = c.execute("SELECT status FROM obs_jobs WHERE source=? AND site=? AND period_start=? AND period_end=?",
                          (source, site, _ts(start), _ts(end))).fetchone()
        return r[0] if r else None

    def obs_mark(self, source: str, site: str, start, end, status: str, n_rows: int = 0, error: str | None = None):
        with self._conn() as c:
            c.execute("INSERT OR REPLACE INTO obs_jobs VALUES (?,?,?,?,?,?,?,?)",
                      (source, site, _ts(start), _ts(end), status, _ts(), int(n_rows), (error or "")[:2000] or None))

    def obs_jobs_df(self) -> pd.DataFrame:
        with self._conn() as c:
            df = pd.read_sql("SELECT * FROM obs_jobs", c)
        for col in ("period_start", "period_end", "attempted_at"):
            if not df.empty:
                df[col] = pd.to_datetime(df[col], utc=True).dt.tz_convert("UTC")
        return df

    # ---- gridpoints ----
    def gridpoint(self, model: str, site: str, grid_shape: str):
        with self._conn() as c:
            r = c.execute("SELECT i,j,grid_lat,grid_lon,grid_elev FROM gridpoints WHERE model=? AND site=? AND grid_shape=?",
                          (model, site, grid_shape)).fetchone()
        return r

    def gridpoint_set(self, model: str, site: str, grid_shape: str, i: int, j: int, lat: float, lon: float, elev):
        with self._conn() as c:
            c.execute("INSERT OR REPLACE INTO gridpoints VALUES (?,?,?,?,?,?,?,?,?)",
                      (model, site, grid_shape, int(i), int(j), float(lat), float(lon),
                       None if elev is None or elev != elev else float(elev), _ts()))

    def gridpoints_df(self) -> pd.DataFrame:
        with self._conn() as c:
            return pd.read_sql("SELECT * FROM gridpoints", c)

    # ---- runs / errors / kv ----
    def run_start(self, kind: str) -> int:
        with self._conn() as c:
            cur = c.execute("INSERT INTO runs (kind, started_at, status) VALUES (?,?,?)", (kind, _ts(), "running"))
            return cur.lastrowid

    def run_finish(self, run_id: int, status: str, summary: dict):
        with self._conn() as c:
            c.execute("UPDATE runs SET finished_at=?, status=?, summary=? WHERE id=?",
                      (_ts(), status, json.dumps(summary, default=str), run_id))

    def runs_df(self, limit: int = 50) -> pd.DataFrame:
        with self._conn() as c:
            return pd.read_sql(f"SELECT * FROM runs ORDER BY id DESC LIMIT {int(limit)}", c)

    def error(self, source: str, msg: str):
        with self._conn() as c:
            c.execute("INSERT INTO errors (ts, source, msg) VALUES (?,?,?)", (_ts(), source, str(msg)[:2000]))

    def errors_df(self, limit: int = 100) -> pd.DataFrame:
        with self._conn() as c:
            return pd.read_sql(f"SELECT * FROM errors ORDER BY id DESC LIMIT {int(limit)}", c)

    def kv_get(self, k: str, default=None):
        with self._conn() as c:
            r = c.execute("SELECT v FROM kv WHERE k=?", (k,)).fetchone()
        return json.loads(r[0]) if r else default

    def kv_set(self, k: str, v):
        with self._conn() as c:
            c.execute("INSERT OR REPLACE INTO kv VALUES (?,?)", (k, json.dumps(v, default=str)))


def fcst_todo(man: "Manifest", model: str, init, fxx_all: list[int], retry_missing_older_than_h: float | None = 2.0,
              retry_errors: bool = True) -> list[int]:
    """fxx values still worth attempting for a cycle: never attempted, errored, or 'missing' but last tried
    longer than `retry_missing_older_than_h` ago (None = never retry missing)."""
    with man._conn() as c:
        rows = c.execute("SELECT fxx, status, attempted_at FROM fcst_jobs WHERE model=? AND init=?",
                         (model, _ts(init))).fetchall()
    st = {r[0]: (r[1], pd.Timestamp(r[2], tz="UTC")) for r in rows}
    now = datetime.now(timezone.utc)
    out = []
    for f in fxx_all:
        if f not in st:
            out.append(f); continue
        s, at = st[f]
        if s == "ok":
            continue
        if s == "error" and retry_errors:
            out.append(f); continue
        if s == "missing" and retry_missing_older_than_h is not None and (pd.Timestamp(now) - at).total_seconds() / 3600 >= retry_missing_older_than_h:
            out.append(f)
    return out
