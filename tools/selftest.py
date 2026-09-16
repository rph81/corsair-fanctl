#!/usr/bin/env python3
"""Self-tests for the parts that decide how fast your fans spin.

Run with:  python3 tools/selftest.py
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fanctl import backends, config as cfg  # noqa: E402
from fanctl.arcconf import parse as parse_arcconf  # noqa: E402
from fanctl.arcconf import parse_controller  # noqa: E402
from fanctl.controller import Controller  # noqa: E402
from fanctl.curves import FanController, interpolate  # noqa: E402
from fanctl.history import History  # noqa: E402
from fanctl.httpd import make_server  # noqa: E402
from fanctl.sensors import mix  # noqa: E402

FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")

FAILURES: list[str] = []


def check(name: str, actual, expected) -> None:
    if actual == expected:
        print(f"  ok   {name}")
    else:
        FAILURES.append(f"{name}: expected {expected!r}, got {actual!r}")
        print(f"  FAIL {name}: expected {expected!r}, got {actual!r}")


def close(name: str, actual: float, expected: float, tol: float = 1e-6) -> None:
    if abs(actual - expected) <= tol:
        print(f"  ok   {name}")
    else:
        FAILURES.append(f"{name}: expected ~{expected}, got {actual}")
        print(f"  FAIL {name}: expected ~{expected}, got {actual}")


def test_interpolation() -> None:
    print("curve interpolation")
    curve = [[30, 20], [50, 50], [70, 100]]
    close("below first point clamps", interpolate(curve, 10), 20)
    close("above last point clamps", interpolate(curve, 95), 100)
    close("on a point", interpolate(curve, 50), 50)
    close("midway on first segment", interpolate(curve, 40), 35)
    close("midway on second segment", interpolate(curve, 60), 75)
    close("empty curve is 0%", interpolate([], 50), 0)
    close("single point is flat", interpolate([[40, 42]], 90), 42)


def test_mix() -> None:
    print("sensor mixing")
    close("max", mix([30.0, 55.0, 41.0], "max"), 55.0)
    close("min", mix([30.0, 55.0, 41.0], "min"), 30.0)
    close("avg", mix([30.0, 50.0], "avg"), 40.0)


def test_config_normalisation() -> None:
    print("config normalisation")
    base = cfg.default_config()
    check("six fans by default", len(base["fans"]), 6)

    hostile = cfg.normalize({
        "fans": [{"index": 1, "min_duty": 900, "max_duty": -5, "mode": "banana",
                  "curve": [[70, 100], [30, "20"], [30.02, 25], ["x", 1]],
                  "hysteresis": "abc", "sensors": "cpro:temp1"}],
        "control": {"interval": 0.001, "emergency_temp": 999},
        "http": {"port": 70000, "auth_token": "   "},
    })
    fan = hostile["fans"][0]
    check("duty clamped to 100", fan["min_duty"], 100)
    check("max_duty raised to min_duty", fan["max_duty"], 100)
    check("unknown mode falls back", fan["mode"], "curve")
    check("curve sorted", [p[0] for p in fan["curve"]], [30.0, 70.0])
    check("near-duplicate x collapsed", fan["curve"][0][1], 25.0)
    check("garbage point dropped", len(fan["curve"]), 2)
    check("non-numeric hysteresis falls back", fan["hysteresis"], 1.5)
    check("bare sensor string accepted", fan["sensors"], ["cpro:temp1"])
    check("interval floored", hostile["control"]["interval"], 0.5)
    check("emergency temp capped", hostile["control"]["emergency_temp"], 120.0)

    # The duties used when something is wrong may never be low enough to
    # leave hardware uncooled.
    floors = cfg.normalize({"control": {"failsafe_duty": 0, "emergency_duty": 0}})
    check("failsafe duty floored", floors["control"]["failsafe_duty"], cfg.MIN_FAILSAFE_DUTY)
    check("emergency duty floored", floors["control"]["emergency_duty"], cfg.MIN_EMERGENCY_DUTY)
    ordered = cfg.normalize({"control": {"failsafe_duty": 90, "emergency_duty": 60}})
    check("emergency never gentler than failsafe", ordered["control"]["emergency_duty"], 90)
    check("port clamped", hostile["http"]["port"], 65535)
    check("blank token becomes None", hostile["http"]["auth_token"], None)
    check("missing fans backfilled", len(hostile["fans"]), 6)

    check("garbage input yields defaults", cfg.normalize("nonsense")["version"], 1)


def test_sensor_naming_and_chart() -> None:
    print("sensor naming and chart selection")
    normalised = cfg.normalize({
        "sensor_names": {
            "cpro:temp1": "  Drive cage intake  ",   # trimmed
            "cpro:temp2": "",                        # blank means "no name"
            "cpro:temp3": "x" * 80,                  # capped
            "bad-value": 123,                        # not a string
            7: "numeric key",                        # not a string key
        },
        "ui": {"chart_sensors": ["a", "a", "b", 7, ""]},
    })
    names = normalised["sensor_names"]
    check("name trimmed", names.get("cpro:temp1"), "Drive cage intake")
    check("blank name dropped", "cpro:temp2" in names, False)
    check("name length capped", len(names["cpro:temp3"]), 40)
    check("non-string value dropped", "bad-value" in names, False)
    check("chart sensors de-duplicated", normalised["ui"]["chart_sensors"], ["a", "b"])

    check("defaults are empty", cfg.default_config()["sensor_names"], {})

    # Renaming must not disturb anything else: the UI sends only this key.
    current = cfg.default_config()
    current["fans"][0]["name"] = "Front intake"
    current["fans"][0]["sensors"] = ["cpro:temp1"]
    merged = cfg.merge(current, {"sensor_names": {"cpro:temp1": "Intake"}})
    check("fan survives a rename PUT", merged["fans"][0]["name"], "Front intake")
    check("rename applied", merged["sensor_names"]["cpro:temp1"], "Intake")

    # Clearing needs a whole-map replacement, which is what the reset button sends.
    cleared = cfg.merge(merged, {"sensor_names": {}})
    check("names can be cleared", cleared["sensor_names"], {})

    # Removing one name is the same mechanism: an absent key must mean removed,
    # which a recursive merge could not express.
    two = cfg.merge(current, {"sensor_names": {"cpro:temp1": "A", "cpro:temp2": "B"}})
    one = cfg.merge(two, {"sensor_names": {"cpro:temp2": "B"}})
    check("a single name can be removed", one["sensor_names"], {"cpro:temp2": "B"})


def test_control_temp_in_every_mode() -> None:
    print("control temperature outside curve mode")
    fan = cfg.default_config()["fans"][0]
    fan["hysteresis"] = 0.0
    fan["mode"] = "fixed"
    fan["fixed_duty"] = 42
    fan["ramp_up"] = 100.0
    fan["ramp_down"] = 100.0
    fan["spin_up_ms"] = 0

    controller = FanController(1)
    controller.duty = 42.0
    controller._started = True

    # A fixed channel still reads its sensors: the card and the chart show the
    # temperature even though the curve is not driving the duty.
    controller.step(fan, 45.0, dt=1.0, failsafe_duty=80)
    close("fixed mode tracks the sensor", controller.control_temp, 45.0)
    close("fixed mode holds its duty", controller.duty, 42.0)

    controller.step(fan, 51.0, dt=1.0, failsafe_duty=80)
    close("and keeps tracking as it moves", controller.control_temp, 51.0)
    close("duty still fixed", controller.duty, 42.0)

    # "off" behaves the same way for the readout.
    fan["mode"] = "off"
    controller.step(fan, 55.0, dt=1.0, failsafe_duty=80)
    close("off mode still reports temperature", controller.control_temp, 55.0)
    close("but stops the fan", controller.duty, 0.0)

    # With no sensors there is nothing to report, in any mode.
    fan["mode"] = "fixed"
    controller = FanController(1)
    controller.step(fan, None, dt=1.0, failsafe_duty=80)
    check("no sensor means no reading", controller.control_temp, None)
    close("fixed duty still applied", controller.target, 42.0)


def test_persistence() -> None:
    print("config persistence")
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "nested", "config.json")
        original = cfg.default_config()
        original["fans"][0]["name"] = "Front intake"
        cfg.save(path, original)
        check("round-trips through disk", cfg.load(path)["fans"][0]["name"], "Front intake")

        with open(path, "w", encoding="utf-8") as handle:
            handle.write("{not json")
        try:
            cfg.load(path)
        except RuntimeError:
            print("  ok   corrupt file raises rather than silently resetting")
        else:
            FAILURES.append("corrupt config did not raise")
            print("  FAIL corrupt config did not raise")

        missing = os.path.join(tmp, "absent.json")
        check("missing file yields defaults", cfg.load(missing)["version"], 1)


def test_control_behaviour() -> None:
    print("control behaviour")
    fan = cfg.default_config()["fans"][0]
    fan["curve"] = [[30, 20], [60, 100]]
    fan["min_duty"] = 0
    fan["hysteresis"] = 2.0
    fan["ramp_up"] = 10.0
    fan["ramp_down"] = 5.0
    fan["spin_up_duty"] = 60
    fan["spin_up_ms"] = 0          # take spin-up out of the picture for these

    controller = FanController(1)
    controller.duty = 40.0
    controller._started = True

    controller.step(fan, 45.0, dt=1.0, failsafe_duty=80)
    close("ramps up at the configured rate", controller.duty, 50.0)
    close("target is the curve value", controller.target, 60.0)

    controller.duty = 40.0
    controller.control_temp = None
    controller.step(fan, 30.0, dt=1.0, failsafe_duty=80)
    close("ramps down at the configured rate", controller.duty, 35.0)

    # Hysteresis: a small wobble must not move the control temperature.
    controller = FanController(1)
    controller.duty = 50.0
    controller._started = True
    controller.step(fan, 45.0, dt=1.0, failsafe_duty=80)
    first = controller.control_temp
    controller.step(fan, 46.0, dt=1.0, failsafe_duty=80)
    close("small wobble ignored", controller.control_temp, first)
    controller.step(fan, 47.5, dt=1.0, failsafe_duty=80)
    close("move beyond the dead-band tracked", controller.control_temp, 47.5)

    # No sensor reading at all -> failsafe, not silence.
    controller = FanController(1)
    controller.duty = 80.0
    controller._started = True
    controller.step(fan, None, dt=100.0, failsafe_duty=80)
    close("failsafe when the sensor is gone", controller.duty, 80.0)

    # Emergency bypasses hysteresis and slew limiting entirely.
    controller = FanController(1)
    controller.duty = 20.0
    duty = controller.step(fan, 95.0, dt=0.1, failsafe_duty=80,
                           emergency=True, emergency_duty=100)
    close("emergency jumps straight to full", duty, 100.0)

    # Spin-up kick from standstill.
    fan["spin_up_ms"] = 1000
    controller = FanController(1)
    controller.duty = 0.0
    duty = controller.step(fan, 31.0, dt=1.0, failsafe_duty=80)
    close("kick duty applied when starting from rest", duty, 60.0)

    # stop_below wins over min_duty so zero-rpm setups actually stop.
    fan["stop_below"] = 35.0
    fan["min_duty"] = 30
    fan["spin_up_ms"] = 0
    controller = FanController(1)
    controller.duty = 30.0
    controller._started = True
    controller.step(fan, 30.0, dt=100.0, failsafe_duty=80)
    close("stops below the threshold", controller.duty, 0.0)


def test_config_merge() -> None:
    print("config merging")
    current = cfg.default_config()
    current["fans"][0]["name"] = "Front intake"
    current["fans"][0]["sensors"] = ["cpro:temp1"]
    current["fans"][2]["name"] = "Drive cage"
    current["fans"][2]["curve"] = [[35, 20], [50, 90]]

    # A partial PUT naming only one section must not disturb the fans.
    merged = cfg.merge(current, {"storage": {"interval": 45}})
    check("unrelated section untouched", merged["fans"][0]["name"], "Front intake")
    check("other fan untouched", merged["fans"][2]["name"], "Drive cage")
    check("patched value applied", merged["storage"]["interval"], 45.0)

    # Patching one fan leaves the others alone.
    merged = cfg.merge(current, {"fans": [{"index": 3, "mode": "fixed"}]})
    check("targeted fan patched", merged["fans"][2]["mode"], "fixed")
    check("its other fields survive", merged["fans"][2]["curve"], [[35.0, 20.0], [50.0, 90.0]])
    check("fan 1 untouched", merged["fans"][0]["name"], "Front intake")

    # Lists still replace, so a sensor selection can be cleared.
    merged = cfg.merge(current, {"fans": [{"index": 1, "sensors": []}]})
    check("sensor list cleared", merged["fans"][0]["sensors"], [])

    # A full document still behaves like a replacement.
    full = cfg.default_config()
    full["fans"][0]["name"] = "Renamed"
    merged = cfg.merge(current, full)
    check("full replacement wins", merged["fans"][0]["name"], "Renamed")
    check("full replacement clears old fan 3", merged["fans"][2]["name"], "Fan 3")


def test_arcconf_controller_parsing() -> None:
    print("arcconf controller parsing")
    with open(os.path.join(FIXTURES, "arcconf-ad.txt"), encoding="utf-8") as handle:
        info = parse_controller(handle.read(), controller=1)

    check("model", info["model"], "MSCC SmartHBA 2100-4i4e")
    check("firmware", info["firmware"], "1.98")
    check("all four sensors found", len(info["sensors"]), 4)
    check("ids slugged from location",
          [s["id"] for s in info["sensors"]],
          ["arcconf:1:ctrl:inlet-ambient", "arcconf:1:ctrl:asic",
           "arcconf:1:ctrl:top", "arcconf:1:ctrl:bottom"])
    close("asic temperature", info["sensors"][1]["temperature"], 48.0)
    close("asic peak", info["sensors"][1]["temperature_max"], 49.0)

    # The Connector section further down also has a "Location" key; it must not
    # overwrite the last sensor's.
    check("connector section did not bleed in", info["sensors"][3]["location"], "Bottom")

    # Identifiers that must never reach the API.
    blob = repr(info)
    check("serial not parsed", "EXAMPLECTRLSN" in blob, False)
    check("world-wide name not parsed", "5000000000000FF0" in blob, False)

    # Older firmware: headline line only, no sensors section.
    headline_only = """   Controller Model                    : Adaptec ASR-8805
   Controller Serial Number            : SECRET123
   Temperature                         : 52 C/ 125 F (Normal)
   Firmware                            : 7.11
"""
    old = parse_controller(headline_only, 2)
    check("falls back to the headline reading", len(old["sensors"]), 1)
    check("fallback id", old["sensors"][0]["id"], "arcconf:2:ctrl")
    close("fallback temperature", old["sensors"][0]["temperature"], 52.0)
    check("fallback peak is unknown", old["sensors"][0]["temperature_max"], None)

    # When both shapes are present the named sensors win, so the headline is not
    # exposed a second time under a different id.
    with open(os.path.join(FIXTURES, "arcconf-ad.txt"), encoding="utf-8") as handle:
        both = parse_controller(handle.read(), 1)
    check("headline not duplicated",
          [s["id"] for s in both["sensors"] if s["id"].endswith(":ctrl")], [])

    check("empty input", parse_controller("", 1)["sensors"], [])
    check("garbage input", parse_controller("nonsense\nlines", 1)["sensors"], [])

    # A sensor with no location falls back to its numeric id.
    unlabelled = """   Sensor ID    : 7
   Current Value : 60 deg C
"""
    check("unlabelled sensor id",
          parse_controller(unlabelled, 1)["sensors"][0]["id"], "arcconf:1:ctrl:sensor7")

    # Two sensors sharing a location must not collide.
    duplicate = """   Sensor ID    : 0
   Current Value : 40 deg C
   Location      : ASIC

   Sensor ID    : 1
   Current Value : 44 deg C
   Location      : ASIC
"""
    check("duplicate locations disambiguated",
          [s["id"] for s in parse_controller(duplicate, 1)["sensors"]],
          ["arcconf:1:ctrl:asic", "arcconf:1:ctrl:asic-1"])


def test_arcconf_parsing() -> None:
    print("arcconf parsing")
    with open(os.path.join(FIXTURES, "arcconf-pd.txt"), encoding="utf-8") as handle:
        drives = parse_arcconf(handle.read(), controller=1)

    check("enclosure device excluded", len(drives), 4)
    check("ids keyed on physical slot",
          [d["id"] for d in drives],
          ["arcconf:1:slot1", "arcconf:1:slot2", "arcconf:1:slot6", "arcconf:1:slot7"])
    check("sorted by slot, not device order", [d["device"] for d in drives], [0, 1, 6, 5])

    first = drives[0]
    check("device path extracted", first["path"], "/dev/sda")
    check("model", first["model"], "MZILT800HBHQ0D3")
    check("serial", first["serial"], "EXAMPLESERIAL001")
    close("current temperature", first["temperature"], 40.0)
    close("peak temperature", first["temperature_max"], 45.0)
    close("threshold", first["temperature_threshold"], 74.0)
    close("usage remaining", first["usage_remaining"], 63.0)
    close("power on hours", first["power_on_hours"], 43822.0)
    check("ssd flag", first["ssd"], True)
    check("no smart warning", first["smart_warning"], False)

    # The Phy and Error Counter sub-sections must not bleed into drive fields.
    check("nested sections ignored", drives[1]["model"], "MZILT800HBHQ0D3")

    # Robustness against other arcconf builds.
    check("empty input", parse_arcconf("", 1), [])
    check("garbage input", parse_arcconf("total nonsense\nwith lines", 1), [])

    odd = """   Channel #0:
      Device #0
         Device is a Hard drive
         Disk Name                                       : /dev/sdz
         Current Temperature                             : Not Applicable
         Reported Location                               : Direct Attached, Slot 4(Connector 0:CN0)
"""
    parsed = parse_arcconf(odd, 2)
    check("unreadable temperature kept as None", parsed[0]["temperature"], None)
    check("controller number in id", parsed[0]["id"], "arcconf:2:slot4")

    no_slot = """   Channel #1:
      Device #3
         Device is a Hard drive
         Current Temperature                             : 51 deg C
"""
    parsed = parse_arcconf(no_slot, 1)
    check("falls back to channel/device id", parsed[0]["id"], "arcconf:1:c1d3")


def _fake_cpro_tree(root: str, fans: dict) -> str:
    """A corsair-cpro hwmon directory with pwmN only for detected channels."""
    path = os.path.join(root, "hwmon0")
    os.makedirs(path, exist_ok=True)
    with open(os.path.join(path, "name"), "w", encoding="utf-8") as handle:
        handle.write("corsair-cpro")
    for index, kind in fans.items():
        if kind is None:
            continue
        for name, value in ((f"fan{index}_label", f"fan{index} {kind}"),
                            (f"fan{index}_input", "900"), (f"pwm{index}", "0")):
            with open(os.path.join(path, name), "w", encoding="utf-8") as handle:
                handle.write(value)
    return path


def test_hwmon_empty_channel_one() -> None:
    print("hwmon backend with an empty channel 1")
    saved_root = backends.HWMON_ROOT
    try:
        with tempfile.TemporaryDirectory() as tmp:
            backends.HWMON_ROOT = tmp
            # The kernel driver creates no pwm1 when nothing is on channel 1.
            _fake_cpro_tree(tmp, {1: None, 2: "4pin", 3: "3pin"})
            backend = backends.HwmonBackend()
            try:
                backend.open()
                print("  ok   opens when pwm1 is absent")
            except backends.BackendError as exc:
                FAILURES.append(f"empty channel 1 blocked the backend: {exc}")
                print(f"  FAIL empty channel 1 blocked the backend: {exc}")
            fans = {f["index"]: f["connected"] for f in backend.describe()["fans"]}
            check("channel 1 reported empty", fans[1], False)
            check("channel 2 reported connected", fans[2], True)

        with tempfile.TemporaryDirectory() as tmp:
            backends.HWMON_ROOT = tmp
            _fake_cpro_tree(tmp, {i: None for i in range(1, 7)})
            try:
                backends.HwmonBackend().open()
            except backends.BackendError:
                print("  ok   no pwm attributes at all is still an error")
            else:
                FAILURES.append("device with no pwm attributes opened")
                print("  FAIL device with no pwm attributes opened")
    finally:
        backends.HWMON_ROOT = saved_root


class _FakeBackend:
    name = "fake"

    def __init__(self, temps: dict):
        self.temps = temps
        self.written: dict = {}

    def describe(self) -> dict:
        return {"backend": self.name, "device": "fake", "path": None, "firmware": None,
                "fans": [{"index": i, "connected": True, "type": "PWM", "note": None}
                         for i in range(1, 7)],
                "probes": [{"index": i, "connected": i in self.temps} for i in range(1, 5)]}

    def read(self) -> dict:
        return {"rpm": {}, "temps": dict(self.temps), "volts": {}}

    def set_duty(self, index: int, duty: float) -> None:
        self.written[index] = duty

    def close(self) -> None:
        return


def test_emergency_ignores_mix() -> None:
    print("emergency detection")
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "config.json")
        config = cfg.default_config()
        config["control"]["emergency_temp"] = 85.0
        config["fans"][0]["sensors"] = ["cpro:temp1", "cpro:temp2"]
        config["fans"][0]["mix"] = "min"        # would hide the hot probe
        cfg.save(path, config)

        controller = Controller(path)
        backend = _FakeBackend({1: 40.0, 2: 95.0})
        controller._backend = backend
        controller._description = backend.describe()
        controller._tick(1.0)

        fan = controller.snapshot()["fans"][0]
        check("hot probe trips emergency despite mix=min", fan["reason"], "emergency")
        close("emergency duty written", backend.written[1], 100.0)


def test_corrupt_config_does_not_stop_control() -> None:
    print("corrupt config at startup")
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "config.json")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("{not json")
        controller = Controller(path)
        check("started on defaults", controller.config["version"], 1)
        check("warning set", bool(controller.config_warning), True)
        check("warning in snapshot", bool(controller.snapshot()["warning"]), True)
        backups = [n for n in os.listdir(tmp) if n.startswith("config.json.corrupt-")]
        check("bad file kept as a backup", len(backups), 1)
        check("original path freed", os.path.exists(path), False)


class _StubController:
    """Just enough of Controller for the HTTP layer."""

    def __init__(self):
        self.config = cfg.default_config()
        self.calls: list = []

    def snapshot(self) -> dict:
        return {"config": self.config}

    def patch_fan(self, index: int, changes: dict) -> dict:
        self.calls.append(("patch_fan", index, changes))
        return self.config

    def reconnect(self) -> None:
        self.calls.append(("reconnect",))


def test_http_refuses_cross_site() -> None:
    print("http cross-site protection")
    stub = _StubController()
    server = make_server(stub, "127.0.0.1", 0, None)
    host, port = server.server_address[:2]
    base = f"http://{host}:{port}"
    import threading
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    def request(method, path, body=None, headers=None):
        req = urllib.request.Request(base + path, data=body, method=method,
                                     headers=headers or {})
        try:
            with urllib.request.urlopen(req, timeout=5) as response:
                return response.status
        except urllib.error.HTTPError as exc:
            return exc.code

    try:
        check("GET state allowed", request("GET", "/api/state"), 200)
        check("text/plain POST refused (no CORS preflight for it)",
              request("POST", "/api/fan/1", b'{"mode":"off"}',
                      {"Content-Type": "text/plain"}), 403)
        check("form-encoded POST refused",
              request("POST", "/api/fan/1", b'{"mode":"off"}',
                      {"Content-Type": "application/x-www-form-urlencoded"}), 403)
        check("foreign Origin refused even with JSON",
              request("POST", "/api/fan/1", b'{"mode":"off"}',
                      {"Content-Type": "application/json",
                       "Origin": "http://evil.example"}), 403)
        check("cross-site Sec-Fetch-Site refused",
              request("POST", "/api/reconnect", None,
                      {"Sec-Fetch-Site": "cross-site"}), 403)
        check("nothing was applied", stub.calls, [])
        check("same-origin JSON POST allowed",
              request("POST", "/api/fan/1", b'{"mode":"off"}',
                      {"Content-Type": "application/json",
                       "Origin": f"http://{host}:{port}"}), 200)
        check("JSON POST without Origin allowed (curl)",
              request("POST", "/api/fan/1", b'{"mode":"fixed"}',
                      {"Content-Type": "application/json"}), 200)
        check("bodiless same-origin POST allowed",
              request("POST", "/api/reconnect", None,
                      {"Sec-Fetch-Site": "same-origin"}), 200)
        check("allowed requests reached the controller", len(stub.calls), 3)
    finally:
        server.shutdown()
        server.server_close()


def _sample(t: float, temp: float = 40.0) -> dict:
    return {"t": t, "temps": {"cpro:temp1": temp}, "rpm": {"1": 900}, "duty": {"1": 50.0}}


def test_history_persistence() -> None:
    print("history persistence")
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "state", "history.json")   # directory does not exist yet
        now = time.time()

        history = History(3600, 2.0, path=path)
        for offset in (-300, -200, -100):
            history.append(_sample(now + offset))
        check("save creates the directory and file", history.save(), True)
        check("file exists", os.path.isfile(path), True)
        check("clean after save", history.save(), True)   # nothing dirty: still fine

        restored = History(3600, 2.0, path=path)
        check("samples restored", restored.load(), 3)
        check("restored in order", [round(s["t"] - now) for s in restored.series()], [-300, -200, -100])

        # Samples outside the retention window, from the future, or malformed
        # are dropped on load.
        with open(path, "w", encoding="utf-8") as handle:
            json.dump({"version": 1, "samples": [
                _sample(now - 7200),               # older than the 1 h window
                _sample(now + 3600),               # clock went backwards
                {"t": now - 10},                   # missing fields
                "garbage",
                _sample(now - 20),
            ]}, handle)
        filtered = History(3600, 2.0, path=path)
        check("stale, future and malformed samples dropped", filtered.load(), 1)

        with open(path, "w", encoding="utf-8") as handle:
            handle.write("{not json")
        broken = History(3600, 2.0, path=path)
        check("corrupt file ignored", broken.load(), 0)

        # Persistence off: nothing is written and load is a no-op.
        off = History(3600, 2.0, path=None)
        off.append(_sample(now))
        check("no path means no write", off.save(), False)
        check("no path means no load", off.load(), 0)

        # An unwritable location must not raise: charts are not fan control.
        unwritable = History(3600, 2.0, path=os.path.join(tmp, "file-not-dir", "x.json"))
        with open(os.path.join(tmp, "file-not-dir"), "w", encoding="utf-8") as handle:
            handle.write("")
        unwritable.append(_sample(now))
        check("unwritable path fails quietly", unwritable.save(), False)

        # maybe_save honours the interval.
        paced = History(3600, 2.0, path=path)
        paced.append(_sample(now))
        paced.maybe_save(every=3600)
        check("maybe_save waits for the interval", paced._dirty, True)
        paced.maybe_save(every=0)
        check("maybe_save writes once due", paced._dirty, False)


def test_history_config() -> None:
    print("history config")
    base = cfg.default_config()
    check("default retention is 7 hours", base["history"]["seconds"], 25200)
    check("persist on by default", base["history"]["persist"], True)
    check("default file", base["history"]["file"], cfg.DEFAULT_HISTORY_FILE)

    hostile = cfg.normalize({"history": {"save_interval": 1, "file": "  ", "persist": 0}})
    check("save interval floored", hostile["history"]["save_interval"], 10.0)
    check("blank file keeps default", hostile["history"]["file"], cfg.DEFAULT_HISTORY_FILE)
    check("persist coerced to bool", hostile["history"]["persist"], False)

    # history.file is a path written as root: file-only, never via the API.
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "config.json")
        seeded = cfg.default_config()
        seeded["history"]["file"] = os.path.join(tmp, "history.json")
        cfg.save(path, seeded)
        controller = Controller(path)
        updated = controller.update_config({"history": {"file": "/etc/passwd", "seconds": 600}})
        check("history.file cannot be changed over the API",
              updated["history"]["file"], os.path.join(tmp, "history.json"))
        check("other history settings still apply", updated["history"]["seconds"], 600)


def main() -> int:
    for test in (test_interpolation, test_mix, test_config_normalisation,
                 test_config_merge, test_arcconf_parsing, test_arcconf_controller_parsing,
                 test_persistence, test_control_behaviour,
                 test_hwmon_empty_channel_one, test_emergency_ignores_mix,
                 test_corrupt_config_does_not_stop_control,
                 test_http_refuses_cross_site,
                 test_history_persistence, test_history_config,
                 test_sensor_naming_and_chart, test_control_temp_in_every_mode):
        test()
        print()
    if FAILURES:
        print(f"{len(FAILURES)} failure(s):")
        for failure in FAILURES:
            print(f"  - {failure}")
        return 1
    print("all tests passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
