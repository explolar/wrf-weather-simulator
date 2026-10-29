#!/usr/bin/env python3
"""
setup_pipeline.py  —  One-time setup wizard for the NCMRWF pipeline.

Run this ONCE before using ncmrwf_pipeline.py:
    python setup_pipeline.py
"""

import subprocess, sys, shutil, json, getpass
from pathlib import Path

REQUIRED = ["requests", "netCDF4", "cfgrib", "eccodes", "xarray", "tqdm"]
OPTIONAL = ["cfgrib"]   # needs eccodes C library too
CREDS_FILE = Path.home() / ".ncmrwf_credentials.json"

def pip_install(packages):
    print(f"\n[setup] Installing: {', '.join(packages)} ...")
    result = subprocess.run(
        [sys.executable, "-m", "pip", "install", "--upgrade"] + packages,
        capture_output=False
    )
    return result.returncode == 0

def check_package(pkg):
    try:
        __import__(pkg.replace("-", "_"))
        return True
    except ImportError:
        return False

def main():
    print("=" * 60)
    print("  NCMRWF 9km Pipeline — Setup Wizard")
    print("=" * 60)

    # ── 1. Install Python packages ────────────────────────────────
    print("\n[1/3] Checking Python dependencies ...")
    missing = [p for p in REQUIRED if not check_package(p.split("[")[0].replace("-","_"))]
    if missing:
        print(f"  Missing: {missing}")
        ok = pip_install(missing)
        if not ok:
            print("  [!] Some packages failed. Try manually:")
            print(f"      pip install {' '.join(missing)}")
    else:
        print("  All packages already installed.")

    # ── 2. eccodes check ──────────────────────────────────────────
    print("\n[2/3] Checking eccodes (GRIB2 support) ...")
    try:
        import cfgrib  # noqa
        print("  cfgrib + eccodes: OK")
    except Exception as e:
        print(f"  [!] cfgrib/eccodes issue: {e}")
        print("  If you only have NetCDF files, this is safe to ignore.")
        print("  To fix: conda install -c conda-forge eccodes  OR")
        print("          winget install ecmwf.eccodes")

    # ── 3. Save credentials ───────────────────────────────────────
    print("\n[3/3] RDS Portal Credentials")
    print("  Register at: https://rds.ncmrwf.gov.in")
    save = input("  Save credentials now? (y/N): ").strip().lower()
    if save == "y":
        email = input("  Email    : ").strip()
        pwd   = getpass.getpass("  Password : ")
        CREDS_FILE.write_text(json.dumps({"email": email, "password": pwd}, indent=2))
        CREDS_FILE.chmod(0o600)
        print(f"  Saved to {CREDS_FILE}")
    else:
        print("  Skipped. Set NCMRWF_EMAIL + NCMRWF_PASSWORD env vars before running pipeline.")

    print("\n" + "=" * 60)
    print("  Setup complete! Next steps:")
    print()
    print("  A) One-off run (local file you already downloaded):")
    print("     python ncmrwf_pipeline.py --local-file <path/to/forecast.nc>")
    print()
    print("  B) Automated download + convert + deploy:")
    print("     python ncmrwf_pipeline.py --date 20261029 --cycle 00")
    print()
    print("  C) Schedule daily auto-run (Windows):")
    print("     Run schedule_ncmrwf.bat as Administrator")
    print("=" * 60)

if __name__ == "__main__":
    main()
