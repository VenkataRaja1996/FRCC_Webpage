"""
Universal signal disabler for Robot Framework files.

Usage:
    python disable_testscript_signals.py <robot_file_or_folder> [more_paths ...]

What it does:
    1. Parses one or more .robot files (or folders containing .robot files)
       and extracts all unique signal names used in vcu_send_signal_update calls.
    2. Sends value 0 for every unique signal via VehicleSimHelper.
    3. Appends a TC_Disable_Signals test case to each robot file
       (only once per file — skips if already present).
"""

import sys
import os
import re
import time

# Resolve the package root so imports work when run standalone or from a runner.
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.join(SCRIPT_DIR, "gmtools_rtostestautomation")
sys.path.insert(0, PROJECT_ROOT)

TC_NAME = "TC_Disable_Signals"


def normalize_robot_files(args):
    """Return ordered unique list of valid .robot files from command-line args."""
    files = []
    seen = set()

    for raw_arg in args:
        arg = raw_arg.strip().strip('"')
        if not arg:
            continue

        if os.path.isdir(arg):
            for root, _, filenames in os.walk(arg):
                for name in filenames:
                    if not name.lower().endswith(".robot"):
                        continue
                    path = os.path.abspath(os.path.join(root, name))
                    if path not in seen:
                        seen.add(path)
                        files.append(path)
            continue

        if not arg.lower().endswith(".robot"):
            print(f"[WARN] Skipping non-.robot input: {raw_arg}")
            continue

        if not os.path.isfile(arg):
            print(f"[WARN] File not found, skipping: {raw_arg}")
            continue

        path = os.path.abspath(arg)
        if path not in seen:
            seen.add(path)
            files.append(path)

    return files

# ---------------------------------------------------------------
# Step 1 — Parse signal names from robot file
# ---------------------------------------------------------------
def extract_signals(robot_file):
    """Return ordered list of unique signal names from vcu_send_signal_update calls."""
    pattern = re.compile(
        r'(?i)vcu[_ ]send[_ ]signal[_ ]update\s+(\S+)',
        re.IGNORECASE
    )
    seen = set()
    signals = []
    with open(robot_file, "r", encoding="utf-8") as f:
        for line in f:
            match = pattern.search(line)
            if match:
                name = match.group(1)
                if name not in seen:
                    seen.add(name)
                    signals.append(name)
    return signals


# ---------------------------------------------------------------
# Step 2 — Disable signals via VehicleSimHelper
# ---------------------------------------------------------------
def disable_signals(signals):
    from gmtools_common_functions.RTOS.rtos_keywords.vehicle_sim_keywords import (
        initialize_vehicle_sim,
        vcu_send_signal_update,
    )

    print("Initializing Vehicle Simulator...")
    vehicle_sim = initialize_vehicle_sim()
    connection_deadline = time.monotonic() + 15
    while not vehicle_sim.is_connected() and time.monotonic() < connection_deadline:
        time.sleep(0.1)

    if not vehicle_sim.is_connected():
        print("ERROR: Vehicle Simulator did not connect within 15 seconds.")
        return False

    print(f"\nDisabling {len(signals)} signal(s)...\n")
    all_ok = True
    for signal in signals:
        result = vcu_send_signal_update(signal, 0)
        status = "OK" if result else "FAILED"
        if not result:
            all_ok = False
        print(f"  [{status}]  {signal} = 0")

    print()
    if all_ok:
        print("All signals disabled successfully.")
    else:
        print("Some signals FAILED — check VehicleSimHelper connection.")
    return all_ok


# ---------------------------------------------------------------
# Step 3 — Append disable test case to robot file
# ---------------------------------------------------------------
def build_test_case_block(signals, robot_file):
    suite_name = os.path.splitext(os.path.basename(robot_file))[0]
    doc = f"Disable all signals used in {suite_name} by setting them to 0."
    lines = [
        "",
        TC_NAME,
        f"    [Documentation]    {doc}",
    ]
    for signal in signals:
        lines.append(f"    vcu_send_signal_update    {signal}    0")
    return "\n".join(lines) + "\n"


def append_test_case(robot_file, signals):
    with open(robot_file, "r", encoding="utf-8") as f:
        content = f.read()

    if TC_NAME in content:
        print(f"[INFO] '{TC_NAME}' already exists in robot file. Skipping append.")
        return

    tc_block = build_test_case_block(signals, robot_file)
    with open(robot_file, "a", encoding="utf-8") as f:
        f.write(tc_block)

    print(f"[OK] Appended '{TC_NAME}' to:\n  {robot_file}")


def process_robot_file(robot_file):
    print("=" * 60)
    print(f"  Robot file : {os.path.basename(robot_file)}")
    print("=" * 60)

    signals = extract_signals(robot_file)
    if not signals:
        print("No vcu_send_signal_update calls found in this file. Skipping append.\n")
        return []

    print(f"\nFound {len(signals)} unique signal(s):")
    for signal in signals:
        print(f"  - {signal}")

    print("\nAppending disable test case to robot file...")
    append_test_case(robot_file, signals)
    print()
    return signals


# ---------------------------------------------------------------
# Main
# ---------------------------------------------------------------
def main():
    if len(sys.argv) < 2:
        print("ERROR: No robot file(s) specified.")
        print(
            f"Usage: python {os.path.basename(__file__)} <path_to_robot_file> "
            "[more_robot_files ...]"
        )
        sys.exit(1)

    robot_files = normalize_robot_files(sys.argv[1:])
    if not robot_files:
        print("ERROR: No valid .robot files found in inputs.")
        sys.exit(1)

    print(f"Processing {len(robot_files)} robot file(s)...\n")

    all_signals = []
    seen_signals = set()
    for robot_file in robot_files:
        file_signals = process_robot_file(robot_file)
        for signal in file_signals:
            if signal not in seen_signals:
                seen_signals.add(signal)
                all_signals.append(signal)

    if not all_signals:
        print("No vcu_send_signal_update calls found in any robot files. Nothing to disable.")
        sys.exit(0)

    print("=" * 60)
    print("  Combined signal disable")
    print("=" * 60)
    print(f"Total unique signals across all files: {len(all_signals)}\n")

    if not disable_signals(all_signals):
        print("ERROR: One or more signals could not be disabled.")
        sys.exit(1)

    print("\nDone.")


if __name__ == "__main__":
    main()

