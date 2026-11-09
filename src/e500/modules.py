"""The car's modules: who they are, and what their identification says about their history.

Identification uses the standard UDS identifiers (ISO 14229 F180-F19F, read with 0x22).
What FCA puts in them, as seen on our 2013 500e (2026-11-07):
- F187 spare part number, F18C serial, F192/F194 supplier hardware/software numbers,
  F180-F182 boot/application/data software versions, F190 VIN (only some modules).
- F183-F185 fingerprints: optional format byte 01, a tester or plant ID, then a date
  as three BCD bytes. Read as YYMMDD it gives no impossible dates on any module (read
  as DDMMYY the battery controller would be programmed in 2031), so YYMMDD it is.

Roles: each says why we think so. "Unknown" is an honest answer.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

IDENT_DIDS = list(range(0xF180, 0xF1A0))


@dataclass(frozen=True)
class Role:
    name: str
    evidence: str


ROLES = {
    0x44: Role("Battery controller (BPCM)", "answers the 97 cell voltages, charge and current"),
    0x42: Role("Vehicle controller (EVCU)",
               "mirrors the battery controller's capacity, cell extremes and temperatures"),
    0x43: Role("Power inverter (PIM)", "software ID INV2.3"),
    0x45: Role("On-board charger", "supplier software CHA501A, 'F500BEV'"),
    0x1F: Role("Telematics unit", "supplier string CONTI-F5GSM (Continental GSM)"),
    0x40: Role("Body controller, probably", "holds the VIN; software BEV_M1US; B-codes"),
    0x28: Role("ABS / stability control, probably", "monitors wheel-speed codes C0030-C003A"),
    0x47: Role("Charging-related, probably", "holds the VIN; a community sheet reads charger current "
               "there; reprogrammed with the 2020 pack"),
}


def _text(b: bytes) -> str:
    return "".join(chr(c) if 32 <= c < 127 else " " for c in b).strip()


def _bcd_date(b: bytes) -> dt.date | None:
    try:
        y, m, d = (int(f"{x:02x}") for x in b)
        return dt.date(2000 + y, m, d)
    except ValueError:  # not BCD, or not a real date
        return None


def fingerprint(raw: bytes) -> dict | None:
    """F183-F185: (format byte) tester/plant ID, then YYMMDD in BCD."""
    if len(raw) < 4:
        return None
    date = _bcd_date(raw[-3:])
    if date is None:
        return None
    body = raw[1:-3] if raw[0] == 0x01 else raw[:-3]
    return {"date": date.isoformat(), "by": _text(body.replace(b"\x00", b" ")) or None}


def parse_ident(dids: dict[str, str]) -> dict:
    """Readable identification from {did hex: data hex}. The VIN is kept apart."""
    raw = {int(k, 16): bytes.fromhex(v) for k, v in dids.items() if v}
    t = {k: _text(v[2:] if k in (0xF180, 0xF181, 0xF182) else v) for k, v in raw.items()}
    vin = t.get(0xF190) or None
    out = {
        "part": t.get(0xF187) or None, "serial": t.get(0xF18C) or None,
        "hw": t.get(0xF192) or None, "sw": t.get(0xF194) or None,
        "versions": [x for x in (t.get(0xF180), t.get(0xF181), t.get(0xF182)) if x],
        "vin": vin if vin and len(vin) == 17 else None,
        "fingerprints": {}, "notes": [],
    }
    for did, key in ((0xF183, "boot"), (0xF184, "application"), (0xF185, "data")):
        fp = fingerprint(raw[did]) if did in raw else None
        if fp:
            out["fingerprints"][key] = fp
    serial = raw.get(0xF18C, b"")
    if serial and not serial.strip(b"\x00 "):
        out["notes"].append("serial number blank")
    if any("xx" in (x or "") for x in (out["hw"], *out["versions"])):
        out["notes"].append("placeholder 'xx' in its numbers")
    return out


def programmed(ident: dict) -> str | None:
    """The latest date in the module's fingerprints: when it was last programmed."""
    dates = [fp["date"] for fp in ident["fingerprints"].values()]
    return max(dates) if dates else None


def history(modules: dict[int, dict], built: str | None = None) -> list[dict]:
    """Events worth a line in a history report, oldest first.

    A module programmed well after the car was built was reflashed or replaced in
    service. A blank serial or placeholder numbers point to a service part."""
    if built is None:
        dates = sorted(d for m in modules.values() if (d := programmed(m)) and d < "2014")
        built = dates[-1] if dates else None
    events = []
    for addr, m in modules.items():
        when = programmed(m)
        if not when or (built and when <= built):
            continue
        role = ROLES.get(addr, Role(f"Module {addr:02X}", "")).name
        what = "replaced or reprogrammed" if m["notes"] else "reprogrammed"
        events.append({"date": when, "module": f"{addr:02X}", "role": role, "what": what,
                       "notes": m["notes"], "by": m["fingerprints"].get("application", {}).get("by")})
    return sorted(events, key=lambda e: e["date"])


def vin_check(modules: dict[int, dict]) -> dict:
    """Do the modules that store a VIN agree? A mismatch means a part from another car."""
    vins = {addr: m["vin"] for addr, m in modules.items() if m.get("vin")}
    return {"modules": sorted(f"{a:02X}" for a in vins), "agree": len(set(vins.values())) <= 1,
            "count": len(vins)}
