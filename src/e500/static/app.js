"use strict";

const $ = (s) => document.querySelector(s);
const NS = "http://www.w3.org/2000/svg";
const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[c]);
const fmt = (n, d = 0) => Number(n).toLocaleString("en-US", { minimumFractionDigits: d, maximumFractionDigits: d });

let status = null;

async function api(path) {
  const r = await fetch(path);
  if (!r.ok) throw new Error((await r.json().catch(() => ({}))).error || r.statusText);
  return r.json();
}

function svg(tag, attrs, parent) {
  const e = document.createElementNS(NS, tag);
  for (const k in attrs) e.setAttribute(k, attrs[k]);
  if (parent) parent.appendChild(e);
  return e;
}

// ---------------------------------------------------------------- tooltip
const tip = $("#tip");
function showTip(ev, html) {
  tip.innerHTML = html;
  tip.hidden = false;
  const x = Math.min(ev.clientX + 14, innerWidth - tip.offsetWidth - 8);
  tip.style.left = `${x}px`;
  tip.style.top = `${ev.clientY - tip.offsetHeight - 12}px`;
}
const hideTip = () => { tip.hidden = true; };

// ---------------------------------------------------------------- header
let linkNow = "";
function setLink(state, html) {
  if (linkNow === state + html) return;
  linkNow = state + html;
  const n = $("#link-state");
  n.dataset.state = state;
  n.innerHTML = `<span class="lamp" aria-hidden="true"></span><span>${html}</span>`;
}
function renderLink() {
  if (status.mode === "mock") setLink("sim", "<strong>Simulated car.</strong> No adapter needed.");
  else setLink("ok", `Adapter on <strong>${esc((status.port || "").replace("/dev/", ""))}</strong>`);
}

function renderCar() {
  const car = status.car || {};
  if (car.hex) document.documentElement.style.setProperty("--car", car.hex);
  $("#car-name").textContent = car.name || "Fiat 500e";
  $("#car-sub").textContent = [car.year, car.colour, "battery"].filter(Boolean).join(" · ");
  document.title = `${car.name || "500e"} battery`;
}

// ---------------------------------------------------------------- charge + stats
function renderCharge(s) {
  $("#soc-dash").textContent = s.soc == null ? "–" : fmt(s.soc);
  $("#soc-true").textContent = s.soc_true == null ? "" : `${fmt(s.soc_true, 1)} % true charge`;
  $("#charge-fill").style.width = `${s.soc_true ?? 0}%`;
  $("#charge-bar").setAttribute("aria-label", `True charge ${fmt(s.soc_true ?? 0, 1)} %`);
}

function renderStats(s) {
  const st = s.cell_stats || {};
  const items = [
    ["Pack", s.pack_v, "V", 1],
    ["Cell spread", st.spread_mv, "mV", 0],
    ["Lowest cell", st.min_v, `V · #${st.min_cell ?? "–"}`, 3],
    ["Highest cell", st.max_v, `V · #${st.max_cell ?? "–"}`, 3],
    ["Capacity", s.capacity_ah, "Ah", 1],
    ["Current", s.current, s.current < -0.5 ? "A · charging" : "A", 1],
    ["Battery temp", s.pack_temps ? Math.max(...s.pack_temps) : null, "°C max", 0],
    ["12 V", s.aux_v_adapter, "V", 1],
  ];
  $("#stats").innerHTML = items.map(([label, v, unit, dec]) =>
    `<div><dt>${label}</dt><dd>${v == null ? "–" : fmt(v, dec)}<small>${v == null ? "not read" : unit}</small></dd></div>`).join("");
}

// ---------------------------------------------------------------- cell by cell
// The pack map follows the controller's own layout: 8 rows of 12, then 1. A cell
// gets darker the further it sits below the pack average; at or above average it
// stays light. One hue, so the eye goes straight to the low ones.
let packCount = 0;
function renderPack(s) {
  const st = s.cell_stats, box = $("#pack");
  if (!st) { box.innerHTML = `<p class="empty">No cell voltages yet.</p>`; packCount = 0; return; }
  const dev = st.deviation_mv, low = new Set(st.lowest5.map((c) => c.cell - 1));
  const depth = Math.max(3, ...dev.map((d) => -d));
  if (packCount !== dev.length) {  // build the tiles once, then only update them
    box.replaceChildren(...dev.map((_, i) => {
      const el = document.createElement("div");
      el.className = "cell";
      el.innerHTML = `<b>${i + 1}</b><span></span>`;
      el.addEventListener("pointerleave", hideTip);
      return el;
    }));
    packCount = dev.length;
  }
  [...box.children].forEach((el, i) => {
    const d = Math.max(0, -dev[i]) / depth;  // 0 at/above average, 1 at the lowest
    el.style.background = `color-mix(in oklab, var(--ink) ${Math.round(d * 62)}%, var(--paper-2))`;
    el.style.color = d > 0.55 ? "var(--paper)" : "var(--ink)";
    el.classList.toggle("low", low.has(i));
    el.querySelector("span").textContent = fmt(s.cells[i], 3);
    el.onpointermove = (ev) => showTip(ev, `Cell ${i + 1}<br><strong>${fmt(s.cells[i], 3)} V</strong> · `
      + `${dev[i] > 0 ? "+" : ""}${fmt(dev[i], 1)} mV vs average`);
  });
  const lows = st.lowest5.map((c) => `#${c.cell} (${fmt(c.v, 3)} V)`).join(", ");
  $("#cells-note").textContent = `${st.count} cells, average ${fmt(st.mean_v, 3)} V, `
    + `${fmt(st.spread_mv)} mV from lowest to highest. Lowest five: ${lows}.`;
}

// Each cell as a bar from the pack average: the same data as the map, read as shape.
function renderDeviation(s) {
  const st = s.cell_stats, box = $("#cells-chart");
  if (!st) { box.innerHTML = ""; return; }
  const dev = st.deviation_mv, n = dev.length, low = new Set(st.lowest5.map((c) => c.cell - 1));
  const W = 1000, H = 200, L = 40, R = 8, T = 10, B = 26;
  const lim = Math.max(4, Math.ceil(Math.max(...dev.map(Math.abs)) / 2) * 2);
  const y = (mv) => T + (H - T - B) * (lim - mv) / (2 * lim);
  const bw = (W - L - R) / n, gap = 2;
  const g = document.createElementNS(NS, "svg");
  g.setAttribute("viewBox", `0 0 ${W} ${H}`);
  g.setAttribute("role", "img");
  g.setAttribute("aria-label", `Cell voltages against the average: spread ${st.spread_mv} mV`);
  const grid = svg("g", { class: "grid" }, g), axis = svg("g", { class: "axis" }, g);
  for (const mv of [-lim, -lim / 2, 0, lim / 2, lim]) {
    svg("line", { x1: L, x2: W - R, y1: y(mv), y2: y(mv), class: mv === 0 ? "zero" : "" }, grid);
    svg("text", { x: L - 6, y: y(mv) + 4, "text-anchor": "end" }, axis).textContent = mv > 0 ? `+${mv}` : mv;
  }
  for (let i = 0; i < n; i += 12) {
    svg("text", { x: L + i * bw + bw / 2, y: H - 8, "text-anchor": "middle" }, axis).textContent = i + 1;
  }
  dev.forEach((mv, i) => {
    const y0 = y(0), y1 = y(mv);
    svg("rect", { x: L + i * bw + gap / 2, y: Math.min(y0, y1), width: bw - gap,
                  height: Math.max(1, Math.abs(y1 - y0)), rx: 2, class: `bar${low.has(i) ? " low" : ""}` }, g);
    const hit = svg("rect", { x: L + i * bw, y: T, width: bw, height: H - T - B, class: "bar hit" }, g);
    hit.addEventListener("pointermove", (ev) => showTip(ev,
      `Cell ${i + 1}<br><strong>${fmt(s.cells[i], 3)} V</strong> · ${mv > 0 ? "+" : ""}${fmt(mv, 1)} mV`));
    hit.addEventListener("pointerleave", hideTip);
  });
  box.replaceChildren(g);
  $("#cells-table").innerHTML = `<div class="cols">${s.cells.map((v, i) =>
    `<div>${i + 1}: ${fmt(v, 3)} V</div>`).join("")}</div>`;
}

// ---------------------------------------------------------------- history
function lineChart(box, rows, key, unit, dec, emptyText) {
  const pts = rows.filter((r) => r[key] != null);
  const cap = box.querySelector("figcaption").outerHTML;
  if (pts.length < 2) {
    box.innerHTML = `${cap}<p class="empty">${pts.length ? "One reading so far. The trend appears after a few sessions." : emptyText}</p>`;
    return;
  }
  const W = 480, H = 200, L = 36, R = 48, T = 10, B = 24;
  const t = pts.map((r) => Date.parse(r.date)), v = pts.map((r) => r[key]);
  const t0 = Math.min(...t), t1 = Math.max(...t);
  let lo = Math.min(...v), hi = Math.max(...v);
  const pad = Math.max((hi - lo) * 0.15, 1);
  lo = Math.floor(lo - pad); hi = Math.ceil(hi + pad);
  if (unit === "%") hi = Math.min(hi, 100);
  const x = (ms) => L + (W - L - R) * (ms - t0) / (t1 - t0 || 1);
  const y = (val) => T + (H - T - B) * (hi - val) / (hi - lo || 1);
  const g = document.createElementNS(NS, "svg");
  g.setAttribute("viewBox", `0 0 ${W} ${H}`);
  g.setAttribute("role", "img");
  g.setAttribute("aria-label", `${key} from ${pts[0].date} to ${pts.at(-1).date}`);
  const grid = svg("g", { class: "grid" }, g), axis = svg("g", { class: "axis" }, g);
  for (let k = 0; k <= 3; k++) {
    const val = lo + (hi - lo) * k / 3;
    svg("line", { x1: L, x2: W - R, y1: y(val), y2: y(val) }, grid);
    svg("text", { x: L - 6, y: y(val) + 4, "text-anchor": "end" }, axis).textContent = fmt(val, 0);
  }
  const y0 = new Date(t0).getFullYear(), y1 = new Date(t1).getFullYear();
  for (let yr = y0; yr <= y1; yr += Math.max(1, Math.ceil((y1 - y0) / 6))) {
    const ms = Date.parse(`${yr}-01-01`);
    if (ms >= t0 && ms <= t1) svg("text", { x: x(ms), y: H - 6, "text-anchor": "middle" }, axis).textContent = yr;
  }
  svg("path", { class: "series", d: pts.map((_, i) => `${i ? "L" : "M"}${x(t[i])},${y(v[i])}`).join("") }, g);
  svg("circle", { class: "dot", cx: x(t.at(-1)), cy: y(v.at(-1)), r: 4 }, g);
  svg("text", { class: "endlabel", x: x(t.at(-1)) + 8, y: y(v.at(-1)) + 4 }, g).textContent = `${fmt(v.at(-1), dec)}${unit}`;
  const cross = svg("line", { class: "cross", y1: T, y2: H - B, visibility: "hidden" }, g);
  const hot = svg("circle", { class: "dot", r: 5, visibility: "hidden" }, g);
  const hit = svg("rect", { x: L, y: T, width: W - L - R, height: H - T - B, fill: "transparent" }, g);
  hit.addEventListener("pointermove", (ev) => {
    const r = g.getBoundingClientRect(), mx = (ev.clientX - r.left) * W / r.width;
    let i = 0;
    for (let k = 1; k < pts.length; k++) if (Math.abs(x(t[k]) - mx) < Math.abs(x(t[i]) - mx)) i = k;
    cross.setAttribute("x1", x(t[i])); cross.setAttribute("x2", x(t[i])); cross.setAttribute("visibility", "visible");
    hot.setAttribute("cx", x(t[i])); hot.setAttribute("cy", y(v[i])); hot.setAttribute("visibility", "visible");
    const ctx = pts[i].soc != null ? ` · at ${fmt(pts[i].soc)} % charge` : "";
    showTip(ev, `${pts[i].date}<br><strong>${fmt(v[i], dec)} ${unit}</strong>${ctx}`);
  });
  hit.addEventListener("pointerleave", () => {
    cross.setAttribute("visibility", "hidden"); hot.setAttribute("visibility", "hidden"); hideTip();
  });
  box.innerHTML = cap;
  box.appendChild(g);
}

async function loadHistory() {
  const rows = await api("/api/history").catch(() => []);
  lineChart($("#soh-chart"), rows, "soh", "%", 1, "Not decoded yet for this car.");
  lineChart($("#spread-chart"), rows, "spread_mv", " mV", 0, "No readings yet.");
  const sim = rows.some((r) => r.simulated);
  $("#hist-note").textContent = rows.length
    ? `${rows.length} days with readings${sim ? " (simulated history)" : ""}. One kept per day, with the charge it was taken at.`
    : "Each day you connect adds a point.";
  $("#hist-table").innerHTML = rows.length ? `<table><tr><th>Date</th><th>Charge</th><th>Spread</th><th>Health</th></tr>${
    rows.map((r) => `<tr><td>${esc(r.date)}</td><td>${r.soc ?? "–"} %</td><td>${r.spread_mv ?? "–"} mV</td><td>${r.soh ?? "–"} %</td></tr>`).join("")}</table>` : "";
}

// ---------------------------------------------------------------- sources
function renderSources(s) {
  const read = s.read_at || {};
  $("#sources").innerHTML = `<tr><th>Reading</th><th>Now</th><th>Module · data IDs</th><th>Evidence</th></tr>` +
    status.fields.map((f) => {
      let v = s[f.name];
      if (Array.isArray(v)) v = `${v.length} values`;
      else if (v != null) v = `${fmt(v, f.unit === "V" ? 3 : 1)} ${f.unit}`;
      else v = s.errors?.[f.name] ? "not read" : "–";
      const ids = f.dids.length > 1 ? `${f.dids[0]}–${f.dids.at(-1)}` : f.dids[0];
      const when = read[f.name] ? new Date(read[f.name] * 1000).toLocaleTimeString() : "";
      return `<tr><td>${esc(f.label)}</td><td>${esc(v)}${when ? `<small class="zero">${when}</small>` : ""}</td>`
        + `<td>${esc(f.ecu.slice(4, 6))} · ${esc(ids)}</td><td>${esc(f.source)}</td></tr>`;
    }).join("");
}

// ---------------------------------------------------------------- live stream
let es = null, lastDay = "";
function startLive() {
  if (es) return;
  es = new EventSource("/api/live");
  es.onerror = (ev) => {
    setLink("err", "<strong>Lost contact with e500.</strong> Reconnecting.");
    document.body.classList.add("stale");
    ev.target.close();
    if (es === ev.target) es = null;
    setTimeout(startLive, 2000);
  };
  es.onmessage = (e) => {
    const s = JSON.parse(e.data);
    if (s.error) {
      setLink("err", s.kind === "internal" ? `<strong>e500 hit an error.</strong> ${esc(s.error)}`
                                           : "<strong>No answer from the car.</strong> Retrying.");
      document.body.classList.add("stale");
      return;
    }
    document.body.classList.remove("stale");
    renderLink();
    $("#verdict").textContent = s.verdict;
    renderCharge(s);
    renderStats(s);
    renderPack(s);
    renderDeviation(s);
    renderSources(s);
    const day = new Date().toDateString();
    if (day !== lastDay) { lastDay = day; loadHistory(); }
  };
}

async function init() {
  try {
    status = await api("/api/status");
  } catch {
    setLink("err", "<strong>e500 isn't running.</strong> Start it with <code>e500 ui</code>.");
    return;
  }
  $("#sim-note").hidden = status.mode !== "mock";
  document.body.classList.toggle("sim", status.mode === "mock");
  renderCar();
  renderLink();
  startLive();
}

init();
