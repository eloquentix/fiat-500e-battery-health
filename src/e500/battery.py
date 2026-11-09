"""What the app reads, and how raw bytes become battery numbers.

Two sources:
- Generic OBD PIDs (SAE J1979, public). The 500e doesn't answer them; kept for other cars.
- The battery controller's own data identifiers (UDS 0x22). These are NOT public. Each
  field in REAL_FIELDS was decoded from our own capture of the car, with the date and
  evidence in `source` (details in docs/PROTOCOL.md). Anything not decoded is left out
  and the app says so, rather than guessing.

Derived cell statistics (spread, lowest five, deviation from the mean) work on any
list of cell voltages, whatever field supplies it.
"""

from __future__ import annotations

from dataclasses import dataclass
from statistics import mean
from typing import Callable

from .uds import module_id


@dataclass(frozen=True)
class Field:
    name: str  # stable key in logs and the UI
    label: str
    unit: str
    ecu: int  # request CAN ID
    did: int | tuple[int, ...]  # several DIDs: their data is read in order and joined
    decode: Callable[[bytes], float | list[float]]
    source: str  # where the decoding came from: our capture, or "FAKE (mock)"

    @property
    def dids(self) -> tuple[int, ...]:
        return self.did if isinstance(self.did, tuple) else (self.did,)


def _u16s(d: bytes) -> list[int]:
    return [int.from_bytes(d[i:i + 2], "big") for i in range(0, len(d) - 1, 2)]


BMS = module_id(0x44)  # battery controller: 18DA44F1
EVCU = module_id(0x42)  # vehicle controller: mirrors the battery controller's figures


def _s16(d: bytes, i: int = 0) -> int:
    return int.from_bytes(d[i:i + 2], "big", signed=True)


def _u24(d: bytes) -> int:
    return int.from_bytes(d[:3], "big")

# Decoded from our captures of a 2013 500e on 2026-11-07 (docs/PROTOCOL.md, Findings).
REAL_FIELDS: list[Field] = [
    Field("cells", "Cell voltages", "V", BMS, tuple(range(0xA100, 0xA109)),
          lambda d: [v / 1000 for v in _u16s(d)],
          "own capture 2026-11-07: 97 u16 mV values; agree with A609-A60B to 1 mV"),
    Field("soc_true", "True charge", "%", BMS, 0xA602, lambda d: d[0] * 0.4,
          "own capture 2026-11-07: fits 5 dash readings within 0.4 %; 240 V energy check 6.5 kW"),
    Field("cell_max", "Highest cell (controller)", "V", BMS, 0xA609, lambda d: _u16s(d)[0] / 1000,
          "own capture 2026-11-07"),
    Field("cell_min", "Lowest cell (controller)", "V", BMS, 0xA60A, lambda d: _u16s(d)[0] / 1000,
          "own capture 2026-11-07"),
    Field("cell_avg", "Average cell (controller)", "V", BMS, 0xA60B, lambda d: _u16s(d)[0] / 1000,
          "own capture 2026-11-07"),
    Field("current", "Pack current", "A", BMS, 0xA012, lambda d: _s16(d) / 20,
          "own capture 2026-11-07: s16 / 20, negative = charging; -15.45 A x 393 V = 6.1 kW "
          "steady on the 6.6 kW charger, ~0.5 A at rest. Scale lead: OBDb gen-2 current (/20)"),
    Field("capacity_ah", "Capacity (controller's figure)", "Ah", BMS, 0xA625,
          lambda d: _u16s(d)[0] / 10,
          "own capture 2026-11-07: u16 / 10 = 37.9 Ah, same as module 42 DID 2050; the "
          "controller's charge moved 36.4-37.9 Ah per 100 % against the measured current"),
    Field("ah_in", "Charge in since controller fitted", "Ah", BMS, 0xA615, lambda d: _u24(d) / 10,
          "own capture 2026-11-07: u24 / 10; rose 7.7 Ah while the measured current gave 7.71 Ah"),
    Field("ah_out", "Charge out since controller fitted", "Ah", BMS, 0xA616, lambda d: _u24(d) / 10,
          "own capture 2026-11-07: u24 / 10, companion of A615; still while charging"),
    Field("pack_temps", "Battery module temperatures", "C", BMS, 0xA200,
          lambda d: [float(x - 40) for x in d],
          "own capture 2026-11-07: 18 x u8 - 40; min/max/mean equal module 42 DIDs 205E-2060. "
          "Layout lead: OBDb gen-2 A200"),
]

# Not measured: the new-pack rating. Published cell ratings are 60 and 63 Ah; the
# advertised 24 kWh over 97 cells at 3.7 V is 67 Ah. The range covers all three.
NOMINAL_AH = (60.0, 67.0)


def soh_range(capacity_ah: float) -> tuple[float, float]:
    """Capacity as a share of new, over the range of new-pack ratings."""
    return (100 * capacity_ah / NOMINAL_AH[1], 100 * capacity_ah / NOMINAL_AH[0])


def soh_about(capacity_ah: float) -> int:
    """One honest number: against the middle of the range, to the nearest 5 %."""
    return int(5 * round(100 * capacity_ah / (sum(NOMINAL_AH) / 2) / 5))

# The dash shows a window of the true charge (fitted 2026-11-07 on 4 dash readings,
# confirmed on a 5th): dash % = 1.1195 x true % - 12.26. Dash 0 % is ~11 % true.
DASH_A, DASH_B = 0.4478 / 0.4, -12.26


def dash_soc(true_pct: float) -> float:
    """The charge % the dashboard would show for a true charge."""
    return max(0.0, min(100.0, DASH_A * true_pct + DASH_B))


@dataclass(frozen=True)
class Pid:
    pid: int
    name: str
    label: str
    unit: str
    decode: Callable[[bytes], float]


# Standard J1979 PIDs worth reading on an EV (other cars; the 500e doesn't answer).
OBD_PIDS = {p.pid: p for p in [
    Pid(0x0D, "speed", "Road speed", "km/h", lambda d: d[0]),
    Pid(0x42, "aux_v", "12 V system", "V", lambda d: ((d[0] << 8) | d[1]) / 1000),
    Pid(0x46, "ambient", "Outside air", "C", lambda d: d[0] - 40),
    Pid(0x5B, "obd_battery_pct", "Battery remaining (OBD)", "%", lambda d: d[0] * 100 / 255),
]}


def cell_stats(cells: list[float]) -> dict:
    """Spread and lowest cells from per-cell voltages (V). Numbers in mV where small."""
    if not cells:
        return {}
    avg = mean(cells)
    order = sorted(range(len(cells)), key=lambda i: cells[i])
    return {
        "count": len(cells),
        "min_v": round(cells[order[0]], 4), "min_cell": order[0] + 1,
        "max_v": round(cells[order[-1]], 4), "max_cell": order[-1] + 1,
        "mean_v": round(avg, 4),
        "spread_mv": round((cells[order[-1]] - cells[order[0]]) * 1000, 1),
        "lowest5": [{"cell": i + 1, "v": round(cells[i], 4)} for i in order[:5]],
        "deviation_mv": [round((v - avg) * 1000, 1) for v in cells],
        "pack_v": round(sum(cells), 2),
    }


def verdict(snap: dict) -> str:
    """Plain sentences, the most important fact first. Says what the numbers are,
    not more: a voltage spread at rest doesn't establish health or capacity."""
    soh, stats, soc = snap.get("soh"), snap.get("cell_stats") or {}, snap.get("soc")
    cap = snap.get("capacity_ah")
    parts = []
    if soh is not None:
        parts.append(f"Battery health {soh:.0f}%.")
    elif cap is not None:
        parts.append(f"The battery holds {cap:.1f} Ah, about {soh_about(cap)}% of new.")
    if soh is None and soc is not None:
        parts.append(f"Charge {soc:.0f}%.")
    if stats:
        parts.append(f"All {stats['count']} cells within {stats['spread_mv']:.0f} mV "
                     f"({stats['min_v']:.3f}–{stats['max_v']:.3f} V).")
    if not parts:
        return ("No battery data decoded for this car yet. Run discovery to find the "
                "battery controller's data.")
    if soh is None and cap is None:
        parts.append("Health (capacity) isn't decoded yet; a small spread at rest doesn't measure it.")
    return " ".join(parts)
