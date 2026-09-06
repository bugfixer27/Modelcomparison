"""Evaluate whether the HRRR-Zarr archive (s3://hrrrzarr) is cheaper than GRIB .idx range requests for
point time-series extraction (one grid point, all leads of one cycle, one variable).

Prints bytes and seconds for each path so the decision in DECISIONS.md is reproducible."""
import sys, time, os, warnings
warnings.filterwarnings("ignore")
import numpy as np

def zarr_path():
    import s3fs, zarr
    fs = s3fs.S3FileSystem(anon=True)
    day, cyc = "20250904", "12"
    base = f"hrrrzarr/sfc/{day}/{day}_{cyc}z_fcst.zarr/2m_above_ground/TMP"
    t0 = time.time()
    try:
        store = s3fs.S3Map(root=base, s3=fs, check=False)
        g = zarr.open_group(store, mode="r")
        print("zarr top-level arrays:", list(g.array_keys()), "groups:", list(g.group_keys()))
        # hrrrzarr nests the data array one level down: <var>/<level>/<var>
        arr = g["2m_above_ground/TMP"] if "2m_above_ground" in list(g.group_keys()) else g[list(g.array_keys())[0]]
        print("shape", arr.shape, "chunks", arr.chunks, "dtype", arr.dtype)
        # projection grid index for Central Park from the HRRR grid: we know the (i,j) from the pipeline gridpoint table (698,1553)
        i, j = 698, 1553
        t1 = time.time()
        v = arr[:, i, j]
        dt = time.time() - t1
        chunk_bytes = np.prod(arr.chunks) * arr.dtype.itemsize
        print(f"point series: {v[:3]} ... n={len(v)}  read {dt:.1f}s; one chunk = {chunk_bytes/1e6:.1f} MB uncompressed (all leads in one chunk: {arr.chunks[0]==arr.shape[0]})")
        print(f"zarr total {time.time()-t0:.1f}s")
        return True
    except Exception as e:
        print("zarr path FAILED:", repr(e)[:300])
        return False

def grib_path():
    from herbie import Herbie
    t0 = time.time(); nbytes = 0
    for f in (1, 2, 3):
        H = Herbie("2025-09-04 12:00", model="hrrr", product="sfc", fxx=f, verbose=False, save_dir=os.environ.get("SB_CACHE", ".cache/herbie"))
        p = H.download(":TMP:2 m above ground:", verbose=False)
        nbytes += os.path.getsize(p); os.remove(p)
    dt = time.time() - t0
    print(f"grib idx path: 3 leads, {nbytes/1e6:.1f} MB, {dt:.1f}s  -> per 48-lead cycle ~{nbytes/3*48/1e6:.0f} MB, ~{dt/3*48:.0f}s (one var)")

if __name__ == "__main__":
    ok = zarr_path()
    grib_path()
