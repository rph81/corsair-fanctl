"""In-memory time series used by the web UI's charts.

Nothing is written to disk: a hypervisor host should not accumulate telemetry
files, and a few thousand samples is all the UI ever draws.
"""

from __future__ import annotations

import threading
from collections import deque


class History:
    def __init__(self, seconds: float, interval: float):
        self._lock = threading.Lock()
        self._samples: deque = deque(maxlen=self._capacity(seconds, interval))

    @staticmethod
    def _capacity(seconds: float, interval: float) -> int:
        return max(60, min(20000, int(seconds / max(interval, 0.5)) + 1))

    def resize(self, seconds: float, interval: float) -> None:
        capacity = self._capacity(seconds, interval)
        with self._lock:
            if capacity != self._samples.maxlen:
                self._samples = deque(self._samples, maxlen=capacity)

    def append(self, sample: dict) -> None:
        with self._lock:
            self._samples.append(sample)

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
