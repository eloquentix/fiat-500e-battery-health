"""Poll every known data ID on the battery-related modules, one JSON line per pass. Read-only.
Runs until logs/charge.stop exists. Usage: charge_log.py PORT"""
import glob, json, os, sys, time
from pathlib import Path
from e500.elm import Elm327, SerialLink

stamp = time.strftime("%Y%m%d-%H%M%S")
dids = {}
for m in ("44", "42", "40"):
    seen = set()
    for f in glob.glob(f"logs/*-full-{m}.jsonl"):
        seen |= {json.loads(l)["did"] for l in open(f) if '"hex"' in l}
    dids[m] = sorted(seen)
e = Elm327(SerialLink(sys.argv[1], 115200))
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


def read(tt, did):
    for _ in range(2):
        try:
            r = reassemble29(e.cmd(f"22{did}", timeout=3)).get(f"18DAF1{tt}")
            return r[3:].hex() if r and r[0] == 0x62 else None
        except Exception:  # lost prompt: resync and retry once
            e.link.write(b"\r")
            try: e.link.read_prompt(3)
            except Exception: pass
    return None


out = open(f"logs/{stamp}-charge.jsonl", "a", buffering=1)
print(f"logging {sum(map(len, dids.values()))} DIDs per pass to logs/{stamp}-charge.jsonl", flush=True)
n = 0
while not Path("logs/charge.stop").exists():
    rec = {"t": round(time.time(), 1), "v12": e.cmd("ATRV")[0]}
    for m, ds in dids.items():
        e.cmd(f"ATSHDA{m.upper()}F1"); e.cmd(f"ATFCSH18DA{m.upper()}F1"); e.cmd("ATFCSD300000"); e.cmd("ATFCSM1")
        rec[m] = {d: read(m.upper(), d.upper()) for d in ds}
    out.write(json.dumps(rec) + "\n")
    n += 1
    if n % 10 == 1:
        print(time.strftime("%H:%M:%S"), f"pass {n}", flush=True)
e.close()
print("stopped", flush=True)
