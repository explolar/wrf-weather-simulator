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
  python ecmwf_pipeline.py --zone west_india --bbox 67 7 78 30
"""

import argparse
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

# ──────────────────────────────────────────────────────────────────────────────
# Config
# ──────────────────────────────────────────────────────────────────────────────

REPO_ROOT  = Path(__file__).parent.resolve()
CONVERTER  = REPO_ROOT / "ecmwf_to_json.py"
INDEX_HTML = REPO_ROOT / "index.html"
RAW_DIR    = REPO_ROOT / "raw_ecmwf"

DEFAULT_BBOX = [67.0, 7.0, 98.0, 38.0]    # India
DEFAULT_ZONE = "india_subcontinent"
DEFAULT_STEPS = list(range(0, 73, 3))       # 0 h … 72 h in 3-h steps (25 files)

# ECMWF IFS open-data parameters (GRIB2 shortNames)
OPENDATA_PARAMS = ["2t", "tp", "10u", "10v", "2d"]

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
# Logging
# ──────────────────────────────────────────────────────────────────────────────

def log(msg, tag="INFO"):
    ts = datetime.now().strftime("%H:%M:%S")
    tags = {"INFO": "[i]", "OK": "[ok]", "WARN": "[!]", "ERR": "[X]", "STEP": ">>>"}
    print(f"[{ts}] {tags.get(tag,'[ ]')} {msg}", flush=True)

def abort(msg):
    log(msg, "ERR")
    sys.exit(1)

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

def download_opendata(date_str, cycle, steps, out_dir: Path) -> Path:
    """
    Download ECMWF IFS HRES open data (0.25°) using the ecmwf-opendata client.
    Returns path to the downloaded GRIB2 file.
    FREE — no account or API key needed.
    Data available for the last ~10 days.
    """
    try:
        from ecmwf.opendata import Client
    except ImportError:
        abort(
            "ecmwf-opendata not installed.\n"
            "  Run: pip install ecmwf-opendata"
        )

    out_dir.mkdir(parents=True, exist_ok=True)
    filename = f"ecmwf_ifs_{date_str}_{cycle}Z.grib2"
    out_path  = out_dir / filename

    log(f"Downloading ECMWF IFS open data — {date_str} {cycle}Z, steps {steps[0]}..{steps[-1]}h ...", "STEP")
    log("  Source: ECMWF open-data (free, no auth required)")

    client = Client(source="ecmwf")
    client.retrieve(
        date   = date_str,
        time   = int(cycle),
        step   = steps,
        stream = "oper",
        type   = "fc",
        param  = OPENDATA_PARAMS,
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
# STEP 2  —  Convert to HydroClim Cast JSON
# ──────────────────────────────────────────────────────────────────────────────

def convert_to_json(src_path: Path, json_path: Path, bbox, max_steps: int) -> Path:
    log(f"Converting {src_path.name} → {json_path.name} ...", "STEP")

    if not CONVERTER.exists():
        abort(f"Converter not found: {CONVERTER}")

    cmd = [sys.executable, str(CONVERTER),
           "--input",  str(src_path),
           "--output", str(json_path)]
    if bbox:
        cmd += ["--bbox"] + [str(v) for v in bbox]
    if max_steps:
        cmd += ["--max-steps", str(max_steps)]

    result = subprocess.run(cmd, capture_output=True, text=True)
    for line in result.stdout.strip().splitlines():
        log(f"  {line}")
    if result.returncode != 0:
        log(result.stderr, "ERR")
        abort("Conversion failed.")

    size_mb = json_path.stat().st_size / 1_048_576
    log(f"JSON ready: {json_path.name}  ({size_mb:.1f} MB)", "OK")
    return json_path


# ──────────────────────────────────────────────────────────────────────────────
# STEP 3  —  Patch index.html
# ──────────────────────────────────────────────────────────────────────────────

def _run_label(date_str, cycle, source):
    d = datetime.strptime(date_str, "%Y%m%d")
    src_tag = "ERA5/CDS" if source == "cds" else "ECMWF IFS open-data"
    return f"{d.strftime('%Y-%m-%d')} {cycle}Z ({src_tag})"


def patch_index_html(json_filename: str, date_str: str, cycle: str,
                     source: str, zone: str):
    log(f"Patching {INDEX_HTML.name} ...", "STEP")

    content = INDEX_HTML.read_text(encoding="utf-8")
    run_key   = f"{date_str}_{cycle}Z"
    run_label = _run_label(date_str, cycle, source)

    new_entry = (
        f'                            "{run_key}": {{\n'
        f'                                dataFile: "{json_filename}",\n'
        f'                                label: "{run_label}"\n'
        f'                            }},\n'
    )

    # Locate ecmwf_ifs → zone → years
    basin_start = content.find('"ecmwf_ifs"')
    if basin_start == -1:
        abort('ecmwf_ifs basin not found in index.html. Was the NCMRWF→ECMWF patch applied?')

    zone_pos = content.find(f'"{zone}"', basin_start)
    if zone_pos == -1:
        abort(f"Zone '{zone}' not found inside ecmwf_ifs.")

    years_pos  = content.find('"years"', zone_pos)
    years_open = content.find('{', years_pos) + 1

    # Replace existing entry for same key
    if f'"{run_key}"' in content[years_open: years_open + 3000]:
        log(f"  Entry '{run_key}' exists — replacing.", "WARN")
        old_re = re.compile(
            rf'(\s*"{re.escape(run_key)}":\s*{{[^}}]*}},?\n?)', re.DOTALL
        )
        content = old_re.sub(new_entry, content, count=1)
    else:
        content = content[:years_open] + "\n" + new_entry + content[years_open:]

    INDEX_HTML.write_text(content, encoding="utf-8")
    log(f"  Added '{run_key}' → zone '{zone}' in index.html", "OK")


# ──────────────────────────────────────────────────────────────────────────────
# STEP 4  —  Git commit + push
# ──────────────────────────────────────────────────────────────────────────────

def git_commit_push(json_filename: str, date_str: str, cycle: str, source: str):
    log("Git commit + push ...", "STEP")
    label = _run_label(date_str, cycle, source)
    msg = f"data: add ECMWF IFS forecast {label}"
    for cmd in [
        ["git", "-C", str(REPO_ROOT), "add", json_filename, "index.html"],
        ["git", "-C", str(REPO_ROOT), "commit", "-m", msg],
        ["git", "-C", str(REPO_ROOT), "push"],
    ]:
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode != 0:
            log(f"git: {r.stderr.strip()}", "WARN")
            break
        log(f"  $ {' '.join(cmd[2:])}", "OK")


# ──────────────────────────────────────────────────────────────────────────────
# Main
# ──────────────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(
        description="ECMWF IFS forecast pipeline: download → convert → deploy → git push"
    )
    ap.add_argument("--setup-cds",  action="store_true",
                    help="Save Copernicus CDS API credentials (run once for ERA5 mode)")
    ap.add_argument("--source",     default="opendata", choices=["opendata", "cds"],
                    help="Data source: opendata (free IFS, default) or cds (ERA5, needs key)")
    ap.add_argument("--date",       default=None,
                    help="YYYYMMDD (default: today UTC; opendata: latest available)")
    ap.add_argument("--cycle",      default="00", choices=["00", "06", "12", "18"],
                    help="Forecast init cycle (default: 00)")
    ap.add_argument("--steps",      nargs="+", type=int, default=None,
                    help="Forecast lead hours (default: 0 3 6 … 72)")
    ap.add_argument("--zone",       default=DEFAULT_ZONE,
                    help="index.html zone key (default: india_subcontinent)")
    ap.add_argument("--bbox",       nargs=4, type=float,
                    metavar=("W", "S", "E", "N"), default=DEFAULT_BBOX,
                    help="Bounding box W S E N (default: 67 7 98 38 = India)")
    ap.add_argument("--max-steps",  type=int, default=25,
                    help="Max timesteps in output JSON (default: 25 = 72h)")
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
    steps    = args.steps or DEFAULT_STEPS
    source   = args.source

    log(f"ECMWF Pipeline — {date_str} {cycle}Z  source={source}", "STEP")
    log(f"  bbox={args.bbox}  zone={args.zone}  max_steps={args.max_steps}")

    json_filename = f"ecmwf_ifs_{date_str}_{cycle}Z.json"
    json_path     = REPO_ROOT / json_filename

    # ── Step 1: Download ──────────────────────────────────────────────────────
    if args.local_file:
        src_path = Path(args.local_file)
        if not src_path.exists():
            abort(f"Local file not found: {src_path}")
        log(f"Using local file: {src_path}", "OK")
    elif source == "opendata":
        RAW_DIR.mkdir(parents=True, exist_ok=True)
        src_path = download_opendata(date_str, cycle, steps, RAW_DIR)
    else:  # cds
        RAW_DIR.mkdir(parents=True, exist_ok=True)
        src_path = download_cds(date_str, cycle, args.bbox, RAW_DIR)

    # ── Step 2: Convert ───────────────────────────────────────────────────────
    convert_to_json(src_path, json_path, args.bbox, args.max_steps)

    # ── Step 3: Deploy ────────────────────────────────────────────────────────
    patch_index_html(json_filename, date_str, cycle, source, args.zone)

    # ── Step 4: Git ───────────────────────────────────────────────────────────
    if not args.no_git:
        git_commit_push(json_filename, date_str, cycle, source)

    log("Pipeline complete!", "OK")
    log(f"  New data: {json_filename}")
    log(f"  Open index.html to see the new ECMWF forecast run.")


if __name__ == "__main__":
    main()

