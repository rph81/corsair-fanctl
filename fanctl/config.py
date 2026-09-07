"""Configuration loading, normalisation and atomic persistence.

The config is a plain JSON document so that no third-party parser is needed on
the Proxmox host.  Everything that arrives from the web UI goes through
`normalize()`, which fills in defaults and clamps values into safe ranges; the
control loop can therefore trust the config without re-validating.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import time
from typing import Any

FAN_COUNT = 6
MAX_FAVORITES = 10
MIN_FAILSAFE_DUTY = 20      # the duty used when a sensor is lost or on exit
MIN_EMERGENCY_DUTY = 50     # the duty used when a sensor is over the limit

DEFAULT_CURVE = [[30, 20], [40, 30], [50, 50], [60, 75], [70, 100]]


def _default_fan(index: int) -> dict:
    return {
        "index": index,
        "name": f"Fan {index}",
        "enabled": True,
        "mode": "curve",            # curve | fixed | off
        "fixed_duty": 50,
        "sensors": [],              # sensor ids; empty => failsafe duty
        "mix": "max",               # max | avg | min
        "curve": [list(p) for p in DEFAULT_CURVE],
        "min_duty": 20,
        "max_duty": 100,
        "hysteresis": 1.5,          # degC of dead-band on the control temperature
        "ramp_up": 25.0,            # max duty increase per second
        "ramp_down": 3.0,           # max duty decrease per second
        "stop_below": None,         # degC below which the fan is stopped entirely
        "spin_up_duty": 60,         # kick duty used when starting from 0
        "spin_up_ms": 800,
        # None = trust the controller's auto-detection. "dc"/"pwm" force the
        # channel, which is what a 2-wire fan (no tach wire) needs: with nothing
        # to sense, the Commander Pro often reports the channel as empty.
        "force_mode": None,
    }


def default_config() -> dict:
    return {
        "version": 1,
        "http": {"bind": "0.0.0.0", "port": 8899, "auth_token": None},
        "control": {
            "backend": "auto",              # auto | hwmon | liquidctl
            "interval": 2.0,
            "failsafe_duty": 80,
            "emergency_temp": 85.0,
            "emergency_duty": 100,
            "apply_failsafe_on_exit": True,
            "reassert_seconds": 30.0,
        },
        "history": {"seconds": 3600},
        "ui": {
            "theme": "dark",        # dark | light | system
            "accent": "#4aa3ff",
            "favorites": [],        # saved accent colours, newest first, max 10
        },
        "storage": {
            "enabled": True,
            "provider": "arcconf",
            # `command` is NOT settable through the HTTP API -- see
            # Controller.update_config. It names a binary run as root.
            "command": "arcconf",
            "controller": 1,
            "interval": 30.0,
            "timeout": 20.0,
            "stale_after": 120.0,
        },
        "fans": [_default_fan(i) for i in range(1, FAN_COUNT + 1)],
    }


# --------------------------------------------------------------------------
# coercion helpers
# --------------------------------------------------------------------------

def _num(value: Any, lo: float, hi: float, fallback: float) -> float:
    try:
        n = float(value)
    except (TypeError, ValueError):
        return fallback
    if n != n:  # NaN
        return fallback
    return max(lo, min(hi, n))


def _int(value: Any, lo: int, hi: int, fallback: int) -> int:
    return int(round(_num(value, lo, hi, fallback)))


def _one_of(value: Any, allowed: tuple, fallback: str) -> str:
    return value if value in allowed else fallback


def _hex_colour(value: Any, fallback: str) -> str:
    """Accept only #rrggbb. The value ends up in a CSS custom property, so it is
    validated strictly here rather than trusted from the browser."""
    if isinstance(value, str):
        candidate = value.strip().lower()
        if re.fullmatch(r"#[0-9a-f]{6}", candidate):
            return candidate
    return fallback


def _normalize_curve(raw: Any) -> list[list[float]]:
    points: list[list[float]] = []
    if isinstance(raw, list):
        for item in raw:
            if isinstance(item, dict):
                temp, duty = item.get("temp"), item.get("duty")
            elif isinstance(item, (list, tuple)) and len(item) >= 2:
                temp, duty = item[0], item[1]
            else:
                continue
            try:
                points.append([round(float(temp), 1), float(_int(duty, 0, 100, 0))])
            except (TypeError, ValueError):
                continue

    points = [p for p in points if -50.0 <= p[0] <= 150.0]
    if not points:
        points = [list(p) for p in DEFAULT_CURVE]

    # Sort by temperature and drop duplicate x values, keeping the last one so
    # that a point dragged onto its neighbour in the UI wins.
    points.sort(key=lambda p: p[0])
    deduped: list[list[float]] = []
    for point in points:
        if deduped and abs(deduped[-1][0] - point[0]) < 0.05:
            deduped[-1] = point
        else:
            deduped.append(point)
    return deduped[:24]


def _normalize_fan(raw: Any, index: int) -> dict:
    fan = _default_fan(index)
    if not isinstance(raw, dict):
        return fan

    name = raw.get("name", fan["name"])
    fan["name"] = str(name)[:40] if isinstance(name, (str, int, float)) else fan["name"]
    fan["enabled"] = bool(raw.get("enabled", fan["enabled"]))
    fan["mode"] = _one_of(raw.get("mode"), ("curve", "fixed", "off"), fan["mode"])
    fan["fixed_duty"] = _int(raw.get("fixed_duty"), 0, 100, fan["fixed_duty"])
    fan["mix"] = _one_of(raw.get("mix"), ("max", "avg", "min"), fan["mix"])
    fan["curve"] = _normalize_curve(raw.get("curve"))

    sensors = raw.get("sensors")
    if isinstance(sensors, str):
        sensors = [sensors]
    if isinstance(sensors, list):
        fan["sensors"] = [str(s) for s in sensors if isinstance(s, str) and s][:8]

    fan["min_duty"] = _int(raw.get("min_duty"), 0, 100, fan["min_duty"])
    fan["max_duty"] = _int(raw.get("max_duty"), 0, 100, fan["max_duty"])
    if fan["max_duty"] < fan["min_duty"]:
        fan["max_duty"] = fan["min_duty"]

    fan["hysteresis"] = round(_num(raw.get("hysteresis"), 0.0, 20.0, fan["hysteresis"]), 2)
    fan["ramp_up"] = round(_num(raw.get("ramp_up"), 0.5, 100.0, fan["ramp_up"]), 2)
    fan["ramp_down"] = round(_num(raw.get("ramp_down"), 0.5, 100.0, fan["ramp_down"]), 2)

    stop_below = raw.get("stop_below", fan["stop_below"])
    fan["stop_below"] = None if stop_below is None else round(_num(stop_below, -50.0, 150.0, 0.0), 1)

    fan["spin_up_duty"] = _int(raw.get("spin_up_duty"), 0, 100, fan["spin_up_duty"])
    fan["spin_up_ms"] = _int(raw.get("spin_up_ms"), 0, 5000, fan["spin_up_ms"])

    force_mode = raw.get("force_mode", fan["force_mode"])
    fan["force_mode"] = force_mode if force_mode in ("dc", "pwm") else None
    return fan


def normalize(raw: Any) -> dict:
    """Return a fully-populated, range-checked config from arbitrary input."""
    cfg = default_config()
    if not isinstance(raw, dict):
        return cfg

    http_raw = raw.get("http") if isinstance(raw.get("http"), dict) else {}
    bind = http_raw.get("bind", cfg["http"]["bind"])
    cfg["http"]["bind"] = str(bind) if isinstance(bind, str) and bind else cfg["http"]["bind"]
    cfg["http"]["port"] = _int(http_raw.get("port"), 1, 65535, cfg["http"]["port"])
    token = http_raw.get("auth_token")
    cfg["http"]["auth_token"] = str(token) if isinstance(token, str) and token.strip() else None

    ctl_raw = raw.get("control") if isinstance(raw.get("control"), dict) else {}
    ctl = cfg["control"]
    ctl["backend"] = _one_of(ctl_raw.get("backend"), ("auto", "hwmon", "liquidctl"), ctl["backend"])
    ctl["interval"] = round(_num(ctl_raw.get("interval"), 0.5, 60.0, ctl["interval"]), 2)
    # Failsafe and emergency are the duties used precisely when something has
    # gone wrong, so neither may be low enough to leave hardware uncooled, and
    # emergency can never be gentler than failsafe.
    ctl["failsafe_duty"] = _int(ctl_raw.get("failsafe_duty"),
                                MIN_FAILSAFE_DUTY, 100, ctl["failsafe_duty"])
    ctl["emergency_temp"] = round(_num(ctl_raw.get("emergency_temp"), 30.0, 120.0, ctl["emergency_temp"]), 1)
    ctl["emergency_duty"] = _int(ctl_raw.get("emergency_duty"),
                                 MIN_EMERGENCY_DUTY, 100, ctl["emergency_duty"])
    ctl["emergency_duty"] = max(ctl["emergency_duty"], ctl["failsafe_duty"])
    ctl["apply_failsafe_on_exit"] = bool(ctl_raw.get("apply_failsafe_on_exit", ctl["apply_failsafe_on_exit"]))
    ctl["reassert_seconds"] = round(_num(ctl_raw.get("reassert_seconds"), 0.0, 3600.0, ctl["reassert_seconds"]), 1)

    hist_raw = raw.get("history") if isinstance(raw.get("history"), dict) else {}
    cfg["history"]["seconds"] = _int(hist_raw.get("seconds"), 60, 86400, cfg["history"]["seconds"])

    ui_raw = raw.get("ui") if isinstance(raw.get("ui"), dict) else {}
    cfg["ui"]["theme"] = _one_of(ui_raw.get("theme"), ("dark", "light", "system"),
                                 cfg["ui"]["theme"])
    cfg["ui"]["accent"] = _hex_colour(ui_raw.get("accent"), cfg["ui"]["accent"])

    favorites: list[str] = []
    raw_favorites = ui_raw.get("favorites")
    if isinstance(raw_favorites, list):
        for item in raw_favorites:
            colour = _hex_colour(item, "")
            if colour and colour not in favorites:   # validated, de-duplicated
                favorites.append(colour)
    cfg["ui"]["favorites"] = favorites[:MAX_FAVORITES]

    store_raw = raw.get("storage") if isinstance(raw.get("storage"), dict) else {}
    store = cfg["storage"]
    store["enabled"] = bool(store_raw.get("enabled", store["enabled"]))
    store["provider"] = _one_of(store_raw.get("provider"), ("arcconf",), store["provider"])
    command = store_raw.get("command")
    if isinstance(command, str) and command.strip():
        store["command"] = command.strip()
    store["controller"] = _int(store_raw.get("controller"), 1, 16, store["controller"])
    store["interval"] = round(_num(store_raw.get("interval"), 5.0, 3600.0, store["interval"]), 1)
    store["timeout"] = round(_num(store_raw.get("timeout"), 2.0, 120.0, store["timeout"]), 1)
    store["stale_after"] = round(
        _num(store_raw.get("stale_after"), 10.0, 7200.0, store["stale_after"]), 1)
    # A stale window shorter than the poll interval would flap the sensors in
    # and out on every cycle.
    store["stale_after"] = max(store["stale_after"], store["interval"] * 2 + 30.0)

    fans_raw = raw.get("fans") if isinstance(raw.get("fans"), list) else []
    cfg["fans"] = [
        _normalize_fan(fans_raw[i] if i < len(fans_raw) else None, i + 1)
        for i in range(FAN_COUNT)
    ]
    return cfg


# --------------------------------------------------------------------------
# merging
# --------------------------------------------------------------------------

def _deep_merge(base: dict, patch: dict) -> dict:
    """Recursively overlay `patch` on `base`. Lists and scalars replace."""
    result = dict(base)
    for key, value in patch.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def merge(current: dict, patch: Any) -> dict:
    """Overlay a partial config on the running one, then normalise.

    `PUT /api/config` would otherwise be destructive: `normalize()` fills every
    absent key with its *default*, so a request naming only one section would
    silently reset all six fan curves. Merging first means a partial request
    changes exactly what it mentions and nothing else, while the web UI -- which
    always sends the whole document -- still behaves like a plain replacement.

    Fans are merged per channel rather than as a flat list, so patching one fan
    cannot disturb the other five. Lists inside a fan (`sensors`, `curve`) are
    still replaced outright, so they can be emptied or reshaped.
    """
    if not isinstance(patch, dict):
        return normalize(current)

    scalar_patch = {k: v for k, v in patch.items() if k != "fans"}
    merged = _deep_merge(current, scalar_patch)

    incoming = patch.get("fans")
    if isinstance(incoming, list):
        fans = [dict(f) for f in current.get("fans", [])]
        positions = {f.get("index"): i for i, f in enumerate(fans)}
        for offset, item in enumerate(incoming):
            if not isinstance(item, dict):
                continue
            position = positions.get(item.get("index"))
            if position is None:
                position = offset if offset < len(fans) else None
            if position is None:
                continue
            fans[position] = _deep_merge(fans[position], item)
        merged["fans"] = fans

    return normalize(merged)


# --------------------------------------------------------------------------
# persistence
# --------------------------------------------------------------------------

def load(path: str) -> dict:
    """Load and normalise the config, falling back to defaults when missing."""
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return normalize(json.load(handle))
    except FileNotFoundError:
        return default_config()
    except (json.JSONDecodeError, OSError) as exc:
        raise RuntimeError(f"cannot read config {path}: {exc}") from exc


def quarantine(path: str) -> str:
    """Move an unreadable config aside so the daemon can start with defaults.

    The original is kept next to the config as `config.json.corrupt-<time>`
    rather than overwritten, so a hand edit that went wrong can be recovered.
    """
    stamp = time.strftime("%Y%m%d-%H%M%S")
    backup = f"{path}.corrupt-{stamp}"
    os.replace(path, backup)
    return backup


def save(path: str, cfg: dict) -> None:
    """Write the config atomically so a crash can never truncate it."""
    directory = os.path.dirname(os.path.abspath(path)) or "."
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".config-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(cfg, handle, indent=2, sort_keys=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
