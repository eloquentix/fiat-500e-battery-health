# CLAUDE.md — e500 (Fiat 500e battery health)

Battery health monitor for the 2013–2019 Fiat 500e, built like pdiag (the Porsche
K-line tool in ../porsche): traced transport, mock car, discovery, local web UI.

## Hard rules
1. **Read-only.** Only 0x22 ReadDataByIdentifier, 0x3E TesterPresent, 0x19
   ReadDTCInformation (read sub-functions only: 0x01, 0x02, 0x04, 0x06, 0x0A), OBD mode 01.
   Never 0x11, 0x14, 0x27, 0x2E, 0x2F, 0x31, 0x34–0x37, 0x3D, 0x85. Clearing fault codes
   (0x14) stays excluded: it erases evidence. (0x19 approved by Radu 2026-11-07 for the
   diagnosis report.)
2. **Clean-room.** Decode only from our own captures. No tables from AlfaOBD, Car
   Scanner profiles, wiTECH, or other proprietary tools. Record each decoded field's
   source in docs/PROTOCOL.md and in `Field.source`.
3. **Logs and history are private** (VIN, cell data). `logs/` and `data/` are gitignored.
4. macOS, Python 3.11+, `uv`, pyserial.

## Architecture (`src/e500/`)
- `elm.py` — ELM327/STN over serial; ATSP7 (29-bit) default; strict ISO-TP reassembly per CAN ID; trace.
- `uds.py` — read-only UDS client; `module_id(0x44)` = 18DA44F1; pending (0x78) is never resent.
- `scan.py` — module discovery and resumable DID sweeps.
- `battery.py` — REAL_FIELDS decoded from our captures (cells, charge, current, capacity,
  lifetime Ah, temperatures), dash window, SOH range against the published new rating, verdict.
- `modules.py` — identification (F180–F19F) parsing, programming dates, roles, VIN agreement.
- `analysis.py` — a logged charge → capacity (Ah per 100 %), charger power, resistance from
  pulses, per-cell offsets at rest and under load, temperatures.
- `report.py` — the battery and history report: findings as data, one self-contained HTML page.
- `survey.py` — one read-only run that records everything the report uses.
- `mock.py` — simulated car shaped like the real one (29-bit, module 44's DIDs); values FAKE.
- `service.py` — `Car`: lock, snapshots shared by tabs, session log, daily history (capacity trend).
- `web.py` + `static/` — `e500 ui`: JSON, SSE `/api/live`, Host check; vanilla JS.

## Status (2026-11-07)
- **First car session 2026-11-07** (2013 500e, orange): CAN 500k, 29-bit, no generic OBD,
  11 modules. Decoded (docs/PROTOCOL.md): 97 cells, true charge and dash window, current,
  capacity 37.9 Ah (two modules; confirmed by ∫current over a 2 h 240 V charge), lifetime Ah,
  18 temperatures, odometer (44/28:2001 ÷ 10 km), programming dates, fault codes (0x19). Battery holds
  ~60 % of new (57–63 %); owner's real range (~60 % of the dash guess) agrees. Pack most likely replaced
  ~2019-12-31 (service BPCM; BPCM sits inside the pack). 6 stored faults; U1487 (28) self-inflicted.
- Adapter: OBDLink EX (STN2232 v5.10.3), `/dev/cu.usbserial-*`, 115200. Plug OBD first, then USB.
- Lesson: never probe other CAN bit rates on this car (ATSP at 250k etc.): our probe on 2026-11-07 set
  U1487 in the ABS. Stay on ATSP7 (500k, 29-bit). Module 1F needs ≥ 200 ms reply timeout (ATST32).
- Findings handoff: `uv run python scripts/dossier.py` → data/dossier-<date>.html (private, has the VIN).

## Next steps
1. Highway test for the owner's symptom (after ~30 min of hard highway driving the battery sometimes keeps
   discharging at a standstill): `e500 record` through the drive and 10–15 min stopped, Enter marks the moment.
   Tells apart pack cooling, gauge correction and a weak cell under load.
   Also: faults after a key cycle without sweeping; `e500 survey --deep` resumes and finishes 42–60.
2. A drive or charge from low: capacity at the steep end, weak cells; test the A005 histogram hypothesis.
3. Find the charge counter: A028/A018/A005 before plug-in and after unplug.
4. CAN-B (50 k, pins 1/9) needs a pin-swap adapter: TPMS (A1: 40A1–40A4), cluster, body faults.

## Open questions
- New capacity: 60 or 63 Ah? (A014 holds 0x025A = 60.2; unproven.)
- The dash range read 93 mi at 99 %, which implies ~7 mi/kWh on 13 kWh usable: a guess-o-meter,
  or are we wrong?
- What A056 (23–27 °C, 18 values) measures; what A611 is versus A012.
