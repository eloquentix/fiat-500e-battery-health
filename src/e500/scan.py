"""Discovery: which modules answer, and which data identifiers they have.

Read-only: TesterPresent (0x3E) to find modules, ReadDataByIdentifier (0x22) to
sweep identifiers. Every answer is logged, so one run on the car is enough to
work from at the desk.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Callable

from .elm import AdapterError, NoData
from .uds import Client, NegativeResponse, module_id

MODULES_29 = [module_id(t) for t in range(0x100)]  # 18DA00F1 ... 18DAFFF1
MODULES_11 = range(0x700, 0x7F8)

PRESETS = {
    "ident": (0xF180, 0xF1FF),  # standard UDS identification block (VIN, part numbers)
    "wide": (0x0000, 0xFFFF),   # everything; about an hour, resumable
}


def find_modules(client: Client, ids=MODULES_29,
                 progress: Callable[[int], None] | None = None) -> dict[int, int]:
    """{request id: reply id} for every module that answers a TesterPresent."""
    found = {}
    for tx in ids:
        if progress:
            progress(tx)
        try:
            rx = client.ping(tx)
        except AdapterError:
            continue
        if rx is not None:
            found[tx] = rx
    return found


def done_dids(log: Path) -> set[int]:
    """DIDs already recorded in a previous (possibly interrupted) sweep."""
    if not log.exists():
        return set()
    return {int(json.loads(line)["did"], 16) for line in log.read_text().splitlines() if line}


def sweep_dids(client: Client, ecu: int, start: int, end: int, log: Path,
               progress: Callable[[int, dict], None] | None = None,
               max_misses: int = 20) -> int:
    """Read every DID in [start, end] at `ecu`, appending one JSON line each to `log`.
    Skips DIDs the log already has. Stops after `max_misses` silent requests in a
    row (the module went to sleep, or the car turned off). Returns how many answered."""
    skip, answered, misses = done_dids(log), 0, 0
    with log.open("a", buffering=1) as f:
        for did in range(start, end + 1):
            if did in skip:
                continue
            rec = {"ecu": f"{ecu:x}", "did": f"{did:04x}"}
            try:
                rec["hex"] = client.read_did(ecu, did).hex()
                answered += 1
                misses = 0
            except NegativeResponse as e:
                rec["nrc"] = f"{e.code:02x}"
                misses = 0
            except NoData:
                rec["error"] = "no data"
                misses += 1
            except AdapterError as e:
                rec["error"] = str(e)
                misses += 1
            f.write(json.dumps(rec) + "\n")
            if progress:
                progress(did, rec)
            if misses >= max_misses:
                raise AdapterError(f"{max_misses} requests in a row went unanswered at "
                                   f"DID {did:04x}; rerun to resume from there")
    return answered
