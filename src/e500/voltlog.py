"""Overnight 12 V battery logger: finds a parasitic drain without waking the car.

Only the adapter's own voltage reading (ATRV) is used: it measures OBD pin 16, which is
wired to the battery with the ignition off, and puts nothing on the car's bus, so no
module is woken by the logging. The adapter itself draws a few tens of mA while awake,
which is in the log too (a constant, small offset).

What a night of readings shows:
- the drop rate (mV per hour) once the surface charge has settled (an hour or two in);
- steps: sudden dips or recoveries of >= 0.1 V, i.e. something switching on or off;
- the resting voltage in the morning (12.6+ V full, 12.4 V ~75 %, 12.2 V ~50 %).
It doesn't say *which* circuit drains: that needs a meter in series and pulling fuses.
The adapter reads in 0.1 V steps; per-minute averages of a reading every few seconds
recover finer changes when the reading flickers between two steps.
"""

from __future__ import annotations

import json
import statistics as st
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

STEP_V = 0.1  # a change this big between minute averages counts as a step
SETTLE_H = 1.0  # surface charge: ignore the first hour for the drop rate


@dataclass
class Night:
    t0: float = 0.0
    readings: int = 0
    missed: int = 0
    last: float | None = None
    minutes: list[tuple[float, float]] = field(default_factory=list)  # (t, mean V)
    steps: list[tuple[float, float]] = field(default_factory=list)  # (t, change V)

    def rate_mv_per_h(self) -> float | None:
        """Least-squares slope of the minute averages after the settling hour."""
        pts = [(t, v) for t, v in self.minutes if t - self.t0 >= SETTLE_H * 3600]
        if len(pts) < 30:
            return None
        ts, vs = [t for t, _ in pts], [v for _, v in pts]
        mt, mv = st.mean(ts), st.mean(vs)
        den = sum((t - mt) ** 2 for t in ts)
        return 1000 * 3600 * sum((t - mt) * (v - mv) for t, v in pts) / den if den else None


def run(read: Callable[[], float | None], out: Path, every: float = 5.0,
        show: Callable[[Night], None] = lambda n: None, max_readings: int | None = None,
        clock: Callable[[], float] = time.time, sleep: Callable[[float], None] = time.sleep) -> Night:
    """Read until KeyboardInterrupt (or max_readings). One JSON line per reading."""
    night = Night(t0=clock())
    bucket, minute = [], None
    with out.open("a", buffering=1) as f:
        while max_readings is None or night.readings + night.missed < max_readings:
            t = clock()
            v = read()
            if v is None:
                night.missed += 1
                f.write(json.dumps({"t": round(t, 1), "v": None}) + "\n")
            else:
                night.readings += 1
                night.last = v
                f.write(json.dumps({"t": round(t, 1), "v": v}) + "\n")
                m = int(t // 60)
                if minute is not None and m != minute and bucket:
                    mean = round(st.mean(bucket), 3)
                    if night.minutes and abs(mean - night.minutes[-1][1]) >= STEP_V:
                        night.steps.append((t, round(mean - night.minutes[-1][1], 2)))
                    night.minutes.append((t, mean))
                    bucket = []
                minute = m
                bucket.append(v)
            show(night)
            sleep(max(0.0, every - (clock() - t)))
    return night
