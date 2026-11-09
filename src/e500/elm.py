"""ELM327-compatible adapter over a serial port (USB, or classic Bluetooth SPP).

The adapter speaks text: we send "0100\\r", it answers lines of hex and ends with a
">" prompt. Settings used here:
- ATE0 echo off, ATL0 no linefeeds, ATS0 no spaces, ATH1 show CAN IDs.
- ATSP7 = ISO 15765-4, 29-bit IDs at 500 kbit/s: the 500e's modules use 29-bit
  addresses (18DA<module>F1). ATSP6 (11-bit) is there for other cars.
- ATCAF1 (default): the adapter adds the ISO-TP length byte to what we send.

With headers on, every reply line is one raw CAN frame: "<id><data>", data
including the ISO-TP PCI byte. An 11-bit ID prints as 3 hex digits, a 29-bit one as
8, so 11-bit lines have odd length and 29-bit lines even. We reassemble multi-frame replies ourselves, per
CAN ID, so answers from several modules never mix.

The serial timeout is set once at open and never changed: pyserial reconfigures
the port on every `timeout` assignment.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Protocol


class AdapterError(Exception):
    """The adapter or the bus reported a problem."""


class NoData(AdapterError):
    """Nobody answered (ELM "NO DATA")."""


# Adapter messages that end a command without data.
_ERRORS = ("?", "CAN ERROR", "BUFFER FULL", "BUS INIT", "BUS ERROR", "FB ERROR",
           "DATA ERROR", "STOPPED", "UNABLE TO CONNECT", "ERR")


class Link(Protocol):
    def write(self, data: bytes) -> None: ...
    def read_prompt(self, timeout: float) -> str: ...
    def close(self) -> None: ...


class SerialLink:
    """Raw text I/O with the adapter: write a command, read up to the ">" prompt."""

    def __init__(self, port: str, baud: int = 38400):
        import serial

        self.port = port
        self.ser = serial.Serial(port, baud, timeout=0.05)  # never reassigned

    def write(self, data: bytes) -> None:
        self.ser.reset_input_buffer()
        self.ser.write(data)

    def read_prompt(self, timeout: float) -> str:
        buf = bytearray()
        deadline = time.monotonic() + timeout
        while not buf.endswith(b">") and time.monotonic() < deadline:
            # read what's there (or wait for one byte), so a reply returns as soon as
            # the prompt arrives instead of after the 50 ms port timeout
            buf += self.ser.read(self.ser.in_waiting or 1)
        if not buf.endswith(b">"):
            raise AdapterError(f"no prompt from adapter (got {buf[-60:]!r})")
        return buf[:-1].decode("ascii", "replace")

    def close(self) -> None:
        self.ser.close()


@dataclass
class Frame:
    can_id: int
    data: bytes  # the ISO-TP payload, PCI bytes removed


def reassemble(lines: list[str]) -> list[Frame]:
    """ISO-TP messages from header-on reply lines, in order of first frame.

    Single frame: PCI 0N (N bytes). First frame: 1N NN (12-bit length), then
    consecutive frames 2N with N counting 1, 2, ... 15, 0, 1. A message is only
    returned whole: a missing or out-of-order consecutive frame, or fewer bytes than
    the first frame announced, drops it, so a lost frame can't turn into a short but
    plausible-looking reading. Lines that aren't hex frames are ignored.
    """
    msgs: dict[int, list] = {}  # can id -> [expected length, data, next sequence number or None]
    order: list[int] = []
    for line in lines:
        if len(line) < 5 or any(c not in "0123456789ABCDEF" for c in line):
            continue
        n = 3 if len(line) % 2 else 8  # 11-bit IDs: 3 hex digits; 29-bit: 8
        if len(line) <= n:
            continue
        can_id, raw = int(line[:n], 16), bytes.fromhex(line[n:])
        if not raw:
            continue
        kind = raw[0] >> 4
        if kind == 0:  # single frame
            msgs[can_id] = [raw[0] & 0xF, bytearray(raw[1:1 + (raw[0] & 0xF)]), None]
        elif kind == 1 and len(raw) >= 2:  # first frame
            msgs[can_id] = [((raw[0] & 0xF) << 8) | raw[1], bytearray(raw[2:]), 1]
        elif kind == 2 and can_id in msgs and msgs[can_id][2] is not None:  # consecutive frame
            m = msgs[can_id]
            if raw[0] & 0xF != m[2]:  # a frame went missing or arrived out of order
                m[2] = None
                m[0] = -1  # poison: never returned
                continue
            m[1].extend(raw[1:])
            m[2] = (m[2] + 1) & 0xF
        else:
            continue
        if can_id not in order:
            order.append(can_id)
    return [Frame(i, bytes(msgs[i][1][:msgs[i][0]])) for i in order
            if msgs[i][0] >= 0 and len(msgs[i][1]) >= msgs[i][0]]


class Elm327:
    INIT = ("ATE0", "ATL0", "ATS0", "ATH1", "ATAT1")

    def __init__(self, link: Link, trace: str | None = None, protocol: str = "7"):
        self.link = link
        self.protocol = protocol  # "7": CAN 29-bit 500k (500e), "6": CAN 11-bit 500k
        self.trace = open(trace, "a", buffering=1) if trace else None
        self.t0 = time.monotonic()
        self.header: int | None = None

    def _log(self, **rec) -> None:
        if self.trace:
            self.trace.write(json.dumps({"t": round(time.monotonic() - self.t0, 4), **rec}) + "\n")

    def cmd(self, text: str, timeout: float = 5.0) -> list[str]:
        """Send one command, return the reply lines (echo, prompts, "SEARCHING..." removed)."""
        self.link.write(text.encode() + b"\r")
        try:
            raw = self.link.read_prompt(timeout)
        except AdapterError as e:
            self._log(tx=text, error=str(e))
            raise
        lines = [ln.strip().replace(" ", "") for ln in raw.replace("\n", "\r").split("\r")]
        lines = [ln for ln in lines if ln and ln != text and not ln.startswith("SEARCHING")]
        self._log(tx=text, rx=lines)
        return lines

    def init(self) -> str:
        """Reset and configure. Returns the adapter's ID string."""
        ident = " ".join(self.cmd("ATZ", timeout=5.0)) or "unknown adapter"
        for c in self.INIT + (f"ATSP{self.protocol}",):
            if self.cmd(c) != ["OK"]:
                raise AdapterError(f"{c} rejected; is this an ELM327-compatible adapter?")
        self.header = None
        return ident

    def set_target(self, tx_id: int) -> None:
        """Address requests to one module (11- or 29-bit CAN ID). Flow control frames
        for its multi-frame replies go to the same ID, which cheap clones don't always
        default to."""
        if self.header == tx_id:
            return
        if tx_id > 0x7FF:  # 29-bit: priority byte via ATCP, the other three via ATSH
            self.cmd(f"ATCP{tx_id >> 24:02X}")
            self.cmd(f"ATSH{tx_id & 0xFFFFFF:06X}")
            fc = f"{tx_id:08X}"
        else:
            self.cmd(f"ATSH{tx_id:03X}")
            fc = f"{tx_id:03X}"
        for c in (f"ATFCSH{fc}", "ATFCSD300000", "ATFCSM1"):
            self.cmd(c)  # not every clone knows ATFC; harmless if it answers "?"
        self.header = tx_id

    def request(self, tx_id: int, data: bytes, timeout: float = 5.0) -> list[Frame]:
        """Send one request, return every module's reassembled reply."""
        self.set_target(tx_id)
        lines = self.cmd(data.hex().upper(), timeout)
        if any(ln.startswith("NODATA") for ln in lines):
            raise NoData(f"no reply to {data.hex()} at {tx_id:X}")
        for ln in lines:
            if any(ln.startswith(e.replace(" ", "")) for e in _ERRORS):
                raise AdapterError(f"adapter said {ln!r} to {data.hex()}")
        frames = reassemble(lines)
        if not frames:
            raise AdapterError(f"unreadable reply to {data.hex()}: {lines}")
        return frames

    def battery_volts(self) -> float | None:
        """12 V supply at the OBD port (ATRV)."""
        lines = self.cmd("ATRV")
        try:
            return float(lines[0].rstrip("V"))
        except (IndexError, ValueError):
            return None

    def close(self) -> None:
        self.link.close()
        if self.trace:
            self.trace.close()
