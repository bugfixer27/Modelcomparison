"""Data-coverage accounting. A verification system with silent holes lies; this module makes holes loud."""
from __future__ import annotations
import pandas as pd
import numpy as np
from .config import Config
from .store import Store
from .manifest import Manifest
from .qc import is_good


def _month(ts: pd.Series) -> pd.Series:
    return ts.dt.strftime("%Y-%m")


def _U(t) -> pd.Timestamp:
    return pd.Timestamp(t).tz_convert("UTC")


def build_health(cfg: Config, store: Store, man: Manifest, now: pd.Timestamp | None = None) -> dict:
    now = _U(now or pd.Timestamp.now("UTC"))
    jobs = man.fcst_jobs_df()
    ojobs = man.obs_jobs_df()
    out = {"generated_at": now.strftime("%Y-%m-%dT%H:%MZ"), "models": {}, "obs": {}, "fcst_coverage": [],
           "obs_coverage": [], "pair_coverage": [], "gaps": {"fcst": [], "obs": []}, "errors": [], "runs": [],
           "last_success": {}}

    # ---- forecast file coverage by model x month (expected = days x cycles x n_fxx) ----
    for model in cfg.models:
        j = jobs[jobs["model"] == model] if not jobs.empty else jobs
        okj = j[j["status"] == "ok"] if not j.empty else j
        n_fxx = len(cfg.model_fxx(model))
        cycles = cfg.main["models"][model]["cycles"]
        info = {"first_init": None, "last_init": None, "files_ok": int(len(okj)), "files_missing": int((j["status"] == "missing").sum()) if not j.empty else 0,
                "files_error": int((j["status"] == "error").sum()) if not j.empty else 0}
        if not okj.empty:
            first, last = okj["init"].min(), okj["init"].max()
            info["first_init"], info["last_init"] = first.strftime("%Y-%m-%dT%HZ"), last.strftime("%Y-%m-%dT%HZ")
            out["last_success"][f"fcst/{model}"] = okj["attempted_at"].max().strftime("%Y-%m-%dT%H:%MZ")
            # month coverage: fraction of (cycle, fxx) files ok within [first, min(now-6h, month end)]
            ok_by_init = okj.groupby("init").size()
            months = pd.period_range(first.to_period("M"), min(now, last).to_period("M"), freq="M")
            for pm in months:
                ms = max(first.floor("D"), _U(pm.start_time.tz_localize("UTC")))
                me = min(now - pd.Timedelta(hours=6), _U(pm.end_time.tz_localize("UTC")))
                if me <= ms:
                    continue
                days = pd.date_range(ms.floor("D"), me.floor("D"), freq="D")
                exp_inits = [d + pd.Timedelta(hours=c) for d in days for c in cycles]
                exp_inits = [i for i in exp_inits if ms <= i <= me]
                expected = len(exp_inits) * n_fxx
                got = int(ok_by_init.reindex(exp_inits).fillna(0).sum())
                out["fcst_coverage"].append({"model": model, "month": str(pm), "expected": expected, "ok": got,
                                             "pct": round(100.0 * got / expected, 1) if expected else None})
                # gaps: expected inits with < 50% of files
                for i in exp_inits:
                    g = int(ok_by_init.get(i, 0))
                    if g < 0.5 * n_fxx:
                        out["gaps"]["fcst"].append({"model": model, "init": i.strftime("%Y-%m-%dT%HZ"), "files_ok": g, "expected": n_fxx})
        out["models"][model] = info

    # ---- obs coverage by site x month (hours with QC-good t2m / hours elapsed) ----
    for site in cfg.sites:
        obs = store.read_obs(site, vars=["t2m"])
        info = {"first": None, "last": None, "n_hours": 0, "n_flagged": 0}
        src = cfg.sites[site]["obs"]
        oj = ojobs[(ojobs["site"] == site) & (ojobs["status"] == "ok")] if not ojobs.empty else ojobs
        if not oj.empty:
            out["last_success"][f"obs/{site}"] = oj["attempted_at"].max().strftime("%Y-%m-%dT%H:%MZ")
        if not obs.empty:
            good = obs[is_good(obs["qc_flag"])]
            info.update(first=obs["valid"].min().strftime("%Y-%m-%dT%HZ"), last=obs["valid"].max().strftime("%Y-%m-%dT%HZ"),
                        n_hours=int(len(obs)), n_flagged=int(len(obs) - len(good)))
            by_month = good.groupby(_month(good["valid"])).size()
            first = obs["valid"].min()
            for pm in pd.period_range(first.to_period("M"), now.to_period("M"), freq="M"):
                ms = max(first.floor("h"), _U(pm.start_time.tz_localize("UTC")))
                me = min(now - pd.Timedelta(hours=3), _U(pm.end_time.tz_localize("UTC")))
                if me <= ms:
                    continue
                expected = int((me - ms).total_seconds() // 3600) + 1
                got = int(by_month.get(str(pm), 0))
                out["obs_coverage"].append({"site": site, "month": str(pm), "expected": expected, "ok": got,
                                            "pct": round(100.0 * got / expected, 1) if expected else None})
            # daily gaps: < 12 good hours in a day inside the archive span
            daily = good.groupby(good["valid"].dt.floor("D")).size()
            span = pd.date_range(first.floor("D"), (now - pd.Timedelta(hours=3)).floor("D"), freq="D")
            for d in span:
                g = int(daily.get(d, 0))
                if g < 12:
                    out["gaps"]["obs"].append({"site": site, "day": d.strftime("%Y-%m-%d"), "hours_ok": g})
        out["obs"][site] = info

    # ---- matched-pair coverage model x site x month: rows of stored forecast t2m with an ob at that valid ----
    for model in cfg.models:
        for site in cfg.sites:
            fc = store.read_fcst(model, site, vars=["t2m"])
            if fc.empty:
                continue
            obs = store.read_obs(site, vars=["t2m"])
            good_valid = set(obs[is_good(obs["qc_flag"])]["valid"]) if not obs.empty else set()
            fc["paired"] = fc["valid"].isin(good_valid)
            g = fc.groupby(_month(fc["init"])).agg(fcst=("value", "size"), paired=("paired", "sum"))
            for month, r in g.iterrows():
                out["pair_coverage"].append({"model": model, "site": site, "month": month, "fcst_rows": int(r["fcst"]), "paired": int(r["paired"])})

    # ---- errors / runs ----
    e = man.errors_df(40)
    out["errors"] = e.to_dict("records") if not e.empty else []
    r = man.runs_df(20)
    out["runs"] = r.to_dict("records") if not r.empty else []
    # cap gap lists so the JSON stays small; keep the most recent
    out["gaps"]["fcst"] = out["gaps"]["fcst"][-400:]
    out["gaps"]["obs"] = out["gaps"]["obs"][-400:]
    return out
