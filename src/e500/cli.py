"""e500 command line. Every command that talks to the car writes a trace to logs/."""

from __future__ import annotations

import argparse
import copy
import json
import os
import sys
import time
from pathlib import Path

from . import battery, scan
from .elm import AdapterError, Elm327, SerialLink
from .service import Car

ROOT = Path(__file__).resolve().parents[2]
LOG_DIR, DATA_DIR = ROOT / "logs", ROOT / "data"
BAUDS = (115200, 38400)  # OBDLink default, then the usual ELM327 default


def _guess_port() -> str:
    from serial.tools import list_ports
    cands = [p.device for p in list_ports.comports()
             if p.device.startswith("/dev/cu.") and ("usbserial" in p.device or "usbmodem" in p.device)]
    if len(cands) != 1:
        sys.exit(f"cannot pick the adapter automatically ({cands or 'no USB serial device'}); "
                 "use --port or E500_PORT")
    return cands[0]


def open_elm(args, cmd: str) -> Elm327:
    trace = None
    if not args.no_trace:
        LOG_DIR.mkdir(exist_ok=True)
        trace = str(LOG_DIR / f"{time.strftime('%Y%m%d-%H%M%S')}-{cmd}.jsonl")
        print(f"trace: {trace}", file=sys.stderr)
    if args.mock:
        from .mock import MockLink
        return Elm327(MockLink(dynamic=cmd == "ui"), trace, args.protocol)
    port = args.port or os.environ.get("E500_PORT") or _guess_port()
    for baud in ([args.baud] if args.baud else BAUDS):
        try:
            elm = Elm327(SerialLink(port, baud), trace, args.protocol)
        except OSError as e:
            sys.exit(f"cannot open {port}: {e}")
        try:
            elm.cmd("ATI", timeout=2.0)
            print(f"adapter on {port} at {baud} baud", file=sys.stderr)
            return elm
        except AdapterError:
            elm.close()
    sys.exit(f"no ELM327 answer on {port} at {', '.join(map(str, BAUDS))} baud")


def car_settings() -> dict:
    """Name, year and colour for the UI, from data/car.json (private, gitignored).
    The VIN can't supply the colour: paint isn't encoded in it."""
    path = DATA_DIR / "car.json"
    if not path.exists():
        DATA_DIR.mkdir(exist_ok=True)
        path.write_text(json.dumps({"name": "Fiat 500e", "year": 2013, "colour": "Orange",
                                    "hex": "#E2571E"}, indent=2) + "\n")
    return json.loads(path.read_text())


def _car(args, elm, log=None) -> Car:
    # The mock is shaped like the real car, so both use the real field table.
    from .mock import MOCK_HISTORY
    return Car(elm, battery.REAL_FIELDS, mock=args.mock, log=log,
               history=None if args.mock else DATA_DIR / "history.jsonl",
               seed_history=MOCK_HISTORY if args.mock else None, car=car_settings())


def _ecu(s: str) -> int:
    """A module as its byte (44) or a full CAN ID (18DA44F1, 7E0)."""
    from .uds import module_id
    v = int(s, 16)
    return module_id(v) if len(s) <= 2 else v


# -- commands ----------------------------------------------------------------------
def cmd_ports(args):
    from serial.tools import list_ports
    for p in list_ports.comports():
        if p.device.startswith("/dev/cu."):
            mark = "  <- likely the adapter" if "usb" in p.device else ""
            print(f"{p.device}  {p.description}{mark}")


def cmd_info(args, elm):
    print("adapter:", elm.init())
    print("protocol:", " ".join(elm.cmd("ATDP")))
    print("12 V at the OBD port:", elm.battery_volts(), "V")


def cmd_obd(args, elm):
    car = _car(args, elm)
    car.connect()
    pids = car._supported_pids()
    print("decodable OBD PIDs the car supports:", " ".join(f"{p:02X}" for p in pids) or "none")
    snap = car.snapshot()
    for pid in pids:
        p = battery.OBD_PIDS[pid]
        print(f"  {p.label:26s} {snap.get(p.name, snap['errors'].get(p.name))} {p.unit}")


def cmd_ecus(args, elm):
    from .uds import Client
    elm.init()
    elm.cmd("ATST19")  # ~100 ms reply wait: 256 addresses in under a minute
    ids = scan.MODULES_11 if args.protocol == "6" else scan.MODULES_29
    print(f"pinging {len(ids)} module addresses with TesterPresent (read-only)...")
    found = scan.find_modules(Client(elm), ids, progress=lambda tx: print(f"\r  {tx:X}", end="", flush=True))
    print()
    for tx, rx in found.items():
        print(f"module at {tx:X}, replies from {rx:X}")
    print(f"{len(found)} module(s). Next: e500 dids --ecu <module> --preset ident")


def cmd_dids(args, elm):
    from .uds import Client
    elm.init()
    start, end = scan.PRESETS[args.preset] if args.preset else (args.start, args.end)
    LOG_DIR.mkdir(exist_ok=True)
    out = LOG_DIR / f"dids-{args.ecu:x}.jsonl"
    print(f"reading DIDs {start:04X}-{end:04X} at {args.ecu:X} -> {out} (resumable)")

    def show(did, rec):
        if "hex" in rec:
            print(f"\r22 {did:04X}: {rec['hex']}")
        elif did % 64 == 0:
            print(f"\r  {did:04X}", end="", flush=True)
    n = scan.sweep_dids(Client(elm), args.ecu, start, end, out, show)
    print(f"\n{n} DID(s) answered")


def cmd_battery(args, elm):
    car = _car(args, elm)
    snap = car.snapshot()
    print(snap["verdict"])
    for f in car.fields:
        v = snap.get(f.name)
        if isinstance(v, list):
            v = f"{len(v)} values, {min(v)}-{max(v)}"
        print(f"  {f.label:26s} {v if v is not None else snap['errors'].get(f.name)} {f.unit}")
    if snap.get("soc") is not None:
        print(f"  {'Charge (as on the dash)':26s} {snap['soc']} %")
    stats = snap.get("cell_stats")
    if stats:
        low = ", ".join(f"#{c['cell']} {c['v']:.3f} V" for c in stats["lowest5"])
        print(f"  lowest five cells: {low}")


def cmd_log(args, elm):
    LOG_DIR.mkdir(exist_ok=True)
    path = LOG_DIR / f"{time.strftime('%Y%m%d-%H%M%S')}-battery.jsonl"
    print(f"logging a snapshot every {args.every:g} s to {path} (Ctrl-C to stop)")
    with path.open("a", buffering=1) as f:
        car = _car(args, elm, log=f)
        try:
            while True:
                snap = car.snapshot()
                stats = snap.get("cell_stats") or {}
                print(time.strftime("%H:%M:%S"), f"charge {snap.get('soc')} %",
                      f"spread {stats.get('spread_mv')} mV", flush=True)
                time.sleep(args.every)
        except KeyboardInterrupt:
            pass


def _latest(pattern: str) -> Path | None:
    found = sorted(LOG_DIR.glob(pattern))
    return found[-1] if found else None


def load_modules(path: Path | None) -> dict[int, dict]:
    """{module: parsed identification} from an identification capture (ecu/did/hex lines)."""
    from . import modules as mods
    raw: dict[int, dict[str, str]] = {}
    if path:
        for line in path.read_text().splitlines():
            r = json.loads(line)
            if "hex" in r and r.get("did", "").lower().startswith("f1"):
                raw.setdefault(int(r["ecu"], 16), {})[r["did"].lower()] = r["hex"]
    return {addr: mods.parse_ident(d) for addr, d in raw.items()}


def cmd_recalls(args):
    """Recalls by make, model and year from NHTSA. No VIN leaves this machine."""
    import urllib.request
    car = car_settings()
    url = ("https://api.nhtsa.gov/recalls/recallsByVehicle?make=FIAT&model=500E"
           f"&modelYear={car.get('year', 2013)}")
    with urllib.request.urlopen(url, timeout=20) as r:
        recalls = json.load(r).get("results", [])
    out = DATA_DIR / f"recalls-{car.get('year', 2013)}.json"
    out.write_text(json.dumps(recalls, indent=1))
    print(f"{len(recalls)} recalls -> {out}")


def report_inputs(charge: Path | None = None):
    """What the report takes from recordings: the latest charge session and the recalls."""
    from . import analysis
    charge = charge or _latest("*-charge.jsonl")
    car = car_settings()
    session = analysis.session(analysis.load(charge)) if charge else None
    rpath = DATA_DIR / f"recalls-{car.get('year', 2013)}.json"
    recalls = json.loads(rpath.read_text()) if rpath.exists() else None
    return session, recalls, [p.name for p in (charge, rpath) if p and p.exists()]


def load_faults(path: Path | None) -> list[dict] | None:
    if not path:
        return None
    recs = [json.loads(line) for line in path.read_text().splitlines() if '"faults"' in line]
    return [r["faults"] for r in recs] or None


def cmd_report(args):
    """The battery and history report from what's been recorded (no car needed)."""
    from . import analysis, report
    charge = Path(args.charge) if args.charge else _latest("*-charge.jsonl")
    ident = Path(args.ident) if args.ident else (_latest("*-survey-result.jsonl")
                                                  or _latest("*ident*-results.jsonl"))
    faults = _latest("*-survey-result.jsonl") or _latest("*-faults-result.jsonl")
    car = car_settings()
    session = analysis.session(analysis.load(charge)) if charge else None
    odometer = None
    if charge:
        last = json.loads(charge.read_text().splitlines()[-1])
        if (last.get("44") or {}).get("2001"):
            raw = int(last["44"]["2001"], 16)
            odometer = {"raw_44": raw, "km": raw / 10, "mi": raw / 10 / 1.609344}
    rpath = DATA_DIR / f"recalls-{car.get('year', 2013)}.json"
    recalls = json.loads(rpath.read_text()) if rpath.exists() else None
    page = report.build(car=car, session=session, modules=load_modules(ident), recalls=recalls,
                        faults=load_faults(faults), odometer=odometer,
                        sources=[str(p.name) for p in (charge, ident, faults, rpath) if p and p.exists()])
    out = DATA_DIR / f"report-{time.strftime('%Y%m%d')}.html"
    out.write_text(page)
    print(f"report -> {out}")
    if not args.no_browser:
        import webbrowser
        webbrowser.open(out.as_uri())


def _clock(seconds: float) -> str:
    seconds = max(0, int(seconds))
    return f"{seconds // 3600}:{seconds // 60 % 60:02d}:{seconds % 60:02d}" if seconds >= 3600 \
        else f"{seconds // 60}:{seconds % 60:02d}"


class SurveyView:
    """Shows a survey's progress: one live line that updates in place, and a permanent
    line when a module or stage finishes. Without a terminal, only the permanent lines."""

    def __init__(self, stream=sys.stderr):
        self.out, self.live = stream, stream.isatty()
        self.stage = self.module = None
        self.notes = 0
        self.t0 = self.stage_t0 = time.monotonic()
        self.stage_reads = 0
        self.width = 0

    def _line(self, text: str) -> None:
        if self.live:
            self.out.write("\r" + text.ljust(self.width) + "\n")  # pad to clear the live line
            self.width = 0
        else:
            self.out.write(text + "\n")
        self.out.flush()

    def _live(self, text: str) -> None:
        if self.live:
            self.width = max(self.width, len(text))
            self.out.write("\r" + text.ljust(self.width))
            self.out.flush()

    @staticmethod
    def _bar(done: int, total: int, width: int = 20) -> str:
        full = int(width * done / total) if total else width
        return "█" * full + "░" * (width - full)

    def _name(self, addr: int) -> str:
        from .modules import ROLES
        role = ROLES.get(addr)
        return f"{addr:02X} {role.name if role else ''}".strip()

    def __call__(self, p) -> None:
        if (p.stage, p.module) != (self.stage, self.module):
            self._finish()
            self.stage, self.module = p.stage, p.module
            if p.stage != getattr(self, "_last_stage", None):
                self.stage_t0, self.stage_reads, self._last_stage = time.monotonic(), p.reads_left, p.stage
        while self.notes < len(p.notes):
            self._line("  " + p.notes[self.notes])
            self.notes += 1
        self.last = copy.copy(p)  # the survey reuses one Progress; keep this moment's numbers
        elapsed = time.monotonic() - self.stage_t0
        if p.stage == "Finding modules":
            self._live(f"Finding modules  {self._bar(p.done, p.total)} {p.done}/{p.total}")
        elif p.stage == "Fault codes":
            self._live(f"Fault codes      {self._bar(p.done, p.total)} {p.done}/{p.total} modules"
                       f" · {p.answered} fault{'s' if p.answered != 1 else ''} so far")
        else:
            rate = (self.stage_reads - p.reads_left) / elapsed if elapsed > 5 else 1 / 0.05
            left = p.reads_left / rate if rate else 0
            self._live(f"[{p.module_index:>2}/{p.module_count}] {self._name(p.module)[:30]:30s} "
                       f"{self._bar(p.done, p.total, 14)} {p.done:,}/{p.total:,} · {p.answered} answered"
                       f" · about {_clock(left)} left in all")

    def _finish(self) -> None:
        p = getattr(self, "last", None)
        if p is None or self.stage is None:
            return
        if p.stage == "Finding modules":
            self._line(f"Finding modules  done ({_clock(time.monotonic() - self.stage_t0)})")
        elif p.stage == "Fault codes" and p.done == p.total:
            self._line(f"Fault codes      {p.total} modules read · {p.answered} stored or active "
                       f"fault{'s' if p.answered != 1 else ''}")
        elif p.stage == "Reading":
            self._line(f"  ✓ {self._name(p.module)}: {p.answered} of {p.done:,} identifiers answered"
                       if p.done == p.total else
                       f"  ⏸ {self._name(p.module)}: stopped at {p.done:,} of {p.total:,}")

    def close(self) -> None:
        self._finish()
        self.stage = None


def cmd_survey(args, elm):
    from . import survey
    from .uds import Client
    elm.init()
    volts = elm.battery_volts()
    if volts is not None and volts < 13.0 and not args.anyway:
        sys.exit(f"The car looks off: {volts:.1f} V at the OBD port (on, it reads about 14.4 V).\n"
                 "Turn it on (ready light), then run this again. --anyway skips this check.")
    LOG_DIR.mkdir(exist_ok=True)
    resume = None if args.fresh else survey.unfinished(LOG_DIR)
    out = LOG_DIR / f"{time.strftime('%Y%m%d-%H%M%S')}-survey-result.jsonl"
    while out.exists():  # two runs in the same second
        out = out.with_name(out.stem + "+.jsonl")
    what = ("fault codes, identification and counters on every module, then the data we already "
            "know" + (", then every identifier in 8 data pages per module" if args.deep else ""))
    print(f"Survey of the car, read-only: {what}.", file=sys.stderr)
    if resume:
        mods_, read, faults = survey.state(resume)
        print(f"Continuing the survey started {resume.name[9:11]}:{resume.name[11:13]} "
              f"({len(read):,} identifiers already read). --fresh starts over.", file=sys.stderr)
    else:
        minutes = max(1, round(survey.estimate_s(args.deep) / 60))
        print(f"About {minutes} minute{'s' if minutes != 1 else ''}; keep the car on the whole time.", file=sys.stderr)
    print("Ctrl-C pauses: everything read so far is kept, and running the same command again "
          "continues from there.\n", file=sys.stderr)
    view = SurveyView()
    try:
        res = survey.run(Client(elm), out, LOG_DIR, deep=args.deep, show=view, resume=resume)
    except KeyboardInterrupt:
        view.close()
        path = resume or out
        print(f"\nPaused. Everything read so far is in {path.name}.\n"
              f"Run `uv run e500 survey{' --deep' if args.deep else ''}` again to continue.", file=sys.stderr)
        return
    view.close()
    print(f"\nDone: {res['faults']} fault{'s' if res['faults'] != 1 else ''}, {res['answered']:,} identifiers "
          f"answered -> {res['path'].name}\n"
          "Next: uv run e500 report (or scripts/dossier.py)", file=sys.stderr)


def cmd_record(args, elm):
    """Record the battery while driving or parked, until Ctrl-C. Enter drops a marker."""
    import select
    from . import recorder, survey
    from .uds import Client
    elm.init()
    elm.cmd("ATST32")
    LOG_DIR.mkdir(exist_ok=True)
    out = LOG_DIR / f"{time.strftime('%Y%m%d-%H%M%S')}-recording.jsonl"
    err = sys.stderr
    print("Recording the battery, read-only: current, charge, cells, temperatures about every 2 s,\n"
          "plus everything else known, once every few minutes.\n"
          "Press Enter when it happens (or anything worth noting): it marks the time.\n"
          "Ctrl-C stops. Driver: don't touch the laptop while driving; a passenger presses Enter,\n"
          "or note the time and tell me.\n", file=err)
    tty = sys.stdin.isatty()

    def marks() -> int:
        n = 0
        while tty and select.select([sys.stdin], [], [], 0)[0]:
            if not sys.stdin.readline():
                break
            n += 1
        return n

    width = [0]

    def show(live) -> None:
        v = live.values
        el = _clock(time.time() - live.t0)
        parts = [el]
        if "speed_mph" in v:
            parts.append(f"{v['speed_mph']:.0f} mph")
        if "current_a" in v:
            parts.append(f"{v['current_a']:+.1f} A" + (f" ({v['power_kw']:+.1f} kW)" if "power_kw" in v else ""))
        if "soc_true" in v:
            parts.append(f"charge {v['soc_true']:.1f} % (dash {v['soc_dash']:.0f})")
        if "cell_min_v" in v:
            parts.append(f"cells {v['cell_min_v']:.3f}–{v['cell_max_v']:.3f} V"
                         + (f" (low #{v['cell_min_no']})" if "cell_min_no" in v else ""))
        if "temp_max_c" in v:
            parts.append(f"battery {v['temp_min_c']}–{v['temp_max_c']} °C")
        if live.marks:
            parts.append(f"marks {live.marks}")
        if live.errors:
            parts.append(f"{live.errors} missed")
        line = " · ".join(parts)
        if err.isatty():
            width[0] = max(width[0], len(line))
            err.write("\r" + line.ljust(width[0]))
            err.flush()
        elif live.loops % 30 == 1:
            print(line, file=err)

    try:
        live = recorder.run(Client(elm), out, survey.known_dids(LOG_DIR), show=show, marks=marks)
    except KeyboardInterrupt:
        pass
    print(f"\n\nStopped. Recording in {out.name}. Tell me roughly when it happened, or use the marks.",
          file=err)


def cmd_volts(args, elm):
    """Log the 12 V battery until Ctrl-C, without putting anything on the car's bus."""
    import datetime as dt
    from . import voltlog
    elm.cmd("ATE0")  # adapter settings only: no protocol, no requests to the car
    LOG_DIR.mkdir(exist_ok=True)
    out = LOG_DIR / f"{time.strftime('%Y%m%d-%H%M%S')}-volts.jsonl"
    err = sys.stderr
    print(f"Logging the 12 V battery every {args.every:g} s to {out.name}.\n"
          "Nothing is sent to the car: the adapter only measures its own supply pin, so no\n"
          "module wakes up. Turn the car off, close and lock it, and leave the laptop awake\n"
          "(lid open, on power). Ctrl-C in the morning.\n", file=err)
    width = [0]

    def show(n) -> None:
        rate = n.rate_mv_per_h()
        parts = [_clock(time.time() - n.t0), f"{n.last:.1f} V" if n.last is not None else "no reading",
                 f"drop {rate:.0f} mV/h" if rate is not None else "drop rate after the first hour",
                 f"{len(n.steps)} step{'s' if len(n.steps) != 1 else ''}"]
        if n.missed:
            parts.append(f"{n.missed} missed")
        line = " · ".join(parts)
        if err.isatty():
            width[0] = max(width[0], len(line))
            err.write("\r" + line.ljust(width[0]))
            err.flush()

    try:
        n = voltlog.run(elm.battery_volts, out, every=args.every, show=show)
    except KeyboardInterrupt:
        n = None
    if n is None:  # summarise from the file
        rows = [json.loads(line) for line in out.read_text().splitlines()]
        vs = [(r["t"], r["v"]) for r in rows if r["v"] is not None]
        if vs:
            hours = (vs[-1][0] - vs[0][0]) / 3600
            print(f"\n\n{hours:.1f} h: {vs[0][1]:.1f} V -> {vs[-1][1]:.1f} V "
                  f"(lowest {min(v for _, v in vs):.1f} V). File: {out.name}. Send it to me for the analysis.",
                  file=err)


def cmd_faults(args, elm):
    """Fault codes from every known module (read-only 0x19; never cleared)."""
    from . import dtc, survey
    from .uds import Client, module_id
    elm.init()
    client = Client(elm)
    LOG_DIR.mkdir(exist_ok=True)
    out = LOG_DIR / f"{time.strftime('%Y%m%d-%H%M%S')}-faults-result.jsonl"
    with out.open("w") as f:
        for addr in survey.KNOWN:
            rec = dtc.read(client, module_id(addr))
            f.write(json.dumps({"faults": rec}) + "\n")
            codes = rec.get("codes")
            state = (f"{len(codes)} code(s)" if codes else "none") if codes is not None else \
                f"not answered ({rec.get('nrc') or rec.get('error')})"
            print(f"{addr:02X}: {state}")
            for c in codes or []:
                print(f"    {c['code']}  {', '.join(c['status_text'])}"
                      + (f"  ({c['meaning']})" if c.get("meaning") else ""))
    print(f"-> {out}")


def cmd_ui(args, elm):
    from .web import serve
    LOG_DIR.mkdir(exist_ok=True)
    path = LOG_DIR / f"{time.strftime('%Y%m%d-%H%M%S')}-battery.jsonl"
    with path.open("a", buffering=1) as f:
        serve(_car(args, elm, log=None if args.mock else f), port=args.http_port,
              every=args.every, open_browser=not args.no_browser,
              report_inputs=(lambda: (None, None, ())) if args.mock else report_inputs)


def main(argv=None):
    ap = argparse.ArgumentParser(prog="e500", description=__doc__)
    ap.add_argument("--port", help="adapter serial device, e.g. /dev/cu.usbserial-XXXX")
    ap.add_argument("--baud", type=int, help=f"adapter baud rate (default: try {BAUDS})")
    ap.add_argument("--mock", action="store_true", help="simulated car and adapter")
    ap.add_argument("--no-trace", action="store_true")
    ap.add_argument("--protocol", choices=["7", "6"], default="7",
                    help="CAN 29-bit (7, the 500e) or 11-bit (6), both 500 kbit/s")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("ports", help="list serial ports")
    sub.add_parser("info", help="adapter, protocol, 12 V")
    sub.add_parser("obd", help="generic OBD values the car supports")
    sub.add_parser("ecus", help="find modules (read-only TesterPresent sweep)")
    p = sub.add_parser("dids", help="sweep a module's data identifiers (read-only)")
    p.add_argument("--ecu", type=_ecu, required=True, help="module byte (44) or CAN ID (18DA44F1)")
    p.add_argument("--preset", choices=sorted(scan.PRESETS))
    p.add_argument("--start", type=lambda s: int(s, 16), default=0xF180)
    p.add_argument("--end", type=lambda s: int(s, 16), default=0xF1FF)
    sub.add_parser("battery", help="one battery snapshot")
    p = sub.add_parser("log", help="log battery snapshots until Ctrl-C")
    p.add_argument("--every", type=float, default=30.0, help="seconds between snapshots")
    p = sub.add_parser("ui", help="open the local web interface")
    p.add_argument("--every", type=float, default=1.0, help="seconds between live updates")
    p.add_argument("--http-port", type=int, default=8500)
    p.add_argument("--no-browser", action="store_true")

    sub.add_parser("faults", help="read fault codes from every module (read-only; never clears)")
    sub.add_parser("record", help="record the battery while driving or parked; Enter marks, Ctrl-C stops")
    p = sub.add_parser("volts", help="log the 12 V battery overnight (adapter only; nothing sent to the car)")
    p.add_argument("--every", type=float, default=5.0, help="seconds between readings")
    p = sub.add_parser("survey", help="record everything the report uses, in one run (read-only)")
    p.add_argument("--deep", action="store_true", help="also sweep every module's data pages (~20 min)")
    p.add_argument("--fresh", action="store_true", help="start over instead of continuing a paused survey")
    p.add_argument("--anyway", action="store_true", help="run even if the 12 V says the car is off")
    p = sub.add_parser("report", help="battery and history report from recorded data (no car)")
    p.add_argument("--charge", help="charge log (default: the latest logs/*-charge.jsonl)")
    p.add_argument("--ident", help="identification capture (default: the latest)")
    p.add_argument("--no-browser", action="store_true")
    sub.add_parser("recalls", help="fetch recalls for the model year from NHTSA (no VIN sent)")

    args = ap.parse_args(argv)
    if args.cmd in ("ports", "report", "recalls"):
        return {"ports": cmd_ports, "report": cmd_report, "recalls": cmd_recalls}[args.cmd](args)
    elm = open_elm(args, args.cmd)
    fn = {"info": cmd_info, "obd": cmd_obd, "ecus": cmd_ecus, "dids": cmd_dids,
          "battery": cmd_battery, "log": cmd_log, "ui": cmd_ui, "survey": cmd_survey,
          "faults": cmd_faults, "record": cmd_record, "volts": cmd_volts}[args.cmd]
    try:
        fn(args, elm)
    except AdapterError as e:
        sys.exit(f"error: {e}")
    finally:
        elm.close()


if __name__ == "__main__":
    main()
