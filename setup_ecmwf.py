#!/usr/bin/env python3
"""
setup_ecmwf.py  —  Setup helper for ECMWF IFS automation.

Installs required dependencies for ECMWF Open Data & GRIB2/NetCDF processing:
  pip install ecmwf-opendata cfgrib eccodes netCDF4 xarray tqdm cdsapi
"""

import subprocess
import sys

PACKAGES = [
    "ecmwf-opendata",
    "netCDF4",
    "xarray",
    "tqdm",
    "cdsapi",
    "cfgrib",
    "eccodes",
]

def main():
    print("=" * 60)
    print("  ECMWF Forecast Pipeline — Environment Setup")
    print("=" * 60)
    print(f"\nInstalling packages: {', '.join(PACKAGES)} ...\n")
    
    cmd = [sys.executable, "-m", "pip", "install", "--upgrade"] + PACKAGES
    res = subprocess.run(cmd)
    
    if res.returncode == 0:
        print("\n[OK] All Python packages installed successfully!")
    else:
        print("\n[!] Note: If eccodes/cfgrib fails on Windows, install via conda:")
        print("      conda install -c conda-forge eccodes cfgrib")
        print("    or using winget:")
        print("      winget install ecmwf.eccodes")
    
    print("\nNext steps:")
    print("  1. Automated test run (latest ECMWF IFS forecast, 72 hours, India):")
    print("     python ecmwf_pipeline.py")
    print("  2. Daily automation via Windows Task Scheduler:")
    print("     Run schedule_ecmwf.bat as Administrator")
    print("=" * 60)

if __name__ == "__main__":
    main()

