# DECISIONS

Design choices made while building, in the order they came up. Where the build prompt was explicit it was
followed; entries here are the judgment calls, plus the facts discovered along the way that constrain them.

## Environment
- **Python 3.13 venv** (`.venv`), not `uv` — `uv` is not installed and the prompt allows either. `requirements.txt` pins nothing hard; herbie 2026.3.0, pandas 4.x, eccodes 2.48 were what resolved.
- Herbie's optional `cartopy` extra is **not** installed. Nearest-grid-point selection is done in-house (`extract._nearest_ij`): squared degree distance with a cos(lat) scaling for 2-D curvilinear grids, separable nearest lat / nearest lon for regular 1-D grids. Verified against an independent haversine over the raw eccodes lat/lon arrays (`scripts/verify_one_value.py`).

## Sites
- **CAMPUS coordinates are a placeholder** (40.8898, −73.9010, 50 m: Manhattan College Riverdale campus centroid). ZentraCloud v4 exposes no device-location endpoint with this token. Replace `lat/lon/elev_m` in `config/sites.yaml` when known; the elevation also drives the station-pressure → MSLP reduction, so a 30 m error is ~3.5 hPa of MSLP bias at CAMPUS.
- IEM station ids are stored without the leading K (`asos_id: NYC`); the parser accepts either form in the response.
- KNYC and KLGA fall in the **same 0.25° grid box** for GFS / IFS / AIFS (40.75, −74.00). That is a fact about the grids, not a bug; the gridpoint table on the health/meta output makes it visible.
- Grid elevation is recorded from `HGT:surface` for HRRR / NAM / GFS. **IFS and AIFS open data carry no orography**, so `grid_elev` is null for them. No elevation corrections are applied anywhere.

## Units
Storage/display units follow the METAR convention the user forecasts in: °F, hPa, statute miles, inches. Conversions happen once, at ingest, on both sides.
- **Visibility is capped at 10 mi on both sides** (obs report "10SM" as the ceiling; models emit 24 km+). Without the cap every clear hour would be a large fake error.
- Precipitation trace ("T") is ingested as 0.0001 in and therefore counts as **dry** at the 0.005 in wet threshold.

## Observations
- One IEM request per period covers all four ASOS stations (`report_type` 3 and 4). Per hour, one report is kept: **COR beats routine beats special**, then closest to the top of the hour, within ±30 min. `valid` is rounded to the nearest hour (the :51 routine → next hour). The raw METAR string is stored on the `t2m` row only.
- MSLP: reported SLP first; fall back to altimeter reduced with the actual temperature and flag `alt_fallback` (informational, not excluded).
- CAMPUS (ZentraCloud): instantaneous values at :00 from the 10-min stream; `precip1h` is the sum of the six 10-min intervals ending at :00 and is null if any interval is missing. Dewpoint is derived from ATMOS-41 vapor pressure + air temperature (Magnus); MSLP from station pressure via the hypsometric equation with the configured elevation (`sta_fallback` flag). Wind m/s → kt.
- The Zentra API allows **one `get_readings` call per minute**; 429s are honoured via `Retry-After`. Backfill chunks are 13 days (2000 readings/page). Already-stored periods are never refetched (manifest `obs_jobs`).
- QC excluding flags: `range`, `td_gt_t`, `flatline` (≥6 h identical, only on t2m / td2m / mslp — vis at 10 mi and calm wind are legitimately constant), `spike` (hour-over-hour jump > 20 °F or > 8 hPa). Informational flags: `corrected`, `alt_fallback`, `sta_fallback`, `derived`. `td_gt_t` uses a 0.05 °F tolerance; at CAMPUS the derived dewpoint exceeds T by a few tenths at saturation (sensor RH rounding) and those hours are flagged, as the prompt requires.
- Sky cover is stored as a numeric obs var `skycov` (max over layers, CLR/SKC=0 … BKN=3, OVC/VV=4) so regimes can be re-derived from the store without re-downloading. Wind (`wdir`, `wspd`) likewise.

## Models / extraction
- One GRIB subset download per (model, init, fxx) carrying **all** needed fields, rather than the prompt's "t2m/td2m first, then mslp/vis/precip" ordering for Phase B. Each extra field is one more byte range in the same request; a second pass would double the request count. Ordering is by cycle (newest first) instead, so the recent record fills first.
- Variables are matched to cfgrib output by `(GRIB_shortName, typeOfLevel)`, not by position, so a model whose file ordering changes cannot silently swap fields.
- **Precipitation**: NCEP buckets reset (NAM every 3 h, GFS every 6 h). At each lead the shortest bucket ending at that lead is chosen; if it is longer than the model's native window the previous step's bucket (same start) is subtracted, re-fetching it after a restart. IFS/AIFS `tp` is cumulative from init and is differenced per native step. Precip variable ids carry the window: `precip1h` (HRRR, NAM), `precip3h` (GFS, IFS), `precip6h` (AIFS). Obs `precip3h`/`precip6h` are sums of hourly obs and are null if any hour is missing or flagged. Nothing is ever compared across different windows; the Trust Today grid shows them as separate columns.
- AIFS `tp` is in kg m⁻² while IFS `tp` is in m; the converter reads `GRIB_units`.
- HRRR MSLP is `MSLMA` (MAPS reduction); NAM/GFS use `PRMSL`; ECMWF `msl`. Recorded here because the reductions differ by up to ~1 hPa in cold air over terrain; for these coastal sites the effect is small.
- **GFS at 3-h steps** (per prompt) even though the 0.25° files exist hourly to f120. IFS 3-hourly to f144. **AIFS 6-hourly** (its native open-data step).
- Lead bins are `(lo, hi]`; `fxx=0` (GFS/IFS/AIFS analyses) lands in `0–6`.
- **Missing files**: in the hourly run a missing file is retried every hour while the cycle is < 12 h old, then left as `missing` (NOAA/ECMWF archives do not fill in later). Backfill attempts once; `--retry-missing` forces another pass.

## HRRR backfill via HRRR-Zarr (evaluated per §13)
`scripts/eval_hrrr_zarr.py`, 2025-09-04 12z, one variable: **Zarr: all 48 leads in one 150×150 chunk, ~2 MB compressed, 0.8 s. GRIB `.idx`: ~57 MB and ~28 s per 48-lead cycle.** All five sites fall in the same chunk. Values agree with the GRIB path to the float (max diff 0.0 over 20 checks). Decision: **HRRR Phase B backfill uses `extract_zarr.ZarrExtractor`; forward collection stays on Herbie/GRIB** (one production code path). Grid indices for the Zarr path come from the manifest gridpoints the GRIB path wrote, so both paths hit the same point. Zarr is not used for NAM (no archive) and is not used forward (the Zarr archive lags and the hourly run needs the freshest cycle).

## Storage
- Parquet per obs `{site}/{year}` and forecast `{model}/{site}/{year}-{month}`; upsert = read, concat, drop duplicates on natural keys keeping the latest `ingested_at`, atomic replace. A per-file `flock` protects against the hourly run and a backfill writing the same month concurrently.
- Timestamps are normalised to one UTC tz object on every read; pandas 4 treats `zoneinfo.UTC` and `datetime.timezone.utc` as *different* time zones in `date_range`/joins, which bit the health module until normalised.
- The manifest is SQLite (`fcst_jobs`, `obs_jobs`, `gridpoints`, `runs`, `errors`). Granularity is one row per (model, init, fxx) so a killed backfill loses at most a handful of files.
- `store/` is committed while under 100 MB (checked at commit time by `run.py`); above that only `site/data/` is committed.

## Regimes
- Derived from obs only. `flow`: calm decided by **speed alone** (< 3 kt) because METARs omit direction when calm; direction absent with speed ≥ 3 kt (METAR `VRB`) is `other`; both absent is `unknown`. Sectors from `config/sites.yaml`; a sector edit re-tags at the next aggregate with no download (test `test_sector_change_retags_without_refetch`).
- `sky`: `skycov ≤ 2` (≤ SCT) clear, `≥ 3` cloudy, absent unknown. CAMPUS is always `unknown` and the Trust Today ladder treats an unknown dimension as unconstrained from the start, labelled "unknown here: sky".
- `daynight` via a NOAA-style solar position routine (`solar.py`, ±0.1°), thresholds in config; transition kept as its own bucket.

## Metrics / Trust Today
- Cells: `n, bias, MAE, RMSE, p10/p50/p90` for continuous vars; contingency table at 0.01/0.10/0.25 in plus conditional amount error (both wet at the lowest threshold) for precip. Precip ranking: CSI at the lowest threshold, tie-break |frequency bias − 1|.
- **Fair mode**: compared models for a (site, var) are the models with *any* pair for it in the window; a (valid, lead bin) key is kept only if every compared model has a pair there. Both modes are always computed and shipped; the page labels which is showing. `test_fair_mode_changes_ranking` proves the mask changes a ranking on synthetic data; `scripts/acceptance.py` reports real cells where it flips.
- Explorer regime slices are pre-aggregated for any single dimension (incl. month) and any pair of dimensions. A third simultaneous filter is disabled in the UI rather than served — full 5-way crossing would be ~1 M cells.
- Trust Today ladder: full regime → drop sky → drop flow → drop season → day/night only → all. A step is accepted when **≥ 2 models reach n ≥ 30** (or the only model does), so the ranking is never between one adequately sampled model and a guess. The chosen step, the constraint actually applied, and the relaxed dimensions are all in the JSON and on the page.
- Leads for Trust Today: `fxx ≤ 24`, both modes.
- The analysis window for aggregates is 12 months (`--months`), recomputed every run. Recomputing everything is simpler and safer than tracking "affected" aggregates; it takes seconds at this data volume.

## Frontend
- Chart.js 4 from cdnjs is the single CDN library (explorer bias bars, series lines). The heatmap is a plain table.
- Series x-axis is a linear ms axis with a UTC tick formatter, avoiding a second CDN dependency (date adapter).
- Colour: bias sign (warm amber / cool blue), a single red intensity ramp for MAE / CSI, one muted grey for missing or under-sampled. Model identity colours appear only inside charts.

## Scheduling
- launchd `StartInterval=3600` with `RunAtLoad`. launchd coalesces intervals missed during sleep into a single run at wake, which is the desired catch-up. A pid lock file in `scripts/run_hourly.sh` prevents overlapping runs. Install: `scripts/install_launchd.sh`.
- Git: commits whenever `site/data` (or `store/`) changed; pushes only if `GIT_REMOTE` is set in `.env`. No remote was supplied, so pushes are off until it is.

## Soil moisture (not shipped)
The campus logger has **two TEROS 10 water-content probes (ports 3 and 5) and two TEROS 21 matric-potential probes (ports 2 and 4)** — so soil moisture is measurable — but the API does not report **installation depths**, and the two readings (0.029 and 0.079 m³/m³ on 2025-09-04) cannot be assigned to 0–10 cm and 10–40 cm without that. HRRR (0–1/1–4/4–10/10–30 cm) would need a thickness-weighted 0–10 cm mean from its top three layers and 10–30 cm standing in for 10–40; NAM/GFS map 1:1. The obs side is wired (`soilm.enabled`, `soilm.port_depth` in config) and the model side is not built. **Ship without it.** To enable later: fill `port_depth` (e.g. `{3: "0_10", 5: "10_40"}`), set `enabled: true`, and add `soilm` fields to the model registry.

## Deviations from the prompt, stated plainly
- Phase B fetches all variables per file in one pass (see above) instead of two variable-ordered passes.
- HRRR MSLP field is MSLMA rather than PRMSL because the HRRR sfc file carries only MSLMA.
- `report_type` 4 (specials) is requested alongside routine so COR values are seen; only one report per hour is kept.
