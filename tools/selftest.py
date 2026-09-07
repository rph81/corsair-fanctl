#!/usr/bin/env python3
"""Self-tests for the parts that decide how fast your fans spin.

Run with:  python3 tools/selftest.py
"""

from __future__ import annotations

import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fanctl import config as cfg  # noqa: E402
from fanctl.arcconf import parse as parse_arcconf  # noqa: E402
from fanctl.curves import FanController, interpolate  # noqa: E402
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
    check("port clamped", hostile["http"]["port"], 65535)
    check("blank token becomes None", hostile["http"]["auth_token"], None)
    check("missing fans backfilled", len(hostile["fans"]), 6)

    check("garbage input yields defaults", cfg.normalize("nonsense")["version"], 1)


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


def main() -> int:
    for test in (test_interpolation, test_mix, test_config_normalisation,
                 test_config_merge, test_arcconf_parsing,
                 test_persistence, test_control_behaviour):
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
