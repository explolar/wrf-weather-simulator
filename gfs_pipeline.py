#!/usr/bin/env python3
"""
gfs_pipeline.py — NOAA GFS 0.25° forecast automation
=======================================================
Same shape as ecmwf_pipeline.py (download -> convert -> deploy -> git push),
reusing forecast_common.py for everything past "I have a GRIB2 file".

Download source: NOAA NOMADS (FREE, no auth, last ~10 days of runs).
  https://nomads.ncep.noaa.gov/pub/data/nccf/com/gfs/prod/

GFS publishes one GRIB2 file per forecast hour (not one multi-step file
like ECMWF's open-data client), so this fetches only the needed fields
(TMP/UGRD/VGRD/SPFH 2m-10m, APCP surface) per hour via HTTP byte-range
requests against each file's .idx sidecar, and concatenates the raw GRIB2
messages into one local file — cfgrib reads the concatenation fine, each
field ends up with one value per forecast hour (a "time" dimension).

Fields (GRIB2 shortNames, already in ecmwf_to_json.py's GRIB_MAP):
  TMP:2 m above ground   -> 2t  -> temperature
  APCP:surface (0-N acc) -> tp  -> precipitation (unit: mm, NOT metres)
  UGRD/VGRD:10 m         -> 10u/10v -> wind_speed
  SPFH:2 m above ground  -> 2sh -> specific_humidity (direct, no RH->q needed)

f000 is skipped: GFS has no APCP message at init (zero accumulation), and
including it would misalign the precipitation time series against the
other fields. Steps start at f003.

Usage
-----
  python gfs_pipeline.py                      # latest run, india_subcontinent
  python gfs_pipeline.py --zone all            # every configured zone
  python gfs_pipeline.py --date 20261029 --cycle 00
  python gfs_pipeline.py --local-file gfs_merged.grib2
  python gfs_pipeline.py --no-git
"""

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

from forecast_common import (
    log, abort, REPO_ROOT, ZONE_BBOXES, DEFAULT_ZONE,
    convert_to_json, patch_index_html, git_commit_push,
)

RAW_DIR  = REPO_ROOT / "raw_gfs"
GFS_BASE = "https://nomads.ncep.noaa.gov/pub/data/nccf/com/gfs/prod"
BASIN_KEY = "gfs"

# Skip f000 — no APCP message at init, would misalign the precip series.
DEFAULT_STEPS = list(range(3, 73, 3))   # 3 h ... 72 h, 24 files

# (GRIB2 param, level, optional extra filter on the idx description).
# APCP has two idx entries per hour beyond f003 — a cumulative "0-N hour
# acc fcst" one and a bucketed "(N-3)-N hour acc fcst" one. We want the
# cumulative one (matches ECMWF tp's semantics: diffed across steps to
# get per-interval mm), so keep only entries whose window starts at 0.
GFS_FIELDS = [
    ("TMP",  "2 m above ground",  None),
    ("UGRD", "10 m above ground", None),
    ("VGRD", "10 m above ground", None),
    ("SPFH", "2 m above ground",  None),
    ("APCP", "surface",           lambda desc: desc.split(":")[2].startswith("0-")),
]


def _grib_idx_ranges(session, idx_url, field_specs):
    r = session.get(idx_url, timeout=20)
    r.raise_for_status()
    lines = [l for l in r.text.strip().splitlines() if l]
    entries = []
    for line in lines:
        parts = line.split(":")
        offset = int(parts[1])
        desc = ":".join(parts[3:])   # "PARAM:LEVEL:FCST_DESC:"
        entries.append((offset, desc))

    ranges = []
    seen = set()
    for i, (offset, desc) in enumerate(entries):
        for param, level, extra in field_specs:
            if not desc.startswith(f"{param}:{level}:"):
                continue
            if extra and not extra(desc):
                continue
            key = (param, level)
            if key in seen:
                break
            seen.add(key)
            end = entries[i + 1][0] - 1 if i + 1 < len(entries) else None
            ranges.append((offset, end))
            break
    return ranges


def download_gfs(date_str, cycle, steps, out_dir: Path) -> Path:
    try:
        import requests
    except ImportError:
        abort("requests not installed.  Run: pip install requests")

    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"gfs_{date_str}_{cycle}Z.grib2"
    base = f"{GFS_BASE}/gfs.{date_str}/{cycle}/atmos"

    log(f"Downloading NOAA GFS 0.25deg — {date_str} {cycle}Z, steps {steps[0]}..{steps[-1]}h ...", "STEP")
    log("  Source: NOAA NOMADS (free, no auth required)")

    session = requests.Session()
    total_fields = 0
    with open(out_path, "wb") as out_f:
        for step in steps:
            fhr = f"f{step:03d}"
            file_url = f"{base}/gfs.t{cycle}z.pgrb2.0p25.{fhr}"
            idx_url = file_url + ".idx"
            try:
                ranges = _grib_idx_ranges(session, idx_url, GFS_FIELDS)
            except Exception as e:
                abort(f"Failed to fetch/parse GFS index for {fhr}: {e}")
            if not ranges:
                abort(f"No matching GRIB fields found for {fhr} "
                      f"(run not published yet, or NOMADS index format changed).")
            for start, end in ranges:
                headers = {"Range": f"bytes={start}-{end}" if end is not None else f"bytes={start}-"}
                r = session.get(file_url, headers=headers, timeout=60)
                r.raise_for_status()
                out_f.write(r.content)
            total_fields += len(ranges)
            log(f"  {fhr}: {len(ranges)} fields fetched", "OK")

    size_mb = out_path.stat().st_size / 1_048_576
    log(f"Downloaded: {out_path.name}  ({size_mb:.1f} MB, {total_fields} GRIB messages)", "OK")
    return out_path


def main():
    ap = argparse.ArgumentParser(
        description="NOAA GFS forecast pipeline: download -> convert -> deploy -> git push"
    )
    ap.add_argument("--date",       default=None,
                    help="YYYYMMDD (default: today UTC)")
    ap.add_argument("--cycle",      default="00", choices=["00", "06", "12", "18"],
                    help="Forecast init cycle (default: 00)")
    ap.add_argument("--steps",      nargs="+", type=int, default=None,
                    help="Forecast lead hours (default: 3 6 ... 72)")
    ap.add_argument("--zone",       default=DEFAULT_ZONE,
                    help="index.html zone key, or 'all' to update every zone "
                         f"in one run ({', '.join(ZONE_BBOXES)}) (default: india_subcontinent)")
    ap.add_argument("--bbox",       nargs=4, type=float,
                    metavar=("W", "S", "E", "N"), default=None,
                    help="Bounding box W S E N (default: zone's known bbox). Ignored with --zone all.")
    ap.add_argument("--max-steps",  type=int, default=24,
                    help="Max timesteps in output JSON (default: 24 = 72h from f003)")
    ap.add_argument("--local-file", default=None,
                    help="Skip download; convert this local GRIB2 file")
    ap.add_argument("--no-git",     action="store_true",
                    help="Skip git commit/push")
    args = ap.parse_args()

    date_str = args.date or datetime.now(timezone.utc).strftime("%Y%m%d")
    cycle    = args.cycle
    steps    = args.steps or DEFAULT_STEPS
    zones    = list(ZONE_BBOXES) if args.zone == "all" else [args.zone]

    log(f"GFS Pipeline — {date_str} {cycle}Z", "STEP")
    log(f"  zones={zones}  max_steps={args.max_steps}")

    if args.local_file:
        src_path = Path(args.local_file)
        if not src_path.exists():
            abort(f"Local file not found: {src_path}")
        log(f"Using local file: {src_path}", "OK")
    else:
        src_path = download_gfs(date_str, cycle, steps, RAW_DIR)

    run_key = f"{date_str}_{cycle}Z"
    d = datetime.strptime(date_str, "%Y%m%d")
    run_label = f"{d.strftime('%Y-%m-%d')} {cycle}Z (NOAA GFS open-data)"

    json_filenames = []
    for zone in zones:
        bbox = args.bbox if (args.bbox and args.zone != "all") else ZONE_BBOXES.get(zone)
        json_filename = f"gfs_{zone}_{date_str}_{cycle}Z.json"
        json_path     = REPO_ROOT / json_filename

        convert_to_json(src_path, json_path, bbox, args.max_steps,
                         source_tag="NOAA_GFS", model_tag="GFS", precip_unit="mm")
        patch_index_html(BASIN_KEY, json_filename, run_key, run_label, zone)
        json_filenames.append(json_filename)

    if not args.no_git:
        git_commit_push(json_filenames, f"add NOAA GFS forecast {run_label}")

    log("Pipeline complete!", "OK")
    for f in json_filenames:
        log(f"  New data: {f}")


if __name__ == "__main__":
    main()
