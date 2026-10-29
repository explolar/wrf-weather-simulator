#!/usr/bin/env python3
"""
ecmwf_pipeline.py  —  End-to-end ECMWF IFS forecast automation
===============================================================

WHAT IT DOES (fully automatic, zero manual steps after setup)
--------------------------------------------------------------
  Step 1 — Download  : Fetch ECMWF IFS HRES forecast via ecmwf-opendata
                        (FREE, no account needed, last 10 days of runs).
                        OR use Copernicus CDS (cdsapi) for ERA5/seasonal.
  Step 2 — Convert   : ecmwf_to_json.py → HydroClim Cast JSON.
  Step 3 — Deploy    : Copy JSON to repo root, patch index.html.
  Step 4 — Commit    : git add + commit + push.

SETUP  (run once)
------------------
  pip install ecmwf-opendata cfgrib eccodes netCDF4 tqdm
  # (No account or API key needed for ecmwf-opendata!)

  # For CDS / ERA5 mode only:
  pip install cdsapi
  python ecmwf_pipeline.py --setup-cds

USAGE
------
  # Latest available ECMWF IFS 00Z run — India, 72 h
  python ecmwf_pipeline.py

  # Specific date + cycle
  python ecmwf_pipeline.py --date 20261029 --cycle 12

  # ERA5 mode via CDS (historical reanalysis, needs API key)
  python ecmwf_pipeline.py --source cds --date 20261029

  # Local GRIB2/NC you already downloaded
  python ecmwf_pipeline.py --local-file forecast.grib2

  # Skip git push
  python ecmwf_pipeline.py --no-git

  # Narrow region  (West India)
  python ecmwf_pipeline.py --zone west_india

  # Every zone in one run (1 download, N conversions, 1 commit) — used by
  # schedule_ecmwf.bat for the daily automated run
  python ecmwf_pipeline.py --zone all

  # AIFS (ECMWF's AI forecast model) instead of physics-based IFS HRES —
  # same free open-data client, just a different model. Runs 6-hourly
  # (not 3-hourly) and its exact field set is less battle-tested than IFS;
  # if a param request fails, rerun with --aifs-minimal-params.
  python ecmwf_pipeline.py --model aifs-single --zone all
"""

import argparse
from datetime import datetime, timezone
from pathlib import Path

from forecast_common import (
    log, abort, REPO_ROOT, ZONE_BBOXES, DEFAULT_ZONE, DEFAULT_BBOX,
    convert_to_json, patch_index_html, git_commit_push,
)

RAW_DIR = REPO_ROOT / "raw_ecmwf"

# IFS HRES: 3-hourly out to 144h (6 days), then 6-hourly to 240h (10 days)
# — that's where ECMWF's open-data cadence itself changes (confirmed live:
# a 3-hourly step past 144h 404s). AIFS-single: 6-hourly throughout, to
# 360h (15 days) — the longest range ECMWF publishes for it.
IFS_STEPS  = list(range(0, 145, 3)) + list(range(150, 241, 6))   # 65 files, 10 days
AIFS_STEPS = list(range(0, 361, 6))                              # 61 files, 15 days

# ECMWF IFS open-data parameters (GRIB2 shortNames). AIFS-single's exact
# published field set is less documented than IFS HRES's; same request is
# tried first, with a smaller fallback if the server rejects it.
OPENDATA_PARAMS = ["2t", "tp", "10u", "10v", "2d", "msl", "tcc", "ssrd"]
# cape/cin are NOT published in ECMWF open-data (confirmed live: "No index
# entries for param=cape") — don't request them, the server has nothing to
# silently drop and errors instead.
# AIFS-single has successfully taken the full 5-param set (2t/tp/10u/10v/2d)
# in practice; this is the fallback if the extended 8-param set above fails.
AIFS_FALLBACK_PARAMS = ["2t", "tp", "10u", "10v", "2d"]

# CDS variable names for ERA5
CDS_VARIABLES = [
    "2m_temperature",
    "total_precipitation",
    "10m_u_component_of_wind",
    "10m_v_component_of_wind",
    "2m_dewpoint_temperature",
]

CDS_CREDS_FILE = Path.home() / ".cdsapirc"

# ──────────────────────────────────────────────────────────────────────────────
# STEP 0  —  CDS credential setup (optional, only for cdsapi/ERA5 mode)
# ──────────────────────────────────────────────────────────────────────────────

def setup_cds():
    """Write ~/.cdsapirc from user input."""
    print("\n=== Copernicus CDS API Setup ===")
    print("  Get your API key from: https://cds.climate.copernicus.eu/user/login")
    print("  (Profile -> API key section)")
    url = input("  CDS URL  [https://cds.climate.copernicus.eu/api]: ").strip()
    key = input("  API key  (UID:token format): ").strip()
    url = url or "https://cds.climate.copernicus.eu/api"
    CDS_CREDS_FILE.write_text(f"url: {url}\nkey: {key}\n")
    CDS_CREDS_FILE.chmod(0o600)
    log(f"CDS credentials saved to {CDS_CREDS_FILE}", "OK")

# ──────────────────────────────────────────────────────────────────────────────
# STEP 1A  —  ECMWF Open Data download  (FREE, no auth)
# ──────────────────────────────────────────────────────────────────────────────

def download_opendata(date_str, cycle, steps, out_dir: Path, model="ifs") -> Path:
    """
    Download ECMWF open data (0.25°) using the ecmwf-opendata client.
    Returns path to the downloaded GRIB2 file.
    FREE — no account or API key needed.
    Data available for the last ~10 days.
    model: "ifs" (physics-based HRES, default) or "aifs-single"
           (ECMWF's AI forecast model, deterministic run).
    """
    try:
        from ecmwf.opendata import Client
    except ImportError:
        abort(
            "ecmwf-opendata not installed.\n"
            "  Run: pip install ecmwf-opendata"
        )

    out_dir.mkdir(parents=True, exist_ok=True)
    tag = "ifs" if model == "ifs" else "aifs"
    filename = f"ecmwf_{tag}_{date_str}_{cycle}Z.grib2"
    out_path  = out_dir / filename

    model_label = "ECMWF IFS HRES" if model == "ifs" else "ECMWF AIFS (AI model)"
    log(f"Downloading {model_label} open data — {date_str} {cycle}Z, steps {steps[0]}..{steps[-1]}h ...", "STEP")
    log("  Source: ECMWF open-data (free, no auth required)")

    client = Client(source="ecmwf")
    try:
        client.retrieve(
            date   = date_str,
            time   = int(cycle),
            step   = steps,
            stream = "oper",
            type   = "fc",
            model  = model,
            param  = OPENDATA_PARAMS,
            target = str(out_path),
        )
    except Exception as e:
        if model == "ifs":
            abort(f"ECMWF open-data retrieval failed: {e}")
        log(f"Full param set failed ({e}); retrying with fallback params {AIFS_FALLBACK_PARAMS} ...", "WARN")
        client.retrieve(
            date   = date_str,
            time   = int(cycle),
            step   = steps,
            stream = "oper",
            type   = "fc",
            model  = model,
            param  = AIFS_FALLBACK_PARAMS,
            target = str(out_path),
        )

    size_mb = out_path.stat().st_size / 1_048_576
    log(f"Downloaded: {out_path.name}  ({size_mb:.1f} MB)", "OK")
    return out_path


# ──────────────────────────────────────────────────────────────────────────────
# STEP 1B  —  Copernicus CDS download  (ERA5 historical, needs API key)
# ──────────────────────────────────────────────────────────────────────────────

def download_cds(date_str, cycle, bbox, out_dir: Path) -> Path:
    """
    Download ERA5 from Copernicus CDS via cdsapi.
    Requires ~/.cdsapirc (run --setup-cds once).
    Returns path to downloaded NetCDF file.
    """
    try:
        import cdsapi
    except ImportError:
        abort("cdsapi not installed.  Run: pip install cdsapi")

    if not CDS_CREDS_FILE.exists():
        abort(
            "CDS credentials not found.\n"
            "  Run: python ecmwf_pipeline.py --setup-cds"
        )

    out_dir.mkdir(parents=True, exist_ok=True)
    filename = f"era5_{date_str}_{cycle}Z.nc"
    out_path  = out_dir / filename

    dt = datetime.strptime(date_str, "%Y%m%d")
    log(f"Downloading ERA5 from CDS — {date_str} {cycle}Z ...", "STEP")

    c = cdsapi.Client()
    c.retrieve(
        "reanalysis-era5-single-levels",
        {
            "product_type": "reanalysis",
            "variable":     CDS_VARIABLES,
            "year":   dt.strftime("%Y"),
            "month":  dt.strftime("%m"),
            "day":    dt.strftime("%d"),
            "time":   [f"{cycle}:00"],
            "area":   [bbox[3], bbox[0], bbox[1], bbox[2]],  # N W S E
            "data_format": "netcdf",
        },
        str(out_path),
    )

    size_mb = out_path.stat().st_size / 1_048_576
    log(f"Downloaded: {out_path.name}  ({size_mb:.1f} MB)", "OK")
    return out_path


# ──────────────────────────────────────────────────────────────────────────────
# Main
# ──────────────────────────────────────────────────────────────────────────────

def _run_label(date_str, cycle, source, model):
    d = datetime.strptime(date_str, "%Y%m%d")
    if source == "cds":
        src_tag = "ERA5/CDS"
    elif model == "ifs":
        src_tag = "ECMWF IFS open-data"
    else:
        src_tag = "ECMWF AIFS open-data"
    return f"{d.strftime('%Y-%m-%d')} {cycle}Z ({src_tag})"


def main():
    ap = argparse.ArgumentParser(
        description="ECMWF forecast pipeline: download -> convert -> deploy -> git push"
    )
    ap.add_argument("--setup-cds",  action="store_true",
                    help="Save Copernicus CDS API credentials (run once for ERA5 mode)")
    ap.add_argument("--source",     default="opendata", choices=["opendata", "cds"],
                    help="Data source: opendata (free, default) or cds (ERA5, needs key)")
    ap.add_argument("--model",      default="ifs", choices=["ifs", "aifs-single"],
                    help="ifs (physics-based HRES, default) or aifs-single "
                         "(ECMWF's AI forecast model). opendata source only.")
    ap.add_argument("--date",       default=None,
                    help="YYYYMMDD (default: today UTC; opendata: latest available)")
    ap.add_argument("--cycle",      default="00", choices=["00", "06", "12", "18"],
                    help="Forecast init cycle (default: 00)")
    ap.add_argument("--steps",      nargs="+", type=int, default=None,
                    help="Forecast lead hours (default: IFS 0-72 by 3h, AIFS 0-72 by 6h)")
    ap.add_argument("--zone",       default=DEFAULT_ZONE,
                    help="index.html zone key, or 'all' to update every zone "
                         f"in one run ({', '.join(ZONE_BBOXES)}) (default: india_subcontinent)")
    ap.add_argument("--bbox",       nargs=4, type=float,
                    metavar=("W", "S", "E", "N"), default=None,
                    help="Bounding box W S E N (default: zone's known bbox, "
                         "or 67 7 98 38 for an unlisted zone). Ignored with --zone all.")
    ap.add_argument("--max-steps",  type=int, default=None,
                    help="Max timesteps in output JSON (default: all downloaded steps)")
    ap.add_argument("--local-file", default=None,
                    help="Skip download; convert this local GRIB2/NC file")
    ap.add_argument("--no-git",     action="store_true",
                    help="Skip git commit/push")
    args = ap.parse_args()

    # ── Setup ─────────────────────────────────────────────────────────────────
    if args.setup_cds:
        setup_cds()
        return

    date_str = args.date or datetime.now(timezone.utc).strftime("%Y%m%d")
    cycle    = args.cycle
    source   = args.source
    model    = args.model
    steps    = args.steps or (IFS_STEPS if model == "ifs" else AIFS_STEPS)
    max_steps = args.max_steps or len(steps)

    if source == "cds":
        basin_key, model_tag, source_tag, file_tag = "ecmwf_ifs", "ERA5", "ERA5_CDS", "era5"
    elif model == "ifs":
        basin_key, model_tag, source_tag, file_tag = "ecmwf_ifs", "IFS", "ECMWF_IFS", "ifs"
    else:
        basin_key, model_tag, source_tag, file_tag = "ecmwf_aifs", "AIFS", "ECMWF_AIFS", "aifs"

    zones = list(ZONE_BBOXES) if args.zone == "all" else [args.zone]

    log(f"ECMWF Pipeline — {date_str} {cycle}Z  source={source}  model={model}", "STEP")
    log(f"  zones={zones}  max_steps={max_steps}")

    # ── Step 1: Download (once, reused for every zone) ───────────────────────
    if args.local_file:
        src_path = Path(args.local_file)
        if not src_path.exists():
            abort(f"Local file not found: {src_path}")
        log(f"Using local file: {src_path}", "OK")
    elif source == "opendata":
        RAW_DIR.mkdir(parents=True, exist_ok=True)
        src_path = download_opendata(date_str, cycle, steps, RAW_DIR, model)
    else:  # cds
        RAW_DIR.mkdir(parents=True, exist_ok=True)
        dl_bbox = args.bbox or ZONE_BBOXES.get(zones[0], DEFAULT_BBOX)
        src_path = download_cds(date_str, cycle, dl_bbox, RAW_DIR)

    # Fixed filename + "operational" key, overwritten every run — not date-
    # stamped. A new file per day would grow the repo forever; the run's
    # actual date/time is still shown to users via run_label.
    run_key   = "operational"
    run_label = _run_label(date_str, cycle, source, model)

    # ── Steps 2+3: Convert + deploy, per zone ─────────────────────────────────
    json_filenames = []
    for zone in zones:
        bbox = args.bbox if (args.bbox and args.zone != "all") else ZONE_BBOXES.get(zone, DEFAULT_BBOX)
        json_filename = f"ecmwf_{file_tag}_{zone}_latest.json"
        json_path     = REPO_ROOT / json_filename

        convert_to_json(src_path, json_path, bbox, max_steps,
                         source_tag=source_tag, model_tag=model_tag,
                         precip_unit="m", cloud_unit="fraction")
        patch_index_html(basin_key, json_filename, run_key, run_label, zone)
        json_filenames.append(json_filename)

    # ── Step 4: Git ───────────────────────────────────────────────────────────
    if not args.no_git:
        git_commit_push(json_filenames, f"add {run_label.split(' (')[0]} {model_tag} forecast")

    log("Pipeline complete!", "OK")
    for f in json_filenames:
        log(f"  New data: {f}")
    log(f"  Open index.html to see the new forecast run(s).")


if __name__ == "__main__":
    main()

