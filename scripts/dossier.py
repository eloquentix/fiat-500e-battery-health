"""Everything we know about this car, in one self-contained HTML file for other agents.

Built from the recordings in logs/ (nothing is read from the car). Private: it carries
the VIN and module serials. Output: data/dossier-<date>.html, with a JSON copy of every
finding embedded in <script id="dossier-data" type="application/json">.

Usage: uv run python scripts/dossier.py
"""

from __future__ import annotations

import datetime as dt
import glob
import json
import statistics as st
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from e500 import analysis, battery, dtc, modules as mods, report  # noqa: E402

LOGS, DATA = ROOT / "logs", ROOT / "data"
esc, n, fmt_date = report.esc, report.n, report.fmt_date


def jl(path: Path) -> list[dict]:
    out = []
    for line in path.read_text().splitlines():
        try:
            out.append(json.loads(line))
        except ValueError:
            pass
    return out


def latest(pattern: str) -> Path:
    return Path(sorted(glob.glob(str(LOGS / pattern)))[-1])


# -- inputs -------------------------------------------------------------------------------
CHARGE = latest("*-charge.jsonl")
IDENT = latest("*ident29-results.jsonl")
SURVEYS = sorted(Path(p) for p in glob.glob(str(LOGS / "*-survey*.jsonl")) if '"modules"' in open(p).readline())
FAULTS = latest("*-faults-result.jsonl")
RECALLS = json.loads((DATA / "recalls-2013.json").read_text())
CAR = json.loads((DATA / "car.json").read_text())

rows = jl(CHARGE)
session = analysis.session(analysis.points(rows))

ident_raw: dict[int, dict[str, str]] = {}
for r in jl(IDENT):
    if "hex" in r:
        ident_raw.setdefault(int(r["ecu"], 16), {})[r["did"].lower()] = r["hex"]
# Every survey merged, later values winning; each value keeps the time of its run.
survey_raw: dict[int, dict[str, str]] = {}
survey_t: dict[tuple[int, str], dt.datetime] = {}
for path in SURVEYS:
    t = dt.datetime.strptime(path.name[:15], "%Y%m%d-%H%M%S")
    for r in jl(path):
        if r.get("ecu") and "hex" in r:
            survey_raw.setdefault(int(r["ecu"], 16), {})[r["did"].lower()] = r["hex"]
            survey_t[(int(r["ecu"], 16), r["did"].lower())] = t
for addr, d in survey_raw.items():  # fill gaps in the first identification read
    for k, v in d.items():
        if k.startswith("f1"):
            ident_raw.setdefault(addr, {}).setdefault(k, v)
modules = {a: mods.parse_ident(d) for a, d in ident_raw.items()}
fault_recs = [r["faults"] for r in jl(FAULTS) if "faults" in r]
fault_rows = dtc.summarise(fault_recs)

vins = {a: m["vin"] for a, m in modules.items() if m.get("vin")}
VIN = next(iter(vins.values()), None)
ODO_KM = int(rows[-1]["44"]["2001"], 16) / 10
ODO_MI = ODO_KM / 1.609344

def clock(addr: int) -> tuple[int, dt.datetime] | None:
    """A module's clock (DID 1008, minutes) and when we read it."""
    h = survey_raw.get(addr, {}).get("1008")
    return (int(h, 16), survey_t[(addr, "1008")]) if h else None


# Module 40's clock equals the car's age: it runs all the time, so it dates faults.
clock40_now, T40 = clock(0x40)
built_from_clock = T40 - dt.timedelta(minutes=clock40_now)
SURVEY_T = T40


def date_from_clock40(minutes: int) -> dt.date:
    return (T40 - dt.timedelta(minutes=clock40_now - minutes)).date()


# BMS clock: running minutes only (a few % of the time since it was programmed).
clock44_now, T44 = clock(0x44)
bms_since = dt.datetime(2019, 12, 31)
bms_duty = clock44_now / ((T44 - bms_since).total_seconds() / 60)

# Today's session ran from the first recording to the last UI log: awake minutes count back from there.
SESSION = [dt.datetime(2026, 11, 7, 13, 30), dt.datetime(2026, 11, 7, 18, 35)]
DOOR40_DATE = None
d2023 = survey_raw.get(0x40, {}).get("2023")
if d2023:
    tail = bytes.fromhex(d2023)
    k = tail.find(b"\x13\x09")  # BCD date after the ASCII block
    DOOR40_DATE = mods._bcd_date(tail[22:25]) if len(tail) >= 25 else None

# Dash readings the owner noted while the car charged (local time), kept in data/ (private).
NOTES = json.loads((DATA / "dossier-notes.json").read_text()) if (DATA / "dossier-notes.json").exists() else {}
DASH = [tuple(d) for d in NOTES.get("dash_readings", [])]  # (time, dash %, range mi, note), private


def a602_at(hhmm: str) -> float | None:
    day = dt.datetime.fromtimestamp(rows[0]["t"]).date()
    t = dt.datetime.combine(day, dt.time(*map(int, hhmm.split(":")))).timestamp()
    best = min(rows, key=lambda r: abs(r["t"] - t))
    return round(int(best["44"]["a602"], 16) * 0.4, 1) if abs(best["t"] - t) < 120 else None


dash_rows = []
for hhmm, dash, rng, note in DASH:
    true = a602_at(hhmm)
    dash_rows.append({"time": hhmm, "dash_pct": dash, "range_mi": rng, "true_pct": true,
                      "predicted_dash": round(battery.dash_soc(true), 1) if true else None, "note": note})


# -- decoded signals (the core table) ------------------------------------------------------
def first_last(m: str, did: str):
    vals = [r.get(m, {}).get(did) for r in rows if r.get(m, {}).get(did)]
    return (vals[0], vals[-1], len(set(vals))) if vals else (None, None, 0)


SIGNALS = [
    # module, DID(s), name, format, scale/unit, confidence, evidence
    ("44", "A100–A108", "Cell voltages 1–97", "97 × u16 BE (8 DIDs × 12, A108 × 1)", "mV", "confirmed",
     "Agree with A609/A60A/A60B to 1 mV in the same minute; sum = pack voltage."),
    ("44", "A602", "True state of charge", "u8", "× 0.4 %", "confirmed",
     "Dash % = 1.1195 × true − 12.26 fits 4 dash readings within 0.4 % and a 5th held out."),
    ("44", "A609 / A60A / A60B", "Highest / lowest / average cell", "u16 BE", "mV", "confirmed",
     "Equal to max/min/mean of A100–A108."),
    ("44", "A009", "Cell extremes", "u8 hi-cell#, u8 lo-cell#, u16 hi mV, u16 lo mV, 4 bytes ?", "mV", "confirmed (first 6 bytes)",
     "Cell numbers and voltages match A100–A108. Layout lead: OBDb gen-2 A009."),
    ("44", "A012", "Pack current", "2 × s16 BE (first used)", "÷ 20 A, negative = charging", "confirmed",
     "−15.45 A × 393 V = 6.1 kW steady on the 6.6 kW charger; ≈ +0.5 A at rest. Scale lead: OBDb gen-2."),
    ("44", "A611", "Pack current (second reading)", "s16 BE", "÷ 20 A", "likely", "Tracks A012."),
    ("44", "A615", "Lifetime charge in", "u24 BE", "÷ 10 Ah", "confirmed",
     "+7.7 Ah while ∫current gave 7.71 Ah; +5.4 vs 5.39 on another window."),
    ("44", "A616", "Lifetime charge out", "u24 BE", "÷ 10 Ah", "confirmed", "Companion of A615; still while charging."),
    ("44", "A625", "Capacity (controller's working figure)", "u16 BE", "÷ 10 Ah", "confirmed",
     "37.9 Ah; equals 42:2050; the controller's charge moved 36.8 Ah per 100 % against ∫current."),
    ("44", "A029", "Capacity block", "u16 ?, u16 capacity, u16 remaining, 4 bytes ?", "÷ 10 Ah", "partial",
     "Bytes 2–3 = A625; bytes 4–5 = 42:2051. OBDb gen-2 A029 (SOH) layout does not fit."),
    ("44", "A200", "Module temperatures × 18", "18 × u8", "− 40 °C", "confirmed",
     "Min/max/mean equal 42:2060/205F/205E. Layout lead: OBDb gen-2 A200."),
    ("44", "A042 (= A629)", "12 V battery", "u16 BE", "mV", "confirmed",
     "14.26 → 14.38 V; adapter ATRV read 14.3–14.4 V. Lead: OBDb gen-2 A042."),
    ("44", "2001", "Odometer", "u24 BE", "÷ 10 km", "confirmed", "99,324 km = 61,717 mi; dash showed 61,720."),
    ("42", "204C", "Odometer", "u24 BE", "km", "confirmed", "99,324: same as 44:2001 ÷ 10."),
    ("42", "2050", "Capacity (mirror)", "u16 BE", "÷ 10 Ah", "confirmed", "= 44:A625."),
    ("42", "2051", "Remaining charge", "u16 BE", "÷ 10 Ah", "likely", "26.6 → 36.7 Ah over the charge (+10.1 vs +10.5 counted)."),
    ("42", "2052 / 205C / 205D", "Average / highest / lowest cell", "u16 BE", "mV", "confirmed", "Mirror 44's values."),
    ("42", "205E / 205F / 2060", "Battery temperature mean / max / min", "u8", "− 40 °C", "confirmed", "Equal A200's stats."),
    ("42", "2063", "Pack voltage", "u16 BE", "V", "likely", "388 → 397 V; cell sum 393 → 397."),
    ("42", "2054 / 2055", "Cell voltage limits", "u16 BE", "mV", "likely", "4150 and 2699 mV: also in 44:A026."),
    ("all", "1008", "Module clock", "u32 BE", "minutes", "confirmed",
     "+1 per minute while awake on every module. Module 40's reads 13.3 years: always running since the car was built."),
    ("44", "A037", "Seconds counter", "u32 BE", "s", "likely", "+7,174 over a 7,174 s log."),
    ("44", "A005", "Time histogram (8 bins)", "12 × u16 BE (4 unused)", "≈ 5 min per count", "hypothesis",
     "Bin 7 grew while cells were ≥ 4.0 V, bin 6 at 3.98–3.99 V: probably time per cell-voltage band. Sum × 5 min ≈ the controller's running time."),
    ("44", "A028", "Four counters", "4 × u32 BE", "?", "unknown",
     "2,539 / 5,521 / 36 / 1,913; did not change across a plug-in and full charge, so not charge counts."),
    ("44", "A014", "0x025A = 602", "u16 + ?", "?", "hypothesis", "60.2 could be the new-pack rating in Ah; unproven."),
    ("44", "A026 / A045", "Voltage thresholds", "12 × u16 BE", "mV", "likely",
     "4200, 4150, 4100, 4080, 4030, 4000, 3000, 2699, 2618, 1999, 1939 mV: cell limits."),
    ("44", "A056", "18 × u8", "u8 − 40?", "°C?", "unknown", "23 → 27 °C, 18 values: not A200; perhaps another sensor set."),
    ("44", "A606 / A607 / A608", "Battery temperature max / min / average", "u8", "− 50 °C", "confirmed",
     "Equal A200's max/min/mean at both ends of the charge (16/15/15 → 23/20/21). Lead: carlrice sheet (A608)."),
    ("44", "A603 / A604 / A605", "Other temperatures?", "u8", "− 50 °C?", "unknown",
     "17 → 18; 31 → 16 and 30 → 16 over the charge (A604/A605 fell while the pack warmed)."),
    ("42", "0303", "Key on / ready", "u8", "1 = on", "likely", "1 throughout. Lead: carlrice sheet."),
    ("42", "2056", "HV contactors", "u8", "1 = closed", "likely", "1 while charging. Lead: carlrice sheet."),
    ("42", "2082 / 20E1", "Charging / charge state", "u8", "1 = charging; 2 = allowed/charging", "likely",
     "1 and 2 throughout the charge. Lead: carlrice sheet."),
    ("42", "2081", "Charging current (EVCU)", "u8", "× 0.25 A", "likely",
     "66 → 0: 16.5 A while charging vs 15.45 A from the battery controller. Scale from the sheet's 0x0C = 3 A."),
    ("42", "205B", "Pack current (EVCU)", "u16 BE", "(raw − 1023) × 0.5 A", "likely",
     "1021 → 993 → 1025: −15 A mid-charge, ≈ 0 at rest; matches A012. Lead: carlrice sheet (unscaled)."),
    ("42", "2083", "Charge voltage limit", "u16 BE", "V", "likely", "408 V constant (97 × 4.2 V = 407.4 V)."),
    ("42", "204F", "Road speed", "u16 BE", "÷ 16 km/h", "confirmed",
     "∫speed = 13.4 km while 44:2001 and 42:204C moved 14 km (1 km steps) over a 25 min drive, 2026-11-08. "
     "The community sheet's ÷ 10 is wrong here; its CAN-B speed uses the same 1/16 scale."),
    ("42", "0404", "Outside air", "u8", "− 51 °F", "plausible", "54 → 49 °F into a November evening. Lead: carlrice sheet."),
    ("42", "20E2", "A/C evaporator", "u8", "− 51 °F", "plausible", "66 → 71 °F. Lead: carlrice sheet."),
    ("47", "010A", "Charger current", "?", "÷ 10 − 50 A (per the sheet)", "lead only", "Not captured yet (module 47 wasn't polled)."),
    ("28", "2001", "Odometer (ABS)", "u24 BE", "÷ 10 km", "confirmed", "993,240 = 44:2001."),
    ("all", "200B", "Clock at the latest fault", "u32 BE", "minutes", "likely",
     "On module 28 it equals the U1487 snapshot's clock exactly (196,152)."),
    ("all", "2008", "Clock at this key-on", "u32 BE", "minutes", "likely", "6–14 minutes below 1008 on every module read."),
    ("40", "2023", "Build record", "ASCII + BCD date", "", "likely",
     "Holds the VIN's serial digits, a 1234567890 placeholder, then 13 09 05: Sep 5, 2013."),
    ("28 / 30", "1004", "12 V", "u16 BE", "mV", "likely", "14,460 and 14,353 with the DC-DC running."),
    ("40", "200B", "Clock at an event", "u32 BE", "minutes", "hypothesis",
     "Module 40's 200B = its clock 6.1 years ago (≈ Oct 2020, when 28 and 48 were programmed)."),
]

signals_json = [{"module": m, "did": d, "name": nm, "format": f, "scale": sc, "confidence": c, "evidence": e}
                for m, d, nm, f, sc, c, e in SIGNALS]


# -- raw inventory: every DID that answered, first/last value over the charge log ----------
def inventory() -> list[dict]:
    out = []
    for m in ("44", "42", "40"):
        for did in sorted(rows[0].get(m, {})):
            first, last, uniq = first_last(m, did)
            out.append({"module": m, "did": did.upper(), "bytes": len(first or "") // 2, "distinct": uniq,
                        "first": first, "last": last, "survey": survey_raw.get(int(m, 16), {}).get(did)})
    return out


INV = inventory()
for addr in sorted(survey_raw):
    if f"{addr:02x}" in ("44", "42", "40"):
        continue
    for did, h in sorted(survey_raw[addr].items()):
        if not did.startswith("f1"):
            INV.append({"module": f"{addr:02x}", "did": did.upper(), "bytes": len(h) // 2, "distinct": 1,
                        "first": h, "last": h, "survey": h})

# -- cycle count ---------------------------------------------------------------------------------
ah_in, ah_out = session["ah_in_life"], session["ah_out_life"]
efc = (ah_in / battery.NOMINAL_AH[1], ah_in / battery.NOMINAL_AH[0])
hist_first = [int(rows[0]["44"]["a005"][i:i + 4], 16) for i in range(0, 32, 4)]
hist_last = [int(rows[-1]["44"]["a005"][i:i + 4], 16) for i in range(0, 32, 4)]

# -- faults with dates -------------------------------------------------------------------------
for row in fault_rows:
    for f in row["faults"]:
        if row["ecu"] == "40" and f["clock"]:
            f["date_estimate"] = date_from_clock40(f["clock"]).isoformat()
            f["date_basis"] = "module 40 clock (runs continuously)"
        elif row["ecu"] == "44" and f["clock"]:
            mins = (clock44_now - f["clock"]) / bms_duty
            f["date_estimate"] = (T44 - dt.timedelta(minutes=mins)).date().isoformat()
            f["date_basis"] = f"battery controller clock at its average {100 * bms_duty:.1f} % duty: rough"
        elif f["clock"] and clock(int(row["ecu"], 16)):
            now, t = clock(int(row["ecu"], 16))
            back = now - f["clock"]  # running minutes before that read
            f["running_minutes_before_read"] = back
            if back < 24 * 60:
                f["date_estimate"] = "2026-11-07"
                f["date_basis"] = (f"{back} running minutes before the {t:%H:%M} read: during our own session "
                                   "today (≈ 14:00, when the protocol probe tried other CAN speeds). Likely caused by us.")

events = mods.history(modules)

# -- JSON ------------------------------------------------------------------------------------------
DOSSIER = {
    "generated": dt.datetime.now().isoformat(timespec="minutes"),
    "car": {**CAR, "vin": VIN, "vin_modules": sorted(f"{a:02X}" for a in vins),
            "vin_agree": len(set(vins.values())) <= 1, "odometer_mi": round(ODO_MI),
            "odometer_km": ODO_KM, "built_estimate": built_from_clock.date().isoformat()},
    "owner_reports": NOTES.get("owner_reports", []),
    "bus": {"adapter": "OBDLink EX (STN2232 v5.10.3), USB, 115200 baud", "can": "500 kbit/s, 29-bit",
            "request_id": "18DA<module>F1", "reply_id": "18DAF1<module>", "generic_obd": "none (0100 → NO DATA)",
            "elm_setup": "ATE0 ATL0 ATS0 ATH1 ATAT1 ATSP7 ATCP18 ATSH DA<tt>F1 ATFCSH 18DA<tt>F1 ATFCSD300000 ATFCSM1",
            "notes": ["Module 1F is slow between frames: ATST19 (100 ms) truncates its replies; use ≥ 200 ms.",
                      "Plug the adapter into the car first, then USB, or the FT-X port can wedge."]},
    "modules": {f"{a:02X}": {**m, "role": (mods.ROLES[a].name if a in mods.ROLES else None),
                             "role_evidence": (mods.ROLES[a].evidence if a in mods.ROLES else None),
                             "programmed": mods.programmed(m),
                             "clock_1008_min": int(survey_raw.get(a, {}).get("1008", "0") or "0", 16) or None}
                for a, m in sorted(modules.items())},
    "signals": signals_json,
    "battery": {
        "capacity_ah": session["capacity_ah"], "soh_about_pct": battery.soh_about(session["capacity_ah"]),
        "soh_range_pct": [round(x, 1) for x in battery.soh_range(session["capacity_ah"])],
        "nominal_ah_assumed": list(battery.NOMINAL_AH),
        "steady_charge": session["steady"], "ah_counted_whole_log": session["ah_counted"],
        "usable_kwh_estimate": round(report.range_estimate(session)["kwh"], 1),
        "range_mi_estimate": [round(report.range_estimate(session)["lo"]), round(report.range_estimate(session)["hi"])],
        "resistance": {k: v for k, v in session["resistance"].items()},
        "temps_c": [session["temp_min"], session["temp_max"]],
        "cells_at_rest_v": session["cells_at_rest"], "offset_rest_mv": session["offset_rest_mv"],
        "offset_charging_mv": session["offset_load_mv"],
        "lifetime_ah_in": ah_in, "lifetime_ah_out": ah_out,
        "equivalent_full_cycles": [round(efc[0]), round(efc[1])],
        "time_histogram_a005": {"first": hist_first, "last": hist_last, "status": "hypothesis"},
        "dash_readings": dash_rows,
    },
    "faults": fault_rows,
    "history": events,
    "recalls": [{"campaign": r.get("NHTSACampaignNumber"), "date": r.get("ReportReceivedDate"),
                 "component": r.get("Component"), "summary": r.get("Summary"),
                 "trace_in_car": report.recall_trace(r, modules)} for r in RECALLS],
    "inventory": INV,
    "files": sorted(p.name for p in LOGS.glob("20261107-*")),
}


# -- HTML ---------------------------------------------------------------------------------------------
def table(head, body, cls="t") -> str:
    h = "".join(f"<th>{esc(x)}</th>" for x in head)
    b = "".join("<tr>" + "".join(f"<td>{c}</td>" for c in r) + "</tr>" for r in body)
    return f'<table class="{cls}"><tr>{h}</tr>{b}</table>'


def sec(id_, title, *parts) -> str:
    return f'<section id="{id_}"><h2>{esc(title)}</h2>{"".join(parts)}</section>'


found = report.findings(session, modules, RECALLS, fault_recs)
toc = [("summary", "Summary"), ("car", "Car and owner"), ("bus", "Bus and adapter"), ("modules", "Modules"),
       ("signals", "Decoded signals"), ("battery", "Battery"), ("cells", "Cells"), ("cycles", "Cycles and use"),
       ("faults", "Fault codes"), ("history", "History and recalls"), ("open", "Open questions"),
       ("tests", "Next tests"), ("method", "Method and rules"), ("inventory", "Raw inventory"),
       ("sources", "Sources"), ("json", "Machine-readable")]

s = session
st_ = s["steady"]
cells = s["cells_at_rest"]
low5 = {k + 1 for k in sorted(range(97), key=lambda k: cells[k])[:5]}
res = s["resistance"]

car_rows = [("VIN", f"<code>{esc(VIN)}</code> (in modules {', '.join(DOSSIER['car']['vin_modules'])}, all agree)"),
            ("Model", "2013 Fiat 500e (first generation, US), orange"),
            ("Odometer", f"{ODO_MI:,.0f} mi ({ODO_KM:,.1f} km): 44:2001 and 28:2001 (÷ 10 km) and 42:204C (km) agree; "
                         "dash 61,720. 42:2001 reads 146 mi higher (995,640): its own distance count, not the odometer"),
            ("Built", f"≈ {fmt_date(built_from_clock.date().isoformat())} first power-on (module 40's clock); "
                      + (f"{fmt_date(DOOR40_DATE.isoformat())} in module 40's DID 2023 (BCD date after the VIN serial); "
                         if DOOR40_DATE else "") + "last factory programming Aug 2013"),
            ("Owner reports", "<br>".join(esc(x) for x in DOSSIER["owner_reports"]))]

module_rows = []
for a, m in sorted(modules.items()):
    fp = m["fingerprints"]
    fps = "<br>".join(f"{k}: {fp[k]['date']}" + (f" by {esc(fp[k]['by'])}" if fp[k]["by"] else "") for k in fp)
    module_rows.append([f"<b>{a:02X}</b>", esc(mods.ROLES[a].name) + f"<small>{esc(mods.ROLES[a].evidence)}</small>"
                        if a in mods.ROLES else "<i>not identified</i>",
                        esc(m.get("part") or "–"), esc(m.get("serial") or "–"),
                        esc(" / ".join(x for x in (m.get("hw"), m.get("sw")) if x) or "–"),
                        esc(" · ".join(m["versions"]) or "–"), fps or "–",
                        esc(", ".join(m["notes"])) + (" VIN" if m.get("vin") else "")])

sig_rows = [[esc(m), f"<code>{esc(d)}</code>", esc(nm), esc(f), esc(sc),
             f'<span class="conf {c.split()[0]}">{esc(c)}</span>', esc(e)] for m, d, nm, f, sc, c, e in SIGNALS]

cell_rows = [[str(k + 1), f"{cells[k]:.3f}", f"{s['offset_rest_mv'][k]:+.1f}", f"{s['offset_load_mv'][k]:+.1f}",
              f"{res['cells_mohm'][k]:.2f}"] for k in range(97)]

fault_html = []
for row in fault_rows:
    a = int(row["ecu"], 16)
    name = mods.ROLES[a].name if a in mods.ROLES else f"Module {row['ecu'].upper()}"
    for f in row["faults"]:
        fault_html.append([esc(name), f"<b>{esc(f['code'])}</b><small>{esc(f.get('meaning') or 'manufacturer code')}</small>",
                           ("Active now" if f["active"] else "Stored") + f"<small>{esc(', '.join(f['status_text']))}</small>",
                           str(f["records"]), esc(f.get("date_estimate") or "–") +
                           (f"<small>{esc(f.get('date_basis'))}</small>" if f.get("date_basis") else "")])
monitor_html = "".join(
    f"<li><b>{r['ecu'].upper()}</b>: {r['monitored']} monitored codes, none failed"
    + ("" if r["answered"] else f" (did not answer: {esc(r['why'])})") + "</li>" for r in fault_rows)
raw_fault_rows = []
for r in fault_recs:
    for c in r.get("codes", []):
        raw_fault_rows.append([r["ecu"].upper(), c["code"], f"{c['status']:02X}", esc(", ".join(c["status_text"])),
                               f"<code>{esc(c.get('snapshot') or c.get('snapshot_nrc') or '')}</code>",
                               f"<code>{esc(c.get('extended') or c.get('extended_nrc') or '')}</code>"])

inv_rows = [[r["module"], f"<code>{r['did']}</code>", str(r["bytes"]), str(r["distinct"]),
             f"<code>{esc((r['first'] or '')[:64])}</code>", f"<code>{esc((r['last'] or '')[:64])}</code>"] for r in INV]

series = s["series"]
html = f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>500e Findings Dossier</title>
<link rel="preconnect" href="https://fonts.googleapis.com"><link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Archivo:wdth,wght@62..125,100..900&display=swap" rel="stylesheet">
<style>{report.CSS.replace("CARHEX", esc(CAR.get("hex", "#E2571E")))}
.toc{{columns:3 200px;font-size:14px;margin:14px 0 0;padding:0;list-style:none}}.toc a{{color:var(--accent);text-decoration:none}}
.conf{{font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:.05em}}
.conf.confirmed{{color:var(--good)}}.conf.likely{{color:var(--accent)}}.conf.partial{{color:var(--accent)}}
.conf.hypothesis{{color:var(--warn)}}.conf.unknown{{color:var(--ink-3)}}
.kv td:first-child{{width:150px;color:var(--ink-2)}}code{{font-size:12px;word-break:break-all}}
.scroll{{max-height:520px;overflow:auto;border-bottom:1px solid var(--rule)}}
.callout{{border-left:3px solid var(--car);background:var(--paper-2);padding:10px 14px;margin:14px 0;max-width:80ch}}
</style></head><body>
<header class="mast"><span class="swatch"></span><div><h1>2013 Fiat 500e · findings</h1>
<p class="sub">Everything read from this car so far · {esc(dt.date.today().isoformat())} · read-only diagnostics</p></div></header>
<main>
<p class="verdict">The battery holds {s['capacity_ah']:.1f} Ah, about {battery.soh_about(s['capacity_ah'])}% of new. The
pack was most likely replaced around Dec 31, 2019. Six stored faults in four modules.</p>
<p class="lede">For people and agents picking this up: every finding with its evidence and confidence, the raw
values behind it, and what's still open. A JSON copy of all of it is embedded at the end
(<code>#dossier-data</code>). Private: contains the VIN.</p>
<ul class="toc">{"".join(f'<li><a href="#{a}">{esc(b)}</a></li>' for a, b in toc)}</ul>

{sec("summary", "Summary", report._summary(found))}

{sec("car", "Car and owner", table(["", ""], [[esc(a), b] for a, b in car_rows], "t kv"))}

{sec("bus", "Bus and adapter", table(["", ""], [[esc(k), esc(v if isinstance(v, str) else "; ".join(v))]
                                                for k, v in DOSSIER["bus"].items()], "t kv"),
     "<p>Modules answering TesterPresent: 1F 28 30 40 42 43 44 45 47 48 60 (sweep of all 256 29-bit targets). "
     "11-bit IDs 700–7F7: nothing. Generic OBD (mode 01) on any CAN variant: nothing.</p>")}

{sec("modules", "Modules", "<p>Identification from UDS F180–F19F. Programming dates are F183/F184/F185 read as "
     "[01] tester ID + YYMMDD in BCD (the only reading with no impossible dates).</p>",
     '<div class="scroll">' + table(["Module", "Role", "Part", "Serial", "Hardware / software", "Versions",
                                     "Programmed", "Notes"], module_rows) + "</div>")}

{sec("signals", "Decoded signals", "<p>Each decoded from our own captures. Leads from open-source projects (OVMS, "
     "OBDb) are named where used; a lead only counts once our data confirms it.</p>",
     '<div class="scroll">' + table(["Module", "DID", "What", "Format", "Scale", "Confidence", "Evidence"], sig_rows) + "</div>")}

{sec("battery", "Battery",
     f'''<div class="callout"><b>Capacity {s['capacity_ah']:.1f} Ah, about {battery.soh_about(s['capacity_ah'])}% of new
({battery.soh_range(s['capacity_ah'])[0]:.0f}–{battery.soh_range(s['capacity_ah'])[1]:.0f}% over new-pack ratings of
{battery.NOMINAL_AH[0]:.0f}–{battery.NOMINAL_AH[1]:.0f} Ah).</b> Four independent lines agree: the stored figure (two
modules); Ah moved per % of charge ({st_['ah_per_100']} Ah per 100 %); the charger ceiling (≤ 6 kW, +16 % in 23 min
→ ≤ 14.5 kWh for 0–100 %); and the owner's real range (≈ 60 % of the dash's 93 mi ≈ 56 mi).</div>''',
     table(["Measure", "Value"], [
         ["Steady charge window", f"{st_['from_min']}–{st_['to_min']} min: {st_['current_a']} A, {st_['power_kw']} kW into the pack"],
         ["Ah / kWh over that window", f"{st_['ah']} Ah, {st_['kwh']} kWh for +{st_['dsoc']} % → {st_['ah_per_100']} Ah, {st_['kwh_per_100']} kWh per 100 %"],
         ["Controller's counter vs measured current", f"{st_['counter_agrees_pct']:+.1f} %"],
         ["Whole log", f"{s['minutes']} min, {s['soc_start']:.1f} → {s['soc_end']:.1f} % true; {s['ah_counted']} Ah counted net"],
         ["Usable (dash 0–100 %)", f"≈ {DOSSIER['battery']['usable_kwh_estimate']} kWh → {DOSSIER['battery']['range_mi_estimate'][0]}–{DOSSIER['battery']['range_mi_estimate'][1]} mi at 3.5–4.5 mi/kWh (EPA new: 87 mi)"],
         ["Resistance", f"median {res['median_mohm']} mΩ/cell (sd {res['sd_mohm']}), pack {res['pack_mohm']} mΩ, from {res['pulses']} charging pulses"],
         ["Temperatures", f"{s['temp_min']}–{s['temp_max']} °C across 18 sensors, max spread {s['temp_spread_max']} °C"],
         ["Dash window", "dash % = 1.1195 × true − 12.26; dash 0 % ≈ 11 % true"]]),
     "<h3>Dash readings against the controller</h3>",
     table(["Time", "Dash %", "Dash range", "True % (A602)", "Predicted dash", "Note"],
           [[d["time"], str(d["dash_pct"]), f"{d['range_mi']} mi", n(d["true_pct"], 1), n(d["predicted_dash"], 1), esc(d["note"])]
            for d in dash_rows]),
     '<div class="pair"><figure><figcaption>True charge, % over minutes of the logged charge</figcaption>'
     + report.line_chart([{"pts": [(p["min"], p["soc"]) for p in series]}]) + '</figure><figure><figcaption>Current, A</figcaption>'
     + report.line_chart([{"pts": [(p["min"], p["i"]) for p in series], "cls": "b"}], zero=True) + "</figure></div>")}

{sec("cells", "Cells",
     f"<p>At rest ({dt.datetime.fromtimestamp(s['cells_at_rest_t']):%H:%M}): {min(cells):.3f}–{max(cells):.3f} V, spread "
     f"{1000 * (max(cells) - min(cells)):.0f} mV. Stronger shading = further below average; dots = lowest five. No cell sags "
     "under load: the lowest cells are low by the same amount at rest and while charging (a balance offset).</p>",
     report.cell_map(cells, low5),
     "<h3>Offset from the average, at rest (bars) and charging (dots)</h3>", report.bar_chart(s["offset_rest_mv"], s["offset_load_mv"], unit=" mV"),
     "<h3>Resistance per cell</h3>", report.bar_chart(res["cells_mohm"], unit=" mΩ", center=res["median_mohm"]),
     '<details><summary>All 97 cells as a table</summary><div class="scroll">'
     + table(["Cell", "At rest V", "Offset at rest mV", "Offset charging mV", "Resistance mΩ"], cell_rows) + "</div></details>")}

{sec("cycles", "Cycles and use",
     f"""<p><b>No confirmed cycle counter.</b> What the car does give: lifetime charge in {ah_in:,.1f} Ah and out
{ah_out:,.1f} Ah since the controller started (A615/A616). That's <b>{efc[0]:.0f}–{efc[1]:.0f} equivalent full
cycles</b> of a new pack ({battery.NOMINAL_AH[0]:.0f}–{battery.NOMINAL_AH[1]:.0f} Ah), about
{ah_in * 97 * 3.75 / 1e6:.1f} MWh, or 24–31 thousand miles at 3.5–4.5 mi/kWh: about half of what 61,717 mi would
take, consistent with a fresh start around the 2019–2020 replacement (≈ 33k mi then).</p>
<p>A028 holds four counters (2,539 / 5,521 / 36 / 1,913) that did not move across a plug-in and full charge:
not charge counts, meaning unknown. The controller's clock (1008) shows {clock44_now:,} running minutes
({clock44_now / 1440:.0f} days) since it was programmed: awake {100 * bms_duty:.1f} % of the time.</p>
<p><b>Time histogram (hypothesis):</b> A005's 8 bins grow ≈ 1 count per 5 minutes of running time; bin 7 grew
while cells were ≥ 4.0 V and bin 6 at ≈ 3.98 V. If the bins are cell-voltage bands, the pack spent
{100 * hist_last[7] / sum(hist_last):.0f} % of its running time in the top band. Bins now:
{", ".join(f"{v:,}" for v in hist_last)}.</p>""")}

{sec("faults", "Fault codes",
     "<p>UDS 0x19 02 FF on every module (read-only; nothing cleared). A module lists every code it monitors; a code "
     "counts as a fault only with a failed / pending / confirmed / failed-since-clear bit. Dates: module 40's clock "
     "runs continuously, so its snapshots date its faults; the battery controller's clock only runs while awake.</p>",
     table(["Module", "Code", "State", "Records", "When (estimate)"], fault_html),
     f"<p>The battery controller's P1E25 snapshot holds A601 = 01, A602 = F2 (96.8 % true charge), A603 = 40, A604 = 41: "
     "it was set near full charge. Meaning of P1E25 (manufacturer code) not confirmed.</p>",
     f"<details><summary>Modules and monitored codes</summary><ul>{monitor_html}</ul></details>",
     '<details><summary>Every record, raw snapshot and extended data</summary><div class="scroll">'
     + table(["Module", "Code", "Status", "Bits", "Snapshot (19 04)", "Extended (19 06)"], raw_fault_rows) + "</div></details>")}

{sec("history", "History and recalls",
     '<ol class="tl">' + "".join(f'<li><time>{fmt_date(e["date"])}</time><span>{esc(e["role"])} {esc(e["what"])}'
                                 f'{" — by " + esc(e["by"]) if e.get("by") else ""}'
                                 f'{" (" + esc(", ".join(e["notes"])) + ")" if e["notes"] else ""}</span></li>' for e in events) + "</ol>",
     """<p>Reading: the battery controller (BPCM) sits inside the pack and isn't sold separately, so a service BPCM
programmed 2019-12-31 (blank serial, placeholder numbers, later part 05186015AM vs the 2013's 68259779AB) means a
replacement pack, installed around Feb 2020 (tester 53751, the same that programmed 28 and 48 in Oct 2020). New or
remanufactured: not in the data. At ~60 % after ~6.8 years and ~28k miles it fits a remanufactured or used pack
better than a new one.</p>""",
     table(["Campaign", "Component", "Summary", "Trace in this car"],
           [[esc(r["campaign"]), esc(r["component"]), esc(r["summary"]), esc(r["trace_in_car"] or "not visible")]
            for r in DOSSIER["recalls"]]))}

{sec("open", "Open questions", """<ul>
<li>New-pack rating: 60, 63 or 67 Ah? (A014 = 0x025A = 60.2 is a candidate.)</li>
<li>Was the 2020 pack new or remanufactured? Repair order or pack label.</li>
<li>What P1E25 means; the B-codes at module 40; U1487 at 28.</li>
<li>Is A005 a cell-voltage histogram? Which band edges?</li>
<li>A028's four counters; A018 (12 × u16, some slowly rising); A056 (18 values, 23–27 °C); A611 vs A012.</li>
<li>Module 42's 2001 (995,640) is 240 km above the odometer: what is it? 2002 = 941,590.</li>
<li>Role of modules 30, 47, 48, 60. (Module 47 is probably charging-related: a community sheet reads charger
current there, and it was reprogrammed with the 2020 pack.)</li>
<li>Were today's "failed this cycle" network codes (U0001 at 48, U1487 at 28) caused by our own scanning? A
community note says plugging devices into the 500e's OBD port can cause temporary errors. Re-read after a key cycle
without sweeping.</li>
<li>What another car's A029 says: a community sample's capacity bytes start 0x02.. (51–60 Ah), against our 0x017B
(37.9 Ah). A second car's full A029 would calibrate "new".</li>
<li>Why the battery sometimes keeps discharging at a standstill after ~30 min of hard highway driving. Three
candidates, each with its own signature: battery cooling running hard (high current while stopped, hot pack); the
charge gauge correcting itself (charge % falls while the Ah counters barely move); a weak cell (one cell sags far
below the rest at 100–250 A, which our 15 A charge couldn't show).</li>
</ul>""")}

{sec("tests", "Next tests at the car", """<ol>
<li><b>Highway test</b> (the owner's symptom): <code>uv run e500 record</code> through ~30 min of hard highway
driving and 10–15 min stopped afterwards, Enter at the moment it happens. Core values every ~2 s, all cells every
~10 s, everything else every few minutes.</li>
<li><b>Low-end capacity</b>: log a drive from full to ≈ 20 % (or a charge from low): ∫current vs A602 at the steep end
of the curve, and weak cells show there.</li>
<li><b>Charge counter</b>: read A028/A018/A005 before plugging in and after unplugging, to find what counts charges.</li>
<li><b>Survey --deep</b>: sweep pages 01–04, 10, 20, A0–A6 on every module (≈ 10 min).</li>
<li>Fault codes again after a key cycle, before any sweeping: what's active vs history, and whether our own
traffic set U0001/U1487.</li>
<li><b>Passive listen</b> (adapter silent, nothing sent): 60 s of broadcast frames on CAN-C. Leads: 0848A046
charge level, 0C50A049 charger volts/amps, 025CA049 wall current, 0C10A040 range (km).</li>
<li><b>Second bus (CAN-B, 50 k on OBD pins 1/9)</b> needs a pin-swap ("yellow") adapter: tire pressures
(module A1, DIDs 40A1–40A4, kPa ÷ 10), cluster, body and its fault codes.</li>
<li>Module 47's DID 010A (charger current) and module 42's 2096/203F/204F/2090 (motor rpm, accelerator, speed,
torque) while driving.</li></ol>""")}

{sec("method", "Method and rules", """<ul>
<li>Read-only: only UDS 0x22, 0x3E, 0x19 (01/02/04/06/0A) and OBD mode 01, enforced in code before anything reaches
the adapter. Never 0x14 (clear codes), 0x2E, 0x31, 0x27, 0x34+, 0x11.</li>
<li>Clean-room: decodes from our own captures; open-source projects (OVMS MIT, OBDb CC BY-SA) used only as leads.</li>
<li>Tool: e500 (Python, ~/dev/fiat500e). <code>e500 survey</code>, <code>e500 faults</code>, <code>e500 ui</code>
(live, /report export), <code>e500 report</code>, <code>scripts/charge_log.py</code>, this file from
<code>scripts/dossier.py</code>.</li></ul>""",
     "<details><summary>Recordings</summary><ul>" + "".join(f"<li><code>{esc(x)}</code></li>" for x in DOSSIER["files"]) + "</ul></details>")}

{sec("inventory", "Raw inventory", f"<p>Every data identifier that answered on modules 44, 42 and 40 during the charge log "
     f"({len(INV)} DIDs): size, distinct values over 185 passes, first and last value (hex, first 32 bytes).</p>",
     '<div class="scroll">' + table(["Module", "DID", "Bytes", "Distinct", "First", "Last"], inv_rows) + "</div>")}

{sec("sources", "Sources", """<ul>
<li>OVMS v3 vehicle_fiat500e (MIT): https://github.com/openvehicles/Open-Vehicle-Monitoring-System-3</li>
<li>OBDb FIAT-500e (CC BY-SA 4.0, second generation): https://github.com/OBDb/FIAT-500e</li>
<li>NHTSA recalls API, by make/model/year: https://api.nhtsa.gov/recalls/recallsByVehicle?make=FIAT&amp;model=500E&amp;modelYear=2013</li>
<li>BPCM inside the pack; dealers replace the pack: fiat500owners.com threads 156275, 157553</li>
<li>BPCM part 05186015AM: fiat500owners.com thread 155312</li>
<li>Pack: 97 Samsung SDI cells, Bosch pack (Green Car Congress 2012-12; evspecifications.com)</li>
<li>carlrice/500e-can-bus (no license) and its linked public sheet "Fiat 500e CAN bus info WIP": first-generation
CAN-C/CAN-B signals, request-only DIDs at 42/44/47/A1. Used as leads; the confirmations above are ours.
https://github.com/carlrice/500e-can-bus</li></ul>""")}

{sec("json", "Machine-readable", "<p>The same findings as JSON, in <code>&lt;script id=\"dossier-data\" "
     "type=\"application/json\"&gt;</code> in this file.</p>")}
</main>
<footer><p>Generated {esc(DOSSIER['generated'])} by e500 scripts/dossier.py from recordings only. Independent of Fiat, FCA and Stellantis.</p></footer>
<script id="dossier-data" type="application/json">{json.dumps(DOSSIER, default=str).replace("</", "<\\/")}</script>
</body></html>"""

out = DATA / f"dossier-{dt.date.today():%Y%m%d}.html"
out.write_text(html)
print(f"{out}  ({len(html) // 1024} KB, {len(INV)} DIDs, {sum(len(r['faults']) for r in fault_rows)} faults)")
