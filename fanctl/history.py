"""Time series used by the web UI's charts.

Samples live in a bounded in-memory ring buffer. When a file path is given the
buffer is also written to disk on a slow schedule and on shutdown, and read
back at startup, so a service restart does not wipe the chart. It is one
bounded file overwritten in place, never an accumulating log: a hypervisor
host should not fill up with telemetry.

Persistence is strictly best-effort. A missing directory, a full disk or a
corrupt file is logged once and otherwise ignored; fan control never depends
on it.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
import time
from collections import deque

LOG = logging.getLogger("fanctl.history")

# A sample stamped further in the future than this is a clock problem, not data.
FUTURE_SLACK = 60.0


class History:
    def __init__(self, seconds: float, interval: float, path: str | None = None):
        self._lock = threading.Lock()
        self._seconds = float(seconds)
        self._samples: deque = deque(maxlen=self._capacity(seconds, interval))
        self._path = path
        self._dirty = False
        self._last_save = time.monotonic()
        self._warned = False

    @staticmethod
    def _capacity(seconds: float, interval: float) -> int:
        return max(60, min(20000, int(seconds / max(interval, 0.5)) + 1))

    @property
    def path(self) -> str | None:
        return self._path

    def resize(self, seconds: float, interval: float) -> None:
        capacity = self._capacity(seconds, interval)
        with self._lock:
            self._seconds = float(seconds)
            if capacity != self._samples.maxlen:
                self._samples = deque(self._samples, maxlen=capacity)

    def append(self, sample: dict) -> None:
        with self._lock:
            self._samples.append(sample)
            self._dirty = True

    def series(self, since: float = 0.0, max_points: int = 600) -> list[dict]:
        """Return samples newer than `since`, decimated to at most `max_points`."""
        with self._lock:
            samples = [s for s in self._samples if s["t"] >= since]
        if len(samples) <= max_points:
            return samples
        stride = len(samples) / max_points
        picked = [samples[int(i * stride)] for i in range(max_points)]
        if picked[-1] is not samples[-1]:
            picked[-1] = samples[-1]
        return picked

    # -- persistence ------------------------------------------------------

    def load(self) -> int:
        """Read samples saved by a previous run. Returns how many were kept."""
        if not self._path:
            return 0
        try:
            with open(self._path, "r", encoding="utf-8") as handle:
                payload = json.load(handle)
        except FileNotFoundError:
            return 0
        except (OSError, ValueError) as exc:
            LOG.warning("ignoring history file %s: %s", self._path, exc)
            return 0

        raw = payload.get("samples") if isinstance(payload, dict) else None
        if not isinstance(raw, list):
            LOG.warning("ignoring history file %s: unexpected layout", self._path)
            return 0

        now = time.time()
        with self._lock:
            oldest = now - self._seconds
            kept = [
                s for s in raw
                if isinstance(s, dict) and isinstance(s.get("t"), (int, float))
                and oldest <= s["t"] <= now + FUTURE_SLACK
                and isinstance(s.get("temps"), dict) and isinstance(s.get("rpm"), dict)
                and isinstance(s.get("duty"), dict)
            ]
            kept.sort(key=lambda s: s["t"])
            # Anything already buffered is newer than the file; keep it last.
            live = list(self._samples)
            self._samples = deque(kept + live, maxlen=self._samples.maxlen)
            self._dirty = False
        if kept:
            LOG.info("restored %d history samples from %s", len(kept), self._path)
        return len(kept)

    def maybe_save(self, every: float) -> None:
        """Write to disk if anything changed and `every` seconds have passed."""
        if not self._path or not self._dirty:
            return
        if time.monotonic() - self._last_save < every:
            return
        self.save()

    def save(self) -> bool:
        """Write the buffer atomically. Returns False if it could not be written."""
        if not self._path:
            return False
        with self._lock:
            if not self._dirty:
                return True
            samples = list(self._samples)
        body = json.dumps({"version": 1, "saved": time.time(), "samples": samples},
                          separators=(",", ":"))

        directory = os.path.dirname(os.path.abspath(self._path)) or "."
        tmp = None
        try:
            os.makedirs(directory, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=directory, prefix=".history-", suffix=".json")
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(body)
            os.replace(tmp, self._path)
        except OSError as exc:
            if tmp:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
            if not self._warned:
                LOG.warning("cannot save history to %s: %s (charts will not "
                            "survive a restart)", self._path, exc)
                self._warned = True
            self._last_save = time.monotonic()
            return False

        with self._lock:
            self._dirty = False
        self._last_save = time.monotonic()
        self._warned = False
        return True
