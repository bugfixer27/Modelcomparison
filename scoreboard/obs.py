"""Observation ingestion orchestration: fetch -> long -> QC (with context) -> upsert -> manifest."""
from __future__ import annotations
import logging
import pandas as pd
from .config import Config
from .store import Store
from .manifest import Manifest
from . import obs_asos, obs_zentra
from .qc import apply_qc
from .util import to_utc

log = logging.getLogger("scoreboard.obs")


def _qc_and_write(cfg: Config, store: Store, site: str, new: pd.DataFrame) -> int:
    if new.empty:
        return 0
    lo = new["valid"].min() - pd.Timedelta(hours=12)
    hi = new["valid"].max() + pd.Timedelta(hours=12)
    ctx = store.read_obs(site, lo, hi)
    both = pd.concat([ctx, new], ignore_index=True)
    both = both.sort_values("ingested_at").drop_duplicates(["site", "valid", "var"], keep="last")
    qcd = apply_qc(both, cfg.main["qc"])
    store.write_obs(qcd)
    return len(new)


def ingest_asos(cfg: Config, store: Store, man: Manifest, start, end, period_key: tuple | None = None) -> dict:
    """Fetch all ASOS sites in one request for [start, end]. Returns {site: n_rows}. Never raises."""
    start, end = to_utc(start), to_utc(end)
    sites = {sid: s for sid, s in cfg.sites.items() if s.get("obs") == "asos"}
    out = {}
    if not sites:
        return out
    try:
        raw = obs_asos.fetch_raw([s["asos_id"] for s in sites.values()], start, end)
    except Exception as e:
        log.error("ASOS fetch failed %s..%s: %s", start, end, e)
        man.error("asos", f"{start}..{end}: {e}")
        for sid in sites:
            man.obs_mark("asos", sid, start, end, "error", 0, str(e))
        return out
    for sid, s in sites.items():
        try:
            long = obs_asos.to_long(raw, sid, s["asos_id"], float(s.get("elev_m", 0)))
            n = _qc_and_write(cfg, store, sid, long)
            man.obs_mark("asos", sid, start, end, "ok", n)
            out[sid] = n
        except Exception as e:
            log.exception("ASOS %s failed", sid)
            man.error(f"asos/{sid}", str(e))
            man.obs_mark("asos", sid, start, end, "error", 0, str(e))
    return out


def ingest_zentra(cfg: Config, store: Store, man: Manifest, start, end) -> dict:
    start, end = to_utc(start), to_utc(end)
    tok, sn = cfg.env.get("ZENTRA_TOKEN"), cfg.env.get("ZENTRA_DEVICE_SN")
    out = {}
    sites = {sid: s for sid, s in cfg.sites.items() if s.get("obs") == "zentra"}
    if not sites:
        return out
    if not tok or not sn:
        log.warning("ZENTRA_TOKEN / ZENTRA_DEVICE_SN not set; skipping campus obs")
        return out
    for sid, s in sites.items():
        try:
            # pad start by 1 h so the first hour's precip window is complete
            pages = obs_zentra.fetch_raw(tok, sn, start - pd.Timedelta(hours=1), end)
            long = obs_zentra.to_long(pages, sid, float(s.get("elev_m", 0)), cfg.main.get("soilm"))
            long = long[(long["valid"] >= start) & (long["valid"] <= end)] if not long.empty else long
            n = _qc_and_write(cfg, store, sid, long)
            man.obs_mark("zentra", sid, start, end, "ok", n)
            out[sid] = n
        except Exception as e:
            log.exception("Zentra %s failed", sid)
            man.error(f"zentra/{sid}", str(e))
            man.obs_mark("zentra", sid, start, end, "error", 0, str(e))
    return out
