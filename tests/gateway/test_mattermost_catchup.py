"""Mattermost adapter: posts made while the WebSocket was down (or the gateway restarting) are replayed on connect."""
import json
import time
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import PlatformConfig

BOT = "bot_id"
T0 = int(time.time() * 1000) - 3_600_000  # an hour ago: inside the 24 h catch-up window


def _adapter(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    from plugins.platforms.mattermost.adapter import MattermostAdapter
    a = MattermostAdapter(PlatformConfig(enabled=True, token="t", extra={"url": "https://mm.example.com"}))
    a._bot_user_id, a._bot_username = BOT, "bot"
    a.handle_message = AsyncMock()
    return a


def _post(pid, at, user="kyle_id", msg="hi", **kw):
    return {"id": pid, "user_id": user, "channel_id": "dm1", "message": msg, "create_at": at, "update_at": at, **kw}


def _serve(a, channels, posts):
    """Fake the server: one team, `channels`, and `posts` (a list) returned for any channel posts?since= call."""
    seen = []

    async def api_get(path):
        seen.append(path)
        if path == "users/me/teams":
            return [{"id": "team1"}]
        if path.startswith("users/me/teams/"):
            return channels
        if path.startswith("channels/"):
            return {"posts": {p["id"]: p for p in posts}}
        if path.startswith("users/"):
            return {"username": "kyle"}
        return {}
    a._api_get = api_get
    return seen


@pytest.mark.asyncio
async def test_replays_only_posts_after_the_watermark_oldest_first(monkeypatch, tmp_path):
    a = _adapter(monkeypatch, tmp_path)
    a._advance_watermark(T0 + 1000)
    _serve(a, [{"id": "dm1", "type": "D", "last_post_at": T0 + 3000}], [
        _post("old", T0 + 900), _post("edited", T0 + 500, update_at=T0 + 2500), _post("gone", T0 + 2200, delete_at=T0 + 2300),
        _post("own", T0 + 2100, user=BOT), _post("b", T0 + 2000, msg="second"), _post("a", T0 + 1500, msg="first")])
    await a._catch_up()
    assert [c.args[0].message_id for c in a.handle_message.call_args_list] == ["a", "b"]
    first = a.handle_message.call_args_list[0].args[0]
    assert (first.text, first.source.chat_type, first.source.user_name) == ("first", "dm", "kyle")
    assert a._last_seen_ms == T0 + 2100  # the bot's own newer post is skipped but still moves the watermark


@pytest.mark.asyncio
async def test_does_not_fetch_channels_with_nothing_new(monkeypatch, tmp_path):
    a = _adapter(monkeypatch, tmp_path)
    a._advance_watermark(T0 + 5000)
    seen = _serve(a, [{"id": "dm1", "type": "D", "last_post_at": T0 + 4000}], [_post("x", T0 + 4000)])
    await a._catch_up()
    assert not a.handle_message.called and not any(p.startswith("channels/") for p in seen)


@pytest.mark.asyncio
async def test_first_run_starts_the_clock_without_replaying(monkeypatch, tmp_path):
    a = _adapter(monkeypatch, tmp_path)
    seen = _serve(a, [{"id": "dm1", "type": "D", "last_post_at": 9}], [_post("x", 9)])
    await a._catch_up()
    assert seen == [] and not a.handle_message.called and a._last_seen_ms > 0


@pytest.mark.asyncio
async def test_watermark_survives_a_restart(monkeypatch, tmp_path):
    a = _adapter(monkeypatch, tmp_path)
    a._advance_watermark(T0 + 1000)
    await a._handle_ws_event({"event": "posted", "data": {"post": json.dumps(_post("live", T0 + 4242)), "channel_type": "D"}})
    b = _adapter(monkeypatch, tmp_path)  # a new process: nothing in memory
    assert b._load_watermark() == T0 + 4242
    _serve(b, [{"id": "dm1", "type": "D", "last_post_at": T0 + 6000}], [_post("live", T0 + 4242), _post("missed", T0 + 5000)])
    await b._catch_up()
    assert [c.args[0].message_id for c in b.handle_message.call_args_list] == ["missed"]


@pytest.mark.asyncio
async def test_a_post_already_delivered_live_is_not_replayed_twice(monkeypatch, tmp_path):
    a = _adapter(monkeypatch, tmp_path)
    a._advance_watermark(T0 + 1000)
    await a._handle_ws_event({"event": "posted", "data": {"post": json.dumps(_post("p", T0 + 2000)), "channel_type": "D"}})
    a._last_seen_ms = T0 + 1000  # e.g. the watermark file could not be written
    _serve(a, [{"id": "dm1", "type": "D", "last_post_at": T0 + 2000}], [_post("p", T0 + 2000)])
    await a._catch_up()
    assert a.handle_message.call_count == 1


@pytest.mark.asyncio
async def test_connect_runs_catch_up_and_survives_its_failure(monkeypatch, tmp_path):
    a = _adapter(monkeypatch, tmp_path)
    ws = MagicMock()
    ws.send_json = AsyncMock()
    ws.__aiter__.return_value = iter(())  # the server closes at once: the listener ends
    a._session = MagicMock()
    a._session.ws_connect = AsyncMock(return_value=ws)
    a._catch_up = AsyncMock(side_effect=RuntimeError("server unreachable"))
    await a._ws_connect_and_listen()  # must return normally so _ws_loop reconnects
    assert a._catch_up.await_count == 1
