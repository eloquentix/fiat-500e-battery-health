"""UDS (ISO 14229) over the adapter — read-only services only.

Used: 0x22 ReadDataByIdentifier, 0x3E TesterPresent, 0x19 ReadDTCInformation (read
sub-functions only), 0x01 (OBD current data). Never sent: anything that writes, clears,
codes, resets or unlocks (0x11, 0x14, 0x27, 0x2E, 0x2F, 0x31, 0x34-0x37, 0x3D, 0x85).
`Client.request` enforces this: anything else raises before it reaches the adapter.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .elm import AdapterError, Elm327, NoData

# service -> allowed sub-functions (None: no sub-function byte to check)
READ_ONLY = {0x22: None, 0x3E: {0x00}, 0x01: None, 0x19: {0x01, 0x02, 0x04, 0x06, 0x0A}}

FUNCTIONAL = 0x7DF  # generic OBD broadcast (11-bit; the 500e doesn't answer it)
NRC = {0x10: "generalReject", 0x11: "serviceNotSupported", 0x12: "subFunctionNotSupported",
       0x13: "incorrectMessageLength", 0x22: "conditionsNotCorrect",
       0x31: "requestOutOfRange", 0x33: "securityAccessDenied", 0x78: "responsePending",
       0x7E: "subFunctionNotSupportedInActiveSession",
       0x7F: "serviceNotSupportedInActiveSession"}


class NegativeResponse(AdapterError):
    def __init__(self, service: int, code: int, can_id: int):
        self.service, self.code, self.can_id = service, code, can_id
        super().__init__(f"{can_id:X} rejected 0x{service:02X}: 0x{code:02X} "
                         f"{NRC.get(code, 'unknown')}")


def module_id(target: int) -> int:
    """29-bit request ID for a module (ISO 15765 normal fixed addressing, tester F1)."""
    return 0x18DA00F1 | target << 8


def rx_id(tx_id: int) -> int:
    """Usual reply ID for a physical request: 11-bit tx + 8 (7E0 -> 7E8); 29-bit
    18DA<target><source> -> 18DA<source><target>."""
    if tx_id > 0x7FF:
        return (tx_id & 0xFFFF0000) | (tx_id & 0xFF) << 8 | (tx_id >> 8) & 0xFF
    return tx_id + 8


@dataclass
class Client:
    elm: Elm327
    seen: dict[int, int] = field(default_factory=dict)  # tx id -> reply id actually used

    def request(self, tx_id: int, data: bytes) -> bytes:
        """One physical request; the positive reply payload (SID + 0x40 first).

        "Response pending" (NRC 0x78) is not answered by resending: the adapter keeps
        listening for the final reply itself. If all we got was "pending", the reply
        came too late for the adapter's timeout, and that's an error, not a retry.
        """
        sid = data[0]
        subs = READ_ONLY.get(sid, set())
        if sid not in READ_ONLY or (subs is not None and data[1:2] and data[1] not in subs):
            raise ValueError(f"refusing to send {data[:2].hex()}: not a read-only service")
        frames = self.elm.request(tx_id, data)
        want = self.seen.get(tx_id, rx_id(tx_id))
        mine = [f for f in frames if f.can_id == want] or frames
        for f in mine:
            d = f.data
            if d[:1] == bytes([sid + 0x40]):
                self.seen[tx_id] = f.can_id
                return d
            if len(d) >= 3 and d[0] == 0x7F and d[1] == sid and d[2] != 0x78:
                raise NegativeResponse(sid, d[2], f.can_id)
        if any(f.data[:1] == b"\x7F" and f.data[2:3] == b"\x78" for f in mine):
            raise AdapterError(f"{tx_id:X} said 'response pending' to {data.hex()} and the final "
                               "reply didn't arrive in time")
        raise AdapterError(f"{tx_id:X} sent no answer to {data.hex()}: {[f.data.hex() for f in mine]}")

    def read_did(self, tx_id: int, did: int) -> bytes:
        """ReadDataByIdentifier: the data after the echoed DID."""
        reply = self.request(tx_id, bytes([0x22, did >> 8, did & 0xFF]))
        if reply[1:3] != bytes([did >> 8, did & 0xFF]):
            raise AdapterError(f"{tx_id:X} answered DID {reply[1:3].hex()} to {did:04x}")
        return reply[3:]

    def obd(self, pid: int) -> dict[int, bytes]:
        """Generic OBD mode 01 to every module: {reply id: data after the PID}."""
        frames = self.elm.request(FUNCTIONAL, bytes([0x01, pid]))
        return {f.can_id: f.data[2:] for f in frames if f.data[:2] == bytes([0x41, pid])}

    def ping(self, tx_id: int) -> int | None:
        """Is a module listening at tx_id? Returns its reply ID. TesterPresent with
        "suppress nothing"; any answer, even a rejection, proves a module."""
        try:
            frames = self.elm.request(tx_id, b"\x3E\x00", timeout=1.0)
        except NoData:
            return None
        return frames[0].can_id
