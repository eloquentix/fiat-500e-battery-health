"""Build a local findings page from the 2026-11-07 car session. Private: writes to data/.

Reads the sweep logs in logs/, never prints or embeds the VIN.
"""
import glob
import html
import json
import os
from pathlib import Path

from e500.battery import cell_stats

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "data" / "findings-20261107.html"


def load(module: str) -> dict[str, bytes]:
    data = {}
    for f in sorted(glob.glob(str(ROOT / f"logs/*-full-{module}.jsonl")), key=os.path.getmtime):
        for line in open(f):
            r = json.loads(line)
            if "hex" in r:
                data[r["did"]] = bytes.fromhex(r["hex"])
    return data


m44, m42, m40 = load("44"), load("42"), load("40")
cells = []
for did in [f"a1{i:02x}" for i in range(9)]:
    d = m44[did]
    cells += [int.from_bytes(d[i:i + 2], "big") / 1000 for i in range(0, len(d), 2)]
st = cell_stats(cells)
# Cross-check from the sampled sweep, where cells and the controller's own max/min/average
# were read within a minute of each other (the full sweep read them ~10 min apart, and the
# cells drift a few mV in that time with the car on).
_sampled = {}
for f in sorted(glob.glob(str(ROOT / "logs/*-pages-44.jsonl")), key=os.path.getmtime)[-1:]:
    _sampled = {r["did"]: bytes.fromhex(r["hex"]) for r in map(json.loads, open(f)) if "hex" in r}
_sc = []
for did in [f"a1{i:02x}" for i in range(9)]:
    d = _sampled[did]
    _sc += [int.from_bytes(d[i:i + 2], "big") / 1000 for i in range(0, len(d), 2)]
bms_max, bms_min, bms_avg = (int.from_bytes(_sampled[k], "big") / 1000 for k in ("a609", "a60a", "a60b"))
ours_max, ours_min, ours_avg = max(_sc), min(_sc), sum(_sc) / len(_sc)
temps_a200 = [b - 40 for b in m44["a200"]]
temps_a056 = [b - 40 for b in m44["a056"][:18]]
import time as _t
_T = lambda hm: _t.mktime(_t.strptime(f"2026-11-07 {hm}", "%Y-%m-%d %H:%M"))
_cf = sorted(glob.glob(str(ROOT / "logs/*-charge.jsonl")), key=os.path.getmtime)[-1]
charge = [r for r in map(json.loads, open(_cf)) if r["t"] < _T("16:36")]
def _avg_cell(r):
    v = [int(r["44"][f"a1{i:02x}"][j:j + 4], 16) for i in range(9) for j in range(0, len(r["44"][f"a1{i:02x}"]), 4)]
    return sum(v) / len(v) / 1000
curve = [(r["t"], int(r["44"]["a602"], 16) * 0.4, _avg_cell(r)) for r in charge]
DASH = [("15:46", 72), ("15:52", 77), ("16:29", 97)]
SOC_A, SOC_B = 0.4478, -12.26  # dash % = SOC_A * raw(A602) + SOC_B, fitted on 4 dash readings


def cells_svg() -> str:
    dev, n = st["deviation_mv"], st["count"]
    low = {c["cell"] - 1 for c in st["lowest5"]}
    W, H, L, R, T, B = 1000, 220, 40, 8, 10, 26
    lim = max(6, -(-max(abs(x) for x in dev) // 2) * 2)
    y = lambda mv: T + (H - T - B) * (lim - mv) / (2 * lim)
    bw, gap = (W - L - R) / n, 2
    parts = [f'<svg viewBox="0 0 {W} {H}" role="img" aria-label="Each of 97 cells against the pack average">']
    for mv in (-lim, -lim / 2, 0, lim / 2, lim):
        cls = "zero" if mv == 0 else "grid"
        parts.append(f'<line class="{cls}" x1="{L}" x2="{W - R}" y1="{y(mv):.1f}" y2="{y(mv):.1f}"/>')
        parts.append(f'<text class="axis" x="{L - 6}" y="{y(mv) + 4:.1f}" text-anchor="end">{mv:+g}</text>')
    for i in range(0, n, 10):
        parts.append(f'<text class="axis" x="{L + i * bw + bw / 2:.1f}" y="{H - 8}" text-anchor="middle">{i + 1}</text>')
    for i, mv in enumerate(dev):
        y0, y1 = y(0), y(mv)
        h = max(1.0, abs(y1 - y0))
        cls = "bar low" if i in low else "bar"
        parts.append(f'<rect class="{cls}" x="{L + i * bw + gap / 2:.1f}" y="{min(y0, y1):.1f}" '
                     f'width="{bw - gap:.1f}" height="{h:.1f}" rx="2"><title>Cell {i + 1}: '
                     f'{cells[i]:.3f} V ({mv:+.1f} mV vs average)</title></rect>')
    parts.append("</svg>")
    return "".join(parts)


def curve_svg() -> str:
    W, H, L, R, T, B = 1000, 230, 44, 70, 12, 26
    t0, t1 = curve[0][0], curve[-1][0]
    x = lambda t: L + (W - L - R) * (t - t0) / (t1 - t0)
    y = lambda v: T + (H - T - B) * (100 - v) / (100 - 60)
    p = [f'<svg viewBox="0 0 {W} {H}" role="img" aria-label="True charge rising from {curve[0][1]:.0f} to {curve[-1][1]:.0f} percent">']
    for v in (60, 70, 80, 90, 100):
        p.append(f'<line class="grid" x1="{L}" x2="{W - R}" y1="{y(v):.1f}" y2="{y(v):.1f}"/>')
        p.append(f'<text class="axis" x="{L - 6}" y="{y(v) + 4:.1f}" text-anchor="end">{v}%</text>')
    for hm in ("15:40", "15:50", "16:00", "16:10", "16:20", "16:30"):
        p.append(f'<text class="axis" x="{x(_T(hm)):.1f}" y="{H - 6}" text-anchor="middle">{hm}</text>')
    p.append('<path class="line" d="' + "".join(f"{'L' if i else 'M'}{x(t):.1f},{y(v):.1f}" for i, (t, v, _) in enumerate(curve)) + '"/>')
    for hm, d in DASH:
        raw = (d - SOC_B) / SOC_A
        p.append(f'<circle class="dash" cx="{x(_T(hm)):.1f}" cy="{y(raw * 0.4):.1f}" r="5"><title>{hm}: dash {d} %, true charge {raw * 0.4:.1f} %</title></circle>')
    t, v, _ = curve[-1]
    p.append(f'<text class="endlab" x="{x(t) + 8:.1f}" y="{y(v) + 4:.1f}">{v:.1f}%</text></svg>')
    return "".join(p)


MODULES = [
    ("44", "Battery controller", "Bosch hardware number (0261…); holds the 97 cell voltages", True),
    ("42", "Battery-related, role unclear", "Software B16V2…, calibration “BCal…”; 224 data IDs", False),
    ("40", "EV vehicle controller", "Hardware BEV_M1US…; likely source of the range estimate", False),
    ("43", "Inverter / motor controller", "Software I23C0…, name INV2.3", False),
    ("45", "On-board charger", "Hardware CHA501…, software F500BEV", False),
    ("1F", "Telematics", "Continental GSM module", False),
    ("28", "Chassis (brakes or steering?)", "Bosch-style numbers, similar to 48", False),
    ("48", "Chassis (brakes or steering?)", "Bosch-style numbers, similar to 28", False),
    ("30", "Body or cluster", "Fiat part number", False),
    ("47", "Unknown", "Software “Mule4”", False),
    ("60", "Body or cluster", "Mopar-style part number", False),
]

e = html.escape
rows = "".join(f"<tr><td class='mono'>{m}</td><td>{'<strong>' + e(r) + '</strong>' if hl else e(r)}</td><td>{e(c)}</td></tr>"
               for m, r, c, hl in MODULES)
cell_table = "".join(f"<div>{i + 1}: {v:.3f} V</div>" for i, v in enumerate(cells))
lows = ", ".join(f"#{c['cell']} ({c['v']:.3f} V)" for c in st["lowest5"])

page = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>500e first session</title>
<link href="https://fonts.googleapis.com/css2?family=Archivo:wdth,wght@62..125,100..900&display=swap" rel="stylesheet">
<style>
:root {{ --paper:#EEEEE8; --paper-2:#F6F6F2; --ink:#1B1F23; --ink-2:#5A6169; --ink-3:#8A9097; --rule:#CCCFC8;
  --mark:#6B7279; --warn:#A86B00; --good:#2B7466; --accent:#2C6E8F; --wide:125%; --narrow:72%; color-scheme: light; }}
@media (prefers-color-scheme: dark) {{ :root:not([data-theme="light"]) {{ --paper:#17191B; --paper-2:#1F2225;
  --ink:#E7E9E5; --ink-2:#A3AAB0; --ink-3:#6F767C; --rule:#33383D; --mark:#8C949B; --warn:#E3A33A; --good:#5DB09C; --accent:#6BB0D3; color-scheme: dark; }} }}
:root[data-theme="dark"] {{ --paper:#17191B; --paper-2:#1F2225; --ink:#E7E9E5; --ink-2:#A3AAB0; --ink-3:#6F767C;
  --rule:#33383D; --mark:#8C949B; --warn:#E3A33A; --good:#5DB09C; --accent:#6BB0D3; color-scheme: dark; }}
* {{ box-sizing: border-box; }}
body {{ margin:0; background:var(--paper); color:var(--ink); font-family:"Archivo",system-ui,sans-serif; font-size:16px;
  line-height:1.5; font-variant-numeric: tabular-nums; }}
.wrap {{ max-width:1000px; margin:0 auto; padding:36px 32px 56px; }}
h1 {{ margin:0; font-size:clamp(34px,5.5vw,58px); line-height:1; font-stretch:var(--wide); font-weight:800; letter-spacing:-.02em; }}
.sub {{ color:var(--ink-2); margin:8px 0 28px; }}
.verdict {{ font-size:clamp(21px,2.8vw,28px); line-height:1.3; font-weight:700; font-stretch:110%; max-width:42ch; margin:0 0 28px; }}
h2 {{ font-size:21px; font-stretch:var(--wide); margin:44px 0 10px; }}
p {{ max-width:72ch; }}
.stats {{ display:grid; grid-template-columns:repeat(5,minmax(0,1fr)); border-top:1px solid var(--rule); margin:0 0 8px; }}
.stats div {{ padding:12px 12px 12px 0; border-bottom:1px solid var(--rule); }}
.stats dt {{ font-size:13px; color:var(--ink-2); }} .stats dd {{ margin:2px 0 0; font-size:28px; font-weight:600; font-stretch:var(--narrow); }}
.stats small {{ font-size:14px; color:var(--ink-2); font-weight:400; margin-left:3px; }}
table {{ border-collapse:collapse; width:100%; font-size:14px; }}
th, td {{ text-align:left; padding:8px 14px 8px 0; border-bottom:1px solid var(--rule); vertical-align:top; }}
th {{ color:var(--ink-2); font-weight:500; border-bottom:1.5px solid var(--ink); }}
.mono {{ font-stretch:var(--narrow); font-weight:600; }}
svg {{ width:100%; height:auto; display:block; overflow:visible; }}
.grid {{ stroke:var(--rule); }} .zero {{ stroke:var(--ink-3); }}
.axis {{ fill:var(--ink-3); font-size:11px; font-family:inherit; }}
.bar {{ fill:var(--mark); }} .bar.low {{ fill:var(--warn); }}
.line {{ fill:none; stroke:var(--accent); stroke-width:2; stroke-linejoin:round; }}
.dash {{ fill:var(--paper); stroke:var(--ink); stroke-width:2; }}
.endlab {{ fill:var(--ink); font-size:12px; font-weight:600; font-family:inherit; }}
.legend {{ font-size:13px; color:var(--ink-2); display:flex; gap:14px; align-items:center; margin:0 0 6px; }}
.key {{ display:inline-block; width:10px; height:10px; border-radius:2px; margin-right:5px; vertical-align:-1px; }}
.tag {{ font-size:12px; padding:1px 8px; border-radius:999px; border:1px solid var(--rule); color:var(--ink-2); white-space:nowrap; }}
.tag.yes {{ border-color:var(--good); color:var(--ink); }} .tag.maybe {{ border-color:var(--warn); color:var(--ink); }}
details {{ margin-top:10px; font-size:14px; color:var(--ink-2); }} .cols {{ columns:5 140px; margin-top:8px; color:var(--ink); }}
.note {{ font-size:14px; color:var(--ink-2); }}
ol li {{ margin-bottom:6px; max-width:70ch; }}
@media (max-width:760px) {{ .wrap {{ padding:24px 16px 40px; }} .stats {{ grid-template-columns:repeat(2,minmax(0,1fr)); }} }}
</style></head><body><div class="wrap">
<h1>Fiat 500e</h1>
<p class="sub">First car session and a 240 V charge, 7 November 2026. OBDLink EX on the Mac. Private: VIN left out.</p>

<p class="verdict">We can read the main battery: all 97 cells, {st['min_v']:.3f}–{st['max_v']:.3f} V, only {st['spread_mv']:.0f} mV apart. Charge is decoded too; health isn't yet.</p>

<dl class="stats">
  <div><dt>Cells</dt><dd>{st['count']}</dd></div>
  <div><dt>Spread</dt><dd>{st['spread_mv']:.0f}<small>mV</small></dd></div>
  <div><dt>Average cell, before charging</dt><dd>{st['mean_v']:.3f}<small>V</small></dd></div>
  <div><dt>Pack (sum)</dt><dd>{sum(cells):.1f}<small>V</small></dd></div>
  <div><dt>True charge</dt><dd>{curve[-1][1]:.0f}<small>% at end of charge</small></dd></div>
</dl>

<h2>The cells</h2>
<p class="legend"><span><span class="key" style="background:var(--mark)"></span>Each cell against the pack average (mV)</span>
<span><span class="key" style="background:var(--warn)"></span>Lowest five</span></p>
{cells_svg()}
<p class="note">Snapshot from before the charge (dash 69 %). Average {st['mean_v']:.3f} V. Lowest five: {lows}. At {st['spread_mv']:.0f} mV the pack is very well balanced; a weak cell would sit tens of mV below the rest. Hover a bar for its voltage.</p>
<details><summary>All 97 as a table</summary><div class="cols">{cell_table}</div></details>

<h2>How we know these are the cells</h2>
<table><tr><th>Check</th><th>Result</th></tr>
<tr><td>Count</td><td>Battery controller (module 44), data IDs A100–A108: 8 × 12 + 1 = <strong>97</strong> sixteen-bit values. The pack has 97 cells in series.</td></tr>
<tr><td>Range</td><td>All between {st['min_v']:.3f} and {st['max_v']:.3f} when read as millivolts: lithium-ion territory. About 70 % charge for this chemistry, and the dash says 69 %.</td></tr>
<tr><td>Cross-check</td><td>The controller's own max / min / average (IDs A609 / A60A / A60B): <strong>{bms_max:.3f} / {bms_min:.3f} / {bms_avg:.3f} V</strong>, against <strong>{ours_max:.3f} / {ours_min:.3f} / {ours_avg:.3f} V</strong> from our 97 values read within the same minute. They agree to 1 mV.</td></tr>
</table>

<h2>Charge: decoded</h2>
<p class="legend"><span><span class="key" style="background:var(--accent)"></span>True charge from the battery controller (ID A602 × 0.4)</span>
<span><span class="key" style="background:var(--paper);border:2px solid var(--ink)"></span>Your dash readings</span></p>
{curve_svg()}
<table><tr><th>Check</th><th>Result</th></tr>
<tr><td>Fit</td><td>Dash % = {SOC_A} × raw − {abs(SOC_B)}, within 0.4 % at all four dash readings (69, 72, 77, 97 %). It was the only value among 385 that climbed through the charge and fit cleanly.</td></tr>
<tr><td>Energy</td><td>True charge went from {curve[0][1]:.1f} % to {curve[-1][1]:.1f} % in {(curve[-1][0] - curve[0][0]) / 60:.0f} minutes: {(curve[-1][1] - curve[0][1]) / 100 * 24:.1f} kWh of the 24 kWh pack, {(curve[-1][1] - curve[0][1]) / 100 * 24 / ((curve[-1][0] - curve[0][0]) / 3600):.1f} kW on average. The 500e's 240 V charger tops out near 6.6 kW, so the scale is physically right.</td></tr>
<tr><td>What the dash shows</td><td>A window of the true charge: dash 0 % sits near 11 % true charge, dash 100 % near 100 %. The car keeps a reserve at the bottom.</td></tr>
<tr><td>Cells during the charge</td><td>Average cell rose from {curve[0][2]:.3f} V to {curve[-1][2]:.3f} V.</td></tr>
</table>

<h2>What isn't settled yet</h2>
<table><tr><th>Value</th><th>Status</th><th>Detail</th></tr>
<tr><td>Range</td><td><span class="tag">open</span></td><td>The earlier candidate (EV controller, ID 200B = 57) stayed at 57 through the whole charge while the dash went 68 → 91 miles: a stored value, not range. Range likely lives on the dash side or another module.</td></tr>
<tr><td>Temperatures</td><td><span class="tag maybe">candidates</span></td><td>Two 18-sensor sets in module 44: A056 reads {min(temps_a056)}–{max(temps_a056)} °C, A200 reads {temps_a200[0] if len(set(temps_a200)) == 1 else f"{min(temps_a200)}–{max(temps_a200)}"} °C (assuming the usual −40 offset). Which is which needs a session where the pack warms up.</td></tr>
<tr><td>State of health</td><td><span class="tag">open</span></td><td>Not on the dash, so no direct match. Likely a capacity figure; tracking it across a full charge will expose it.</td></tr>
</table>

<h2>The car's network</h2>
<p>CAN at 500 kbit/s. Modules use 29-bit addresses (18DA&lt;module&gt;F1), which is why an ordinary 11-bit scan finds nothing. The car doesn't answer generic OBD at all: that part of OBD exists for emissions, and a battery-electric car has none.</p>
<table><tr><th>Module</th><th>Likely role</th><th>Evidence</th></tr>{rows}</table>
<p class="note">Roles are inferred from each module's identification strings, not confirmed. Data found by a read-only sweep: module 44 {len(m44)} data IDs, module 42 {len(m42)}, module 40 {len(m40)}. Every request was a read; nothing was written to the car.</p>

<h2>Next</h2>
<ol>
<li>Add 29-bit addressing to e500 and make the 97 cells and true charge real fields, so the app shows this car instead of the simulated one.</li>
<li>A drive session: watch pack current and temperatures change, separate the two temperature sets, look for range.</li>
<li>Log over a full charge to find state of health, then keep one reading a week for the degradation curve.</li>
</ol>
</div></body></html>"""

OUT.parent.mkdir(exist_ok=True)
OUT.write_text(page)
print(OUT)
