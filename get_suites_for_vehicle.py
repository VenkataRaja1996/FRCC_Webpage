"""
Reads vehicle_program from rtos_configuration.py and prints the robot suite
file paths that should be executed for that program.

Suite-to-vehicle mapping is defined in vehicle_suites.json (next to this
script) — no robot files need to be edited.

vehicle_suites.json structure:
  {
    "FCC_ALL":        ["suite_a.robot", ...],   <- fallback for unknown FCC vehicles
    "RCC_ALL":        ["suite_rcc.robot", ...], <- common RCC suites
    "L246_FCC":       ["suite_a.robot", ...],   <- exact suites for L246_FCC
    "BT1YL_FCC":      ["suite_b.robot", ...],
    "BT1ZL_FCC":      ["suite_b.robot", ...],
    "BT1YL_TRIM_FCC": ["suite_b.robot", ...],
    "T1XLF_FCC":      ["suite_c.robot", ...],
    "T1YLF_FCC":      ["suite_c.robot", ...],
    "T1ULF_FCC":      ["suite_c.robot", ...],
    "L234_FCC":       ["suite_d.robot", ...],
    "L234_FCC_RHD":   ["suite_d.robot", ...]
  }

  If the vehicle_program has an entry in vehicle_suites.json, only those
    suites are executed.  If there is no matching entry, the "FCC_ALL" list is
  used as a fallback.

Usage (called by run_regressions.bat):
    python get_suites_for_vehicle.py <suite_dir>
"""

import sys
import os
import re
import json
import argparse
from pathlib import Path

THIS_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_DIR = os.path.join(THIS_DIR, "gmtools_rtostestautomation")
if not os.path.isdir(REPO_DIR):
    REPO_DIR = THIS_DIR

# ---------- 1. Read vehicle_program from rtos_configuration.py ----------
CONFIG_PATH = os.path.join(
    REPO_DIR,
    "RTOSAutomation",
    "src",
    "rtos",
    "rtos_configuration.py",
)

vehicle_program = None
display_size = None
with open(CONFIG_PATH, encoding="utf-8") as f:
    for line in f:
        m = re.match(r"""^\s*vehicle_program\s*=\s*['"](.+)['"]\s*""", line)
        if m:
            vehicle_program = m.group(1).strip()
        m = re.match(r"""^\s*display_size\s*=\s*['"](.+)['"]\s*""", line)
        if m:
            display_size = m.group(1).strip()

if not vehicle_program:
    print("ERROR: vehicle_program not found in rtos_configuration.py", file=sys.stderr)
    sys.exit(1)

# ---------- 2. Parse args and read vehicle_suites.json ----------
_parser = argparse.ArgumentParser(add_help=False)
_parser.add_argument("suite_dir", nargs="?", default=".")
_parser.add_argument("--mapping", default=None, help="Override path to vehicle_suites.json")
_args, _ = _parser.parse_known_args()

suite_dir = _args.suite_dir
MAPPING_PATH = _args.mapping if _args.mapping else os.path.join(THIS_DIR, "vehicle_suites.json")

if not os.path.exists(MAPPING_PATH):
    print(f"ERROR: vehicle_suites.json not found at {MAPPING_PATH}", file=sys.stderr)
    sys.exit(1)

with open(MAPPING_PATH, encoding="utf-8") as f:
    mapping = json.load(f)

allowed_variants = mapping.get("_display_variants", {}).get(display_size, [])
if allowed_variants and vehicle_program not in allowed_variants:
    print(
        f"ERROR: vehicle_program '{vehicle_program}' is not applicable for display_size '{display_size}'",
        file=sys.stderr,
    )
    sys.exit(1)

# ---------- 3. Build ordered, deduplicated suite list ----------
# Use vehicle-specific suites when available; fall back to FCC_ALL for unknown vehicles.
if vehicle_program in mapping:
    selected_suites = mapping[vehicle_program]
else:
    selected_suites = mapping.get("FCC_ALL", [])
    print(
        f"WARNING: vehicle_program '{vehicle_program}' not found in vehicle_suites.json — "
        "falling back to FCC_ALL suites",
        file=sys.stderr,
    )

if vehicle_program.endswith("_FCC"):
    selected_suites = list(selected_suites) + mapping.get("_FCC_SANITY", [])
elif vehicle_program.endswith("_RCC"):
    selected_suites = list(selected_suites) + mapping.get("_RCC_SANITY", [])

# RCC suites are common regression coverage for every configured vehicle.
selected_suites = list(selected_suites) + mapping.get("RCC_ALL", [])

seen = set()
matched = []
for fname in selected_suites:
    if fname in seen:
        continue
    seen.add(fname)
    direct_path = Path(suite_dir) / fname
    candidates = [direct_path] if direct_path.exists() else list(Path(suite_dir).rglob(fname))
    if candidates:
        matched.append(str(candidates[0]))
    else:
        print(f"WARNING: {fname} listed in vehicle_suites.json but not found in {suite_dir}", file=sys.stderr)

# Print one path per line; bat file reads these
for p in matched:
    print(p)
