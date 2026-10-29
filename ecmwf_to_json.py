#!/usr/bin/env python3
"""
ecmwf_to_json.py  —  Convert ECMWF IFS forecast files to HydroClim Cast JSON format.

Supported input formats
-----------------------
  • GRIB2  (ECMWF open-data, CDS download)   — requires cfgrib + eccodes
  • NetCDF4 (CDS / ERA5 downloads)            — requires netCDF4

ECMWF GRIB2 short-name → internal key mapping
-----------------------------------------------
  2t   → temperature        (K  → °C)
  tp   → precipitation      (m  → mm, accumulated → per-interval)
  10u  → _u10  ┐ combined
  10v  → _v10  ┘ → wind_speed  (m/s)
  2d   → _dewpoint  → specific_humidity  (kg/kg, via Magnus formula)
  q    → specific_humidity  (kg/kg, direct)

Output JSON schema  (identical to WRF output — plug-in compatible)
-------------------------------------------------------------------
  {
    "metadata": {
      "source": "ECMWF_IFS",
      "model":  "IFS",
      "resolution": "0.25deg",
      "extent": [lon_min, lat_min, lon_max, lat_max],
      "grid_dimensions": {"width": N, "height": M},
      "total_timesteps": T
    },
    "timesteps": [
      { "time": "YYYY-MM-DD_HH:MM:SS",
        "data": { "temperature": [...], "precipitation": [...],
                  "wind_speed": [...],  "specific_humidity": [...] } }
    ]
  }

Usage
-----
  # GRIB2 (ecmwf-opendata download)
  python ecmwf_to_json.py --input forecast.grib2 --output ecmwf_ifs_india.json

  # NetCDF from CDS
  python ecmwf_to_json.py --input era5_fc.nc --output ecmwf_ifs_india.json

  # Crop to India bounding box
  python ecmwf_to_json.py --input forecast.grib2 --bbox 67 7 98 38

  # First 72 h only  (3-hourly → 25 steps)
  python ecmwf_to_json.py --input forecast.grib2 --max-steps 25
"""

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path


# ──────────────────────────────────────────────────────────────────────────────
# Variable maps
# ──────────────────────────────────────────────────────────────────────────────

# ECMWF GRIB2 shortName → our key
GRIB_MAP = {
    "2t":   "temperature",
    "t2m":  "temperature",
    "tp":   "precipitation",
    "10u":  "_u10",
    "10v":  "_v10",
    "2d":   "_dewpoint",
    "q":    "specific_humidity",
    "sh":   "specific_humidity",
    "2sh":  "specific_humidity",
    # Pressure, Pa in both sources -> hPa (see _finalise)
    "msl":    "pressure",     # ECMWF
    "prmsl":  "pressure",     # GFS
    # Cloud cover: ECMWF tcc is a 0-1 fraction, GFS tcc is already % (see --cloud-unit)
    "tcc":    "cloud_cover",
    # CAPE/CIN: GFS only — not published in ECMWF open-data
    "cape":   "cape",
    "cin":    "cin",
    # Solar: ECMWF ssrd is accumulated J/m^2 (diffed -> W/m^2, see _finalise);
    # GFS sdswrf is already an interval-average W/m^2 flux, used as-is.
    "ssrd":   "_solar_accum",
    "sdswrf": "solar_radiation",
}

# NetCDF variable name candidates → our key
NETCDF_MAP = {
    "temperature":       ["t2m", "T2", "tas", "temperature", "2m_temperature"],
    "precipitation":     ["tp",  "pr", "rain", "RAIN", "total_precipitation", "precipitation"],
    "wind_speed":        ["ws",  "wind_speed", "si10"],
    "_u10":              ["u10", "U10", "uas", "10u"],
    "_v10":              ["v10", "V10", "vas", "10v"],
    "specific_humidity": ["q2m", "Q2", "hus", "sh", "specific_humidity", "q"],
    "_dewpoint":         ["d2m", "2d",  "dewpoint_temperature"],
}


# ──────────────────────────────────────────────────────────────────────────────
# Shared utilities
# ──────────────────────────────────────────────────────────────────────────────

def _to_celsius(arr, unit_hint=""):
    """Convert temperature to °C if it looks like Kelvin."""
    import numpy as np
    if unit_hint and "K" in unit_hint and "°" not in unit_hint:
        return arr - 273.15
    if float(np.nanmedian(arr)) > 150:
        return arr - 273.15
    return arr


def _precip_to_mm_interval(arr_3d, unit="m"):
    """
    Accumulated precipitation → per-interval millimetres.
    arr_3d shape: (T, H, W). unit: "m" (ECMWF tp) or "mm" (GFS/ICON APCP,
    already kg/m**2 == mm).
    """
    import numpy as np
    mm = arr_3d * 1000.0 if unit == "m" else arr_3d
    diff = np.diff(mm, axis=0, prepend=mm[[0]])   # per-interval
    diff[diff < 0] = 0.0
    return diff


def _wind_speed(u, v):
    import numpy as np
    return np.sqrt(u ** 2 + v ** 2)


def _dewpoint_to_q(td_k, p_pa=101325.0):
    """
    Convert 2-m dewpoint temperature (K) to specific humidity (kg/kg)
    using the Magnus formula (Alduchov & Eskridge 1996).
    """
    import numpy as np
    td_c = td_k - 273.15
    e_s  = 6.1078 * np.exp(17.2694 * td_c / (td_c + 237.29))  # hPa
    e_s_pa = e_s * 100.0
    q = 0.622 * e_s_pa / (p_pa - 0.378 * e_s_pa)
    return np.clip(q, 0, None)


def _flip_ns(arr_2d, lat):
    """Flip grid S→N if lat array is descending (N→S)."""
    if lat[0] > lat[-1]:
        return arr_2d[::-1, :]
    return arr_2d


def _pressure_pa_to_hpa(arr_3d):
    return arr_3d / 100.0


def _cloud_cover_to_pct(arr_3d, unit="percent"):
    """ECMWF tcc is a 0-1 fraction ("fraction"); GFS tcc is already 0-100 ("percent")."""
    return arr_3d * 100.0 if unit == "fraction" else arr_3d


def _solar_accum_to_wm2(arr_3d, time_strs):
    """
    Accumulated solar radiation (J/m^2, since forecast start) -> per-interval
    average flux (W/m^2). Interval length is derived from consecutive
    timestep labels so it works for any step cadence (3h IFS, 6h AIFS, ...).
    arr_3d shape: (T, H, W).
    """
    import numpy as np
    diff = np.diff(arr_3d, axis=0, prepend=arr_3d[[0]])
    diff[diff < 0] = 0.0
    if len(time_strs) >= 2:
        t0 = datetime.strptime(time_strs[0], "%Y-%m-%d_%H:%M:%S")
        t1 = datetime.strptime(time_strs[1], "%Y-%m-%d_%H:%M:%S")
        interval_s = max((t1 - t0).total_seconds(), 1.0)
    else:
        interval_s = 3 * 3600.0
    return diff / interval_s


def _build_metadata(lon, lat, n_steps, source="ECMWF_IFS", model="IFS"):
    lats = sorted([float(lat[0]), float(lat[-1])])
    return {
        "source":          source,
        "model":           model,
        "resolution":      "0.25deg",
        "extent":          [float(lon[0]), lats[0], float(lon[-1]), lats[1]],
        "grid_dimensions": {"width": len(lon), "height": len(lat)},
        "total_timesteps": n_steps,
        "generated_at":    datetime.now(timezone.utc).isoformat(),
    }


# ──────────────────────────────────────────────────────────────────────────────
# GRIB2 reader  (ecmwf-opendata files or CDS GRIB downloads)
# ──────────────────────────────────────────────────────────────────────────────

def read_grib2(path: Path, bbox=None, max_steps=None, precip_unit="m",
               source_tag="ECMWF_IFS", model_tag="IFS", cloud_unit="percent"):
    try:
        import cfgrib
        import numpy as np
    except ImportError:
        sys.exit("ERROR: cfgrib/eccodes not installed.  Run: pip install cfgrib eccodes")

    print(f"[ecmwf_to_json] Opening GRIB2: {path}")

    raw = {}          # our_key → np.ndarray (T, H, W)
    time_strs = None
    lat = lon = None

    for ds in cfgrib.open_datasets(str(path)):
        for var in list(ds.data_vars):
            sn = str(ds[var].attrs.get("GRIB_shortName", var))
            key = GRIB_MAP.get(sn)
            if key is None:
                continue
            arr = ds[var].values
            if arr.ndim == 2:
                arr = arr[np.newaxis, ...]   # (1, H, W)

            # Gather lat/lon once
            if lat is None:
                for coord in ("latitude", "lat"):
                    if coord in ds.coords:
                        lat = np.array(ds.coords[coord])
                        if lat.ndim == 2:
                            lat = lat[:, 0]
                        break
                for coord in ("longitude", "lon"):
                    if coord in ds.coords:
                        lon = np.array(ds.coords[coord])
                        if lon.ndim == 2:
                            lon = lon[0, :]
                        break

            # Gather valid_time once
            if time_strs is None and "valid_time" in ds.coords:
                vt = np.atleast_1d(ds.coords["valid_time"].values)
                time_strs = [str(t)[:19].replace("T", "_") for t in vt]

            if key not in raw or arr.shape[0] > raw[key].shape[0]:
                raw[key] = arr

    if lat is None or lon is None:
        sys.exit("ERROR: could not extract lat/lon from GRIB2 file.")

    return _finalise(raw, lat, lon, time_strs, bbox, max_steps,
                      precip_unit, source_tag, model_tag, cloud_unit)


# ──────────────────────────────────────────────────────────────────────────────
# NetCDF4 reader  (CDS / ERA5 downloads)
# ──────────────────────────────────────────────────────────────────────────────

def read_netcdf(path: Path, bbox=None, max_steps=None, precip_unit="m",
                 source_tag="ECMWF_IFS", model_tag="IFS", cloud_unit="percent"):
    try:
        import netCDF4 as nc4
        import numpy as np
    except ImportError:
        sys.exit("ERROR: netCDF4 not installed.  Run: pip install netCDF4")

    print(f"[ecmwf_to_json] Opening NetCDF: {path}")
    nc = nc4.Dataset(path)

    # lat / lon
    lat_names = ["lat", "latitude", "y"]
    lon_names = ["lon", "longitude", "x"]
    lat_v = next((v for v in lat_names if v in nc.variables), None)
    lon_v = next((v for v in lon_names if v in nc.variables), None)
    if not lat_v or not lon_v:
        sys.exit(f"ERROR: lat/lon not found. Variables: {list(nc.variables)}")
    lat = np.array(nc.variables[lat_v][:]).squeeze()
    lon = np.array(nc.variables[lon_v][:]).squeeze()

    # time
    time_strs = None
    for tn in ["time", "valid_time"]:
        if tn in nc.variables:
            try:
                times = nc4.num2date(nc.variables[tn][:], nc.variables[tn].units)
                time_strs = [t.strftime("%Y-%m-%d_%H:%M:%S") for t in times]
            except Exception:
                pass
            break

    # variables
    raw = {}
    for our_key, candidates in NETCDF_MAP.items():
        for c in candidates:
            if c in nc.variables:
                arr = np.array(nc.variables[c][:])
                if arr.ndim == 2:
                    arr = arr[np.newaxis, ...]
                if arr.ndim == 4:
                    arr = arr[:, 0, :, :]
                raw[our_key] = arr
                print(f"[ecmwf_to_json]  {our_key} <- NC var '{c}'")
                break
    nc.close()
    return _finalise(raw, lat, lon, time_strs, bbox, max_steps,
                      precip_unit, source_tag, model_tag, cloud_unit)


# ──────────────────────────────────────────────────────────────────────────────
# Shared finalisation
# ──────────────────────────────────────────────────────────────────────────────

def _finalise(raw, lat, lon, time_strs, bbox, max_steps, precip_unit="m",
              source_tag="ECMWF_IFS", model_tag="IFS", cloud_unit="percent"):
    import numpy as np

    # ── Bounding-box crop ─────────────────────────────────────────────────────
    if bbox:
        lon_min, lat_min, lon_max, lat_max = bbox
        lon_mask = (lon >= lon_min) & (lon <= lon_max)
        lat_mask = (lat >= lat_min) & (lat <= lat_max)
        for k in list(raw.keys()):
            raw[k] = raw[k][:, lat_mask, :][:, :, lon_mask]
        lat = lat[lat_mask]
        lon = lon[lon_mask]

    n_steps = min(list(raw.values())[0].shape[0], max_steps or 9999)
    if time_strs:
        time_strs = time_strs[:n_steps]
    else:
        time_strs = [f"step_{i:04d}" for i in range(n_steps)]

    # ── Derived fields ────────────────────────────────────────────────────────
    if "wind_speed" not in raw and "_u10" in raw and "_v10" in raw:
        raw["wind_speed"] = _wind_speed(raw["_u10"], raw["_v10"])

    if "temperature" in raw:
        # NetCDF units attr unavailable here; use heuristic
        raw["temperature"] = _to_celsius(raw["temperature"])

    if "precipitation" in raw:
        raw["precipitation"] = _precip_to_mm_interval(raw["precipitation"], precip_unit)

    if "specific_humidity" not in raw and "_dewpoint" in raw:
        raw["specific_humidity"] = _dewpoint_to_q(raw["_dewpoint"])

    if "pressure" in raw:
        raw["pressure"] = _pressure_pa_to_hpa(raw["pressure"])

    if "cloud_cover" in raw:
        raw["cloud_cover"] = _cloud_cover_to_pct(raw["cloud_cover"], cloud_unit)

    if "_solar_accum" in raw:
        raw["solar_radiation"] = _solar_accum_to_wm2(raw["_solar_accum"], time_strs)

    # ── Build output ──────────────────────────────────────────────────────────
    keys_out = [k for k in ["temperature", "precipitation", "wind_speed", "specific_humidity",
                             "pressure", "cloud_cover", "cape", "cin", "solar_radiation"]
                if k in raw]
    print(f"[ecmwf_to_json]  Grid : {len(lon)}x{len(lat)}   Steps: {n_steps}   Vars: {keys_out}")

    timesteps = []
    for t in range(n_steps):
        frame = {}
        for key in keys_out:
            arr = raw[key][t]
            arr = _flip_ns(arr, lat)
            frame[key] = [round(float(v), 5) for v in arr.flatten()]
        timesteps.append({"time": time_strs[t], "data": frame})

    metadata = _build_metadata(lon, lat, n_steps, source=source_tag, model=model_tag)
    metadata["variables_present"] = keys_out
    return metadata, timesteps


# ──────────────────────────────────────────────────────────────────────────────
# Entry point
# ──────────────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(
        description="Convert ECMWF IFS GRIB2/NetCDF forecast to HydroClim Cast JSON"
    )
    ap.add_argument("--input",     "-i", required=True)
    ap.add_argument("--output",    "-o", default=None)
    ap.add_argument("--bbox",      nargs=4, type=float,
                    metavar=("LON_MIN", "LAT_MIN", "LON_MAX", "LAT_MAX"), default=None)
    ap.add_argument("--max-steps", type=int, default=None)
    ap.add_argument("--indent",    type=int, default=None)
    ap.add_argument("--source-tag", default="ECMWF_IFS",
                    help="metadata.source value, e.g. ECMWF_IFS, NOAA_GFS")
    ap.add_argument("--model-tag",  default="IFS",
                    help="metadata.model value, e.g. IFS, AIFS, GFS")
    ap.add_argument("--precip-unit", choices=["m", "mm"], default="m",
                    help="Input precipitation unit before conversion to mm "
                         "(ECMWF tp: metres: 'm' [default]; GFS/ICON APCP: already mm: 'mm')")
    ap.add_argument("--cloud-unit", choices=["fraction", "percent"], default="percent",
                    help="Input cloud cover unit before conversion to %% "
                         "(ECMWF tcc: 0-1 fraction: 'fraction'; GFS TCDC: already %%: 'percent' [default])")
    args = ap.parse_args()

    src = Path(args.input)
    if not src.exists():
        sys.exit(f"ERROR: file not found: {src}")

    out = Path(args.output) if args.output else src.with_suffix(".json")

    is_grib = src.suffix.lower() in (".grb", ".grb2", ".grib", ".grib2")
    reader = read_grib2 if is_grib else read_netcdf
    metadata, timesteps = reader(src, args.bbox, args.max_steps,
                                  args.precip_unit, args.source_tag, args.model_tag, args.cloud_unit)

    payload = {"metadata": metadata, "timesteps": timesteps}
    with open(out, "w") as f:
        json.dump(payload, f, indent=args.indent,
                  separators=(",", ":") if args.indent is None else None)

    size_mb = out.stat().st_size / 1_048_576
    print(f"[ecmwf_to_json] Written: {out}  ({size_mb:.1f} MB, {len(timesteps)} timesteps)")


if __name__ == "__main__":
    main()

