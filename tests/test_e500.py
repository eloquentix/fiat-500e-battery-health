import http.client
import json
import threading
from http.server import ThreadingHTTPServer

import pytest

from e500 import battery, cli, scan, web
from e500.elm import AdapterError, Elm327, NoData, reassemble
from e500.mock import MockLink, _iso_tp
from e500.service import Car
from e500.uds import Client, NegativeResponse, module_id, rx_id

BMS = module_id(0x44)

# Real replies from a 2013 500e (2026-11-07), as the OBDLink EX printed them. No VIN.
REAL_A100 = ["18DAF144101B62A1000F8E0F", "18DAF14421900F8F0F900F8F",
             "18DAF144220F900F900F900F", "18DAF14423910F900F900F91"]  # 12 cells, 4 frames
REAL_A602 = ["18DAF1440462A602B5"]  # true charge raw 181 (dash showed 69 %)
REAL_A609 = ["18DAF1440562A6090F8E"]  # controller's highest cell, 3.982 V
REAL_NRC = ["18DAF144037F2231"]  # requestOutOfRange


def elm(link=None):
    e = Elm327(link or MockLink())
    e.init()
    return e


class Replay(MockLink):
    """Answers with recorded lines per request; anything else gets NO DATA."""

    def __init__(self, answers: dict):
        super().__init__()
        self.answers, self.sent = answers, []

    def write(self, data):
        text = data.decode().strip().upper()
        if text.startswith("AT"):
            return super().write(data)
        self.sent.append(text)
        self.reply = "\r".join(self.answers.get(text, ["NO DATA"]))


# -- ISO-TP ---------------------------------------------------------------------------
def test_real_29bit_multiframe():
    f = reassemble(REAL_A100)[0]
    assert f.can_id == 0x18DAF144 and len(f.data) == 27 and f.data[:3] == b"\x62\xa1\x00"
    cells = battery._u16s(f.data[3:])
    assert len(cells) == 12 and cells[0] == 3982


@pytest.mark.parametrize("lines", [
    REAL_A100[:2] + REAL_A100[3:],                       # a consecutive frame lost
    [REAL_A100[0], REAL_A100[2], REAL_A100[1], REAL_A100[3]],  # out of order
    REAL_A100[:3],                                        # reply cut short
])
def test_incomplete_multiframe_is_dropped(lines):
    assert reassemble(lines) == []  # never a short but plausible-looking reading


def test_11bit_still_parsed_and_streams_stay_apart():
    a, b = _iso_tp(0x7E8, bytes(range(20))), _iso_tp(0x7EF, bytes(range(100, 130)))
    mixed = [a[0], b[0], a[1], b[1], a[2], b[2], b[3], b[4]]
    assert {f.can_id: f.data for f in reassemble(mixed)} == {0x7E8: bytes(range(20)),
                                                            0x7EF: bytes(range(100, 130))}


def test_noise_lines_ignored():
    assert reassemble(["SEARCHING...", "OK", "7E8"]) == []


# -- UDS --------------------------------------------------------------------------------
def test_addressing():
    assert module_id(0x44) == 0x18DA44F1 and rx_id(0x18DA44F1) == 0x18DAF144 and rx_id(0x7E0) == 0x7E8


def test_real_replies_through_the_client():
    e = elm(Replay({"22A602": REAL_A602, "22A609": REAL_A609, "22A0FF": REAL_NRC}))
    c = Client(e)
    assert c.read_did(BMS, 0xA602) == b"\xb5"
    assert c.read_did(BMS, 0xA609) == b"\x0f\x8e"
    with pytest.raises(NegativeResponse) as err:
        c.read_did(BMS, 0xA0FF)
    assert err.value.code == 0x31


def test_response_pending_is_not_resent():
    link = Replay({"22A602": ["18DAF144037F2278"]})
    with pytest.raises(AdapterError, match="pending"):
        Client(elm(link)).read_did(BMS, 0xA602)
    assert link.sent == ["22A602"]  # asked once


def test_nobody_home():
    with pytest.raises(NoData):
        Client(elm()).read_did(module_id(0x99), 0xF190)


@pytest.mark.parametrize("text", ["CAN ERROR", "BUFFER FULL", "?", "STOPPED"])
def test_adapter_errors_raise(text):
    with pytest.raises(AdapterError):
        Client(elm(Replay({"22A602": [text]}))).read_did(BMS, 0xA602)


# -- decoders -----------------------------------------------------------------------------
@pytest.mark.parametrize("raw,dash", [(181, 69), (189, 72), (199, 77), (244, 97), (249, 99)])
def test_true_charge_and_dash_window(raw, dash):
    field = next(f for f in battery.REAL_FIELDS if f.name == "soc_true")
    assert abs(battery.dash_soc(field.decode(bytes([raw]))) - dash) <= 0.6  # the 5 readings


@pytest.mark.parametrize("name,hexdata,value", [
    ("current", "ffa2ffbe", -4.7),     # charge starting (real bytes, 2026-11-07)
    ("current", "000b000a", 0.55),     # at rest after the charge
    ("capacity_ah", "017b", 37.9),     # the controller's capacity
    ("ah_in", "02ec8e", 19163.0),      # lifetime charge counter
])
def test_new_decoders_on_real_bytes(name, hexdata, value):
    f = next(f for f in battery.REAL_FIELDS if f.name == name)
    assert f.decode(bytes.fromhex(hexdata)) == pytest.approx(value)


def test_temperatures_decode():
    f = next(f for f in battery.REAL_FIELDS if f.name == "pack_temps")
    assert f.decode(bytes.fromhex("37" * 17 + "38")) == [15.0] * 17 + [16.0]


def test_soh_range():
    lo, hi = battery.soh_range(37.9)
    assert round(lo) == 57 and round(hi) == 63 and battery.soh_about(37.9) == 60


def test_real_fields_cover_the_cells():
    cells = next(f for f in battery.REAL_FIELDS if f.name == "cells")
    assert cells.ecu == BMS and cells.dids == tuple(range(0xA100, 0xA109))


def test_cell_stats():
    st = battery.cell_stats([4.0] * 95 + [3.95, 4.02])
    assert st["count"] == 97 and st["min_cell"] == 96 and st["max_cell"] == 97
    assert st["spread_mv"] == 70.0 and st["lowest5"][0] == {"cell": 96, "v": 3.95}
    assert st["pack_v"] == round(4.0 * 95 + 3.95 + 4.02, 2)


@pytest.mark.parametrize("snap,start", [
    ({"soc": 69, "cell_stats": {"spread_mv": 8, "count": 97, "min_v": 3.979, "max_v": 3.987}},
     "Charge 69%. All 97 cells within 8 mV"),
    ({"soh": 87.0, "cell_stats": {"spread_mv": 8, "count": 97, "min_v": 3.9, "max_v": 3.908}},
     "Battery health 87%."),
    ({}, "No battery data decoded"),
])
def test_verdict(snap, start):
    v = battery.verdict(snap)
    assert v.startswith(start)
    assert "health" in v.lower() or "decoded" in v.lower()  # never silent about health


# -- the simulated car (shaped like the real one) -----------------------------------------
def test_mock_finds_modules_on_29bit_only():
    c = Client(elm())
    assert scan.find_modules(c, [module_id(t) for t in (0x43, 0x44, 0x99)]) == \
        {module_id(0x43): 0x18DAF143, module_id(0x44): 0x18DAF144}
    assert scan.find_modules(c, range(0x7E0, 0x7E8)) == {}  # no 11-bit modules, like the car


def test_did_sweep_logs_and_resumes(tmp_path):
    log, c = tmp_path / "dids.jsonl", Client(elm())
    assert scan.sweep_dids(c, BMS, 0xA600, 0xA60B, log) == 4  # A602, A609, A60A, A60B
    assert scan.sweep_dids(c, BMS, 0xA600, 0xA60B, log) == 0  # resumed: nothing new
    assert len(log.read_text().splitlines()) == 12


def test_sweep_stops_when_module_goes_quiet(tmp_path):
    with pytest.raises(AdapterError, match="unanswered"):
        scan.sweep_dids(Client(elm()), module_id(0x99), 0, 0xFF, tmp_path / "d.jsonl", max_misses=5)


def test_snapshot_with_real_fields_and_history(tmp_path):
    hist = tmp_path / "history.jsonl"
    car = Car(elm(), battery.REAL_FIELDS, mock=True, history=hist, car={"colour": "Orange"})
    snap = car.snapshot()
    assert len(snap["cells"]) == 97 and snap["cell_stats"]["count"] == 97
    assert snap["soc_true"] == 74.0 and snap["soc"] == round(battery.dash_soc(74.0), 1)
    assert abs(snap["cell_max"] - snap["cell_stats"]["max_v"]) < 0.002  # controller agrees
    assert set(snap["read_at"]) >= {"cells", "soc_true"}  # per-field timestamps
    assert snap["capacity_ah"] == 52.0 and snap["current"] == 0.5 and len(snap["pack_temps"]) == 18
    assert snap["verdict"].startswith("The battery holds 52.0 Ah, about 80% of new.")
    car.snapshot()
    rows = car.history()
    assert len(rows) == 1 and rows[0]["soc_true"] == 74.0 and "cells" in rows[0]["decoders"]
    assert rows[0]["capacity_ah"] == 52.0  # the health trend
    assert car.status()["car"]["colour"] == "Orange"


def test_cli_smoke(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(cli, "LOG_DIR", tmp_path)
    monkeypatch.setattr(cli, "DATA_DIR", tmp_path / "data")
    for argv in (["battery"], ["info"], ["ecus"]):
        cli.main(["--mock", *argv])
    out = capsys.readouterr().out
    assert "lowest five cells" in out and "Charge (as on the dash)" in out and "ELM327" in out
    assert "module at 18DA44F1, replies from 18DAF144" in out
    assert json.loads((tmp_path / "data" / "car.json").read_text())["colour"] == "Orange"


# -- web ----------------------------------------------------------------------------------
def _serve(car):
    srv = ThreadingHTTPServer(("127.0.0.1", 0), web.make_handler(car, every=0.01))
    threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True).start()
    return srv, http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=5)


def test_web_live_and_host_check():
    srv, c = _serve(Car(elm(), battery.REAL_FIELDS, mock=True))
    try:
        c.request("GET", "/api/status", headers={"Host": "attacker.example"})
        assert c.getresponse().status == 403
        c.request("GET", "/api/live")
        r = c.getresponse()
        msg = json.loads(r.fp.readline().decode()[len("data: "):])
        assert msg["cell_stats"]["count"] == 97 and msg["soc_true"] == 74.0
    finally:
        srv.shutdown()


# -- module identification and history ---------------------------------------------------
# Shaped like the real captures; the VIN and serials are made up.
FAKE_VIN = "ZFAFAKEVIN0000001".encode().hex()
IDENT = {
    0x44: {"f180": "0101422e30392e30332e3032009c", "f182": "0101422e78782e78782e7878009c",
           "f184": "01353431363335202020191231", "f187": "3035313836303135414d20",
           "f18c": "00" * 15, "f192": "3032363178787878787878"},
    0x42: {"f184": "01333433373720202020161220", "f187": "3035313932333537414320", "f190": FAKE_VIN,
           "f183": "012000200020002000200020002000200020000c"},
    0x43: {"f184": "01333433373720202020161220"},
    0x45: {"f184": "20202020202020202020130122"},
    0x60: {"f184": "01202020202020202020130809"},
    0x40: {"f184": "01202020202020202020130516", "f190": FAKE_VIN},
}


def _mods():
    from e500 import modules as mods
    return mods, {a: mods.parse_ident(d) for a, d in IDENT.items()}


def test_fingerprint_dates_and_tester():
    mods, m = _mods()
    assert m[0x44]["fingerprints"]["application"] == {"date": "2019-12-31", "by": "541635"}
    assert m[0x45]["fingerprints"]["application"] == {"date": "2013-01-22", "by": None}
    assert "boot" not in m[0x42]["fingerprints"]  # not a date: ignored, not guessed
    assert set(m[0x44]["notes"]) == {"serial number blank", "placeholder 'xx' in its numbers"}


def test_history_and_vin_check():
    mods, m = _mods()
    ev = mods.history(m)
    assert [(e["date"], e["module"]) for e in ev] == [("2016-12-20", "42"), ("2016-12-20", "43"),
                                                      ("2019-12-31", "44")]
    assert ev[-1]["what"] == "replaced or reprogrammed"
    assert mods.vin_check(m) == {"modules": ["40", "42"], "agree": True, "count": 2}
    m[0x40]["vin"] = "ZFAFAKEVIN0000002"
    assert not mods.vin_check(m)["agree"]


# -- charge-session analysis and the report -----------------------------------------------
def _charge_rows(cap_ah=40.0, minutes=60, amps=15.0):
    """A synthetic charge log in charge_log.py's format: steady current, then pulses."""
    rows, soc, ah, t = [], 70.0, 1000.0, 1.8e9
    for k in range(minutes * 2):
        pulse = k > minutes * 1.5
        i = (0.5 if k % 2 else -amps) if pulse else -amps
        cells = [round(3900 + soc + (k % 3) - (5 if c == 65 else 0) - (0 if i > 0 else -2)) for c in range(97)]
        b = {f"{0xA100 + j:x}": "".join(f"{v:04x}" for v in cells[12 * j:12 * j + 12]) for j in range(9)}
        b.update(a012=f"{round(i * 20) & 0xFFFF:04x}" * 2, a602=f"{round(soc / 0.4):02x}",
                 a615=f"{round(ah * 10):06x}", a616="002710", a625=f"{round(cap_ah * 10):04x}",
                 a200="3c" * 17 + "3e")
        rows.append({"t": t, "44": b, "42": {"2050": f"{round(cap_ah * 10):04x}"}})
        dah = -i * 30 / 3600 if i < 0 else 0
        ah, soc, t = ah + dah, min(99.6, soc + dah / cap_ah * 100), t + 30
    return rows


def test_session_measures_capacity_and_resistance():
    from e500 import analysis
    s = analysis.session(analysis.points(_charge_rows()))
    assert s["usable"] and s["capacity_ah"] == 40.0
    assert s["steady"]["ah_per_100"] == pytest.approx(40.0, rel=0.05)  # SOC is quantised to 0.4 %
    assert s["steady"]["power_kw"] > 5 and abs(s["steady"]["counter_agrees_pct"]) < 3
    assert s["temp_min"] == 20 and s["temp_max"] == 22
    assert s["resistance"] and s["resistance"]["pulses"] >= 5


def test_report_findings_and_page():
    from e500 import analysis, report
    mods, m = _mods()
    s = analysis.session(analysis.points(_charge_rows(cap_ah=37.9)))
    recalls = [{"NHTSACampaignNumber": "16V303000", "Summary": "PIM software"},
               {"NHTSACampaignNumber": "14V235000", "Summary": "PIM coolant"}]
    f = {x["area"]: x for x in report.findings(s, m, recalls, None)}
    assert f["Battery capacity"]["level"] == "serious" and "about 60%" in f["Battery capacity"]["headline"]
    assert f["Service history"]["level"] == "caution"  # the battery controller is a service part
    assert f["VIN"]["level"] == "good" and f["Fault codes"]["level"] == "unknown"
    assert report.recall_trace(recalls[0], m) == "inverter software dated Dec 20, 2016"
    assert report.recall_trace(recalls[1], m) is None
    page = report.build(car={"name": "Fiat 500e", "year": 2013, "colour": "Orange", "hex": "#E2571E"},
                        session=s, modules=m, recalls=recalls)
    assert page.startswith("<!doctype html>") and page.count("<section") >= 8
    assert "ZFAFAKEVIN" not in page  # the VIN never goes on the page
    assert report.findings(None, {}, None, None)[0]["level"] == "unknown"


def test_survey_on_the_mock(tmp_path):
    from e500 import survey
    (tmp_path / "x-full-44.jsonl").write_text('{"ecu": "44", "did": "a602", "hex": "b5"}\n')
    out = tmp_path / "s-survey-result.jsonl"
    seen = []
    res = survey.run(Client(elm()), out, tmp_path, show=lambda p: seen.append(p.stage))
    assert res["modules"] == [0x40, 0x42, 0x43, 0x44, 0x45] and 0x1F in res["missing"]
    assert res["faults"] == 1
    lines = [json.loads(line) for line in out.read_text().splitlines()]
    assert "modules" in lines[0] and lines[-1].get("complete")
    assert "faults" in lines[1]  # fault codes come first, right after discovery
    assert {"ecu": "44", "did": "a602", "hex": "b9"} in lines  # known DID re-read (mock value)
    assert seen[0] == "Finding modules" and "Fault codes" in seen and seen[-1] == "Reading"
    assert survey.unfinished(tmp_path) is None  # complete: nothing to continue


def test_survey_pauses_and_continues(tmp_path):
    from e500 import survey
    out = tmp_path / "a-survey-result.jsonl"

    def stop_midway(p):
        if p.stage == "Reading" and p.module_index == 3 and p.done >= 16:
            raise KeyboardInterrupt  # Ctrl-C
    with pytest.raises(KeyboardInterrupt):
        survey.run(Client(elm()), out, tmp_path, show=stop_midway)
    assert survey.unfinished(tmp_path) == out
    modules, read, faults = survey.state(out)
    assert modules and len(faults) == 5 and read  # discovery and faults done, some reads done
    before = len(out.read_text().splitlines())
    res = survey.run(Client(elm()), tmp_path / "b-survey-result.jsonl", tmp_path, resume=out)
    lines = [json.loads(line) for line in out.read_text().splitlines()]
    assert res["path"] == out and lines[-1].get("complete") and len(lines) > before
    assert res["faults"] == 1  # counted over the whole file, including the part before the pause
    pairs = [(r["ecu"], r["did"]) for r in lines if "did" in r]
    assert len(pairs) == len(set(pairs))  # nothing read twice
    assert sum("modules" in r for r in lines) == 1 and sum("faults" in r for r in lines) == 5
    assert not (tmp_path / "b-survey-result.jsonl").exists()


def test_survey_view_without_a_terminal(tmp_path):
    import io
    from e500 import survey
    buf = io.StringIO()
    view = cli.SurveyView(buf)
    survey.run(Client(elm()), tmp_path / "v-survey-result.jsonl", tmp_path, show=view)
    view.close()
    text = buf.getvalue()
    assert "\r" not in text  # no in-place updates when it isn't a terminal
    assert "Finding modules  done" in text and "Fault codes      5 modules read · 1 stored" in text
    assert "✓ 44 Battery controller (BPCM):" in text and text.count("✓") == 5


def test_estimate_is_honest():
    from e500 import survey
    assert 15 * 60 < survey.estimate_s(deep=True) < 25 * 60
    assert survey.estimate_s(deep=False) < 3 * 60


def test_fault_codes_on_the_mock():
    from e500 import dtc
    c = Client(elm())
    rec = dtc.read(c, BMS)
    assert [x["code"] for x in rec["codes"]] == ["P0AA6-00"]
    assert rec["codes"][0]["status_text"] == ["confirmed", "failed since clear"]
    assert rec["codes"][0]["meaning"] == "Hybrid/EV battery voltage system isolation fault"
    assert rec["codes"][0]["snapshot"] == "0aa600010203"
    assert dtc.read(c, module_id(0x42))["codes"] == []


def test_dtc_code_letters():
    from e500 import dtc
    assert [dtc.code(bytes.fromhex(h)) for h in ("0aa100", "4123ff", "9a0001", "c10087")] == \
        ["P0AA1-00", "C0123-FF", "B1A00-01", "U0100-87"]


@pytest.mark.parametrize("req", ["1401ffff", "2e0102", "3101ff00", "1101", "2701", "1985", "8501"])
def test_writes_never_reach_the_adapter(req):
    link = Replay({})
    with pytest.raises(ValueError, match="read-only"):
        Client(elm(link)).request(BMS, bytes.fromhex(req))
    assert link.sent == []


def test_recorder_on_the_mock(tmp_path):
    from e500 import recorder
    out = tmp_path / "r-recording.jsonl"
    known = {0x44: [0xA602, 0xA625, 0xA615], 0x42: [0x2050]}
    presses = iter([0, 1, 0, 0, 0, 0])
    seen = []
    live = recorder.run(Client(elm()), out, known, show=lambda l: seen.append(dict(l.values)),
                        marks=lambda: next(presses), max_loops=6)
    lines = [json.loads(line) for line in out.read_text().splitlines()]
    assert lines[0]["start"] and live.loops == 6 and live.marks == 1
    assert [r["mark"] for r in lines if "mark" in r] == [1]
    loops = [r for r in lines if "core" in r]
    assert len(loops) == 6 and "a012" in loops[0]["core"]["44"]
    assert "a100" in loops[0]["core"]["44"] and "a100" not in loops[1]["core"]["44"]  # cells every 5th loop
    assert seen[0]["current_a"] == 0.5 and len(seen[0]["cells_mv"]) == 97 and "cells_mv" not in seen[1]
    assert seen[0]["soc_true"] == 74.0 and seen[0]["spread_mv"] >= 0
    rot = [(m, d) for r in loops for m, ds in r["rot"].items() for d in ds]
    assert ("44", "a625") in rot and ("44", "a602") not in rot  # core values aren't repeated in the rotation


def test_volt_logger_rate_and_steps(tmp_path):
    from e500 import voltlog
    clock = [0.0]
    def read():  # 12.60 V falling 20 mV/h, a 0.3 V dip for 3 minutes at 2 h (a module waking)
        h = clock[0] / 3600
        return round(12.60 - 0.02 * h - (0.3 if 2.0 <= h < 2.05 else 0), 2)
    def sleep(s):
        clock[0] += 60  # one reading per simulated minute
    n = voltlog.run(read, tmp_path / "v.jsonl", every=60, max_readings=240,
                    clock=lambda: clock[0], sleep=sleep)
    assert n.readings == 240 and len(n.minutes) > 200
    assert n.rate_mv_per_h() == pytest.approx(-20, abs=8)
    assert [round(d, 1) for _, d in n.steps] == [-0.3, 0.3]  # the dip, and the recovery


def test_volts_command_sends_nothing_to_the_car(tmp_path, monkeypatch):
    from e500 import voltlog
    monkeypatch.setattr(cli, "LOG_DIR", tmp_path)
    sent = []
    real_run = voltlog.run
    def short_run(read, out, every, show):
        return real_run(read, out, every=0, show=show, max_readings=3, sleep=lambda s: None)
    monkeypatch.setattr(voltlog, "run", short_run)
    link = MockLink()
    real_write = link.write
    link.write = lambda data: (sent.append(data.decode().strip()), real_write(data))[1]
    e = Elm327(link)
    cli.cmd_volts(type("A", (), {"every": 0})(), e)
    assert sent and all(x.upper().startswith("AT") for x in sent)  # adapter commands only
    assert set(sent) <= {"ATE0", "ATRV"}
    lines = [json.loads(line) for line in next(tmp_path.glob("*-volts.jsonl")).read_text().splitlines()]
    assert [r["v"] for r in lines] == [14.2, 14.2, 14.2]
