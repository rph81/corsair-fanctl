"""Temperature sources.

Sensors come from two places and share one flat id namespace so a fan curve can
bind to either:

``cpro:tempN``            one of the four Commander Pro probes
``hwmon:<driver>:tempN``  any Linux hwmon temperature (coretemp, k10temp, nvme,
                          drivetemp, acpitz, ...)

Ids are built from the *driver name* rather than the ``hwmonN`` index, because
the kernel does not guarantee stable hwmon numbering across reboots.  When a
driver appears more than once (two NVMe drives, say) the later ones get a
``#2``, ``#3`` suffix in probe order.
"""

from __future__ import annotations

import glob
import os
import time

HWMON_ROOT = "/sys/class/hwmon"
EXCLUDED_DRIVERS = {"corsair-cpro"}
RESCAN_INTERVAL = 30.0

PRETTY_NAMES = {
    "coretemp": "CPU (Intel)",
    "k10temp": "CPU (AMD)",
    "zenpower": "CPU (AMD)",
    "nvme": "NVMe",
    "drivetemp": "Drive",
    "acpitz": "ACPI",
    "pch_cannonlake": "PCH",
    "pch_skylake": "PCH",
    "iwlwifi_1": "Wi-Fi",
}


def _read(path: str) -> str | None:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return handle.read().strip()
    except OSError:
        return None


class HostSensors:
    """Discovers and reads Linux hwmon temperature inputs."""

    def __init__(self) -> None:
        self._entries: list[dict] = []
        self._last_scan = 0.0

    def scan(self) -> list[dict]:
        """(Re)enumerate hwmon temperature inputs. Cheap enough to call often."""
        entries: list[dict] = []
        seen_drivers: dict[str, int] = {}

        for hwmon_dir in sorted(glob.glob(os.path.join(HWMON_ROOT, "hwmon*"))):
            driver = _read(os.path.join(hwmon_dir, "name"))
            if not driver or driver in EXCLUDED_DRIVERS:
                continue

            inputs = sorted(
                glob.glob(os.path.join(hwmon_dir, "temp*_input")),
                key=lambda p: int(os.path.basename(p)[4:-6]),
            )
            if not inputs:
                continue

            seen_drivers[driver] = seen_drivers.get(driver, 0) + 1
            occurrence = seen_drivers[driver]
            key = driver if occurrence == 1 else f"{driver}#{occurrence}"
            pretty = PRETTY_NAMES.get(driver, driver)
            if occurrence > 1:
                pretty = f"{pretty} {occurrence}"

            for path in inputs:
                number = int(os.path.basename(path)[4:-6])
                label = _read(os.path.join(hwmon_dir, f"temp{number}_label"))
                entries.append({
                    "id": f"hwmon:{key}:temp{number}",
                    "label": f"{pretty} · {label or f'temp{number}'}",
                    "source": "host",
                    "path": path,
                })

        self._entries = entries
        self._last_scan = time.monotonic()
        return entries

    def maybe_rescan(self) -> None:
        if time.monotonic() - self._last_scan >= RESCAN_INTERVAL:
            self.scan()

    def read_all(self) -> dict[str, float]:
        """Return {sensor_id: degrees C} for every host sensor readable now."""
        if not self._entries:
            self.scan()
        values: dict[str, float] = {}
        for entry in self._entries:
            raw = _read(entry["path"])
            if raw is None:
                continue
            try:
                values[entry["id"]] = int(raw) / 1000.0
            except ValueError:
                continue
        return values

    def catalog(self) -> list[dict]:
        return [
            {"id": e["id"], "label": e["label"], "source": e["source"]}
            for e in self._entries
        ]


def device_catalog(description: dict) -> list[dict]:
    """Sensor catalog entries for the Commander Pro's own probes."""
    return [
        {"id": f"cpro:temp{probe['index']}",
         "label": f"Commander Pro · Probe {probe['index']}",
         "source": "device"}
        for probe in description.get("probes", [])
        if probe.get("connected")
    ]


def mix(values: list[float], how: str) -> float:
    if how == "avg":
        return sum(values) / len(values)
    if how == "min":
        return min(values)
    return max(values)
