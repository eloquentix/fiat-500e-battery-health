"""What a logged charging session says about the battery.

Input: the records `scripts/charge_log.py` writes (one pass over the battery modules'
data identifiers per line: {"t", "v12", "44": {did: hex}, "42": {...}}). Everything is
computed from the car's own figures with the decoders in battery.py.

- Capacity: Ah moved per 1 % of the controller's charge, over the steady part of the
  charge. This is the capacity the controller works with; it agrees with its stored
  figure (A625) when that figure is right.
- Charger: power into the pack while charging steadily.
- Resistance: near full the charger pulses on and off; consecutive readings with and
  without current at the same charge give dV/dI per cell. The readings aren't
  simultaneous, so a median over many pulses is used, never one pair.
- Cells: each cell's average offset from the pack mean, at rest and while charging. A
  cell low in both is a balance offset; one that drops only under load is weak.
"""

from __future__ import annotations

import json
import statistics as st
from pathlib import Path

from . import battery

STEADY = 0.9  # share of the peak charging current that counts as steady charging


def _cells(b: dict) -> list[int] | None:
    out = []
    for did in range(0xA100, 0xA109):
        h = b.get(f"{did:x}")
        if not h:
            return None
        out += [int(h[i:i + 4], 16) for i in range(0, len(h), 4)]
    return out if len(out) == 97 else None


def points(rows: list[dict]) -> list[dict]:
    """One point per pass with every value we can decode; passes missing any are dropped."""
    pts = []
    for r in rows:
        b, e = r.get("44") or {}, r.get("42") or {}
        cells = _cells(b)
        if not cells or not b.get("a012") or not b.get("a602"):
            continue
        p = {"t": r["t"], "i": battery._s16(bytes.fromhex(b["a012"])) / 20,
             "soc": int(b["a602"], 16) * 0.4, "cells": cells, "pack_v": sum(cells) / 1000}
        if b.get("a615") and b.get("a616"):
            p["ah_in"], p["ah_out"] = int(b["a615"], 16) / 10, int(b["a616"], 16) / 10
        if b.get("a200"):
            p["temps"] = [x - 40 for x in bytes.fromhex(b["a200"])]
        if b.get("a625"):
            p["capacity_ah"] = int(b["a625"], 16) / 10
        if e.get("2050"):
            p["capacity_ah_evcu"] = int(e["2050"], 16) / 10
        if e.get("2051"):
            p["remaining_ah"] = int(e["2051"], 16) / 10
        pts.append(p)
    return pts


def load(path: Path) -> list[dict]:
    return points([json.loads(line) for line in path.read_text().splitlines() if line.strip()])


def _integrate(pts: list[dict]) -> tuple[float, float]:
    """Ah and kWh into the pack (charging positive), trapezoid rule."""
    ah = kwh = 0.0
    for a, b in zip(pts, pts[1:]):
        h = (b["t"] - a["t"]) / 3600
        ah += -(a["i"] + b["i"]) / 2 * h
        kwh += -(a["i"] * a["pack_v"] + b["i"] * b["pack_v"]) / 2 * h / 1000
    return ah, kwh


def steady_window(pts: list[dict]) -> tuple[int, int] | None:
    """The longest run of passes charging at >= 90 % of the peak current."""
    peak = -min((p["i"] for p in pts), default=0)
    if peak < 2:
        return None
    best, start = None, None
    for k, p in enumerate(pts + [{"i": 0}]):
        if -p["i"] >= STEADY * peak:
            start = k if start is None else start
        elif start is not None:
            if best is None or k - 1 - start > best[1] - best[0]:
                best = (start, k - 1)
            start = None
    return best if best and best[1] > best[0] else None


def resistance(pts: list[dict]) -> dict | None:
    """Per-cell and pack resistance from on/off charging pulses (median of many)."""
    per_pulse = []
    for a, b in zip(pts, pts[1:]):
        if abs(a["soc"] - b["soc"]) > 0.4 or abs(a["i"] - b["i"]) < 10:
            continue
        on, off = (a, b) if a["i"] < b["i"] else (b, a)
        di = off["i"] - on["i"]
        per_pulse.append([(x - y) / di for x, y in zip(on["cells"], off["cells"])])  # mV/A = mOhm
    if len(per_pulse) < 5:
        return None
    cells = [st.median(col) for col in zip(*per_pulse)]
    return {"pulses": len(per_pulse), "cells_mohm": [round(x, 3) for x in cells],
            "median_mohm": round(st.median(cells), 3), "sd_mohm": round(st.pstdev(cells), 3),
            "pack_mohm": round(sum(cells), 1)}


def offsets(group: list[dict]) -> list[float] | None:
    if not group:
        return None
    devs = [[x - st.mean(p["cells"]) for x in p["cells"]] for p in group]
    return [round(st.mean(col), 2) for col in zip(*devs)]


def session(pts: list[dict]) -> dict:
    """Everything the report needs from one charging session."""
    if len(pts) < 10:
        return {"usable": False, "why": f"only {len(pts)} complete readings"}
    t0 = pts[0]["t"]
    out = {"usable": True, "start": t0, "end": pts[-1]["t"], "readings": len(pts),
           "minutes": round((pts[-1]["t"] - t0) / 60, 1),
           "soc_start": round(pts[0]["soc"], 1), "soc_end": round(pts[-1]["soc"], 1)}
    ah, kwh = _integrate(pts)
    out["ah_measured"], out["kwh_measured"] = round(ah, 2), round(kwh, 2)
    if "ah_in" in pts[0] and "ah_in" in pts[-1]:
        out["ah_counted"] = round((pts[-1]["ah_in"] - pts[0]["ah_in"])
                                  - (pts[-1]["ah_out"] - pts[0]["ah_out"]), 1)
    win = steady_window(pts)
    if win:
        a, b = win
        seg = pts[a:b + 1]
        s_ah, s_kwh = _integrate(seg)
        dsoc = seg[-1]["soc"] - seg[0]["soc"]
        out["steady"] = {
            "from_min": round((seg[0]["t"] - t0) / 60, 1), "to_min": round((seg[-1]["t"] - t0) / 60, 1),
            "current_a": round(-st.median(p["i"] for p in seg), 2),
            "power_kw": round(st.median(-p["i"] * p["pack_v"] / 1000 for p in seg), 2),
            "ah": round(s_ah, 2), "kwh": round(s_kwh, 2), "dsoc": round(dsoc, 1)}
        if dsoc >= 5:
            out["steady"]["ah_per_100"] = round(s_ah / dsoc * 100, 1)
            out["steady"]["kwh_per_100"] = round(s_kwh / dsoc * 100, 2)
            if "ah_in" in seg[0]:
                counted = seg[-1]["ah_in"] - seg[0]["ah_in"]
                out["steady"]["counter_agrees_pct"] = round(100 * (counted - s_ah) / s_ah, 1)
    caps = {p.get("capacity_ah") for p in pts} - {None}
    out["capacity_ah"] = max(caps) if caps else None
    out["capacity_ah_evcu"] = pts[-1].get("capacity_ah_evcu")
    out["resistance"] = resistance(pts)
    rest = [p for p in pts if abs(p["i"]) < 1]
    load = [p for p in pts if win and -p["i"] >= STEADY * -min(q["i"] for q in pts)]
    out["offset_rest_mv"], out["offset_load_mv"] = offsets(rest), offsets(load)
    out["rest_readings"], out["load_readings"] = len(rest), len(load)
    last_rest = rest[-1] if rest else pts[-1]
    out["cells_at_rest"] = [c / 1000 for c in last_rest["cells"]]
    out["cells_at_rest_t"] = last_rest["t"]
    temps = [x for p in pts for x in p.get("temps", [])]
    if temps:
        out["temp_min"], out["temp_max"] = min(temps), max(temps)
        out["temp_spread_max"] = max(max(p["temps"]) - min(p["temps"]) for p in pts if p.get("temps"))
    step = max(1, len(pts) // 120)
    out["series"] = [{"min": round((p["t"] - t0) / 60, 2), "soc": p["soc"], "i": p["i"],
                      "v": round(p["pack_v"], 2), "spread": max(p["cells"]) - min(p["cells"]),
                      "tmax": max(p["temps"]) if p.get("temps") else None}
                     for p in pts[::step]]
    if "ah_in" in pts[-1]:
        out["ah_in_life"], out["ah_out_life"] = pts[-1]["ah_in"], pts[-1]["ah_out"]
    return out
