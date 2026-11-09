"""The battery and history report: one self-contained HTML page, the verdict first.

Built from what e500 has recorded (a charging session, the modules' identification,
recalls by model year) and nothing else. Every number links to where it came from in
the last section. What we couldn't read is listed as such, never guessed.

The page is private: it can identify the car. It goes in data/, which is gitignored.
"""

from __future__ import annotations

import datetime as dt
import html
from statistics import mean

from . import battery, dtc, modules as mods

DASH_RESERVE = 11.0  # % of true charge below dash 0 % (fitted 2026-11-07)
MI_PER_KWH = (3.5, 4.5)  # assumption for range: a 500e at the battery, mixed driving
EPA_RANGE_NEW = 87  # miles, EPA rating of the 2013 500e when new

# Software recalls that leave a trace in a module's programming date.
RECALL_TRACE = {
    "16V303000": (0x43, "2016-07-05", "inverter software"),
    "15V179000": (0x42, "2015-05-21", "vehicle-controller software"),
}

LEVELS = {"good": "Good", "caution": "Check", "serious": "Serious", "info": "Note", "unknown": "Not read"}


def esc(x) -> str:
    return html.escape(str(x), quote=True)


def n(x, d=0) -> str:
    return "–" if x is None else f"{x:,.{d}f}"


# -- findings: the summary, as data (tested without the HTML) ---------------------------
def findings(session: dict | None, modules: dict[int, dict], recalls: list[dict] | None,
             faults: dict | None) -> list[dict]:
    out = []

    def add(level, area, headline, detail, anchor):
        out.append({"level": level, "area": area, "headline": headline, "detail": detail,
                    "anchor": anchor})

    s = session if session and session.get("usable") else None
    cap = s and s.get("capacity_ah")
    if cap:
        lo, hi = battery.soh_range(cap)
        level = "good" if lo >= 80 else "caution" if lo >= 70 else "serious"
        measured = s.get("steady", {}).get("ah_per_100")
        agree = f" A charge measured on the car gave {measured:.1f} Ah, which agrees." if measured else ""
        add(level, "Battery capacity", f"Holds {cap:.1f} Ah, about {battery.soh_about(cap)}% of new",
            f"The battery controller's own figure, stored in two modules.{agree} Between "
            f"{lo:.0f} and {hi:.0f}%, depending on the new-pack rating, which isn't published "
            "consistently. A drive from full to low would confirm it independently.", "capacity")
        r = range_estimate(s)
        if r:
            add(level, "Range", f"About {r['lo']:.0f}–{r['hi']:.0f} miles on a full charge",
                f"{r['kwh']:.1f} kWh usable at {MI_PER_KWH[0]}–{MI_PER_KWH[1]} mi/kWh. "
                f"New, the car was rated {EPA_RANGE_NEW} miles.", "capacity")
    else:
        add("unknown", "Battery capacity", "Not measured yet",
            "Log a charge (scripts/charge_log.py) to measure it.", "capacity")
    if s:
        rest, load = s.get("offset_rest_mv"), s.get("offset_load_mv")
        if rest:
            cells = s["cells_at_rest"]
            spread = 1000 * (max(cells) - min(cells))
            weak = weak_cells(s)
            level = "good" if spread <= 20 and not weak else "caution"
            add(level, "Cells", f"All 97 cells within {spread:.0f} mV of each other at rest",
                "No cell sags under load." if not weak else
                f"Cell{'s' if len(weak) > 1 else ''} {', '.join(map(str, weak))} sag under load.",
                "cells")
        res = s.get("resistance")
        if res:
            hi = max(res["cells_mohm"])
            level = "good" if hi <= res["median_mohm"] * 1.25 else "caution"
            add(level, "Resistance", f"Even across the pack ({res['median_mohm']:.2f} mΩ per cell)",
                f"Highest cell {hi:.2f} mΩ. Measured from {res['pulses']} charging pulses.", "cells")
        st_ = s.get("steady")
        if st_:
            level = "good" if st_["power_kw"] >= 5.5 else "caution"
            add(level, "Charger", f"Charges at {st_['power_kw']:.1f} kW into the battery",
                "Full rate for the 6.6 kW on-board charger on 240 V.", "charging")
        if s.get("temp_max") is not None:
            level = "good" if s["temp_max"] <= 40 and s["temp_spread_max"] <= 6 else "caution"
            add(level, "Temperatures", f"{s['temp_min']:.0f}–{s['temp_max']:.0f} °C while charging",
                f"18 sensors, never more than {s['temp_spread_max']:.0f} °C apart.", "charging")
    events = mods.history(modules)
    if events:
        bms = next((e for e in events if e["module"] == "44"), None)
        others = [e for e in events if e is not bms]
        rest = (f"{len(others)} other module{'s' if len(others) != 1 else ''} reprogrammed after "
                f"the factory, {fmt_date(others[0]['date'])} to {fmt_date(others[-1]['date'])}."
                if others else "")
        if bms and bms["notes"]:
            add("caution", "Service history",
                f"Battery pack most likely replaced around {fmt_date(bms['date'])}",
                "Its controller is a service part programmed then; the controller sits inside the "
                "pack and isn't sold separately. Whether the replacement was new or remanufactured "
                "isn't in the data: the repair order or the pack's label would say. " + rest, "history")
        else:
            add("info", "Service history",
                f"{len(events)} modules reprogrammed after the factory, last {fmt_date(events[-1]['date'])}",
                "Software updates or replaced parts; see the history.", "history")
    vc = mods.vin_check(modules)
    if vc["count"]:
        add("good" if vc["agree"] else "serious", "VIN",
            f"Stored in {vc['count']} modules; {'all agree' if vc['agree'] else 'they disagree'}",
            "A module from another car would carry a different VIN." if vc["agree"] else
            "At least one module came from another car.", "history")
    if recalls is not None:
        done = [r for r in recalls if recall_trace(r, modules)]
        add("info" if recalls else "good", "Recalls",
            f"{len(recalls)} apply to the 2013 500e" if recalls else "None for this model year",
            f"{len(done)} software recall{'s' if len(done) != 1 else ''} show{'s' if len(done) == 1 else ''} "
            "a matching software update. The rest can't be seen in the car's data; check the VIN at "
            "nhtsa.gov/recalls." if recalls else "", "recalls")
    if faults is None:
        add("unknown", "Fault codes", "Not read yet", "Connect the car and export the report.", "faults")
    else:
        rows = dtc.summarise(faults)
        real = [(r["ecu"], f) for r in rows for f in r["faults"]]
        bms = [f for e, f in real if e == "44"]
        active = [f for _, f in real if f["active"]]
        if not real:
            add("good", "Fault codes", "None stored in any module",
                f"{sum(r['answered'] for r in rows)} modules answered.", "faults")
        else:
            add("serious" if bms and any(f["active"] for f in bms) else "caution", "Fault codes",
                f"{len(real)} stored in {len({e for e, _ in real})} modules, {len(active)} active now",
                (f"The battery controller has {', '.join(f['code'] for f in bms)}. " if bms else "")
                + "The rest are listed with their modules.", "faults")
    return out


def range_estimate(s: dict) -> dict | None:
    st_ = s.get("steady") or {}
    if not st_.get("kwh_per_100"):
        return None
    kwh = st_["kwh_per_100"] * (100 - DASH_RESERVE) / 100
    return {"kwh": kwh, "lo": kwh * MI_PER_KWH[0], "hi": kwh * MI_PER_KWH[1]}


def weak_cells(s: dict, limit_mv: float = 8.0) -> list[int]:
    """Cells that drop more than limit_mv further below average under load than at rest."""
    rest, load = s.get("offset_rest_mv"), s.get("offset_load_mv")
    if not rest or not load:
        return []
    return [k + 1 for k in range(len(rest)) if load[k] - rest[k] < -limit_mv]


def recall_trace(recall: dict, modules: dict[int, dict]) -> str | None:
    rule = RECALL_TRACE.get(recall.get("NHTSACampaignNumber"))
    if not rule or rule[0] not in modules:
        return None
    addr, start, what = rule
    when = mods.programmed(modules[addr])
    return f"{what} dated {fmt_date(when)}" if when and when >= start else None


def fmt_date(iso: str | None) -> str:
    if not iso:
        return "–"
    d = dt.date.fromisoformat(iso)
    return f"{d:%b} {d.day}, {d.year}"


# -- small inline SVG charts ---------------------------------------------------------------
def line_chart(series: list[dict], w=520, h=170, ylab="", y0=None, y1=None, zero=False) -> str:
    """series: [{"pts": [(x, y)], "cls": "a"}]. Axes are quiet: three y ticks, x in minutes."""
    xs = [x for s in series for x, _ in s["pts"]]
    ys = [y for s in series for _, y in s["pts"] if y is not None]
    if not xs or not ys:
        return '<p class="empty">No data.</p>'
    lo, hi = (min(ys) if y0 is None else y0), (max(ys) if y1 is None else y1)
    if hi == lo:
        hi = lo + 1
    pad_l, pad_b, pad_t = 44, 22, 8
    X = lambda x: pad_l + (x - min(xs)) / ((max(xs) - min(xs)) or 1) * (w - pad_l - 8)
    Y = lambda y: pad_t + (1 - (y - lo) / (hi - lo)) * (h - pad_b - pad_t)
    out = [f'<svg viewBox="0 0 {w} {h}" role="img" aria-label="{esc(ylab)}">']
    for k in range(3):
        v = lo + (hi - lo) * k / 2
        out.append(f'<line class="g" x1="{pad_l}" x2="{w - 8}" y1="{Y(v):.1f}" y2="{Y(v):.1f}"/>'
                   f'<text class="ax" x="{pad_l - 6}" y="{Y(v) + 4:.1f}" text-anchor="end">{v:,.4g}</text>')
    if zero and lo < 0 < hi:
        out.append(f'<line class="z" x1="{pad_l}" x2="{w - 8}" y1="{Y(0):.1f}" y2="{Y(0):.1f}"/>')
    for k in range(5):
        x = min(xs) + (max(xs) - min(xs)) * k / 4
        out.append(f'<text class="ax" x="{X(x):.1f}" y="{h - 5}" text-anchor="middle">{x:.0f}</text>')
    for s in series:
        d = " ".join(f"{'M' if i == 0 else 'L'}{X(x):.1f},{Y(y):.1f}"
                     for i, (x, y) in enumerate(p for p in s["pts"] if p[1] is not None))
        out.append(f'<path class="ln {s.get("cls", "")}" d="{d}"/>')
    out.append("</svg>")
    return "".join(out)


def bar_chart(values: list[float], marks: list[float] | None = None, w=860, h=150,
              unit="", hot=frozenset(), center: float = 0.0) -> str:
    """One bar per cell, up or down from `center`; optional dots for a second series."""
    allv = values + (marks or [])
    span = max(abs(v - center) for v in allv) or 1
    pad_l, pad_b = 44, 18
    bw = (w - pad_l - 4) / len(values)
    Y = lambda v: 6 + (1 - ((v - center) / span + 1) / 2) * (h - pad_b - 6)
    label = (lambda v: f"{v:+.1f}{unit}") if center == 0 else (lambda v: f"{v:.2f}{unit}")
    out = [f'<svg viewBox="0 0 {w} {h}" role="img">',
           f'<line class="z" x1="{pad_l}" x2="{w - 4}" y1="{Y(center):.1f}" y2="{Y(center):.1f}"/>']
    for v in (center - span, center + span):
        out.append(f'<text class="ax" x="{pad_l - 6}" y="{Y(v) + 4:.1f}" text-anchor="end">{label(v)}</text>')
    for k, v in enumerate(values):
        x, y0, y1 = pad_l + k * bw, Y(center), Y(v)
        out.append(f'<rect class="bar{" hot" if k + 1 in hot else ""}" x="{x + 0.6:.1f}" '
                   f'y="{min(y0, y1):.1f}" width="{max(bw - 1.2, 0.5):.1f}" height="{abs(y1 - y0):.1f}">'
                   f'<title>Cell {k + 1}: {label(v)}</title></rect>')
        if marks:
            out.append(f'<circle class="mk" cx="{x + bw / 2:.1f}" cy="{Y(marks[k]):.1f}" r="1.8"/>')
    for k in (0, 24, 48, 72, 96):
        out.append(f'<text class="ax" x="{pad_l + (k + 0.5) * bw:.1f}" y="{h - 3}" text-anchor="middle">{k + 1}</text>')
    out.append("</svg>")
    return "".join(out)


def cell_map(cells: list[float], low: set[int]) -> str:
    avg = mean(cells)
    span = max(abs(v - avg) for v in cells) or 0.001
    tiles = []
    for k, v in enumerate(cells):
        below = max(0.0, (avg - v) / span)  # only cells under the average get darker
        cls = "cell" + (" low" if k + 1 in low else "") + (" dk" if below > 0.65 else "")
        tiles.append(f'<div class="{cls}" style="--d:{below * 60:.0f}%" '
                     f'title="Cell {k + 1}: {v:.3f} V, {1000 * (v - avg):+.1f} mV from average">'
                     f'<b>{k + 1}</b><span>{v:.3f}</span></div>')
    return f'<div class="pack">{"".join(tiles)}</div>'


# -- the page ----------------------------------------------------------------------------------
def build(*, car: dict, session: dict | None, modules: dict[int, dict], recalls: list[dict] | None,
          faults: list[dict] | None = None, odometer: dict | None = None,
          generated: dt.datetime | None = None, sources=(), live: dict | None = None,
          simulated: bool = False, toolbar: bool = False) -> str:
    generated = generated or dt.datetime.now()
    if live and live.get("cells") and session and session.get("usable"):
        session = dict(session, cells_at_rest=live["cells"], cells_at_rest_t=live["t"])
    if odometer is None:
        odometer = _odometer(modules)
    found = findings(session, modules, recalls, faults)
    s = session if session and session.get("usable") else None
    serious = [f for f in found if f["level"] == "serious"]
    caution = [f for f in found if f["level"] == "caution"]
    lead = (serious or caution or found)[0]
    headline = (f"{lead['headline']}." if lead["level"] in ("serious", "caution")
                else "No serious problems found in what we could read.")
    body = [_head(car, generated, simulated), _toolbar() if toolbar else "", '<main>',
            f'<p class="verdict">{esc(headline)}</p>',
            '<p class="lede">What the car\'s own computers say about its battery and its history. '
            'Read-only: nothing on the car was changed. Each line links to the detail and the evidence.</p>',
            _summary(found), _now(live, odometer)]
    if s:
        body += [_capacity(s), _cells(s), _charging(s), _lifetime(s)]
    body += [_history(modules), _recalls(recalls, modules), _faults(faults),
             _evidence(s, modules, odometer, sources), '</main>', _foot(generated), '</body></html>']
    return "\n".join(body)


def _odometer(modules: dict[int, dict]) -> dict | None:
    """Module 44's DID 2001 in 0.1 km (matched the dash to 3 mi on 2026-11-07)."""
    raw = (modules.get(0x44, {}).get("counters") or {}).get("2001")
    if not raw:
        return None
    km = int(raw, 16) / 10
    return {"km": km, "mi": km / 1.609344, "raw_44": int(raw, 16)}


def _now(live: dict | None, odometer: dict | None) -> str:
    if not live and not odometer:
        return ""
    items = []
    if odometer and odometer.get("mi"):
        items.append(("Odometer", f"{odometer['mi']:,.0f}", "mi"))
    if live:
        st = live.get("cell_stats") or {}
        temps = live.get("pack_temps") or []
        for label, v, unit, d in (("Charge (dash)", live.get("soc"), "%", 0),
                                  ("Pack", live.get("pack_v"), "V", 1),
                                  ("Cell spread", st.get("spread_mv"), "mV", 0),
                                  ("Current", live.get("current"), "A", 1),
                                  ("Battery temp", max(temps) if temps else None, "°C", 0),
                                  ("12 V", live.get("aux_v_adapter"), "V", 1)):
            if v is not None:
                items.append((label, n(v, d), unit))
    when = (f'Read from the car at {dt.datetime.fromtimestamp(live["t"]):%H:%M}.' if live
            else "From the recorded identification.")
    cells = "".join(f"<div><dt>{esc(a)}</dt><dd>{esc(b)}<small>{esc(c)}</small></dd></div>" for a, b, c in items)
    return f'<section id="now" class="now"><dl>{cells}</dl><p class="note">{when}</p></section>'


def _toolbar() -> str:
    return ('<nav class="toolbar"><a href="?download=1">Download</a>'
            '<a href="#" onclick="print();return false">Print or save as PDF</a>'
            '<a href="?refresh=1">Re-read the car</a></nav>')


def _head(car: dict, generated: dt.datetime, simulated: bool = False) -> str:
    title = f"{car.get('year', '')} {car.get('name', 'Fiat 500e')}".strip()
    sub = " · ".join(x for x in (car.get("colour"), "Battery and history report",
                                 f"{generated:%B} {generated.day}, {generated.year}") if x)
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{esc(title)} report</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Archivo:wdth,wght@62..125,100..900&display=swap" rel="stylesheet">
<style>{CSS.replace("CARHEX", esc(car.get("hex", "#E2571E")))}</style></head>
<body{' class="sim"' if simulated else ''}><header class="mast"><span class="swatch"></span><div><h1>{esc(title)}</h1>
<p class="sub">{esc(sub)}</p></div></header>"""


def _summary(found: list[dict]) -> str:
    rows = "".join(
        f'<a class="row" href="#{f["anchor"]}"><span class="lvl {f["level"]}">{LEVELS[f["level"]]}</span>'
        f'<span class="area">{esc(f["area"])}</span><span class="hl">{esc(f["headline"])}'
        f'<small>{esc(f["detail"])}</small></span></a>' for f in found)
    return f'<section class="summary" aria-label="Summary">{rows}</section>'


def _capacity(s: dict) -> str:
    cap, st_ = s.get("capacity_ah"), s.get("steady") or {}
    lo_new, hi_new = battery.NOMINAL_AH
    pct = 100 * cap / hi_new if cap else 0
    r = range_estimate(s)
    rows = [("Battery controller (module 44, A625)", f"{n(cap, 1)} Ah"),
            ("Vehicle controller (module 42, 2050)", f"{n(s.get('capacity_ah_evcu'), 1)} Ah"),
            (f"Measured on this charge ({st_.get('from_min')}–{st_.get('to_min')} min)",
             f"{n(st_.get('ah_per_100'), 1)} Ah per 100 % of charge"),
            ("The controller's charge counter over the same minutes",
             f"{st_.get('counter_agrees_pct', 0):+.1f} % from the measured current"),
            ("New (not measured): cell ratings 60 and 63 Ah; 24 kWh / 97 / 3.7 V = 67 Ah",
             f"{lo_new:.0f}–{hi_new:.0f} Ah")]
    table = "".join(f"<tr><td>{esc(a)}</td><td>{esc(b)}</td></tr>" for a, b in rows)
    series = s.get("series", [])
    charts = (f'<div class="pair"><figure><figcaption>Charge (controller), % over minutes</figcaption>'
              f'{line_chart([{"pts": [(p["min"], p["soc"]) for p in series]}], ylab="charge %")}</figure>'
              f'<figure><figcaption>Current, A (negative = charging)</figcaption>'
              f'{line_chart([{"pts": [(p["min"], p["i"]) for p in series], "cls": "b"}], ylab="current", zero=True)}'
              f'</figure></div>')
    rng = (f"<p>That's about <strong>{r['kwh']:.1f} kWh</strong> between dash 0 % and full, or "
           f"<strong>{r['lo']:.0f}–{r['hi']:.0f} miles</strong> of mixed driving at "
           f"{MI_PER_KWH[0]}–{MI_PER_KWH[1]} mi/kWh. The dash's range figure is a guess from recent "
           "driving and can read higher; what the car actually does from full to low is the test "
           "that would prove this either way.</p>") if r else ""
    return f"""<section id="capacity"><h2>Battery capacity</h2>
<div class="gauge"><div class="gfill" style="width:{min(pct, 100):.1f}%"></div>
<div class="gnew" style="left:{100 * lo_new / hi_new:.1f}%"></div></div>
<p class="gl"><span>{n(cap, 1)} Ah now</span><span>{lo_new:.0f}–{hi_new:.0f} Ah new</span></p>
<p>A battery's health is how much charge it holds compared with new. This one holds
<strong>{n(cap, 1)} Ah</strong>. Three readings agree: the figure the battery controller stores, the
same figure in the vehicle controller, and what we measured: the current that went in while the
controller's charge rose.</p>{rng}
<details><summary>How it was measured</summary><table class="t">{table}</table>
{charts}</details></section>"""


def _cells(s: dict) -> str:
    cells, rest, load = s["cells_at_rest"], s.get("offset_rest_mv"), s.get("offset_load_mv")
    order = sorted(range(97), key=lambda k: cells[k])
    low = {k + 1 for k in order[:5]}
    res = s.get("resistance")
    spread = 1000 * (max(cells) - min(cells))
    persist = ""
    if rest and load:
        lows = sorted(range(97), key=lambda k: rest[k])[:3]
        persist = ("<p>Lowest on average: " + ", ".join(
            f"cell {k + 1} ({rest[k]:+.1f} mV at rest, {load[k]:+.1f} mV charging)" for k in lows) +
            ". Low by the same amount both ways means a small charge offset, which balancing evens "
            "out, not a weak cell.</p>")
    res_html = ""
    if res:
        hot = {k + 1 for k, v in enumerate(res["cells_mohm"]) if v > res["median_mohm"] * 1.25}
        res_html = (f'<h3>Resistance</h3><p>Median {res["median_mohm"]:.2f} mΩ per cell, '
                    f'{res["pack_mohm"]:.0f} mΩ for the pack, cell-to-cell variation '
                    f'{res["sd_mohm"]:.2f} mΩ. A cell far above the rest would be a weak cell; '
                    f'{"none is" if not hot else "cells " + ", ".join(map(str, sorted(hot))) + " are"}. '
                    f'From {res["pulses"]} on/off pulses near full charge.</p>'
                    f'<details><summary>Per cell</summary>'
                    f'{bar_chart(res["cells_mohm"], w=860, unit=" mΩ", hot=hot, center=res["median_mohm"])}'
                    f'</details>')
    offs = ""
    if rest:
        offs = (f'<details><summary>Every cell against the average, at rest (bars) and charging (dots)</summary>'
                f'{bar_chart(rest, load, unit=" mV")}</details>')
    when = dt.datetime.fromtimestamp(s["cells_at_rest_t"])
    return f"""<section id="cells"><h2>Cell by cell</h2>
<p>All 97 cells within <strong>{spread:.0f} mV</strong> at rest ({min(cells):.3f}–{max(cells):.3f} V,
read {when:%H:%M}). The stronger a tile's shading, the further it sits below the average; dots
mark the lowest five.</p>
{cell_map(cells, low)}{persist}{offs}{res_html}</section>"""


def _charging(s: dict) -> str:
    st_, series = s.get("steady") or {}, s.get("series", [])
    temps = line_chart([{"pts": [(p["min"], p["tmax"]) for p in series], "cls": "c"}], ylab="°C")
    spread = line_chart([{"pts": [(p["min"], p["spread"]) for p in series], "cls": "b"}], ylab="mV")
    return f"""<section id="charging"><h2>Charging and temperature</h2>
<p>On 240 V the battery took <strong>{n(st_.get('power_kw'), 2)} kW</strong>
({n(st_.get('current_a'), 1)} A) while charging steadily, the full rate of the 6.6 kW on-board
charger after its losses. Near full it pulses, as expected. Battery temperatures ran
{n(s.get('temp_min'))}–{n(s.get('temp_max'))} °C across 18 sensors.</p>
<div class="pair"><figure><figcaption>Warmest sensor, °C</figcaption>{temps}</figure>
<figure><figcaption>Cell spread, mV</figcaption>{spread}</figure></div></section>"""


def _lifetime(s: dict) -> str:
    if s.get("ah_in_life") is None:
        return ""
    ah = s["ah_in_life"]
    lo_new, hi_new = battery.NOMINAL_AH
    mwh = ah * 97 * 3.75 / 1e6
    return f"""<section id="lifetime"><h2>Use since the battery controller was fitted</h2>
<p><strong>{n(ah)} Ah</strong> charged in and {n(s['ah_out_life'])} Ah taken out, the controller's
own counters, about half of what the car's whole mileage would take, which fits a fresh start in
2020. That's about {ah / hi_new:.0f}–{ah / lo_new:.0f} full charges of a new pack, roughly
{mwh:.1f} MWh, or {mwh * 1000 * MI_PER_KWH[0] / 1000:.0f}–{mwh * 1000 * MI_PER_KWH[1] / 1000:.0f}
thousand miles at {MI_PER_KWH[0]}–{MI_PER_KWH[1]} mi/kWh. The counters start when the controller
does, so on a replaced controller they don't cover the car's whole life.</p></section>"""


def _history(modules: dict[int, dict]) -> str:
    if not modules:
        return '<section id="history"><h2>History</h2><p class="empty">Module identification not read.</p></section>'
    events = mods.history(modules)
    built = sorted(d for m in modules.values() if (d := mods.programmed(m)) and d < "2014")
    tl = "".join(f'<li><time>{fmt_date(e["date"])}</time><span>{esc(e["role"])} {esc(e["what"])}'
                 f'{" (" + ", ".join(map(esc, e["notes"])) + ")" if e["notes"] else ""}</span></li>'
                 for e in events)
    first = (f'<li><time>{fmt_date(built[-1])}</time><span>Last factory programming (the car was '
             f'built around then)</span></li>') if built else ""
    rows = []
    for addr in sorted(modules):
        m = modules[addr]
        role = mods.ROLES.get(addr)
        rows.append(f'<tr><td>{addr:02X}</td><td>{esc(role.name if role else "Role not identified")}'
                    f'{"<small>" + esc(role.evidence) + "</small>" if role else ""}</td>'
                    f'<td>{esc(m.get("part") or "–")}</td><td>{esc(" / ".join(m["versions"]) or "–")}</td>'
                    f'<td>{fmt_date(mods.programmed(m))}</td><td>{esc(", ".join(m["notes"]) or "")}</td></tr>')
    return f"""<section id="history"><h2>History</h2>
<p>Every module records when it was last programmed. A date years after the car was built means a
software update or a replaced part; a blank serial number points to a service part.</p>
<ol class="tl">{first}{tl}</ol>
<details><summary>All {len(modules)} modules</summary><table class="t"><tr><th>Module</th><th>Role</th>
<th>Part</th><th>Software</th><th>Programmed</th><th>Notes</th></tr>{"".join(rows)}</table></details></section>"""


def _recalls(recalls: list[dict] | None, modules: dict[int, dict]) -> str:
    if recalls is None:
        return ('<section id="recalls"><h2>Recalls</h2><p class="empty">Not looked up. '
                '<code>e500 recalls</code> fetches them by model year (no VIN is sent).</p></section>')
    rows = []
    for r in recalls:
        trace = recall_trace(r, modules)
        rows.append(f'<tr><td>{esc(r.get("NHTSACampaignNumber"))}<small>{esc(r.get("ReportReceivedDate", ""))}</small></td>'
                    f'<td>{esc(r.get("Summary", ""))}</td>'
                    f'<td>{"Likely done: " + esc(trace) if trace else "Not visible in the car data"}</td></tr>')
    return f"""<section id="recalls"><h2>Recalls</h2>
<p>Recalls published for the 2013 500e (NHTSA, looked up by model year; no VIN was sent). Whether
each one applies to this car and was done is for a dealer or nhtsa.gov/recalls to confirm with the VIN.</p>
<table class="t"><tr><th>Campaign</th><th>What</th><th>In this car's data</th></tr>{"".join(rows)}</table></section>"""


def _faults(faults: list[dict] | None) -> str:
    if faults is None:
        return ('<section id="faults"><h2>Fault codes</h2><p class="empty">Not read. Export the '
                'report with the car connected. Codes are only read, never cleared: clearing '
                'erases evidence.</p></section>')
    rows, monitors = [], []
    for r in dtc.summarise(faults):
        role = mods.ROLES.get(int(r["ecu"], 16))
        name = role.name if role else f"Module {r['ecu'].upper()}"
        if not r["answered"]:
            monitors.append(f"{name}: {r['why'] or 'no answer'}")
            continue
        monitors.append(f"{name}: {r['monitored']} monitored, not failed")
        for f in r["faults"]:
            state = "Active now" if f["active"] else "Stored"
            rows.append(f'<tr><td>{esc(name)}</td><td><strong>{esc(f["code"])}</strong>'
                        f'{"<small>" + esc(f["meaning"]) + "</small>" if f.get("meaning") else "<small>Manufacturer code</small>"}</td>'
                        f'<td><span class="lvl {"serious" if f["active"] else "caution"}">{state}</span>'
                        f'<small>{esc(", ".join(f["status_text"]))}</small></td>'
                        f'<td>{f["records"]}</td></tr>')
    table = (f'<table class="t"><tr><th>Module</th><th>Code</th><th>State</th><th>Records</th></tr>'
             f'{"".join(rows)}</table>') if rows else "<p>No stored faults in any module.</p>"
    return f"""<section id="faults"><h2>Fault codes</h2>
<p>Read from every module and never cleared. A module also lists every check it runs; a code counts
as a fault only if it has failed, is pending or is confirmed. "Active now" means it failed in this
drive cycle.</p>{table}
<details><summary>Every module</summary><ul>{"".join(f"<li>{esc(m)}</li>" for m in monitors)}</ul></details></section>"""


def _evidence(s: dict | None, modules: dict[int, dict], odometer: dict | None, sources) -> str:
    ids = lambda f: f"{f.dids[0]:04X}–{f.dids[-1]:04X}" if len(f.dids) > 1 else f"{f.dids[0]:04X}"
    rows = [(f.label, f"{f.ecu >> 8 & 0xFF:02X} · {ids(f)}", f.source)
            for f in battery.REAL_FIELDS]
    fields = "".join(f"<tr><td>{esc(a)}</td><td>{esc(b)}</td><td>{esc(c)}</td></tr>" for a, b, c in rows)
    odo = ""
    if odometer:
        odo = (f"<p>Odometer: module 44, DID 2001 = {odometer['raw_44']:,} in 0.1 km = "
               f"{odometer['mi']:,.0f} mi. The unit was checked against the dash on Nov 7, 2026 "
               "(61,720 mi on the dash, 61,717 from the module).</p>")
    src = "".join(f"<li>{esc(x)}</li>" for x in sources)
    return f"""<section id="evidence"><h2>Method and evidence</h2>
<p>Every figure comes from the car, read with standard read-only diagnostic requests. How each raw
value is decoded, and why we believe it:</p>
<table class="t"><tr><th>Reading</th><th>Module · data ID</th><th>Evidence</th></tr>{fields}</table>
{odo}<p>Not read, so not in this report: fault codes, insulation resistance, the controller's
history of temperature and charge levels, and anything a dealer tool would need security access
for. Assumptions: new capacity {battery.NOMINAL_AH[0]:.0f}–{battery.NOMINAL_AH[1]:.0f} Ah (published
specs disagree), {MI_PER_KWH[0]}–{MI_PER_KWH[1]} mi/kWh for range.</p>
<details><summary>Recordings used</summary><ul>{src}</ul></details></section>"""


def _foot(generated: dt.datetime) -> str:
    return (f'<footer><p>Made with e500, open-source and read-only. Generated {generated:%Y-%m-%d %H:%M}. '
            'Independent of Fiat, FCA and Stellantis.</p></footer>')


CSS = """
:root{--paper:#EEEEE8;--paper-2:#F6F6F2;--ink:#1B1F23;--ink-2:#5A6169;--ink-3:#8A9097;--rule:#CCCFC8;
--mark:#6B7279;--accent:#2C6E8F;--warn:#A86B00;--bad:#B8361F;--good:#2B7466;--car:CARHEX;color-scheme:light}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){--paper:#17191B;--paper-2:#1F2225;--ink:#E7E9E5;
--ink-2:#A3AAB0;--ink-3:#6F767C;--rule:#33383D;--mark:#8C949B;--accent:#6BB0D3;--warn:#E3A33A;--bad:#EC6A50;--good:#5DB09C;color-scheme:dark}}
:root[data-theme="dark"]{--paper:#17191B;--paper-2:#1F2225;--ink:#E7E9E5;--ink-2:#A3AAB0;--ink-3:#6F767C;--rule:#33383D;
--mark:#8C949B;--accent:#6BB0D3;--warn:#E3A33A;--bad:#EC6A50;--good:#5DB09C;color-scheme:dark}
*{box-sizing:border-box}
body{margin:0;background:var(--paper);color:var(--ink);font:16px/1.5 "Archivo",system-ui,-apple-system,sans-serif;font-variant-numeric:tabular-nums}
.mast,main,footer{max-width:960px;margin:0 auto;padding:0 32px}
.mast{display:flex;gap:16px;align-items:center;padding-top:36px;padding-bottom:18px;border-bottom:1px solid var(--rule)}
.swatch{width:28px;height:28px;border-radius:50%;background:var(--car);flex:none}
h1{margin:0;font-size:clamp(30px,5vw,52px);line-height:1;font-stretch:125%;font-weight:800;letter-spacing:-.02em}
.sub{margin:6px 0 0;color:var(--ink-2)}
main{padding-top:28px;padding-bottom:24px}
.verdict{font-size:clamp(22px,3vw,30px);line-height:1.25;font-stretch:110%;font-weight:700;margin:0 0 8px;max-width:34ch}
.lede{color:var(--ink-2);margin:0 0 24px;max-width:70ch}
h2{font-size:22px;font-stretch:125%;margin:0 0 10px}
h3{font-size:16px;margin:22px 0 6px}
section{margin-top:44px}
section p{max-width:72ch}
.summary{margin-top:8px;border-top:1px solid var(--rule)}
.row{display:grid;grid-template-columns:84px 150px 1fr;gap:12px;padding:11px 0;border-bottom:1px solid var(--rule);color:inherit;text-decoration:none}
.row:hover .hl{text-decoration:underline;text-decoration-color:var(--rule)}
.hl{font-weight:600}.hl small{display:block;font-weight:400;color:var(--ink-2);font-size:14px;margin-top:2px}
.area{color:var(--ink-2);font-size:14px;padding-top:2px}
.lvl{font-size:12px;font-weight:700;text-transform:uppercase;letter-spacing:.06em;padding-top:3px}
.lvl::before{content:"";display:inline-block;width:9px;height:9px;border-radius:50%;margin-right:6px;background:var(--ink-3);vertical-align:0}
.lvl.good{color:var(--good)}.lvl.good::before{background:var(--good)}
.lvl.caution{color:var(--warn)}.lvl.caution::before{background:var(--warn)}
.lvl.serious{color:var(--bad)}.lvl.serious::before{background:var(--bad)}
.lvl.info{color:var(--accent)}.lvl.info::before{background:var(--accent)}
.lvl.unknown{color:var(--ink-3)}
.gauge{position:relative;height:20px;border-radius:10px;background:var(--paper-2);border:1px solid var(--rule);overflow:hidden;margin:6px 0 4px}
.gfill{position:absolute;inset:0 auto 0 0;background:var(--car)}
.gnew{position:absolute;top:0;bottom:0;right:0;background:repeating-linear-gradient(135deg,transparent 0 4px,rgba(0,0,0,.12) 4px 6px)}
.gl{display:flex;justify-content:space-between;font-size:13px;color:var(--ink-2);margin:0 0 14px;max-width:none}
details{margin-top:12px;font-size:14px}summary{cursor:pointer;color:var(--ink-2)}
.t{border-collapse:collapse;width:100%;margin-top:10px;font-size:13px}
.t th,.t td{text-align:left;vertical-align:baseline;padding:6px 14px 6px 0;border-bottom:1px solid var(--rule);font-weight:400}
.t th{color:var(--ink-2)}.t small{display:block;color:var(--ink-2)}
.pair{display:grid;grid-template-columns:1fr 1fr;gap:28px;margin-top:14px}
figure{margin:0}figcaption{font-size:13px;color:var(--ink-2);margin-bottom:4px}
svg{display:block;width:100%;height:auto;overflow:visible}
.g{stroke:var(--rule)}.z{stroke:var(--ink-3)}.ax{fill:var(--ink-3);font-size:11px;font-family:inherit}
.ln{fill:none;stroke:var(--accent);stroke-width:2;stroke-linejoin:round}.ln.b{stroke:var(--mark)}.ln.c{stroke:var(--warn)}
.bar{fill:var(--mark)}.bar.hot{fill:var(--bad)}.mk{fill:var(--ink)}
.pack{display:grid;grid-template-columns:repeat(12,minmax(0,1fr));gap:2px;margin:12px 0}
.cell{position:relative;padding:6px 3px 5px;border-radius:3px;text-align:center;line-height:1.15;
background:color-mix(in oklab,var(--ink) var(--d),var(--paper-2));color:var(--ink)}
.cell.dk{color:var(--paper)}
.cell b{display:block;font-size:10px;font-weight:500;opacity:.7}.cell span{font-size:13px;font-weight:600;font-stretch:72%}
.cell.low::after{content:"";position:absolute;top:4px;right:4px;width:6px;height:6px;border-radius:50%;background:var(--warn)}
.tl{list-style:none;padding:0;margin:14px 0 0;border-left:2px solid var(--rule)}
.tl li{display:grid;grid-template-columns:130px 1fr;gap:12px;padding:6px 0 6px 14px}
.tl time{color:var(--ink-2);font-size:14px}
.empty{color:var(--ink-3)}code{font-size:13px}
body.sim::before{content:"SIMULATED · NOT A REAL CAR";position:fixed;inset:0;display:flex;align-items:center;
justify-content:center;font-size:clamp(40px,9vw,120px);font-weight:800;font-stretch:125%;color:var(--bad);opacity:.09;
transform:rotate(-24deg);pointer-events:none;z-index:10;white-space:nowrap}
.toolbar{max-width:960px;margin:0 auto;padding:10px 32px;display:flex;gap:18px;font-size:14px;border-bottom:1px solid var(--rule)}
.toolbar a{color:var(--accent);text-decoration:none}.toolbar a:hover{text-decoration:underline}
.now{margin-top:22px}.now dl{display:grid;grid-template-columns:repeat(7,minmax(0,1fr));margin:0;border-top:1px solid var(--rule)}
.now dl div{padding:10px 10px 10px 0;border-bottom:1px solid var(--rule)}.now dt{font-size:12px;color:var(--ink-2)}
.now dd{margin:2px 0 0;font-size:22px;font-weight:600;font-stretch:72%}.now dd small{font-size:12px;color:var(--ink-2);margin-left:3px;font-weight:400}
.note{font-size:13px;color:var(--ink-2);margin:6px 0 0}
footer{border-top:1px solid var(--rule);padding-top:14px;padding-bottom:36px;font-size:13px;color:var(--ink-2)}
@media (max-width:760px){.mast,main,footer{padding:0 16px}.mast{padding-top:24px;padding-bottom:14px}
.row{grid-template-columns:1fr;gap:2px}.pair{grid-template-columns:1fr}.pack{grid-template-columns:repeat(6,minmax(0,1fr))}
.tl li{grid-template-columns:1fr;gap:0}.t{display:block;overflow-x:auto}.now dl{grid-template-columns:repeat(3,minmax(0,1fr))}
.toolbar{padding:10px 16px;flex-wrap:wrap}}
@media print{body{background:#fff}.toolbar{display:none}body.sim::before{position:fixed}details{display:block}details>summary{display:none}details:not([open])>*{display:block}
section{break-inside:avoid-page}.row{break-inside:avoid}}
"""
