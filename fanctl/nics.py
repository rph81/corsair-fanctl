"""Physical network interfaces on the host: link speed and whether they are up.

Not used for fan control. It is here because this daemon already runs on the
Proxmox host, and the Proxmox API does not report link speeds, so a dashboard
that scales a network gauge to the host's real capacity has nowhere else to
ask. Everything is read from sysfs; nothing is executed.
"""

from __future__ import annotations

import os
import threading
import time

SYS_NET = "/sys/class/net"
CACHE_SECONDS = 10.0


def _read(path: str) -> str | None:
    try:
        with open(path, encoding="ascii", errors="replace") as handle:
            return handle.read().strip()
    except OSError:
        # Reading `speed` on a NIC without carrier fails with EINVAL.
        return None


def read_nics(root: str = SYS_NET) -> list[dict]:
    """Every interface backed by real hardware, sorted by name.

    An interface is physical when it has a `device` link; bridges, bonds,
    VLANs, taps and veths do not. `speed_mbit` is None when the kernel does
    not know it, which is the case for any NIC without a cable in.
    """
    try:
        names = sorted(os.listdir(root))
    except OSError:
        return []
    nics = []
    for name in names:
        base = os.path.join(root, name)
        if not os.path.exists(os.path.join(base, "device")):
            continue
        if os.path.exists(os.path.join(base, "wireless")):
            continue
        state = _read(os.path.join(base, "operstate")) or "unknown"
        carrier = _read(os.path.join(base, "carrier"))
        speed_raw = _read(os.path.join(base, "speed"))
        try:
            speed = int(speed_raw) if speed_raw is not None else None
        except ValueError:
            speed = None
        up = state == "up" or (state == "unknown" and carrier == "1")
        nics.append({
            "name": name,
            "up": up,
            "speed_mbit": speed if speed and speed > 0 else None,
            "state": state,
        })
    return nics


class Nics:
    """read_nics() behind a short cache, since /api/state is polled often."""

    def __init__(self, root: str = SYS_NET, ttl: float = CACHE_SECONDS):
        self.root = root
        self.ttl = ttl
        self._lock = threading.Lock()
        self._at = 0.0
        self._value: list[dict] = []

    def status(self) -> list[dict]:
        now = time.monotonic()
        with self._lock:
            if now - self._at >= self.ttl or not self._at:
                self._value = read_nics(self.root)
                self._at = now
            return [dict(n) for n in self._value]
