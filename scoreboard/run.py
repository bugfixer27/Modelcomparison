"""Hourly entry point: python -m scoreboard.run [--no-git] [--no-fcst] [--no-obs] [--no-aggregate] [--models m1,m2]

Every step is idempotent and gap-tolerant. One failing source never aborts the run."""
from __future__ import annotations
import argparse, logging, subprocess, sys, time
import pandas as pd
from .config import load
from .store import Store
from .manifest import Manifest, fcst_todo
from .util import setup_logging
from . import obs as obsmod
from .extract import Extractor, Stats
from . import aggregate


def git_commit_push(cfg, log, message: str) -> str:
    root = cfg.root
    def git(*a, check=True):
        return subprocess.run(["git", *a], cwd=root, capture_output=True, text=True, check=check)
    if not (root / ".git").exists():
        return "no git repo"
    paths = ["site/data"]
    store_mb = sum(p.stat().st_size for p in cfg.store.rglob("*.parquet")) / 1e6
    if store_mb < 100:
        paths.append(str(cfg.store.relative_to(root)))
    git("add", "-A", *paths)
    st = git("status", "--porcelain", "--", *paths).stdout.strip()
    if not st:
        return "nothing to commit"
    git("commit", "-q", "-m", message)
    if cfg.main["git"].get("auto_push"):
        remotes = git("remote", check=False).stdout.split()
        if "origin" not in remotes and cfg.env.get("GIT_REMOTE"):
            git("remote", "add", "origin", cfg.env["GIT_REMOTE"])
            remotes.append("origin")
        if "origin" in remotes:
            r = git("push", "-q", "-u", "origin", "HEAD", check=False)
            return "committed+pushed" if r.returncode == 0 else f"committed; push failed: {r.stderr.strip()[:200]}"
    return "committed (no remote 'origin' and GIT_REMOTE unset, or auto_push off)"


def fetch_recent_cycles(cfg, store, man, log, models=None, hours=None, stats=None) -> dict:
    """Query the manifest for missing model cycles in the trailing window and fetch them, newest first."""
    now = pd.Timestamp.now("UTC")
    hours = hours or cfg.main["trailing"]["fcst_hours"]
    ex = Extractor(cfg, store, man, stats)
    summary = {}
    for model in (models or list(cfg.models)):
        mc = cfg.main["models"][model]
        fxx_all = cfg.model_fxx(model)
        inits = []
        d = (now - pd.Timedelta(hours=hours)).floor("D")
        while d <= now:
            for c in mc["cycles"]:
                i = d + pd.Timedelta(hours=c)
                if now - pd.Timedelta(hours=hours) <= i <= now - pd.Timedelta(minutes=45):
                    inits.append(i)
            d += pd.Timedelta(days=1)
        for init in sorted(inits, reverse=True):
            age_h = (now - init).total_seconds() / 3600
            # missing files: retry hourly while the cycle is young, then give up (archives don't fill in later)
            todo = fcst_todo(man, model, init, fxx_all, retry_missing_older_than_h=(0.9 if age_h < 12 else None))
            if not todo:
                continue
            try:
                s = ex.extract_cycle(model, init, todo, retry_missing=True)
            except Exception as e:  # extract_cycle is defensive; this is belt and braces
                log.exception("extract %s %s crashed", model, init)
                man.error(f"fcst/{model}", f"{init}: {e}")
                continue
            summary[f"{model}/{init:%Y%m%d%H}"] = s
            log.info("%s %s: %s | %s", model, init.strftime("%Y-%m-%dT%HZ"), s, ex.stats.line())
    return summary


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-git", action="store_true")
    ap.add_argument("--no-fcst", action="store_true")
    ap.add_argument("--no-obs", action="store_true")
    ap.add_argument("--no-aggregate", action="store_true")
    ap.add_argument("--models", default=None)
    ap.add_argument("--months", type=int, default=12, help="analysis window for aggregates")
    a = ap.parse_args(argv)
    cfg = load()
    log = setup_logging(cfg.store / "logs")
    store, man = Store(cfg.store), Manifest(cfg.store / "manifest.sqlite")
    run_id = man.run_start("run")
    t0 = time.time()
    summary = {}
    now = pd.Timestamp.now("UTC").floor("h")

    if not a.no_obs:
        s, e = now - pd.Timedelta(hours=cfg.main["trailing"]["obs_hours"]), now
        try:
            summary["asos"] = obsmod.ingest_asos(cfg, store, man, s, e)
        except Exception as ex:
            log.exception("asos step crashed"); man.error("asos", str(ex))
        try:
            summary["zentra"] = obsmod.ingest_zentra(cfg, store, man, s, e)
        except Exception as ex:
            log.exception("zentra step crashed"); man.error("zentra", str(ex))
        log.info("obs: %s", summary)

    if not a.no_fcst:
        stats = Stats()
        models = a.models.split(",") if a.models else None
        summary["fcst"] = fetch_recent_cycles(cfg, store, man, log, models, stats=stats)
        summary["fcst_stats"] = stats.line()

    if not a.no_aggregate:
        try:
            summary["aggregate"] = aggregate.build_all(cfg, store, man, months=a.months)
            log.info("aggregate: %d pairs, %d files changed", summary["aggregate"]["n_pairs"], len(summary["aggregate"]["changed"]))
        except Exception as ex:
            log.exception("aggregate crashed"); man.error("aggregate", str(ex))

    if not a.no_git and cfg.main["git"].get("auto_commit"):
        try:
            summary["git"] = git_commit_push(cfg, log, f"scoreboard: hourly update {now:%Y-%m-%dT%HZ}")
            log.info("git: %s", summary["git"])
        except Exception as ex:
            log.exception("git step failed"); man.error("git", str(ex))

    summary["elapsed_s"] = round(time.time() - t0, 1)
    man.run_finish(run_id, "ok", summary)
    log.info("run done in %.0fs", summary["elapsed_s"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
