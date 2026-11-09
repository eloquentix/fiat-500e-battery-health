"""Fault codes over UDS 0x19 ReadDTCInformation. Read-only; nothing here clears codes.

Sub-functions used (ISO 14229-1):
- 0x02 reportDTCByStatusMask, mask 0xFF: every stored code with its status byte.
- 0x04 reportDTCSnapshotRecordByDTCNumber: the conditions when a code was set.
- 0x06 reportDTCExtDataRecordByDTCNumber: counters (occurrences, aging, odometer).
The snapshot and extended-data layouts are manufacturer-specific: they're kept raw
until our captures decode them.

Code text: the 2-bit system letter and four hex digits are SAE J2012 notation; the
third byte is the failure type. Descriptions are given only for a few public SAE codes;
everything else shows as the code alone.
"""

from __future__ import annotations

from .elm import AdapterError, NoData
from .uds import Client, NegativeResponse

STATUS_BITS = ["test failed", "failed this cycle", "pending", "confirmed",
               "not tested since clear", "failed since clear", "not tested this cycle",
               "warning lamp on"]

# A code is a fault if any of these status bits is set: test failed (now or this cycle),
# pending, confirmed, failed since clear, warning lamp. Codes with only "not tested"
# bits are monitors the module supports that haven't run yet: listed, not faults.
FAULT_BITS = 0xAF

# Public SAE J2012 meanings for codes an EV battery report cares about.
SAE = {
    "U0001": "High-speed CAN communication bus",
    "U0121": "Lost communication with the ABS module",
    "U0140": "Lost communication with the body control module",
    "U0415": "Invalid data received from the ABS module",
    "P0A7F": "Hybrid/EV battery pack deterioration",
    "P0A80": "Replace hybrid/EV battery pack",
    "P0AA1": "Hybrid/EV battery positive contactor stuck closed",
    "P0AA4": "Hybrid/EV battery negative contactor stuck closed",
    "P0AA6": "Hybrid/EV battery voltage system isolation fault",
}


def code(b: bytes) -> str:
    """3 DTC bytes -> 'P0AA1-00'."""
    letter = "PCBU"[b[0] >> 6]
    return f"{letter}{(b[0] >> 4) & 3}{b[0] & 0xF:X}{b[1]:02X}-{b[2]:02X}"


def status_text(s: int) -> list[str]:
    return [name for bit, name in enumerate(STATUS_BITS) if s >> bit & 1]


def parse_list(reply: bytes) -> list[dict]:
    """A 59 02 reply (after the SID and sub-function): availability mask, then 4-byte records."""
    body = reply[1:]  # skip the availability mask
    out = []
    for k in range(0, len(body) - 3, 4):
        raw, st = body[k:k + 3], body[k + 3]
        c = code(raw)
        out.append({"code": c, "raw": raw.hex(), "status": st, "status_text": status_text(st),
                    "meaning": SAE.get(c[:5])})
    return out


def read(client: Client, tx: int) -> dict:
    """All codes at one module, with snapshot and extended data where the module has them."""
    rec: dict = {"ecu": f"{tx >> 8 & 0xFF:02x}"}
    try:
        reply = client.request(tx, bytes([0x19, 0x02, 0xFF]))
    except NegativeResponse as e:
        rec["nrc"] = f"{e.code:02x}"
        return rec
    except (NoData, AdapterError) as e:
        rec["error"] = str(e)
        return rec
    rec["hex"] = reply[2:].hex()
    rec["codes"] = parse_list(reply[2:])
    for c in rec["codes"]:
        dtc = bytes.fromhex(c["raw"])
        for sub, key in ((0x04, "snapshot"), (0x06, "extended")):
            try:
                c[key] = client.request(tx, bytes([0x19, sub]) + dtc + b"\xFF")[2:].hex()
            except NegativeResponse as e:
                c[key + "_nrc"] = f"{e.code:02x}"
            except (NoData, AdapterError) as e:
                c[key + "_error"] = str(e)
    return rec


def is_fault(status: int) -> bool:
    return bool(status & FAULT_BITS)


def clock_at_fault(snapshot_hex: str | None) -> int | None:
    """Module clock (DID 1008, minutes of running time) when the code was set, if the
    snapshot starts with it: DTC(3) status(1) record(1) count(1) 1008 value(4)."""
    if not snapshot_hex or len(snapshot_hex) < 20 or snapshot_hex[12:16] != "1008":
        return None
    return int(snapshot_hex[16:24], 16)


def summarise(records: list[dict]) -> list[dict]:
    """Per module: faults (one line per code, with how many records), monitors, errors."""
    out = []
    for r in records:
        codes = r.get("codes")
        row = {"ecu": r["ecu"], "answered": codes is not None,
               "why": r.get("nrc") and f"rejected (0x{r['nrc']})" or r.get("error"),
               "faults": [], "monitored": 0}
        merged: dict[str, dict] = {}
        for c in codes or []:
            if not is_fault(c["status"]):
                row["monitored"] += 1
                continue
            m = merged.setdefault(c["code"], {"code": c["code"], "records": 0, "status": 0,
                                              "meaning": c.get("meaning"), "clock": None})
            m["records"] += 1
            m["status"] |= c["status"]
            m["clock"] = m["clock"] or clock_at_fault(c.get("snapshot"))
        for m in merged.values():
            m["status_text"] = status_text(m["status"])
            m["active"] = bool(m["status"] & 0x03)
        row["faults"] = sorted(merged.values(), key=lambda m: (not m["active"], m["code"]))
        out.append(row)
    return out
