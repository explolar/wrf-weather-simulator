#!/usr/bin/env python3
"""
icon_pipeline.py — DWD ICON global forecast automation
=========================================================
Free, no-auth, like the other pipelines — but ICON's native grid is an
*icosahedral* (unstructured) mesh, not a regular lat/lon grid, so this
pipeline looks different from ecmwf_pipeline.py / gfs_pipeline.py:

  1. Download each needed parameter as its own small per-step GRIB2.bz2
     file (DWD publishes one file per param per hour — no idx/byte-range
     tricks needed here, unlike GFS).
  2. Download CLAT/CLON — the lat/lon of each of the ~2.95M grid cells,
     published as their own tiny time-invariant GRIB2 files, in the same
     cell ordering as every data field.
  3. Build a nearest-neighbour index (scipy cKDTree) from CLAT/CLON to
     each zone's regular target lat/lon grid (same 0.25deg grid shape
     every other model here uses), once per run.
  4. Remap every field via that index (flat_field[nn_index].reshape(H,W))
     into the same (raw, lat, lon, time_strs) shape ecmwf_to_json.py's
     GRIB2 reader produces, then hand off to its _finalise() — so all the
     unit conversions (pressure, cloud cover, humidity, etc) are shared,
     not reimplemented.

Native resolution: R03B07, ~13 km (DWD's own figure).

Usage
-----
  python icon_pipeline.py                      # latest run, india_subcontinent
  python icon_pipeline.py --zone all            # every configured zone
  python icon_pipeline.py --date 20261029 --cycle 00
  python icon_pipeline.py --no-git
"""

import argparse
import bz2
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from forecast_common import (
    log, abort, REPO_ROOT, ZONE_BBOXES, DEFAULT_ZONE,
    patch_index_html, git_commit_push,
)
from ecmwf_to_json import GRIB_MAP, _finalise

RAW_DIR  = REPO_ROOT / "raw_icon"
DWD_BASE = "https://opendata.dwd.de/weather/nwp/icon/grib"
BASIN_KEY = "icon"

# 3-hourly out to 180h (7.5 days) — ICON's own cadence, and its max step
# (confirmed live: f186 404s, f180 is the last published hour).
DEFAULT_STEPS = list(range(3, 181, 3))   # 3 h ... 180 h, 59 steps

# DWD param name -> (used for every zone's remap). CIN isn't published for
# ICON's single-level open-data set (confirmed: not in the param directory
# listing), same gap as ECMWF.
ICON_PARAMS = [
    "T_2M", "TOT_PREC", "U_10M", "V_10M", "RELHUM_2M",
    "PMSL", "CLCT", "CAPE_ML", "ASWDIR_S", "ASWDIFD_S",
]


def _download_bz2(session, url, out_path: Path, attempts=3, backoff=2.0):
    import time
    last_err = None
    for attempt in range(attempts):
        try:
            r = session.get(url, timeout=60)
            r.raise_for_status()
            out_path.write_bytes(bz2.decompress(r.content))
            return
        except Exception as e:
            last_err = e
            if attempt < attempts - 1:
                time.sleep(backoff * (attempt + 1))
    raise last_err


def fetch_icon_fields(date_str, cycle, steps, out_dir: Path):
    """
    Downloads CLAT/CLON once, plus every ICON_PARAMS field for every step.
    Returns (clat_path, clon_path, {step: {param: path}}).
    Missing individual step/param files are skipped with a warning rather
    than aborting the whole run — DWD occasionally lags publishing one
    field for one hour; better to have a dataset missing one timestep's
    worth of one variable than no dataset at all.
    """
    import requests

    out_dir.mkdir(parents=True, exist_ok=True)
    session = requests.Session()
    run = f"{date_str}{cycle}"
    base = f"{DWD_BASE}/{cycle}"

    log("Downloading DWD ICON grid coordinates (CLAT/CLON) ...", "STEP")
    clat_path = out_dir / "CLAT.grib2"
    clon_path = out_dir / "CLON.grib2"
    _download_bz2(session,
                   f"{base}/clat/icon_global_icosahedral_time-invariant_{run}_CLAT.grib2.bz2",
                   clat_path)
    _download_bz2(session,
                   f"{base}/clon/icon_global_icosahedral_time-invariant_{run}_CLON.grib2.bz2",
                   clon_path)
    log("  Grid coordinates ready.", "OK")

    log(f"Downloading DWD ICON fields — {date_str} {cycle}Z, steps {steps[0]}..{steps[-1]}h ...", "STEP")
    step_files = {}
    for step in steps:
        step_files[step] = {}
        for p in ICON_PARAMS:
            fname = f"icon_global_icosahedral_single-level_{run}_{step:03d}_{p}.grib2.bz2"
            url = f"{base}/{p.lower()}/{fname}"
            out_path = out_dir / f"{p}_{step:03d}.grib2"
            try:
                _download_bz2(session, url, out_path)
                step_files[step][p] = out_path
            except Exception as e:
                log(f"  f{step:03d} {p}: fetch failed ({e}) — skipping", "WARN")
        log(f"  f{step:03d}: {len(step_files[step])}/{len(ICON_PARAMS)} fields fetched", "OK")

    return clat_path, clon_path, step_files


def _read_flat(path: Path):
    """Read a single-message GRIB2 file as a flat (N,) array + its shortName."""
    import cfgrib
    for ds in cfgrib.open_datasets(str(path)):
        for var in ds.data_vars:
            sn = str(ds[var].attrs.get("GRIB_shortName", var))
            return ds[var].values, sn
    return None, None


def build_remap_indices(clat_path, clon_path, zones):
    """
    One nearest-neighbour index array per zone, mapping each cell of that
    zone's regular 0.25deg target grid to the nearest ICON icosahedral
    cell. Returns {zone: (nn_index, lat_1d, lon_1d)}.
    """
    import numpy as np
    from scipy.spatial import cKDTree

    log("Building nearest-neighbour remap index from ICON's icosahedral grid ...", "STEP")
    clat, _ = _read_flat(clat_path)
    clon, _ = _read_flat(clon_path)
    if clat is None or clon is None:
        abort("Could not read CLAT/CLON grid coordinate files.")

    tree = cKDTree(np.column_stack([clat, clon]))

    indices = {}
    for zone in zones:
        w, s, e, n = ZONE_BBOXES[zone]
        lon_1d = np.arange(w, e + 1e-9, 0.25)
        lat_1d = np.arange(s, n + 1e-9, 0.25)
        lon_grid, lat_grid = np.meshgrid(lon_1d, lat_1d)   # (H, W) each
        query_pts = np.column_stack([lat_grid.ravel(), lon_grid.ravel()])
        _, nn_idx = tree.query(query_pts, k=1)
        indices[zone] = (nn_idx, lat_1d, lon_1d)
        log(f"  {zone}: {len(lat_1d)}x{len(lon_1d)} target grid indexed", "OK")

    return indices


def convert_zone(zone, nn_idx, lat_1d, lon_1d, steps, step_files,
                  date_str, cycle, max_steps):
    import numpy as np

    raw = {}   # our_key -> list of 2D arrays, one per step (built up, then stacked)
    time_strs = []
    run_dt = datetime.strptime(f"{date_str}{cycle}", "%Y%m%d%H").replace(tzinfo=timezone.utc)

    for step in steps:
        time_strs.append((run_dt + timedelta(hours=step)).strftime("%Y-%m-%d_%H:%M:%S"))
        files = step_files.get(step, {})
        for p in ICON_PARAMS:
            path = files.get(p)
            if not path or not path.exists():
                continue
            flat, sn = _read_flat(path)
            if flat is None:
                continue
            key = GRIB_MAP.get(sn)
            if key is None:
                continue
            remapped = np.asarray(flat)[nn_idx].reshape(len(lat_1d), len(lon_1d))
            raw.setdefault(key, []).append(remapped)

    # Keep only fields with one frame per step (a field missing even one
    # step would misalign the time axis) — safer than silently truncating.
    n_steps = len(steps)
    raw = {k: np.stack(v) for k, v in raw.items() if len(v) == n_steps}
    if not raw:
        abort(f"No complete fields for zone '{zone}' — check DWD ICON availability.")

    return _finalise(raw, lat_1d, lon_1d, time_strs, bbox=None, max_steps=max_steps,
                      precip_unit="mm", source_tag="DWD_ICON", model_tag="ICON",
                      cloud_unit="percent")


def main():
    ap = argparse.ArgumentParser(
        description="DWD ICON forecast pipeline: download -> remap -> deploy -> git push"
    )
    ap.add_argument("--date",  default=None, help="YYYYMMDD (default: today UTC)")
    ap.add_argument("--cycle", default="00", choices=["00", "06", "12", "18"])
    ap.add_argument("--steps", nargs="+", type=int, default=None,
                    help="Forecast lead hours (default: 3 6 ... 72)")
    ap.add_argument("--zone",  default=DEFAULT_ZONE,
                    help="index.html zone key, or 'all' to update every zone "
                         f"in one run ({', '.join(ZONE_BBOXES)}) (default: india_subcontinent)")
    ap.add_argument("--max-steps", type=int, default=None,
                    help="Max timesteps in output JSON (default: all downloaded steps)")
    ap.add_argument("--no-git", action="store_true")
    args = ap.parse_args()

    date_str = args.date or datetime.now(timezone.utc).strftime("%Y%m%d")
    cycle    = args.cycle
    steps    = args.steps or DEFAULT_STEPS
    zones    = list(ZONE_BBOXES) if args.zone == "all" else [args.zone]
    max_steps = args.max_steps or len(steps)

    log(f"ICON Pipeline — {date_str} {cycle}Z", "STEP")
    log(f"  zones={zones}  max_steps={max_steps}")

    _, _, step_files = fetch_icon_fields(date_str, cycle, steps, RAW_DIR)
    clat_path, clon_path = RAW_DIR / "CLAT.grib2", RAW_DIR / "CLON.grib2"
    remap = build_remap_indices(clat_path, clon_path, zones)

    run_key = f"{date_str}_{cycle}Z"
    d = datetime.strptime(date_str, "%Y%m%d")
    run_label = f"{d.strftime('%Y-%m-%d')} {cycle}Z (DWD ICON open-data)"

    json_filenames = []
    for zone in zones:
        nn_idx, lat_1d, lon_1d = remap[zone]
        log(f"Converting zone '{zone}' ...", "STEP")
        metadata, timesteps = convert_zone(zone, nn_idx, lat_1d, lon_1d, steps,
                                            step_files, date_str, cycle, max_steps)

        json_filename = f"icon_{zone}_{date_str}_{cycle}Z.json"
        json_path = REPO_ROOT / json_filename
        import json as json_mod
        with open(json_path, "w") as f:
            json_mod.dump({"metadata": metadata, "timesteps": timesteps}, f, separators=(",", ":"))
        size_mb = json_path.stat().st_size / 1_048_576
        log(f"  Vars: {metadata['variables_present']}  ({size_mb:.1f} MB, {len(timesteps)} steps)", "OK")

        patch_index_html(BASIN_KEY, json_filename, run_key, run_label, zone)
        json_filenames.append(json_filename)

    if not args.no_git:
        git_commit_push(json_filenames, f"add DWD ICON forecast {run_label}")

    log("Pipeline complete!", "OK")
    for f in json_filenames:
        log(f"  New data: {f}")


if __name__ == "__main__":
    main()
