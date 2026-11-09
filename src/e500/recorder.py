"""Record the battery while driving (or parked): fast core values plus a slow full rotation.

Built for the owner's symptom: after ~30 minutes of hard highway driving the battery
sometimes keeps discharging while the car isn't moving. Each loop reads the core set
(current, charge, cell extremes, temperatures, pack voltage, contactors), then the next
few identifiers of a rotation through everything known on modules 44, 42 and 40 (a
full pass every few minutes); all 97 cells every fifth loop. Markers (the owner pressing Enter when it
happens) are written with the time. Read-only: UDS 0x22 only.

One JSON line per loop: {"t", "core": {module: {did: hex}}, "rot": {...}} or {"t", "mark": n}.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from . import battery
from .elm import AdapterError
from .uds import Client, module_id

CORE = {
    0x44: [0xA012, 0xA602, 0xA609, 0xA60A, 0xA60B, 0xA009, 0xA606, 0xA607, 0xA608],
    0x42: [0x2063, 0x205B, 0x2056, 0x0303, 0x204F, 0x0404, 0x20E2],
}
CELLS = list(range(0xA100, 0xA109))
CELLS_EVERY = 5  # loops: all 97 cells every few seconds, to catch a cell sagging under load
ROTATION_PER_LOOP = 4


@dataclass
class Live:
    """The latest decoded core values, for the status line."""
    t0: float = 0.0
    loops: int = 0
    marks: int = 0
    rot_done: int = 0
    rot_total: int = 0
    values: dict = field(default_factory=dict)
    errors: int = 0


def decode_core(core: dict) -> dict:
    """Readable values from one loop's core reads (whatever answered)."""
    b, e = core.get("44", {}), core.get("42", {})
    v = {}
    raw = lambda d, k: bytes.fromhex(d[k]) if d.get(k) else None
    if (x := raw(b, "a012")) is not None:
        v["current_a"] = battery._s16(x) / 20
    if (x := raw(b, "a602")) is not None:
        v["soc_true"] = x[0] * 0.4
        v["soc_dash"] = round(battery.dash_soc(v["soc_true"]), 1)
    for k, name in (("a609", "cell_max_v"), ("a60a", "cell_min_v"), ("a60b", "cell_avg_v")):
        if (x := raw(b, k)) is not None:
            v[name] = int.from_bytes(x[:2], "big") / 1000
    if (x := raw(b, "a009")) is not None and len(x) >= 2:
        v["cell_max_no"], v["cell_min_no"] = x[0], x[1]
    for k, name in (("a606", "temp_max_c"), ("a607", "temp_min_c"), ("a608", "temp_avg_c")):
        if (x := raw(b, k)) is not None:
            v[name] = x[0] - 50
    if (x := raw(e, "2063")) is not None:
        v["pack_v"] = int.from_bytes(x[:2], "big")
    if (x := raw(e, "2056")) is not None:
        v["contactors"] = x[0]
    if (x := raw(e, "204f")) is not None:
        # 1/16 km/h: ∫speed gave 13.4 km while both odometers moved 14 km (2026-11-08 drive)
        v["speed_kmh"] = int.from_bytes(x[:2], "big") / 16
        v["speed_mph"] = round(v["speed_kmh"] / 1.609344, 1)
    if (x := raw(e, "0404")) is not None:
        v["outside_f"] = x[0] - 51
    if "current_a" in v and "pack_v" in v:
        v["power_kw"] = round(v["current_a"] * v["pack_v"] / 1000, 1)
    if "cell_max_v" in v and "cell_min_v" in v:
        v["spread_mv"] = round(1000 * (v["cell_max_v"] - v["cell_min_v"]))
    if all(b.get(f"{d:04x}") for d in CELLS):
        cells = battery._u16s(b"".join(bytes.fromhex(b[f"{d:04x}"]) for d in CELLS))
        v["cells_mv"] = cells
    return v


def rotation(known: dict[int, list[int]]) -> list[tuple[int, int]]:
    """Every known identifier on the battery-related modules, minus what the core loop reads."""
    core = {(m, d) for m, ds in CORE.items() for d in ds} | {(0x44, d) for d in CELLS}
    out = []
    for m in (0x44, 0x42, 0x40):
        out += [(m, d) for d in known.get(m, []) if (m, d) not in core and (m, d) not in out]
    return out


def run(client: Client, out: Path, known: dict[int, list[int]],
        show: Callable[[Live], None] = lambda s: None, marks: Callable[[], int] = lambda: 0,
        max_loops: int | None = None) -> Live:
    """Record until KeyboardInterrupt (or max_loops). `marks()` returns how many times the
    owner pressed Enter since the last call."""
    rot = rotation(known)
    live = Live(t0=time.time(), rot_total=len(rot))
    k = 0
    with out.open("a", buffering=1) as f:
        f.write(json.dumps({"t": round(time.time(), 2), "start": True, "rotation": len(rot)}) + "\n")
        while max_loops is None or live.loops < max_loops:
            rec = {"t": round(time.time(), 2), "core": {}, "rot": {}}
            core = dict(CORE)
            if live.loops % CELLS_EVERY == 0:
                core[0x44] = core[0x44] + CELLS
            for m, dids in core.items():
                for did in dids:
                    try:
                        rec["core"].setdefault(f"{m:02x}", {})[f"{did:04x}"] = client.read_did(module_id(m), did).hex()
                    except AdapterError:
                        live.errors += 1
            for _ in range(min(ROTATION_PER_LOOP, len(rot))):
                m, did = rot[k % len(rot)]
                k += 1
                try:
                    rec["rot"].setdefault(f"{m:02x}", {})[f"{did:04x}"] = client.read_did(module_id(m), did).hex()
                except AdapterError:
                    live.errors += 1
            f.write(json.dumps(rec) + "\n")
            for _ in range(marks()):
                live.marks += 1
                f.write(json.dumps({"t": round(time.time(), 2), "mark": live.marks}) + "\n")
            live.loops += 1
            live.rot_done = k % len(rot) if rot else 0
            live.values = decode_core(rec["core"])
            show(live)
    return live
