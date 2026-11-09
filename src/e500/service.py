"""One car, one adapter: snapshots of the battery, logged, plus a long-term history.

A snapshot reads the generic OBD PIDs the car supports and every decoded battery
field, then derives cell statistics. Every snapshot goes to the session log; one
summary per day goes to the history file, which is what the degradation curve is
drawn from.
"""

from __future__ import annotations

import datetime as dt
import json
import threading
import time
from pathlib import Path
from typing import TextIO

from . import battery
from .battery import Field
from .elm import AdapterError, Elm327
from .uds import Client


class Car:
    def __init__(self, elm: Elm327, fields: list[Field], mock: bool = False,
                 log: TextIO | None = None, history: Path | None = None,
                 seed_history: list[dict] | None = None, car: dict | None = None):
        self.elm, self.client = elm, Client(elm)
        self.fields, self.mock, self.log, self.history_path = fields, mock, log, history
        self.seed_history = seed_history or []
        self.car = car or {}  # name, year, colour: from data/car.json, kept private
        self.lock = threading.Lock()
        self.adapter = None
        self.supported: list[int] | None = None
        self.latest: dict = {}
        self.seq = 0  # snapshots taken; lets browser tabs share one instead of queueing

    def connect(self) -> None:
        with self.lock:
            if self.adapter is None:
                self.adapter = self.elm.init()

    def _supported_pids(self) -> list[int]:
        """Walk the PID 00/20/40 bitmaps once per session. Only on 11-bit CAN: the 500e
        (29-bit) doesn't answer generic OBD, and 7DF isn't a 29-bit address."""
        if self.supported is None and self.elm.protocol != "6":
            self.supported = []
        if self.supported is None:
            found, base = set(), 0x00
            while base <= 0x40:
                try:
                    replies = self.client.obd(base)
                except AdapterError:
                    break
                more = False
                for data in replies.values():
                    bits = int.from_bytes(data[:4], "big")
                    found |= {base + i + 1 for i in range(32) if bits >> (31 - i) & 1}
                    more |= bool(bits & 1)
                if not more:
                    break
                base += 0x20
            self.supported = sorted(p for p in found if p in battery.OBD_PIDS)
        return self.supported

    def snapshot(self) -> dict:
        """Everything readable right now. Fields that fail are left out, not fatal."""
        seen = self.seq
        self.connect()
        with self.lock:
            if self.seq != seen and self.latest:
                return self.latest  # another tab took one while we waited
            snap: dict = {"t": round(time.time(), 3), "errors": {}}
            for pid in self._supported_pids():
                p = battery.OBD_PIDS[pid]
                try:
                    data = next(iter(self.client.obd(pid).values()))
                    snap[p.name] = round(p.decode(data), 2)
                except (AdapterError, StopIteration, IndexError) as e:
                    snap["errors"][p.name] = str(e) or "no reply"
            snap["read_at"] = {}  # when each field was actually read: a pass takes a while
            for f in self.fields:
                try:
                    raw = b"".join(self.client.read_did(f.ecu, did) for did in f.dids)
                    v = f.decode(raw)
                    # 4 places keeps millivolts; 2 would turn 3.994 V into 3.99
                    snap[f.name] = [round(x, 4) for x in v] if isinstance(v, list) else round(v, 4)
                    snap["read_at"][f.name] = round(time.time(), 3)
                except (AdapterError, IndexError) as e:
                    snap["errors"][f.name] = str(e)
            snap["aux_v_adapter"] = self.elm.battery_volts()
            if snap.get("soc_true") is not None:
                snap["soc"] = round(battery.dash_soc(snap["soc_true"]), 1)
            if snap.get("cells"):
                snap["cell_stats"] = battery.cell_stats(snap["cells"])
                snap["pack_v"] = snap["cell_stats"]["pack_v"]
            snap["verdict"] = battery.verdict(snap)
            if self.log:
                self.log.write(json.dumps(snap) + "\n")
            self._remember(snap)
            self.latest, self.seq = snap, self.seq + 1
            return snap

    # -- long-term history ---------------------------------------------------------
    def _remember(self, snap: dict) -> None:
        """Keep one summary per day (the latest), only for readings worth trending."""
        if not self.history_path or (snap.get("soh") is None and not snap.get("cell_stats")):
            return
        stats = snap.get("cell_stats") or {}
        # Context travels with the numbers: a spread at 99 % on the charger isn't
        # comparable with one at 40 % after a drive.
        entry = {"date": dt.date.today().isoformat(), "soh": snap.get("soh"),
                 "capacity_ah": snap.get("capacity_ah"), "ah_in": snap.get("ah_in"),
                 "soc": snap.get("soc"), "soc_true": snap.get("soc_true"),
                 "spread_mv": stats.get("spread_mv"), "min_v": stats.get("min_v"),
                 "min_cell": stats.get("min_cell"), "mean_v": stats.get("mean_v"),
                 "aux_v": snap.get("aux_v_adapter"),
                 "decoders": sorted(f.name for f in self.fields)}
        rows = [r for r in self._read_history() if r["date"] != entry["date"]]
        self.history_path.parent.mkdir(parents=True, exist_ok=True)
        self.history_path.write_text("".join(json.dumps(r) + "\n" for r in rows + [entry]))

    def _read_history(self) -> list[dict]:
        if not self.history_path or not self.history_path.exists():
            return []
        return [json.loads(line) for line in self.history_path.read_text().splitlines() if line]

    def history(self) -> list[dict]:
        return sorted(self.seed_history + self._read_history(), key=lambda r: r["date"])

    # -- for the report (read once per session, on request) -------------------------
    def identity(self, refresh: bool = False) -> dict[int, dict]:
        """Every known module's identification and counters (F180-F19F, 1008, 2001...)."""
        from . import modules as mods, survey
        from .uds import module_id
        if getattr(self, "_identity", None) is not None and not refresh:
            return self._identity
        self.connect()
        out: dict[int, dict] = {}
        with self.lock:
            for addr in survey.KNOWN:
                tx, raw = module_id(addr), {}
                for did in mods.IDENT_DIDS + survey.COUNTERS:
                    try:
                        raw[f"{did:04x}"] = self.client.read_did(tx, did).hex()
                    except AdapterError:
                        if did == mods.IDENT_DIDS[0] and not raw:
                            break  # silent or absent module: don't wait 32 times
                if raw:
                    ident = mods.parse_ident({k: v for k, v in raw.items() if k.startswith("f1")})
                    ident["counters"] = {k: v for k, v in raw.items() if not k.startswith("f1")}
                    out[addr] = ident
        self._identity = out
        return out

    def faults(self, refresh: bool = False) -> list[dict]:
        """Fault codes from every known module (read-only 0x19, never cleared)."""
        from . import dtc, survey
        from .uds import module_id
        if getattr(self, "_faults", None) is not None and not refresh:
            return self._faults
        self.connect()
        with self.lock:
            self._faults = [dtc.read(self.client, module_id(a)) for a in survey.KNOWN]
        return self._faults

    def report(self, session: dict | None, recalls: list[dict] | None, refresh: bool = False,
               sources=()) -> str:
        """The battery and history report with what the car says right now."""
        from . import report
        live = self.snapshot()
        return report.build(car=self.car, session=session, modules=self.identity(refresh),
                            recalls=recalls, faults=self.faults(refresh), live=live,
                            simulated=self.mock, toolbar=True, sources=sources)

    def status(self) -> dict:
        return {"mode": "mock" if self.mock else "hardware", "car": self.car,
                "port": getattr(self.elm.link, "port", None), "adapter": self.adapter,
                "fields": [{"name": f.name, "label": f.label, "unit": f.unit, "source": f.source,
                            "dids": [f"{d:04X}" for d in f.dids], "ecu": f"{f.ecu:X}"}
                           for f in self.fields],
                "units": {p.name: p.unit for p in battery.OBD_PIDS.values()},
                "labels": {p.name: p.label for p in battery.OBD_PIDS.values()}}
