"""Fanout encodes each admitted frame once, then hands the same wire to every peer."""

from __future__ import annotations

from tests.waiting import wait_until
from tui_gateway import transport as transport_mod
from tui_gateway.transport import FanoutTransport, serialize_frame


class _WireSink:
    def __init__(self) -> None:
        self.lines: list[str] = []

    def write_wire(self, line: str) -> bool:
        self.lines.append(line if line.endswith("\n") else line + "\n")
        return True

    def write(self, obj: dict) -> bool:
        raise AssertionError("fanout must not re-encode via write() when write_wire exists")

    def close(self) -> None:
        return None


def _delta(text: str) -> dict:
    return {
        "jsonrpc": "2.0",
        "method": "event",
        "params": {"type": "message.delta", "session_id": "s", "payload": {"text": text}},
    }


def test_one_encode_reaches_every_subscriber(monkeypatch):
    encodes: list[str] = []
    real = transport_mod.serialize_frame

    def counted(obj, peer, log):
        encodes.append(peer)
        return real(obj, peer, log)

    monkeypatch.setattr(transport_mod, "serialize_frame", counted)
    sinks = [_WireSink(), _WireSink(), _WireSink()]
    fanout = FanoutTransport(*sinks)
    frame = _delta("hello")
    assert fanout.write(frame)
    wait_until(lambda: all(len(sink.lines) == 1 for sink in sinks), timeout=5, what="all peers got the frame")
    assert encodes == ["fanout"]
    expected = serialize_frame(frame, "fanout", transport_mod.logger) + "\n"
    assert [sink.lines[0] for sink in sinks] == [expected, expected, expected]
    fanout.close()
