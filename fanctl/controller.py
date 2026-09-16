"""The control loop.

One background thread owns the device: it polls temperatures, evaluates each
fan's curve and writes duties.  The HTTP threads never touch the backend
directly -- they read a snapshot dict and post config updates, both under the
same lock -- so USB/sysfs access stays serialised and predictable.
"""

from __future__ import annotations

import logging
import threading
import time

from . import __version__, arcconf, backends, config as config_module, sensors
from .curves import FanController
from .history import History

LOG = logging.getLogger("fanctl.controller")

RECONNECT_DELAY = 5.0
MAX_TICK_DT = 10.0          # ignore huge gaps (suspend/resume) when slew limiting


class Controller:
    def __init__(self, config_path: str):
        self.config_path = config_path
        # A config that cannot be parsed must not stop fan control: the
        # process would crash-loop under systemd with the fans unmanaged. Move
        # the bad file aside, run on defaults (every fan at the failsafe duty)
        # and say so loudly in the log and the UI.
        self.config_warning: str | None = None
        try:
            self.config = config_module.load(config_path)
        except RuntimeError as exc:
            backup = config_module.quarantine(config_path)
            self.config = config_module.default_config()
            self.config_warning = (
                f"config could not be read ({exc}); it was moved to {backup} and "
                f"the daemon started with defaults. Reassign sensors, or restore "
                f"the file and restart."
            )
            LOG.error(self.config_warning)

        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None

        self._backend = None
        self._backend_pref = self.config["control"]["backend"]
        self._description: dict = {}
        self._error: str | None = None
        self._next_connect = 0.0

        self._host = sensors.HostSensors()
        self._storage = arcconf.ArcconfSensors(self.config["storage"])
        self._fans = {i: FanController(i) for i in range(1, backends.FAN_COUNT + 1)}
        self._written: dict[int, float] = {}
        self._last_write = time.monotonic()
        self._overrides: dict[int, tuple[float, float]] = {}

        history_cfg = self.config["history"]
        self._history = History(
            history_cfg["seconds"], self.config["control"]["interval"],
            path=history_cfg["file"] if history_cfg["persist"] else None,
        )
        self._latest: dict = {"temps": {}, "rpm": {}, "volts": {}, "t": 0.0}

    # ------------------------------------------------------------------
    # lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        self._history.load()
        self._host.scan()
        self._storage.start()
        self._thread = threading.Thread(target=self._run, name="fan-control", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        self._storage.stop()
        if self._thread is not None:
            self._thread.join(timeout=10.0)
        self._shutdown_device()
        self._history.save()

    def _shutdown_device(self) -> None:
        with self._lock:
            backend = self._backend
            if backend is None:
                return
            if self.config["control"]["apply_failsafe_on_exit"]:
                duty = self.config["control"]["failsafe_duty"]
                LOG.info("applying failsafe duty %s%% to all fans on exit", duty)
                for index in range(1, backends.FAN_COUNT + 1):
                    try:
                        backend.set_duty(index, duty)
                    except backends.BackendError as exc:
                        LOG.warning("failsafe write to fan%d failed: %s", index, exc)
            try:
                backend.close()
            finally:
                self._backend = None

    # ------------------------------------------------------------------
    # connection
    # ------------------------------------------------------------------

    def _ensure_backend(self) -> bool:
        if self._backend is not None:
            return True
        if time.monotonic() < self._next_connect:
            return False
        with self._lock:
            forced = {f["index"]: f["force_mode"]
                      for f in self.config["fans"] if f["force_mode"]}
        try:
            backend = backends.create(self._backend_pref, forced=forced)
        except Exception as exc:
            self._error = str(exc)
            self._next_connect = time.monotonic() + RECONNECT_DELAY
            return False

        with self._lock:
            self._backend = backend
            self._description = backend.describe()
            self._error = None
            self._written.clear()
            for controller in self._fans.values():
                controller.reset()
        LOG.info("connected via %s backend", backend.name)
        return True

    def _drop_backend(self, reason: str) -> None:
        LOG.warning("device lost: %s", reason)
        with self._lock:
            if self._backend is not None:
                try:
                    self._backend.close()
                except Exception:
                    pass
            self._backend = None
            self._error = reason
            self._next_connect = time.monotonic() + RECONNECT_DELAY

    # ------------------------------------------------------------------
    # main loop
    # ------------------------------------------------------------------

    def _run(self) -> None:
        last_tick = time.monotonic()
        while not self._stop.is_set():
            with self._lock:
                interval = self.config["control"]["interval"]

            try:
                now = time.monotonic()
                dt = min(max(now - last_tick, 0.01), MAX_TICK_DT)
                last_tick = now
                self._tick(dt)
            except Exception:  # never let the loop die
                LOG.exception("unhandled error in control tick")

            self._wake.wait(timeout=interval)
            self._wake.clear()

    def _tick(self, dt: float) -> None:
        if not self._ensure_backend():
            return

        backend = self._backend
        try:
            reading = backend.read()
        except backends.BackendError as exc:
            self._drop_backend(str(exc))
            return

        self._host.maybe_rescan()
        temps: dict[str, float] = {
            f"cpro:temp{index}": value for index, value in reading["temps"].items()
        }
        temps.update(self._host.read_all())
        temps.update(self._storage.read_all())

        with self._lock:
            cfg = self.config
            control = cfg["control"]
            now = time.monotonic()
            reassert = control["reassert_seconds"]
            force_write = reassert > 0 and (now - self._last_write) >= reassert

            duties: dict[int, float] = {}
            for fan_cfg in cfg["fans"]:
                index = fan_cfg["index"]
                controller = self._fans[index]

                override = self._overrides.get(index)
                if override is not None:
                    duty, until = override
                    if now < until:
                        controller.duty = duty
                        controller.target = duty
                        controller.reason = "test"
                        duties[index] = duty
                        continue
                    del self._overrides[index]

                if not fan_cfg["enabled"]:
                    controller.reason = "disabled"
                    continue

                values = [temps[s] for s in fan_cfg["sensors"] if s in temps]
                temp = sensors.mix(values, fan_cfg["mix"]) if values else None
                # The emergency test deliberately ignores the mix setting: with
                # "avg" or "min" one drive at 90 degC next to one at 40 degC
                # would never trip it. Any sensor on the channel over the
                # limit is an emergency.
                emergency = bool(values) and max(values) >= control["emergency_temp"]
                controller.reason = self._reason(fan_cfg, temp, emergency)

                duties[index] = controller.step(
                    fan_cfg,
                    temp,
                    dt,
                    control["failsafe_duty"],
                    emergency=emergency,
                    emergency_duty=control["emergency_duty"],
                )

            self._latest = {
                "t": time.time(),
                "temps": temps,
                "rpm": reading["rpm"],
                "volts": reading["volts"],
            }

        for index, duty in duties.items():
            previous = self._written.get(index)
            # 0.4% is just under one PWM step, so this skips writes the device
            # could not distinguish anyway.
            if previous is not None and abs(previous - duty) < 0.4 and not force_write:
                continue
            try:
                backend.set_duty(index, duty)
                self._written[index] = duty
            except backends.BackendError as exc:
                self._drop_backend(str(exc))
                return
        if force_write:
            self._last_write = time.monotonic()

        self._history.append({
            "t": self._latest["t"],
            "temps": {k: round(v, 1) for k, v in temps.items()},
            "rpm": dict(reading["rpm"]),
            "duty": {i: round(d, 1) for i, d in duties.items()},
        })
        with self._lock:
            save_every = self.config["history"]["save_interval"]
        self._history.maybe_save(save_every)

    # ------------------------------------------------------------------
    # API surface
    # ------------------------------------------------------------------

    @staticmethod
    def _reason(fan_cfg: dict, temp: float | None, emergency: bool) -> str:
        """Why a channel is at the duty it is -- shown verbatim in the UI."""
        if fan_cfg["mode"] == "off":
            return "off"
        if fan_cfg["mode"] == "fixed":
            return "fixed"
        if not fan_cfg["sensors"]:
            return "no-sensors"
        if temp is None:
            return "sensors-unavailable"
        if emergency:
            return "emergency"
        stop_below = fan_cfg.get("stop_below")
        if stop_below is not None and temp < stop_below:
            return "stopped"
        return "curve"

    def snapshot(self) -> dict:
        with self._lock:
            description = dict(self._description) if self._description else None
            fans = []
            for fan_cfg in self.config["fans"]:
                index = fan_cfg["index"]
                controller = self._fans[index]
                fans.append({
                    "index": index,
                    "duty": round(controller.duty, 1),
                    "target": round(controller.target, 1),
                    "rpm": self._latest["rpm"].get(index),
                    "control_temp": (
                        round(controller.control_temp, 1)
                        if controller.control_temp is not None else None
                    ),
                    "override": index in self._overrides,
                    "reason": controller.reason,
                })
            catalog = (sensors.device_catalog(self._description or {})
                       + self._host.catalog()
                       + self._storage.catalog())
            # Apply user-chosen names here rather than in each provider, so
            # every consumer -- cards, chart legend, every browser -- agrees on
            # what a sensor is called. `default_label` is kept so the UI can
            # show what it would revert to.
            names = self.config["sensor_names"]
            for entry in catalog:
                custom = names.get(entry["id"])
                if custom:
                    entry["default_label"] = entry["label"]
                    entry["label"] = custom
            return {
                "version": __version__,
                "connected": self._backend is not None,
                "error": self._error,
                "warning": self.config_warning,
                "device": description,
                "time": self._latest["t"],
                "temps": {k: round(v, 1) for k, v in self._latest["temps"].items()},
                "volts": {k: round(v, 2) for k, v in self._latest["volts"].items()},
                "fans": fans,
                "sensors": catalog,
                "storage": self._storage.status(),
                "config": self.config,
            }

    def history(self, since: float = 0.0, max_points: int = 600) -> list[dict]:
        return self._history.series(since, max_points)

    def update_config(self, raw: dict) -> dict:
        with self._lock:
            new_config = config_module.merge(self.config, raw)
        with self._lock:
            # storage.command names a binary this daemon executes as root.
            # Accepting it over HTTP would turn any request that can reach the
            # API into arbitrary root code execution, so the running value
            # always wins: it can only be changed by editing the config file.
            new_config["storage"]["command"] = self.config["storage"]["command"]
            # Same reasoning for history.file: it is a path the daemon writes
            # as root, so it is file-only too. `persist` itself only takes
            # effect on restart, since the History object is built at startup.
            new_config["history"]["file"] = self.config["history"]["file"]
            backend_changed = new_config["control"]["backend"] != self._backend_pref
            # Fan modes are programmed during initialize(), so a change only
            # takes effect on a fresh connection.
            forced_changed = (
                {f["index"]: f["force_mode"] for f in new_config["fans"]}
                != {f["index"]: f["force_mode"] for f in self.config["fans"]}
            )
            self.config = new_config
            self._backend_pref = new_config["control"]["backend"]
            self._history.resize(
                new_config["history"]["seconds"], new_config["control"]["interval"]
            )
            self._written.clear()  # re-assert duties against the new settings
            config_module.save(self.config_path, new_config)
        self._storage.configure(new_config["storage"])
        if backend_changed or forced_changed:
            self._drop_backend("backend preference changed" if backend_changed
                               else "fan type override changed")
            self._next_connect = 0.0
        self._wake.set()
        return new_config

    def patch_fan(self, index: int, changes: dict) -> dict:
        with self._lock:
            if not any(f["index"] == index for f in self.config["fans"]):
                raise KeyError(f"no such fan: {index}")
        return self.update_config({"fans": [dict(changes, index=index)]})

    def identify(self, index: int, duty: float = 100.0, seconds: float = 5.0) -> None:
        """Briefly force a channel to a duty so it can be located by ear."""
        with self._lock:
            self._overrides[index] = (
                max(0.0, min(100.0, duty)),
                time.monotonic() + max(0.5, min(60.0, seconds)),
            )
            self._written.pop(index, None)
        self._wake.set()

    def refresh_storage(self) -> None:
        self._storage.refresh()

    def reconnect(self) -> None:
        self._drop_backend("reconnect requested")
        self._next_connect = 0.0
        self._wake.set()
