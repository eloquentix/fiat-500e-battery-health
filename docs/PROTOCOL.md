# Protocol notes

Facts from public standards (SAE J1979, ISO 15765, ISO 14229) unless marked. Car-specific
findings go in the Findings table with their source.

## Bus
- OBD-II over CAN (ISO 15765-4), 11-bit IDs, 500 kbit/s, pins 6/14. ELM327: `ATSP6`.
- Generic OBD: functional request 0x7DF, replies 0x7E8–0x7EF.
- Physical UDS requests: usually tx → tx+8 replies; `uds.Client` remembers the actual reply ID.

## Pack (published spec, verify)
- 24 kWh, 97 Samsung SDI cells in series (Bosch pack); sources give 60–63 Ah per cell.

## Open-source leads (used as hints only; every field above is confirmed by our captures)
- carlrice/500e-can-bus (no license) + its public sheet "Fiat 500e CAN bus info WIP": first-gen CAN-C
  passive frames (0848A046 SoC, 0C50A049 charger V/A, 025CA049 wall A, 0C10A040 range km), request DIDs at 42
  (0303 key, 2056 contactor, 2082/20E1 charging, 2081 charge current, 2083, 205B, 2063, 0404, 20E2, 2096, 203F,
  204F, 2090/2094), 44 (A042 12 V, A012 current, A608 temp), 47 (010A charger current), and CAN-B (50 k, pins 1/9,
  pin-swap adapter): TPMS at A1 (22 40A1-40A4). Confirmed on our car: A606-A608 (−50 °C = max/min/avg temp),
  2081 (× 0.25 A), 205B ((raw − 1023) × 0.5 A), 0303/2056/2082/20E1 states. Wrong on our car: 42:200B is a clock.
  Its README: plugging devices into the 500e's OBD port has caused temporary errors.
- OVMS v3, `vehicle_fiat500e` (MIT): broadcast frames on C-CAN; odometer at 42/47 DID 2001.
- OBDb FIAT-500e (CC BY-SA 4.0): second-generation (2020+) signals at the same module 44,
  same cell layout (A100+). Its A009 and A200 layouts and the ÷20 current scale held on our car;
  its A029 (state of health) layout did not.

## Findings
| Date | Item | Value | Source |
|---|---|---|---|
| 2026-11-07 | Generic OBD | Not supported: `0100` → NO DATA on every CAN variant (11/29-bit, 500/250 k). BEVs have no emissions OBD. | own capture, `logs/*-protoprobe.jsonl` |
| 2026-11-07 | Bus and addressing | CAN 500 kbit/s; modules use 29-bit IDs `18DA<tt>F1` → replies `18DAF1<tt>`. 11-bit sweep 700–7F7: nothing. | own capture, `logs/*-ecus.jsonl`, `logs/*-addrprobe.jsonl` |
| 2026-11-07 | Modules | 1F 28 30 40 42 43 44 45 47 48 60 answer TesterPresent. Roles inferred from F18x/F19x strings: 44 battery controller, 43 inverter, 45 charger, 40 EV controller. | own capture, `logs/*-ident29*.jsonl` |
| 2026-11-07 | Cell voltages | Module 44, DIDs A100–A108: 97 × u16 big-endian, mV (8 blocks of 12, then 1). | own capture; count = 97, range 3.98 V, matches A609–A60B |
| 2026-11-07 | Cell max / min / average | Module 44, A609 / A60A / A60B: u16 BE, mV. Agree with A100–A108 to 1 mV when read in the same minute. | own capture |
| 2026-11-07 | True charge (SOC) | Module 44, A602: u8 × 0.4 %. Dash % = 0.4478 × raw − 12.26 (±0.4 % at 4 dash readings). Energy check: +26.4 % in 59 min on 240 V = 6.5 kW avg. | own capture, `logs/*-charge.jsonl` + dash readings |
| 2026-11-07 | Not range | Module 40, 200B = 57 stayed constant while dash range went 68 → 91 mi. | own capture |
| 2026-11-07 | Battery temperatures | Module 44, A200: 18 × u8 − 40 °C (15 → 23 °C over the charge). Min/max/mean equal module 42 DIDs 2060/205F/205E. A056 (23 → 27 °C) is something else: open. | own capture, `logs/*-charge.jsonl`; layout lead: OBDb gen-2 A200 |
| 2026-11-07 | Pack current | Module 44, A012: s16 / 20 A, negative = charging (two close readings; the first is used). −15.45 A × 393 V = 6.1 kW steady on 240 V (6.6 kW charger after losses); ≈ +0.5 A at rest. A611 (s16 / 20) is similar. | own capture; scale lead: OBDb gen-2 current (÷20) |
| 2026-11-07 | Lifetime charge counters | Module 44, A615 (in) / A616 (out): u24 / 10 Ah. A615 rose 7.7 Ah while ∫current gave 7.71 Ah (5.4 vs 5.39 on another window). 19,163 Ah in, 18,982 Ah out. | own capture |
| 2026-11-07 | Capacity | Module 44, A625 and module 42, 2050: u16 / 10 = 37.9 Ah, constant. The controller's charge (A602) moved 36.4–37.9 Ah per 100 % against ∫current on the steady part of the charge, so 37.9 Ah is the capacity it works with. New: 60–63 Ah (published specs; unmeasured). | own capture |
| 2026-11-07 | Remaining charge | Module 42, 2051 (= A029 bytes 4–5): u16 / 10 Ah, 26.6 → 36.7 over the charge. | own capture |
| 2026-11-07 | Cell extremes | Module 44, A009: highest-cell number, lowest-cell number, highest mV, lowest mV (u8, u8, u16, u16), then 4 bytes not decoded. | own capture; layout lead: OBDb gen-2 A009 |
| 2026-11-07 | Pack voltage | Module 42, 2063: u16 V (388 → 397; cell sum 393 → 397). | own capture |
| 2026-11-07 | Resistance | From the charger's on/off pulses near full: median 1.59 mΩ per cell (sd 0.07), 155 mΩ pack. Not a stored value; computed (analysis.py). | own capture |
| 2026-11-07 | Programming dates | F183–F185: [01] tester ID, then YYMMDD in BCD (DDMMYY would put module 44 in 2031). Factory dates 2012–2013; 42 and 43 on 2016-12-20 (tester 34377); 44 on 2019-12-31 (541635; blank serial, 'xx' placeholders: a service part); 47 on 2020-02-14 and 28, 48 on 2020-10-16 (tester 53751). | own capture, `logs/*-ident29-results.jsonl` |
| 2026-11-07 | Odometer (unconfirmed) | DID 2001 at 42 = 995,640, at 44 = 993,240 (u24). OVMS reads 2001 at 42/47 as the odometer. Unit not confirmed; check against the dash. | own capture; lead: OVMS vehicle_fiat500e |

## Fault codes (UDS 0x19, read 2026-11-07)
- `19 02 FF` returns every code a module *monitors*. Status 0x40/0x50 = not tested yet: not a fault.
  Faults are codes with any of bits 0x01/0x02/0x04/0x08/0x20/0x80 (dtc.FAULT_BITS).
- Stored faults: 44 P1E25-00 (8 records; snapshot shows A602 = 0xF2, 96.8 % true charge), 40 B11FA-00,
  B1666-18, B16B3-13, 28 U1487-00, 48 U0001-00.
- Snapshot records: DTC(3) status(1) record(1) count(1), then DID+data; the first DID is 1008.
- DID 1008 on every module counts minutes (+1/min while awake). On module 40 it reads 6.99 M min =
  13.3 years: running since the car was built, so its fault snapshots date the faults.

## Counters and dates (2026-11-07, surveys 18:21 and 21:55)
- 2001 (u24, ÷ 10 km) is the odometer on 44 and 28 (99,324 km = 61,717 mi; dash 61,720); 42:204C = km.
  42:2001 (995,640) runs 146 mi ahead: the EVCU's own distance count.
- 1008: module clock in minutes (+1/min while awake). 2008: clock at this key-on (6–14 min below 1008).
  200B: clock at the latest fault (28: equals U1487's snapshot clock, 196,152).
- 40:2023: ASCII with the VIN's serial digits and a 1234567890 placeholder, then BCD 13 09 05 (Sep 5, 2013).
- U1487 at 28 was set ≈ 14:00 on 2026-11-07, during our protocol probe (other CAN speeds on the 500 k bus):
  self-inflicted. Never transmit at another bit rate on this car.

## First drive (2026-11-08, logs/20261108-121604-recording.jsonl)
- 42:204F = road speed, u16 ÷ 16 km/h: ∫speed 13.4 km vs both odometers +14 km (1 km resolution).
- 25 min, 8.3 mi, max 68 km/h: 224 Wh/mi (4.46 mi/kWh) at the battery, 13–16 °C.
- Discharge-side capacity: −12.4 % true charge for 5.0 Ah net (A616 +7.0, A615 +2.0 regen) = 41 Ah per
  100 % (charge side: 36.8). Standing after the drive, key on: +0.75 A (290 W).
- Under ~100–124 A: cell spread ~60 mV (sequential reads over ~1 s; part of it is timing). Lowest cell varies;
  cell 1 sags most often (−29 mV beyond its rest offset).
