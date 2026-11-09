"""One run at the car that records everything the report can use. Read-only.

In order of value, so stopping early still leaves the most useful part:
1. Find the modules (TesterPresent on the 29-bit range).
2. Fault codes from every module (UDS 0x19, read-only; never cleared).
3. Identification (F180-F19F) and counters (odometer, clocks) on every module, then
   the data identifiers that answered in earlier sweeps.
4. `deep`: every identifier in the pages where FCA keeps data, on every module.

Records go to logs/<time>-survey-result.jsonl, one JSON line each ({"ecu", "did",
"hex" | "nrc" | "error"}, or {"faults": ...}), written as they arrive. Stopping is
safe: the next run finds the unfinished file and continues it, skipping what's done.
"""

from __future__ import annotations

import datetime as dt
import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from . import dtc, modules as mods
from .elm import AdapterError, NoData
from .uds import Client, NegativeResponse, module_id

KNOWN = [0x1F, 0x28, 0x30, 0x40, 0x42, 0x43, 0x44, 0x45, 0x47, 0x48, 0x60]  # answered 2026-11-07
PAGES = [0x01, 0x02, 0x03, 0x04, 0x10, 0x20, 0xA0, 0xA6]  # where the 500e's modules keep data
COUNTERS = [0x1008, 0x1009, 0x2001, 0x2002, 0x2003, 0x2008, 0x2009, 0x200A, 0x200B, 0x200C]
# ^ odometer (2001, 0.1 km) and the clocks next to it (1008 minutes, 2008 key-on, 200B last fault)
SECONDS_PER_READ = 0.05  # measured on the car: a rejected identifier costs ~48 ms
SECONDS_PER_PING = 0.1   # module discovery with a 100 ms reply wait
RESUME_HOURS = 24


@dataclass
class Progress:
    """What the survey reports while it runs; the CLI decides how to show it."""
    stage: str = ""
    module: int | None = None
    module_index: int = 0
    module_count: int = 0
    done: int = 0
    total: int = 0
    answered: int = 0
    reads_left: int = 0  # over the whole survey, for the time estimate
    notes: list[str] = field(default_factory=list)


def known_dids(log_dir: Path) -> dict[int, list[int]]:
    """DIDs that answered in earlier sweeps, per module byte."""
    out: dict[int, set[int]] = {}
    for path in log_dir.glob("*.jsonl"):
        if not any(k in path.name for k in ("-full-", "-pages-", "-survey")):
            continue
        for line in path.read_text().splitlines():
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if isinstance(r, dict) and "hex" in r and "ecu" in r and "did" in r:
                ecu = int(r["ecu"], 16)
                out.setdefault(ecu >> 8 & 0xFF if ecu > 0xFF else ecu, set()).add(int(r["did"], 16))
    return {k: sorted(v) for k, v in out.items()}


def unfinished(log_dir: Path) -> Path | None:
    """The latest survey that stopped before the end, if it's recent enough to continue."""
    files = sorted(log_dir.glob("*-survey-result.jsonl"))
    if not files:
        return None
    last = files[-1]
    if '"complete"' in last.read_text()[-300:]:
        return None
    return last if (time.time() - last.stat().st_mtime) / 3600 < RESUME_HOURS else None


def state(path: Path) -> tuple[list[int] | None, set[tuple[int, int]], set[int]]:
    """From a survey file: its module list, the (module, DID) pairs read, and the
    modules whose fault codes were read. Errors don't count as read: they get another try."""
    modules, read, faults = None, set(), set()
    for line in path.read_text().splitlines():
        try:
            r = json.loads(line)
        except ValueError:
            continue
        if "modules" in r:
            modules = [int(m, 16) for m in r["modules"]]
        elif "faults" in r:
            faults.add(int(r["faults"]["ecu"], 16))
        elif "ecu" in r and "did" in r and "error" not in r:
            read.add((int(r["ecu"], 16), int(r["did"], 16)))
    return modules, read, faults


def plan(addrs: list[int], known: dict[int, list[int]], deep: bool,
         read=frozenset()) -> dict[int, list[int]]:
    """Identifiers to read per module, most useful first, minus what's already read."""
    out = {}
    for addr in addrs:
        dids = list(mods.IDENT_DIDS) + COUNTERS + known.get(addr, [])
        if deep:
            dids += [p << 8 | k for p in PAGES for k in range(0x100)]
        seen, todo = set(), []
        for d in dids:
            if d not in seen and (addr, d) not in read:
                seen.add(d)
                todo.append(d)
        out[addr] = todo
    return out


def estimate_s(deep: bool, modules: int = len(KNOWN), known: int = 450) -> float:
    """Rough duration of a fresh survey, for the plan shown before it starts."""
    per_module = len(mods.IDENT_DIDS) + len(COUNTERS) + (len(PAGES) * 256 if deep else 0)
    return 256 * SECONDS_PER_PING + (modules * (per_module + 3) + known) * SECONDS_PER_READ


def _read(client: Client, tx: int, did: int) -> dict:
    rec = {"ecu": f"{tx >> 8 & 0xFF:02x}", "did": f"{did:04x}"}
    try:
        rec["hex"] = client.read_did(tx, did).hex()
    except NegativeResponse as e:
        rec["nrc"] = f"{e.code:02x}"
    except NoData:
        rec["error"] = "no data"
    except AdapterError as e:
        rec["error"] = str(e)
    return rec


def run(client: Client, out: Path, log_dir: Path, deep: bool = False,
        show: Callable[[Progress], None] = lambda p: None, resume: Path | None = None) -> dict:
    """The survey. Writes to `out`, or continues `resume`. A KeyboardInterrupt leaves a
    valid file that the next run continues. Returns a summary."""
    p = Progress()
    modules, read, faults_done = state(resume) if resume else (None, set(), set())
    path = resume or out
    with path.open("a", buffering=1) as f:
        if modules is None:
            p.stage, p.total = "Finding modules", 256
            client.elm.cmd("ATST19")  # 100 ms: a silent address costs 0.1 s, not 1 s
            found = []
            for t in range(0x100):
                try:
                    if client.ping(module_id(t)) is not None:
                        found.append(t)
                except AdapterError:
                    pass
                p.done = t + 1
                if p.done % 4 == 0:
                    show(p)
            modules = found
            f.write(json.dumps({"modules": [f"{a:02x}" for a in modules], "deep": deep,
                                "started": dt.datetime.now().isoformat(timespec="seconds")}) + "\n")
        client.elm.cmd("ATST32")  # 200 ms for reads: module 1F is slow between frames
        missing = sorted(set(KNOWN) - set(modules))
        p.notes.append(f"{len(modules)} modules answer: {' '.join(f'{a:02X}' for a in modules)}"
                       + (f"; not answering: {' '.join(f'{a:02X}' for a in missing)}" if missing else ""))

        todo = plan(modules, known_dids(log_dir), deep, read)
        fault_todo = [a for a in modules if a not in faults_done]
        p.reads_left = sum(map(len, todo.values())) + 3 * len(fault_todo)

        p.stage, p.module_count, p.total, p.done, p.answered = "Fault codes", len(fault_todo), len(fault_todo), 0, 0
        for k, addr in enumerate(fault_todo):
            p.module, p.module_index = addr, k + 1
            show(p)
            rec = dtc.read(client, module_id(addr))
            f.write(json.dumps({"faults": rec}) + "\n")
            p.answered += sum(dtc.is_fault(c["status"]) for c in rec.get("codes", []))
            p.done, p.reads_left = k + 1, p.reads_left - 3
            show(p)

        gaps = False
        p.stage, p.module_count = "Reading", len(modules)
        for k, addr in enumerate(modules):
            dids = todo[addr]
            p.module, p.module_index, p.total, p.done, p.answered = addr, k + 1, len(dids), 0, 0
            show(p)
            tx, misses = module_id(addr), 0
            for did in dids:
                rec = _read(client, tx, did)
                f.write(json.dumps(rec) + "\n")
                p.done, p.reads_left = p.done + 1, p.reads_left - 1
                if "hex" in rec:
                    p.answered += 1
                misses = misses + 1 if "error" in rec else 0
                if p.done % 8 == 0 or p.done == p.total:
                    show(p)
                if misses >= 20:
                    p.notes.append(f"module {addr:02X} stopped answering at {did:04X}; the rest is left for the next run")
                    p.reads_left -= p.total - p.done
                    gaps = True
                    break
        if not gaps:  # with gaps, the next run picks up what didn't answer
            f.write(json.dumps({"complete": True, "ended": dt.datetime.now().isoformat(timespec="seconds")}) + "\n")
    return {"modules": modules, "missing": missing, "path": path, **totals(path)}


def totals(path: Path) -> dict:
    """Identifiers answered and real faults in a whole survey file (all its runs)."""
    answered, faults = 0, 0
    for line in path.read_text().splitlines():
        try:
            r = json.loads(line)
        except ValueError:
            continue
        answered += "hex" in r and "did" in r
        faults += sum(dtc.is_fault(c["status"]) for c in r.get("faults", {}).get("codes", []))
    return {"answered": answered, "faults": faults}
