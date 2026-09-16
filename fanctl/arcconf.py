"""Drive temperatures from Microsemi/Adaptec controllers via `arcconf`.

Disks behind a SAS HBA are invisible to Linux hwmon, so the only way to read
their temperature is to ask the controller. `arcconf getconfig <n> PD` prints a
long human-readable report; this module runs it on its own slow schedule and
scrapes the per-drive temperatures out of it.

The result is merged into the same sensor namespace as everything else, so a
drive temperature can drive a fan curve exactly like a CPU or Commander Pro
probe can.

Sensor ids are keyed on the **physical slot**, not the kernel's `/dev/sdX` name:
`sdX` letters are assigned in discovery order and can move between boots, while
"slot 3" stays the drive cage bay you actually pointed a fan at.

A second report, `arcconf getconfig <n> AD`, carries the controller's *own*
temperature sensors -- the HBA can easily be the hottest thing in the case, and
it is not visible to hwmon either. That call is best-effort: if it fails or the
firmware does not report sensors, the drive temperatures still work.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import threading
import time

LOG = logging.getLogger("fanctl.arcconf")

# Only these keys are pulled out of each device block. Everything else -- the
# Phy and Error Counter sub-sections in particular -- is ignored, and the first
# occurrence of a key within a block wins, so a nested section cannot overwrite
# a drive attribute.
_FIELDS = {
    "Disk Name": "disk_name",
    "Vendor": "vendor",
    "Model": "model",
    "Serial number": "serial",
    "Total Size": "size",
    "State": "state",
    "SSD": "ssd",
    "S.M.A.R.T. warnings": "smart_warnings",
    "Current Temperature": "temperature",
    "Maximum Temperature": "temperature_max",
    "Threshold Temperature": "temperature_threshold",
    "Reported Location": "location",
    "Power On Hours": "power_on_hours",
    "Usage Remaining": "usage_remaining",
    "Last Failure Reason": "last_failure",
}

# systemd gives a service a minimal PATH, nothing like an interactive root
# shell, and the Adaptec/Microsemi CLI frequently installs outside it. These are
# the usual locations, searched only when the configured command is a bare name.
_SEARCH_PATHS = (
    "/usr/sbin", "/usr/bin", "/usr/local/sbin", "/usr/local/bin", "/sbin", "/bin",
    "/usr/Arcconf", "/usr/StorMan", "/opt/arcconf", "/opt/Arcconf",
    "/usr/local/Arcconf", "/usr/adaptec", "/usr/Adaptec_Event_Monitor",
)


def resolve_command(command: str) -> tuple[str | None, str]:
    """Find the arcconf binary. Returns (path or None, description of the hunt).

    An absolute path is used as given. A bare name is looked up on PATH first,
    then in the well-known install directories above.
    """
    if os.path.sep in command:
        if os.path.isfile(command) and os.access(command, os.X_OK):
            return command, command
        return None, f"{command} is not an executable file"

    found = shutil.which(command)
    if found:
        return found, found

    for directory in _SEARCH_PATHS:
        candidate = os.path.join(directory, command)
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate, candidate

    LOG.debug("searched PATH=%s and %s", os.environ.get("PATH", ""), _SEARCH_PATHS)
    return None, (f"{command} not found on the service PATH or in the usual "
                  f"Adaptec install directories. Set an absolute path in "
                  f"storage.command in the config file, then restart.")


_CHANNEL_RE = re.compile(r"^Channel #(\d+)\s*:")
_DEVICE_RE = re.compile(r"^Device #(\d+)\s*$")
_SLOT_RE = re.compile(r"\bSlot\s+(\d+)")
_NUMBER_RE = re.compile(r"-?\d+")


def _number(value: str | None) -> float | None:
    """Pull the leading number out of values like '40 deg C' or '63 percent'."""
    if not value:
        return None
    match = _NUMBER_RE.search(value)
    return float(match.group()) if match else None


def parse(text: str, controller: int = 1) -> list[dict]:
    """Turn `arcconf getconfig <n> PD` output into a list of drive records."""
    devices: list[dict] = []
    channel: int | None = None
    current: dict | None = None

    for raw_line in text.splitlines():
        line = raw_line.strip()

        match = _CHANNEL_RE.match(line)
        if match:
            channel = int(match.group(1))
            current = None
            continue

        match = _DEVICE_RE.match(line)
        if match:
            current = {"channel": channel, "device": int(match.group(1)),
                       "kind": None, "fields": {}}
            devices.append(current)
            continue

        if current is None:
            continue

        if line.startswith("Device is "):
            current["kind"] = line[len("Device is "):].strip()
            continue

        key, sep, value = line.partition(":")
        if not sep:
            continue
        field = _FIELDS.get(key.strip())
        if field and field not in current["fields"]:
            current["fields"][field] = value.strip()

    drives = []
    for entry in devices:
        fields = entry["fields"]
        kind = entry["kind"] or ""
        temperature = _number(fields.get("temperature"))

        # Enclosure/SES devices share the "Device #n" shape but are not drives.
        if "Hard drive" not in kind and temperature is None:
            continue

        location = fields.get("location", "")
        slot_match = _SLOT_RE.search(location)
        slot = int(slot_match.group(1)) if slot_match else None

        if slot is not None:
            sensor_id = f"arcconf:{controller}:slot{slot}"
        else:
            sensor_id = f"arcconf:{controller}:c{entry['channel']}d{entry['device']}"

        disk_name = fields.get("disk_name", "")
        # "/dev/sda (Disk0) (Bus: 0, ...)" -> "/dev/sda"
        device_path = disk_name.split()[0] if disk_name else None

        drives.append({
            "id": sensor_id,
            "slot": slot,
            "channel": entry["channel"],
            "device": entry["device"],
            "path": device_path,
            "vendor": fields.get("vendor"),
            "model": fields.get("model"),
            "serial": fields.get("serial"),
            "size": fields.get("size"),
            "state": fields.get("state"),
            "ssd": fields.get("ssd") == "Yes",
            "smart_warning": fields.get("smart_warnings") not in (None, "No"),
            "last_failure": fields.get("last_failure"),
            "temperature": temperature,
            "temperature_max": _number(fields.get("temperature_max")),
            "temperature_threshold": _number(fields.get("temperature_threshold")),
            "power_on_hours": _number(fields.get("power_on_hours")),
            "usage_remaining": _number(fields.get("usage_remaining")),
        })

    drives.sort(key=lambda d: (d["slot"] is None, d["slot"], d["channel"], d["device"]))
    return drives


def _slug(text: str) -> str:
    """"Inlet Ambient" -> "inlet-ambient". Used to build stable sensor ids."""
    return re.sub(r"-+", "-", re.sub(r"[^a-z0-9]+", "-", text.lower())).strip("-")


def parse_controller(text: str, controller: int = 1) -> dict:
    """Turn `arcconf getconfig <n> AD` output into controller info + sensors.

    Two firmware shapes exist. Older builds report a single headline
    `Temperature : 48 C/ 118 F (Normal)` in the controller block; newer ones add
    a `Temperature Sensors Information` section with several named sensors
    (inlet ambient, ASIC, board top/bottom). When both are present the named
    sensors win, because the headline just duplicates one of them -- usually the
    ASIC -- and exposing it twice would be confusing.

    The controller serial number, world-wide name and SAS addresses are
    deliberately *not* parsed. They would otherwise reach /api/state, which is
    exactly the output people paste into bug reports.
    """
    model: str | None = None
    firmware: str | None = None
    headline: float | None = None
    sensors: list[dict] = []
    current: dict | None = None

    for raw_line in text.splitlines():
        key, sep, value = raw_line.strip().partition(":")
        if not sep:
            continue
        key, value = key.strip(), value.strip()

        if key == "Sensor ID":
            current = {"sensor_id": _number(value), "temperature": None,
                       "temperature_max": None, "location": None}
            sensors.append(current)
            continue

        # First occurrence within a block wins, so a later stray key -- the
        # Connector section further down, say -- cannot overwrite a sensor.
        if current is not None:
            if key == "Current Value" and current["temperature"] is None:
                current["temperature"] = _number(value)
                continue
            if key == "Max Value Since Powered On" and current["temperature_max"] is None:
                current["temperature_max"] = _number(value)
                continue
            if key == "Location" and current["location"] is None:
                current["location"] = value
                continue

        if key == "Controller Model" and model is None:
            model = value
        elif key == "Firmware" and firmware is None:
            firmware = value
        elif key == "Temperature" and headline is None:
            headline = _number(value)

    usable = [s for s in sensors if s["temperature"] is not None]

    if usable:
        seen: set[str] = set()
        for sensor in usable:
            base = _slug(sensor["location"] or "")
            if not base:
                base = f"sensor{int(sensor['sensor_id'] or 0)}"
            slug = base
            if slug in seen:                       # two sensors, same location
                slug = f"{base}-{int(sensor['sensor_id'] or 0)}"
            seen.add(slug)
            sensor["id"] = f"arcconf:{controller}:ctrl:{slug}"
            sensor["label"] = sensor["location"] or f"Sensor {sensor['sensor_id']}"
    elif headline is not None:
        usable = [{
            "id": f"arcconf:{controller}:ctrl",
            "label": "Controller",
            "location": "Controller",
            "sensor_id": None,
            "temperature": headline,
            "temperature_max": None,
        }]

    return {"model": model, "firmware": firmware, "sensors": usable}


class ArcconfSensors:
    """Polls `arcconf` on a slow interval in its own thread.

    The command takes a second or more and talks to the controller, so it must
    not run on the fan control tick. Values are cached and served to the control
    loop; if a poll stops succeeding the cache is declared stale and the sensors
    disappear, which makes fans bound to them fall back to the failsafe duty
    rather than run on a reading from ten minutes ago.
    """

    def __init__(self, config: dict):
        self._lock = threading.Lock()
        self._config = dict(config)
        self._drives: list[dict] = []
        self._controller: dict = {"model": None, "firmware": None, "sensors": []}
        self._controller_error: str | None = None
        self._updated: float = 0.0
        self._error: str | None = None
        self._resolved: str | None = None
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None

    # -- lifecycle --------------------------------------------------------

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="arcconf", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread is not None:
            self._thread.join(timeout=5.0)

    def configure(self, config: dict) -> None:
        with self._lock:
            was_enabled = self._config.get("enabled")
            # `command` is deliberately not taken from here; see Controller.
            self._config = dict(config)
        if config.get("enabled") and not was_enabled:
            self._wake.set()

    def refresh(self) -> None:
        self._wake.set()

    # -- polling ----------------------------------------------------------

    def _run(self) -> None:
        while not self._stop.is_set():
            with self._lock:
                enabled = bool(self._config.get("enabled"))
                interval = float(self._config.get("interval", 30.0))
            if enabled:
                self._poll_once()
            self._wake.wait(timeout=interval if enabled else 10.0)
            self._wake.clear()

    def _poll_once(self) -> None:
        with self._lock:
            command = self._config.get("command") or "arcconf"
            controller = int(self._config.get("controller", 1))
            timeout = float(self._config.get("timeout", 20.0))

        resolved, detail = resolve_command(command)
        if resolved is None:
            self._resolved = None
            self._fail(detail)
            return
        if resolved != self._resolved:
            LOG.info("using arcconf at %s", resolved)
            self._resolved = resolved

        stdout, error = self._run_report(resolved, command, controller, "PD", timeout)
        if error is not None:
            self._fail(error)
            return

        try:
            drives = parse(stdout, controller)
        except Exception as exc:  # a format we do not understand
            self._fail(f"cannot parse {command} output: {exc}")
            return

        if not drives:
            self._fail("no drives reported by the controller")
            return

        # The controller's own sensors come from a second report. It is
        # best-effort on purpose: firmware that does not provide it, or an
        # arcconf build that rejects the argument, must not cost us the drive
        # temperatures that already parsed successfully.
        controller_info = {"model": None, "firmware": None, "sensors": []}
        ad_stdout, ad_error = self._run_report(
            resolved, command, controller, "AD", timeout)
        if ad_error is None:
            try:
                controller_info = parse_controller(ad_stdout, controller)
                if not controller_info["sensors"]:
                    ad_error = "this firmware reports no controller temperature sensors"
            except Exception as exc:
                ad_error = f"cannot parse controller report: {exc}"

        if ad_error is not None and ad_error != self._controller_error:
            LOG.warning("arcconf controller temperatures unavailable: %s", ad_error)

        with self._lock:
            self._drives = drives
            self._controller = controller_info
            self._controller_error = ad_error
            self._updated = time.time()
            if self._error is not None:
                LOG.info("arcconf recovered: %d drives, %d controller sensors",
                         len(drives), len(controller_info["sensors"]))
            self._error = None

    def _run_report(self, resolved: str, command: str, controller: int,
                    report: str, timeout: float) -> tuple[str, str | None]:
        """Run one `arcconf getconfig <n> <report>`. Returns (stdout, error)."""
        # argv form, never a shell string: nothing here is interpolated into a
        # shell, so a hostile config value cannot become a command injection.
        argv = [resolved, "getconfig", str(controller), report]
        try:
            completed = subprocess.run(
                argv,
                capture_output=True,
                text=True,
                timeout=timeout,
                stdin=subprocess.DEVNULL,
                cwd="/tmp",           # arcconf drops log files in the cwd
                check=False,
            )
        except FileNotFoundError:
            return "", f"{command} not found"
        except subprocess.TimeoutExpired:
            return "", f"{command} {report} timed out after {timeout:g}s"
        except OSError as exc:
            return "", f"cannot run {command}: {exc}"

        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout or "").strip().splitlines()
            return "", (f"{command} {report} exited {completed.returncode}"
                        + (f": {detail[0]}" if detail else ""))
        return completed.stdout, None

    def _fail(self, message: str) -> None:
        with self._lock:
            first = self._error != message
            self._error = message
        if first:
            LOG.warning("arcconf: %s", message)

    # -- readers ----------------------------------------------------------

    def _fresh_drives(self) -> list[dict]:
        with self._lock:
            if not self._drives:
                return []
            stale_after = float(self._config.get("stale_after", 120.0))
            if time.time() - self._updated > stale_after:
                return []
            return list(self._drives)

    def _fresh_controller(self) -> dict:
        with self._lock:
            stale_after = float(self._config.get("stale_after", 120.0))
            if not self._updated or time.time() - self._updated > stale_after:
                return {"model": None, "firmware": None, "sensors": []}
            return dict(self._controller)

    def read_all(self) -> dict[str, float]:
        """{sensor_id: degrees C} for fresh readings only."""
        values: dict[str, float] = {}
        temperatures = []
        for drive in self._fresh_drives():
            if drive["temperature"] is None:
                continue
            values[drive["id"]] = drive["temperature"]
            temperatures.append(drive["temperature"])
        with self._lock:
            controller = int(self._config.get("controller", 1))
        if temperatures:
            values[f"arcconf:{controller}:max"] = max(temperatures)

        card: list[float] = []
        for sensor in self._fresh_controller()["sensors"]:
            if sensor["temperature"] is None:
                continue
            values[sensor["id"]] = sensor["temperature"]
            card.append(sensor["temperature"])
        if card:
            values[f"arcconf:{controller}:ctrl:max"] = max(card)
        return values

    def catalog(self) -> list[dict]:
        entries = []
        drives = self._fresh_drives()
        for drive in drives:
            if drive["temperature"] is None:
                continue
            where = f"Slot {drive['slot']}" if drive["slot"] is not None \
                else f"Ch{drive['channel']} Dev{drive['device']}"
            path = f" ({drive['path']})" if drive["path"] else ""
            entries.append({
                "id": drive["id"],
                "label": f"Disk · {where}{path}",
                "source": "storage",
            })
        with self._lock:
            controller = int(self._config.get("controller", 1))
        if entries:
            entries.append({
                "id": f"arcconf:{controller}:max",
                "label": "Disk · Hottest drive",
                "source": "storage",
            })

        card = self._fresh_controller()["sensors"]
        for sensor in card:
            if sensor["temperature"] is None:
                continue
            entries.append({
                "id": sensor["id"],
                "label": f"Controller · {sensor['label']}",
                "source": "storage",
            })
        if len(card) > 1:
            entries.append({
                "id": f"arcconf:{controller}:ctrl:max",
                "label": "Controller · Hottest sensor",
                "source": "storage",
            })
        return entries

    def status(self) -> dict:
        with self._lock:
            enabled = bool(self._config.get("enabled"))
            updated = self._updated
            error = self._error
            drives = list(self._drives)
            controller_info = dict(self._controller)
            controller_error = self._controller_error
            resolved = self._resolved
            stale_after = float(self._config.get("stale_after", 120.0))
        age = (time.time() - updated) if updated else None
        return {
            "enabled": enabled,
            "error": error,
            "command": resolved,
            "updated": updated or None,
            "age": round(age, 1) if age is not None else None,
            "stale": bool(updated and age is not None and age > stale_after),
            "drives": drives,
            "controller": controller_info,
            "controller_error": controller_error,
        }
