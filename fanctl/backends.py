"""Device backends for the Corsair Commander Pro.

Two transports are supported:

hwmon
    The in-kernel ``corsair-cpro`` driver (Linux 5.9+) exposes the device at
    ``/sys/class/hwmon/hwmonN`` with readable ``fan*_input`` / ``temp*_input``
    and *writable* ``pwm[1-6]``.  This is the preferred path on Proxmox: no USB
    contention, no userspace HID library, and it keeps working across suspend.

liquidctl
    Raw USB HID via the ``liquidctl`` package.  Used when the kernel driver is
    unavailable (older kernel, module blacklisted, non-Linux development host).

Both classes are safe to call from multiple threads; each serialises device
access behind its own lock.
"""

from __future__ import annotations

import glob
import os
import threading

FAN_COUNT = 6
PROBE_COUNT = 4

HWMON_ROOT = "/sys/class/hwmon"
HWMON_NAME = "corsair-cpro"


class BackendError(RuntimeError):
    """Raised when the device cannot be reached or driven."""


def _duty_to_pwm(duty: float) -> int:
    return max(0, min(255, int(round(duty * 255.0 / 100.0))))


# --------------------------------------------------------------------------
# hwmon
# --------------------------------------------------------------------------

class HwmonBackend:
    name = "hwmon"

    def __init__(self, path: str | None = None, forced: dict[int, str] | None = None):
        self._path = path
        self._forced = forced or {}
        self._lock = threading.Lock()
        self._fans: dict[int, str | None] = {}
        self._notes: dict[int, str] = {}
        self._probes: list[int] = []

    @staticmethod
    def discover() -> str | None:
        """Return the hwmon directory belonging to the Commander Pro, if any."""
        for candidate in sorted(glob.glob(os.path.join(HWMON_ROOT, "hwmon*"))):
            try:
                with open(os.path.join(candidate, "name"), "r", encoding="utf-8") as handle:
                    if handle.read().strip() == HWMON_NAME:
                        return candidate
            except OSError:
                continue
        return None

    # -- sysfs helpers ----------------------------------------------------

    def _attr(self, name: str) -> str:
        return os.path.join(self._path, name)

    def _read_text(self, name: str) -> str | None:
        try:
            with open(self._attr(name), "r", encoding="utf-8") as handle:
                return handle.read().strip()
        except OSError:
            return None

    def _read_int(self, name: str) -> int | None:
        raw = self._read_text(name)
        if raw is None:
            return None
        try:
            return int(raw)
        except ValueError:
            return None

    # -- lifecycle --------------------------------------------------------

    def open(self) -> None:
        if self._path and not self._still_present():
            self._path = None  # stale after a re-enumeration; scan again
        path = self._path or self.discover()
        if not path:
            raise BackendError(
                "no corsair-cpro hwmon device found "
                "(is the kernel module loaded and the Commander Pro plugged in?)"
            )
        self._path = path

        fans: dict[int, str | None] = {}
        notes: dict[int, str] = {}
        for index in range(1, FAN_COUNT + 1):
            label = self._read_text(f"fan{index}_label")
            has_pwm = os.path.exists(self._attr(f"pwm{index}"))
            forced = self._forced.get(index)

            if forced:
                # Trust the operator over auto-detection, but only as far as the
                # kernel allows: corsair-cpro only exposes pwmN for channels it
                # believes are populated, and a 2-wire fan has no tach for it to
                # sense. When the attribute is absent there is nothing to write.
                if has_pwm:
                    fans[index] = f"{forced.upper()} (forced)"
                else:
                    fans[index] = None
                    notes[index] = (
                        f"forced to {forced.upper()} but the kernel driver does not "
                        f"expose pwm{index}; switch to the liquidctl backend to "
                        f"drive an undetected fan"
                    )
            elif label and label.endswith("4pin"):
                fans[index] = "PWM"
            elif label and label.endswith("3pin"):
                fans[index] = "DC"
            elif has_pwm and label is None:
                # Older kernels omit the label; assume the channel is usable.
                fans[index] = "unknown"
            else:
                fans[index] = None
        self._fans = fans
        self._notes = notes

        self._probes = [
            index for index in range(1, PROBE_COUNT + 1)
            if os.path.exists(self._attr(f"temp{index}_input"))
        ]

        # corsair-cpro only creates pwmN for channels it detected a fan on, so
        # probe every channel rather than assuming pwm1 exists: an empty
        # channel 1 must not make the whole device unusable.
        present = [index for index in range(1, FAN_COUNT + 1)
                   if os.path.exists(self._attr(f"pwm{index}"))]
        if not present:
            raise BackendError(
                f"{path} exposes no pwm attributes (no fans detected on any channel)"
            )
        unwritable = [index for index in present
                      if not os.access(self._attr(f"pwm{index}"), os.W_OK)]
        if unwritable:
            raise BackendError(
                f"{self._attr(f'pwm{unwritable[0]}')} is not writable (run as root)"
            )

    def close(self) -> None:  # nothing to release
        return

    # -- data -------------------------------------------------------------

    def describe(self) -> dict:
        return {
            "backend": self.name,
            "device": "Corsair Commander Pro",
            "path": self._path,
            "firmware": None,
            "fans": [
                {"index": i, "connected": self._fans.get(i) is not None,
                 "type": self._fans.get(i), "note": self._notes.get(i)}
                for i in range(1, FAN_COUNT + 1)
            ],
            "probes": [
                {"index": i, "connected": i in self._probes}
                for i in range(1, PROBE_COUNT + 1)
            ],
        }

    def _still_present(self) -> bool:
        """Guard against the device re-enumerating under a different hwmonN."""
        return self._read_text("name") == HWMON_NAME

    def read(self) -> dict:
        rpm: dict[int, int] = {}
        temps: dict[int, float] = {}
        volts: dict[str, float] = {}
        with self._lock:
            if not self._still_present():
                raise BackendError(f"{self._path} is no longer the Commander Pro")
            for index in range(1, FAN_COUNT + 1):
                if self._fans.get(index) is None:
                    continue
                value = self._read_int(f"fan{index}_input")
                if value is not None:
                    rpm[index] = value
            for index in self._probes:
                value = self._read_int(f"temp{index}_input")
                if value is not None:
                    temps[index] = value / 1000.0
            for slot, rail in enumerate(("+12V", "+5V", "+3.3V")):
                value = self._read_int(f"in{slot}_input")
                if value is not None:
                    volts[rail] = value / 1000.0
        return {"rpm": rpm, "temps": temps, "volts": volts}

    def set_duty(self, index: int, duty: float) -> None:
        if self._fans.get(index) is None:
            return
        with self._lock:
            try:
                with open(self._attr(f"pwm{index}"), "w", encoding="utf-8") as handle:
                    handle.write(str(_duty_to_pwm(duty)))
            except OSError as exc:
                raise BackendError(f"cannot set fan{index} duty: {exc}") from exc


# --------------------------------------------------------------------------
# liquidctl
# --------------------------------------------------------------------------

class LiquidctlBackend:
    name = "liquidctl"

    def __init__(self, forced: dict[int, str] | None = None) -> None:
        self._lock = threading.Lock()
        self._forced = forced or {}
        self._device = None
        self._static: list = []
        self._fans: dict[int, str | None] = {}
        self._probes: list[int] = []
        self._direct = bool(self._forced)

    def open(self) -> None:
        try:
            from liquidctl.driver import find_liquidctl_devices
        except ImportError as exc:  # pragma: no cover - depends on host
            raise BackendError("the liquidctl package is not installed") from exc

        device = None
        for candidate in find_liquidctl_devices():
            if type(candidate).__name__ == "CommanderPro" and getattr(candidate, "_fan_count", 0):
                device = candidate
                break
        if device is None:
            raise BackendError("no Corsair Commander Pro found on USB")

        device.connect()
        # initialize() must run before set_fixed_speed(): the driver looks up a
        # cached fan-mode table and silently ignores writes to channels it
        # believes are disconnected.
        #
        # direct_access matters when forcing a mode: if the corsair-cpro kernel
        # driver is bound, liquidctl takes the hwmon path and *silently discards*
        # fan_mode (it only logs a warning). Forcing therefore has to bypass it.
        fan_mode = {str(i): mode for i, mode in self._forced.items()}
        self._static = list(
            device.initialize(fan_mode=fan_mode, direct_access=self._direct) or []
        )
        self._device = device

        self._fans = {}
        self._probes = []
        for prop, value, _unit in self._static:
            if prop.startswith("Fan ") and prop.endswith("control mode"):
                index = int(prop.split()[1])
                self._fans[index] = value if value else None
            elif prop.startswith("Temperature probe "):
                index = int(prop.split()[2])
                if value:
                    self._probes.append(index)

    def close(self) -> None:
        with self._lock:
            if self._device is not None:
                try:
                    self._device.disconnect()
                except Exception:  # pragma: no cover - best effort on shutdown
                    pass
                self._device = None

    def describe(self) -> dict:
        firmware = next(
            (v for p, v, _ in self._static if p == "Firmware version"), None
        )
        return {
            "backend": self.name,
            "device": "Corsair Commander Pro",
            "path": None,
            "firmware": firmware,
            "fans": [
                {"index": i, "connected": self._fans.get(i) is not None,
                 "type": (f"{self._fans[i]} (forced)"
                          if self._fans.get(i) and i in self._forced
                          else self._fans.get(i)),
                 "note": None}
                for i in range(1, FAN_COUNT + 1)
            ],
            "probes": [
                {"index": i, "connected": i in self._probes}
                for i in range(1, PROBE_COUNT + 1)
            ],
        }

    def read(self) -> dict:
        with self._lock:
            if self._device is None:
                raise BackendError("device is not connected")
            try:
                status = self._device.get_status(direct_access=self._direct)
            except Exception as exc:
                raise BackendError(f"cannot read device status: {exc}") from exc

        rpm: dict[int, int] = {}
        temps: dict[int, float] = {}
        volts: dict[str, float] = {}
        for prop, value, unit in status:
            try:
                if unit == "rpm" and prop.startswith("Fan "):
                    rpm[int(prop.split()[1])] = int(value)
                elif unit == "°C" and prop.startswith("Temperature "):
                    temps[int(prop.split()[1])] = float(value)
                elif unit == "V":
                    volts[prop.split()[0]] = float(value)
            except (ValueError, IndexError):
                continue
        return {"rpm": rpm, "temps": temps, "volts": volts}

    def set_duty(self, index: int, duty: float) -> None:
        if self._fans.get(index) is None:
            return
        with self._lock:
            if self._device is None:
                raise BackendError("device is not connected")
            try:
                self._device.set_fixed_speed(f"fan{index}", int(round(duty)))
            except Exception as exc:
                raise BackendError(f"cannot set fan{index} duty: {exc}") from exc


# --------------------------------------------------------------------------
# selection
# --------------------------------------------------------------------------

def create(preference: str = "auto", forced: dict[int, str] | None = None):
    """Open the best available backend and return it.

    ``preference`` is one of ``auto``, ``hwmon`` or ``liquidctl``.  In ``auto``
    mode hwmon wins when present, because it avoids fighting the kernel driver
    for the USB endpoint.
    """
    attempts: list = []
    if preference in ("auto", "hwmon"):
        attempts.append(HwmonBackend(forced=forced))
    if preference in ("auto", "liquidctl"):
        attempts.append(LiquidctlBackend(forced=forced))

    errors = []
    for backend in attempts:
        try:
            backend.open()
            return backend
        except BackendError as exc:
            errors.append(f"{backend.name}: {exc}")
        except Exception as exc:  # unexpected, but keep trying the next backend
            errors.append(f"{backend.name}: {exc}")
    raise BackendError("; ".join(errors) or f"unknown backend '{preference}'")
