"""Sampled DID sweep over 29-bit modules: 16 DIDs per page, every page. Read-only (0x22).

Logs every answer (data or NRC) to logs/<stamp>-pages-<ecu>.jsonl and flags blocks that
look like per-cell voltages (many 16-bit values in the 2.8-4.3 V range, in mV)."""
import json, sys, time
from e500.elm import Elm327, SerialLink

PORT, MODULES = sys.argv[1], [int(m, 16) for m in sys.argv[2:]]
stamp = time.strftime("%Y%m%d-%H%M%S")
e = Elm327(SerialLink(PORT, 115200), trace=f"logs/{stamp}-pages.trace.jsonl")
e.cmd("ATZ"); [e.cmd(c) for c in ("ATE0", "ATL0", "ATS0", "ATH1", "ATSP7", "ATCP18", "ATST19")]


def reassemble29(lines):
    msgs = {}
    for ln in lines:
        if len(ln) < 10 or len(ln) % 2 or any(c not in "0123456789ABCDEF" for c in ln):
            continue
        cid, raw = ln[:8], bytes.fromhex(ln[8:])
        k = raw[0] >> 4
        if k == 0: msgs[cid] = [raw[0] & 15, bytearray(raw[1:])]
        elif k == 1: msgs[cid] = [((raw[0] & 15) << 8) | raw[1], bytearray(raw[2:])]
        elif k == 2 and cid in msgs: msgs[cid][1] += raw[1:]
    return {c: bytes(d[:n]) for c, (n, d) in msgs.items()}


def cell_like(d):
    for order in ("big", "little"):
        vals = [int.from_bytes(d[i:i + 2], order) for i in range(0, len(d) - 1, 2)]
        if sum(2800 <= v <= 4300 for v in vals) >= 60:
            return order
    return None


for tt in MODULES:
    e.cmd(f"ATSHDA{tt:02X}F1"); e.cmd(f"ATFCSH18DA{tt:02X}F1"); e.cmd("ATFCSD300000"); e.cmd("ATFCSM1")
    out = open(f"logs/{stamp}-pages-{tt:02x}.jsonl", "w")
    pos, nrcs = 0, {}
    for page in range(0x100):
        for low in range(0x10):
            did = page << 8 | low
            r = reassemble29(e.cmd(f"22{did:04X}", timeout=3)).get(f"18DAF1{tt:02X}")
            rec = {"ecu": f"{tt:02x}", "did": f"{did:04x}"}
            if r and r[0] == 0x62:
                rec["hex"] = r[3:].hex(); pos += 1
                if cell_like(r[3:]):
                    rec["cell_like"] = cell_like(r[3:])
                    print(f"  ** module {tt:02X} DID {did:04X}: {len(r) - 3} bytes look like cell voltages", flush=True)
            elif r and r[0] == 0x7F:
                rec["nrc"] = f"{r[2]:02x}"; nrcs[rec["nrc"]] = nrcs.get(rec["nrc"], 0) + 1
            else:
                rec["error"] = "no reply"
            out.write(json.dumps(rec) + "\n")
        if page % 32 == 31:
            print(f"module {tt:02X}: through page {page:02X}, {pos} DIDs with data", flush=True)
    out.close()
    print(f"module {tt:02X} done: {pos} DIDs with data; rejections {nrcs}", flush=True)
e.close()
print("ALL DONE", flush=True)
