"""Fan curve evaluation and per-channel control state.

The daemon computes duty in software rather than uploading a curve to the
device, which is what makes it possible to drive a fan from *any* sensor -- a
CPU package temperature, an NVMe drive -- and not just the Commander Pro's own
probes.  The price is that transitions have to be shaped here, so each channel
carries three pieces of smoothing state:

* a hysteresis dead-band on the control temperature, so a sensor flickering
  between 44 and 45 degrees does not audibly modulate the fan;
* asymmetric slew limits, so the fan ramps up quickly but backs off slowly;
* a spin-up kick, because DC fans will not reliably start from 0% at a low duty.
"""

from __future__ import annotations

import time


def interpolate(curve: list, temp: float) -> float:
    """Linear interpolation over sorted (temp, duty) points, clamped at the ends."""
    if not curve:
        return 0.0
    if temp <= curve[0][0]:
        return float(curve[0][1])
    if temp >= curve[-1][0]:
        return float(curve[-1][1])

    for left, right in zip(curve, curve[1:]):
        if left[0] <= temp <= right[0]:
            span = right[0] - left[0]
            if span <= 0:
                return float(right[1])
            ratio = (temp - left[0]) / span
            return float(left[1]) + ratio * (float(right[1]) - float(left[1]))
    return float(curve[-1][1])


class FanController:
    """Smoothing and slew-rate state for a single fan channel."""

    def __init__(self, index: int):
        self.index = index
        self.control_temp: float | None = None
        self.duty: float = 0.0
        self.target: float = 0.0
        self.stopped = True
        self.reason = "starting"
        self._spin_up_until: float = 0.0
        self._started = False

    def reset(self) -> None:
        self.control_temp = None
        self._spin_up_until = 0.0
        self._started = False

    # -- steps ------------------------------------------------------------

    def _update_control_temp(self, temp: float, hysteresis: float) -> float:
        if self.control_temp is None or abs(temp - self.control_temp) >= hysteresis:
            self.control_temp = temp
        return self.control_temp

    def _raw_target(self, fan: dict, temp: float | None) -> float:
        # Track the control temperature in every mode, not just `curve`. It is a
        # readout as much as a curve input: the UI shows it on the card and the
        # chart plots it, and both used to freeze at the last curve-mode value
        # the moment a channel was switched to fixed.
        control_temp = None
        if temp is not None:
            control_temp = self._update_control_temp(temp, fan["hysteresis"])

        mode = fan["mode"]
        if mode == "off":
            return 0.0
        if mode == "fixed":
            return float(fan["fixed_duty"])

        if control_temp is None:
            return None  # caller substitutes the failsafe duty
        stop_below = fan.get("stop_below")
        if stop_below is not None and control_temp < stop_below:
            return 0.0

        duty = interpolate(fan["curve"], control_temp)
        return max(float(fan["min_duty"]), min(float(fan["max_duty"]), duty))

    def _slew(self, target: float, fan: dict, dt: float, now: float) -> float:
        # Starting from a standstill: hold a kick duty long enough for the
        # rotor to actually spin up before dropping to the requested duty.
        if target > 0 and self.duty <= 0.5:
            if not self._started:
                self._spin_up_until = now + fan["spin_up_ms"] / 1000.0
                self._started = True
            return max(target, float(fan["spin_up_duty"]))

        if now < self._spin_up_until:
            return max(target, float(fan["spin_up_duty"]))

        if target <= 0:
            self._started = False

        delta = target - self.duty
        if delta > 0:
            limit = fan["ramp_up"] * dt
            return self.duty + min(delta, limit)
        limit = fan["ramp_down"] * dt
        return self.duty - min(-delta, limit)

    def step(
        self,
        fan: dict,
        temp: float | None,
        dt: float,
        failsafe_duty: float,
        emergency: bool = False,
        emergency_duty: float = 100.0,
    ) -> float:
        """Advance one control tick and return the duty to command (0-100)."""
        now = time.monotonic()

        if emergency:
            # Skip hysteresis and slew limiting entirely; this is the path that
            # protects hardware.
            self.target = emergency_duty
            self.duty = emergency_duty
            self._started = True
            self.stopped = False
            return self.duty

        target = self._raw_target(fan, temp)
        if target is None:
            target = float(failsafe_duty)
            self.control_temp = None

        self.target = target
        self.duty = max(0.0, min(100.0, self._slew(target, fan, dt, now)))
        self.stopped = self.duty <= 0.5
        return self.duty
