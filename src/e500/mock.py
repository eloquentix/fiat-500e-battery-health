"""Simulated 500e behind a simulated ELM327, for development without the car.

Shaped like the real car captured on 2026-11-07: CAN with 29-bit addressing, no
answer to generic OBD, the battery controller at module 0x44 with the real data
identifiers (cells A100-A108, true charge A602, max/min/average A609-A60B), and a
few other modules answering TesterPresent. It answers in real ELM327 text
(header-on CAN frames, ISO-TP framing, NO DATA), so the parser is exercised for real.

All VALUES here are FAKE: a pack charging slowly, cells near the real car's
voltages with some variation. The VIN is fake too.
"""

from __future__ import annotations

import random
import time

from .uds import module_id

BMS = module_id(0x44)
MODULES = {module_id(t) for t in (0x40, 0x42, 0x43, 0x44, 0x45)}
CELLS = 97
OFFSETS = [((i * 37) % 11 - 5) / 1000 for i in range(CELLS)]  # FAKE: fixed per-cell spread, +-5 mV
OFFSETS[92] -= 0.004  # FAKE: one cell that sits a little lower
CAPACITY_AH = 52.0  # FAKE: deliberately not the real car's figure

# FAKE history: one reading a season since 2019, so the trend chart has something to show.
MOCK_HISTORY = [
    {"date": f"{2019 + i // 4}-{(i % 4) * 3 + 1:02d}-15", "soh": round(100 - i * 0.4 - (i * 0.03) ** 2, 1),
     "spread_mv": round(6 + i * 0.1 + (i % 3) * 0.5, 1), "simulated": True}
    for i in range(31)
]


def _id(can_id: int) -> str:
    return f"{can_id:08X}" if can_id > 0x7FF else f"{can_id:03X}"


def _iso_tp(can_id: int, payload: bytes) -> list[str]:
    """How an ELM327 with headers on prints a reply: one line per CAN frame."""
    if len(payload) <= 7:
        frames = [bytes([len(payload)]) + payload]
    else:
        frames = [bytes([0x10 | (len(payload) >> 8), len(payload) & 0xFF]) + payload[:6]]
        rest, n = payload[6:], 1
        while rest:
            frames.append(bytes([0x20 | (n & 0xF)]) + rest[:7])
            rest, n = rest[7:], n + 1
    return [_id(can_id) + (f + bytes(8 - len(f))).hex().upper() for f in frames]


def _reply_id(tx: int) -> int:
    return (tx & 0xFFFF0000) | (tx & 0xFF) << 8 | (tx >> 8) & 0xFF


class MockLink:
    """Plays the adapter: takes command text, returns the reply text."""

    def __init__(self, dynamic: bool = False):
        self.dynamic = dynamic  # False: fixed values (tests)
        self.priority, self.header = 0x18, 0xDB33F1
        self.reply = ""
        self.t0 = time.monotonic()
        self.port = None

    def close(self) -> None:
        pass

    def read_prompt(self, timeout: float) -> str:
        return self.reply + "\r\r"  # like SerialLink: the ">" prompt is stripped

    def write(self, data: bytes) -> None:
        text = data.decode().strip().upper()
        if text.startswith("AT"):
            self.reply = self._at(text[2:])
            return
        lines = self._answer(bytes.fromhex(text))
        self.reply = "\r".join(lines) if lines else "NO DATA"

    def _at(self, c: str) -> str:
        if c == "Z":
            return "\r\rELM327 v1.4b (mock)"
        if c == "RV":
            return "14.2V" if not self.dynamic else f"{14.2 + random.uniform(-0.1, 0.1):.1f}V"
        if c.startswith("CP"):
            self.priority = int(c[2:], 16)
        elif c.startswith("SH"):
            self.header = int(c[2:], 16)
        return "OK"

    # -- the simulated car ---------------------------------------------------------
    def _state(self) -> dict:
        t = time.monotonic() - self.t0 if self.dynamic else 0.0
        soc = min(99.6, 74.0 + t * 0.03)  # FAKE: charging, about 6.6 kW worth
        base = 3.70 + 0.0039 * soc
        jitter = (lambda: random.uniform(-0.0015, 0.0015)) if self.dynamic else (lambda: 0.0)
        cells = [round(base + OFFSETS[i] + jitter(), 3) for i in range(CELLS)]
        current = -15.4 if self.dynamic and soc < 99.6 else 0.5  # FAKE: amps, negative = charging
        ah_in = 12000.0 + (t * 15.4 / 3600 if self.dynamic else 0)  # FAKE lifetime counter
        return {"soc": soc, "cells": cells, "current": current, "ah_in": ah_in}

    def _answer(self, req: bytes) -> list[str]:
        tx = self.priority << 24 | self.header
        if tx not in MODULES:
            return []  # nobody there; generic OBD (18DB33F1 / 7DF) gets no answer either
        rx = _reply_id(tx)
        if req == b"\x3E\x00":
            return _iso_tp(rx, b"\x7E\x00")
        if req[:3] == b"\x19\x02\xFF":  # FAKE: one stored, inactive code at the battery controller
            return _iso_tp(rx, b"\x59\x02\xFF" + (b"\x0A\xA6\x00\x28" if tx == BMS else b""))
        if req[:2] in (b"\x19\x04", b"\x19\x06"):
            return _iso_tp(rx, b"\x59" + req[1:5] + b"\x01\x02\x03" if tx == BMS else b"\x7F\x19\x31")
        if req[0] == 0x22 and len(req) == 3:
            data = self._did(tx, (req[1] << 8) | req[2])
            return _iso_tp(rx, b"\x62" + req[1:3] + data if data is not None else b"\x7F\x22\x31")
        return _iso_tp(rx, bytes([0x7F, req[0], 0x11]))

    def _did(self, tx: int, did: int) -> bytes | None:
        if did == 0xF190 and tx == BMS:
            return b"ZFAFAKEVIN0000000"  # FAKE VIN
        if tx != BMS:
            return None
        s = self._state()
        mv = [round(v * 1000) for v in s["cells"]]
        if 0xA100 <= did <= 0xA108:  # 8 blocks of 12 cells, then 1
            block = mv[(did - 0xA100) * 12:(did - 0xA100) * 12 + 12]
            return b"".join(v.to_bytes(2, "big") for v in block)
        if did == 0xA602:
            return bytes([round(s["soc"] / 0.4)])
        if did == 0xA012:  # current twice (the car sends two close readings)
            return round(s["current"] * 20).to_bytes(2, "big", signed=True) * 2
        if did == 0xA625:
            return round(CAPACITY_AH * 10).to_bytes(2, "big")
        if did == 0xA615:
            return round(s["ah_in"] * 10).to_bytes(3, "big")
        if did == 0xA616:
            return round((s["ah_in"] - 150) * 10).to_bytes(3, "big")
        if did == 0xA200:
            return bytes(60 + (i % 3) for i in range(18))  # FAKE: 20-22 C
        if did in (0xA609, 0xA60A, 0xA60B):
            v = {0xA609: max(mv), 0xA60A: min(mv), 0xA60B: round(sum(mv) / len(mv))}[did]
            return v.to_bytes(2, "big")
        return None
