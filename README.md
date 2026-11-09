# e500: Fiat 500e battery health check

Battery diagnostics for the 2013–2019 Fiat 500e. An $80 adapter, a Mac, and this.

**Try the demo first** (simulated car, no hardware): https://apps.eloquentix.com/fiat500/

## Why this exists

The first-generation 500e is now one of the cheapest used EVs you can buy, and almost all
of its value is the battery. Two cars that look identical can differ by half their range.
The dash won't tell you which one you're looking at: it shows a charge percentage and a
range guess that adapts to the last few drives. Nothing on the car shows battery health.

The battery controller knows. It keeps its own capacity figure, all 97 cell voltages,
18 temperatures, and lifetime charge counters, and the other modules record when they were
reprogrammed or replaced. Getting at that has needed a dealer's wiTECH or a paid Android
app. e500 reads it directly, explains it in plain sentences, and shows how each number
was worked out.

On our test car it found a battery holding about 60 % of its new capacity, and a
replacement pack fitted in early 2020. The owner's experience matched: real range is
about 60 % of what the dash promises.

## When it's useful

- **Before buying a used 500e:** ten minutes in a parking lot gives capacity against new,
  every cell's voltage, fault codes from all 11 modules, and the modules' service history
  (a battery controller programmed years after the car was built means a replacement pack).
- **Owning one:** follow capacity over months, find out why range dropped, check whether
  a single cell is weak, record a drive or a charge, log the 12 V battery overnight.
- **After battery work:** confirm what was actually done.

## What it reads

Tested on a 2013 500e (US). Every value below was decoded from our own recordings of that
car, with the evidence in [docs/PROTOCOL.md](docs/PROTOCOL.md).

| Reading | Status |
|---|---|
| All 97 cell voltages, highest and lowest cell | Confirmed |
| True state of charge, and how the dash percentage relates to it | Confirmed |
| Pack current, voltage, 18 module temperatures | Confirmed |
| Capacity the battery controller works with (Ah), lifetime charge in and out | Confirmed; matches charging and driving measurements |
| Capacity against new | About ±5 %: the new-pack rating (60–67 Ah) isn't published consistently |
| Odometer, road speed, module identification and programming dates | Confirmed |
| Fault codes from every module | Read only; never cleared |
| A manufacturer "state of health" figure | Not found; e500 measures capacity directly instead |
| 2014–2019 cars | Expected to match; untested |

The 2024-onward 500e is a different car and isn't supported.

## What you need

- A **2013–2019 Fiat 500e**.
- An **OBDLink EX** USB adapter (about $80). The 500e talks over CAN with long multi-part
  replies, and cheap ELM327 clones drop parts of them. Other genuine STN-based adapters
  should work but are untested. K-line cables (like the one pdiag uses) won't work.
- A **Mac**, **Python 3.11+** and **uv**.

## Setup

```sh
brew install uv                          # or: curl -LsSf https://astral.sh/uv/install.sh | sh
git clone https://github.com/eloquentix/fiat-500e-battery-health.git
cd fiat-500e-battery-health
uv sync
uv run e500 --mock ui                    # try it with the simulated car first
uv run pytest                            # optional: the test suite
```

## At the car

1. Plug the adapter into the car's OBD port first (under the dash, left of the steering
   column), then the USB cable into the Mac.
2. Turn the car **on** (ready light).
3. `uv run e500 survey`: about two minutes. It reads the fault codes first, then every
   module's identification and counters, then the battery data. Ctrl-C pauses; the same
   command continues. `--deep` reads everything the modules offer (about 20 minutes).
4. `uv run e500 report`: the battery and history report, at your desk, no car needed.

For the live view, `uv run e500 ui` (http://127.0.0.1:8500): charge, every cell, capacity,
current and temperatures, with an export button for the report.

**For the best capacity measurement**, record a long charge from low on 240 V, or a drive
from full to low:

```sh
caffeinate -i uv run e500 record         # Enter marks a moment, Ctrl-C stops
```

## Commands

| Command | What |
|---|---|
| `e500 survey [--deep] [--fresh]` | Everything the report uses, in one resumable run |
| `e500 report` | Battery and history report from what's been recorded |
| `e500 ui` | Live view with report export |
| `e500 record` | Record while driving or charging; Enter marks a moment |
| `e500 faults` | Fault codes from every module |
| `e500 battery` | One battery snapshot on the command line |
| `e500 volts` | Log the 12 V battery overnight. Sends nothing to the car (works on any car) |
| `e500 recalls` | Recalls for the model year from NHTSA (no VIN is sent) |
| `e500 ports`, `info`, `ecus`, `dids` | Adapter check and discovery tools |

Options: `--port` (or `E500_PORT`), `--mock` for the simulated car, `--no-trace`.

## Safety and privacy

e500 only reads. Every request goes through an allowlist of read services; anything else
is refused in code before it reaches the adapter. It never clears fault codes: a code
history is evidence, especially when buying.

Recordings (`logs/`) and reports (`data/`) contain your VIN and stay on your machine.

## How you can help

1. **You have a 500e:** run `uv run e500 survey` and send us the `logs/*-survey-result.jsonl`
   file, with the model year and, if you know them, the mileage and battery history. A car
   with a recently replaced pack is especially valuable: its capacity figure tells everyone
   what "new" reads.
2. **Long recordings:** a charge from below 20 % to full, or a drive from full to low,
   recorded with `e500 record`.
3. **Code:** a phone app with a Bluetooth adapter, Linux and Windows support.

Send recordings privately (they contain your VIN), or open an issue and we'll arrange it.
e500 decodes only from owners' own recordings, never from dealer software or paid apps'
internals.

## Buying or selling a 500e?

We're building **verified battery reports**: plug in for ten minutes and get a shareable,
tamper-evident report of capacity, cells, fault codes and battery history, for buyers,
sellers and dealers. Email
[info@eloquentix.com](mailto:info@eloquentix.com?subject=Fiat%20500e%20battery%20report)
to hear when it's ready.

## FAQ

**How do I check a used Fiat 500e's battery before buying?**
Plug an OBDLink EX into the car, run `e500 survey` (about two minutes), then `e500 report`.
You get the capacity against new, all 97 cells, fault codes from every module, and whether
the battery was replaced.

**What's a good battery capacity for a 500e?**
New packs are rated about 60–63 Ah. Our 2013 test car reads 37.9 Ah (about 60 %), which
means about 55–60 miles of real range. We don't yet have enough cars to say what's typical
for a given age or mileage; that's what recordings from other owners will show.

**Does it show state of health like LeafSpy does for the Leaf?**
No manufacturer health percentage was found on the 500e. e500 shows the capacity the
battery controller works with, checked against measured charging and driving, and compares
it with the new rating.

**Will a cheap Bluetooth ELM327 work?**
Not reliably. The cell voltages arrive as long multi-part CAN replies that clones drop.

**Does it work on the 2024 Fiat 500e?**
No. It's a different car with a different battery and electronics.

**Can it clear fault codes or change settings?**
No. It only reads, and it refuses anything else in code before it reaches the adapter.

## Disclaimer

Not affiliated with, endorsed by, or connected to Fiat, FCA or Stellantis. Brand names are
used only to describe compatibility. You use it on your own car at your own risk.

## Credits and license

A project by [Eloquentix](https://eloquentix.com). Sister project: [pdiag](https://github.com/eloquentix/porsche-996-diagnostics)
for K-line era Porsches. [GPL-3.0-or-later](LICENSE).
