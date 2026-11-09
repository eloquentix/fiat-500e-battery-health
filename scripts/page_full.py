"""Full sweep of chosen DID pages on 29-bit modules. Read-only (0x22).
Usage: page_full.py PORT MODULE:PAGE,PAGE,... [MODULE:PAGES ...]"""
import json, sys, time
from e500.elm import Elm327, SerialLink

stamp = time.strftime("%Y%m%d-%H%M%S")
e = Elm327(SerialLink(sys.argv[1], 115200), trace=f"logs/{stamp}-full.trace.jsonl")
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


for spec in sys.argv[2:]:
    mod, pages = spec.split(":")
    tt = int(mod, 16)
    e.cmd(f"ATSHDA{tt:02X}F1"); e.cmd(f"ATFCSH18DA{tt:02X}F1"); e.cmd("ATFCSD300000"); e.cmd("ATFCSM1")
    with open(f"logs/{stamp}-full-{tt:02x}.jsonl", "w") as out:
        for page in (int(p, 16) for p in pages.split(",")):
            n = 0
            for low in range(0x100):
                did = page << 8 | low
                try:
                    lines = e.cmd(f"22{did:04X}", timeout=3)
                except Exception as err:  # lost prompt: resync with a bare CR, retry once
                    print(f"  {did:04X}: {err}; retrying", flush=True)
                    e.link.write(b"\r")
                    try:
                        e.link.read_prompt(3)
                    except Exception:
                        pass
                    lines = e.cmd(f"22{did:04X}", timeout=3)
                r = reassemble29(lines).get(f"18DAF1{tt:02X}")
                rec = {"ecu": f"{tt:02x}", "did": f"{did:04x}", "t": round(time.time(), 2)}
                if r and r[0] == 0x62:
                    rec["hex"] = r[3:].hex(); n += 1
                elif r and r[0] == 0x7F:
                    rec["nrc"] = f"{r[2]:02x}"
                else:
                    rec["error"] = "no reply"
                out.write(json.dumps(rec) + "\n")
            print(f"module {tt:02X} page {page:02X}: {n} DIDs with data", flush=True)
e.close()
print("ALL DONE", flush=True)
