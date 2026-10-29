#!/usr/bin/env python3
"""
ncmrwf_to_json.py  —  Convert NCMRWF 9 km NWP forecast files to HydroClim Cast JSON format.

Supported input formats
-----------------------
  • NetCDF4 / Classic NetCDF   (NCMRWF NEPS, IMDAA, UM outputs)
  • GRIB2                      (requires cfgrib + eccodes)

Produced output
---------------
  A JSON file compatible with the HydroClim Cast viewer (index.html).
  Schema:
    {
      "metadata": {
        "source": "NCMRWF_9km",
        "model":  "<model name>",
        "extent": [lon_min, lat_min, lon_max, lat_max],
        "grid_dimensions": {"width": N, "height": M},
        "resolution_km": 9,
        "total_timesteps": T
      },
      "timesteps": [
        {
          "time": "YYYY-MM-DD_HH:MM:SS",
          "data": {
            "temperature":        [float, ...],   # °C, row-major S→N then W→E
            "precipitation":      [float, ...],   # mm  (accumulated → interval)
            "wind_speed":         [float, ...],   # m/s
            "specific_humidity":  [float, ...]    # kg/kg
          }
        }, ...
      ]
    }

Usage
-----
  # NetCDF
  python ncmrwf_to_json.py --input forecast.nc --output ncmrwf_9km_india.json

  # GRIB2
  python ncmrwf_to_json.py --input forecast.grb2 --format grib2 --output ncmrwf_9km_india.json

  # Crop to a bounding box  (lon_min lat_min lon_max lat_max)
  python ncmrwf_to_json.py --input forecast.nc \
      --bbox 67.0 7.0 98.0 38.0 \
      --output ncmrwf_9km_india.json

  # Limit forecast horizon to first 72 h  (3-hourly → 25 steps)
  python ncmrwf_to_json.py --input forecast.nc --max-steps 25 --output ncmrwf_9km_india.json

Dependencies
------------
  pip install numpy netCDF4 cfgrib eccodes
"""

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

# ──────────────────────────────────────────────────────────────────────────────
# Variable name mappings:  NetCDF / GRIB short-name → internal key
# Extend these lists if your files use different variable names.
# ──────────────────────────────────────────────────────────────────────────────

NETCDF_VAR_MAP = {
    # temperature (K → °C conversion applied automatically)
    "temperature":       ["T2", "t2m", "tas", "TMP_2maboveground", "temp2m", "T2M", "temperature"],
    # precipitation (kg m-2 = mm; accumulated → interval diff applied)
    "precipitation":     ["RAIN", "RAINNC", "rain", "pr", "tp", "RAIN_TOT", "PREC", "precipitation"],
    # 10-m wind speed (m/s; or computed from U10/V10)
    "wind_speed":        ["WSPD10", "ws10", "si10", "wind_speed", "WIND10"],
    # U/V components if wind_speed is absent
    "_u10":              ["U10", "u10", "uas", "U10M"],
    "_v10":              ["V10", "v10", "vas", "V10M"],
    # 2-m specific humidity (kg/kg)
    "specific_humidity": ["Q2", "q2m", "hus", "SH2", "specific_humidity", "Q2M"],
}

GRIB_SHORT_NAME_MAP = {
    "2t":   "temperature",
    "t2m":  "temperature",
    "tp":   "precipitation",
    "10u":  "_u10",
    "10v":  "_v10",
    "2sh":  "specific_humidity",
    "q":    "specific_humidity",
}

# ──────────────────────────────────────────────────────────────────────────────
def _to_celsius(arr, unit_hint: str):
    """Convert temperature array to °C if it looks like Kelvin."""
    if unit_hint and "K" in unit_hint and "°" not in unit_hint:
        return arr - 273.15
    # heuristic: median > 150 → assume Kelvin
    try:
        import numpy as np
        if float(np.nanmedian(arr)) > 150:
            return arr - 273.15
    except Exception:
        pass
    return arr


def _precip_interval(arr_2d):
    """
    Convert cumulative precipitation (time × grid) to per-interval amounts.
    Negative diffs (model re-initialisation) are zeroed.
    """
    import numpy as np
    diff = np.diff(arr_2d, axis=0, prepend=arr_2d[[0]])
    diff[diff < 0] = 0.0
    return diff


def _wind_speed(u, v):
    """Compute wind speed magnitude from U/V components."""
    import numpy as np
    return np.sqrt(u**2 + v**2)


# ──────────────────────────────────────────────────────────────────────────────
#  NetCDF reader
# ──────────────────────────────────────────────────────────────────────────────

def _find_nc_var(nc, candidates):
    """Return the first matching variable name present in nc, or None."""
    for c in candidates:
        if c in nc.variables:
            return c
    return None


def _nc_latlon(nc):
    """Return (lat_1d, lon_1d) arrays from a NetCDF file."""
    import numpy as np
    lat_names = ["lat", "latitude", "XLAT", "y", "rlat"]
    lon_names = ["lon", "longitude", "XLONG", "x", "rlon"]
    lat_var = _find_nc_var(nc, lat_names)
    lon_var = _find_nc_var(nc, lon_names)
    if not lat_var or not lon_var:
        raise ValueError(
            f"Cannot find lat/lon variables. Available: {list(nc.variables.keys())}"
        )
    lat = np.array(nc.variables[lat_var][:]).squeeze()
    lon = np.array(nc.variables[lon_var][:]).squeeze()
    # Handle 2-D lat/lon grids (e.g. WRF rotated grids)
    if lat.ndim == 2:
        lat = lat[:, 0]
    if lon.ndim == 2:
        lon = lon[0, :]
    return lat, lon


def _nc_times(nc):
    """Return list of ISO time strings from NetCDF time variable."""
    import numpy as np
    time_names = ["time", "Time", "XTIME", "forecast_time"]
    tvar_name = _find_nc_var(nc, time_names)
    if tvar_name is None:
        return [f"step_{i:04d}" for i in range(1000)]
    tvar = nc.variables[tvar_name]
    try:
        import netCDF4 as nc4
        times = nc4.num2date(tvar[:], tvar.units)
        return [t.strftime("%Y-%m-%d_%H:%M:%S") for t in times]
    except Exception:
        return [str(v) for v in np.array(tvar[:])]


def read_netcdf(path: Path, bbox=None, max_steps=None):
    """Read NCMRWF NetCDF forecast; return (metadata_dict, timesteps_list)."""
    try:
        import netCDF4 as nc4
    except ImportError:
        sys.exit("ERROR: netCDF4 not installed.  Run:  pip install netCDF4")
    import numpy as np

    print(f"[ncmrwf_to_json] Opening NetCDF: {path}")
    nc = nc4.Dataset(path)

    lat, lon = _nc_latlon(nc)
    time_strs = _nc_times(nc)

    # ── Crop to bounding box ──────────────────────────────────────────────────
    if bbox:
        lon_min, lat_min, lon_max, lat_max = bbox
        lon_mask = (lon >= lon_min) & (lon <= lon_max)
        lat_mask = (lat >= lat_min) & (lat <= lat_max)
        lon = lon[lon_mask]
        lat = lat[lat_mask]
    else:
        lon_mask = lat_mask = None

    height, width = len(lat), len(lon)
    if max_steps:
        time_strs = time_strs[:max_steps]

    print(f"[ncmrwf_to_json]  Grid  : {width}×{height}  ({width*height} points)")
    print(f"[ncmrwf_to_json]  Steps : {len(time_strs)}")

    # ── Resolve variable names ────────────────────────────────────────────────
    nc_vars = {}
    for key, candidates in NETCDF_VAR_MAP.items():
        vname = _find_nc_var(nc, candidates)
        if vname:
            nc_vars[key] = vname
            print(f"[ncmrwf_to_json]  Found '{key}' → NC variable '{vname}'")

    if "wind_speed" not in nc_vars and "_u10" in nc_vars and "_v10" in nc_vars:
        print("[ncmrwf_to_json]  Computing wind_speed from U10/V10 components")

    # ── Read all data upfront for precipitation differencing ─────────────────
    def _read_var_all(vname):
        v = nc.variables[vname]
        data = np.array(v[:len(time_strs)])  # (T, Y?, X?) or (T, lev, Y, X)
        # Drop pressure-level dimension if present
        if data.ndim == 4:
            data = data[:, 0, :, :]
        # Apply bounding-box masks
        if lat_mask is not None:
            data = data[:, lat_mask, :]
        if lon_mask is not None:
            data = data[:, :, lon_mask]
        return data  # (T, H, W)

    raw = {}
    for key, vname in nc_vars.items():
        try:
            raw[key] = _read_var_all(vname)
        except Exception as e:
            print(f"[ncmrwf_to_json]  WARNING: could not read '{key}': {e}")

    # Derived wind speed
    if "wind_speed" not in raw and "_u10" in raw and "_v10" in raw:
        raw["wind_speed"] = _wind_speed(raw["_u10"], raw["_v10"])

    # Temperature unit conversion
    if "temperature" in raw:
        units = getattr(nc.variables.get(nc_vars.get("temperature", ""), None), "units", "")
        raw["temperature"] = _to_celsius(raw["temperature"], units)

    # Precipitation: accumulated → per interval
    if "precipitation" in raw:
        raw["precipitation"] = _precip_interval(raw["precipitation"])

    nc.close()

    # ── Build output structure ────────────────────────────────────────────────
    keys_out = [k for k in ["temperature", "precipitation", "wind_speed", "specific_humidity"] if k in raw]

    timesteps = []
    for t_idx, t_str in enumerate(time_strs):
        frame_data = {}
        for key in keys_out:
            arr = raw[key][t_idx]          # (H, W)
            # Flatten row-major (south-to-north already if lat is ascending)
            if lat[0] > lat[-1]:           # descending → flip to S→N
                arr = arr[::-1, :]
            frame_data[key] = [round(float(v), 5) for v in arr.flatten()]
        timesteps.append({"time": t_str, "data": frame_data})

    # Ensure S→N lat ordering for extent
    lat_sorted = sorted([float(lat[0]), float(lat[-1])])
    metadata = {
        "source": "NCMRWF_9km",
        "model": "NCMRWF-NEPS-9km",
        "extent": [float(lon[0]), lat_sorted[0], float(lon[-1]), lat_sorted[1]],
        "grid_dimensions": {"width": width, "height": height},
        "resolution_km": 9,
        "total_timesteps": len(timesteps),
        "variables_present": keys_out,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }
    return metadata, timesteps


# ──────────────────────────────────────────────────────────────────────────────
#  GRIB2 reader
# ──────────────────────────────────────────────────────────────────────────────

def read_grib2(path: Path, bbox=None, max_steps=None):
    """Read NCMRWF GRIB2 forecast; return (metadata_dict, timesteps_list)."""
    try:
        import cfgrib
        import xarray as xr
    except ImportError:
        sys.exit("ERROR: cfgrib / xarray not installed.  Run:  pip install cfgrib xarray eccodes")
    import numpy as np

    print(f"[ncmrwf_to_json] Opening GRIB2: {path}")
    raw = {}

    # Read all short-names we care about
    desired = {"2t", "tp", "10u", "10v", "2sh", "q"}
    ds_list = cfgrib.open_datasets(str(path))

    for ds in ds_list:
        for var in ds.data_vars:
            sn = ds[var].attrs.get("GRIB_shortName", var)
            key = GRIB_SHORT_NAME_MAP.get(sn)
            if key is None:
                continue
            arr = ds[var].values  # (T?, Y, X)
            if arr.ndim == 2:
                arr = arr[np.newaxis, ...]
            raw[key] = arr

    # Resolve lat/lon from first dataset
    first_ds = ds_list[0]
    lat = np.array(first_ds.latitude if "latitude" in first_ds else first_ds.lat)
    lon = np.array(first_ds.longitude if "longitude" in first_ds else first_ds.lon)
    if lat.ndim == 2:
        lat = lat[:, 0]
    if lon.ndim == 2:
        lon = lon[0, :]

    # Try to get time strings
    try:
        times_raw = first_ds.valid_time.values
        time_strs = [str(t)[:19].replace("T", "_") for t in times_raw]
    except Exception:
        time_strs = [f"step_{i:04d}" for i in range(list(raw.values())[0].shape[0])]

    # ── Crop ──────────────────────────────────────────────────────────────────
    if bbox:
        lon_min, lat_min, lon_max, lat_max = bbox
        lon_mask = (lon >= lon_min) & (lon <= lon_max)
        lat_mask = (lat >= lat_min) & (lat <= lat_max)
        lon = lon[lon_mask]
        lat = lat[lat_mask]
        for key in list(raw.keys()):
            raw[key] = raw[key][:, lat_mask, :][:, :, lon_mask]

    n_steps = min(len(time_strs), max_steps or 9999)
    time_strs = time_strs[:n_steps]

    # ── Derived / converted fields ─────────────────────────────────────────────
    if "wind_speed" not in raw and "_u10" in raw and "_v10" in raw:
        raw["wind_speed"] = _wind_speed(raw["_u10"], raw["_v10"])
    if "temperature" in raw:
        raw["temperature"] = _to_celsius(raw["temperature"], "K")
    if "precipitation" in raw:
        raw["precipitation"] = _precip_interval(raw["precipitation"])

    # ── Build timesteps ────────────────────────────────────────────────────────
    keys_out = [k for k in ["temperature", "precipitation", "wind_speed", "specific_humidity"] if k in raw]
    height, width = len(lat), len(lon)
    timesteps = []
    for t_idx, t_str in enumerate(time_strs):
        frame_data = {}
        for key in keys_out:
            arr = raw[key][t_idx] if raw[key].shape[0] > t_idx else raw[key][-1]
            if lat[0] > lat[-1]:
                arr = arr[::-1, :]
            frame_data[key] = [round(float(v), 5) for v in arr.flatten()]
        timesteps.append({"time": t_str, "data": frame_data})

    lat_sorted = sorted([float(lat[0]), float(lat[-1])])
    metadata = {
        "source": "NCMRWF_9km",
        "model": "NCMRWF-NEPS-9km",
        "extent": [float(lon[0]), lat_sorted[0], float(lon[-1]), lat_sorted[1]],
        "grid_dimensions": {"width": width, "height": height},
        "resolution_km": 9,
        "total_timesteps": len(timesteps),
        "variables_present": keys_out,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }
    return metadata, timesteps


# ──────────────────────────────────────────────────────────────────────────────
#  Entry point
# ──────────────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Convert NCMRWF 9 km NWP forecast (NetCDF4 or GRIB2) to HydroClim Cast JSON"
    )
    parser.add_argument("--input",   "-i", required=True, help="Input forecast file")
    parser.add_argument("--output",  "-o", required=True, help="Output JSON file")
    parser.add_argument("--format",  "-f", choices=["netcdf", "grib2", "auto"], default="auto",
                        help="Input file format (default: auto-detect from extension)")
    parser.add_argument("--bbox", nargs=4, type=float, metavar=("LON_MIN", "LAT_MIN", "LON_MAX", "LAT_MAX"),
                        default=None,
                        help="Crop to bounding box.  Default for India: 67 7 98 38")
    parser.add_argument("--max-steps", type=int, default=None,
                        help="Maximum number of timesteps to include (e.g. 25 for 72-h at 3-h intervals)")
    parser.add_argument("--indent", type=int, default=None,
                        help="JSON indentation (leave blank for compact/smallest files)")
    args = parser.parse_args()

    src = Path(args.input)
    if not src.exists():
        sys.exit(f"ERROR: input file not found: {src}")

    fmt = args.format
    if fmt == "auto":
        fmt = "grib2" if src.suffix.lower() in (".grb", ".grb2", ".grib", ".grib2") else "netcdf"

    if fmt == "grib2":
        metadata, timesteps = read_grib2(src, bbox=args.bbox, max_steps=args.max_steps)
    else:
        metadata, timesteps = read_netcdf(src, bbox=args.bbox, max_steps=args.max_steps)

    out = {"metadata": metadata, "timesteps": timesteps}
    dst = Path(args.output)
    with open(dst, "w") as f:
        json.dump(out, f, indent=args.indent, separators=(",", ":") if args.indent is None else None)

    size_mb = dst.stat().st_size / 1_048_576
    print(f"[ncmrwf_to_json] Written → {dst}  ({size_mb:.1f} MB, {len(timesteps)} timesteps)")
    print("[ncmrwf_to_json] Done.")


if __name__ == "__main__":
    main()
