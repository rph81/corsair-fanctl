#!/usr/bin/env python3
"""Run the daemon against a simulated Commander Pro.

Builds a fake sysfs tree that looks like the `corsair-cpro` kernel driver plus a
`coretemp` host sensor, then points the hwmon backend at it.  Temperatures react
to the duties the control loop writes, so curves, ramping and hysteresis can all
be exercised on a laptop with no hardware attached.

    python3 tools/devsim.py --port 8377
"""

from __future__ import annotations

import argparse
import os
import random
import sys
import tempfile
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fanctl import backends, sensors  # noqa: E402
from fanctl.__main__ import main  # noqa: E402

FAN_TYPES = {1: "4pin", 2: "4pin", 3: "3pin", 4: "4pin", 5: None, 6: None}
# Which fans cool which Commander Pro probe.
ZONES = {1: [1, 2], 2: [3], 3: [4]}
# Channel 5 mimics a 2-wire fan; channel 6 is genuinely empty.
NO_TACH_FANS = {5}


def write(path: str, value) -> None:
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(str(value))


def build_tree(root: str) -> tuple[str, str]:
    cpro = os.path.join(root, "hwmon0")
    host = os.path.join(root, "hwmon1")
    os.makedirs(cpro, exist_ok=True)
    os.makedirs(host, exist_ok=True)

    write(os.path.join(cpro, "name"), "corsair-cpro")
    for index, kind in FAN_TYPES.items():
        if kind is None:
            # Stand-in for a 2-wire fan the controller cannot sense: a writable
            # pwm attribute but no tach, so no fan*_label and no fan*_input.
            if index in NO_TACH_FANS:
                write(os.path.join(cpro, f"pwm{index}"), 0)
            continue
        write(os.path.join(cpro, f"fan{index}_label"), f"fan{index} {kind}")
        write(os.path.join(cpro, f"fan{index}_input"), 0)
        write(os.path.join(cpro, f"pwm{index}"), 0)
    for probe in (1, 2, 3):
        write(os.path.join(cpro, f"temp{probe}_input"), 30000)
    for slot, millivolts in enumerate((12096, 5040, 3312)):
        write(os.path.join(cpro, f"in{slot}_input"), millivolts)

    write(os.path.join(host, "name"), "coretemp")
    write(os.path.join(host, "temp1_label"), "Package id 0")
    write(os.path.join(host, "temp1_input"), 45000)
    write(os.path.join(host, "temp2_label"), "Core 0")
    write(os.path.join(host, "temp2_input"), 44000)
    return cpro, host


FAKE_ARCCONF = """#!/usr/bin/env python3
import os, random, sys
if sys.argv[1:4] != ["getconfig", "1", "PD"]:
    print("Invalid arguments"); sys.exit(1)
fixture = {fixture!r}
text = open(fixture).read()
# Jitter each drive's temperature so the panel visibly updates.
out = []
for line in text.splitlines():
    if "Current Temperature" in line:
        key, _, _ = line.partition(":")
        out.append(f"{{key}}: {{random.randint(38, 46)}} deg C")
    else:
        out.append(line)
print("\\n".join(out))
"""


def build_fake_arcconf(root: str) -> str:
    """Write a stand-in for the arcconf binary that replays a captured report."""
    fixture = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "fixtures", "arcconf-pd.txt")
    path = os.path.join(root, "fake-arcconf")
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(FAKE_ARCCONF.format(fixture=fixture))
    os.chmod(path, 0o755)
    return path


def read_int(path: str, fallback: int = 0) -> int:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return int(handle.read().strip())
    except (OSError, ValueError):
        return fallback


def simulate(cpro: str, host: str, stop: threading.Event) -> None:
    """First-order thermal model: more airflow pulls each zone toward ambient."""
    temps = {probe: 30.0 for probe in ZONES}
    cpu = 45.0
    load = 0.35

    while not stop.is_set():
        duties = {
            index: read_int(os.path.join(cpro, f"pwm{index}")) / 255.0 * 100.0
            for index in FAN_TYPES if FAN_TYPES[index]
        }

        for index, duty in duties.items():
            # Rotors take a moment to reach the commanded duty, and stall below ~15%.
            target_rpm = 0 if duty < 5 else int(300 + duty * 16 + random.uniform(-25, 25))
            current = read_int(os.path.join(cpro, f"fan{index}_input"))
            write(os.path.join(cpro, f"fan{index}_input"),
                  max(0, int(current + (target_rpm - current) * 0.35)))

        # A slowly wandering workload drives the heat input.
        load = min(1.0, max(0.05, load + random.uniform(-0.06, 0.06)))

        for probe, fans in ZONES.items():
            airflow = sum(duties.get(f, 0.0) for f in fans) / (len(fans) * 100.0)
            equilibrium = 24.0 + 46.0 * load - 22.0 * airflow
            temps[probe] += (equilibrium - temps[probe]) * 0.12
            write(os.path.join(cpro, f"temp{probe}_input"),
                  int(round(temps[probe] * 1000)))

        cpu += ((38.0 + 45.0 * load) - cpu) * 0.2
        write(os.path.join(host, "temp1_input"), int(round(cpu * 1000)))
        write(os.path.join(host, "temp2_input"), int(round((cpu - 1.5) * 1000)))

        stop.wait(1.0)


def run() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8377)
    parser.add_argument("--bind", default="127.0.0.1")
    parser.add_argument("--config", default=None)
    parser.add_argument("--log-level", default="info")
    args = parser.parse_args()

    root = tempfile.mkdtemp(prefix="fanctl-sim-")
    cpro, host = build_tree(root)
    backends.HWMON_ROOT = root
    sensors.HWMON_ROOT = root
    print(f"simulated sysfs at {root}", flush=True)

    stop = threading.Event()
    thread = threading.Thread(target=simulate, args=(cpro, host, stop), daemon=True)
    thread.start()

    config_path = args.config or os.path.join(root, "config.json")

    # storage.command cannot be set over the API, so seed it in the config file
    # exactly the way a real operator would.
    from fanctl import config as config_module
    seeded = config_module.default_config()
    seeded["storage"]["command"] = build_fake_arcconf(root)
    seeded["storage"]["interval"] = 10.0
    config_module.save(config_path, seeded)

    try:
        return main([
            "--config", config_path,
            "--bind", args.bind,
            "--port", str(args.port),
            "--log-level", args.log_level,
        ])
    finally:
        stop.set()


if __name__ == "__main__":
    sys.exit(run())
