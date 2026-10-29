#!/usr/bin/env python3
"""
ncmrwf_pipeline.py  —  End-to-end automation for NCMRWF 9 km NWP forecast
=============================================================================

WHAT THIS SCRIPT DOES (fully automatic)
----------------------------------------
  Step 1 — Download  : Fetch NCMRWF NEPS / NGFS 9km forecast data
                        from rds.ncmrwf.gov.in using your stored credentials.
  Step 2 — Convert   : Run ncmrwf_to_json.py to produce HydroClim Cast JSON.
  Step 3 — Deploy    : Copy the JSON to the repo root and patch index.html
                        so the new run appears in the UI dropdown instantly.
  Step 4 — Commit    : Optionally git-commit and push the result.

SETUP (one-time)
-----------------
  1. pip install requests netCDF4 cfgrib eccodes xarray tqdm
  2. python ncmrwf_pipeline.py --setup          <- stores credentials
  3. python ncmrwf_pipeline.py                  <- runs today's 00Z cycle
  4. Add to Windows Task Scheduler / cron for daily automation.

USAGE
------
  # Run today's 00Z cycle (default)
  python ncmrwf_pipeline.py

  # Run a specific date + cycle
  python ncmrwf_pipeline.py --date 20261029 --cycle 12

  # Run without git commit
  python ncmrwf_pipeline.py --no-git

  # Dry-run: skip download, convert an existing local file
  python ncmrwf_pipeline.py --local-file ./raw/forecast.nc

  # Adjust forecast zone
  python ncmrwf_pipeline.py --zone india_subcontinent --bbox 67 7 98 38

CREDENTIALS
-----------
  The script stores credentials in  ~/.ncmrwf_credentials.json
  Run  python ncmrwf_pipeline.py --setup  to set them interactively.
  Or set environment variables:
      NCMRWF_EMAIL=your@email.com
      NCMRWF_PASSWORD=yourpassword
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import zipfile
from datetime import datetime, timezone, timedelta
from pathlib import Path

# ──────────────────────────────────────────────────────────────────────────────
# CONFIGURATION DEFAULTS
# ──────────────────────────────────────────────────────────────────────────────

REPO_ROOT   = Path(__file__).parent.resolve()
CONVERTER   = REPO_ROOT / "ncmrwf_to_json.py"
INDEX_HTML  = REPO_ROOT / "index.html"
RAW_DIR     = REPO_ROOT / "raw_ncmrwf"           # temporary download staging
CREDS_FILE  = Path.home() / ".ncmrwf_credentials.json"

# NCMRWF RDS portal endpoints
RDS_BASE    = "https://rds.ncmrwf.gov.in"
RDS_LOGIN   = f"{RDS_BASE}/accounts/login/"
RDS_SUBMIT  = f"{RDS_BASE}/api/request/"          # dataset extraction API
RDS_STATUS  = f"{RDS_BASE}/api/request/{{rid}}/"  # status poll
RDS_DL      = f"{RDS_BASE}/api/request/{{rid}}/download/"

# Default NCMRWF dataset IDs on the RDS portal
# (Update these if the portal changes them — check the portal Network tab)
DATASET_NGFS = "NGFS"      # NCMRWF Global Forecast System (operational, 9 km)
DATASET_NEPS = "NEPS"      # NCMRWF Ensemble Prediction System (9/12 km)

# Default spatial domain for India
DEFAULT_BBOX = [67.0, 7.0, 98.0, 38.0]   # lon_min lat_min lon_max lat_max
DEFAULT_ZONE = "india_subcontinent"

# Variables to request from the portal
PORTAL_VARIABLES = [
    "Temperature_surface",
    "Precipitation_surface",
    "u-component_of_wind_height_above_ground",
    "v-component_of_wind_height_above_ground",
    "Specific_humidity_height_above_ground",
]

# ──────────────────────────────────────────────────────────────────────────────
# HELPERS
# ──────────────────────────────────────────────────────────────────────────────

def log(msg, level="INFO"):
    ts = datetime.now().strftime("%H:%M:%S")
    prefix = {"INFO": "ℹ", "OK": "✔", "WARN": "⚠", "ERR": "✘", "STEP": "▶"}.get(level, "·")
    # Fallback for consoles that can't display Unicode
    prefix_ascii = {"INFO": "[i]", "OK": "[ok]", "WARN": "[!]", "ERR": "[X]", "STEP": ">>>"}
    try:
        print(f"[{ts}] {prefix}  {msg}", flush=True)
    except UnicodeEncodeError:
        print(f"[{ts}] {prefix_ascii.get(level,'[-]')} {msg}", flush=True)


def abort(msg):
    log(msg, "ERR")
    sys.exit(1)


# ──────────────────────────────────────────────────────────────────────────────
# STEP 0 — CREDENTIAL MANAGEMENT
# ──────────────────────────────────────────────────────────────────────────────

def setup_credentials():
    """Interactive credential setup (run once)."""
    import getpass
    log("=== NCMRWF RDS Credential Setup ===", "STEP")
    print("  Register at https://rds.ncmrwf.gov.in if you don't have an account.")
    email = input("  RDS email    : ").strip()
    pwd   = getpass.getpass("  RDS password : ")
    creds = {"email": email, "password": pwd}
    CREDS_FILE.write_text(json.dumps(creds, indent=2))
    CREDS_FILE.chmod(0o600)
    log(f"Credentials saved to {CREDS_FILE}", "OK")


def load_credentials():
    """Return (email, password) from env vars or credentials file."""
    email = os.environ.get("NCMRWF_EMAIL")
    pwd   = os.environ.get("NCMRWF_PASSWORD")
    if email and pwd:
        return email, pwd
    if CREDS_FILE.exists():
        creds = json.loads(CREDS_FILE.read_text())
        return creds.get("email"), creds.get("password")
    abort(
        "No credentials found.\n"
        "  Run:  python ncmrwf_pipeline.py --setup\n"
        "  Or set env vars NCMRWF_EMAIL and NCMRWF_PASSWORD"
    )


# ──────────────────────────────────────────────────────────────────────────────
# STEP 1 — DOWNLOAD FROM RDS PORTAL
# ──────────────────────────────────────────────────────────────────────────────

def get_session(email, password):
    """Authenticate with the RDS portal and return a requests Session."""
    try:
        import requests
    except ImportError:
        abort("requests not installed.  Run: pip install requests")

    session = requests.Session()
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (HydroClim-NCMRWF-Pipeline/1.0)",
        "Referer": RDS_BASE,
    })

    # Get CSRF token from login page
    log("Authenticating with RDS portal ...", "STEP")
    try:
        r = session.get(RDS_LOGIN, timeout=30)
        r.raise_for_status()
    except Exception as e:
        abort(f"Cannot reach RDS portal: {e}\n  Check your internet connection or VPN.")

    csrf = _extract_csrf(r.text)
    if not csrf:
        abort("Could not extract CSRF token from login page. The portal HTML may have changed.")

    # POST login
    payload = {
        "csrfmiddlewaretoken": csrf,
        "login": email,
        "password": password,
        "next": "/",
    }
    r = session.post(RDS_LOGIN, data=payload, timeout=30)
    if "logout" not in r.text.lower() and r.status_code != 302:
        abort(
            "Login failed — wrong credentials or CAPTCHA required.\n"
            "  1. Try logging in at https://rds.ncmrwf.gov.in manually.\n"
            "  2. If successful, use --cookie-file to supply session cookies."
        )
    log("Authenticated successfully.", "OK")
    return session


def _extract_csrf(html):
    m = re.search(r'csrfmiddlewaretoken.*?value=["\']([^"\']+)', html)
    if m:
        return m.group(1)
    m = re.search(r'name="csrfmiddlewaretoken"\s+value="([^"]+)"', html)
    return m.group(1) if m else None


def submit_data_request(session, dataset, date_str, cycle, bbox, variables, max_steps):
    """Submit a data extraction request to the RDS API; return request ID."""
    log(f"Submitting {dataset} data request for {date_str} {cycle}Z ...", "STEP")

    lon_min, lat_min, lon_max, lat_max = bbox
    # Build ISO8601 date range for the requested forecast
    run_dt = datetime.strptime(f"{date_str}{cycle}", "%Y%m%d%H")
    end_dt = run_dt + timedelta(hours=max_steps * 3)  # 3-hourly assumed

    payload = {
        "dataset":    dataset,
        "format":     "netcdf",
        "startDate":  run_dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "endDate":    end_dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "lonMin":     lon_min,
        "lonMax":     lon_max,
        "latMin":     lat_min,
        "latMax":     lat_max,
        "variables":  variables,
        "levels":     "surface",
        "frequency":  "3hourly",
    }

    # Try to get CSRF for API call
    r = session.get(f"{RDS_BASE}/", timeout=20)
    csrf = _extract_csrf(r.text) or session.cookies.get("csrftoken", "")

    headers = {"X-CSRFToken": csrf, "Content-Type": "application/json"}
    r = session.post(RDS_SUBMIT, json=payload, headers=headers, timeout=60)

    if r.status_code not in (200, 201, 202):
        log(f"API response {r.status_code}: {r.text[:300]}", "WARN")
        abort(
            "Data request submission failed.\n"
            "  The RDS API endpoints may have changed. Please:\n"
            "  1. Log into https://rds.ncmrwf.gov.in manually.\n"
            "  2. Use the web interface to submit a request.\n"
            "  3. Download the ZIP and run with --local-file option."
        )

    rid = r.json().get("id") or r.json().get("request_id")
    if not rid:
        abort(f"No request ID in response: {r.text[:200]}")
    log(f"Request submitted — ID: {rid}", "OK")
    return str(rid)


def wait_and_download(session, rid, out_dir: Path, timeout_min=30):
    """Poll request status until ready, then download the ZIP."""
    import time
    try:
        from tqdm import tqdm
        USE_TQDM = True
    except ImportError:
        USE_TQDM = False

    log(f"Waiting for request {rid} to complete (timeout: {timeout_min} min) ...", "STEP")
    out_dir.mkdir(parents=True, exist_ok=True)
    deadline = time.time() + timeout_min * 60
    poll_interval = 15  # seconds

    while time.time() < deadline:
        r = session.get(RDS_STATUS.format(rid=rid), timeout=20)
        info = r.json() if r.ok else {}
        status = info.get("status", "unknown").lower()
        log(f"  Status: {status}")

        if status in ("complete", "done", "ready", "success"):
            break
        if status in ("failed", "error", "cancelled"):
            abort(f"Request {rid} ended with status: {status}")
        time.sleep(poll_interval)
    else:
        abort(f"Timed out waiting for request {rid}.")

    # Download
    log("Downloading data package ...", "STEP")
    dl_url = RDS_DL.format(rid=rid)
    r = session.get(dl_url, stream=True, timeout=300)
    r.raise_for_status()

    # Determine filename
    cd = r.headers.get("Content-Disposition", "")
    m = re.search(r'filename="?([^";\r\n]+)', cd)
    fname = m.group(1) if m else f"ncmrwf_{rid}.zip"
    out_path = out_dir / fname

    total = int(r.headers.get("Content-Length", 0))
    chunk_size = 1024 * 1024  # 1 MB

    if USE_TQDM and total:
        from tqdm import tqdm
        with tqdm(total=total, unit="B", unit_scale=True, desc=fname) as pbar:
            with open(out_path, "wb") as f:
                for chunk in r.iter_content(chunk_size):
                    f.write(chunk)
                    pbar.update(len(chunk))
    else:
        downloaded = 0
        with open(out_path, "wb") as f:
            for chunk in r.iter_content(chunk_size):
                f.write(chunk)
                downloaded += len(chunk)
                pct = f"{downloaded/total*100:.0f}%" if total else f"{downloaded//1048576} MB"
                print(f"\r  Downloaded {pct}   ", end="", flush=True)
        print()

    log(f"Downloaded: {out_path}  ({out_path.stat().st_size // 1048576} MB)", "OK")
    return out_path


def extract_netcdf(zip_path: Path, out_dir: Path):
    """Extract NetCDF file(s) from downloaded ZIP archive."""
    log(f"Extracting {zip_path.name} ...", "STEP")
    out_dir.mkdir(parents=True, exist_ok=True)

    # If it's already a NetCDF (portal sometimes sends .nc directly)
    if zip_path.suffix in (".nc", ".nc4", ".netcdf", ".grb2", ".grib2"):
        dest = out_dir / zip_path.name
        shutil.copy2(zip_path, dest)
        return dest

    with zipfile.ZipFile(zip_path) as zf:
        members = zf.namelist()
        nc_files = [m for m in members if m.endswith((".nc", ".nc4", ".grib2", ".grb2"))]
        if not nc_files:
            abort(f"No NetCDF/GRIB2 files found in ZIP. Contents: {members}")
        zf.extractall(out_dir, nc_files)
        first = out_dir / nc_files[0]
        log(f"Extracted: {first.name}", "OK")
        return first


def download_pipeline(email, password, dataset, date_str, cycle, bbox, variables, max_steps, raw_dir):
    """Full download flow: auth → request → wait → download → extract."""
    session = get_session(email, password)
    rid = submit_data_request(session, dataset, date_str, cycle, bbox, variables, max_steps)
    zip_path = wait_and_download(session, rid, raw_dir / "zips")
    nc_path = extract_netcdf(zip_path, raw_dir / "extracted")
    return nc_path


# ──────────────────────────────────────────────────────────────────────────────
# STEP 2 — CONVERT TO JSON
# ──────────────────────────────────────────────────────────────────────────────

def convert_to_json(nc_path: Path, json_path: Path, bbox, max_steps):
    """Call ncmrwf_to_json.py as subprocess so it works independently."""
    log(f"Converting {nc_path.name} → {json_path.name} ...", "STEP")

    if not CONVERTER.exists():
        abort(f"Converter not found: {CONVERTER}")

    fmt = "grib2" if nc_path.suffix in (".grb2", ".grib2", ".grb", ".grib") else "netcdf"
    cmd = [
        sys.executable, str(CONVERTER),
        "--input",  str(nc_path),
        "--output", str(json_path),
        "--format", fmt,
    ]
    if bbox:
        cmd += ["--bbox"] + [str(v) for v in bbox]
    if max_steps:
        cmd += ["--max-steps", str(max_steps)]

    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.stdout:
        for line in result.stdout.strip().splitlines():
            log(f"  converter: {line}")
    if result.returncode != 0:
        log(result.stderr, "ERR")
        abort("Conversion failed.")

    size_mb = json_path.stat().st_size / 1_048_576
    log(f"JSON produced: {json_path.name} ({size_mb:.1f} MB)", "OK")
    return json_path


# ──────────────────────────────────────────────────────────────────────────────
# STEP 3 — DEPLOY: patch index.html
# ──────────────────────────────────────────────────────────────────────────────

def _run_label(date_str, cycle, dataset):
    """Human-readable year-slot label, e.g. '2026-10-29 00Z (NGFS)'"""
    d = datetime.strptime(date_str, "%Y%m%d")
    return f"{d.strftime('%Y-%m-%d')} {cycle}Z ({dataset})"


def patch_index_html(json_filename: str, date_str: str, cycle: str,
                     dataset: str, zone: str, index_html: Path):
    """
    Insert or update a years entry in basins['ncmrwf_9km'].zones[zone].years.
    Idempotent: re-running for the same date replaces the previous entry.
    """
    log(f"Patching {index_html.name} ...", "STEP")

    content = index_html.read_text(encoding="utf-8")
    run_key   = f"{date_str}_{cycle}Z"          # e.g. "20261029_00Z"
    run_label = _run_label(date_str, cycle, dataset)

    # Build the new years entry lines
    new_entry = (
        f'                            "{run_key}": {{\n'
        f'                                dataFile: "{json_filename}",\n'
        f'                                label: "{run_label}"\n'
        f'                            }},\n'
    )

    # ── Strategy: find the zone block inside ncmrwf_9km and its years {} ──────
    # Locate the zone key inside the ncmrwf_9km block
    ncmrwf_start = content.find('"ncmrwf_9km"')
    if ncmrwf_start == -1:
        abort("ncmrwf_9km basin not found in index.html. Run the original patch first.")

    zone_anchor = content.find(f'"{zone}"', ncmrwf_start)
    if zone_anchor == -1:
        abort(f"Zone '{zone}' not found inside ncmrwf_9km.")

    years_start = content.find('"years"', zone_anchor)
    years_open  = content.find('{', years_start) + 1

    # Check if this run_key already exists → replace it
    if f'"{run_key}"' in content[years_open:years_open + 2000]:
        log(f"  Entry '{run_key}' already exists — updating.", "WARN")
        old_entry_re = re.compile(
            rf'(\s*"{re.escape(run_key)}":\s*{{[^}}]*}},?\n?)',
            re.DOTALL
        )
        content = old_entry_re.sub(new_entry, content, count=1)
    else:
        # Insert after the opening brace of years: {
        content = content[:years_open] + "\n" + new_entry + content[years_open:]

    index_html.write_text(content, encoding="utf-8")
    log(f"  Added run '{run_key}' to zone '{zone}' in index.html", "OK")


# ──────────────────────────────────────────────────────────────────────────────
# STEP 4 — GIT COMMIT & PUSH
# ──────────────────────────────────────────────────────────────────────────────

def git_commit_push(json_filename: str, date_str: str, cycle: str, dataset: str):
    log("Committing and pushing to git ...", "STEP")
    run_label = _run_label(date_str, cycle, dataset)
    msg = f"data: add NCMRWF 9km forecast {run_label}"
    cmds = [
        ["git", "-C", str(REPO_ROOT), "add", json_filename, "index.html"],
        ["git", "-C", str(REPO_ROOT), "commit", "-m", msg],
        ["git", "-C", str(REPO_ROOT), "push"],
    ]
    for cmd in cmds:
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode != 0:
            log(f"Git error: {r.stderr.strip()}", "WARN")
            log("Continuing without push — commit the files manually.", "WARN")
            break
        log(f"  $ {' '.join(cmd[2:])}", "OK")


# ──────────────────────────────────────────────────────────────────────────────
# MAIN ENTRY POINT
# ──────────────────────────────────────────────────────────────────────────────

def main():
    p = argparse.ArgumentParser(
        description="NCMRWF 9km forecast pipeline: download → convert → deploy → commit"
    )
    p.add_argument("--setup",       action="store_true",  help="Store RDS credentials (run once)")
    p.add_argument("--date",        default=None,         help="YYYYMMDD (default: today UTC)")
    p.add_argument("--cycle",       default="00",         choices=["00", "06", "12", "18"],
                   help="Model run cycle (default: 00)")
    p.add_argument("--dataset",     default=DATASET_NGFS, help="RDS dataset ID (default: NGFS)")
    p.add_argument("--zone",        default=DEFAULT_ZONE, help="index.html zone key to update")
    p.add_argument("--bbox",        nargs=4, type=float,
                   metavar=("LON_MIN", "LAT_MIN", "LON_MAX", "LAT_MAX"),
                   default=DEFAULT_BBOX, help="Bounding box (default: India)")
    p.add_argument("--max-steps",   type=int, default=25, help="Max timesteps (default: 25 = 72h at 3h)")
    p.add_argument("--local-file",  default=None,         help="Skip download; convert this local file")
    p.add_argument("--no-git",      action="store_true",  help="Skip git commit/push")
    p.add_argument("--cookie-file", default=None,         help="JSON file with session cookies (advanced)")
    args = p.parse_args()

    # ── Setup mode ──────────────────────────────────────────────────────────
    if args.setup:
        setup_credentials()
        return

    # ── Resolve date ─────────────────────────────────────────────────────────
    date_str = args.date or datetime.now(timezone.utc).strftime("%Y%m%d")
    cycle    = args.cycle
    log(f"Pipeline start — {date_str} {cycle}Z  dataset={args.dataset}", "STEP")
    log(f"  bbox: {args.bbox}   zone: {args.zone}   max_steps: {args.max_steps}")

    # ── Output JSON filename ──────────────────────────────────────────────────
    json_filename = f"ncmrwf_9km_{date_str}_{cycle}Z.json"
    json_path     = REPO_ROOT / json_filename

    # ── Step 1: Download (or skip if --local-file) ────────────────────────────
    if args.local_file:
        nc_path = Path(args.local_file)
        if not nc_path.exists():
            abort(f"Local file not found: {nc_path}")
        log(f"Using local file: {nc_path}", "OK")
    else:
        email, password = load_credentials()
        RAW_DIR.mkdir(parents=True, exist_ok=True)
        nc_path = download_pipeline(
            email, password,
            dataset   = args.dataset,
            date_str  = date_str,
            cycle     = cycle,
            bbox      = args.bbox,
            variables = PORTAL_VARIABLES,
            max_steps = args.max_steps,
            raw_dir   = RAW_DIR,
        )

    # ── Step 2: Convert ───────────────────────────────────────────────────────
    convert_to_json(nc_path, json_path, args.bbox, args.max_steps)

    # ── Step 3: Deploy ────────────────────────────────────────────────────────
    patch_index_html(
        json_filename = json_filename,
        date_str      = date_str,
        cycle         = cycle,
        dataset       = args.dataset,
        zone          = args.zone,
        index_html    = INDEX_HTML,
    )

    # ── Step 4: Git ───────────────────────────────────────────────────────────
    if not args.no_git:
        git_commit_push(json_filename, date_str, cycle, args.dataset)

    log("Pipeline complete!", "OK")
    log(f"  New data file : {json_filename}")
    log(f"  Open index.html in a browser to see the new forecast run.")


if __name__ == "__main__":
    main()
