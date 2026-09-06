"""Phased, resumable backfill: python -m scoreboard.backfill --phase A|B|C|all [--models ...] [--start YYYY-MM-DD] [--end ...]

Phase A: ASOS obs 2019->present (chunked by year) + ZentraCloud history (~2 years, 13-day chunks).
Phase B: HRRR + NAM3km, backfill_cycles (00/12z), full fxx range, trailing 12 months.
Phase C: GFS trailing 12 months; IFS from 2024-02-01; AIFS from 2024-06-01.
Every unit of work is checked against the manifest first, so this can be killed and restarted at any time."""
from __future__ import annotations
import argparse, logging, sys, time
import pandas as pd
from .config import load
from .store import Store
from .manifest import Manifest, fcst_todo
from .util import setup_logging
from . import obs as obsmod
from .extract import Extractor, Stats


def phase_a(cfg, store, man, log, start_year=2019, end=None):
    now = end or pd.Timestamp.now("UTC").floor("h")
    # ASOS: one request per year for all stations (4 station-years, far below the ~1000 cap)
    for y in range(start_year, now.year + 1):
        s = pd.Timestamp(f"{y}-01-01", tz="UTC")
        e = min(pd.Timestamp(f"{y+1}-01-01", tz="UTC"), now)
        sites = [sid for sid, sc in cfg.sites.items() if sc.get("obs") == "asos"]
        if all(man.obs_status("asos", sid, s, e) == "ok" for sid in sites) and e < now:
            log.info("ASOS %d already done", y); continue
        log.info("ASOS %d ...", y)
        r = obsmod.ingest_asos(cfg, store, man, s, e)
        log.info("ASOS %d: %s", y, r)
        time.sleep(2)
    # Zentra: 13-day chunks, oldest first, from 2 years back
    if any(sc.get("obs") == "zentra" for sc in cfg.sites.values()):
        s = (now - pd.DateOffset(years=2)).floor("D")
        while s < now:
            e = min(s + pd.Timedelta(days=13), now)
            zsites = [sid for sid, sc in cfg.sites.items() if sc.get("obs") == "zentra"]
            if all(man.obs_status("zentra", sid, s, e) == "ok" for sid in zsites) and e < now:
                s = e; continue
            log.info("Zentra %s..%s ...", s.date(), e.date())
            r = obsmod.ingest_zentra(cfg, store, man, s, e)
            log.info("Zentra %s: %s", s.date(), r)
            s = e


def model_inits(cfg, model, start, end):
    mc = cfg.main["models"][model]
    cycles = mc.get("backfill_cycles", mc["cycles"])
    d = start.floor("D")
    out = []
    while d <= end:
        for c in cycles:
            i = d + pd.Timedelta(hours=c)
            if start <= i <= end:
                out.append(i)
        d += pd.Timedelta(days=1)
    return out


def phase_models(cfg, store, man, log, models, start=None, end=None, retry_missing=False, hrrr_source="zarr"):
    now = pd.Timestamp.now("UTC")
    end = end or now - pd.Timedelta(hours=6)
    stats = Stats()
    ex = Extractor(cfg, store, man, stats)
    zx = None
    if "hrrr" in models and hrrr_source == "zarr":
        from .extract_zarr import ZarrExtractor
        try:
            zx = ZarrExtractor(cfg, store, man, stats)
        except Exception as e:
            log.warning("zarr extractor unavailable (%s); HRRR backfill falls back to GRIB", e)
    for model in models:
        mc = cfg.main["models"][model]
        bs = mc.get("backfill_start")
        s = pd.Timestamp(bs, tz="UTC") if bs else (now - pd.DateOffset(months=12))
        if start is not None:
            s = max(s, start)
        inits = model_inits(cfg, model, s, end)
        fxx_all = cfg.model_fxx(model)
        n_cycles = len(inits)
        log.info("%s: %d cycles %s..%s, %d files each", model, n_cycles, s.date(), end.date(), len(fxx_all))
        done_cycles = 0
        t0 = time.time()
        for k, init in enumerate(reversed(inits)):   # newest first: the recent record is the most useful
            todo = fcst_todo(man, model, init, fxx_all, retry_missing_older_than_h=(0.0 if retry_missing else None))
            if todo:
                if model == "hrrr" and zx is not None:
                    r = zx.extract_cycle(init, todo)
                    if r["missing"] and retry_missing:          # zarr day absent: try the GRIB archive
                        r = ex.extract_cycle(model, init, todo, retry_missing=True)
                else:
                    r = ex.extract_cycle(model, init, todo, retry_missing=retry_missing)
                done_cycles += 1
                if done_cycles % 5 == 0 or r.get("error"):
                    log.info("%s %s %s | %s", model, init.strftime("%Y-%m-%dT%HZ"), r, stats.line(todo=n_cycles, done=k + 1))
            elif k % 50 == 0:
                log.info("%s %s already done (%d/%d)", model, init.strftime("%Y-%m-%dT%HZ"), k + 1, n_cycles)
        log.info("%s finished in %.1f min | %s", model, (time.time() - t0) / 60, stats.line())


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--phase", default="all", choices=["A", "B", "C", "all"])
    ap.add_argument("--models", default=None, help="override model list for B/C")
    ap.add_argument("--start", default=None)
    ap.add_argument("--end", default=None)
    ap.add_argument("--retry-missing", action="store_true")
    ap.add_argument("--asos-start-year", type=int, default=2019)
    ap.add_argument("--hrrr-source", default="zarr", choices=["zarr", "grib"], help="HRRR backfill path (forward collection is always GRIB)")
    a = ap.parse_args(argv)
    cfg = load()
    log = setup_logging(cfg.store / "logs", "scoreboard.backfill")
    store, man = Store(cfg.store), Manifest(cfg.store / "manifest.sqlite")
    run_id = man.run_start(f"backfill-{a.phase}")
    start = pd.Timestamp(a.start, tz="UTC") if a.start else None
    end = pd.Timestamp(a.end, tz="UTC") if a.end else None
    try:
        if a.phase in ("A", "all"):
            phase_a(cfg, store, man, log, a.asos_start_year, end)
        if a.phase in ("B", "all"):
            models = a.models.split(",") if a.models else [m for m in ("hrrr", "nam3km") if m in cfg.models]
            phase_models(cfg, store, man, log, models, start, end, a.retry_missing, a.hrrr_source)
        if a.phase in ("C", "all"):
            models = a.models.split(",") if a.models else [m for m in ("gfs", "ifs", "aifs") if m in cfg.models]
            phase_models(cfg, store, man, log, models, start, end, a.retry_missing)
        man.run_finish(run_id, "ok", {"phase": a.phase})
    except KeyboardInterrupt:
        man.run_finish(run_id, "interrupted", {"phase": a.phase})
        log.warning("interrupted; manifest is consistent, rerun to resume")
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())
