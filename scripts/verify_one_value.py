"""Acceptance check 1: hand-verify one stored HRRR value against an independent low-level eccodes decode.

Independent path: download the single TMP:2m message with Herbie, decode with the raw eccodes C API
(no cfgrib, no xarray), find the nearest grid point by brute-force great-circle distance, compare.
Usage: python scripts/verify_one_value.py [model] [init 'YYYY-MM-DD HH'] [fxx] [site]
"""
import sys, os, math
import numpy as np, pandas as pd
import eccodes
from herbie import Herbie
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from scoreboard.config import load
from scoreboard.store import Store

model = sys.argv[1] if len(sys.argv) > 1 else "hrrr"
init = pd.Timestamp(sys.argv[2] if len(sys.argv) > 2 else "2025-09-04 12:00")
fxx = int(sys.argv[3]) if len(sys.argv) > 3 else 6
site = sys.argv[4] if len(sys.argv) > 4 else "KNYC"
cfg = load()
mc = cfg.main["models"][model]
H = Herbie(init.to_pydatetime(), model=mc["herbie"]["model"], product=mc["herbie"]["product"], fxx=fxx, save_dir=str(cfg.herbie_cache), verbose=False)
path = H.download(mc["fields"]["t2m"], verbose=False)
slat, slon = cfg.sites[site]["lat"], cfg.sites[site]["lon"]
with open(path, "rb") as f:
    gid = eccodes.codes_grib_new_from_file(f)
    assert gid is not None
    short = eccodes.codes_get(gid, "shortName")
    lats = eccodes.codes_get_array(gid, "latitudes")
    lons = eccodes.codes_get_array(gid, "longitudes")
    vals = eccodes.codes_get_array(gid, "values")
    eccodes.codes_release(gid)
os.remove(path)
lons = np.where(lons > 180, lons - 360, lons)
# brute-force haversine
phi1, phi2 = math.radians(slat), np.radians(lats)
dphi, dl = np.radians(lats - slat), np.radians(lons - slon)
a = np.sin(dphi / 2) ** 2 + math.cos(phi1) * np.cos(phi2) * np.sin(dl / 2) ** 2
k = int(np.argmin(a))
val_f = (vals[k] - 273.15) * 9 / 5 + 32
print(f"independent eccodes decode: shortName={short} nearest=({lats[k]:.4f},{lons[k]:.4f}) dist={2*6371*math.asin(math.sqrt(a[k])):.2f} km  t2m={val_f:.3f} F")
st = Store(cfg.store)
df = st.read_fcst(model, site)
row = df[(df["init"] == init.tz_localize("UTC")) & (df["fxx"] == fxx) & (df["var"] == "t2m")]
if row.empty:
    print("no stored value for this init/fxx/site — run extraction first"); sys.exit(2)
sv = float(row["value"].iloc[0])
print(f"stored pipeline value:      grid=({row['grid_lat'].iloc[0]:.4f},{row['grid_lon'].iloc[0]:.4f}) t2m={sv:.3f} F")
ok = abs(sv - val_f) < 0.01 and abs(row['grid_lat'].iloc[0] - lats[k]) < 1e-3 and abs(row['grid_lon'].iloc[0] - lons[k]) < 1e-3
print("MATCH" if ok else "MISMATCH"); sys.exit(0 if ok else 1)
