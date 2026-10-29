#!/usr/bin/env python3
"""
forecast_common.py — shared plumbing for every model pipeline
================================================================
Logging, index.html patching (zone-scoped), GRIB→JSON conversion,
and git commit/push. Used by ecmwf_pipeline.py (IFS/AIFS) and
gfs_pipeline.py (GFS). A new model pipeline only needs its own
download step — everything past "I have a GRIB2 file" is here.
"""

import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path

REPO_ROOT  = Path(__file__).parent.resolve()
CONVERTER  = REPO_ROOT / "ecmwf_to_json.py"
INDEX_HTML = REPO_ROOT / "index.html"

# All index.html zones, with their W S E N bbox. Shared across every model —
# "--zone all" downloads once and converts for every zone here.
ZONE_BBOXES = {
    "india_subcontinent": [67.0, 7.0, 98.0, 38.0],
    "west_india":         [67.0, 8.0, 78.0, 30.0],
    "east_india":         [80.0, 8.0, 97.0, 27.0],
    "north_india":        [70.0, 27.0, 95.0, 36.0],
}
DEFAULT_ZONE = "india_subcontinent"
DEFAULT_BBOX = ZONE_BBOXES[DEFAULT_ZONE]


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
# Convert to HydroClim Cast JSON
# ──────────────────────────────────────────────────────────────────────────────

def convert_to_json(src_path: Path, json_path: Path, bbox, max_steps: int,
                     source_tag: str, model_tag: str, precip_unit: str = "m",
                     cloud_unit: str = "percent") -> Path:
    log(f"Converting {src_path.name} -> {json_path.name} ...", "STEP")

    if not CONVERTER.exists():
        abort(f"Converter not found: {CONVERTER}")

    cmd = [sys.executable, str(CONVERTER),
           "--input",  str(src_path),
           "--output", str(json_path),
           "--source-tag", source_tag,
           "--model-tag",  model_tag,
           "--precip-unit", precip_unit,
           "--cloud-unit", cloud_unit]
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
# Patch index.html  (scoped to one top-level basin key -> one zone -> years{})
# ──────────────────────────────────────────────────────────────────────────────

def patch_index_html(basin_key: str, json_filename: str, run_key: str,
                      run_label: str, zone: str):
    log(f"Patching {INDEX_HTML.name} ...", "STEP")

    content = INDEX_HTML.read_text(encoding="utf-8")

    new_entry = (
        f'\n                            "{run_key}": {{\n'
        f'                                dataFile: "{json_filename}",\n'
        f'                                label: "{run_label}"\n'
        f'                            }},\n'
    )

    basin_start = content.find(f'"{basin_key}"')
    if basin_start == -1:
        abort(f"'{basin_key}' basin not found in index.html.")

    zone_pos = content.find(f'"{zone}"', basin_start)
    if zone_pos == -1:
        abort(f"Zone '{zone}' not found inside '{basin_key}'.")

    years_pos = content.find('years:', zone_pos)
    if years_pos == -1:
        abort(f"'years:' block not found for zone '{zone}'.")
    years_open = content.find('{', years_pos) + 1
    if years_open == 0:
        abort(f"Could not locate opening brace of 'years:' block for zone '{zone}'.")

    # Find the matching closing brace for this zone's years block (brace
    # counting), so lookup/replacement stays scoped to THIS zone only and
    # never touches another zone or basin that happens to share a run key.
    depth = 1
    years_close = years_open
    for i in range(years_open, len(content)):
        if content[i] == '{':
            depth += 1
        elif content[i] == '}':
            depth -= 1
            if depth == 0:
                years_close = i
                break
    else:
        abort(f"Could not locate closing brace of 'years:' block for zone '{zone}'.")

    years_block = content[years_open:years_close]

    if f'"{run_key}"' in years_block:
        log(f"  Entry '{run_key}' exists — replacing.", "WARN")
        old_re = re.compile(
            rf'\s*"{re.escape(run_key)}":\s*{{[^}}]*}},?\n?', re.DOTALL
        )
        years_block, n = old_re.subn(new_entry, years_block, count=1)
        if n == 0:
            abort(f"Could not locate existing '{run_key}' entry to replace in zone '{zone}'.")
        content = content[:years_open] + years_block + content[years_close:]
    else:
        content = content[:years_open] + new_entry + content[years_open:]

    INDEX_HTML.write_text(content, encoding="utf-8")
    log(f"  Added '{run_key}' to zone '{zone}' of '{basin_key}' in index.html", "OK")


# ──────────────────────────────────────────────────────────────────────────────
# Git commit + push
# ──────────────────────────────────────────────────────────────────────────────

def git_commit_push(json_filenames, commit_label: str):
    log("Git commit + push ...", "STEP")
    if isinstance(json_filenames, str):
        json_filenames = [json_filenames]
    n = len(json_filenames)
    msg = f"data: {commit_label} ({n} zone{'s' if n != 1 else ''})"
    for cmd in [
        ["git", "-C", str(REPO_ROOT), "add", *json_filenames, "index.html"],
        ["git", "-C", str(REPO_ROOT), "commit", "-m", msg],
        ["git", "-C", str(REPO_ROOT), "push"],
    ]:
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode != 0:
            log(f"git: {r.stderr.strip()}", "WARN")
            break
        log(f"  $ {' '.join(cmd[2:])}", "OK")
