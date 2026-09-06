# Point Forecast Verification Scoreboard

Answers, before you forecast: **for this site and this variable, right now, which model should I trust, and how wrong is it likely to be?**

Sites: KNYC, KLGA, KEWR, KWST (ASOS via IEM) and a Riverdale campus ZentraCloud logger. Models: HRRR, NAM-3km, GFS, IFS, AIFS via Herbie. Variables: 2 m T / Td / RH, MSLP, visibility, precipitation (as events). Everything is $0, runs on a laptop that sleeps, and publishes a static GitHub Pages site.

## Layout
```
config/scoreboard.yaml   models, variables, units, lead bins, QC, thresholds, paths
config/sites.yaml        sites (+ flow sectors), regime thresholds     <- add a site here, nothing else
.env                     ZENTRA_TOKEN, ZENTRA_DEVICE_SN, GIT_REMOTE
scoreboard/              python package (see below)
store/                   obs/{site}/{year}.parquet, fcst/{model}/{site}/{y}-{m}.parquet, manifest.sqlite, logs/
docs/                    static pages (GitHub Pages source: main, /docs) + docs/data/*.json (pre-aggregated, < 1 MB each)
scripts/                 verify_one_value.py, acceptance.py, eval_hrrr_zarr.py, run_hourly.sh, install_launchd.sh
launchd/                 com.scoreboard.run.plist template
tests/                   pytest suite (native-step guard, regimes, QC, metrics, precip buckets, health gap)
DECISIONS.md             every judgment call and discovered constraint
```

## Setup
```bash
python3.13 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env   # fill in ZENTRA_TOKEN / ZENTRA_DEVICE_SN / GIT_REMOTE
.venv/bin/python -m pytest -q tests
```

## Run
```bash
.venv/bin/python -m scoreboard.run                 # hourly job: obs 72 h, missing cycles 96 h, aggregates, JSON, git
.venv/bin/python -m scoreboard.backfill --phase A  # obs history (ASOS 2019->, Zentra 2 y)
.venv/bin/python -m scoreboard.backfill --phase B  # HRRR (via HRRR-Zarr) + NAM-3km, 00/12z, 12 months
.venv/bin/python -m scoreboard.backfill --phase C  # GFS 12 months, IFS from 2024-02-01, AIFS from 2024-06-01
scripts/install_launchd.sh                         # hourly launchd agent (catches up after sleep)
python3 -m http.server 8791 --directory docs       # local preview of the pages
```
Backfills are manifest-driven; kill and restart them freely. `--models`, `--start`, `--end`, `--retry-missing`, `--hrrr-source grib` are available.

## Package
| module | role |
|---|---|
| `config` | yaml + .env loader; model registry helpers (`model_fxx`, `model_vars`, `precip_var`) |
| `store` | parquet upsert / read, per-file lock |
| `manifest` | sqlite job ledger (`fcst_jobs`, `obs_jobs`, `gridpoints`, `runs`, `errors`) |
| `obs_asos`, `obs_zentra`, `obs` | fetch → hourly long rows → QC with context → upsert |
| `qc`, `derive`, `solar`, `regimes` | flags, thermodynamics, solar elevation, obs-only regime tags |
| `extract` | Herbie GRIB subset → nearest grid point → rows; precip bucket/cumulative differencing |
| `extract_zarr` | HRRR-Zarr backfill path (backfill only) |
| `metrics` | matched pairs, lead bins, cells, contingency, fair-comparison mask |
| `aggregate` | Trust Today ladder, explorer slices, series, meta → `docs/data/*.json` |
| `health` | coverage matrices, gaps, last-success, errors |
| `run`, `backfill` | entry points |

## Acceptance
```bash
.venv/bin/python scripts/verify_one_value.py hrrr "2025-09-04 12:00" 6 KNYC   # independent eccodes decode == stored value
.venv/bin/python -m pytest -q tests                                          # native steps, regimes, QC, fair mode, 2-day gap
.venv/bin/python scripts/acceptance.py                                       # real-data fair-mode flips; NAM cool bias at KNYC
```

## Reading the numbers
Bias is **forecast − observation** (negative = model too cool / too low). Cells with n < 30 are greyed, never hidden. Every Trust Today number states the regime it is conditioned on and which constraints were relaxed to reach n ≥ 30. "fair" mode ranks models only on valid times all compared models forecast; "all data" scores each on everything it has. IFS/AIFS carry no visibility; GFS/IFS/AIFS precip is scored over 3 h (AIFS 6 h) windows, HRRR/NAM over 1 h — different columns, never mixed.
