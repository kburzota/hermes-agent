"""stdlib fallback for the optional orjson wire encoder."""

from __future__ import annotations

import json

from tui_gateway.json_codec import dumps, dumps_compact_bytes


def test_dumps_round_trips_stream_events():
    frame = {
        "jsonrpc": "2.0",
        "method": "event",
        "params": {"type": "message.delta", "session_id": "s", "payload": {"text": "café"}},
    }
    assert json.loads(dumps(frame)) == frame


def test_compact_bytes_match_stdlib_when_orjson_is_absent(monkeypatch):
    import tui_gateway.json_codec as codec

    monkeypatch.setattr(codec, "_orjson", None)
    params = {"type": "message.delta", "session_id": "s", "payload": {"text": "x"}}
    expected = json.dumps(params, ensure_ascii=False, separators=(",", ":")).encode(
        "utf-8", errors="surrogatepass"
    )
    assert codec.dumps_compact_bytes(params) == expected


def test_dumps_falls_back_for_unserializable_then_raises():
    class _Nope:
        pass

    try:
        dumps({"x": _Nope()})
    except (TypeError, ValueError):
        return
    raise AssertionError("unserializable payload must still raise")
